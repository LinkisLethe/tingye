"""Atomic Obsidian exports with provenance, timestamps, and safe duplicate handling."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from urllib.parse import quote, urlencode

from .common import UserError


class ExportError(UserError):
    """An export could not complete without risking an existing file."""


_LOCK = threading.Lock()
_PREPARE_LOCK = threading.Lock()
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_BV = re.compile(r"BV[A-Za-z0-9]{10}\Z")


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ExportError("保存位置超出了所选笔记库，请检查文件夹设置。")
    return resolved


def _safe_component(component: str) -> bool:
    return bool(component and component not in {".", ".."} and len(component) <= 100
                and not _UNSAFE.search(component) and not _RESERVED.match(component)
                and component == component.rstrip(" .") and not component.startswith("."))


def validate_destination(settings: dict) -> tuple[Path, Path]:
    """Resolve existing vault and relative output path, including existing symlinks."""
    raw = settings.get("vault_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ExportError("请先选择 Obsidian 笔记库文件夹。")
    vault = Path(raw).expanduser()
    if not vault.is_absolute():
        raise ExportError("笔记库需要使用完整的文件夹路径。")
    vault = vault.resolve()
    if not vault.is_dir():
        raise ExportError("所选笔记库文件夹不存在，请重新选择。")
    subfolder = settings.get("subfolder", "视频笔记")
    if not isinstance(subfolder, str):
        raise ExportError("保存子文件夹格式不正确。")
    if not subfolder:
        return vault, vault
    if Path(subfolder).is_absolute() or PureWindowsPath(subfolder).is_absolute() or PureWindowsPath(subfolder).drive:
        raise ExportError("保存子文件夹需要位于所选笔记库内，不能填写绝对路径。")
    parts = re.split(r"[/\\]", subfolder)
    if not all(_safe_component(part) for part in parts):
        raise ExportError("子文件夹包含无效名称。请去掉 ..、特殊符号、隐藏文件夹或 Windows 保留名称。")
    return vault, _inside(vault.joinpath(*parts), vault)


def _identity(video):
    bvid = video.get("bvid")
    page = video.get("page", 1)
    if not isinstance(bvid, str) or not _BV.fullmatch(bvid) or type(page) is not int or not 1 <= page <= 99999:
        raise ExportError("视频编号或分 P 信息不正确，无法保存笔记。")
    return f"{bvid}_P{page}"


def _note_filename(title):
    clean = unicodedata.normalize("NFC", str(title))
    clean = _UNSAFE.sub("_", clean).strip(" .")
    clean = re.sub(r"\s+", " ", clean)[:120].rstrip(" .")
    if not clean or _RESERVED.match(clean) or clean.startswith("."):
        clean = "视频笔记_" + clean.lstrip(".")
    return clean + ".md"


def find_existing_note(video, settings):
    """Read-only compatibility lookup; the worker always retranscribes/overwrites."""
    vault, base = validate_destination(settings)
    notes = _matching_notes(base, video, vault) if base.exists() else []
    if not notes:
        return None
    result = _result(notes[0], base, vault, False, 0)
    result["existing"] = True
    return result


@contextmanager
def _export_lock(base, vault, check_cancel):
    started = time.monotonic()
    while not _LOCK.acquire(timeout=0.1):
        check_cancel()
        if time.monotonic() - started > 60:
            raise ExportError("另一项保存仍在进行，请稍后重试。")
    stream = None
    acquired = False
    try:
        check_cancel()
        lock_path = _inside(base / ".tingye-export.lock", vault)
        stream = lock_path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        while True:
            check_cancel()
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() - started > 60:
                    raise ExportError("另一项保存仍在进行，请稍后重试。") from None
                time.sleep(0.1)
        yield
    finally:
        try:
            if stream is not None:
                try:
                    if acquired:
                        stream.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
        finally:
            _LOCK.release()


def _normalize_segments(transcript):
    values = transcript.get("segments")
    if not isinstance(values, list):
        raise ExportError("转写结果格式不完整，无法保存。")
    segments = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ExportError("转写段落格式不正确，无法保存。")
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            raise ExportError("转写结果缺少有效的时间戳。") from None
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start <= end:
            raise ExportError("转写结果包含无效的时间戳。")
        # Only trim outer whitespace. No wording, punctuation, or language correction.
        text = item["text"].strip()
        if text:
            segments.append({"start": start, "end": end, "text": text})
    segments.sort(key=lambda item: item["start"])
    return segments


def _timestamp(seconds, *, srt=False):
    millis = int(round(seconds * 1000))
    hours, remainder = divmod(millis, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, ms = divmod(remainder, 1000)
    if srt:
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _md(text):
    # Video titles and transcripts are data, including when they contain Markdown/HTML.
    value = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"([\\`*_{}\[\]<>()!#|~])", r"\\\1", value)


def _result(note, folder, vault, duplicate, count):
    return {
        "note_path": str(note), "folder_path": str(folder),
        "obsidian_uri": "obsidian://open?" + urlencode({"path": str(note)}, quote_via=quote),
        "duplicate": duplicate, "segments_count": count,
    }


def _write(path, content):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _staging_registration(settings):
    """Read trusted worker-only recovery fields; browser settings never contain them."""
    raw = settings.get("_staging_registry")
    attempt = settings.get("_attempt_id")
    if raw is None and attempt is None:
        return None, None
    if not isinstance(raw, (str, Path)) or not isinstance(attempt, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt):
        raise ExportError("保存任务的恢复信息不完整，请重试。")
    registry = Path(raw)
    if not registry.is_absolute() or registry.name != "export-staging.json":
        raise ExportError("保存任务的恢复文件位置不正确，请重试。")
    return registry.resolve(), attempt


def _register_staging(registry, attempt, staging):
    owner = {"owner": "tingye", "attempt_id": attempt}
    _write(staging / ".tingye-owner.json", json.dumps(owner) + "\n")
    registry.parent.mkdir(parents=True, exist_ok=True)
    temporary = registry.with_name(f".export-staging-{uuid.uuid4().hex}.json")
    try:
        _write(temporary, json.dumps({**owner, "path": str(staging)}, ensure_ascii=False) + "\n")
        os.replace(temporary, registry)
    finally:
        temporary.unlink(missing_ok=True)


def _frontmatter(path):
    import yaml
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            head = stream.read(32768)
        match = re.match(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", head, re.S)
        value = yaml.safe_load(match.group(1)) if match else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def _matching_notes(base, video, vault):
    from urllib.parse import parse_qs, urlsplit
    found = []
    for path in base.rglob("*.md"):
        if any(part.startswith(".") for part in path.relative_to(base).parts):
            continue
        path = _inside(path, vault)
        data = _frontmatter(path)
        url = str(data.get("url") or data.get("source") or "")
        bvid = str(data.get("bvid") or "")
        if not bvid:
            match = re.search(r"/video/(BV[A-Za-z0-9]{10})(?:/|[?#]|$)", url)
            bvid = match.group(1) if match else ""
        try:
            page = int(data.get("page") or parse_qs(urlsplit(url).query).get("p", [1])[0])
        except (ValueError, TypeError):
            continue
        if bvid == video["bvid"] and page == video["page"]:
            found.append(path)
    return sorted(set(found), key=lambda path: (path.parent != base, path.name, str(path)))


def _backup_notes(paths, base, identity):
    from zipfile import ZipFile, ZIP_DEFLATED
    from .settings import data_directory
    if not paths:
        return None
    backup_dir = data_directory() / "backups" / "overwritten-notes"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{identity}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.zip"
    with ZipFile(backup, "x", compression=ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(base).as_posix())
    return str(backup)


def _clipping_text(video, transcript, segments, created):
    canonical = f"https://www.bilibili.com/video/{video['bvid']}/"
    if video["page"] != 1:
        canonical += f"?p={video['page']}"
    published = str(video.get("published_at") or "")[:10]
    language = str(transcript.get("language") or "")
    language = {"zh": "中文", "zh-CN": "中文", "zh-Hans": "中文", "en": "英语"}.get(language, language)
    data = {
        "title": str(video["title"]), "url": canonical, "bvid": video["bvid"],
        "cid": str(video.get("cid") or ""), "author": str(video.get("uploader") or ""),
        "upload_date": published, "subtitle_lang": language,
        "created": created, "tags": ["clippings", "bilibili"],
        "page": video["page"], "transcript_source": transcript["source"],
    }
    if transcript.get("model") and transcript["source"] == "local_asr":
        data["asr_model"] = transcript["model"]
    lines = ["---"] + [key + ": " + json.dumps(value, ensure_ascii=False) for key, value in data.items()] + ["---", ""]
    params = {"bvid": video["bvid"], "cid": video.get("cid", ""), "page": video["page"], "autoplay": 0}
    if video.get("aid"):
        params = {"aid": video["aid"], **params}
    iframe = "https://player.bilibili.com/player.html?" + urlencode(params)
    lines.extend([
        f'<iframe src="{iframe}" scrolling="no" border="0" frameborder="no" framespacing="0" allow="fullscreen; picture-in-picture" allowfullscreen="true" style="height:100%;width:100%; aspect-ratio: 16 / 9;"> </iframe>',
        "", "## 简介", "", _md(video.get("description") or ""), "", "## 字幕", "",
    ])
    for segment in segments:
        text = _md(segment["text"]).replace("\n", " ")
        lines.append(f'\u0060{_timestamp(segment["start"])}\u0060 {text}')
    if not segments:
        lines.append("未识别到语音内容。")
    return "\n".join(lines) + "\n"


def export_note(video, transcript, audio_path: Path | None, settings, check_cancel=lambda: None) -> dict:
    """Publish one flat Clipper-style Markdown; atomically replace the same video."""
    # Do not resolve a partly-created directory tree while another local writer
    # is creating it. File publication is separately locked across processes.
    with _PREPARE_LOCK:
        vault, base = validate_destination(settings)
        base.mkdir(parents=True, exist_ok=True)
        vault, base = validate_destination(settings)
    identity = _identity(video)
    registry, attempt = _staging_registration(settings)
    check_cancel()
    try:
        base.mkdir(parents=True, exist_ok=True)
        _inside(base, vault)
        with _export_lock(base, vault, check_cancel):
            existing = _matching_notes(base, video, vault)
            segments = _normalize_segments(transcript)
            if transcript.get("source") not in {"local_asr", "bilibili_subtitle"}:
                raise ExportError("转写结果缺少来源标识，无法保存。")
            created = str(_frontmatter(existing[0]).get("created") or "")[:10] if existing else ""
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", created):
                created = datetime.now().astimezone().date().isoformat()
            target = existing[0] if existing else _inside(base / (created + "-" + _note_filename(video["title"])), vault)
            # Two unrelated videos can have identical titles; use their identities.
            if target.exists() and target not in existing:
                target = _inside(base / (created + "-" + _note_filename(video["title"])[:-3] + f" [{identity}].md"), vault)
                if target.exists() and target not in _matching_notes(base, video, vault):
                    raise ExportError("保存文件名与其他文件冲突，请先更改保存位置。")
            staging = Path(tempfile.mkdtemp(prefix=".tingye-export-", dir=base)).resolve()
            try:
                if registry is not None:
                    _register_staging(registry, attempt, staging)
                pending = staging / "note.md"
                _write(pending, _clipping_text(video, transcript, segments, created))
                check_cancel()
                backup = _backup_notes(existing, base, identity)
                check_cancel()
                _inside(target, vault)
                os.replace(pending, target)
                # De-duplicate only notes whose frontmatter identified this exact BV+part.
                for duplicate in existing:
                    if duplicate != target:
                        _inside(duplicate, vault).unlink()
                result = _result(target, base, vault, False, len(segments))
                result.update(overwritten=bool(existing), duplicates_removed=max(0, len(existing) - 1), backup_path=backup, format="bilibili_clipping", audio_retained=False)
                return result
            finally:
                if staging.exists():
                    resolved = _inside(staging, vault)
                    if resolved.parent != base or not resolved.name.startswith(".tingye-export-"):
                        raise ExportError("临时文件夹位置异常，已停止清理。")
                    shutil.rmtree(resolved)
                    if registry is not None:
                        registry.unlink(missing_ok=True)
    except OSError as error:
        if isinstance(error, PermissionError):
            raise ExportError("无法写入所选文件夹，请检查权限或文件是否被占用。") from None
        raise ExportError("保存笔记失败，请检查磁盘空间和文件夹是否可用。") from None
