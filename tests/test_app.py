import json
from pathlib import Path
import tempfile
import threading
import unittest
import os
import subprocess
import sys
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.common import Cancelled, UserError
from app.server import Application, handler_for, QueueManager
from app.settings import defaults, validate_settings
from app.store import Store, public_job
from app.worker import run
from http.server import ThreadingHTTPServer


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingye-app-test-")
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.settings = {**defaults(), "vault_path": str(self.vault), "subfolder": "B站摘录", "cpu_threads": 1}
        self.backup_patch = patch("app.settings.data_directory", return_value=self.root / "data")
        self.backup_patch.start()

    def tearDown(self):
        self.backup_patch.stop()
        self.temp.cleanup()

    def test_settings_refuse_parent_hidden_absolute_and_reserved_destinations(self):
        for subfolder in ("../outside", ".obsidian", "abc/../../outside", "C:/Windows", "NUL", "folder/COM1.txt", "foo.", "a//b"):
            with self.subTest(subfolder=subfolder), self.assertRaises(UserError):
                validate_settings({**self.settings, "subfolder": subfolder})
        self.assertEqual(validate_settings(self.settings)["subfolder"], "B站摘录")
        self.assertEqual(validate_settings({**self.settings, "subfolder": ""})["subfolder"], "")

    def test_settings_require_existing_vault_and_validate_model(self):
        for delta in ({"vault_path": str(self.root)}, {"model": "cloud-service"}, {"cpu_threads": 0}, {"cpu_threads": True}, {"keep_audio": "false"}):
            with self.subTest(delta=delta), self.assertRaises(UserError):
                validate_settings({**self.settings, **delta})

    def test_queue_persists_settings_snapshot_and_recovery(self):
        store = Store(self.root / "data")
        job = store.create("BV1xx411c7mD", self.settings)
        self.settings["subfolder"] = "different"
        store.update(job["id"], status="running")
        reopened = Store(self.root / "data")
        reopened.recover()
        recovered = reopened.get(job["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["settings"]["subfolder"], "B站摘录")
        self.assertNotIn("settings", public_job(recovered))

    def test_queued_cancel_and_retry_preserve_original_destination(self):
        app = Application(self.root / "data")
        app.settings = dict(self.settings)
        job = app.post("/api/jobs", {"links": "BV1xx411c7mD\nBV1xx411c7mD"})["jobs"][0]
        self.assertEqual(len(app.store.all()), 1)
        self.assertEqual(app.post(f"/api/jobs/{job['id']}/cancel", {})["job"]["status"], "cancelled")
        app.settings["subfolder"] = "new"
        self.assertEqual(app.post(f"/api/jobs/{job['id']}/retry", {})["job"]["status"], "queued")
        self.assertEqual(app.store.get(job["id"])["settings"]["subfolder"], "B站摘录")

    def test_worker_publishes_real_files_and_repeat_overwrites(self):
        store = Store(self.root / "data")
        job = store.create("BV1xx411c7mD", self.settings)
        store.update(job["id"], status="running")
        video = {"bvid": "BV1xx411c7mD", "cid": 36857516014, "title": "测试视频", "uploader": "测试", "page": 1, "duration": 2, "url": "https://www.bilibili.com/video/BV1xx411c7mD/"}
        transcript = {"segments": [{"start": 0, "end": 1, "text": "测试内容"}], "source": "local_asr", "language": "zh", "model": "small", "duration": 2}
        def download(video, target, progress, check_cancel):
            target.write_bytes(b"test audio")
            return target
        with patch("app.media.fetch_video", return_value=video), patch("app.media.fetch_subtitles", return_value=None), patch("app.media.download_audio", side_effect=download) as dl, patch("app.asr.transcribe_audio", return_value=transcript) as asr:
            self.assertEqual(run(store.directory, job["id"]), 0)
            done = store.get(job["id"])
            self.assertEqual(done["status"], "completed")
            note = Path(done["result"]["note_path"])
            self.assertTrue(note.is_file())
            self.assertIn("测试内容", note.read_text(encoding="utf-8"))
            self.assertFalse((store.directory / "work" / job["id"] / "audio.m4a").exists())
            note.write_text(note.read_text(encoding="utf-8") + "用户改过的笔记", encoding="utf-8")
            repeat = store.create("BV1xx411c7mD", self.settings)
            store.update(repeat["id"], status="running")
            self.assertEqual(run(store.directory, repeat["id"]), 0)
            self.assertEqual(dl.call_count, 2)
            self.assertEqual(asr.call_count, 2)
            self.assertNotIn("用户改过的笔记", note.read_text(encoding="utf-8"))
            self.assertEqual(len(list(note.parent.glob('*.md'))), 1)

    def test_worker_cancellation_does_not_publish(self):
        store = Store(self.root / "data")
        job = store.create("BV1xx411c7mD", self.settings)
        store.update(job["id"], status="running", cancel_requested=True)
        with patch("app.media.fetch_video") as fetch:
            self.assertEqual(run(store.directory, job["id"]), 0)
            fetch.assert_not_called()
        self.assertEqual(store.get(job["id"])["status"], "cancelled")
        self.assertFalse((self.vault / "B站摘录").exists())

    def test_cleanup_only_removes_application_owned_audio(self):
        store = Store(self.root / "data")
        manager = QueueManager(store)
        ident = "a" * 32
        folder = store.directory / "work" / ident
        folder.mkdir(parents=True)
        (folder / "audio.m4a").write_bytes(b"x")
        part = folder / ("audio.m4a." + "b" * 32 + ".part")
        part.write_bytes(b"x")
        (folder / "unrelated.txt").write_text("keep")
        manager.clean_work(ident)
        self.assertFalse(part.exists())
        self.assertFalse((folder / "audio.m4a").exists())
        self.assertEqual((folder / "unrelated.txt").read_text(), "keep")

    def test_recovery_cleans_only_matching_export_staging(self):
        store = Store(self.root / "data")
        manager = QueueManager(store)
        ident = "a" * 32
        work = store.directory / "work" / ident
        work.mkdir(parents=True)
        base = self.vault / "B站摘录"
        staging = base / ".tingye-export-test"
        staging.mkdir(parents=True)
        (staging / ".tingye-owner.json").write_text(json.dumps({"owner": "tingye", "attempt_id": ident}), encoding="utf-8")
        (staging / "partial.txt").write_text("partial")
        registry = work / "export-staging.json"
        registry.write_text(json.dumps({"owner": "tingye", "attempt_id": ident, "path": str(staging)}), encoding="utf-8")
        manager.clean_work(ident, {"settings": self.settings})
        self.assertFalse(staging.exists())
        self.assertFalse(registry.exists())
        manager.guard.close()

    @unittest.skipUnless(os.name == "nt", "Windows process guard")
    def test_server_guard_terminates_only_its_assigned_child(self):
        from app.process_guard import ChildProcessGuard
        guard = ChildProcessGuard()
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(20)"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            if not guard.attach(process):
                self.skipTest("Windows job assignment unavailable")
            guard.close()
            process.wait(timeout=4)
            self.assertIsNotNone(process.returncode)
        finally:
            guard.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=4)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingye-http-test-")
        self.app = Application(Path(self.temp.name))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.app))
        self.app.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.app.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, route, headers=None, body=None):
        return urlopen(Request(self.url + route, data=body, headers=headers or {}), timeout=5)

    def test_state_requires_local_token(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/state")
        self.assertEqual(error.exception.code, 403)
        with self.request("/api/state", {"X-Local-Token": self.app.token}) as response:
            self.assertEqual(json.load(response)["app"]["name"], "听页")

    def test_untrusted_host_and_cross_origin_requests_rejected(self):
        for headers in ({"Host": "attacker.example"}, {"X-Local-Token": self.app.token, "Origin": "https://attacker.example", "Content-Type": "application/json"}):
            with self.assertRaises(HTTPError) as error:
                self.request("/api/jobs", headers, b'{"links":"BV1xx411c7mD"}')
            self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.app.store.all(), [])

    def test_static_resources_and_token_bootstrap(self):
        with self.request("/") as response:
            html = response.read().decode("utf-8")
            self.assertIn(self.app.token, html)
            self.assertNotIn("__LOCAL_TOKEN__", html)
        for route in ("/static/app.js", "/static/style.css"):
            with self.request(route) as response:
                self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError):
            self.request("/../app/server.py")


if __name__ == "__main__":
    unittest.main()
