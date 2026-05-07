"""Subtitle-source picker route.

Implements the operator-facing source switch behind the now-playing tools
panel. ``POST /subtitle_source`` pins one source per song, kicks off an
on-demand fetch when the picked source is not yet cached, and broadcasts
``subtitle_off`` / ``subtitle_on`` so the splash can hide/show the
SubtitlesOctopus canvas in real time.
"""

import json
import logging
import os

from flask import request
from flask_smorest import Blueprint

from pikaraoke.lib.current_app import broadcast_event, get_karaoke_instance, is_admin
from pikaraoke.lib.karaoke_database import (
    SUBTITLE_SOURCE_OFF,
    SUBTITLE_SOURCE_USER,
    VALID_SUBTITLE_SOURCES,
    VARIANT_FILE_SOURCES,
)
from pikaraoke.lib.lyrics import variant_ass_path

subtitle_bp = Blueprint("subtitle", __name__)

logger = logging.getLogger(__name__)


def _json_error(message: str, status: int) -> tuple[str, int]:
    return json.dumps({"status": "error", "error": message}), status


@subtitle_bp.route("/subtitle_source", methods=["POST"])
def set_subtitle_source():
    """Pin (or clear) the subtitle source for one song.

    Body (JSON or form): ``{song_id: int, source: str}``. ``source`` must
    be a member of ``VALID_SUBTITLE_SOURCES`` (CG2 — whitelist enforced
    BEFORE any DB or filesystem work, blocking path traversal via the
    variant filename).

    Responses:
      * ``{"status": "ok"}`` — override persisted, picker should now
        show this source as ``ready``.
      * ``{"status": "pending"}`` — variant not yet cached; an on-demand
        fetch was dispatched. The picker shows ``downloading`` until the
        next ``/now_playing`` poll picks up the cached file.
      * ``400`` — unknown source, missing fields, or N/A on this host.
      * ``403`` — not admin.
      * ``404`` — song id not found.
    """
    if not is_admin():
        return _json_error("Unauthorized", 403)

    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    raw_song_id = payload.get("song_id")
    source = payload.get("source")

    if source is None or not isinstance(source, str):
        return _json_error("source is required", 400)
    # CG2: whitelist BEFORE the DB lookup so a hostile client can't probe
    # row existence with a malformed source value.
    if source not in VALID_SUBTITLE_SOURCES:
        return _json_error(f"unknown source: {source}", 400)

    try:
        song_id = int(raw_song_id)
    except (TypeError, ValueError):
        return _json_error("song_id must be an integer", 400)

    k = get_karaoke_instance()
    if k.db is None:
        return _json_error("Database unavailable", 500)

    try:
        row = k.db.get_song_by_id(song_id)
    except Exception:
        logger.exception("set_subtitle_source: get_song_by_id failed for %s", song_id)
        return _json_error("DB error", 500)
    if row is None:
        return _json_error(f"song {song_id} not found", 404)

    file_path = row["file_path"]
    previous = k.db.get_subtitle_source_override(song_id)
    is_off = source == SUBTITLE_SOURCE_OFF

    # ``user`` is special: there is no <stem>.user.ass — the canonical
    # <stem>.ass IS the user file when lyrics_provenance == 'user'. Block
    # pinning ``user`` when no user-authored .ass is present.
    if source == SUBTITLE_SOURCE_USER:
        try:
            provenance = row["lyrics_provenance"]
        except (KeyError, IndexError):
            provenance = None
        if provenance != "user":
            return _json_error("user-authored .ass not present for this song", 400)

    # Variant sources: short-circuit the disk check. If the file exists
    # already, just pin the override and broadcast — no fetch needed.
    variant_ready = False
    if source in VARIANT_FILE_SOURCES:
        try:
            variant_ready = os.path.exists(variant_ass_path(file_path, source))
        except OSError:
            variant_ready = False

    deferred = source in VARIANT_FILE_SOURCES and not variant_ready

    if deferred:
        # User tapped a not-yet-fetched variant. Schedule the fetch but
        # leave the override + displayed subs alone — the splash keeps
        # showing the previous source. The pick is recorded as pending
        # and committed by ``Karaoke._on_subtitle_variant_landed`` once
        # the variant file lands. Re-clicking another unready source
        # overwrites the pending pick: only the latest intent wins.
        k.set_pending_subtitle_pick(file_path, source)
        # Dispatch (idempotent: dedups on the (song, source) in-flight
        # slot) so the worker actually starts. Return value is immaterial
        # to the caller — pending pick is set either way.
        k.lyrics_service.dispatch_variant_fetch(file_path, source)
        # Refresh the picker so it sees ``subtitle_source_pending`` and
        # the row's ``downloading`` status. No URL bump — splash must
        # not refetch /subtitle while the previous source is still
        # rendering.
        try:
            k.update_now_playing_socket()
        except Exception:
            logger.exception("set_subtitle_source: now_playing emit failed (deferred)")
        return json.dumps({"status": "pending"}), 202

    # Immediate path: ``off``, ``user``, or a variant whose file is
    # already on disk. Write the override, broadcast on/off transitions,
    # and bump the subtitle URL via ``lyrics_upgraded`` so the splash
    # hot-swaps to the chosen source.
    try:
        k.db.set_subtitle_source_override(song_id, source)
    except Exception:
        logger.exception("set_subtitle_source: write failed for song_id=%s", song_id)
        return _json_error("DB error", 500)
    # Any prior deferred pick is superseded by this immediate switch.
    k.clear_pending_subtitle_pick(file_path)

    # Off → on transition needs the canvas un-hidden BEFORE the new URL
    # arrives so the still-running SubtitlesOctopus instance is visible
    # again on the next ``lyrics_upgraded`` push.
    if previous == SUBTITLE_SOURCE_OFF and not is_off:
        broadcast_event("subtitle_on")
    if is_off:
        broadcast_event("subtitle_off")

    # Force the splash to re-fetch /subtitle/<id> with the new pin in place.
    # ``lyrics_upgraded`` is the established cache-bust: the karaoke
    # handler bumps ``?v=`` on ``now_playing_subtitle_url`` and re-emits
    # ``now_playing``. Splash sees a new URL → tear down + reinit Octopus
    # → /subtitle/<id> serves the variant.
    if not is_off:
        try:
            k.events.emit("lyrics_upgraded", file_path)
        except Exception:
            logger.exception("set_subtitle_source: lyrics_upgraded emit failed")
    else:
        # ``off`` doesn't need a URL bump (the splash skips Octopus init
        # via ``subtitle_disabled``); just refresh the picker payload.
        try:
            k.update_now_playing_socket()
        except Exception:
            logger.exception("set_subtitle_source: now_playing emit failed")

    return json.dumps({"status": "ok"}), 200


@subtitle_bp.route("/subtitle_offset", methods=["POST"])
def set_subtitle_offset():
    """Store a per-(song, source) subtitle timing offset.

    Body (JSON or form): ``{song_id: int, source: str, offset: float}``.
    The value is clamped to ``Karaoke.SUBTITLE_OFFSET_MIN..MAX`` and rounded
    to two decimals. Memoisation lives in process memory only — restarting
    the server resets every song's offset to 0.00s, matching the "default
    for a fresh song" requirement.
    """
    if not is_admin():
        return _json_error("Unauthorized", 403)

    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    raw_song_id = payload.get("song_id")
    source = payload.get("source")
    raw_offset = payload.get("offset")

    if source is None or not isinstance(source, str) or not source:
        return _json_error("source is required", 400)
    try:
        song_id = int(raw_song_id)
    except (TypeError, ValueError):
        return _json_error("song_id must be an integer", 400)
    try:
        offset = float(raw_offset)
    except (TypeError, ValueError):
        return _json_error("offset must be a number", 400)

    k = get_karaoke_instance()
    stored = k.set_subtitle_offset(song_id, source, offset)

    # Re-emit now_playing so every connected splash + control panel
    # picks up the new value on the next render. Splash already maps
    # ``np.subtitle_offset`` to ``octopusInstance.timeOffset`` live.
    try:
        k.update_now_playing_socket()
    except Exception:
        logger.exception("set_subtitle_offset: now_playing emit failed")

    return json.dumps({"status": "ok", "offset": stored}), 200
