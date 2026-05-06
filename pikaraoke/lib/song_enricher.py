"""Best-effort music metadata enrichment for downloaded songs.

Reactive model: the enricher reruns every time a new language signal
flips ``songs.language``. iTunes is consulted only when ``songs.language``
is non-empty; the chosen hit must agree with that language (otherwise
the row stays "language_mismatch" with no textual iTunes fields written).
Idempotency is gated on ``songs.language_at_enrich``: when it equals
``songs.language`` and ``metadata_status='enriched'``, the run is a no-op.

Pipeline per song:
  1. Idempotency check. If ``metadata_status='enriched'`` and
     ``language_at_enrich == songs.language``, return early.
  2. Read ``expected_lang = songs.language``. Empty → stamp
     ``awaiting_language`` (no iTunes call) and return — a later
     language-write site will dispatch us again.
  3. Pull iTunes top-5 for the query (LRU-cached). Empty → ``not_found``.
  4. Pick the first hit whose derived language (langdetect over
     collection+track+artist, with country fallback) matches
     ``expected_lang``. None → ``language_mismatch``, no textual fields
     written.
  5. Apply the chosen hit's fields via ``update_track_metadata_with_provenance``,
     run the existing variant guard, fetch MusicBrainz IDs, download
     cover art, stamp ``language_at_enrich``.

All network calls are best-effort: failures are logged and swallowed so
enrichment cannot crash playback. The caller typically spawns this in a
background thread so the 3-6s of iTunes + MusicBrainz latency doesn't block
the download pipeline.
"""

import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import requests

from pikaraoke.lib.karaoke_database import KaraokeDatabase
from pikaraoke.lib.lyrics import _VARIANT_RE
from pikaraoke.lib.lyrics_language_classifier import COUNTRY_TO_LANG, signal_itunes_text
from pikaraoke.lib.music_metadata import (
    fetch_musicbrainz_ids,
    normalize_title,
    project_full_hit,
    search_itunes_full,
)

logger = logging.getLogger(__name__)

COVER_ART_ROLE = "cover_art"
_COVER_DOWNLOAD_TIMEOUT_S = 5.0

_YT_ID_SUFFIX_RE = re.compile(r"(?:---[A-Za-z0-9_-]{11}|\s*\[[A-Za-z0-9_-]{11}\])$")

# Module-level hook for forwarding enrichment milestones into the per-song
# event timeline. karaoke.py registers a callback that re-emits each payload
# as a ``song_event`` on the EventSystem. The enricher itself is a module of
# free functions with no events handle of its own — same pattern as
# ``demucs_processor.set_warning_hook``.
_event_hook: Callable[[dict[str, Any]], None] | None = None


def set_event_hook(hook: Callable[[dict[str, Any]], None] | None) -> None:
    """Register (or clear) the module-level enrichment-event hook."""
    global _event_hook
    _event_hook = hook


def _notify(payload: dict[str, Any]) -> None:
    hook = _event_hook
    if hook is None:
        return
    try:
        hook(payload)
    except Exception:
        logger.exception("enrichment event hook raised")


def _query_from_song(row, song_path: str) -> str:
    """Build an iTunes/MusicBrainz query: "<artist> - <title>".

    Prefers the ``songs`` row seeded by register_download / scanner; falls
    back to the filename stem (with the YouTube-id suffix stripped) when
    the row is missing artist or title. Empty string means "skip
    enrichment" — there's nothing to query with.
    """
    if row is not None:
        artist = (row["artist"] or "").strip()
        title = (row["title"] or "").strip()
        if artist and title:
            return f"{artist} - {title}"
    stem = os.path.splitext(os.path.basename(song_path))[0]
    return _YT_ID_SUFFIX_RE.sub("", stem).strip()


def _download_cover(url: str, dest: str) -> bool:
    """Download ``url`` to ``dest`` atomically. Returns True on success."""
    try:
        r = requests.get(url, timeout=_COVER_DOWNLOAD_TIMEOUT_S, stream=True)
    except requests.RequestException as e:
        logger.warning("cover download failed for %s: %s", url, e)
        return False
    if r.status_code != 200:
        logger.warning("cover HTTP %d for %s", r.status_code, url)
        return False
    tmp = dest + ".part"
    try:
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=32768):
                if chunk:
                    f.write(chunk)
        os.replace(tmp, dest)
    except OSError as e:
        logger.warning("cover write failed for %s: %s", dest, e)
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False
    return True


SOURCE_ITUNES = "itunes"
SOURCE_MUSICBRAINZ = "musicbrainz"


def _itunes_adds_variant(query: str, itunes: dict) -> bool:
    """True when iTunes' canonical track name adds a mix/version marker the
    original query did not have.

    Guards against iTunes' only hit being the instrumental/karaoke/live cut
    (common on small catalogues): overriding ``title`` with that suffix
    poisons every downstream LRCLib/Genius query, which index by the
    canonical song. Returns False when either side lacks a track name.
    """
    itunes_track = (itunes.get("track") or "").strip()
    if not itunes_track or not query:
        return False
    return bool(_VARIANT_RE.search(itunes_track)) and not _VARIANT_RE.search(query)


def enrich_song(db: KaraokeDatabase, song_id: int, song_path: str) -> None:
    """Run iTunes + MusicBrainz enrichment for a single song.

    Always updates ``metadata_status``, ``enrichment_attempts``, and
    ``last_enrichment_attempt`` so failed attempts are visible in the DB
    for later retry. Unexpected exceptions are caught here and stamped
    as ``error`` so a crashing thread can't silently leave a row stuck
    on ``pending`` with ``enrichment_attempts = 0``.

    Provenance (US-28): each metadata field is written via
    ``update_track_metadata_with_provenance`` with the originating source
    tag. The DB applies a confidence ladder (musicbrainz > itunes >
    youtube > scanner) so MusicBrainz-supplied artist/title overrides
    iTunes if both arrive, but neither overrides a ``manual`` write.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        _enrich_song_inner(db, song_id, song_path, now)
    except Exception as exc:
        logger.exception("enrich_song crashed for song_id=%d path=%s", song_id, song_path)
        _notify(
            {
                "phase": "enrichment",
                "severity": "error",
                "message": "Metadata enrichment failed",
                "detail": repr(exc),
                "song": os.path.basename(song_path),
            }
        )
        try:
            db.stamp_enrichment_attempt(song_id, "error", now)
        except Exception:
            logger.exception("stamping enrichment error failed for song_id=%d", song_id)


def _hit_language(raw_hit: dict) -> str | None:
    """Derive a primary subtag for one iTunes hit.

    Runs ``signal_itunes_text`` (langdetect over collection+track+artist
    + dub markers) first; falls back to ``COUNTRY_TO_LANG`` keyed on the
    storefront when the text is too short for langdetect. Returns None
    when neither signal fires — the caller treats that as "language
    unknown" and won't reject on lack of evidence.
    """
    sig = signal_itunes_text(raw_hit)
    if sig is not None:
        return sig.language
    country = (raw_hit.get("country") or "").upper()
    return COUNTRY_TO_LANG.get(country)


def _pick_hit_for_language(candidates: list[dict], expected_lang: str) -> tuple[dict | None, str]:
    """Return ``(chosen_raw_hit, reason)``.

    ``reason`` is one of ``"lang_match"`` (a candidate's derived language
    matched ``expected_lang``) or ``"no_match"`` (every candidate either
    disagreed or had no derivable language). Hits with no derivable
    language are NOT accepted — they're indistinguishable from a
    "wrong-language hit whose text is too short to detect", and accepting
    them would leak the same Pedalini-style false-match the language
    guard exists to prevent. Callers requiring a permissive accept-on-
    unknown policy should fall back outside this function.
    """
    for raw_hit in candidates:
        if _hit_language(raw_hit) == expected_lang:
            return raw_hit, "lang_match"
    return None, "no_match"


def _enrich_song_inner(db: KaraokeDatabase, song_id: int, song_path: str, now: str) -> None:
    row = db.get_song_by_id(song_id)
    if row is None:
        # Row deleted between dispatch and execution (rename/delete race).
        # Nothing to stamp -- the row is gone.
        logger.warning("enrich_song: song_id=%d gone before enrichment ran", song_id)
        return

    basename = os.path.basename(song_path)
    youtube_id = (row["youtube_id"] or "") if row is not None else ""

    def _emit(message: str, *, severity: str = "info", detail: str = "") -> None:
        _notify(
            {
                "phase": "enrichment",
                "severity": severity,
                "message": message,
                "detail": detail,
                "song": basename,
                "youtube_id": youtube_id,
            }
        )

    expected_lang = (row["language"] or "").strip() or None

    # Idempotency: if we already enriched against the current language,
    # the iTunes pick can't change. Re-running would be a no-op write
    # but still costs an HTTP roundtrip on cold cache, so short-circuit
    # here. ``manual``-tagged fields are preserved by the provenance
    # ladder regardless.
    if row["metadata_status"] == "enriched" and (row["language_at_enrich"] or "") == (
        expected_lang or ""
    ):
        return

    if expected_lang is None:
        db.stamp_enrichment_attempt(song_id, "awaiting_language", now)
        _emit(
            "Metadata enrichment deferred",
            detail="awaiting_language: no consensus signal yet",
        )
        return

    query = _query_from_song(row, song_path)
    if not query:
        db.stamp_enrichment_attempt(song_id, "skipped", now, language_at_enrich=expected_lang)
        _emit("Metadata enrichment skipped", detail="no artist/title to query")
        return

    _emit("Metadata enrichment starting", detail=f"{query} (lang={expected_lang})")

    candidates: list[dict] = []
    try:
        candidates = search_itunes_full(normalize_title(query), limit=5)
    except Exception:
        logger.exception("iTunes lookup crashed for %r", query)

    if not candidates:
        db.stamp_enrichment_attempt(song_id, "not_found", now, language_at_enrich=expected_lang)
        _emit("Metadata enrichment finished", detail="iTunes: no match")
        return

    chosen_raw, reason = _pick_hit_for_language(candidates, expected_lang)
    if chosen_raw is None:
        db.stamp_enrichment_attempt(
            song_id, "language_mismatch", now, language_at_enrich=expected_lang
        )
        _emit(
            "Metadata enrichment finished",
            severity="warning",
            detail=(
                f"language_mismatch: audio={expected_lang}, "
                f"top-{len(candidates)} iTunes hits had no match"
            ),
        )
        return

    itunes = project_full_hit(chosen_raw)

    if _itunes_adds_variant(query, itunes):
        logger.info(
            "iTunes canonical track %r adds a variant marker not in %r; "
            "dropping title/artist override",
            itunes.get("track"),
            query,
        )
        itunes = {**itunes, "track": None, "artist": None}

    applied = db.update_track_metadata_with_provenance(
        song_id,
        SOURCE_ITUNES,
        {
            "itunes_id": itunes.get("itunes_id"),
            "artist": itunes.get("artist"),
            "title": itunes.get("track"),
            "album": itunes.get("album"),
            "track_number": itunes.get("track_number"),
            "release_date": itunes.get("release_date"),
            "genre": itunes.get("genre"),
        },
    )

    # MusicBrainz is optional; skip when iTunes gave us no artist/track.
    mb_artist = itunes.get("artist")
    mb_track = itunes.get("track")
    if mb_artist and mb_track:
        try:
            mb = fetch_musicbrainz_ids(mb_artist, mb_track)
        except Exception:
            logger.exception("MusicBrainz lookup crashed for %r / %r", mb_artist, mb_track)
            mb = None
        if mb:
            applied.update(
                db.update_track_metadata_with_provenance(
                    song_id,
                    SOURCE_MUSICBRAINZ,
                    {
                        "musicbrainz_recording_id": mb.get("musicbrainz_recording_id"),
                        "isrc": mb.get("isrc"),
                    },
                )
            )

    cover_url = itunes.get("cover_art_url")
    if cover_url:
        cover_path = f"{os.path.splitext(song_path)[0]}.cover.jpg"
        if not os.path.exists(cover_path) and _download_cover(cover_url, cover_path):
            db.upsert_artifacts(song_id, [{"role": COVER_ART_ROLE, "path": cover_path}])

    final_status = "enriched" if applied else "no_new_fields"
    db.stamp_enrichment_attempt(song_id, final_status, now, language_at_enrich=expected_lang)
    _emit(
        "Metadata enrichment finished",
        detail=f"{final_status}: " + ", ".join(sorted(applied)) if applied else final_status,
    )
