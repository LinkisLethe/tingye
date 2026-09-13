"""Loopback-only web application and durable local job queue."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
import webbrowser

from . import __version__
from .common import UserError
from .settings import atomic_json, data_directory, discover_vaults, load_settings, model_status, validate_settings
from .store import Store, public_job
from .process_guard import ChildProcessGuard

PROJECT = Path(__file__).resolve().parent.parent
STATIC = PROJECT / "app" / "static"


class QueueManager:
    def __init__(self, store):
        self.store = store
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.process = None
        self.job_id = None
        self.attempt_id = None
        self.guard = ChildProcessGuard()
        self.log = None
        self.cancel_deadline = None
        self.thread = threading.Thread(target=self.loop, name="local-job-queue", daemon=True)

    def start(self):
        self.store.recover()
        for job in self.store.all():
            if job["status"] in ("interrupted", "failed", "cancelled", "completed"):
                self.clean_work(job.get("attempt_id") or job["id"], job)
        self.thread.start()

    def loop(self):
        while not self.stop_event.wait(0.25):
            with self.lock:
                if self.process is not None:
                    if self.cancel_deadline is not None and time.monotonic() >= self.cancel_deadline and self.process.poll() is None:
                        self.process.terminate()
                    if self.process.poll() is None:
                        continue
                    job = self.store.get(self.job_id)
                    if job and job["status"] == "running":
                        if job.get("cancel_requested"):
                            self.store.update(self.job_id, status="cancelled", stage="cancelled", message="已取消", error=None)
                        else:
                            self.store.update(self.job_id, status="interrupted", stage="interrupted", message="处理意外停止", error="本机处理进程已停止，可点击重试。")
                    if self.log:
                        self.log.close()
                    self.clean_work(self.attempt_id or self.job_id, job)
                    self.process, self.job_id, self.attempt_id, self.log, self.cancel_deadline = None, None, None, None, None
                job = self.store.next_queued()
                if job is None:
                    continue
                job = self.store.claim(job["id"])
                if job is None:
                    continue
                self.job_id, self.attempt_id = job["id"], job["attempt_id"]
                logs = self.store.directory / "logs"
                logs.mkdir(exist_ok=True)
                self.log = (logs / f"{self.job_id}.log").open("ab")
                try:
                    self.process = subprocess.Popen([sys.executable, "-m", "app.worker", "--data-dir", str(self.store.directory), "--job-id", self.job_id, "--attempt-id", self.attempt_id], cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                    guarded = self.guard.attach(self.process)
                    self.store.update_if(self.job_id, {"attempt_id": self.attempt_id, "status": "running"}, worker_pid=self.process.pid, process_guarded=guarded)
                except OSError:
                    self.store.update(self.job_id, status="failed", stage="failed", message="无法启动本机转写", error="请重新启动听页，再重试此任务。")
                    self.log.close()
                    self.process, self.job_id, self.attempt_id, self.log = None, None, None, None

    def cancel(self, ident):
        with self.lock:
            job = self.store.request_cancel(ident)
            if not job:
                raise UserError("没有找到这个任务。")
            if job["status"] == "running":
                self.cancel_deadline = time.monotonic() + 3 if ident == self.job_id else None
            return job

    def shutdown(self):
        self.stop_event.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                self.store.request_cancel(self.job_id)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
                job = self.store.get(self.job_id)
                if job and job["status"] == "running":
                    self.store.update(self.job_id, status="interrupted", stage="interrupted", message="听页已关闭，任务可重试", error="处理未完成，可在下次启动后重试。")
            if self.log:
                self.log.close()
            if self.job_id:
                self.clean_work(self.attempt_id or self.job_id, self.store.get(self.job_id))
            self.guard.close()

    def clean_work(self, ident, job=None):
        if not re.fullmatch(r"[0-9a-f]{32}", ident or ""):
            return
        work = (self.store.directory / "work").resolve()
        folder = (work / ident).resolve()
        if not folder.is_relative_to(work) or not folder.is_dir():
            return
        registry = folder / "export-staging.json"
        if registry.is_file() and job is not None:
            try:
                record = json.loads(registry.read_text(encoding="utf-8"))
                from .exporter import validate_destination
                vault, base = validate_destination(job["settings"])
                staging = Path(record["path"]).resolve()
                marker = staging / ".tingye-owner.json"
                owned = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
                if record.get("owner") == "tingye" and record.get("attempt_id") == ident and owned.get("owner") == "tingye" and owned.get("attempt_id") == ident and staging.is_relative_to(vault) and staging.parent == base and staging.name.startswith(".tingye-export-"):
                    shutil.rmtree(staging)
                    registry.unlink(missing_ok=True)
                elif record.get("owner") == "tingye" and record.get("attempt_id") == ident and not staging.exists():
                    registry.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError, UserError):
                pass
        for path in folder.iterdir():
            if path.is_file() and path.resolve().is_relative_to(folder) and (path.name == "audio.m4a" or re.fullmatch(r"audio\.m4a\.[0-9a-f]{32}\.part", path.name) or re.fullmatch(r"\.export-staging-[0-9a-f]{32}\.json", path.name)):
                try:
                    path.unlink()
                except OSError:
                    pass
        try:
            folder.rmdir()
        except OSError:
            pass


class Application:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.store = Store(self.directory)
        self.settings = load_settings(self.directory)
        self.settings_lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.manager = QueueManager(self.store)
        self.port = 0

    def state(self):
        jobs = self.store.all()
        with self.settings_lock:
            settings = dict(self.settings)
        return {"app": {"name": "听页", "version": __version__}, "settings": settings, "vaults": discover_vaults(), "models": model_status(), "jobs": [public_job(job) for job in jobs], "runtime": {"busy": any(job["status"] == "running" for job in jobs), "model_note": "模型首次使用需要下载，之后在本机转写。", "cpu_count": os.cpu_count() or 1}}

    def post(self, route, body):
        if route == "/api/settings":
            settings = validate_settings(body)
            with self.settings_lock:
                atomic_json(self.directory / "settings.json", settings)
                self.settings = settings
            return {"settings": settings}
        if route == "/api/jobs":
            links = body.get("links")
            if not isinstance(links, str) or len(links) > 20000:
                raise UserError("请粘贴视频链接，每行一个。")
            inputs = list(dict.fromkeys(line.strip() for line in links.splitlines() if line.strip()))
            if not inputs or len(inputs) > 50:
                raise UserError("一次可以添加1至50个视频链接。")
            for value in inputs:
                if len(value) > 2000 or not re.search(r"BV[0-9A-Za-z]{10}|https?://b23\.tv/", value):
                    raise UserError("请使用 B站视频链接、b23.tv短链接或完整BV号。")
            with self.settings_lock:
                settings = validate_settings(self.settings)
            return {"jobs": [public_job(self.store.create(value, settings)) for value in inputs]}
        if route == "/api/pick-vault":
            # The user explicitly clicked the folder chooser in the local UI.
            process = subprocess.run([sys.executable, "-m", "app.pick_folder"], cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            if process.returncode:
                raise UserError("无法打开文件夹选择窗口，请直接粘贴笔记库路径。")
            return {"path": json.loads(process.stdout).get("path")}
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/(cancel|retry|open-folder)", route)
        if match:
            ident, action = match.groups()
            job = self.store.get(ident)
            if not job:
                raise UserError("没有找到这个任务。")
            if action == "cancel":
                return {"job": public_job(self.manager.cancel(ident))}
            if action == "retry":
                if job["status"] not in ("failed", "cancelled", "interrupted"):
                    raise UserError("这个任务不需要重试。")
                with self.manager.lock:
                    fresh = self.store.get(ident)
                    if fresh["status"] not in ("failed", "cancelled", "interrupted"):
                        raise UserError("任务已在队列中。")
                    # Retry reuses the original destination and settings snapshot.
                    validate_settings(fresh["settings"])
                    job = self.store.update(ident, status="queued", stage="queued", progress=0, error=None, result=None, cancel_requested=False, message="等待重试")
                return {"job": public_job(job)}
            result = job.get("result") or {}
            folder = Path(result.get("folder_path", "")).resolve()
            vault = Path(job["settings"]["vault_path"]).resolve()
            if job["status"] != "completed" or not folder.is_dir() or folder == vault or not folder.is_relative_to(vault):
                raise UserError("保存文件夹暂时不可用。")
            if os.name == "nt":
                os.startfile(str(folder))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True}
        raise UserError("没有找到这个操作。")


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TingyeLocal"

        def allowed_host(self):
            return self.headers.get("Host", "") in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        def authorized(self):
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{app.port}", f"http://localhost:{app.port}"):
                return False
            return secrets.compare_digest(self.headers.get("X-Local-Token", ""), app.token)

        def send(self, code, data, content_type="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, {"error": "仅允许通过本机地址访问。"})
            route = urlparse(self.path).path
            if route == "/health":
                return self.send(200, {"app": "tingye", "version": __version__})
            if route.startswith("/api/"):
                if not self.authorized():
                    return self.send(403, {"error": "页面连接已更新，请刷新后重试。"})
                if route == "/api/state":
                    return self.send(200, app.state())
                return self.send(404, {"error": "没有找到这个接口。"})
            files = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css", "/app.js": "app.js", "/favicon.svg": "favicon.svg", "/static/style.css": "style.css", "/static/app.js": "app.js", "/static/favicon.svg": "favicon.svg"}
            if route not in files or not (STATIC / files[route]).is_file():
                return self.send(404, {"error": "没有找到这个页面。"})
            content = (STATIC / files[route]).read_bytes()
            if files[route] == "index.html":
                content = content.replace(b"__LOCAL_TOKEN__", app.token.encode("ascii"))
            self.send(200, content, (mimetypes.guess_type(files[route])[0] or "application/octet-stream") + "; charset=utf-8")

        def do_POST(self):
            if not self.allowed_host() or not self.authorized():
                return self.send(403, {"error": "页面连接已更新，请刷新后重试。"})
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                return self.send(415, {"error": "请求格式不正确。"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 65536:
                    return self.send(413, {"error": "这次提交的内容太长。"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise UserError("请求格式不正确。")
                route = urlparse(self.path).path
                if route == "/api/shutdown":
                    self.send(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                self.send(200, app.post(route, body))
            except (ValueError, UnicodeError):
                self.send(400, {"error": "请求内容无法读取。"})
            except UserError as exc:
                self.send(400, {"error": str(exc)})
            except Exception:
                import traceback
                traceback.print_exc()
                self.send(500, {"error": "本机服务遇到错误，请重试或查看日志。"})

        def do_OPTIONS(self):
            self.send(403, {"error": "此服务只接受本机页面请求。"})

        def log_message(self, fmt, *args):
            # Do not write every poll or input URL into the log.
            if args and str(args[0]).startswith("POST"):
                print(fmt % args, flush=True)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18761)
    parser.add_argument("--data-dir", type=Path, default=data_directory())
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    app = Application(args.data_dir)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(app))
    except OSError:
        from urllib.request import urlopen
        try:
            with urlopen(f"http://127.0.0.1:{args.port}/health", timeout=2) as response:
                same_app = json.load(response).get("app") == "tingye"
        except Exception:
            same_app = False
        if same_app:
            if not args.no_browser:
                webbrowser.open(f"http://127.0.0.1:{args.port}")
            return 0
        raise SystemExit("启动端口正在被其他程序使用，可指定另一个端口。")
    app.port = server.server_address[1]
    app.manager.start()
    atomic_json(args.data_dir / "runtime.json", {"pid": os.getpid(), "port": app.port, "url": f"http://127.0.0.1:{app.port}", "version": __version__})
    print(f"听页已启动 http://127.0.0.1:{app.port}", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{app.port}")).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        app.manager.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
