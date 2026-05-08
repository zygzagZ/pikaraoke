"""Production-path lyrics-coverage probe.

For every row in ``songs`` runs each lyrics source through
``LyricsService._try_with_candidates`` — the exact path the runtime
takes — so the report mirrors what an end user actually gets:

  1. LRCLib synced LRC.
  2. Spotify lyrics payload (LINE_SYNCED / SYLLABLE_SYNCED / UNSYNCED).
  3. Genius plain text (the wav2vec2 ``genius-sync`` aligner consumes it).

The candidate ladder rescues rows where the DB tag drifts from the
canonical credit (split-artist credits, regex_tidy, iTunes,
MusicBrainz) and — crucially — also re-seeds from the filename when
the DB tag is blank, so a metadata-empty row no longer falls off the
edge of the report.

Writes a per-song matrix to ``tasks/lyrics_coverage.md`` that surfaces
the *winning* candidate, so a reviewer can see at a glance whether a
hit came from the DB tag, a collaborator split, or a filename rescue.

Usage:
    uv run python scripts/verify_lyrics_coverage.py
    uv run python scripts/verify_lyrics_coverage.py --no-spotify
    uv run python scripts/verify_lyrics_coverage.py --limit 5
    uv run python scripts/verify_lyrics_coverage.py --song-id 98
"""

import argparse
import functools
import os
import sys
import time
from pathlib import Path

# Force unbuffered stdout so per-song progress streams to tee/log files.
print = functools.partial(print, flush=True)  # noqa: A001

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Load .env so GENIUS_ACCESS_TOKEN is visible to lyrics.py at import time.
_env = REPO_ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

from pikaraoke.lib import lyrics as lyrics_mod  # noqa: E402
from pikaraoke.lib.events import EventSystem  # noqa: E402
from pikaraoke.lib.karaoke_database import KaraokeDatabase  # noqa: E402
from pikaraoke.lib.lyrics import LyricsService  # noqa: E402
from pikaraoke.lib.preference_manager import PreferenceManager  # noqa: E402

OUT_PATH = REPO_ROOT / "tasks" / "lyrics_coverage.md"


def _short(text: str | None, n: int = 60) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "..."


def _winner_label(winner: dict | None, info: dict) -> str:
    """Compact tag for which candidate won. ``db`` if it matches the DB
    tag, otherwise ``alt: <artist> / <track>`` so filename / split-artist
    rescues are visible in the report."""
    if not winner:
        return ""
    db_a, db_t = (info.get("artist") or "").strip(), (info.get("track") or "").strip()
    w_a, w_t = (winner.get("artist") or "").strip(), (winner.get("track") or "").strip()
    if (w_a, w_t) == (db_a, db_t):
        return "db"
    return f"alt: {_short(w_a, 22)} / {_short(w_t, 22)}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-spotify", action="store_true", help="Skip Spotify probes")
    parser.add_argument("--no-genius", action="store_true", help="Skip Genius probes")
    parser.add_argument("--no-lrclib", action="store_true", help="Skip LRCLib probes")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N songs")
    parser.add_argument(
        "--song-id",
        type=int,
        default=None,
        help="Probe a single song id (debug)",
    )
    args = parser.parse_args()

    db = KaraokeDatabase()
    prefs = PreferenceManager()
    service = LyricsService(
        download_path=str(REPO_ROOT),
        events=EventSystem(),
        db=db,
        preferences=prefs,
    )

    has_genius = bool(os.environ.get("GENIUS_ACCESS_TOKEN", "").strip())
    has_spotify_cookie = bool((prefs.get_or_default("spotify_sp_dc") or "").strip())

    print(f"GENIUS_ACCESS_TOKEN present: {has_genius}")
    print(f"spotify_sp_dc present:       {has_spotify_cookie}")
    print(f"LRCLib base:                 {lyrics_mod.LRCLIB_BASE}")
    print()

    with db._connect() as conn:
        cur = conn.cursor()
        if args.song_id is not None:
            rows = cur.execute(
                "SELECT id, file_path, artist, title, duration_seconds, "
                "language, isrc, metadata_status FROM songs WHERE id = ?",
                (args.song_id,),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT id, file_path, artist, title, duration_seconds, "
                "language, isrc, metadata_status FROM songs ORDER BY id"
            ).fetchall()

    if args.limit:
        rows = rows[: args.limit]

    print(f"Probing {len(rows)} songs (production candidate path).\n")

    spotify_globally_locked_out = False
    results: list[dict] = []

    for row in rows:
        song = {
            "id": row["id"],
            "file": os.path.basename(row["file_path"]),
            "file_path": row["file_path"],
            "artist": (row["artist"] or "").strip(),
            "title": (row["title"] or "").strip(),
            "duration": row["duration_seconds"],
            "language": row["language"],
            "isrc": (row["isrc"] or "").strip() or None,
            "status": row["metadata_status"],
        }
        info = {
            "track": song["title"],
            "artist": song["artist"],
            "duration": song["duration"],
            "isrc": song["isrc"],
        }

        # Sentinel: no candidates means even the filename couldn't be
        # parsed — every source will short-circuit to None.
        candidates = service._metadata_candidates(info, row["file_path"])
        song["candidates"] = len(candidates)

        # Defaults — overwritten by each probe.
        song["lrclib"] = None
        song["lrclib_winner"] = None
        song["lrclib_err"] = None
        song["spotify_sync_type"] = None
        song["spotify_winner"] = None
        song["spotify_err"] = None
        song["genius"] = None
        song["genius_winner"] = None
        song["genius_err"] = None

        print(
            f"[{song['id']:>3}] DB: {song['artist'] or '—'} — {song['title'] or '—'}"
            f"  (status={song['status']}, candidates={song['candidates']})"
        )

        if not candidates:
            print("        SKIP — no candidates (filename has no Artist-Title separator)")
            results.append(song)
            continue

        # --- LRCLib ----------------------------------------------------
        if not args.no_lrclib:
            try:
                lrc, winner = service._try_with_candidates(
                    lambda c: lyrics_mod._fetch_lrclib(c["track"], c["artist"], c.get("duration")),
                    info,
                    row["file_path"],
                    label="LRCLib",
                )
                song["lrclib"] = bool(lrc)
                song["lrclib_winner"] = winner
                if lrc:
                    print(
                        f"        LRCLib   : HIT   ({len(lrc)} chars)  "
                        f"[{_winner_label(winner, info)}]"
                    )
                else:
                    print("        LRCLib   : miss")
            except Exception as exc:  # noqa: BLE001
                song["lrclib_err"] = repr(exc)
                print(f"        LRCLib   : ERROR {exc!r}")

        # --- Spotify ---------------------------------------------------
        if spotify_globally_locked_out:
            song["spotify_err"] = "locked_out"
            print("        Spotify  : skipped (locked out earlier)")
        elif not args.no_spotify and has_spotify_cookie:
            now = time.time()
            wait_remaining = service._spotify_rate_limited_until - now
            if wait_remaining > 120:
                print(
                    f"        Spotify  : LOCKED OUT (cooldown {wait_remaining:.0f}s); "
                    f"skipping Spotify for the rest of the run"
                )
                spotify_globally_locked_out = True
                song["spotify_err"] = "locked_out"
            else:
                if wait_remaining > 0:
                    wait = wait_remaining + 0.5
                    print(f"        Spotify  : waiting {wait:.0f}s for rate-limit cooldown")
                    time.sleep(wait)
                    service._spotify_search_cache.clear()
                try:
                    payload, winner = service._try_with_candidates(
                        lambda c: service._fetch_spotify_lyrics_payload(
                            c["track"], c["artist"], isrc=c.get("isrc")
                        ),
                        info,
                        row["file_path"],
                        label="Spotify",
                    )
                    if payload is None and time.time() < service._spotify_rate_limited_until:
                        # Rate-limited mid-call; not a real miss.
                        song["spotify_err"] = "rate_limited"
                        print("        Spotify  : RATE-LIMITED (will not count as miss)")
                    elif payload:
                        sync_type = payload.get("syncType")
                        lines = payload.get("lines") or []
                        song["spotify_sync_type"] = sync_type
                        song["spotify_winner"] = winner
                        print(
                            f"        Spotify  : {sync_type} ({len(lines)} lines)  "
                            f"[{_winner_label(winner, info)}]"
                        )
                    else:
                        song["spotify_sync_type"] = None
                        print("        Spotify  : miss")
                except Exception as exc:  # noqa: BLE001
                    song["spotify_err"] = repr(exc)
                    print(f"        Spotify  : ERROR {exc!r}")
                # Pace Spotify search requests — they throttle aggressively.
                time.sleep(1.0)
        elif not args.no_spotify:
            print("        Spotify  : skipped (sp_dc missing)")

        # --- Genius ----------------------------------------------------
        if not args.no_genius and has_genius:
            try:
                txt, winner = service._try_with_candidates(
                    lambda c: lyrics_mod._fetch_genius(c["track"], c["artist"]),
                    info,
                    row["file_path"],
                    label="Genius",
                )
                song["genius"] = bool(txt)
                song["genius_winner"] = winner
                if txt:
                    print(
                        f"        Genius   : HIT   ({len(txt)} chars plain text)  "
                        f"[{_winner_label(winner, info)}]"
                    )
                else:
                    print("        Genius   : miss")
            except Exception as exc:  # noqa: BLE001
                song["genius_err"] = repr(exc)
                print(f"        Genius   : ERROR {exc!r}")
        elif not args.no_genius:
            print("        Genius   : skipped (token missing)")

        results.append(song)
        time.sleep(0.2)

    # ---- Retry rate-limited Spotify probes ---------------------------
    rate_limited = [s for s in results if s.get("spotify_err") == "rate_limited"]
    if rate_limited and not args.no_spotify and has_spotify_cookie:
        now = time.time()
        if service._spotify_rate_limited_until > now:
            wait = min(service._spotify_rate_limited_until - now + 1.0, 120.0)
            print(
                f"\nWaiting {wait:.0f}s before retrying "
                f"{len(rate_limited)} rate-limited Spotify probes."
            )
            time.sleep(wait)
        service._spotify_search_cache.clear()
        for song in rate_limited:
            print(f"[retry {song['id']}] {song['artist']} — {song['title']}")
            info = {
                "track": song["title"],
                "artist": song["artist"],
                "duration": song["duration"],
                "isrc": song["isrc"],
            }
            try:
                payload, winner = service._try_with_candidates(
                    lambda c: service._fetch_spotify_lyrics_payload(
                        c["track"], c["artist"], isrc=c.get("isrc")
                    ),
                    info,
                    song["file_path"],
                    label="Spotify",
                )
                if payload is None and time.time() < service._spotify_rate_limited_until:
                    print("        Spotify  : STILL RATE-LIMITED")
                elif payload:
                    sync_type = payload.get("syncType")
                    lines = payload.get("lines") or []
                    song["spotify_sync_type"] = sync_type
                    song["spotify_winner"] = winner
                    song["spotify_err"] = None
                    print(f"        Spotify  : {sync_type} ({len(lines)} lines)")
                else:
                    song["spotify_sync_type"] = None
                    song["spotify_err"] = None
                    print("        Spotify  : miss")
            except Exception as exc:  # noqa: BLE001
                song["spotify_err"] = repr(exc)
                print(f"        Spotify  : ERROR {exc!r}")
            time.sleep(2.0)

    # ---- Summary -----------------------------------------------------
    total = len(results)
    no_candidates = [s for s in results if s["candidates"] == 0]
    has_any_synced = [
        s
        for s in results
        if s["candidates"] > 0
        and (s["lrclib"] or s["spotify_sync_type"] in ("LINE_SYNCED", "SYLLABLE_SYNCED"))
    ]
    has_any_source = [
        s
        for s in results
        if s["candidates"] > 0
        and (
            s["lrclib"]
            or s["spotify_sync_type"] in ("LINE_SYNCED", "SYLLABLE_SYNCED", "UNSYNCED")
            or s["genius"]
        )
    ]
    has_any_synced_or_genius = [
        s
        for s in results
        if s["candidates"] > 0
        and (
            s["lrclib"]
            or s["spotify_sync_type"] in ("LINE_SYNCED", "SYLLABLE_SYNCED")
            or s["genius"]
        )
    ]
    zero_coverage = [
        s
        for s in results
        if s["candidates"] > 0
        and not s["lrclib"]
        and s["spotify_sync_type"] not in ("LINE_SYNCED", "SYLLABLE_SYNCED", "UNSYNCED")
        and not s["genius"]
    ]
    rate_limited_unresolved = [s for s in results if s.get("spotify_err") == "rate_limited"]
    spotify_locked_out = [s for s in results if s.get("spotify_err") == "locked_out"]

    print()
    print("=== SUMMARY ===")
    print(f"Total songs                 : {total}")
    print(f"No candidates (unfetchable) : {len(no_candidates)}")
    probe_total = total - len(no_candidates)
    print(f"Synced (LRCLib or Spotify)  : {len(has_any_synced)}/{probe_total}")
    print(f"Synced or Genius (real cov) : {len(has_any_synced_or_genius)}/{probe_total}")
    print(f"Any text source             : {len(has_any_source)}/{probe_total}")
    print(f"Zero coverage               : {len(zero_coverage)}")
    if rate_limited_unresolved:
        print(f"Spotify rate-limited        : {len(rate_limited_unresolved)} (uncounted)")
    if spotify_locked_out:
        print(f"Spotify locked out          : {len(spotify_locked_out)} (untested)")
    if zero_coverage:
        for s in zero_coverage:
            print(f"  - id={s['id']} {s['artist']} — {s['title']}")
    if no_candidates:
        print("Songs with no candidates:")
        for s in no_candidates:
            print(f"  - id={s['id']} status={s['status']} file={s['file']}")

    # ---- Markdown report --------------------------------------------
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    md = ["# Lyrics-source coverage report (production candidate path)", ""]
    md.append(
        "Probes every song through "
        "``LyricsService._try_with_candidates`` so split-artist credits, "
        "filename `regex_tidy`, iTunes, and MusicBrainz alternatives are "
        "all exercised — i.e. exactly what end users get."
    )
    md.append("")
    md.append(f"- Total songs: **{total}**")
    md.append(f"- Songs with no candidates: **{len(no_candidates)}**")
    md.append(
        f"- Synced coverage (LRCLib or Spotify timed): "
        f"**{len(has_any_synced)} / {probe_total}**"
    )
    md.append(
        f"- Synced or Genius (production-usable coverage): "
        f"**{len(has_any_synced_or_genius)} / {probe_total}**"
    )
    md.append(
        f"- Any text source (incl. Spotify UNSYNCED): " f"**{len(has_any_source)} / {probe_total}**"
    )
    md.append("")
    md.append(
        "| id | artist | title | dur | lang | LRCLib (winner) | Spotify (winner) | Genius (winner) | status |"
    )
    md.append(
        "|----|--------|-------|-----|------|-----------------|------------------|-----------------|--------|"
    )

    def cell(hit: bool | None, winner: dict | None, info_in: dict) -> str:
        if hit is None:
            return "—"
        if not hit:
            return "·"
        tag = _winner_label(winner, info_in)
        return f"✓ ({tag})" if tag else "✓"

    def spot_cell(s: dict, info_in: dict) -> str:
        err = s.get("spotify_err")
        if err == "locked_out":
            return "lock"
        if err == "rate_limited":
            return "429"
        if err:
            return "ERR"
        st = s.get("spotify_sync_type")
        if st in ("LINE_SYNCED", "SYLLABLE_SYNCED", "UNSYNCED"):
            tag = _winner_label(s.get("spotify_winner"), info_in)
            short = {
                "LINE_SYNCED": "line",
                "SYLLABLE_SYNCED": "syll",
                "UNSYNCED": "text",
            }[st]
            return f"{short} ({tag})" if tag else short
        if st is None and s.get("spotify_winner") is None and not args.no_spotify:
            # Fetcher executed and returned no payload across all candidates.
            return "·"
        return "—"

    for s in results:
        info_in = {
            "track": s["title"],
            "artist": s["artist"],
            "duration": s["duration"],
            "isrc": s["isrc"],
        }
        md.append(
            "| {id} | {artist} | {title} | {dur} | {lang} | "
            "{lrc} | {spot} | {gen} | {status} |".format(
                id=s["id"],
                artist=_short(s["artist"], 30) or "—",
                title=_short(s["title"], 35) or "—",
                dur=int(s["duration"]) if s["duration"] else "—",
                lang=s["language"] or "—",
                lrc=("ERR" if s["lrclib_err"] else cell(s["lrclib"], s["lrclib_winner"], info_in)),
                spot=spot_cell(s, info_in),
                gen=("ERR" if s["genius_err"] else cell(s["genius"], s["genius_winner"], info_in)),
                status=s["status"] or "—",
            )
        )

    if zero_coverage:
        md.append("")
        md.append("## Zero-coverage songs")
        md.append("")
        for s in zero_coverage:
            md.append(f"- id={s['id']} `{s['artist']} — {s['title']}` (lang={s['language']})")

    if no_candidates:
        md.append("")
        md.append("## Songs with no candidates (filename has no `Artist - Title`)")
        md.append("")
        for s in no_candidates:
            md.append(f"- id={s['id']} status=`{s['status']}` file=`{s['file']}`")

    OUT_PATH.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nReport written to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
