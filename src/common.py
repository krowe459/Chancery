"""Shared helpers: America/New_York clock and crash-safe JSON state I/O.

Every date/time in this project is produced by now_ny(). Nothing calls
datetime.now() without a tzinfo, so the runner's UTC clock never leaks into
state keys.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
PROCESSED_PATH = STATE_DIR / "processed_cases.json"
ACCUMULATION_PATH = STATE_DIR / "daily_accumulation.json"
SENT_PATH = STATE_DIR / "sent_digests.json"
ARCHIVE_DIR = STATE_DIR / "archive"


def now_ny() -> datetime:
    """Current wall-clock time in America/New_York (EST/EDT, DST-aware)."""
    return datetime.now(tz=NY)


def today_ny() -> str:
    """Today's date in New York as YYYY-MM-DD. Used as the digest key."""
    return now_ny().date().isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    """Atomic write: a crash mid-write can never leave a truncated state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def set_github_output(key: str, value: str) -> None:
    """Expose a value to later workflow steps (no-op when run locally)."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default
