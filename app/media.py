"""Bilibili public metadata, subtitles, and audio. No video frames are fetched."""

from __future__ import annotations

import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from .common import UserError


class MediaError(UserError):
    """An actionable media error safe to display without request credentials."""


_BV = re.compile(r"BV[A-Za-z0-9]{10}\Z")
_VIDEO_PATH = re.compile(r"/video/(BV[A-Za-z0-9]{10})/?\Z")
_WEB_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
_SHORT_HOSTS = {"b23.tv", "www.b23.tv"}
_CDN_ROOTS = ("bilivideo.com", "bilivideo.cn", "hdslb.com", "akamaized.net")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
_MAX_AUDIO_BYTES = 2 * 1024 * 1024 * 1024
_MAX_JSON_BYTES = 20 * 1024 * 1024


def _client():
    return httpx.Client(headers=_HEADERS, timeout=httpx.Timeout(25.0, connect=15.0), follow_redirects=False)


def _url_parts(url: str):
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.port not in {None, 80, 443}):
            raise ValueError
        return parts
    except (ValueError, TypeError):
        raise MediaError("链接格式不正确，请粘贴 B 站视频链接或 BV 号。") from None


def _canonical(bvid: str, page: int) -> str:
    return f"https://www.bilibili.com/video/{bvid}/?p={page}"


def _parse_video_url(url: str) -> tuple[str, int]:
    parts = _url_parts(url)
    if parts.hostname not in _WEB_HOSTS:
        raise MediaError("只支持 bilibili.com 视频链接、b23.tv 短链接或 BV 号。")
    match = _VIDEO_PATH.fullmatch(parts.path)
    if not match:
        raise MediaError("请使用单个视频的 BV 链接，暂不支持合集或番剧地址。")
    query = parse_qs(parts.query, keep_blank_values=True)
    pages = query.get("p", ["1"])
    if len(pages) != 1 or not re.fullmatch(r"[1-9][0-9]{0,4}", pages[0]):
        raise MediaError("分 P 参数需要是大于零的整数。")
    return match.group(1), int(pages[0])


def parse_video_input(text: str, check_cancel=lambda: None) -> tuple[str, int]:
    """Accept one BV or known Bilibili URL; short-link redirects are checked per hop."""
    if not isinstance(text, str):
        raise MediaError("请粘贴一个 B 站视频链接或 BV 号。")
    value = text.strip()
    if _BV.fullmatch(value):
        return value, 1
    if not value or len(value) > 4096 or any(c in value for c in "\r\n\t"):
        raise MediaError("请每行粘贴一个 B 站视频链接或 BV 号。")
    if value.startswith(("www.bilibili.com/", "m.bilibili.com/", "bilibili.com/", "b23.tv/")):
        value = "https://" + value
    parts = _url_parts(value)
    if parts.hostname in _WEB_HOSTS:
        return _parse_video_url(value)
    if parts.hostname not in _SHORT_HOSTS:
        raise MediaError("只支持 bilibili.com 视频链接、b23.tv 短链接或 BV 号。")
    try:
        with _client() as client:
            for _ in range(5):
                check_cancel()
                parts = _url_parts(value)
                if parts.hostname in _WEB_HOSTS:
                    return _parse_video_url(value)
                if parts.hostname not in _SHORT_HOSTS:
                    raise MediaError("短链接跳转到了非 B 站地址，已停止访问。")
                value = parts._replace(scheme="https", netloc=parts.hostname).geturl()
                with client.stream("GET", value) as response:
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        raise MediaError("无法展开 b23.tv 短链接，请复制完整的 BV 视频链接。")
                    location = response.headers.get("location")
                    if not location:
                        raise MediaError("短链接没有返回有效的视频地址。")
                    value = urljoin(value, location)
            raise MediaError("短链接跳转次数过多，请复制完整的 BV 视频链接。")
    except httpx.HTTPError:
        raise MediaError("无法连接 B 站短链接，请检查网络或使用完整 BV 视频链接。") from None


def _json_get(client, url, *, params=None, check_cancel=lambda: None):
    check_cancel()
    try:
        with client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                check_cancel()
                size += len(chunk)
                if size > _MAX_JSON_BYTES:
                    raise MediaError("B 站返回的数据过大，已停止读取。")
                chunks.append(chunk)
            import json
            data = json.loads(b"".join(chunks))
        if not isinstance(data, dict):
            raise MediaError("B 站返回了无法识别的数据，请稍后重试。")
        return data
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        if status in {403, 412, 429}:
            raise MediaError("B 站暂时限制了访问，请稍后重试。当前版本不读取浏览器登录信息。") from None
        raise MediaError(f"B 站请求失败（HTTP {status}），请稍后重试。") from None
    except httpx.HTTPError:
        raise MediaError("连接 B 站失败，请检查网络后重试。") from None
    except (ValueError, UnicodeError):
        raise MediaError("B 站返回了无法识别的数据，请稍后重试。") from None


def _api_get(client, endpoint, params, check_cancel):
    payload = _json_get(client, "https://api.bilibili.com" + endpoint, params=params, check_cancel=check_cancel)
    code = payload.get("code")
    if code != 0:
        if code in {-404, 62002, 62012}:
            raise MediaError("这个视频不存在、已被删除，或当前无法公开访问。")
        if code in {-101, -104, -403, -412, 87007}:
            raise MediaError("这个视频需要登录、存在访问限制，或 B 站暂时限制请求。当前版本只处理可公开访问的视频。")
        raise MediaError(f"B 站接口暂时无法提供数据（错误码 {code if isinstance(code, int) else '未知'}）。")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MediaError("B 站未返回有效的视频信息。")
    return data


def fetch_video(text, check_cancel=lambda: None) -> dict:
    bvid, page = parse_video_input(text, check_cancel)
    with _client() as client:
        data = _api_get(client, "/x/web-interface/view", {"bvid": bvid}, check_cancel)
    pages = data.get("pages") or []
    selected = next((p for p in pages if isinstance(p, dict) and p.get("page") == page), None)
    if selected is None:
        raise MediaError(f"这个视频没有第 {page} P，请检查链接中的 p 参数。")
    cid = selected.get("cid")
    if not isinstance(cid, int) or cid <= 0:
        raise MediaError("B 站未返回有效的音频编号。")
    title = str(data.get("title") or bvid)
    if len(pages) > 1:
        title += f" · P{page} {selected.get('part') or ''}".rstrip()
    published = ""
    try:
        published = datetime.fromtimestamp(int(data.get("pubdate", 0)), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        pass
    return {
        "bvid": bvid, "aid": data.get("aid"), "cid": cid, "title": title,
        "uploader": str((data.get("owner") or {}).get("name") or ""),
        "url": _canonical(bvid, page), "page": page,
        "duration": int(selected.get("duration") or data.get("duration") or 0),
        "description": str(data.get("desc") or ""), "published_at": published,
    }


def _cdn_url(url: str) -> str:
    if isinstance(url, str) and url.startswith("//"):
        url = "https:" + url
    parts = _url_parts(url)
    host = parts.hostname
    if not any(host == root or host.endswith("." + root) for root in _CDN_ROOTS):
        raise MediaError("音频或字幕地址不属于受支持的 B 站资源域名。")
    return parts._replace(scheme="https", netloc=host).geturl()


def fetch_subtitles(video, check_cancel=lambda: None) -> dict | None:
    """Use public subtitle tracks where available, otherwise allow local ASR."""
    with _client() as client:
        try:
            data = _api_get(client, "/x/player/v2", {"bvid": video["bvid"], "cid": video["cid"]}, check_cancel)
            candidates = (data.get("subtitle") or {}).get("subtitles") or []
            candidates = [s for s in candidates if isinstance(s, dict) and s.get("subtitle_url")]
            candidates.sort(key=lambda s: (not str(s.get("lan", "")).startswith("zh"), str(s.get("lan", "")).startswith("ai-")))
            for candidate in candidates:
                check_cancel()
                try:
                    body = _json_get(client, _cdn_url(candidate["subtitle_url"]), check_cancel=check_cancel).get("body")
                    if not isinstance(body, list):
                        continue
                    segments = []
                    for item in body:
                        if not isinstance(item, dict):
                            continue
                        start, end = float(item.get("from", 0)), float(item.get("to", 0))
                        text = str(item.get("content") or "").strip()
                        if text and math.isfinite(start) and math.isfinite(end) and end >= start >= 0:
                            segments.append({"start": start, "end": end, "text": text})
                    if segments:
                        segments.sort(key=lambda s: s["start"])
                        return {"segments": segments, "language": str(candidate.get("lan") or "unknown"), "source": "bilibili_subtitle"}
                except (MediaError, TypeError, ValueError):
                    continue
        except MediaError as error:
            video.setdefault("notes", []).append("公开字幕读取失败，将使用本地转写。" + str(error))
            return None
    video.setdefault("notes", []).append("公开接口未提供可用的独立字幕，使用本地转写。")
    return None


def download_audio(video, target: Path, progress, check_cancel=lambda: None) -> Path:
    """Download one DASH audio stream with checked redirects and atomic completion."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + f".{uuid.uuid4().hex}.part")
    progress(0, "正在获取音频地址")
    try:
        with _client() as client:
            data = _api_get(client, "/x/player/playurl", {
                "bvid": video["bvid"], "cid": video["cid"], "fnval": 4048,
                "fnver": 0, "fourk": 1, "qn": 64,
            }, check_cancel)
            tracks = (data.get("dash") or {}).get("audio") or []
            tracks = [item for item in tracks if isinstance(item, dict) and (item.get("baseUrl") or item.get("base_url"))]
            if not tracks:
                raise MediaError("B 站没有提供独立音轨。这个视频可能需要登录或受到访问限制。")
            # Ordinary AAC is sufficient for speech and avoids downloading video or lossless audio.
            tracks.sort(key=lambda item: (not str(item.get("codecs", "")).startswith("mp4a"), int(item.get("bandwidth") or 0)))
            track = tracks[0]
            urls = [track.get("baseUrl") or track.get("base_url")]
            urls.extend(track.get("backupUrl") or track.get("backup_url") or [])
            last_error = None
            for raw_url in urls[:4]:
                check_cancel()
                try:
                    _download_stream(client, _cdn_url(raw_url), temp, progress, check_cancel)
                    check_cancel()
                    os.replace(temp, target)
                    progress(100, "音频下载完成")
                    return target
                except (httpx.HTTPError, MediaError) as error:
                    last_error = error
                    temp.unlink(missing_ok=True)
            if isinstance(last_error, MediaError):
                raise last_error
            raise MediaError("音频下载失败，请检查网络后重试。")
    finally:
        temp.unlink(missing_ok=True)


def _download_stream(client, url, temp, progress, check_cancel):
    for _ in range(5):
        check_cancel()
        url = _cdn_url(url)
        with client.stream("GET", url) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise MediaError("音频服务器返回了无效的跳转地址。")
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "text/" in content_type or "json" in content_type:
                raise MediaError("音频服务器未返回音频文件，请稍后重试。")
            try:
                total = int(response.headers.get("content-length") or 0)
            except ValueError:
                total = 0
            if total > _MAX_AUDIO_BYTES:
                raise MediaError("音轨超过 2 GB，当前版本无法处理。")
            count, last_percent = 0, -1
            with temp.open("xb") as stream:
                for chunk in response.iter_bytes(chunk_size=128 * 1024):
                    check_cancel()
                    count += len(chunk)
                    if count > _MAX_AUDIO_BYTES:
                        raise MediaError("音轨超过 2 GB，已停止下载。")
                    stream.write(chunk)
                    percent = min(99, int(count * 100 / total)) if total > 0 else 0
                    if percent != last_percent:
                        progress(percent, f"正在下载音频，已接收 {count / 1048576:.1f} MB")
                        last_percent = percent
                stream.flush()
                os.fsync(stream.fileno())
            if count == 0 or (total > 0 and count != total):
                raise MediaError("音频文件下载不完整，请重试。")
            return
    raise MediaError("音频地址跳转次数过多，已停止下载。")
