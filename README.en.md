# Tingye · 听页

[![Tests](https://github.com/LinkisLethe/tingye/actions/workflows/tests.yml/badge.svg)](https://github.com/LinkisLethe/tingye/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white&style=flat-square)](https://www.python.org/downloads/)
![Windows](https://img.shields.io/badge/Platform-Windows-255b4d?style=flat-square)

[中文说明](README.md)

A local web app that saves Bilibili videos as timestamped Markdown notes in an Obsidian vault. It uses available subtitles first and falls back to local audio transcription with faster-whisper. No LLM API key or Obsidian plugin is required.

The app includes a Python backend. Opening the HTML file directly or hosting it on GitHub Pages does not run transcription.

## Run on Windows

Install Python 3.11 or newer with Python on PATH, and create an Obsidian vault. Download and extract this repository, then double-click `启动听页.vbs`.

If VBS is disabled, run this command from the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch.ps1
```

The launcher prepares dependencies and opens `http://127.0.0.1:18761`. Select your vault, paste a video link or BV ID on each line, and start collection. A batch accepts up to 50 entries. The default destination is `Clippings/Bilibili` inside the vault.

## Output and overwrite behavior

- Saves one `date-video title.md` file with metadata, an embedded player, the original description, and timestamped text.
- Identifies videos by BV ID and part number. Repeated collection replaces the existing note and removes additional matching copies within the selected destination, including subfolders.
- Backs up overwritten Markdown outside the vault under `~/.tingye/backups/overwritten-notes`.
- Removes temporary audio after processing. No audio, TXT, SRT, or JSON sidecars are exported to the vault.
- Supports cancellation, retries, and interrupted-job recovery. Closing the browser leaves the queue running.

**Re-collection replaces the entire note, including manual edits.** Keep personal annotations in separate notes.

## Local processing

The default model is `large-v3-turbo` (about 1.6 GB); `small` is a lighter option (about 0.5 GB). Web-submitted tasks use the local CPU. Video access and initial model downloads require a network connection; audio recognition runs locally once the model is available.

Settings, queue data, logs, backups, temporary files, and the launcher-managed Python environment live in `~/.tingye`. Models use the user's Hugging Face cache. The server listens only on `127.0.0.1`, validates local requests, and does not read browser login cookies automatically.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m app.server --no-browser
```

`requirements.txt` pins direct dependencies; `requirements.lock.txt` records the full dependency snapshot of the validated environment. Tests use temporary vaults and mocked media/model responses, without real video or model downloads. See [validation scope](VALIDATION.md) and the [integration contract](CONTRACT.md).

Optional `app.remote_*` modules support SSH-based Linux GPU workers. They require manual setup of the runtime, `requirements-gpu.txt`, model, and batch manifest. There is no one-click remote deployment in the web UI.

## Limitations

Publicly accessible Bilibili videos are the primary target. Authentication, regional restrictions, paywalls, and platform API changes may prevent collection. Transcripts can contain errors or omissions; bounded recovery is not a guarantee of complete recognition. There is no visual verification or LLM summarization. Windows is the primary application platform.

Built with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [Whisper](https://github.com/openai/whisper), [CTranslate2](https://github.com/OpenNMT/CTranslate2), [Hugging Face Hub](https://github.com/huggingface/huggingface_hub), [HTTPX](https://github.com/encode/httpx), and [OpenCC Python](https://github.com/yichen0831/opencc-python).
