"""One isolated process per collection job, no model or cloud text API."""
import argparse
from pathlib import Path
import traceback

from .common import Cancelled, UserError
from .settings import validate_settings
from .store import Store


def run(directory, job_id, attempt_id=None):
    store = Store(directory)
    job = store.get(job_id)
    if not job:
        return 2
    work_root = (Path(directory) / "work").resolve()
    scratch = (work_root / (attempt_id or job_id)).resolve()
    if scratch.parent != work_root:
        return 2
    fence = {"status": "running", "attempt_id": attempt_id}

    def check_cancel():
        current = store.get(job_id)
        if (not current or current.get("status") != "running" or
                current.get("attempt_id") != attempt_id or current.get("cancel_requested")):
            raise Cancelled()

    def update(**fields):
        # Cancellation can arrive between a checkpoint and its following write.
        result = store.update_if(job_id, {**fence, "cancel_requested": False}, **fields)
        if result is None:
            raise Cancelled()
        return result

    def cancelled():
        store.update_if(job_id, fence, status="cancelled", stage="cancelled", message="已取消", error=None)

    def failed(error):
        result = store.update_if(job_id, {**fence, "cancel_requested": False}, status="failed",
                                 stage="failed", message="处理未完成", error=error)
        if result is None:
            store.update_if(job_id, {**fence, "cancel_requested": True}, status="cancelled",
                            stage="cancelled", message="已取消", error=None)

    def progress(stage, start, span):
        def emit(percent, message):
            check_cancel()
            value = start + span * max(0, min(100, float(percent))) / 100
            update(stage=stage, progress=round(value, 1), message=str(message)[:300])
        return emit

    try:
        check_cancel()
        scratch.mkdir(parents=True, exist_ok=True)
        from .media import fetch_video, fetch_subtitles, download_audio
        from .asr import transcribe_audio
        from .exporter import export_note
        settings = validate_settings(job["settings"])
        settings.update(_staging_registry=str(scratch / "export-staging.json"), _attempt_id=attempt_id or job_id)
        check_cancel()
        update(stage="metadata", progress=2, message="读取视频信息")
        video = fetch_video(job["input"], check_cancel=check_cancel)
        check_cancel()
        update(title=video["title"], progress=5, message="检查可用字幕", stage="subtitles")
        transcript = fetch_subtitles(video, check_cancel=check_cancel) if settings["prefer_subtitles"] else None
        audio_path = None
        if transcript is None:
            audio_path = download_audio(video, scratch / "audio.m4a", progress("download", 7, 18), check_cancel=check_cancel)
        if transcript is None:
            transcript = transcribe_audio(audio_path, settings, progress("transcription", 25, 65), check_cancel=check_cancel)
        check_cancel()
        update(stage="saving", progress=93, message="写入 Obsidian 笔记库")
        result = export_note(video, transcript, audio_path, settings, check_cancel=check_cancel)
        details = transcript.get("metadata") or {}
        result.update(transcript_source=transcript.get("source"), asr_model=transcript.get("model"),
                      duration_seconds=transcript.get("duration", video.get("duration")),
                      last_timestamp=max((s["end"] for s in transcript["segments"]), default=0),
                      processing_seconds=details.get("elapsed_seconds"),
                      automatic_recoveries=(details.get("recovery") or {}).get("accepted", 0))
        # A successful atomic publication wins a late cancellation request.
        store.update_if(job_id, fence, status="completed", stage="completed", progress=100, result=result, error=None, message="已覆盖原有笔记，仅保留一份" if result.get("overwritten") else "已保存到 Obsidian")
        return 0
    except Cancelled:
        cancelled()
        return 0
    except UserError as exc:
        failed(str(exc)[:500])
        return 1
    except Exception as exc:
        traceback.print_exc()
        failed(f"处理遇到错误（{type(exc).__name__}），可重试或查看本机日志。")
        return 1
    finally:
        # This is a fixed application-owned path, never a user-selected folder.
        if scratch.parent == work_root:
            (scratch / "audio.m4a").unlink(missing_ok=True)
            (scratch / "audio.m4a.part").unlink(missing_ok=True)
            try:
                scratch.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--attempt-id")
    args = parser.parse_args()
    raise SystemExit(run(args.data_dir, args.job_id, args.attempt_id))
