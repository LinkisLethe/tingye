import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from app.common import Cancelled
from app.exporter import ExportError, export_note, validate_destination, _matching_notes

VIDEO = {"bvid":"BV1xx411c7mD","aid":1234,"cid":123,"page":1,"title":"测试：标题","uploader":"作者","duration":90,
         "description":"视频简介","published_at":"2026-01-01T12:00:00Z"}
TRANSCRIPT = {"source":"local_asr","model":"large-v3-turbo","language":"zh",
              "segments":[{"start":1.2,"end":3.5,"text":"原始语音文字"},{"start":61,"end":65,"text":"第二段"}]}

class ExporterTests(unittest.TestCase):
    def setUp(self):
        # MSIX may remap Windows TEMP while parallel handles create descendants.
        # Use the same stable user-directory filesystem as the real application.
        scratch_root=Path.home()/'.tingye'/'test-tmp'
        scratch_root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=scratch_root)
        self.root=Path(self.temp.name)
        self.vault=self.root/"vault"
        self.vault.mkdir()
        self.settings={"vault_path":str(self.vault),"subfolder":"Clippings/Bilibili","keep_audio":False}
        self.backup_patch=patch("app.settings.data_directory",return_value=self.root/"appdata")
        self.backup_patch.start()

    def tearDown(self):
        self.backup_patch.stop()
        self.temp.cleanup()

    def test_clipper_format_and_markdown_only_even_with_audio_option(self):
        audio=self.root/"audio.m4a";audio.write_bytes(b"audio")
        r=export_note(VIDEO,TRANSCRIPT,audio,{**self.settings,"keep_audio":True})
        note=Path(r["note_path"]);text=note.read_text(encoding="utf8")
        self.assertEqual(note.parent,self.vault/"Clippings/Bilibili")
        self.assertRegex(note.name,r"^\d{4}-\d{2}-\d{2}-")
        for field in ("title:","url:","bvid:","cid:","author:","upload_date:","subtitle_lang:","created:","tags:"):
            self.assertIn(field,text)
        self.assertIn("<iframe ",text)
        self.assertIn("autoplay=0",text)
        self.assertIn("## 简介\n\n视频简介",text)
        self.assertIn("## 字幕\n\n\u006000:01\u0060 原始语音文字",text)
        self.assertEqual([p for p in note.parent.iterdir() if not p.name.startswith(".")],[note])
        self.assertFalse(r["audio_retained"])

    def test_overwrites_same_video_and_keeps_filename_after_title_change(self):
        first=export_note(VIDEO,TRANSCRIPT,None,self.settings)
        p=Path(first["note_path"]);p.write_text(p.read_text(encoding="utf8")+"\n旧修改",encoding="utf8")
        newer={**TRANSCRIPT,"segments":[{"start":0,"end":1,"text":"新转录"}]}
        second=export_note({**VIDEO,"title":"改变的标题"},newer,None,self.settings)
        self.assertEqual(first["note_path"],second["note_path"])
        self.assertTrue(second["overwritten"])
        self.assertNotIn("旧修改",p.read_text(encoding="utf8"))
        self.assertIn("新转录",p.read_text(encoding="utf8"))
        with ZipFile(second["backup_path"]) as z:
            self.assertIn("旧修改",z.read(z.namelist()[0]).decode("utf8"))

    def test_removes_exact_duplicates_but_not_other_parts_or_body_mentions(self):
        first=export_note(VIDEO,TRANSCRIPT,None,self.settings)
        p=Path(first["note_path"]);nested=p.parent/"分类";nested.mkdir()
        duplicate=nested/"旧副本.md";duplicate.write_bytes(p.read_bytes())
        unrelated=p.parent/"相关讨论.md";unrelated.write_text("讨论 "+VIDEO["bvid"],encoding="utf8")
        other=export_note({**VIDEO,"page":2},TRANSCRIPT,None,self.settings)
        result=export_note(VIDEO,TRANSCRIPT,None,self.settings)
        self.assertEqual(result["duplicates_removed"],1)
        self.assertFalse(duplicate.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(Path(other["note_path"]).exists())
        self.assertEqual(len(_matching_notes(p.parent,VIDEO,self.vault)),1)

    def test_url_identity_when_clipper_bvid_is_empty(self):
        r=export_note(VIDEO,TRANSCRIPT,None,self.settings);p=Path(r["note_path"])
        p.write_text(p.read_text(encoding="utf8").replace('bvid: "BV1xx411c7mD"','bvid: ""'),encoding="utf8")
        again=export_note(VIDEO,TRANSCRIPT,None,self.settings)
        self.assertEqual(again["note_path"],str(p))
        self.assertTrue(again["overwritten"])

    def test_concurrent_writes_end_with_one_markdown(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:export_note(VIDEO,TRANSCRIPT,None,self.settings),range(4)))
        self.assertEqual(len({r["note_path"] for r in results}),1)
        self.assertEqual(sum(r["overwritten"] for r in results),3)

    def test_collision_with_unrelated_title_is_not_overwritten(self):
        r=export_note(VIDEO,TRANSCRIPT,None,self.settings);p=Path(r["note_path"])
        r2=export_note({**VIDEO,"bvid":"BV1yy411c7mE"},TRANSCRIPT,None,self.settings)
        self.assertNotEqual(r2["note_path"],r["note_path"])
        self.assertIn(VIDEO["bvid"],p.read_text(encoding="utf8"))

    def test_cancel_preserves_old_note_and_cleans_registered_staging(self):
        first=export_note(VIDEO,TRANSCRIPT,None,self.settings);p=Path(first["note_path"]);before=p.read_bytes()
        registry=self.root/"work"/"export-staging.json"
        def cancel():
            if registry.exists():raise Cancelled()
        with self.assertRaises(Cancelled):
            export_note(VIDEO,TRANSCRIPT,None,{**self.settings,"_staging_registry":str(registry),"_attempt_id":"a"*32},cancel)
        self.assertEqual(p.read_bytes(),before)
        self.assertFalse(registry.exists())
        self.assertEqual(list(p.parent.glob(".tingye-export-*")),[])

    def test_invalid_destination_or_timestamp_rejected(self):
        for sub in ("../outside",".obsidian","C:/outside","NUL"):
            with self.subTest(sub=sub),self.assertRaises(ExportError):
                validate_destination({**self.settings,"subfolder":sub})
        with self.assertRaises(ExportError):
            export_note(VIDEO,{**TRANSCRIPT,"segments":[{"start":float("nan"),"end":1,"text":"x"}]},None,self.settings)

if __name__=="__main__":
    unittest.main()
