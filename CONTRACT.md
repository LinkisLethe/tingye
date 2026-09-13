# Integration contract 0.2

Python package app, local static web UI, one guarded worker at a time.

## Media and ASR

fetch_video(input, check_cancel) returns bvid, aid, cid, title, uploader, url, page, duration, description, published_at.
fetch_subtitles(video, check_cancel) returns segments/language/source or None.
download_audio(video, target, progress, check_cancel) downloads audio only to application scratch.
transcribe_audio(path, settings, progress, check_cancel) runs local Whisper and bounded automatic recovery. It does not use LLM APIs, OCR or manual corrections.

## Export

export_note(video, transcript, audio_path, settings, check_cancel) creates one flat, Clipper-style Markdown inside the selected destination. No audio or sidecar transcript files are exported.
Matching uses frontmatter bvid or video URL plus part number, recursively inside the selected destination. Repeated collection overwrites the canonical Markdown and removes additional exact matches. Backups are ZIP archives outside the vault under .tingye/backups/overwritten-notes.
Results contain note_path, folder_path, obsidian_uri, segments_count, overwritten, duplicates_removed, audio_retained=False, format and optional backup_path. The duplicate compatibility flag is False.
Source description is included unchanged apart from Markdown escaping. Transcribed wording is not edited. There is no generated summary.

## Settings

vault_path, subfolder (default Clippings/Bilibili), model (large-v3-turbo or small), language (auto/zh/en), cpu_threads, prefer_subtitles, hotwords.
keep_audio remains accepted as a compatibility boolean but is normalized to False. The UI has no retain-audio switch.

## Persistence and API

User data and independent Python runtime live under the stable .tingye directory in the user's home. Do not use LOCALAPPDATA because MSIX can virtualize it.
All /api calls require X-Local-Token from the local page. Server binds only 127.0.0.1 and validates Host/Origin.

GET /api/state returns app/settings/vaults/models/jobs/runtime.
POST /api/settings updates settings.
POST /api/jobs accepts links, one per line.
POST /api/jobs/id/cancel and /retry manage an attempt.
POST /api/jobs/id/open-folder opens the saved destination.
POST /api/pick-vault and /api/shutdown handle explicit user actions.

Queued jobs snapshot their settings. Claims generate an attempt ID; progress and result writes are fenced to that attempt. Windows workers belong to a kill-on-close job object. Temporary exports carry ownership markers and a recovery registry. Audio scratch and stale staging are cleaned only after checking ownership and paths.
