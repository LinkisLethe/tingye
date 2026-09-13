"""Local speech recognition with bounded, signal-based automatic recovery.

All recognition and language detection use the local Whisper model. The optional
OpenCC step only changes traditional Chinese characters to simplified Chinese.
"""

from __future__ import annotations

import math
import os
import re
import time
from pathlib import Path

from .common import UserError

SAMPLE_RATE = 16000
MODELS = {"small", "large-v3-turbo"}
MAX_RECOVERY_WINDOWS = 12
MAX_RECOVERY_SECONDS = 180.0
RECOVERY_CONTEXT_SECONDS = 2.5


def _merge_intervals(intervals, distance=0.0):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1] + distance:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([float(start), float(end)])
    return result


def _overlap(intervals, start, end):
    return sum(max(0.0, min(b, end) - max(a, start)) for a, b in intervals)


def _word_intervals(segments, reliable=True):
    intervals = []
    for segment in segments:
        for word in segment.get("words", []):
            start, end = word["start"], word["end"]
            if reliable and (end - start > 2.2 or word.get("probability", 1) < 0.18):
                continue
            if end > start:
                intervals.append((start, end))
    return _merge_intervals(intervals)


def _recovery_windows(segments, speech, duration):
    """Find speech with missing/implausible word timing, without content rules."""
    coverage = _word_intervals(segments)
    candidates = []
    boundaries = _merge_intervals([[0, 0]] + coverage + [[duration, duration]])
    cursor = 0.0
    for start, end in boundaries + [[duration, duration]]:
        if start - cursor >= 1.0 and _overlap(speech, cursor + 0.15, start - 0.15) >= 0.65:
            gap_start, gap_end = cursor, start
            # Keep an existing phrase together if its failed alignment produced
            # the gap. A boundary shared with an adjacent phrase is not overlap.
            for segment in segments:
                if segment["start"] < gap_end - 0.001 and segment["end"] > gap_start + 0.001:
                    union_start = min(gap_start, segment["start"])
                    union_end = max(gap_end, segment["end"])
                    if union_end - union_start <= 22:
                        gap_start, gap_end = union_start, union_end
            candidates.append({"start": gap_start, "end": gap_end, "reasons": ["speech_gap"]})
        cursor = max(cursor, end)
    for segment in segments:
        words = segment.get("words", [])
        for word in words:
            span = word["end"] - word["start"]
            if span > 2.2 or (span >= 1.2 and word.get("probability", 1) < 0.35):
                start, end = segment["start"], segment["end"]
                if end - start > 22:
                    start, end = word["start"], word["end"]
                if _overlap(speech, start, end) >= 0.5:
                    candidates.append({"start": start, "end": end, "reasons": ["stretched_word"]})
        if segment.get("avg_logprob", 0) < -1.0 and segment["end"] - segment["start"] >= 1:
            if _overlap(speech, segment["start"], segment["end"]) >= 0.8:
                candidates.append({"start": segment["start"], "end": segment["end"], "reasons": ["low_probability"]})
    merged = []
    for item in sorted(candidates, key=lambda c: c["start"]):
        if merged and (item["start"] < merged[-1]["end"] or
                       (item["start"] <= merged[-1]["end"] + 0.7 and item["end"] - merged[-1]["start"] <= 22)):
            merged[-1]["end"] = max(item["end"], merged[-1]["end"])
            merged[-1]["reasons"] = sorted(set(merged[-1]["reasons"] + item["reasons"]))
        else:
            merged.append(dict(item))
    split = []
    for item in merged:
        start = item["start"]
        while start < item["end"]:
            end = min(start + 22, item["end"])
            if _overlap(speech, start, end) >= 0.5:
                split.append({"start": start, "end": end, "reasons": item["reasons"]})
            start = end
    max_windows = min(MAX_RECOVERY_WINDOWS, max(2, math.ceil(duration / 120) * 2 + 2))
    budget = min(MAX_RECOVERY_SECONDS, max(30.0, duration * 0.2))
    selected, used = [], 0.0
    for item in sorted(split, key=lambda c: ("speech_gap" not in c["reasons"], "stretched_word" not in c["reasons"], c["start"])):
        length = min(duration, item["end"] + RECOVERY_CONTEXT_SECONDS) - max(0, item["start"] - RECOVERY_CONTEXT_SECONDS)
        if len(selected) < max_windows and used + length <= budget:
            selected.append(item)
            used += length
    return sorted(selected, key=lambda c: c["start"]), len(split), budget


def _segment_dict(segment, offset=0.0):
    return {
        "start": round(float(max(0.0, segment.start + offset)), 3),
        "end": round(float(max(0.0, segment.end + offset)), 3),
        "text": segment.text.strip(),
        "avg_logprob": float(segment.avg_logprob),
        "no_speech_prob": float(segment.no_speech_prob),
        "compression_ratio": float(segment.compression_ratio),
        "words": [{"start": round(float(max(0.0, w.start + offset)), 3),
                   "end": round(float(max(0.0, w.end + offset)), 3),
                   "word": w.word, "probability": float(w.probability)}
                  for w in (segment.words or [])],
    }


def _from_words(segment, words):
    result = dict(segment)
    result.update(start=words[0]["start"], end=words[-1]["end"], words=words,
                  text="".join(word["word"] for word in words).strip())
    return result


def _inside(segments, start, end):
    result = []
    for segment in segments:
        words = [dict(w, start=max(start, w["start"]), end=min(end, w["end"]))
                 for w in segment.get("words", []) if start <= (w["start"] + w["end"]) / 2 < end]
        if words:
            result.append(_from_words(segment, words))
        elif not segment.get("words") and start <= (segment["start"] + segment["end"]) / 2 < end:
            result.append(dict(segment))
    return result


def _replace_window(segments, replacement, start, end):
    """Preserve words outside the replacement boundary, including split lines."""
    result = _inside(segments, -1, start) + replacement + _inside(segments, end, float("inf"))
    return sorted((s for s in result if s["text"] and s["end"] > s["start"]), key=lambda s: (s["start"], s["end"]))


def _quality(segments, speech, start, end):
    words = [w for s in segments for w in s.get("words", [])]
    text = " ".join(s["text"] for s in segments)
    counts = [max(1, len(s.get("words", []))) for s in segments]
    total = sum(counts)
    score = sum(s.get("avg_logprob", -2) * n for s, n in zip(segments, counts)) / max(total, 1)
    covered = _word_intervals(segments)
    # VAD overlap rewards recovered speech, not words stretched across silence.
    coverage = sum(_overlap(speech, max(start, a), min(end, b)) for a, b in covered if min(end, b) > max(start, a))
    long_words = sum(w["end"] - w["start"] > 2.2 for w in words)
    repeated = bool(re.search(r"(.{2,16})\1{3,}", re.sub(r"\s+", "", text), re.I))
    good = bool(segments and text.strip()) and score >= -1.0 and not repeated
    good = good and all(s.get("compression_ratio", 0) < 2.8 and s.get("no_speech_prob", 0) < 0.6 for s in segments)
    return {"log_probability": round(score, 4), "speech_coverage_seconds": round(coverage, 3), "stretched_words": long_words, "usable": good}


def _accept_recovery(original, replacement, speech, window):
    before = _quality(original, speech, window["start"], window["end"])
    after = _quality(replacement, speech, window["start"], window["end"])
    if not after["usable"]:
        return False, "recovery_quality_rejected", before, after
    gain = after["speech_coverage_seconds"] - before["speech_coverage_seconds"]
    before_intervals = _word_intervals(original)
    novel = []
    for start, end in _word_intervals(replacement):
        cursor = start
        for a, b in before_intervals:
            if b <= cursor:
                continue
            if a >= end:
                break
            if a > cursor:
                novel.append([cursor, min(a, end)])
            cursor = max(cursor, b)
        if cursor < end:
            novel.append([cursor, end])
    newly_covered = sum(_overlap(speech, max(window["start"], a), min(window["end"], b)) for a, b in novel)
    after["newly_covered_speech_seconds"] = round(newly_covered, 3)
    improved_timing = after["stretched_words"] < before["stretched_words"] and gain >= -0.2
    improved_probability = after["log_probability"] >= before["log_probability"] + 0.2 and gain >= -0.2
    accepted = (gain >= 0.5 or (newly_covered >= 0.45 and gain >= -0.25) or improved_timing or
                ("low_probability" in window["reasons"] and improved_probability))
    return bool(accepted), "accepted" if accepted else "no_measured_improvement", before, after


def _decode_audio(path, progress, check_cancel):
    import av
    import numpy as np

    chunks, sample_count, last_percent = [], 0, -1
    next_cancel_check = 0.0
    with av.open(str(path), mode="r", metadata_errors="ignore") as container:
        if not container.streams.audio:
            raise UserError("文件中没有可识别的音轨")
        total = float(container.duration / av.time_base) if container.duration else 0.0
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            now = time.monotonic()
            if now >= next_cancel_check:
                check_cancel()
                next_cancel_check = now + 0.2
            frame.pts = None
            for converted in resampler.resample(frame):
                arr = converted.to_ndarray().flatten()
                chunks.append(arr)
                sample_count += arr.size
            percent = min(9, int(sample_count / SAMPLE_RATE / max(total, 1) * 9)) if total else 0
            if percent > last_percent:
                progress(percent, "正在解码音轨")
                last_percent = percent
        for converted in resampler.resample(None):
            chunks.append(converted.to_ndarray().flatten())
    check_cancel()
    if not chunks:
        raise UserError("音轨为空，无法转写")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def _speech_intervals(audio, check_cancel):
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    speech = []
    # Cancellation checkpoints bound VAD work to at most one minute of audio.
    block = SAMPLE_RATE * 60
    options = VadOptions(min_speech_duration_ms=250, min_silence_duration_ms=500, speech_pad_ms=120)
    for offset in range(0, len(audio), block):
        check_cancel()
        timestamps = get_speech_timestamps(audio[offset:offset + block], options)
        speech.extend(((x["start"] + offset) / SAMPLE_RATE, (x["end"] + offset) / SAMPLE_RATE) for x in timestamps)
    return _merge_intervals(speech)


def transcribe_audio(path: Path, settings: dict, progress=lambda percent, message: None, check_cancel=lambda: None) -> dict:
    """Transcribe locally and return a JSON-serializable, auditable result."""
    started = time.monotonic()
    check_cancel()
    model_name = settings.get("model", "large-v3-turbo")
    language = settings.get("language", "auto")
    if model_name not in MODELS:
        raise UserError("请选择 small 或 large-v3-turbo 语音模型")
    if language not in {"auto", "zh", "en"}:
        raise UserError("请选择自动、中文或英文")
    try:
        threads = int(settings.get("cpu_threads", min(8, os.cpu_count() or 4)))
    except (ValueError, TypeError):
        raise UserError("CPU 线程数需要是整数") from None
    if not 1 <= threads <= 64:
        raise UserError("CPU 线程数需要在 1 到 64 之间")
    hotwords = str(settings.get("hotwords", "") or "").strip()
    device = settings.get("_device", "cpu")
    if device not in {"cpu", "cuda"}:
        raise UserError("不支持这个计算设备")
    compute_type = "float16" if device == "cuda" else "int8"
    model_location = settings.get("_model_path", model_name)
    if len(hotwords) > 1000:
        raise UserError("提示词请控制在 1000 个字符以内")
    if not Path(path).is_file():
        raise UserError("没有找到下载的音轨文件")
    # Ordinary Windows accounts must never need symlink privileges.
    os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "20")
    try:
        from faster_whisper import WhisperModel, __version__ as engine_version
        from opencc import OpenCC
    except ImportError as exc:
        raise UserError("本地语音依赖尚未安装，请运行安装脚本") from exc
    audio = _decode_audio(path, progress, check_cancel)
    duration = len(audio) / SAMPLE_RATE
    progress(10, "正在准备本地语音模型，首次使用会下载模型文件")
    model_started = time.monotonic()
    # Prefer cached files so repeat use works without any network connection.
    try:
        model = WhisperModel(model_location, device=device, compute_type=compute_type, cpu_threads=threads, num_workers=1, local_files_only=True)
        model_cached = True
    except (OSError, ValueError):
        check_cancel()
        model = WhisperModel(model_location, device=device, compute_type=compute_type, cpu_threads=threads, num_workers=1)
        model_cached = False
    model_seconds = time.monotonic() - model_started
    check_cancel()
    progress(13, "正在检测语音区间")
    speech = _speech_intervals(audio, check_cancel)
    check_cancel()
    progress(15, "正在本地转写 0%")
    decode_options = dict(beam_size=5, word_timestamps=True, temperature=0.0,
                          condition_on_previous_text=False, task="transcribe",
                          hotwords=hotwords or None, repetition_penalty=1.05,
                          no_speech_threshold=0.6, log_prob_threshold=-1.0)
    raw_segments, info = model.transcribe(
        audio, language=None if language == "auto" else language,
        multilingual=language == "auto", vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 400},
        **decode_options,
    )
    segments, last_percent = [], -1
    for segment in raw_segments:
        check_cancel()
        item = _segment_dict(segment)
        if item["text"] and item["end"] > item["start"]:
            segments.append(item)
        percent = min(100, int(segment.end / max(duration, 0.001) * 100))
        if percent > last_percent:
            progress(15 + percent * 0.68, f"正在本地转写 {percent}%")
            last_percent = percent
    check_cancel()
    windows, candidate_count, recovery_budget = _recovery_windows(segments, speech, duration)
    records = []
    for index, window in enumerate(windows):
        check_cancel()
        progress(84 + index / max(1, len(windows)) * 13, f"正在自动补转语音片段 {index + 1}/{len(windows)}")
        start = max(0.0, window["start"] - RECOVERY_CONTEXT_SECONDS)
        end = min(duration, window["end"] + RECOVERY_CONTEXT_SECONDS)
        clip = audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
        core = audio[int(window["start"] * SAMPLE_RATE):int(window["end"] * SAMPLE_RATE)]
        record = dict(window, decode_start=round(start, 3), decode_end=round(end, 3), accepted=False)
        if language == "auto":
            local_language, probability, _ = model.detect_language(audio=core, vad_filter=False)
            record.update(language=local_language, language_probability=round(float(probability), 4))
            check_cancel()
            if probability < 0.45:
                record["outcome"] = "language_uncertain"
                records.append(record)
                continue
        else:
            local_language = language
            record.update(language=language, language_probability=None)
        generated, _ = model.transcribe(clip, language=local_language, multilingual=False,
                                        vad_filter=False, **decode_options)
        replacement = []
        for segment in generated:
            check_cancel()
            replacement.append(_segment_dict(segment, start))
        replacement = _inside(replacement, window["start"], window["end"])
        original = _inside(segments, window["start"], window["end"])
        accepted, outcome, before, after = _accept_recovery(original, replacement, speech, window)
        record.update(accepted=accepted, outcome=outcome, before=before, after=after)
        if accepted:
            segments = _replace_window(segments, replacement, window["start"], window["end"])
        records.append(record)
    check_cancel()
    progress(98, "正在生成简体文字和时间戳")
    converter = OpenCC("t2s")
    for segment in segments:
        check_cancel()
        segment["text"] = converter.convert(segment["text"])
        for word in segment["words"]:
            word["word"] = converter.convert(word["word"])
        segment["start"] = min(duration, max(0, segment["start"]))
        segment["end"] = min(duration, max(segment["start"], segment["end"]))
    progress(100, "本地转写完成")
    return {
        "segments": segments, "language": info.language, "source": "local_asr",
        "duration": round(duration, 3), "model": model_name,
        "metadata": {
            "engine": "faster-whisper", "engine_version": engine_version,
            "device": device, "compute_type": compute_type, "cpu_threads": threads,
            "requested_language": language, "language_probability": float(info.language_probability),
            "model_loaded_from_cache": model_cached, "model_load_seconds": round(model_seconds, 2),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "simplified_chinese": "OpenCC t2s", "hotwords_used": bool(hotwords),
            "llm_used": False, "visual_verification": False, "manual_corrections": False,
            "recovery": {"method": "vad_word_timing_local_language_detection", "candidate_count": candidate_count,
                         "attempted": len(records), "transcribed": sum("before" in r for r in records),
                         "accepted": sum(bool(r["accepted"]) for r in records),
                         "budget_seconds": round(recovery_budget, 2), "max_windows": MAX_RECOVERY_WINDOWS,
                         "context_seconds": RECOVERY_CONTEXT_SECONDS,
                         "records": records},
            "limitations": ["语音识别可能误写专有名词、数字，或漏掉重叠和较轻的声音",
                            "自动补转按语音活动和时间戳异常触发，不能保证发现所有漏转",
                            "自动补转受次数和音频时长上限约束，模型概率不代表文字准确率",
                            "未结合画面、调用大语言模型或进行人工校对"],
        },
    }
