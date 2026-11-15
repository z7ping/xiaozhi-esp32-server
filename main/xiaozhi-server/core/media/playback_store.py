"""Persistent playback progress so playback resumes across reconnects.

Progress lives in ``data/playback_progress.json`` (the mounted data
volume), keyed by device id, written atomically and pruned on read.
Resume matches by track title: queue order may change (shuffle, playlist
edits, auto discovery), but the title identifies the content. Everything
here is best-effort — storage failures must never break playback.
"""

import json
import os
import time
from pathlib import Path

MAX_AGE_SECONDS = 7 * 24 * 3600
_NEAR_END_MS = 2000


def _progress_path() -> Path:
    from config.config_loader import get_project_dir

    return Path(get_project_dir()) / "data" / "playback_progress.json"


def device_id_of(conn):
    """Device id from the connection headers (same key the auth uses)."""
    headers = getattr(conn, "headers", None) or {}
    return headers.get("device-id")


def load_progress(device_id, now=None):
    """Return the saved record for this device, or None when stale/absent."""
    if not device_id:
        return None
    try:
        data = json.loads(_progress_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    record = data.get(device_id)
    if not isinstance(record, dict):
        return None
    updated = record.get("updated_at") or 0
    now = time.time() if now is None else now
    if not 0 <= now - updated <= MAX_AGE_SECONDS:
        return None
    return record


def save_progress(device_id, record):
    """Atomically upsert this device's record; never raises."""
    if not device_id or not isinstance(record, dict):
        return
    path = _progress_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[device_id] = record
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def resume_point(progress, tracks):
    """Return ``(index, position_ms)`` for the saved title, else ``(0, 0)``.

    Falls back to the start of the queue when the title is gone or the
    saved position is past the (possibly re-fetched) track duration.
    """
    if not progress or not tracks:
        return 0, 0
    title = progress.get("title")
    if not title:
        return 0, 0
    for index, track in enumerate(tracks):
        if track.title != title:
            continue
        position = int(progress.get("position_ms") or 0)
        duration = int(progress.get("duration_ms") or 0)
        if position <= 0:
            return index, 0
        if duration > 0 and position >= duration:
            return index, 0
        return index, position
    return 0, 0
