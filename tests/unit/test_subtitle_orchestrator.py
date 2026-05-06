"""Unit tests for SubtitleOrchestrator (Phase 1)."""

from unittest.mock import MagicMock

import pytest

from pikaraoke.lib.events import EventSystem
from pikaraoke.lib.karaoke_database import KaraokeDatabase
from pikaraoke.lib.subtitle_orchestrator import (
    DEFAULT_AUTO_SOURCES,
    SubtitleOrchestrator,
)


@pytest.fixture
def db(tmp_path):
    d = KaraokeDatabase(str(tmp_path / "x.db"))
    yield d
    d.close()


@pytest.fixture
def events():
    return EventSystem()


def _insert_song(db, file_path: str = "/songs/x.mp4") -> int:
    db.insert_songs([{"file_path": file_path, "youtube_id": "abc12345xyz", "format": "mp4"}])
    return db.get_song_id_by_path(file_path)


def _make_lyrics_service(*, results: dict[str, dict]) -> MagicMock:
    """Stub LyricsService with a scripted ``fetch_variant_sync`` per source."""

    svc = MagicMock()

    def _sync(_song_path, source):
        # Default to ``not_found`` so any source not scripted returns a
        # plausible failure rather than an empty dict.
        return results.get(source, {"state": "failed", "error_code": "not_found"})

    svc.fetch_variant_sync.side_effect = _sync
    return svc


class TestKickoff:
    def test_skips_when_song_not_in_db(self, db, events):
        svc = MagicMock()
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib",), max_workers=2)
        try:
            orch.kickoff("/songs/missing.mp4")
        finally:
            orch.shutdown()
        # No job rows, no fetch attempts.
        svc.fetch_variant_sync.assert_not_called()

    def test_marks_each_source_terminal_state(self, db, events, tmp_path):
        # Real song path so ``os.path.exists(variant_ass_path(...))`` returns
        # False (no pre-existing variants); the orchestrator must dispatch.
        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        sid = _insert_song(db, song_path)
        svc = _make_lyrics_service(
            results={
                "lrclib": {"state": "success", "tier": "line"},
                "AI": {"state": "failed", "error_code": "not_found"},
            }
        )
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib", "AI"), max_workers=2)
        try:
            orch.kickoff(song_path)
        finally:
            orch.shutdown(wait=True)

        rows = {r["source"]: r for r in db.get_subtitle_jobs(sid)}
        assert rows["lrclib"]["state"] == "success"
        assert rows["lrclib"]["tier"] == "line"
        assert rows["lrclib"]["finished_at"] is not None
        assert rows["lrclib"]["attempt_count"] >= 1
        assert rows["AI"]["state"] == "failed"
        assert rows["AI"]["error_code"] == "not_found"

    def test_cache_hit_short_circuits_dispatch(self, db, events, tmp_path):
        """Pre-existing variant ASS file → success without calling fetch."""
        song_path = str(tmp_path / "cache_hit.mp4")
        with open(song_path, "wb"):
            pass
        # Variant file pre-exists on disk.
        with open(str(tmp_path / "cache_hit.lrclib.ass"), "w") as f:
            f.write("[Script Info]\n")
        sid = _insert_song(db, song_path)
        svc = _make_lyrics_service(results={})
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib",), max_workers=2)
        try:
            orch.kickoff(song_path)
        finally:
            orch.shutdown(wait=True)

        rows = {r["source"]: r for r in db.get_subtitle_jobs(sid)}
        assert rows["lrclib"]["state"] == "success"
        # No worker fired.
        svc.fetch_variant_sync.assert_not_called()

    def test_emits_subtitle_job_update_per_transition(self, db, events, tmp_path):
        events_seen: list[dict] = []
        events.on("subtitle_job_update", lambda d: events_seen.append(d))

        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        _insert_song(db, song_path)
        svc = _make_lyrics_service(results={"lrclib": {"state": "success", "tier": "line"}})
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib",), max_workers=2)
        try:
            orch.kickoff(song_path)
        finally:
            orch.shutdown(wait=True)

        states = [e["state"] for e in events_seen if e["source"] == "lrclib"]
        # Expected lifecycle: queued -> running -> success.
        assert states == ["queued", "running", "success"]
        # Picker payload: ``label`` + UI-translated ``status`` ride alongside
        # the raw ``state`` so the picker doesn't need a label-registry fetch
        # or its own state→status translation table.
        statuses = [e["status"] for e in events_seen if e["source"] == "lrclib"]
        assert statuses == ["queued", "downloading", "ready"]
        for evt in events_seen:
            assert evt["song"] == "song.mp4"
            assert evt["label"] == "LRCLib"

    def test_orchestrator_crash_recorded_as_failed(self, db, events, tmp_path):
        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        sid = _insert_song(db, song_path)
        svc = MagicMock()
        svc.fetch_variant_sync.side_effect = RuntimeError("boom")
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib",), max_workers=2)
        try:
            orch.kickoff(song_path)
        finally:
            orch.shutdown(wait=True)

        row = db.get_subtitle_job(sid, "lrclib")
        assert row["state"] == "failed"
        assert row["error_code"] == "orchestrator_crash"
        assert "boom" in (row["error_message"] or "")

    def test_no_lyrics_warning_emitted_once_when_all_fail(self, db, events, tmp_path):
        warnings: list[dict] = []
        events.on("song_warning", lambda d: warnings.append(d))

        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        _insert_song(db, song_path)
        svc = _make_lyrics_service(results={})  # everything not_found
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib", "AI"), max_workers=2)
        try:
            orch.kickoff(song_path)
        finally:
            orch.shutdown(wait=True)

        no_lyrics = [w for w in warnings if w.get("message") == "No lyrics found"]
        # One warning total — not one per source.
        assert len(no_lyrics) == 1
        assert no_lyrics[0]["song"] == "song.mp4"

    def test_no_warning_when_one_source_succeeds(self, db, events, tmp_path):
        warnings: list[dict] = []
        events.on("song_warning", lambda d: warnings.append(d))

        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        _insert_song(db, song_path)
        svc = _make_lyrics_service(results={"lrclib": {"state": "success", "tier": "line"}})
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib", "AI"), max_workers=2)
        try:
            orch.kickoff(song_path)
        finally:
            orch.shutdown(wait=True)

        no_lyrics = [w for w in warnings if w.get("message") == "No lyrics found"]
        assert no_lyrics == []


class TestConfig:
    def test_rejects_unknown_source(self, db, events):
        svc = MagicMock()
        with pytest.raises(ValueError):
            SubtitleOrchestrator(svc, events, db, sources=("not_a_source",))

    def test_default_sources_match_variant_universe(self):
        # Sanity: every default source must be a real variant source so the
        # orchestrator can never queue a no-op kickoff.
        from pikaraoke.lib.karaoke_database import VARIANT_FILE_SOURCES

        assert set(DEFAULT_AUTO_SOURCES).issubset(VARIANT_FILE_SOURCES)


class TestBackoff:
    """A failed (song, source) row with a future ``next_retry_at`` is not
    re-queued. Bypassed by ``force=True``."""

    def _setup(self, db, events, tmp_path, *, results=None):
        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        sid = _insert_song(db, song_path)
        svc = _make_lyrics_service(results=results or {})
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib",), max_workers=2)
        return song_path, sid, svc, orch

    def test_first_failure_writes_next_retry_at(self, db, events, tmp_path):
        song_path, sid, _svc, orch = self._setup(
            db, events, tmp_path, results={"lrclib": {"state": "failed", "error_code": "not_found"}}
        )
        try:
            orch.kickoff(song_path)
        finally:
            orch.shutdown(wait=True)

        row = db.get_subtitle_job(sid, "lrclib")
        assert row["state"] == "failed"
        assert row["next_retry_at"] is not None
        assert row["attempt_count"] == 1

    def test_future_next_retry_at_skips_dispatch(self, db, events, tmp_path):
        # Pre-seed a failed row whose backoff hasn't elapsed.
        import datetime

        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        sid = _insert_song(db, song_path)
        future = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        ).isoformat(timespec="seconds")
        db.upsert_subtitle_job(
            sid,
            "lrclib",
            "failed",
            error_code="not_found",
            attempt_count=1,
            next_retry_at=future,
        )
        svc = _make_lyrics_service(results={})
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib",), max_workers=2)
        try:
            orch.kickoff(song_path)
        finally:
            orch.shutdown(wait=True)

        # Worker not invoked at all.
        svc.fetch_variant_sync.assert_not_called()
        # Row state preserved (not bumped to running/queued).
        row = db.get_subtitle_job(sid, "lrclib")
        assert row["state"] == "failed"
        assert row["attempt_count"] == 1

    def test_past_next_retry_at_re_queues(self, db, events, tmp_path):
        import datetime

        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        sid = _insert_song(db, song_path)
        past = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        ).isoformat(timespec="seconds")
        db.upsert_subtitle_job(
            sid,
            "lrclib",
            "failed",
            error_code="not_found",
            attempt_count=1,
            next_retry_at=past,
        )
        svc = _make_lyrics_service(
            results={"lrclib": {"state": "failed", "error_code": "not_found"}}
        )
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib",), max_workers=2)
        try:
            orch.kickoff(song_path)
        finally:
            orch.shutdown(wait=True)

        svc.fetch_variant_sync.assert_called_once()
        row = db.get_subtitle_job(sid, "lrclib")
        assert row["state"] == "failed"
        # Second failed attempt: backoff schedule index 1 (3 days).
        assert row["attempt_count"] == 2

    def test_force_bypasses_backoff(self, db, events, tmp_path):
        import datetime

        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        sid = _insert_song(db, song_path)
        future = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
        ).isoformat(timespec="seconds")
        db.upsert_subtitle_job(
            sid,
            "lrclib",
            "failed",
            error_code="not_found",
            attempt_count=1,
            next_retry_at=future,
        )
        svc = _make_lyrics_service(results={"lrclib": {"state": "success", "tier": "line"}})
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib",), max_workers=2)
        try:
            orch.kickoff(song_path, force=True)
        finally:
            orch.shutdown(wait=True)

        svc.fetch_variant_sync.assert_called_once()
        row = db.get_subtitle_job(sid, "lrclib")
        assert row["state"] == "success"

    def test_orchestrator_crash_writes_next_retry_at(self, db, events, tmp_path):
        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        sid = _insert_song(db, song_path)
        svc = MagicMock()
        svc.fetch_variant_sync.side_effect = RuntimeError("boom")
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib",), max_workers=2)
        try:
            orch.kickoff(song_path)
        finally:
            orch.shutdown(wait=True)

        row = db.get_subtitle_job(sid, "lrclib")
        assert row["state"] == "failed"
        assert row["error_code"] == "orchestrator_crash"
        # Crashes are transient (OOM, code bug) - retry on the same backoff
        # ladder rather than locking the source out forever.
        assert row["next_retry_at"] is not None

    def test_skipped_state_does_not_write_next_retry_at(self, db, events, tmp_path):
        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        sid = _insert_song(db, song_path)
        svc = _make_lyrics_service(
            results={"lrclib": {"state": "skipped", "error_code": "in_flight_dedup"}}
        )
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib",), max_workers=2)
        try:
            orch.kickoff(song_path)
        finally:
            orch.shutdown(wait=True)

        row = db.get_subtitle_job(sid, "lrclib")
        assert row["state"] == "skipped"
        # ``skipped`` is not a failure — the canonical path is still doing
        # the work; no backoff applies.
        assert row["next_retry_at"] is None

    def test_success_clears_next_retry_at_via_overwrite(self, db, events, tmp_path):
        song_path = str(tmp_path / "song.mp4")
        with open(song_path, "wb"):
            pass
        sid = _insert_song(db, song_path)
        # Seed a failed row with backoff.
        db.upsert_subtitle_job(
            sid,
            "lrclib",
            "failed",
            error_code="not_found",
            attempt_count=2,
            next_retry_at="2099-01-01T00:00:00+00:00",
        )
        svc = _make_lyrics_service(results={"lrclib": {"state": "success", "tier": "line"}})
        orch = SubtitleOrchestrator(svc, events, db, sources=("lrclib",), max_workers=2)
        try:
            orch.kickoff(song_path, force=True)
        finally:
            orch.shutdown(wait=True)

        row = db.get_subtitle_job(sid, "lrclib")
        assert row["state"] == "success"
        # The success transition didn't carry a next_retry_at, so the
        # COALESCE-free overwrite zeroes it. A future failed retry would
        # compute a fresh schedule based on the bumped attempt_count.
        assert row["next_retry_at"] is None
