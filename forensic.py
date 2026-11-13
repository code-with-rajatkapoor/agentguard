"""Forensic logging for AgentGuard.

Every AgentGuard decision is appended to logs/forensics.jsonl as one JSON
object per line. The file is the system of record; GET /api/forensics reads it
back for the dashboard timeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

if os.environ.get("VERCEL"):
    LOG_DIR = Path("/tmp")
else:
    LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "forensics.jsonl"
_LOCK = threading.Lock()
_LAST_HASH: str | None = None


def _ensure_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _compute_hash(prev_hash: str, event: dict[str, Any]) -> str:
    """SHA256(prev_hash + canonical event data). The hash field is excluded."""
    payload = {key: value for key, value in event.items() if key != "hash"}
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((prev_hash + data).encode("utf-8")).hexdigest()


def _last_hash_from_file() -> str:
    """Return the hash of the most recent complete record on disk.

    A partially written trailing line (interrupted append) is skipped: the
    chain continues from the last complete record rather than from "".
    """
    if not LOG_FILE.exists():
        return ""
    last_hash = ""
    with open(LOG_FILE, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            last_hash = event.get("hash") or last_hash
    return last_hash


def _file_ends_with_newline(path: Path) -> bool:
    """True when the log is empty or already ends on a line boundary.

    A previous append can be interrupted (process killed, disk full) and leave
    a partial trailing line. Appending the next record directly would glue it
    onto that garbage line, and read_events()/verify_chain() would then skip
    the new record as unparseable JSON — silent loss of a forensic event.
    """
    try:
        if not path.exists() or path.stat().st_size == 0:
            return True
        with open(path, "rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"
    except OSError:
        return True


def record_event(event: dict[str, Any]) -> None:
    """Append one forensic event to logs/forensics.jsonl (thread-safe).

    Each event is hash-chained: it stores the previous event's hash and its own
    SHA256(prev_hash + event_data), so the log is tamper-evident.
    """
    _ensure_dir()
    global _LAST_HASH
    with _LOCK:
        if _LAST_HASH is None:
            _LAST_HASH = _last_hash_from_file()
        prev_hash = _LAST_HASH or ""
        event["prev_hash"] = prev_hash
        event["hash"] = _compute_hash(prev_hash, event)
        _LAST_HASH = event["hash"]
        line = json.dumps(event, ensure_ascii=False)
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            if not _file_ends_with_newline(LOG_FILE):
                # Start a fresh line so this record stays parseable.
                handle.write("\n")
            handle.write(line + "\n")


def verify_chain() -> dict[str, Any]:
    """Re-check every hash link in the log.

    Returns {"ok", "total", "verified", "legacy", "head_orphaned", "broken_at",
    "last_hash"}.

    Records written before the hash chain existed carry no "hash" and are
    counted in "legacy" and skipped: the chain keeps the previous hash instead
    of silently restarting. Such a record stays visible in read_events() and is
    surfaced by the "legacy" count, but it is not itself verified — inserting a
    hash-less record mid-log does not break the chain, it only raises "legacy".

    A file that does not start at the genesis record (earlier records were
    removed or rotated away) is reported separately through "head_orphaned" and
    the remaining records are still verified, so the operator can see whether
    the intact tail survived instead of being told the whole log is
    unverifiable. Any other mismatch stops verification and is reported
    through "broken_at": a modified or deleted record breaks the chain.
    """
    events = read_events()
    prev = ""
    verified = 0
    legacy = 0
    broken_at: str | None = None
    head_orphaned = False
    seen_hashed = False
    for event in events:
        if "hash" not in event:
            legacy += 1
            continue
        expected = _compute_hash(event.get("prev_hash", ""), event)
        if event.get("prev_hash", "") != prev or expected != event.get("hash"):
            if not seen_hashed and event.get("prev_hash", "") != "":
                head_orphaned = True
                verified += 1
                prev = event["hash"]
                seen_hashed = True
                continue
            broken_at = event.get("event_id")
            break
        verified += 1
        prev = event["hash"]
        seen_hashed = True
    return {
        "ok": broken_at is None and not head_orphaned,
        "total": len(events),
        "verified": verified,
        "legacy": legacy,
        "head_orphaned": head_orphaned,
        "broken_at": broken_at,
        "last_hash": prev,
    }


def read_events() -> list[dict[str, Any]]:
    """Read every forensic event currently on disk (oldest first)."""
    if not LOG_FILE.exists():
        return []
    events: list[dict[str, Any]] = []
    with open(LOG_FILE, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # A partially written line must never break the dashboard.
                continue
    return events


def clear_events() -> None:
    """Empty the forensic log (used by tests / demo resets)."""
    _ensure_dir()
    global _LAST_HASH
    with _LOCK:
        LOG_FILE.write_text("", encoding="utf-8")
        # The in-memory chain head must be re-read from the now-empty file.
        # Otherwise the next event points at a record that no longer exists
        # and verify_chain() reports a break.
        _LAST_HASH = None
