"""Consensus source pool must re-fetch LRCLib when the caller passes None.

Regression for the 2026-07-06 audit P0 (US-55): recompute dispatches
(`_maybe_recompute_consensus`) call the consensus engine with
``lrclib_lrc=None``. The engine used to include LRCLib only when the
parameter was provided, so every rerun silently dropped the strongest
synced source and degraded to "consensus: no sources" (or whisper-only
garbage) even though ``<stem>.lrclib.ass`` existed on disk.
"""

import os
from unittest.mock import patch

import pytest

from pikaraoke.lib.events import EventSystem
from pikaraoke.lib.karaoke_database import KaraokeDatabase
from pikaraoke.lib.lyrics import LyricsService

_LRC = "[00:10.00]I bless the rains down in Africa\n[00:15.00]Gonna take some time"


@pytest.fixture
def song_with_db(tmp_path):
    db = KaraokeDatabase(str(tmp_path / "test.db"))
    song = str(tmp_path / "Toto - Africa---abc12345678.mp4")
    open(song, "w").close()
    db.insert_songs([{"file_path": song, "youtube_id": "abc12345678", "format": "mp4"}])
    sid = db.get_song_id_by_path(song)
    db.update_track_metadata(sid, artist="Toto", title="Africa", duration_seconds=295.0)
    return song, db


def test_consensus_refetches_lrclib_when_not_passed_in(song_with_db):
    song, db = song_with_db
    service = LyricsService(os.path.dirname(song), EventSystem(), aligner=None, db=db)

    captured: dict = {}

    def fake_build_consensus(sources, audio_ref):
        captured["sources"] = sources
        return None  # pool inspection only; no write path

    with (
        patch("pikaraoke.lib.lyrics._fetch_lrclib", return_value=_LRC),
        patch("pikaraoke.lib.lyrics._consensus_providers", return_value=[]),
        patch("pikaraoke.lib.lyrics._whisper_fallback_enabled", return_value=False),
        patch("pikaraoke.lib.lyrics.GENIUS_ACCESS_TOKEN", ""),
        patch("pikaraoke.lib.lyrics._pick_best_vtt", return_value=None),
        patch("pikaraoke.lib.lyrics_consensus.build_consensus", fake_build_consensus),
    ):
        service._upgrade_via_consensus_locked(
            song, {"track": "Africa", "artist": "Toto"}, None, None
        )

    sources = captured.get("sources")
    assert sources, "consensus ran with an empty source pool despite an LRCLib hit"
    lrclib = [s for s in sources if s.name == "lrclib"]
    assert lrclib, f"lrclib missing from pool: {[s.name for s in sources]}"
    assert lrclib[0].lrc == _LRC
    assert lrclib[0].is_synced


def test_consensus_uses_caller_lrc_without_refetch(song_with_db):
    song, db = song_with_db
    service = LyricsService(os.path.dirname(song), EventSystem(), aligner=None, db=db)

    captured: dict = {}

    def fake_build_consensus(sources, audio_ref):
        captured["sources"] = sources
        return None

    with (
        patch("pikaraoke.lib.lyrics._fetch_lrclib") as fetch_mock,
        patch("pikaraoke.lib.lyrics._consensus_providers", return_value=[]),
        patch("pikaraoke.lib.lyrics._whisper_fallback_enabled", return_value=False),
        patch("pikaraoke.lib.lyrics.GENIUS_ACCESS_TOKEN", ""),
        patch("pikaraoke.lib.lyrics._pick_best_vtt", return_value=None),
        patch("pikaraoke.lib.lyrics_consensus.build_consensus", fake_build_consensus),
    ):
        service._upgrade_via_consensus_locked(
            song, {"track": "Africa", "artist": "Toto"}, _LRC, None
        )

    fetch_mock.assert_not_called()
    names = [s.name for s in captured.get("sources", [])]
    assert names.count("lrclib") == 1
