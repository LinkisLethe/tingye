import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.common import UserError
from app.settings import defaults
from app.store import Store
from app.worker import run


class AttemptFencingTests(unittest.TestCase):
    def test_remote_execution_survives_local_ui_restart(self):
        claim=self.store.claim(self.job['id'])
        self.store.update(claim['id'],execution_backend='ssh')
        self.store.recover()
        current=self.store.get(claim['id'])
        self.assertEqual(current['status'],'running')
        self.assertFalse(current['cancel_requested'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingye-fencing-")
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.store = Store(self.root / "data")
        self.settings = {**defaults(), "vault_path": str(self.vault), "cpu_threads": 1}
        self.job = self.store.create("BV1xx411c7mD", self.settings)
        self.video = {"bvid": "BV1xx411c7mD", "cid": 1, "title": "测试", "uploader": "测试",
                      "page": 1, "duration": 1, "url": "https://www.bilibili.com/video/BV1xx411c7mD/"}

    def tearDown(self):
        self.temp.cleanup()

    def test_concurrent_claim_has_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: self.store.claim(self.job["id"]), range(2)))
        self.assertEqual(sum(c is not None for c in claims), 1)
        winner = next(c for c in claims if c)
        self.assertEqual(winner["status"], "running")
        self.assertEqual(len(winner["attempt_id"]), 32)

    def test_old_attempt_cannot_update_a_new_attempt(self):
        old = self.store.claim(self.job["id"])
        self.store.recover()
        self.store.update(self.job["id"], status="queued", cancel_requested=False)
        new = self.store.claim(self.job["id"])
        changed = self.store.update_if(self.job["id"], {"status": "running", "attempt_id": old["attempt_id"]},
                                       status="completed", progress=100)
        self.assertIsNone(changed)
        self.assertEqual(self.store.get(self.job["id"]), new)

    def test_recovery_stops_worker_before_next_state_write(self):
        attempt = self.store.claim(self.job["id"])
        def fetch(*args, **kwargs):
            self.store.recover()
            return self.video
        with patch("app.media.fetch_video", side_effect=fetch), patch("app.exporter.find_existing_note") as existing:
            self.assertEqual(run(self.store.directory, self.job["id"], attempt["attempt_id"]), 0)
        existing.assert_not_called()
        current = self.store.get(self.job["id"])
        self.assertEqual(current["status"], "interrupted")
        self.assertTrue(current["cancel_requested"])
        self.assertEqual(current["stage"], "interrupted")

    def test_cancel_never_mutates_completed_job(self):
        claimed = self.store.claim(self.job["id"])
        finished = self.store.update_if(self.job["id"], {"attempt_id": claimed["attempt_id"]}, status="completed", result={"note_path": "saved"})
        self.assertEqual(self.store.request_cancel(self.job["id"]), finished)
        self.assertEqual(self.store.get(self.job["id"]), finished)

    def test_cancel_running_sets_flag_and_queued_finishes_immediately(self):
        queued = self.store.request_cancel(self.job["id"])
        self.assertEqual(queued["status"], "cancelled")
        other = self.store.create("other", self.settings)
        self.store.claim(other["id"])
        running = self.store.request_cancel(other["id"])
        self.assertEqual(running["status"], "running")
        self.assertTrue(running["cancel_requested"])

    def test_stale_worker_never_touches_new_attempt_scratch(self):
        old = self.store.claim(self.job["id"])
        self.store.recover()
        self.store.update(self.job["id"], status="queued", cancel_requested=False)
        new = self.store.claim(self.job["id"])
        current_scratch = self.store.directory / "work" / new["attempt_id"]
        current_scratch.mkdir(parents=True)
        audio = current_scratch / "audio.m4a"
        audio.write_bytes(b"new attempt")
        with patch("app.media.fetch_video") as fetch:
            self.assertEqual(run(self.store.directory, self.job["id"], old["attempt_id"]), 0)
        fetch.assert_not_called()
        self.assertEqual(audio.read_bytes(), b"new attempt")
        self.assertEqual(self.store.get(self.job["id"]), new)

    def test_progress_from_recovered_worker_is_rejected(self):
        claimed = self.store.claim(self.job["id"])
        def download(video, target, progress, check_cancel):
            self.store.recover()
            progress(80, "late progress")
            self.fail("Recovered worker continued after a cancellation checkpoint")
        with patch("app.media.fetch_video", return_value=self.video), patch("app.exporter.find_existing_note", return_value=None), \
             patch("app.media.fetch_subtitles", return_value=None), patch("app.media.download_audio", side_effect=download):
            self.assertEqual(run(self.store.directory, self.job["id"], claimed["attempt_id"]), 0)
        current = self.store.get(self.job["id"])
        self.assertEqual(current["status"], "interrupted")
        self.assertNotEqual(current["message"], "late progress")

    def test_late_export_completion_cannot_complete_new_attempt(self):
        claimed = self.store.claim(self.job["id"])
        new_attempt = []
        transcript = {"segments": [{"start": 0, "end": 1, "text": "测试"}]}
        def publish(*args, **kwargs):
            self.store.recover()
            self.store.update(self.job["id"], status="queued", cancel_requested=False)
            new_attempt.append(self.store.claim(self.job["id"]))
            return {"note_path": "old-attempt-result", "duplicate": False}
        with patch("app.media.fetch_video", return_value=self.video), patch("app.exporter.find_existing_note", return_value=None), \
             patch("app.media.fetch_subtitles", return_value=transcript), patch("app.media.download_audio", return_value=None), \
             patch("app.exporter.export_note", side_effect=publish):
            self.assertEqual(run(self.store.directory, self.job["id"], claimed["attempt_id"]), 0)
        self.assertEqual(self.store.get(self.job["id"]), new_attempt[0])

    def test_late_error_cannot_fail_new_attempt(self):
        claimed = self.store.claim(self.job["id"])
        new_attempt = []
        def fetch(*args, **kwargs):
            self.store.recover()
            self.store.update(self.job["id"], status="queued", cancel_requested=False)
            new_attempt.append(self.store.claim(self.job["id"]))
            raise UserError("old attempt error")
        with patch("app.media.fetch_video", side_effect=fetch):
            self.assertEqual(run(self.store.directory, self.job["id"], claimed["attempt_id"]), 1)
        self.assertEqual(self.store.get(self.job["id"]), new_attempt[0])


if __name__ == "__main__":
    unittest.main()
