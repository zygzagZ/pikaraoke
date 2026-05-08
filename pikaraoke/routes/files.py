"""File management routes for browsing, editing, and deleting songs."""

from __future__ import annotations

import datetime
import logging
import os
import re
import unicodedata
from urllib.parse import unquote

import flask_babel
from flask import flash, redirect, render_template, request, url_for
from flask_paginate import Pagination, get_page_parameter
from flask_smorest import Blueprint
from marshmallow import Schema, fields

from pikaraoke.lib.current_app import get_karaoke_instance, get_site_name, is_admin
from pikaraoke.lib.metadata_parser import youtube_id_suffix

_YT_ID_FROM_SUFFIX_RE = re.compile(r"(?:---|\[)([A-Za-z0-9_-]{11})\]?$")


def _youtube_id_from_path(path: str) -> str:
    """Extract the bare 11-char YouTube id from a song path.

    ``youtube_id_suffix`` returns the *suffix* (``---ID`` or `` [ID]``);
    the per-song event log keys by the bare id, so strip the framing.
    """
    suffix = youtube_id_suffix(path).strip()
    match = _YT_ID_FROM_SUFFIX_RE.search(suffix)
    return match.group(1) if match else ""


_ = flask_babel.gettext


files_bp = Blueprint("files", __name__)


class SongReferrerQuery(Schema):
    song = fields.String(required=True, metadata={"description": "Path to the song file"})
    referrer = fields.String(metadata={"description": "URL to redirect back to"})


class EditFileForm(Schema):
    new_file_name = fields.String(
        required=True, metadata={"description": "New filename (without extension)"}
    )
    old_file_name = fields.String(
        required=True, metadata={"description": "Current full path of the song file"}
    )
    referrer = fields.String(metadata={"description": "URL to redirect back to after editing"})
    language = fields.String(
        load_default="",
        metadata={
            "description": (
                "Optional ISO-639-1 lower-case language code (e.g. 'pl'). Empty keeps "
                "the existing value; non-empty writes with manual provenance."
            )
        },
    )


@files_bp.route("/browse", methods=["GET"])
def browse():
    """Browse available songs page."""
    k = get_karaoke_instance()
    site_name = get_site_name()
    search = False
    q = request.args.get("q")
    if q:
        search = True
    page = int(request.args.get("page", 1))

    available_songs = k.song_manager.songs

    letter = request.args.get("letter")

    if letter:
        result = []
        if letter == "numeric":
            for song in available_songs:
                f = k.song_manager.display_name_from_path(song)[0]
                if f.isnumeric():
                    result.append(song)
        else:
            for song in available_songs:
                f = k.song_manager.display_name_from_path(song).lower()
                # Normalize accented characters so e.g. "Édith" matches "e"
                normalized = unicodedata.normalize("NFD", f)
                base_char = normalized[0] if normalized else ""
                if base_char == letter.lower():
                    result.append(song)
        available_songs = result

    def _split(song):
        name = k.song_manager.display_name_from_path(song)
        parts = name.split(" - ", 1)
        if len(parts) == 2:
            return parts[0].strip().lower(), parts[1].strip().lower(), name.lower()
        return "", name.strip().lower(), name.lower()

    sort = request.args.get("sort", "title")
    if sort == "artist":
        songs = sorted(available_songs, key=lambda x: (_split(x)[0], _split(x)[1]))
        sort_order = "Artist"
    elif sort == "date":
        songs = sorted(available_songs, key=lambda x: os.path.getmtime(x), reverse=True)
        sort_order = "Date"
    else:
        songs = sorted(available_songs, key=lambda x: (_split(x)[1], _split(x)[0]))
        sort_order = "Title"

    results_per_page = k.browse_results_per_page

    args = request.args.copy()
    args.pop("_", None)

    current_url = url_for("files.browse", **args.to_dict())

    page_param = get_page_parameter()
    args[page_param] = "{0}"

    args_dict = args.to_dict()
    pagination_href = unquote(url_for("files.browse", **args_dict))  # type: ignore

    pagination = Pagination(
        css_framework="bulma",
        page=page,
        total=len(songs),
        search=search,
        record_name="songs",
        per_page=results_per_page,
        display_msg="Showing <b>{start} - {end}</b> of <b>{total}</b> {record_name}",
        href=pagination_href,
    )
    start_index = (page - 1) * results_per_page

    # All unique artists in the library — used for autocomplete in inline edit.
    def _artist_orig(song):
        name = k.song_manager.display_name_from_path(song)
        parts = name.split(" - ", 1)
        return parts[0].strip() if len(parts) == 2 else ""

    artists = sorted(
        {_artist_orig(s) for s in k.song_manager.songs if _artist_orig(s)},
        key=str.lower,
    )

    return render_template(
        "files.html",
        pagination=pagination,
        sort_order=sort_order,
        site_title=site_name,
        letter=letter,
        # MSG: Title of the files page.
        title=_("Browse"),
        songs=songs[start_index : start_index + results_per_page],
        admin=is_admin(),
        current_url=current_url,
        artists=artists,
        lyrics_sources=k.db.get_lyrics_sources(),
    )


@files_bp.route("/files/delete", methods=["GET"])
@files_bp.arguments(SongReferrerQuery, location="query")
def delete_file(query):
    """Delete a song file."""
    k = get_karaoke_instance()
    song_path = query["song"]
    referrer = query.get("referrer") or url_for("files.browse")
    if not is_admin():
        flash(_("You don't have permission to delete songs"), "is-danger")
        return redirect(referrer)
    if k.queue_manager.is_song_in_queue(song_path):
        flash(
            # MSG: Message shown after trying to delete a song that is in the queue.
            _("Error: Can't delete this song because it is in the current queue")
            + ": "
            + song_path,
            "is-danger",
        )
    else:
        k.song_manager.delete(song_path)
        # MSG: Message shown after deleting a song. Followed by the song path
        flash(
            _("Song deleted: %s") % k.song_manager.display_name_from_path(song_path),
            "is-warning",
        )
    return redirect(referrer)


@files_bp.route("/files/edit", methods=["GET"])
@files_bp.arguments(SongReferrerQuery, location="query")
def edit_file(query):
    """Show the song rename page."""
    k = get_karaoke_instance()
    site_name = get_site_name()
    song_path = query["song"]
    referrer = query.get("referrer") or url_for("files.browse")
    if not is_admin():
        flash(_("You don't have permission to edit songs"), "is-danger")
        return redirect(referrer)
    if k.queue_manager.is_song_in_queue(song_path):
        # MSG: Message shown after trying to edit a song that is in the queue.
        flash(
            _("Error: Can't edit this song because it is in the current queue: ") + song_path,
            "is-danger",
        )
        return redirect(referrer)
    raw_stem = k.song_manager.filename_from_path(song_path, tidy=False)

    def _artist(song):
        name = k.song_manager.display_name_from_path(song)
        parts = name.split(" - ", 1)
        return parts[0].strip() if len(parts) == 2 else ""

    artists = sorted(
        {_artist(s) for s in k.song_manager.songs if _artist(s)},
        key=str.lower,
    )

    basename = os.path.basename(song_path)
    youtube_id = _youtube_id_from_path(song_path)
    events = [
        _decorate_event(e) for e in k.get_song_events_for(song=basename, youtube_id=youtube_id)
    ]
    # Used by the live subtitle-status chip row; falls back to ``None`` for
    # files not yet ingested into ``songs`` (the chip row hides itself).
    song_id = k.db.get_song_id_by_path(song_path) if k.db else None

    # Surfaces the metadata-enrichment state to the template so we can show
    # an "awaiting language" / "language mismatch" chip. Both states are
    # transient (a later whisper probe re-runs the enricher), but they tell
    # the user why iTunes-derived fields look empty or odd; for an enriched
    # row the chip stays hidden.
    metadata_status = None
    metadata_language = None
    db_artist = ""
    db_title = ""
    if song_id is not None and k.db is not None:
        row = k.db.get_song_by_id(song_id)
        if row is not None:
            metadata_status = row["metadata_status"]
            metadata_language = row["language"]
            db_artist = (row["artist"] or "").strip()
            db_title = (row["title"] or "").strip()

    # Prefer DB values (canonical after manual edit / iTunes enrichment) and
    # fall back to filename split when the row isn't ingested yet or fields
    # are empty — keeps scanner-discovered songs editable before enrichment
    # has a chance to populate the columns.
    parts = raw_stem.split(" - ", 1)
    if len(parts) == 2:
        fallback_artist, fallback_title = parts[0].strip(), parts[1].strip()
    else:
        fallback_artist, fallback_title = "", raw_stem
    initial_artist = db_artist or fallback_artist
    initial_title = db_title or fallback_title

    return render_template(
        "edit.html",
        site_title=site_name,
        title="Song File Edit",
        song=song_path,
        raw_stem=raw_stem,
        referrer=referrer,
        artists=artists,
        song_events=events,
        song_basename=basename,
        song_youtube_id=youtube_id,
        song_id=song_id,
        metadata_status=metadata_status,
        metadata_language=metadata_language,
        initial_artist=initial_artist,
        initial_title=initial_title,
    )


def _decorate_event(event: dict) -> dict:
    """Add a human-readable ``time_str`` (local HH:MM:SS) to an event row."""
    out = dict(event)
    ts = event.get("timestamp")
    if isinstance(ts, (int, float)):
        try:
            out["time_str"] = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        except (OSError, ValueError):
            out["time_str"] = ""
    else:
        out["time_str"] = ""
    return out


@files_bp.route("/files/edit", methods=["POST"])
@files_bp.arguments(EditFileForm, location="form")
def rename_file(form):
    """Process a song rename."""
    k = get_karaoke_instance()
    referrer = form.get("referrer") or url_for("files.browse")
    new_name = form["new_file_name"]
    old_name = form["old_file_name"]
    language = (form.get("language") or "").strip().lower()
    if not is_admin():
        flash(_("You don't have permission to edit songs"), "is-danger")
    yt_suffix = youtube_id_suffix(old_name)
    new_name_full = new_name + yt_suffix
    if k.queue_manager.is_song_in_queue(old_name):
        # check one more time just in case someone added it during editing
        # MSG: Message shown after trying to edit a song that is in the queue.
        flash(
            _("Error: Can't edit this song because it is in the current queue: ") + old_name,
            "is-danger",
        )
    else:
        file_extension = os.path.splitext(old_name)[1]
        if os.path.isfile(
            os.path.join(k.song_manager.download_path, new_name_full + file_extension)
        ):
            flash(
                # MSG: Message shown after trying to rename a file to a name that already exists.
                _("Error renaming file: '%s' to '%s', Filename already exists")
                % (old_name, new_name_full + file_extension),
                "is-danger",
            )
        else:
            try:
                new_path = k.song_manager.rename(old_name, new_name_full)
            except OSError as e:
                logging.error(f"Error renaming file: {e}")
                flash(
                    _("Error renaming file: '%s' to '%s', %s") % (old_name, new_name_full, e),
                    "is-danger",
                )
            else:
                _apply_metadata_edit(k, new_path, new_name, language)
                flash(
                    # MSG: Message shown after renaming a file.
                    _("Renamed file: %s to %s") % (old_name, new_name_full),
                    "is-warning",
                )
    return redirect(referrer)


def _apply_metadata_edit(k, new_path: str, new_name: str, language: str) -> None:
    """Persist artist/title/language to the songs row and re-fetch lyrics.

    The lyrics pipeline reads ``songs.artist`` / ``songs.title`` /
    ``songs.language`` directly — the on-disk filename is not consulted.
    Without writing the DB the user's edit changes the display name only;
    LRCLib, Genius and Spotify keep getting queried with the original
    metadata and the cached ``.ass`` files are never refreshed.

    Note: ``Karaoke._on_track_metadata_change`` (DB listener) also fires
    when artist/title actually change value, so the invalidation calls
    below run twice in the artist/title-edit case. They are idempotent —
    deleting an already-deleted .ass is a no-op, ``kickoff(force=True)``
    is a no-op once the orchestrator has the latest pick — so the
    redundancy is cheap. Keeping the explicit calls here means the
    manual path also covers ``language``-only edits, which the listener
    skips on purpose (to avoid a loop with the lyrics pipeline's own
    language-detection writes).
    """
    if k.db is None:
        return
    song_id = k.db.get_song_id_by_path(new_path)
    if song_id is None:
        return

    artist, sep, title = new_name.partition(" - ")
    artist, title = artist.strip(), title.strip()
    fields_to_write: dict[str, str] = {}
    if sep and title:
        fields_to_write["artist"] = artist
        fields_to_write["title"] = title
    elif artist:
        # User entered only a title (no " - " separator); keep the existing
        # artist rather than blanking it.
        fields_to_write["title"] = artist
    if language:
        fields_to_write["language"] = language
    if fields_to_write:
        k.db.update_track_metadata_with_provenance(song_id, "manual", fields_to_write)

    k.lyrics_service.invalidate_for_metadata_change(new_path)
    k._dispatch_lyrics_fetch_async(new_path)
    k.subtitle_orchestrator.kickoff(new_path, force=True)
