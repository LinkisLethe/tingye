import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.asr import (_accept_recovery, _merge_intervals, _recovery_windows,
                     _replace_window, _segment_dict, transcribe_audio)
from app.common import UserError


def seg(start, end, text=" hello", probability=0.9, score=-0.2):
    return {"start": start, "end": end, "text": text.strip(), "avg_logprob": score,
            "no_speech_prob": 0.01, "compression_ratio": 1.1,
            "words": [{"start": start, "end": end, "word": text, "probability": probability}]}


class RecoveryTests(unittest.TestCase):
    def test_decode_keeps_full_audio_without_database_check_per_frame(self):
        import tempfile, wave
        from app.asr import _decode_audio
        checks=[]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'sample.wav'
            with wave.open(str(path),'wb') as out:
                out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
                out.writeframes(b'\x01\x00'*80000)
            audio=_decode_audio(path,lambda p,m:None,lambda:checks.append(1))
        self.assertEqual(len(audio),80000)
        self.assertLess(len(checks),30)

    def test_missing_speech_is_candidate_but_silence_is_not(self):
        segments = [seg(0, 1), seg(6, 7)]
        windows, count, _ = _recovery_windows(segments, [[0, 3], [6, 7]], 7)
        self.assertEqual(count, 1)
        self.assertEqual((windows[0]["start"], windows[0]["end"]), (1, 6))
        self.assertEqual(_recovery_windows(segments, [[0, 1], [6, 7]], 7)[0], [])

    def test_stretched_word_detected_without_gap_or_content_rule(self):
        windows, _, _ = _recovery_windows([seg(0, 1), seg(1, 8, " x")], [[0, 2], [7, 8]], 8)
        self.assertEqual(len(windows), 1)
        self.assertIn("stretched_word", windows[0]["reasons"])

    def test_recovery_budget_and_window_size_are_bounded(self):
        windows, count, budget = _recovery_windows([], [[0, 3600]], 3600)
        self.assertGreater(count, len(windows))
        self.assertLessEqual(len(windows), 12)
        self.assertLessEqual(sum(w["end"] - w["start"] for w in windows), budget)
        self.assertTrue(all(w["end"] - w["start"] <= 22 for w in windows))

    def test_failed_alignment_recovers_whole_existing_phrase(self):
        broken = seg(1, 9, " broken")
        broken["words"] = [seg(1, 1.7, " broken", probability=0.01)["words"][0],
                           seg(7, 9, " phrase")["words"][0]]
        windows, _, _ = _recovery_windows([seg(0, 1), broken], [[0, 2], [7, 9]], 9)
        self.assertEqual((windows[0]["start"], windows[0]["end"]), (1, 9))

    def test_native_numbers_make_model_output_json_serializable(self):
        import numpy as np
        word = SimpleNamespace(start=np.float64(0), end=np.float64(1), word=" test", probability=np.float32(0.9))
        segment = SimpleNamespace(start=np.float64(0), end=np.float64(1), text="test", words=[word],
                                  avg_logprob=np.float64(-0.2), no_speech_prob=np.float32(0.01), compression_ratio=1.1)
        converted = _segment_dict(segment)
        result = _accept_recovery([], [converted], [[0, 1]], {"start": 0, "end": 1, "reasons": ["speech_gap"]})
        json.dumps([converted, result])

    def test_replacement_preserves_words_outside_window(self):
        old = seg(0, 3)
        old["words"] = [seg(i, i + 1, word)["words"][0] for i, word in enumerate([" one", " two", " three"])]
        replaced = _replace_window([old], [seg(1, 2, " middle")], 1, 2)
        self.assertEqual([s["text"] for s in replaced], ["one", "middle", "three"])

    def test_noisy_recovery_cannot_replace_original(self):
        window = {"start": 0, "end": 2, "reasons": ["speech_gap"]}
        accepted, reason, _, _ = _accept_recovery([], [seg(0, 2, " noise", score=-2)], [[0, 2]], window)
        self.assertFalse(accepted)
        self.assertEqual(reason, "recovery_quality_rejected")

    def test_repeated_hallucination_is_rejected(self):
        window = {"start": 0, "end": 2, "reasons": ["speech_gap"]}
        accepted, _, _, _ = _accept_recovery([], [seg(0, 2, " hello hello hello hello")], [[0, 2]], window)
        self.assertFalse(accepted)

    def test_new_speech_with_valid_timing_can_be_accepted(self):
        window = {"start": 0, "end": 2, "reasons": ["speech_gap"]}
        accepted, _, before, after = _accept_recovery([], [seg(0, 2)], [[0, 2]], window)
        self.assertTrue(accepted)
        self.assertGreater(after["speech_coverage_seconds"], before["speech_coverage_seconds"])

    def test_short_recovered_speech_survives_timing_redistribution(self):
        window = {"start": 0, "end": 5, "reasons": ["speech_gap"]}
        original = [seg(0, 0.5, " wrong", probability=0.01), seg(3, 5, " known")]
        replacement = [seg(0, 0.5, " recovered"), seg(4.4, 5, " known")]
        accepted, _, _, after = _accept_recovery(original, replacement, [[0, 0.5], [4, 5]], window)
        self.assertTrue(accepted)
        self.assertEqual(after["newly_covered_speech_seconds"], 0.5)

    def test_cancellation_precedes_model_or_file_access(self):
        def cancel():
            raise InterruptedError("cancelled")
        with self.assertRaises(InterruptedError):
            transcribe_audio(Path("missing"), {}, check_cancel=cancel)

    def test_invalid_settings_fail_before_importing_model(self):
        with self.assertRaises(UserError):
            transcribe_audio(Path("missing"), {"model": "other"})
        with self.assertRaises(UserError):
            transcribe_audio(Path("missing"), {"cpu_threads": 0})

    def test_interval_union_does_not_double_count(self):
        self.assertEqual(_merge_intervals([[0, 2], [1, 3], [4, 5]]), [[0, 3], [4, 5]])


if __name__ == "__main__":
    unittest.main()
