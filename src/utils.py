from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def utcish_now_id() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ValueError(f"Expected a numeric Discord channel ID, got {value!r}.") from exc


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(value: str, fallback: str = "Speaker") -> str:
    cleaned = _SAFE_FILENAME_RE.sub("_", value).strip(" ._")
    return cleaned[:80] or fallback


def atomic_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def copy_if_exists(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    ensure_dir(destination.parent)
    shutil.copy2(source, destination)
    return True


def relative_display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve())
