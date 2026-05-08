"""Re-probe LRCLib for the zero-coverage / no-metadata songs using
``LyricsService._try_with_candidates`` — the path production actually
takes (split-artist credits, filename regex_tidy, iTunes, MusicBrainz).

This isolates "no LRCLib record exists" from "production path would
have rescued this song after the naive (DB artist, DB title) miss".
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_env = REPO_ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from pikaraoke.lib import lyrics as lyrics_mod  # noqa: E402
from pikaraoke.lib.events import EventSystem  # noqa: E402
from pikaraoke.lib.karaoke_database import KaraokeDatabase  # noqa: E402
from pikaraoke.lib.lyrics import LyricsService  # noqa: E402
from pikaraoke.lib.preference_manager import PreferenceManager  # noqa: E402

# Songs flagged in the first pass.
TARGET_IDS = [96, 97, 98, 101, 107, 108, 119]


def main() -> int:
    db = KaraokeDatabase()
    prefs = PreferenceManager()
    service = LyricsService(
        download_path=str(REPO_ROOT),
        events=EventSystem(),
        db=db,
        preferences=prefs,
    )

    with db._connect() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, file_path, artist, title, duration_seconds, isrc, "
            "language, metadata_status FROM songs WHERE id IN ({})".format(
                ",".join("?" for _ in TARGET_IDS)
            ),
            TARGET_IDS,
        ).fetchall()

    for row in sorted(rows, key=lambda r: r["id"]):
        sid = row["id"]
        artist = (row["artist"] or "").strip()
        title = (row["title"] or "").strip()
        info = {
            "track": title,
            "artist": artist,
            "duration": row["duration_seconds"],
            "isrc": (row["isrc"] or "").strip() or None,
        }
        print(
            f"\n[{sid}] file={os.path.basename(row['file_path'])} "
            f"status={row['metadata_status']} lang={row['language']}"
        )
        print(f"      DB artist=  {artist!r}")
        print(f"      DB title =  {title!r}")
        print(f"      DB dur   =  {row['duration_seconds']}")
        print(f"      DB isrc  =  {info['isrc']}")

        candidates = service._metadata_candidates(info, row["file_path"])
        print(f"      candidates ({len(candidates)}):")
        for i, c in enumerate(candidates, 1):
            print(f"        {i}. artist={c['artist']!r} track={c['track']!r}")

        if not candidates:
            print("      → cannot probe (no candidates)")
            continue

        def _fetch_lrclib(cand: dict) -> str | None:
            return lyrics_mod._fetch_lrclib(cand["track"], cand["artist"], cand.get("duration"))

        lrc, winner = service._try_with_candidates(
            _fetch_lrclib, info, row["file_path"], label="LRCLib"
        )
        if lrc:
            print(
                f"      LRCLib HIT via candidate: artist={winner['artist']!r} "
                f"track={winner['track']!r} (LRC {len(lrc)} chars)"
            )
        else:
            print("      LRCLib: every candidate missed")

        if os.environ.get("GENIUS_ACCESS_TOKEN"):
            text, gwinner = service._try_with_candidates(
                lambda c: lyrics_mod._fetch_genius(c["track"], c["artist"]),
                info,
                row["file_path"],
                label="Genius",
            )
            if text:
                print(
                    f"      Genius HIT via candidate: artist={gwinner['artist']!r} "
                    f"track={gwinner['track']!r} ({len(text)} chars)"
                )
            else:
                print("      Genius: every candidate missed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
