"""Unit tests for the per-song event helpers in routes.files."""

import json
from unittest.mock import MagicMock, patch

import pytest
import werkzeug
from flask import Flask

if not hasattr(werkzeug, "__version__"):
    werkzeug.__version__ = "3.0.0"

from pikaraoke.lib.karaoke_database import KaraokeDatabase
from pikaraoke.routes.admin import admin_bp
from pikaraoke.routes.files import (
    _apply_metadata_edit,
    _decorate_event,
    _youtube_id_from_path,
)


class TestYoutubeIdFromPath:
    def test_pikaraoke_format(self):
        assert _youtube_id_from_path("/songs/Artist - Song---abc12345678.mp4") == "abc12345678"

    def test_ytdlp_bracket_format(self):
        assert _youtube_id_from_path("/songs/Artist - Song [abc12345678].mp4") == "abc12345678"

    def test_no_id(self):
        assert _youtube_id_from_path("/songs/uploaded.mp4") == ""


class TestDecorateEvent:
    def test_adds_local_time_string(self):
        ev = {"timestamp": 1700000000.0, "message": "x"}
        out = _decorate_event(ev)
        assert "time_str" in out
        # HH:MM:SS shape — value depends on local tz, just verify the format.
        assert len(out["time_str"]) == 8 and out["time_str"].count(":") == 2
        # Original entry untouched.
        assert "time_str" not in ev

    def test_missing_timestamp_yields_empty_string(self):
        out = _decorate_event({"message": "x"})
        assert out["time_str"] == ""

    def test_invalid_timestamp_yields_empty_string(self):
        # Far-future or otherwise unrepresentable timestamps must not crash.
        out = _decorate_event({"timestamp": "not-a-number", "message": "x"})
        assert out["time_str"] == ""


@pytest.fixture
def admin_client():
    test_app = Flask(__name__)
    test_app.register_blueprint(admin_bp)
    return test_app.test_client()


class TestSongEventsRoute:
    """JSON endpoint backing the edit-view refresh button."""

    @patch("pikaraoke.routes.admin.get_karaoke_instance")
    @patch("pikaraoke.routes.admin.is_admin", return_value=True)
    def test_returns_events_for_song(self, _admin, mock_get, admin_client):
        k = MagicMock()
        k.get_song_events_for.return_value = [{"phase": "download", "message": "x"}]
        mock_get.return_value = k

        resp = admin_client.get("/song_events?song=Foo.mp4&youtube_id=abc12345678")

        assert resp.status_code == 200
        assert json.loads(resp.data) == {"events": [{"phase": "download", "message": "x"}]}
        k.get_song_events_for.assert_called_once_with(song="Foo.mp4", youtube_id="abc12345678")

    @patch("pikaraoke.routes.admin.get_karaoke_instance")
    @patch("pikaraoke.routes.admin.is_admin", return_value=True)
    def test_no_keys_returns_empty_without_hitting_karaoke(self, _admin, mock_get, admin_client):
        k = MagicMock()
        mock_get.return_value = k

        resp = admin_client.get("/song_events")

        assert resp.status_code == 200
        assert json.loads(resp.data) == {"events": []}
        k.get_song_events_for.assert_not_called()

    @patch("pikaraoke.routes.admin.is_admin", return_value=False)
    def test_requires_admin(self, _admin, admin_client):
        resp = admin_client.get("/song_events?song=x")
        assert resp.status_code == 403


# GET /api/songs/<id>/subtitles was deprecated in Phase 2 — the picker now
# reads ``subtitle_sources`` straight from the ``now_playing`` payload, and
# bulk lookups go through POST /api/songs/subtitles/bulk (see
# tests/unit/test_subtitle_jobs_routes.py).


@pytest.fixture
def edit_ctx(tmp_path):
    """Karaoke MagicMock with a real DB and one song row pre-inserted."""
    db = KaraokeDatabase(str(tmp_path / "test.db"))
    db.insert_songs([{"file_path": "/songs/x.mp4", "youtube_id": "abc12345678", "format": "mp4"}])
    song_id = db.get_song_id_by_path("/songs/x.mp4")
    k = MagicMock()
    k.db = db
    yield k, db, song_id
    db.close()


class TestApplyMetadataEdit:
    """The seam that turns a rename into a DB write + lyrics re-fetch."""

    def test_writes_artist_title_with_manual_provenance(self, edit_ctx):
        k, db, song_id = edit_ctx
        _apply_metadata_edit(k, "/songs/x.mp4", "Perfect - Autobiografia", "")
        row = db.get_song_by_id(song_id)
        assert row["artist"] == "Perfect"
        assert row["title"] == "Autobiografia"
        sources = json.loads(row["metadata_sources"])
        assert sources["artist"] == "manual"
        assert sources["title"] == "manual"

    def test_writes_language_when_provided(self, edit_ctx):
        k, db, song_id = edit_ctx
        _apply_metadata_edit(k, "/songs/x.mp4", "Perfect - Autobiografia", "pl")
        row = db.get_song_by_id(song_id)
        assert row["language"] == "pl"
        sources = json.loads(row["metadata_sources"])
        assert sources["language"] == "manual"

    def test_empty_language_keeps_existing_value(self, edit_ctx):
        k, db, song_id = edit_ctx
        # Seed an enricher-written language; an empty manual edit must not clear it.
        db.update_track_metadata_with_provenance(song_id, "scanner", {"language": "en"})
        _apply_metadata_edit(k, "/songs/x.mp4", "Perfect - Autobiografia", "")
        row = db.get_song_by_id(song_id)
        assert row["language"] == "en"

    def test_title_only_preserves_existing_artist(self, edit_ctx):
        k, db, song_id = edit_ctx
        db.update_track_metadata_with_provenance(
            song_id, "scanner", {"artist": "Original", "title": "OldTitle"}
        )
        _apply_metadata_edit(k, "/songs/x.mp4", "JustATitle", "")
        row = db.get_song_by_id(song_id)
        assert row["artist"] == "Original"
        assert row["title"] == "JustATitle"

    def test_manual_write_beats_subsequent_enrichment(self, edit_ctx):
        k, db, song_id = edit_ctx
        _apply_metadata_edit(k, "/songs/x.mp4", "Perfect - Autobiografia", "pl")
        # Simulate an iTunes enrichment trying to overwrite later — manual
        # provenance (confidence 100) must shut it out.
        db.update_track_metadata_with_provenance(
            song_id, "itunes", {"artist": "Wrong", "title": "Wrong", "language": "en"}
        )
        row = db.get_song_by_id(song_id)
        assert row["artist"] == "Perfect"
        assert row["title"] == "Autobiografia"
        assert row["language"] == "pl"

    def test_invalidates_lyrics_cache_and_kicks_off_refetch(self, edit_ctx):
        k, _db, _sid = edit_ctx
        _apply_metadata_edit(k, "/songs/x.mp4", "Perfect - Autobiografia", "")
        k.lyrics_service.invalidate_for_metadata_change.assert_called_once_with("/songs/x.mp4")
        k._dispatch_lyrics_fetch_async.assert_called_once_with("/songs/x.mp4")
        k.subtitle_orchestrator.kickoff.assert_called_once_with("/songs/x.mp4", force=True)

    def test_no_op_when_song_not_in_db(self, edit_ctx):
        k, _db, _sid = edit_ctx
        _apply_metadata_edit(k, "/songs/missing.mp4", "Perfect - Autobiografia", "pl")
        k.lyrics_service.invalidate_for_metadata_change.assert_not_called()
        k._dispatch_lyrics_fetch_async.assert_not_called()
        k.subtitle_orchestrator.kickoff.assert_not_called()
