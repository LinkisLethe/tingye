"""Local preferences and Obsidian vault discovery without reading notes."""
from pathlib import Path, PureWindowsPath
import json
import os
import re
import tempfile

from .common import UserError

MODELS = {"large-v3-turbo": "准确优先", "small": "轻量模型"}


def data_directory():
    # A normal home folder stays consistent across packaged and unpackaged
    # Windows launchers; LOCALAPPDATA can be virtualized by a packaged parent.
    return Path(os.environ.get("TINGYE_DATA_DIR", str(Path.home() / ".tingye"))).resolve()


def discover_vaults():
    config = Path(os.environ.get("APPDATA", Path.home())) / "obsidian" / "obsidian.json"
    try:
        values = json.loads(config.read_text(encoding="utf-8-sig")).get("vaults", {}).values()
        values = sorted(values, key=lambda item: item.get("ts", 0), reverse=True)
        return [{"name": Path(v["path"]).name, "path": str(Path(v["path"]).resolve())} for v in values if Path(v.get("path", "")).is_dir() and (Path(v.get("path", "")) / ".obsidian").is_dir()]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def defaults():
    vaults = discover_vaults()
    return {"vault_path": vaults[0]["path"] if vaults else "", "subfolder": "Clippings/Bilibili", "model": "large-v3-turbo", "language": "auto", "cpu_threads": max(1, min(8, (os.cpu_count() or 2) // 2)), "keep_audio": False, "prefer_subtitles": True, "hotwords": ""}


def validate_settings(value):
    if not isinstance(value, dict):
        raise UserError("设置格式不正确。")
    result = defaults()
    for key in result:
        if key in value:
            result[key] = value[key]
    for key in ("vault_path", "subfolder", "model", "language", "hotwords"):
        if not isinstance(result[key], str):
            raise UserError("请检查保存位置和转写设置。")
    vault = Path(result["vault_path"].strip()).expanduser()
    if not result["vault_path"].strip() or not vault.is_dir() or not (vault / ".obsidian").is_dir():
        raise UserError("请选择已有的 Obsidian 笔记库文件夹，里面应有 .obsidian 文件夹。")
    result["vault_path"] = str(vault.resolve())
    sub = result["subfolder"].strip().replace("\\", "/")
    parts = sub.split("/") if sub else []
    if len(sub) > 120 or PureWindowsPath(sub).is_absolute() or PureWindowsPath(sub).drive:
        raise UserError("请填写笔记库内的子文件夹，例如 B站摘录。")
    for part in parts:
        if not part or part in (".", "..") or part.startswith(".") or part.endswith((" ", ".")) or re.search(r'[<>:"|?*\x00-\x1f]', part) or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I):
            raise UserError("子文件夹名称不能包含系统保留名称、隐藏目录或上级路径。")
    result["subfolder"] = sub
    if result["model"] not in MODELS or result["language"] not in ("auto", "zh", "en"):
        raise UserError("请选择列表中的模型和语言。")
    if isinstance(result["cpu_threads"], bool) or not isinstance(result["cpu_threads"], int) or not 1 <= result["cpu_threads"] <= min(32, os.cpu_count() or 1):
        raise UserError("处理线程数超出这台电脑可用的范围。")
    for key in ("keep_audio", "prefer_subtitles"):
        if not isinstance(result[key], bool):
            raise UserError("选项格式不正确。")
    result["keep_audio"] = False
    if len(result["hotwords"]) > 500:
        raise UserError("术语提示请控制在500字以内。")
    result["hotwords"] = result["hotwords"].strip()
    return result


def atomic_json(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".write-", suffix=".json", delete=False) as out:
        json.dump(content, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
        temporary = Path(out.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_settings(directory):
    result = defaults()
    try:
        saved = json.loads((directory / "settings.json").read_text(encoding="utf-8"))
        result.update({k: v for k, v in saved.items() if k in result})
    except (OSError, ValueError, AttributeError):
        pass
    result["keep_audio"] = False
    return result


def model_status():
    cache = Path(os.environ.get("HF_HUB_CACHE", Path.home() / ".cache" / "huggingface" / "hub"))
    repositories = {"small": "models--Systran--faster-whisper-small", "large-v3-turbo": "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"}
    return [{"id": ident, "label": label, "cached": any(path.stat().st_size > 100_000_000 for path in (cache / repositories[ident] / "snapshots").glob("*/model.bin"))} for ident, label in MODELS.items()]
