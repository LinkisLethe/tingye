import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.common import Cancelled
from app import media


BV = "BV1xx411c7mD"
VIDEO = {"bvid": BV, "cid": 123, "page": 1}


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


class MediaTests(unittest.TestCase):
    def test_accept_bv_and_explicit_page_without_arbitrary_fetch(self):
        self.assertEqual(media.parse_video_input(BV), (BV, 1))
        self.assertEqual(media.parse_video_input(f"https://www.bilibili.com/video/{BV}/?p=2&other=a"), (BV, 2))
        for value in [f"https://evil.test/video/{BV}", f"https://www.bilibili.com.evil.test/video/{BV}",
                      f"https://user@www.bilibili.com/video/{BV}", f"https://www.bilibili.com:9999/video/{BV}",
                      f"https://www.bilibili.com/video/{BV}/?p=0", f"https://www.bilibili.com/video/{BV}/?p=1&p=2",
                      "file:///etc/passwd", "http://127.0.0.1/admin", f"https://www.bilibili.com/video/{BV}/?p=-1"]:
            with self.subTest(value=value), self.assertRaises(media.MediaError):
                media.parse_video_input(value)

    def test_short_link_redirect_checked_before_request(self):
        called = []
        def handler(request):
            called.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        with patch.object(media, "_client", lambda: mock_client(handler)), self.assertRaises(media.MediaError):
            media.parse_video_input("https://b23.tv/abc")
        self.assertEqual(len(called), 1)

    def test_short_link_to_known_video(self):
        def handler(request):
            return httpx.Response(302, headers={"location": f"https://www.bilibili.com/video/{BV}/?p=3"})
        with patch.object(media, "_client", lambda: mock_client(handler)):
            self.assertEqual(media.parse_video_input("https://b23.tv/abc"), (BV, 3))

    def test_metadata_uses_selected_part(self):
        def handler(request):
            return httpx.Response(200, json={"code": 0, "data": {"title": "示例", "owner": {"name": "作者"}, "pubdate": 1,
                "pages": [{"page": 1, "cid": 123, "duration": 90, "part": "第一节"}, {"page": 2, "cid": 456, "duration": 45, "part": "第二节"}]}})
        with patch.object(media, "_client", lambda: mock_client(handler)):
            value = media.fetch_video(f"https://www.bilibili.com/video/{BV}/?p=2")
        self.assertEqual(value["cid"], 456)
        self.assertEqual(value["duration"], 45)
        self.assertIn("P2 第二节", value["title"])
        self.assertNotIn("other", value["url"])

    def test_public_subtitles_and_unavailable_diagnostic(self):
        def handler(request):
            if request.url.host == "api.bilibili.com":
                return httpx.Response(200, json={"code": 0, "data": {"subtitle": {"subtitles": [{"lan": "zh-CN", "subtitle_url": "//aisubtitle.hdslb.com/test"}]}}})
            return httpx.Response(200, json={"body": [{"from": 1, "to": 2, "content": "Hello 技术词"}]})
        with patch.object(media, "_client", lambda: mock_client(handler)):
            transcript = media.fetch_subtitles(dict(VIDEO))
        self.assertEqual(transcript["segments"][0]["text"], "Hello 技术词")
        self.assertEqual(transcript["source"], "bilibili_subtitle")
        video = dict(VIDEO)
        with patch.object(media, "_client", lambda: mock_client(lambda request: httpx.Response(412))):
            self.assertIsNone(media.fetch_subtitles(video))
        self.assertIn("读取失败", video["notes"][0])

    def test_download_only_audio_and_omit_sensitive_urls(self):
        called = []
        payload = b"audio bytes" * 100
        def handler(request):
            called.append(str(request.url))
            if request.url.host == "api.bilibili.com":
                return httpx.Response(200, json={"code": 0, "data": {"dash": {
                    "video": [{"baseUrl": "https://upos.bilivideo.com/video.mp4"}],
                    "audio": [{"codecs": "mp4a.40.2", "bandwidth": 65536, "baseUrl": "https://upos.bilivideo.com/audio.m4a?secret=expiry"}]}}})
            return httpx.Response(200, content=payload, headers={"content-length": str(len(payload)), "content-type": "audio/mp4"})
        with tempfile.TemporaryDirectory() as folder, patch.object(media, "_client", lambda: mock_client(handler)):
            target = Path(folder) / "audio.m4a"
            result = media.download_audio(dict(VIDEO), target, lambda *args: None)
            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(list(Path(folder).iterdir()), [target])
        self.assertFalse(any("video.mp4" in url for url in called))

    def test_audio_redirect_rejects_private_host_without_request(self):
        called = []
        def handler(request):
            called.append(request.url.host)
            if request.url.host == "api.bilibili.com":
                return httpx.Response(200, json={"code": 0, "data": {"dash": {"audio": [{"baseUrl": "https://upos.bilivideo.com/audio"}]}}})
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secrets"})
        with tempfile.TemporaryDirectory() as folder, patch.object(media, "_client", lambda: mock_client(handler)):
            with self.assertRaises(media.MediaError):
                media.download_audio(VIDEO, Path(folder) / "audio.m4a", lambda *args: None)
            self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertEqual(called, ["api.bilibili.com", "upos.bilivideo.com"])

    def test_cancel_is_not_converted_to_subtitle_absence(self):
        def cancel():
            raise Cancelled()
        with patch.object(media, "_client", lambda: mock_client(lambda request: httpx.Response(200))):
            with self.assertRaises(Cancelled):
                media.fetch_subtitles(VIDEO, cancel)

    def test_truncated_audio_is_not_committed(self):
        def handler(request):
            if request.url.host == "api.bilibili.com":
                return httpx.Response(200, json={"code": 0, "data": {"dash": {"audio": [{"baseUrl": "https://upos.bilivideo.com/audio"}]}}})
            return httpx.Response(200, content=b"short", headers={"content-length": "100"})
        with tempfile.TemporaryDirectory() as folder, patch.object(media, "_client", lambda: mock_client(handler)):
            target = Path(folder) / "audio.m4a"
            with self.assertRaises(media.MediaError):
                media.download_audio(VIDEO, target, lambda *args: None)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_cancel_during_audio_download_removes_partial_file(self):
        def handler(request):
            if request.url.host == "api.bilibili.com":
                return httpx.Response(200, json={"code": 0, "data": {"dash": {"audio": [{"baseUrl": "https://upos.bilivideo.com/audio"}]}}})
            return httpx.Response(200, content=b"a" * (512 * 1024))
        with tempfile.TemporaryDirectory() as folder, patch.object(media, "_client", lambda: mock_client(handler)):
            def check():
                if list(Path(folder).glob("*.part")):
                    raise Cancelled()
            target = Path(folder) / "audio.m4a"
            with self.assertRaises(Cancelled):
                media.download_audio(VIDEO, target, lambda *args: None, check)
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
