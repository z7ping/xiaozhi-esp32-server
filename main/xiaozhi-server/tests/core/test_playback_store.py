"""Offline tests for persistent playback progress (resume across reconnects)."""

import json
import time
from types import SimpleNamespace

from core.media import playback_store


def _tracks(*titles):
    return [SimpleNamespace(title=t) for t in titles]


def test_save_load_roundtrip_and_expiry(tmp_path, monkeypatch):
    path = tmp_path / "progress.json"
    monkeypatch.setattr(playback_store, "_progress_path", lambda: path)
    now = time.time()

    playback_store.save_progress("dev1", {
        "title": "故事一", "index": 0, "position_ms": 1000,
        "duration_ms": 60000, "updated_at": now,
    })
    record = playback_store.load_progress("dev1", now=now)
    assert record is not None and record["title"] == "故事一"

    assert playback_store.load_progress("dev2", now=now) is None
    assert playback_store.load_progress(None, now=now) is None
    # Older than the7-day window → treated as absent.
    assert playback_store.load_progress(
        "dev1", now=now + playback_store.MAX_AGE_SECONDS + 1
    ) is None

    playback_store.save_progress("dev2", {"title": "x", "updated_at": now})
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"dev1", "dev2"}


def test_corrupt_progress_file_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "progress.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(playback_store, "_progress_path", lambda: path)
    assert playback_store.load_progress("dev1") is None
    # A later save repairs the file instead of raising.
    playback_store.save_progress("dev1", {"title": "a", "updated_at": time.time()})
    assert playback_store.load_progress("dev1") is not None


def test_resume_point_matches_title_and_clamps():
    tracks = _tracks("甲", "乙", "丙")

    assert playback_store.resume_point(None, tracks) == (0, 0)
    assert playback_store.resume_point({"title": "zz"}, tracks) == (0, 0)

    index, position = playback_store.resume_point(
        {"title": "乙", "position_ms": 42000, "duration_ms": 60000}, tracks
    )
    assert (index, position) == (1, 42000)

    # Saved position past the (re-fetched) duration → restart that track.
    index, position = playback_store.resume_point(
        {"title": "乙", "position_ms": 90000, "duration_ms": 60000}, tracks
    )
    assert (index, position) == (1, 0)

    # No stored duration → trust the saved position as-is.
    index, position = playback_store.resume_point(
        {"title": "丙", "position_ms": 7000}, tracks
    )
    assert (index, position) == (2, 7000)


def test_device_id_of_reads_connection_headers():
    assert playback_store.device_id_of(
        SimpleNamespace(headers={"device-id": "aa:bb"})
    ) == "aa:bb"
    assert playback_store.device_id_of(SimpleNamespace()) is None
    assert playback_store.device_id_of(None) is None
