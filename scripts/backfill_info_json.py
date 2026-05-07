"""One-shot backfill: re-fetch ``<stem>.info.json`` for songs that don't
have one on disk, then re-seed the row from it.

Together with the new "preserve info.json on download" policy in
``song_manager.register_download``, this script closes the metadata-bug
class where pre-existing rows (scanner imports, or post-``register_download``
rows that lost their info.json to the old consume-then-delete behaviour)
silently lacked any way to recover artist/title from YouTube.

For every song row whose ``<stem>.info.json`` is missing on disk:

  1. Extract the 11-char YouTube ID from the row's ``youtube_id`` column
     (or as a fallback from the filename suffix via
     ``metadata_parser.youtube_id_suffix``).
  2. Run ``yt-dlp --skip-download --write-info-json`` against
     ``https://www.youtube.com/watch?v=<id>``, dropping the file next to
     the song using the song's existing stem.
  3. Replay the post-download seeding ``register_download`` does after a
     fresh download:
       - ``upsert_artifacts(... discover_song_artifacts(path))`` so the
         info_json artifact is registered.
       - ``update_track_metadata_with_provenance(song_id, "youtube",
         _track_metadata_from_info_json(path))`` so the DB picks up
         track/artist/duration/language from the freshly-written file
         (the confidence ladder leaves any ``itunes`` / ``musicbrainz``
         / ``manual`` write untouched).
       - ``classify_and_persist`` so the language is re-classified with
         the new info.json signals.
  4. When the row's existing artist/title came from a low-confidence
     source (``scanner``, raw ``youtube`` from a previous poisoned write)
     and the new info.json's track/artist disagree, reset
     ``metadata_status='pending'`` and ``enrichment_attempts=0`` so the
     normal enricher re-evaluates from the new seed.

Usage::

    uv run python scripts/backfill_info_json.py
    uv run python scripts/backfill_info_json.py --dry-run
    uv run python scripts/backfill_info_json.py --limit 5
    uv run python scripts/backfill_info_json.py --song-id 96
    uv run python scripts/backfill_info_json.py --force
"""

from __future__ import annotations

import argparse
import functools
import os
import subprocess
import sys
import time
from pathlib import Path

# Force unbuffered stdout so per-song progress streams to tee/log files.
print = functools.partial(print, flush=True)  # noqa: A001

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Load .env so any yt-dlp proxy / cookie env vars are visible at import.
_env = REPO_ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

from pikaraoke.lib.karaoke_database import KaraokeDatabase  # noqa: E402
from pikaraoke.lib.lyrics_language_classifier import (  # noqa: E402
    classify_and_persist,
    read_info_json,
)
from pikaraoke.lib.metadata_parser import youtube_id_suffix  # noqa: E402
from pikaraoke.lib.song_manager import (  # noqa: E402
    _track_metadata_from_info_json,
    discover_song_artifacts,
)
from pikaraoke.lib.youtube_dl import _js_runtime_args, yt_dlp_cmd  # noqa: E402

INTER_FETCH_SLEEP_S = 2.0
YT_VIDEO_URL = "https://www.youtube.com/watch?v={id}"


def _extract_youtube_id(file_path: str, db_youtube_id: str | None) -> str | None:
    """Return the 11-char YouTube ID for a song row, or None."""
    if db_youtube_id and len(db_youtube_id) == 11:
        return db_youtube_id
    suffix = youtube_id_suffix(file_path)
    if not suffix:
        return None
    # ``youtube_id_suffix`` returns either '---<id>' (PiKaraoke style) or
    # ' [<id>]' (yt-dlp default). Trailing 11 chars are the ID either way.
    sanitized = suffix.rstrip("]")
    return sanitized[-11:] if len(sanitized) >= 11 else None


def _info_json_path(song_path: str) -> str:
    return f"{os.path.splitext(song_path)[0]}.info.json"


def _fetch_info_json(song_path: str, youtube_id: str) -> tuple[bool, str]:
    """Call yt-dlp to write ``<stem>.info.json`` next to the song.

    Returns ``(ok, message)``. ``message`` is the last few lines of yt-dlp
    stderr on failure (handy for spotting private/removed videos).
    """
    stem_no_ext = os.path.splitext(song_path)[0]
    output_template = f"{stem_no_ext}.%(ext)s"
    cmd = (
        yt_dlp_cmd
        + [
            "--skip-download",
            "--write-info-json",
            "--no-playlist",
            "-o",
            output_template,
            YT_VIDEO_URL.format(id=youtube_id),
        ]
        + _js_runtime_args()
    )
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "yt-dlp timed out after 60s"
    except (FileNotFoundError, PermissionError) as exc:
        return False, f"yt-dlp invocation failed: {exc}"

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        return False, " | ".join(tail) or f"yt-dlp exit {result.returncode}"

    if not os.path.exists(_info_json_path(song_path)):
        # Some failures (e.g. age-gate, private) still exit 0 with no
        # info.json written; treat the missing-file case as a failure.
        return False, "yt-dlp returned 0 but info.json was not written"

    return True, ""


def _reseed_row(db: KaraokeDatabase, song_id: int, song_path: str) -> dict:
    """Replay the post-download seeding for one song. Returns a stats dict."""
    stats = {"artist_changed": False, "title_changed": False}

    before_row = db.get_song_by_id(song_id)
    before_artist = (before_row["artist"] or "").strip() if before_row is not None else ""
    before_title = (before_row["title"] or "").strip() if before_row is not None else ""

    db.upsert_artifacts(song_id, discover_song_artifacts(song_path))
    meta = _track_metadata_from_info_json(song_path)
    if meta:
        db.update_track_metadata_with_provenance(song_id, "youtube", meta)

    try:
        yt_info = read_info_json(song_path)
        row = db.get_song_by_id(song_id)
        db_title = row["title"] if row is not None else None
        db_artist = row["artist"] if row is not None else None
        classify_and_persist(
            db,
            song_id,
            song_path=song_path,
            yt_info=yt_info,
            itunes_hit=None,
            mb_signals=None,
            db_title=db_title,
            db_artist=db_artist,
        )
    except Exception as exc:  # best-effort; don't abort the run
        print(f"  ! classifier failed for song_id={song_id}: {exc!r}")

    after_row = db.get_song_by_id(song_id)
    after_artist = (after_row["artist"] or "").strip() if after_row is not None else ""
    after_title = (after_row["title"] or "").strip() if after_row is not None else ""

    stats["artist_changed"] = before_artist != after_artist
    stats["title_changed"] = before_title != after_title

    if stats["artist_changed"] or stats["title_changed"]:
        db.reset_enrichment_state(song_id)

    return stats


def _scan_missing(db: KaraokeDatabase, force: bool) -> list[tuple[int, str, str | None]]:
    """Return [(song_id, file_path, youtube_id)] for rows we should re-fetch."""
    with db._connect() as conn:
        rows = conn.execute("SELECT id, file_path, youtube_id FROM songs ORDER BY id").fetchall()

    out: list[tuple[int, str, str | None]] = []
    for row in rows:
        info_path = _info_json_path(row["file_path"])
        if os.path.exists(info_path) and not force:
            continue
        out.append((row["id"], row["file_path"], row["youtube_id"]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List songs that would be fetched without calling yt-dlp.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process at most N songs.")
    parser.add_argument(
        "--song-id",
        type=int,
        default=None,
        help="Single-song debug mode: only process this song id.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch even when info.json already exists (recovers stale dumps).",
    )
    args = parser.parse_args()

    db = KaraokeDatabase()
    targets = _scan_missing(db, force=args.force)

    if args.song_id is not None:
        targets = [t for t in targets if t[0] == args.song_id]
        if not targets:
            # When the song isn't in the missing-list (info.json already exists)
            # but the user explicitly named it, honour --force-style behaviour.
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT id, file_path, youtube_id FROM songs WHERE id = ?",
                    (args.song_id,),
                ).fetchone()
            if row is None:
                print(f"No such song id: {args.song_id}")
                return 1
            targets = [(row["id"], row["file_path"], row["youtube_id"])]

    if args.limit is not None:
        targets = targets[: args.limit]

    print(f"info.json backfill: {len(targets)} song(s) to process")
    if args.dry_run:
        for sid, path, yid in targets:
            print(f"  [dry-run] song_id={sid} youtube_id={yid or '?'} path={path}")
        return 0

    fetched_ok = 0
    fetched_fail = 0
    artist_or_title_changed = 0
    no_youtube_id = 0

    for idx, (song_id, song_path, db_yid) in enumerate(targets, start=1):
        if not os.path.exists(song_path):
            print(f"  [{idx}/{len(targets)}] song_id={song_id}: media missing on disk, skipping")
            continue

        youtube_id = _extract_youtube_id(song_path, db_yid)
        if not youtube_id:
            print(f"  [{idx}/{len(targets)}] song_id={song_id}: no YouTube ID derivable, skipping")
            no_youtube_id += 1
            continue

        info_path = _info_json_path(song_path)
        if os.path.exists(info_path) and args.force:
            try:
                os.unlink(info_path)
            except OSError as exc:
                print(f"  ! song_id={song_id}: failed to remove stale info.json: {exc}")
                continue

        print(
            f"  [{idx}/{len(targets)}] song_id={song_id} youtube_id={youtube_id} "
            f"file={os.path.basename(song_path)}"
        )
        ok, err = _fetch_info_json(song_path, youtube_id)
        if not ok:
            fetched_fail += 1
            print(f"    FAIL: {err}")
            time.sleep(INTER_FETCH_SLEEP_S)
            continue

        fetched_ok += 1
        stats = _reseed_row(db, song_id, song_path)
        if stats["artist_changed"] or stats["title_changed"]:
            artist_or_title_changed += 1
            row = db.get_song_by_id(song_id)
            print(
                f"    OK reseed: artist={row['artist']!r} title={row['title']!r} "
                f"(reset enrichment_state)"
            )
        else:
            print("    OK reseed: artist/title unchanged")

        # Be polite to YouTube.
        time.sleep(INTER_FETCH_SLEEP_S)

    print()
    print("Summary:")
    print(f"  scanned:                    {len(targets)}")
    print(f"  fetched OK:                 {fetched_ok}")
    print(f"  fetched FAIL:               {fetched_fail}")
    print(f"  no YouTube ID:              {no_youtube_id}")
    print(f"  artist/title changed:       {artist_or_title_changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
