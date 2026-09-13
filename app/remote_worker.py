"""Standalone GPU worker for a user's own SSH workstation."""
import argparse
import json
import os
from pathlib import Path
import signal
import time
import traceback

from .common import Cancelled, UserError
from .media import fetch_video, fetch_subtitles, download_audio
from .asr import transcribe_audio


def atomic(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--gpu', required=True)
    args = parser.parse_args()
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    job_path = Path(args.job).resolve()
    job = json.loads(job_path.read_text(encoding='utf8'))
    folder = job_path.parent
    cancelled = False
    status = {'state': 'running', 'bvid': job['bvid'], 'pid': os.getpid(), 'gpu': args.gpu, 'progress': 0, 'message': '读取视频信息'}
    def request_cancel(*_):
        nonlocal cancelled
        cancelled = True
    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)
    def check_cancel():
        if cancelled or (folder / 'cancel').exists():
            raise Cancelled()
    def progress(phase, start, width):
        def update(percent, message):
            check_cancel()
            status.update(phase=phase, progress=round(start + width * percent / 100, 1), message=message, updated=time.time())
            atomic(folder / 'status.json', status)
        return update
    audio = folder / 'audio.m4a'
    try:
        atomic(folder / 'status.json', status)
        video = fetch_video(job['bvid'], check_cancel=check_cancel)
        status['title'] = video['title']
        transcript = fetch_subtitles(video, check_cancel=check_cancel) if job['settings'].get('prefer_subtitles', True) else None
        if transcript is None:
            download_audio(video, audio, progress('download', 2, 13), check_cancel)
            settings = {**job['settings'], '_device': 'cuda', 'cpu_threads': 8}
            transcript = transcribe_audio(audio, settings, progress('transcription', 15, 83), check_cancel)
        check_cancel()
        atomic(folder / 'result.json', {'video': video, 'transcript': transcript})
        status.update(state='completed', progress=100, message='远端转写完成', updated=time.time())
        atomic(folder / 'status.json', status)
        return 0
    except Cancelled:
        status.update(state='cancelled', message='远端任务已取消', updated=time.time())
        atomic(folder / 'status.json', status)
        return 0
    except Exception as exc:
        traceback.print_exc()
        status.update(state='failed', error=str(exc) if isinstance(exc, UserError) else type(exc).__name__, updated=time.time())
        atomic(folder / 'status.json', status)
        return 1
    finally:
        audio.unlink(missing_ok=True)
        for part in folder.glob('audio.m4a.*.part'):
            part.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
