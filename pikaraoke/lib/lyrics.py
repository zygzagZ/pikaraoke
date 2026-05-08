"""Auto-fetch synced lyrics from LRCLib and render as ASS subtitles.

Pipeline:
  song_downloaded event -> LyricsService.fetch_and_convert
    1. Read track/artist/duration from the ``songs`` table
       (``register_download`` seeded them from yt-dlp's info.json).
    2. Query LRCLib for syncedLyrics.
    3. Convert LRC to line-level ASS and write <stem>.ass.
    4. (Optional) in a background thread, run forced alignment and
       replace the ASS with per-word \\k-tagged highlighting.

The existing .ass stack (FileResolver, SubtitlesOctopus in splash.js)
renders the output automatically - no UI changes required.

When LRCLib and VTT conversion both fail, the original ``<stem>*.vtt``
is left on disk so the user's YouTube captions are not deleted along
with the failed conversion attempt — raw captions beat zero captions.
"""

import hashlib
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from threading import Thread
from typing import Protocol
from urllib.parse import quote_plus

import librosa
import requests

from pikaraoke.lib.events import EventSystem
from pikaraoke.lib.karaoke_database import (
    SUBTITLE_SOURCE_AI,
    SUBTITLE_SOURCE_CONSENSUS,
    SUBTITLE_SOURCE_GENIUS_SYNC,
    SUBTITLE_SOURCE_LRCLIB,
    SUBTITLE_SOURCE_LRCLIB_SYNC,
    SUBTITLE_SOURCE_SPOTIFY,
    SUBTITLE_SOURCE_SPOTIFY_SYNC,
    SUBTITLE_SOURCE_TEKSTOWO_SYNC,
    SUBTITLE_SOURCE_USER,
    SUBTITLE_SOURCE_YOUTUBE_VTT,
    VARIANT_FILE_SOURCES,
    KaraokeDatabase,
)
from pikaraoke.lib.lyrics_audio_probe import probe_language as _probe_audio_language
from pikaraoke.lib.lyrics_audio_probe import (
    probe_language_whole_song as _probe_audio_language_whole_song,
)
from pikaraoke.lib.lyrics_audio_probe import (
    read_cached_verdict as _read_cached_probe_verdict,
)
from pikaraoke.lib.lyrics_language_classifier import (
    classify_and_persist as _classify_language,
)
from pikaraoke.lib.lyrics_language_classifier import read_info_json as _read_info_json
from pikaraoke.lib.lyrics_pipeline_failure_cache import (
    clear_failure as _clear_pipeline_failure,
)
from pikaraoke.lib.lyrics_pipeline_failure_cache import (
    record_failure as _record_pipeline_failure,
)
from pikaraoke.lib.metadata_parser import (
    has_artist_title_separator,
    regex_tidy,
    remove_accents,
)
from pikaraoke.lib.music_metadata import (
    _itunes_row_to_dict,
    _search_itunes_cached,
    fetch_musicbrainz_language_signals,
    normalize_title,
    resolve_metadata,
    search_musicbrainz,
)
from pikaraoke.lib.whisper_transcript_cache import (
    read_cached_transcript as _read_cached_whisper_transcript,
)
from pikaraoke.lib.whisper_transcript_cache import (
    write_cached_transcript as _write_cached_whisper_transcript,
)

logger = logging.getLogger(__name__)

LRCLIB_BASE = "https://lrclib.net"
# (connect, read) — server is consistently slow; legitimate /api/get
# responses regularly take 8-10s, and a too-aggressive read timeout
# masquerades as a miss. Connect stays short to fail fast on outages.
LRCLIB_TIMEOUT: tuple[float, float] = (5.0, 15.0)

# Tolerance (seconds) when filtering ``/api/search`` results by duration.
# LRCLib's ``/api/search`` doesn't accept a duration query parameter, so
# the index can return synced lyrics for a different edit (radio cut vs
# extended mix). Skipping results whose duration drifts beyond this
# threshold defends the consensus engine before its prior-reliability
# grader has to clean up after a known-bad source. 30s mirrors the
# duration band used by ``_grade_priors``.
_LRCLIB_DURATION_TOLERANCE_S = 30.0

GENIUS_BASE = "https://api.genius.com"
GENIUS_TIMEOUT = 5.0
GENIUS_ACCESS_TOKEN = os.environ.get("GENIUS_ACCESS_TOKEN", "").strip()

TEKSTOWO_BASE = "https://www.tekstowo.pl"
TEKSTOWO_TIMEOUT = 5.0
# Tekstowo serves the bot-detector page when the User-Agent looks scriptable;
# pretend to be a desktop browser to get the real song page.
TEKSTOWO_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

SPOTIFY_TIMEOUT = 5.0
SPOTIFY_SERVER_TIME_URL = "https://open.spotify.com/api/server-time"
SPOTIFY_TOKEN_URL = "https://open.spotify.com/api/token"
SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"
SPOTIFY_LYRICS_URL = (
    "https://spclient.wg.spotify.com/color-lyrics/v2/track/{track_id}"
    "?format=json&vocalRemoval=false"
)
SPOTIFY_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# Upper bound on the in-thread sleep we accept from Spotify's Retry-After
# on a 429 to ``/v1/search``. Beyond this we punt back to the orchestrator
# rather than parking a worker.
SPOTIFY_RATE_LIMIT_SLEEP_CAP = 90.0
# Above this Retry-After value we treat Spotify as locked-out for the day
# and short-circuit without sleeping any of it — the cooldown gate alone
# parks future calls until the window passes. Avoids stacking 30s+50s+...
# sleeps on a single song right before the second 429 inevitably hands
# us a 24h Retry-After.
SPOTIFY_RATE_LIMIT_LONG_S = 300.0

# Last-resort ASR fallback. When LRCLib / Genius / YouTube VTT all miss,
# transcribe the vocals stem with faster-whisper so the song still gets
# subtitles (flagged as auto-generated in the UI). Set the env var to
# one of {"off","none","false","0"} to disable; otherwise the value is
# the faster-whisper model name ("tiny" / "base" / "small" / "medium" /
# "large-v2" / "large-v3" / "large-v3-turbo" / ...).
#
# Default "large-v3-turbo" (aka "turbo"): ~1.5 GB, distilled from
# large-v3 — near-large-v3 transcription quality at ~7x the decoding
# speed. Heavy enough that Demucs-isolated vocals + non-English lyrics
# actually come out legible (small/medium routinely mangle Polish rap)
# but still fits in RAM and runs in ~real-time on a modern CPU with
# int8. Downgrade to "medium" on low-RAM boxes; bump to "large-v3" when
# raw accuracy matters more than wall time.
_WHISPER_OPT_OUT = {"off", "none", "false", "0"}
_WHISPER_LOW_RAM_GB = 6.0
_WHISPER_LOW_RAM_DEFAULT = "tiny.en"
_WHISPER_DEFAULT = "large-v3-turbo"


def _resolve_whisper_model() -> str:
    """Honour ``WHISPER_FALLBACK_MODEL`` but auto-downgrade on low-RAM hosts.

    Demucs (~2 GB) plus the default ``large-v3-turbo`` (~1.5 GB) saturate a
    Pi 4 / 4 GB Mac mini. When total RAM is below 6 GB and the user did
    not pin a "tiny" variant explicitly, swap to ``tiny.en`` so the
    consensus pipeline can still cite Whisper as an audio reference.
    Resolved once per process, cached.
    """
    global _WHISPER_MODEL_RESOLVED
    if _WHISPER_MODEL_RESOLVED is not None:
        return _WHISPER_MODEL_RESOLVED
    requested = os.environ.get("WHISPER_FALLBACK_MODEL", "").strip() or _WHISPER_DEFAULT
    if requested.lower() in _WHISPER_OPT_OUT:
        _WHISPER_MODEL_RESOLVED = requested
        return requested
    try:
        import psutil

        total_gb = psutil.virtual_memory().total / (1024**3)
    except Exception:
        total_gb = float("inf")
    if total_gb < _WHISPER_LOW_RAM_GB and "tiny" not in requested.lower():
        logger.warning(
            "Whisper: detected %.1f GB RAM, auto-downgrading %r -> %r. "
            "Set WHISPER_FALLBACK_MODEL explicitly to override.",
            total_gb,
            requested,
            _WHISPER_LOW_RAM_DEFAULT,
        )
        _WHISPER_MODEL_RESOLVED = _WHISPER_LOW_RAM_DEFAULT
    else:
        _WHISPER_MODEL_RESOLVED = requested
    return _WHISPER_MODEL_RESOLVED


_WHISPER_MODEL_RESOLVED: str | None = None
_whisper_model_cache: list = [None]
_whisper_model_lock = threading.Lock()


# ----- Multi-source consensus dependencies (soft imports) -----
#
# The ``syncedlyrics`` PyPI package wraps Musixmatch + Megalobiz token
# rotation. It is optional: when missing or its mobile-token rotation
# breaks in a release, the consensus pipeline degrades to LRCLib +
# Genius + VTT + Whisper without code changes.
try:
    import syncedlyrics as _syncedlyrics

    _SYNCEDLYRICS_AVAILABLE = True
except ImportError:
    _syncedlyrics = None  # type: ignore[assignment]
    _SYNCEDLYRICS_AVAILABLE = False


# Operator gates for the consensus pipeline. ``LYRICS_CONSENSUS_ENABLED``
# is the master switch — when "0"/"off"/"false" (or any value below) the
# legacy LRC -> Genius -> Whisper sequential pipeline runs unchanged.
# Default-on as of the confidence-driven hybrid aligner: the orchestrator
# now grades each song's priors (``_grade_priors``) and routes between
# the fast LRC-windowed path and the synthetic-LRC fallback, so the
# voted consensus + audio-anchored grader is the right default.
# Operators on tiny Pi devices can opt out with ``LYRICS_CONSENSUS_ENABLED=0``.
# ``LYRICS_CONSENSUS_PROVIDERS`` is a comma-separated allowlist for the
# syncedlyrics-backed sources. Empty = both disabled.
def _consensus_enabled() -> bool:
    return os.environ.get("LYRICS_CONSENSUS_ENABLED", "1").strip().lower() in {
        "1",
        "on",
        "true",
        "yes",
    }


def _consensus_providers() -> set[str]:
    raw = os.environ.get("LYRICS_CONSENSUS_PROVIDERS", "musixmatch,megalobiz")
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


# Process-wide semaphore that bounds simultaneous consensus orchestrators.
# Each ``_upgrade_via_consensus`` call holds one slot for the duration of
# its fan-out + alignment + ASS write. Default 2 keeps Pi-class hardware
# from drowning when consensus mode is default-on; operators on tiny
# Pis can drop to ``LYRICS_CONSENSUS_MAX_CONCURRENT=1`` and bigger boxes
# can raise it. The semaphore self-rebuilds when the env value changes
# so tests can resize it between cases.
_consensus_semaphore_state: dict = {"limit": None, "sem": None}
_consensus_semaphore_state_lock = threading.Lock()


def _consensus_max_concurrent() -> int:
    try:
        return max(1, int(os.environ.get("LYRICS_CONSENSUS_MAX_CONCURRENT", "2")))
    except ValueError:
        return 2


def _get_consensus_semaphore() -> threading.Semaphore:
    limit = _consensus_max_concurrent()
    with _consensus_semaphore_state_lock:
        if _consensus_semaphore_state["limit"] != limit:
            _consensus_semaphore_state["limit"] = limit
            _consensus_semaphore_state["sem"] = threading.Semaphore(limit)
        return _consensus_semaphore_state["sem"]


# Trailing mix/version markers in parens or brackets: "(Instrumental)",
# "[Karaoke]", "(Acoustic Version)", "(Punk Version)", "(Cover)", etc.
# LRCLib + Genius index lyrics once per song regardless of release variant,
# so these suffixes drop otherwise-good matches. Applied to the upstream
# query only; DB titles are untouched.
#
# The ``\w+\s+version`` and ``\w+\s+mix`` alternations catch genre-prefixed
# variants ("Punk Version", "Album Version", "Studio Version", "Club Mix")
# without enumerating every genre — and the upstream caller only strips
# this from the iTunes side, so a query that genuinely names "Punk Version"
# won't be touched (the guard fires only when iTunes adds a variant the
# query lacks).
_VARIANT_RE = re.compile(
    r"\s*[\(\[]"
    r"[^)\]]*?"
    r"\b(?:"
    r"instrumental|karaoke|acoustic(?:\s+version)?|live|remix|"
    r"remastered|extended|radio\s+edit|"
    r"cover(?:\s+version)?|unplugged|demo|bonus\s+track|"
    r"\w+\s+version|\w+\s+mix"
    r")\b"
    r"[^)\]]*"
    r"[\)\]]\s*$",
    re.IGNORECASE,
)

# LRC timestamp: [mm:ss.xx] or [mm:ss.xxx] or [mm:ss]
_LRC_TAG = re.compile(r"\[(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]")

# VTT cue timestamp line: `00:00:01.000 --> 00:00:03.000`.
_VTT_CUE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})\.(\d{3})"
)
# Inline tags like `<c>`, `</c>`, `<00:00:01.000>`, `<v Speaker>`.
_VTT_TAG = re.compile(r"<[^>]+>")

# Marker in [Script Info] used to distinguish auto-generated ASS from
# user-supplied Aegisub files. Auto-generated files may be overwritten
# on re-download; user files are left alone.
ASS_MARKER = "PiKaraoke Auto-Lyrics"

VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".avi")


# Lyrics quality tiers. Progressive writes only upgrade (never downgrade):
# a later source with a lower tier is silently dropped by the tier gate.
_TIER_NONE = 0
_TIER_LINE_VTT = 1
_TIER_LINE_LRC = 2
_TIER_WORD = 3

_TIER_NAMES = {
    _TIER_NONE: "none",
    _TIER_LINE_VTT: "line_vtt",
    _TIER_LINE_LRC: "line_lrc",
    _TIER_WORD: "word",
}

# Coarse "word" vs "line" tier exposed to the chip UI / subtitle_jobs row.
# Anything aligner-driven is word-level; the line-level sources are LRClib's
# raw line .ass and YouTube's VTT captions. The granular tier-gate above
# uses _TIER_LINE_VTT vs _TIER_LINE_LRC to break ties between two
# line-level renders, but the UI doesn't need that distinction.
_VARIANT_SOURCE_TIERS: dict[str, str] = {
    SUBTITLE_SOURCE_LRCLIB: "line",
    SUBTITLE_SOURCE_SPOTIFY: "line",
    SUBTITLE_SOURCE_YOUTUBE_VTT: "line",
    SUBTITLE_SOURCE_LRCLIB_SYNC: "word",
    SUBTITLE_SOURCE_GENIUS_SYNC: "word",
    SUBTITLE_SOURCE_SPOTIFY_SYNC: "word",
    SUBTITLE_SOURCE_TEKSTOWO_SYNC: "word",
    SUBTITLE_SOURCE_AI: "word",
    SUBTITLE_SOURCE_CONSENSUS: "word",
}


def _tier_for_variant_source(source: str) -> str | None:
    return _VARIANT_SOURCE_TIERS.get(source)


# Map consensus-engine source names (`lyrics_consensus.SourceResult.name`)
# to the canonical subtitle-source keys used by ``subtitle_jobs.source``
# and the picker chip row. Sources that don't have a per-song variant
# file (Musixmatch, Megalobiz — no on-demand picker entry) are absent;
# the persister silently skips them. Audio-ref sources (vtt, whisper)
# never appear in ``ConsensusResult.source_scores`` so they don't need
# a mapping either.
_CONSENSUS_SOURCE_TO_VARIANT: dict[str, str] = {
    "lrclib": SUBTITLE_SOURCE_LRCLIB,
    "genius": SUBTITLE_SOURCE_GENIUS_SYNC,
}

# Which ``subtitle_jobs.source`` row carries the coverage score the
# variant render path should gate against. ``lrclib-sync`` re-uses
# LRCLib's raw record (only the alignment differs) so both line + sync
# variants gate on the same persisted score under ``lrclib``. Variants
# that don't share upstream data with another source point to
# themselves.
_VARIANT_SCORE_KEY: dict[str, str] = {
    SUBTITLE_SOURCE_LRCLIB: SUBTITLE_SOURCE_LRCLIB,
    SUBTITLE_SOURCE_LRCLIB_SYNC: SUBTITLE_SOURCE_LRCLIB,
    SUBTITLE_SOURCE_GENIUS_SYNC: SUBTITLE_SOURCE_GENIUS_SYNC,
}


# Per-source coverage rejection thresholds — re-exported here so the
# variant gate can mirror the consensus engine's accept/reject decision
# without importing the private name. ``_DEFAULT_THRESHOLD`` is used
# when a variant source has no entry. Keep in sync with
# ``lyrics_consensus._REJECT_THRESHOLDS``.
def _coverage_threshold_for(variant_source: str) -> float:
    from pikaraoke.lib import lyrics_consensus as _lc

    score_key = _VARIANT_SCORE_KEY.get(variant_source, variant_source)
    consensus_key_map = {v: k for k, v in _CONSENSUS_SOURCE_TO_VARIANT.items()}
    consensus_name = consensus_key_map.get(score_key, score_key)
    return _lc._REJECT_THRESHOLDS.get(consensus_name, _lc._DEFAULT_THRESHOLD)


def _dispatch_reenrich(db, song_id: int, song_path: str) -> None:
    """Fire ``enrich_song`` in a daemon thread after a language-write site.

    The enricher is idempotent: when ``songs.language_at_enrich ==
    songs.language`` and ``metadata_status == 'enriched'`` it short-circuits.
    So calling this hook is safe even if the language didn't actually change
    — every classifier / whisper-probe write site invokes it, and the
    enricher decides whether work is needed. Imports are deferred so the
    lyrics module stays importable in environments where the enricher's
    requests/network chain isn't installed.
    """
    if db is None:
        return

    def _run() -> None:
        try:
            from pikaraoke.lib.song_enricher import enrich_song

            enrich_song(db, song_id, song_path)
        except Exception:
            logger.exception("re-enrich after language write failed for %s", song_path)

    threading.Thread(
        target=_run, name=f"reenrich-{os.path.basename(song_path)}", daemon=True
    ).start()


@dataclass(frozen=True)
class WordPart:
    """Sub-word chunk with its audio-aligned start/end in seconds.

    Used for both per-character alignment (WhisperX path, real wav2vec2
    CTC timings per glyph) and per-syllable alignment (Whisper-ASR
    fallback, pyphen-derived boundaries with uniformly interpolated
    timings inside the word duration). The ASS renderer emits one
    ``\\kf`` tag per part.
    """

    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Word:
    """A single word with its start/end time in seconds.

    ``parts`` is the sub-word breakdown used by the ASS renderer to emit
    multiple ``\\kf`` tags inside a single word. On the WhisperX path
    these are per-character with real wav2vec2 CTC timings; on the
    Whisper-ASR fallback path they are per-syllable (pyphen) with
    timings interpolated across the word duration. ``None`` means the
    info is unavailable or the word is a single part - the renderer
    emits one ``\\kf`` spanning the whole word in that case.
    """

    text: str
    start: float
    end: float
    parts: tuple[WordPart, ...] | None = None


class Aligner(Protocol):
    """Produces word-level timings for a song given its audio and reference lyrics."""

    def align(
        self, audio_path: str, reference_text: str, language: str | None = None
    ) -> list[Word]:
        """``language`` is an optional hint that lets the aligner skip its own
        detection pass (e.g. when the caller already cached a prior result)."""
        ...

    @property
    def model_id(self) -> str:
        """Stable identifier recorded in the DB so model swaps invalidate cached .ass."""
        ...


class LyricsService:
    """Fetches synced lyrics from LRCLib and writes them as ASS subtitles."""

    def __init__(
        self,
        download_path: str,
        events: EventSystem,
        aligner: Aligner | None = None,
        db: KaraokeDatabase | None = None,
        preferences: object | None = None,
    ) -> None:
        self._download_path = download_path
        self._events = events
        self._aligner = aligner
        self._db = db
        # PreferenceManager is read dynamically on every Spotify fetch so
        # operators can rotate the sp_dc cookie via the settings UI without
        # restarting the server. Optional to keep the test surface narrow.
        self._preferences = preferences
        # Cached Spotify access token: (token, exp_unix). The token is short-
        # lived (~1h) and is regenerated lazily from the sp_dc cookie.
        self._spotify_token_cache: tuple[str, float] | None = None
        self._spotify_token_lock = threading.Lock()
        # Memoized track-id lookups + global rate-limit cooldown. The
        # public Search API (api.spotify.com/v1/search) throttles
        # web-player tokens hard — without these two, hitting 429 once
        # blocks subsequent unrelated lookups for ~40s. Both are
        # process-local; clearing happens implicitly when LyricsService
        # is recreated.
        self._spotify_search_cache: dict[tuple[str, str, str], str | None] = {}
        self._spotify_rate_limited_until: float = 0.0
        # Per-song tier of the most recently written .ass. Parallel source
        # workers go through `_try_write_ass_tiered` which reads + updates
        # this under `_tier_lock` — a later source with a lower tier is
        # dropped so VTT can never overwrite a word-level .ass that already
        # landed.
        self._tier_state: dict[str, int] = {}
        self._tier_lock = threading.Lock()
        # In-flight on-demand variant fetches keyed by (song_path, source).
        # Two admin clients clicking the same source for the same song must
        # not spawn two workers; the second call early-returns. The set is
        # mutated only under ``_in_flight_lock`` (CG1 — Python's bare set
        # is not thread-safe under concurrent add/remove).
        self._in_flight_variants: set[tuple[str, str]] = set()
        self._in_flight_lock = threading.Lock()

    @property
    def has_aligner(self) -> bool:
        """True when whisperx (or any word-level aligner) is configured."""
        return self._aligner is not None

    def _reset_tier(self, song_path: str) -> None:
        """Clear the tier for a song at the start of a fresh pipeline run."""
        with self._tier_lock:
            self._tier_state[song_path] = _TIER_NONE

    def _current_tier(self, song_path: str) -> int:
        with self._tier_lock:
            return self._tier_state.get(song_path, _TIER_NONE)

    def _try_write_ass_tiered(
        self,
        song_path: str,
        new_tier: int,
        ass: str,
        *,
        lyrics_source: str,
        aligner_model: str | None,
        lyrics_sha: str | None,
    ) -> bool:
        """Write + register the .ass only when it upgrades the current tier.

        Thread-safe. Holds ``_tier_lock`` across the atomic write + DB
        provenance update so two workers finishing in close succession
        can't interleave and leave the DB describing a file that's no
        longer on disk. ``_register_ass`` fires ``lyrics_upgraded`` which
        the splash uses to hot-swap the subtitle URL.

        ``lyrics_provenance`` is derived from ``aligner_model``: present ⇒
        word-level (``auto_word``), absent ⇒ line-level (``auto_line``).
        The startup sweep (``Karaoke._invalidate_stale_alignments_from_db``)
        reads it to decide which cached files to invalidate after a model
        bump without touching line-level files.
        """
        from pikaraoke.lib.karaoke_database import (
            LYRICS_PROVENANCE_AUTO_LINE,
            LYRICS_PROVENANCE_AUTO_WORD,
        )

        provenance = (
            LYRICS_PROVENANCE_AUTO_WORD
            if aligner_model is not None
            else LYRICS_PROVENANCE_AUTO_LINE
        )
        # Variant write is unconditional — every successful source render
        # leaves a per-source ``<stem>.<source>.ass`` so the operator can
        # later pin that source via the picker even when a higher tier
        # already won the canonical ``<stem>.ass``. ``user`` / ``off``
        # skip the variant file (no entry in ``VARIANT_FILE_SOURCES``);
        # ``consensus`` writes both canonical and ``<stem>.consensus.ass``
        # so the picker exposes "Auto" as a pinnable variant.
        write_variant = lyrics_source in VARIANT_FILE_SOURCES
        with self._tier_lock:
            current = self._tier_state.get(song_path, _TIER_NONE)
            if write_variant:
                try:
                    _write_ass_atomic(
                        song_path, ass, target_path=_variant_ass_path(song_path, lyrics_source)
                    )
                except OSError:
                    # Variant write failure is non-fatal: log and fall through
                    # so the canonical write still has a chance to land.
                    logger.exception(
                        "tier gate: variant write failed for %s/%s",
                        os.path.basename(song_path),
                        lyrics_source,
                    )
                    write_variant = False
            if new_tier < current:
                if write_variant:
                    # Canonical write tier-gated, but the variant landed —
                    # still register it so the picker can find the file.
                    self._register_variant_artifact(song_path, lyrics_source)
                logger.info(
                    "tier gate: %s dropped %s canonical (tier=%s < current=%s); " "variant=%s",
                    os.path.basename(song_path),
                    lyrics_source,
                    _TIER_NAMES[new_tier],
                    _TIER_NAMES[current],
                    "kept" if write_variant else "skipped",
                )
                return False
            _write_ass_atomic(song_path, ass)
            self._tier_state[song_path] = new_tier
            self._register_ass(
                song_path,
                lyrics_source=lyrics_source,
                aligner_model=aligner_model,
                lyrics_sha=lyrics_sha,
                lyrics_provenance=provenance,
                also_register_variant=write_variant,
            )
            # Canonical write succeeded — drop any stale failure marker so
            # the integrity scan doesn't keep this song in backoff after
            # we've actually produced a .ass for it.
            self._clear_pipeline_failure_marker(song_path)
            logger.info(
                "tier gate: %s wrote %s (tier=%s -> %s, variant=%s)",
                os.path.basename(song_path),
                lyrics_source,
                _TIER_NAMES[current],
                _TIER_NAMES[new_tier],
                "yes" if write_variant else "no",
            )
            return True

    def _clear_pipeline_failure_marker(self, song_path: str) -> None:
        """Remove the canonical-pipeline failure record for ``song_path``.

        Called from inside ``_try_write_ass_tiered`` on successful canonical
        writes. Best-effort: lookup or write failures are logged and
        swallowed so a transient DB hiccup can't poison the success path.
        """
        if self._db is None:
            return
        audio_sha = self._audio_sha_for_song(song_path)
        if not audio_sha:
            return
        _clear_pipeline_failure(self._db.set_metadata, audio_sha)

    def _register_ass(
        self,
        song_path: str,
        lyrics_source: str,
        aligner_model: str | None,
        lyrics_sha: str | None,
        lyrics_provenance: str,
        *,
        also_register_variant: bool = False,
    ) -> None:
        """Record the written .ass in song_artifacts and stamp processing config.

        ``lyrics_sha`` fingerprints the LRC text that produced the .ass, so a
        later LRCLib refresh returning different content invalidates the cache.
        No-op when db is not wired or when the song is not in the DB.

        When ``also_register_variant`` is set, also upsert an
        ``ass_<lyrics_source>`` row pointing at ``<stem>.<source>.ass`` —
        used by the source-picker UI to flag pre-cached variants.

        Emits ``lyrics_upgraded`` so the splash UI can refresh its
        lyrics_source badge (and cache-bust the subtitle URL if the song
        is already playing and the .ass was swapped in mid-song).
        """
        if self._db is None:
            return
        song_id = self._db.get_song_id_by_path(song_path)
        if song_id is None:
            return
        artifacts = [{"role": "ass_auto", "path": _ass_path(song_path)}]
        if also_register_variant:
            artifacts.append(
                {
                    "role": f"ass_{lyrics_source}",
                    "path": _variant_ass_path(song_path, lyrics_source),
                }
            )
        self._db.upsert_artifacts(song_id, artifacts)
        self._db.update_processing_config(
            song_id,
            lyrics_source=lyrics_source,
            aligner_model=aligner_model,
            lyrics_sha=lyrics_sha,
            lyrics_provenance=lyrics_provenance,
        )
        try:
            self._events.emit("lyrics_upgraded", song_path)
        except Exception:
            logger.exception("failed to emit lyrics_upgraded for %s", song_path)
        self._emit_song_event(
            song_path,
            phase="lyrics",
            message="Lyrics fetched",
            detail=f"source={lyrics_source}, provenance={lyrics_provenance}",
        )

    def _register_variant_artifact(self, song_path: str, source: str) -> None:
        """Upsert an ``ass_<source>`` artifact row without touching processing config.

        Used by ``_try_write_ass_tiered`` when the canonical write was
        tier-gated but the variant file still landed on disk; the picker
        then has the row to surface it as ``GOTOWE``.
        """
        if self._db is None:
            return
        song_id = self._db.get_song_id_by_path(song_path)
        if song_id is None:
            return
        self._db.upsert_artifacts(
            song_id,
            [{"role": f"ass_{source}", "path": _variant_ass_path(song_path, source)}],
        )
        self._maybe_recompute_consensus(song_path, source)

    def _maybe_recompute_consensus(self, song_path: str, just_landed_source: str) -> None:
        """Re-run consensus when the available source set changed.

        Computes a hash of on-disk variant files + Whisper cache state,
        compares against the hash from the last consensus run, and
        dispatches a rerun if different. Whisper transcript reads from
        cache so a rerun never re-invokes the ASR model. The consensus
        engine's own semaphore serializes concurrent dispatches.

        Skips when:
          * the just-landed source IS consensus (avoid feedback loop),
          * no audio fingerprint to key the hash off,
          * the hash is unchanged (no new input vs last run).
        """
        from pikaraoke.lib.karaoke_database import SUBTITLE_SOURCE_CONSENSUS as _CONSENSUS

        if just_landed_source == _CONSENSUS:
            return
        if self._db is None:
            return
        if not _consensus_enabled() or self._aligner is None:
            return
        try:
            new_hash = self._compute_consensus_input_hash(song_path)
        except Exception:
            logger.exception(
                "consensus rerun: hash compute failed for %s", os.path.basename(song_path)
            )
            return
        if not new_hash:
            return
        audio_sha = self._audio_sha_for_song(song_path)
        if not audio_sha:
            return
        cache_key = f"consensus_input_hash:{audio_sha}"
        try:
            prev = self._db.get_metadata(cache_key)
        except Exception:
            prev = None
        if prev == new_hash:
            return
        try:
            self._db.set_metadata(cache_key, new_hash)
        except Exception:
            logger.exception(
                "consensus rerun: failed to persist hash for %s", os.path.basename(song_path)
            )
        info = self._read_metadata_for_lrclib(song_path)
        # Don't re-fetch lrclib here — _upgrade_via_consensus_locked will
        # only include lrclib in its source pool when ``lrclib_lrc`` is
        # passed in. The first run of the pipeline already wrote
        # ``<stem>.lrclib.ass`` (variant); LRCLib's network call is HTTP-
        # cached so we let the consensus engine re-fetch via its own path.
        # ``lyrics_sha`` is derived inside the engine when needed.
        threading.Thread(
            target=self._upgrade_via_consensus,
            args=(song_path, info, None, None),
            name=f"consensus-rerun-{os.path.basename(song_path)}",
            daemon=True,
        ).start()
        logger.info(
            "consensus rerun: dispatched for %s (trigger=%s, hash=%s)",
            os.path.basename(song_path),
            just_landed_source,
            new_hash,
        )

    def _compute_consensus_input_hash(self, song_path: str) -> str:
        """Stable digest of which variant inputs are currently available.

        Includes every on-disk ``<stem>.<source>.ass`` (excluding consensus
        itself, which is the output) keyed by mtime+size, plus a marker
        when a Whisper transcript is cached for this audio sha. The hash
        flips whenever a new fetch lands or Whisper finally transcribes,
        which is the signal to re-run consensus.
        """
        import hashlib

        from pikaraoke.lib.karaoke_database import SUBTITLE_SOURCE_CONSENSUS as _CONSENSUS

        parts: list[str] = []
        for source in VARIANT_FILE_SOURCES:
            if source == _CONSENSUS:
                continue
            path = _variant_ass_path(song_path, source)
            try:
                st = os.stat(path)
            except OSError:
                continue
            parts.append(f"{source}:{int(st.st_mtime)}:{st.st_size}")
        audio_sha = self._audio_sha_for_song(song_path)
        if audio_sha and self._db is not None:
            try:
                model_name = _resolve_whisper_model()
                cached = _read_cached_whisper_transcript(
                    self._db.get_metadata, audio_sha, model_name
                )
                if cached is not None:
                    parts.append(f"whisper:{model_name}")
            except Exception:
                logger.exception(
                    "consensus rerun: whisper cache probe failed for %s",
                    os.path.basename(song_path),
                )
        if not parts:
            return ""
        return hashlib.sha256(":".join(sorted(parts)).encode()).hexdigest()[:16]

    def _write_and_register_variant(
        self,
        song_path: str,
        source: str,
        ass: str,
    ) -> None:
        """Write a per-source variant ASS file and register it (no tier-gate).

        Used by ``fetch_variant`` to land an on-demand source render
        without touching the canonical ``<stem>.ass`` or its tier state.
        Emits ``lyrics_upgraded`` so the splash hot-swap path picks up the
        new variant when the operator's pinned source matches.
        """
        if source not in VARIANT_FILE_SOURCES:
            logger.warning(
                "_write_and_register_variant: refusing to write variant for source=%r",
                source,
            )
            return
        target = _variant_ass_path(song_path, source)
        _write_ass_atomic(song_path, ass, target_path=target)
        if self._db is None:
            return
        song_id = self._db.get_song_id_by_path(song_path)
        if song_id is None:
            return
        self._db.upsert_artifacts(song_id, [{"role": f"ass_{source}", "path": target}])
        # Emit landed BEFORE lyrics_upgraded so pending-pick commit (if any)
        # writes the override before the splash hot-swap reads it.
        try:
            self._events.emit(
                "subtitle_variant_landed",
                {"song_path": song_path, "source": source},
            )
        except Exception:
            logger.exception("failed to emit subtitle_variant_landed for %s/%s", source, song_path)
        try:
            self._events.emit("lyrics_upgraded", song_path)
        except Exception:
            logger.exception("failed to emit lyrics_upgraded for variant %s/%s", source, song_path)
        self._emit_song_event(
            song_path,
            phase="lyrics",
            message="Lyrics variant fetched",
            detail=f"source={source}",
        )
        self._maybe_recompute_consensus(song_path, source)

    def is_fetch_in_flight(self, song_path: str, source: str) -> bool:
        """Public read-only probe for the (song, source) in-flight slot."""
        with self._in_flight_lock:
            return (song_path, source) in self._in_flight_variants

    def claim_fetch_in_flight(self, song_path: str, source: str) -> bool:
        """Atomically reserve the (song, source) in-flight slot.

        Returns ``True`` if the slot was free and is now ours; ``False`` if
        another fetch is already running. Pair with ``release_fetch_in_flight``
        when the work completes (success or failure).
        """
        key = (song_path, source)
        with self._in_flight_lock:
            if key in self._in_flight_variants:
                return False
            self._in_flight_variants.add(key)
        return True

    def release_fetch_in_flight(self, song_path: str, source: str) -> None:
        """Release a slot previously reserved with ``claim_fetch_in_flight``."""
        with self._in_flight_lock:
            self._in_flight_variants.discard((song_path, source))

    def dispatch_variant_fetch(self, song_path: str, source: str) -> bool:
        """Synchronously claim the in-flight slot, then spawn a worker
        thread to render + write the variant.

        This is the safe entry point for HTTP routes: by the time the
        route returns, the in-flight slot is guaranteed claimed, so any
        cache-bust the route emits afterwards (e.g. ``lyrics_upgraded``)
        cannot race the splash's ``GET /subtitle/<id>`` past the worker's
        own ``add(key)``. Without this guarantee the stream route would
        see no in-flight + no file and silently clear the operator's pin.

        Returns ``True`` when a worker was dispatched, ``False`` for
        unknown source / song-gone-from-disk / dedup hit.
        """
        if source not in VARIANT_FILE_SOURCES:
            logger.warning("dispatch_variant_fetch: refusing unknown source=%r", source)
            return False
        if not os.path.exists(song_path):
            logger.info(
                "dispatch_variant_fetch: %s gone from disk; skipping %s",
                os.path.basename(song_path),
                source,
            )
            return False
        if not self.claim_fetch_in_flight(song_path, source):
            logger.info(
                "dispatch_variant_fetch: dedup %s/%s (already in-flight)",
                os.path.basename(song_path),
                source,
            )
            return False

        def _worker() -> None:
            try:
                self._fetch_variant_after_claim(song_path, source)
            finally:
                self.release_fetch_in_flight(song_path, source)

        try:
            threading.Thread(
                target=_worker,
                name=f"variant-fetch-{source}",
                daemon=True,
            ).start()
        except Exception:
            # OS thread limit / fork pressure: release the claim so the
            # slot doesn't stay held forever and block future retries.
            self.release_fetch_in_flight(song_path, source)
            raise
        return True

    def fetch_variant(self, song_path: str, source: str) -> bool:
        """On-demand fetch of one subtitle source variant.

        Dispatches to the matching source helper without re-running the
        full tier pipeline, writes ``<stem>.<source>.ass``, and emits
        ``lyrics_upgraded``. Returns ``True`` when the variant landed,
        ``False`` on miss / dedup / unknown source / song-gone-from-disk.

        ``source`` must be a key from ``VARIANT_FILE_SOURCES``. Concurrent
        calls for the same (song, source) are deduplicated through the
        thread-safe ``_in_flight_variants`` set (CG1). The in-flight slot
        is held until the variant ASS file has actually landed on disk —
        clearing it in a ``finally`` around the render alone leaves a
        race window where a concurrent ``GET /subtitle/<id>`` sees no
        in-flight + no file and clears the operator's pin.
        """
        if source not in VARIANT_FILE_SOURCES:
            logger.warning("fetch_variant: refusing unknown source=%r", source)
            return False
        if not os.path.exists(song_path):
            logger.info(
                "fetch_variant: %s gone from disk; skipping %s",
                os.path.basename(song_path),
                source,
            )
            return False
        if not self.claim_fetch_in_flight(song_path, source):
            logger.info(
                "fetch_variant: dedup %s/%s (already in-flight)",
                os.path.basename(song_path),
                source,
            )
            return False
        try:
            return self._fetch_variant_after_claim(song_path, source)
        finally:
            self.release_fetch_in_flight(song_path, source)

    def fetch_variant_sync(self, song_path: str, source: str) -> dict:
        """Synchronous variant fetch with structured status (Phase 1 entry).

        Used by ``SubtitleOrchestrator`` from its own thread pool — the pool
        owns concurrency and feeds the result into ``subtitle_jobs``. Same
        semantics as ``fetch_variant`` (claim → render → write → release)
        but returns one of:

          ``{"state": "success", "tier": "word"|"line"}``
          ``{"state": "failed",  "error_code": <code>, "error_message": str}``
          ``{"state": "skipped", "error_code": <code>}``

        ``error_code`` is one of: ``unknown_source``, ``song_gone``,
        ``in_flight_dedup``, ``not_found``, ``render_error``, ``write_error``.
        ``not_found`` is the soft-miss path (render returned None — e.g.
        LRCLib has no row, Genius search empty); the orchestrator surfaces
        it as ``failed`` so the chip shows amber rather than silently
        nothing happened.
        """
        if source not in VARIANT_FILE_SOURCES:
            logger.warning("fetch_variant_sync: refusing unknown source=%r", source)
            return {"state": "skipped", "error_code": "unknown_source"}
        if not os.path.exists(song_path):
            return {"state": "skipped", "error_code": "song_gone"}
        if not self.claim_fetch_in_flight(song_path, source):
            return {"state": "skipped", "error_code": "in_flight_dedup"}
        try:
            try:
                ass = self._render_for_variant(song_path, source)
            except Exception as exc:
                logger.exception(
                    "fetch_variant_sync: render crashed for %s/%s",
                    os.path.basename(song_path),
                    source,
                )
                return {
                    "state": "failed",
                    "error_code": "render_error",
                    "error_message": str(exc)[:200],
                }
            if not ass:
                return {"state": "failed", "error_code": "not_found"}
            try:
                self._write_and_register_variant(song_path, source, ass)
            except OSError as exc:
                logger.exception(
                    "fetch_variant_sync: write failed for %s/%s",
                    os.path.basename(song_path),
                    source,
                )
                return {
                    "state": "failed",
                    "error_code": "write_error",
                    "error_message": str(exc)[:200],
                }
            return {"state": "success", "tier": _tier_for_variant_source(source)}
        finally:
            self.release_fetch_in_flight(song_path, source)

    def _fetch_variant_after_claim(self, song_path: str, source: str) -> bool:
        """Render + write a variant assuming the caller already holds the
        in-flight claim. Caller is responsible for releasing it.
        """
        try:
            ass = self._render_for_variant(song_path, source)
        except Exception:
            logger.exception(
                "fetch_variant: render crashed for %s/%s",
                os.path.basename(song_path),
                source,
            )
            try:
                self._events.emit(
                    "song_warning",
                    {
                        "message": "Lyrics fetch failed",
                        "detail": f"Could not fetch {source} for this song.",
                        "song": os.path.basename(song_path),
                        "severity": "warning",
                    },
                )
            except Exception:
                logger.exception("failed to emit song_warning for fetch_variant")
            return False
        if not ass:
            # Soft miss: render returned None (e.g., Genius search empty,
            # LRCLib has no row). Without an event the deferred picker pin
            # set by /subtitle_source stays in `downloading` forever — no
            # subtitle_variant_landed fires to clear it.
            logger.info(
                "fetch_variant: soft miss for %s/%s",
                os.path.basename(song_path),
                source,
            )
            try:
                self._events.emit(
                    "subtitle_variant_miss",
                    {"song_path": song_path, "source": source},
                )
            except Exception:
                logger.exception(
                    "failed to emit subtitle_variant_miss for %s/%s", source, song_path
                )
            return False
        try:
            self._write_and_register_variant(song_path, source, ass)
        except OSError:
            logger.exception(
                "fetch_variant: write failed for %s/%s",
                os.path.basename(song_path),
                source,
            )
            return False
        return True

    def _persisted_variant_blocked_by_score(self, song_path: str, source: str) -> bool:
        """Soft-miss gate keyed on a previously persisted coverage score.

        ``subtitle_jobs.coverage`` is populated by the consensus engine
        whenever a song goes through ``_upgrade_via_consensus_locked``.
        On the next on-demand pick, the variant render path consults the
        same score: when it falls below the per-source threshold (e.g.
        the LRCLib record is mislabel — Polish title, English text), we
        refuse to write the variant rather than letting the operator
        pin a known-bad source. The Kolorowy wiatr case.

        Returns True when the score exists and is below the threshold.
        Missing row, NULL coverage, or no DB → False (let the renderer
        decide as before).
        """
        if self._db is None:
            return False
        score_key = _VARIANT_SCORE_KEY.get(source)
        if score_key is None:
            return False
        try:
            song_id = self._db.get_song_id_by_path(song_path)
        except Exception:
            logger.exception(
                "variant gate: get_song_id_by_path failed for %s",
                os.path.basename(song_path),
            )
            return False
        if song_id is None:
            return False
        try:
            score = self._db.get_subtitle_job_score(song_id, score_key)
        except Exception:
            logger.exception(
                "variant gate: get_subtitle_job_score failed for %s/%s",
                os.path.basename(song_path),
                score_key,
            )
            return False
        if score is None:
            return False
        coverage, order_uncertain = score
        threshold = _coverage_threshold_for(source)
        if coverage < threshold:
            logger.info(
                "variant gate: %s coverage %.2f < %.2f, soft miss for %s",
                source,
                coverage,
                threshold,
                os.path.basename(song_path),
            )
            return True
        if order_uncertain:
            logger.info(
                "variant gate: %s order_uncertain for %s; rendering anyway "
                "(coverage %.2f >= %.2f)",
                source,
                os.path.basename(song_path),
                coverage,
                threshold,
            )
        return False

    def _render_for_variant(self, song_path: str, source: str) -> str | None:
        """Dispatch to the per-source render helper. Pure-ish: returns ASS or None."""
        if self._persisted_variant_blocked_by_score(song_path, source):
            return None
        if source == SUBTITLE_SOURCE_YOUTUBE_VTT:
            return self._render_vtt_ass(song_path)
        if source == SUBTITLE_SOURCE_LRCLIB:
            info = self._read_metadata_for_lrclib(song_path)
            ass, _sha, _info = self._render_lrclib_line_ass(info, song_path=song_path)
            return ass
        if source == SUBTITLE_SOURCE_LRCLIB_SYNC:
            info = self._read_metadata_for_lrclib(song_path)
            lrc, _info = self._fetch_lrc_with_itunes_fallback(info, song_path=song_path)
            if not lrc:
                return None
            return self._render_lrclib_word_ass(song_path, lrc)
        if source == SUBTITLE_SOURCE_GENIUS_SYNC:
            info = self._read_metadata_for_lrclib(song_path)
            if not info:
                return None
            return self._render_genius_word_ass(song_path, info)
        if source == SUBTITLE_SOURCE_TEKSTOWO_SYNC:
            info = self._read_metadata_for_lrclib(song_path)
            if not info:
                return None
            return self._render_tekstowo_word_ass(song_path, info)
        if source == SUBTITLE_SOURCE_SPOTIFY:
            info = self._read_metadata_for_lrclib(song_path)
            if not info:
                return None
            return self._render_spotify_native_ass(song_path, info)
        if source == SUBTITLE_SOURCE_SPOTIFY_SYNC:
            info = self._read_metadata_for_lrclib(song_path)
            if not info:
                return None
            return self._render_spotify_word_ass(song_path, info)
        if source == SUBTITLE_SOURCE_AI:
            return self._render_whisper_word_ass(song_path)
        return None

    def _register_user_ass(self, song_path: str) -> None:
        from pikaraoke.lib.karaoke_database import LYRICS_PROVENANCE_USER

        if self._db is None:
            return
        song_id = self._db.get_song_id_by_path(song_path)
        if song_id is None:
            return
        self._db.upsert_artifacts(song_id, [{"role": "ass_user", "path": _ass_path(song_path)}])
        # Tag the row so the UI badge distinguishes user-authored subtitles
        # from auto-generated ones.
        try:
            self._db.update_processing_config(
                song_id,
                lyrics_source=SUBTITLE_SOURCE_USER,
                aligner_model=None,
                lyrics_sha=None,
                lyrics_provenance=LYRICS_PROVENANCE_USER,
            )
        except Exception:
            logger.exception("failed to stamp user lyrics_source for %s", song_path)
        try:
            self._events.emit("lyrics_upgraded", song_path)
        except Exception:
            logger.exception("failed to emit lyrics_upgraded for %s", song_path)

    def _emit_stage_notification(self, song_path: str, stage: str) -> None:
        """Toast a pipeline-stage message (e.g. "Fetching lyrics: Song Title").

        Swallows emit exceptions so a missing/misconfigured event bus never
        breaks the stage it was meant to announce.
        """
        if self._events is None:
            return
        try:
            self._events.emit("notification", f"{stage}: {_title_from_filename(song_path)}")
        except Exception:
            logger.exception("failed to emit %s stage notification", stage)

    def _emit_song_event(
        self,
        song_path: str,
        *,
        phase: str,
        message: str,
        detail: str = "",
        severity: str = "info",
    ) -> None:
        """Push a milestone into the per-song timeline (best-effort)."""
        if self._events is None:
            return
        try:
            self._events.emit(
                "song_event",
                {
                    "phase": phase,
                    "message": message,
                    "detail": detail,
                    "severity": severity,
                    "song": os.path.basename(song_path),
                },
            )
        except Exception:
            logger.exception("failed to emit song_event %s/%s", phase, message)

    def _maybe_drop_stale_auto_ass(self, song_path: str, lyrics_sha: str | None) -> None:
        """Delete the auto .ass when any upstream dependency changed.

        Invalidates on: audio sha change (US-15 — source bytes replaced),
        aligner model swap, demucs model swap (whisper aligned to stems from
        the old model), or LRC content change (LRCLib updated the lyrics).
        Runs before re-generating lyrics so stale artifacts are not served.
        """
        if self._db is None:
            return
        song_id = self._db.get_song_id_by_path(song_path)
        if song_id is None:
            return
        from pikaraoke.lib.audio_fingerprint import (
            ensure_audio_fingerprint,
            ensure_lyrics_config,
        )
        from pikaraoke.lib.demucs_processor import DEMUCS_MODEL, resolve_audio_source

        # Audio sha check first — a re-downloaded source invalidates
        # everything downstream (stems + auto .ass via invalidate_auto_ass).
        # Cheap when mtime+size match the DB.
        try:
            ensure_audio_fingerprint(self._db, song_id, resolve_audio_source(song_path))
        except Exception:
            logger.exception("ensure_audio_fingerprint failed for %s", song_path)

        aligner_id = self._aligner.model_id if self._aligner is not None else None
        ensure_lyrics_config(
            self._db,
            song_id,
            current_aligner_model=aligner_id,
            current_demucs_model=DEMUCS_MODEL,
            current_lyrics_sha=lyrics_sha,
        )

    def invalidate_for_metadata_change(self, song_path: str) -> None:
        """Drop every cached subtitle artifact + job state for a song.

        A manual artist/title/language edit changes the inputs to LRCLib,
        Genius, Spotify and Tekstowo lookups, so every prior variant is a
        stale answer to a different question. Deleting the on-disk .ass
        files lets ``SubtitleOrchestrator.kickoff`` re-queue each source
        (its cache-hit branch checks for the variant file), and clearing
        ``subtitle_jobs`` resets the failure-backoff gate so previously
        failed sources retry immediately.
        """
        ass_files = [_ass_path(song_path)]
        ass_files.extend(variant_ass_path(song_path, src) for src in VARIANT_FILE_SOURCES)
        for path in ass_files:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception("invalidate_for_metadata_change: unlink failed for %s", path)
        if self._db is None:
            return
        try:
            song_id = self._db.get_song_id_by_path(song_path)
        except Exception:
            logger.exception(
                "invalidate_for_metadata_change: get_song_id_by_path failed for %s", song_path
            )
            return
        if song_id is None:
            return
        try:
            self._db.delete_subtitle_jobs(song_id)
        except Exception:
            logger.exception(
                "invalidate_for_metadata_change: delete_subtitle_jobs failed for %s", song_path
            )

    def fetch_and_convert(self, song_path: str) -> None:
        """Entry point - event listener for `song_downloaded`."""
        try:
            self._do_fetch_and_convert(song_path)
        except Exception:
            logger.exception("Unexpected error fetching lyrics for %s", song_path)

    def _do_fetch_and_convert(self, song_path: str) -> None:
        """Progressive lyrics pipeline.

        Fan out YouTube VTT in parallel with the LRCLib fetch so a
        line-level .ass lands within milliseconds when captions were
        downloaded with the song — the splash renders T1 subs while LRC
        is still in flight. A later LRC hit upgrades to T2 (synced LRC)
        and wav2vec2 alignment upgrades to T3 (per-word). Each write
        goes through ``_try_write_ass_tiered`` so a slower, lower-tier
        write can never clobber a higher-tier .ass that already landed.

        LRC fetch stays on the main thread so ``_maybe_drop_stale_auto_ass``
        can compare ``lyrics_sha`` before the cache-hit check (preserving
        LRC-content-change invalidation from US-31).
        """
        basename = os.path.basename(song_path)
        logger.info("lyrics pipeline: starting for %s", basename)
        # User-supplied Aegisub files (without the auto-lyrics marker) are sacred.
        if _user_owned_ass(song_path):
            logger.info(
                "lyrics pipeline: %s -> user-supplied .ass (skipping auto pipeline)",
                basename,
            )
            self._register_user_ass(song_path)
            _cleanup_yt_vtt(song_path, self._db)
            return

        info = self._read_metadata_for_lrclib(song_path)
        if info:
            logger.info(
                "lyrics pipeline: %s metadata track=%r artist=%r duration=%s",
                basename,
                info.get("track"),
                info.get("artist"),
                info.get("duration"),
            )
        else:
            logger.info(
                "lyrics pipeline: %s has no usable artist/title — LRCLib query will be skipped",
                basename,
            )

        # Tier 1 classifier (US-43): seed songs.language from every text
        # signal we already have in hand (yt-dlp info.json, cached iTunes
        # hit, cached MusicBrainz recording, langdetect on DB fields). Each
        # signal persists under its own rung in the provenance ladder, so a
        # stronger later source overwrites a weaker one and LRCLib's
        # ``lrc_heuristic`` (lowest rung) can never overwrite anything the
        # classifier seeded. Runs BEFORE the LRC fetch so
        # ``_is_lrc_language_mismatch`` has DB-side ground truth to compare
        # against on cold-DB first runs (the Kolorowy wiatr poison path).
        self._run_language_classifier(song_path, info)

        # If a previous session already ran Tier-2b (``whisper_probe_stems``)
        # for this audio sha, apply its cached verdict NOW — before the
        # LRCLib round-trip. Otherwise Tier-1 consensus may have just
        # written a wrong language (the Kolorowy wiatr text-signals-all-
        # lying case), we'd fetch LRC in the wrong language and then
        # discard it via the dub guard. Reading the cache is free; the
        # expensive probe itself has already been paid for.
        self._apply_cached_stems_probe(song_path)

        # Reset tier state for this run (handles replays after cache
        # invalidation) and seed to WORD if a cached word-level .ass
        # exists — the VTT/LRC workers below use the tier gate so they
        # can't clobber a valid cache while the main thread is still
        # verifying LRC-sha invalidation.
        self._reset_tier(song_path)
        if _is_word_level_auto_ass(song_path):
            with self._tier_lock:
                self._tier_state[song_path] = _TIER_WORD

        # Start the parallel background workers:
        #   - VTT worker: writes T1 line-level at ~100ms when captions
        #     were downloaded with the song.
        #   - wav2vec2 preload: loads the Polish/English/etc. align model
        #     in parallel with Demucs, saving ~13s of cold-start when
        #     alignment runs.
        #   - Demucs prewarm: only when something downstream will use
        #     stems (the aligner for word-level, or Whisper ASR for the
        #     no-source fallback). Already idempotent via download_manager
        #     for fresh downloads; we call here too so scanner-imported
        #     songs prewarm too.
        vtt_thread = Thread(
            target=self._worker_vtt,
            args=(song_path,),
            name=f"lyrics-vtt-{basename}",
            daemon=True,
        )
        vtt_thread.start()
        self._warmup_aligner_async(song_path)
        if self._aligner is not None or _whisper_fallback_enabled():
            _prewarm_stems(song_path)

        # Tell the operator we're about to hit LRCLib / iTunes. Emitted
        # BEFORE the network call so the "Fetching lyrics…" toast lands
        # while the HTTP round-trip is in flight.
        self._emit_stage_notification(song_path, "Fetching lyrics")

        # Fetch LRC up front so we can fingerprint it BEFORE deciding whether
        # the cached .ass is still valid. Subtitle changes (LRCLib updated the
        # lyrics for this song) must force a whisper re-run even if the audio
        # and models haven't moved.
        lrc, info = self._fetch_lrc_with_itunes_fallback(info, song_path=song_path)
        if lrc and self._is_lrc_language_mismatch(song_path, lrc):
            # Dub-trap: LRCLib indexes by canonical song name, so a Polish
            # dub of an English original gets the English lyrics (the
            # Pocahontas "Kolorowy wiatr" case). When the DB already knows
            # the audio language, reject the LRC and fall through to the
            # other sources — Whisper ASR on the vocals stem produces
            # matching-language subs, VTT might carry the dub captions.
            lrc = None
        lyrics_sha = _lrc_sha(lrc) if lrc else None

        self._maybe_drop_stale_auto_ass(song_path, lyrics_sha)

        # Cache hit: word-level .ass survived every invalidation trigger
        # (aligner/demucs models + LRC content). If the invalidation
        # above deleted the file, tier state still says WORD — lower it
        # so subsequent writes can land.
        if _is_word_level_auto_ass(song_path):
            logger.info(
                "lyrics pipeline: %s -> word-level .ass cache hit (no work)",
                basename,
            )
            _cleanup_yt_vtt(song_path, self._db)
            # Drain VTT worker so it doesn't race a later replay.
            vtt_thread.join(timeout=5)
            return
        if self._current_tier(song_path) == _TIER_WORD:
            # Cached .ass was just invalidated — reset so the live writes land.
            self._reset_tier(song_path)

        # Write T2 LINE_LRC if LRC hit (upgrades any T1 VTT already on disk).
        if lrc:
            line_ass = _lrc_to_ass_line_level(lrc)
            if line_ass:
                wrote = self._try_write_ass_tiered(
                    song_path,
                    _TIER_LINE_LRC,
                    line_ass,
                    lyrics_source=SUBTITLE_SOURCE_LRCLIB,
                    aligner_model=None,
                    lyrics_sha=lyrics_sha,
                )
                if wrote:
                    logger.info(
                        "LRCLib: wrote line-level .ass for %s - %s",
                        info["artist"] if info else "?",
                        info["track"] if info else "?",
                    )

        # Alignment / Genius workers. Two paths:
        # - Consensus path (LYRICS_CONSENSUS_ENABLED=1): one thread fans out
        #   MXM/Megalobiz/Genius/Whisper in parallel, votes a token-level
        #   consensus against the VTT/Whisper audio reference, runs the
        #   aligner once, writes T3.
        # - Legacy path (default): LRC > Genius. When LRC hit, align on it;
        #   otherwise Genius is the alignment source (text-only,
        #   whole-song align).
        align_thread: Thread | None = None
        if _consensus_enabled() and self._aligner is not None:
            align_thread = Thread(
                target=self._upgrade_via_consensus,
                args=(song_path, info, lrc, lyrics_sha),
                name=f"lyrics-consensus-{basename}",
                daemon=True,
            )
            align_thread.start()
        elif lrc and self._aligner is not None:
            align_thread = Thread(
                target=self._upgrade_to_word_level,
                args=(song_path, lrc, lyrics_sha),
                name=f"lyrics-align-{basename}",
                daemon=True,
            )
            align_thread.start()
        elif self._aligner is not None and GENIUS_ACCESS_TOKEN and info:
            align_thread = Thread(
                target=self._try_genius_fallback,
                args=(song_path, info),
                name=f"lyrics-genius-{basename}",
                daemon=True,
            )
            align_thread.start()

        # Wait for background workers. VTT is fast (<100ms); alignment
        # can take up to ~150s (stems wait 120 + whisperx 20-30s).
        vtt_thread.join(timeout=180)
        if align_thread is not None:
            align_thread.join(timeout=180)

        final_tier = self._current_tier(song_path)
        if final_tier == _TIER_NONE:
            if _whisper_fallback_enabled():
                logger.info(
                    "lyrics pipeline: %s -> no LRC/Genius/VTT source; queuing Whisper ASR fallback",
                    basename,
                )
                # Fire-and-forget: ASR is slow (~1x realtime on CPU) and
                # must not block the download pipeline. If it succeeds the
                # .ass lands mid-song and `lyrics_upgraded` flips the UI;
                # if it fails we surface a song_warning from inside.
                Thread(
                    target=self._try_whisper_fallback,
                    args=(song_path,),
                    name=f"whisper-fallback-{basename}",
                    daemon=True,
                ).start()
            else:
                logger.info(
                    "lyrics pipeline: %s -> no LRC/Genius/VTT source; Whisper fallback disabled",
                    basename,
                )
                try:
                    self._events.emit(
                        "song_warning",
                        {
                            "message": "No lyrics found",
                            "detail": "LRCLib / Genius / YouTube captions all missed, and Whisper fallback is disabled.",
                            "song": basename,
                            "severity": "warning",
                        },
                    )
                except Exception:
                    logger.exception("failed to emit song_warning for missing lyrics")
                # Also persist a failure marker so the integrity scan doesn't
                # re-run the LRCLib/Genius/VTT round on every restart for songs
                # that never had any chance of succeeding.
                audio_sha = self._audio_sha_for_song(song_path)
                if audio_sha and self._db is not None:
                    _record_pipeline_failure(
                        self._db.get_metadata,
                        self._db.set_metadata,
                        audio_sha,
                        error_code="whisper_disabled_no_lyrics",
                    )
            return

        # VTT cleanup is conditional: only drop YouTube's raw captions once
        # we have our own .ass. When every source failed we already
        # returned above, so reaching here means at least one tier wrote.
        _cleanup_yt_vtt(song_path, self._db)

        self._events.emit(
            "notification",
            f"Lyrics ready: {_title_from_filename(song_path)}",
            "info",
        )
        logger.info(
            "lyrics pipeline: %s -> final_tier=%s db_lang=%s",
            basename,
            _TIER_NAMES[final_tier],
            self._db_language(song_path),
        )

    def _render_lrclib_line_ass(
        self, info: dict | None, song_path: str | None = None
    ) -> tuple[str | None, str | None, dict | None]:
        """Fetch LRCLib (across candidate credits) and render line-level ASS.

        Returns ``(ass, lyrics_sha, info)``. ``info`` is returned with
        the winning artist/track when an alternative candidate rescued
        the fetch, mirroring ``_fetch_lrc_with_itunes_fallback``.
        """
        lrc, info = self._fetch_lrc_with_itunes_fallback(info, song_path=song_path)
        if not lrc:
            return None, None, info
        ass = _lrc_to_ass_line_level(lrc)
        if not ass:
            return None, None, info
        return ass, _lrc_sha(lrc), info

    def _render_lrclib_word_ass(self, song_path: str, lrc: str) -> str | None:
        """Align an LRC against vocals via wav2vec2 and render word-level ASS.

        Pure-ish: no .ass write, no tier-gate touch, no
        ``lyrics_upgraded`` emit. Used by both the variant fetch path and
        ``_upgrade_to_word_level`` (via the latter's existing tier-gate
        write). Returns ``None`` when the aligner is missing, language
        cannot be determined, or alignment yields no words.
        """
        if self._aligner is None:
            return None
        try:
            audio_path = _wait_for_alignment_audio(song_path)
            song_id = self._db.get_song_id_by_path(song_path) if self._db else None
            db_lang = None
            if self._db is not None and song_id is not None:
                row = self._db.get_song_by_id(song_id)
                db_lang = row["language"] if row is not None else None
            language = db_lang or _detect_language(_lrc_plain_text(lrc))
            if not language:
                return None
            words = self._aligner.align(
                audio_path,
                _lrc_plain_text(lrc),
                lrc_lines=lrc_line_windows(lrc),
                language=language,
                vad_cache=self._vad_cache_for_song(song_path),
            )
            if not words:
                return None
            line_starts = getattr(self._aligner, "last_line_starts", None)
            if isinstance(line_starts, dict) and line_starts:
                render_lrc = _shift_lrc_per_line(lrc, line_starts)
            else:
                render_lrc = lrc
            bpm = self._cached_estimate_bpm(song_path, audio_path)
            return _words_to_ass_with_k_tags(words, render_lrc, params=_anim_params_for_bpm(bpm))
        except Exception:
            logger.warning(
                "_render_lrclib_word_ass: alignment failed for %s", song_path, exc_info=True
            )
            return None

    def _render_genius_word_ass(self, song_path: str, info: dict) -> str | None:
        """Genius lyrics + wav2vec2 → word-level ASS. Returns ASS string or None.

        Iterates through ``_metadata_candidates`` so a Genius miss on the
        DB credit retries with token-split / iTunes / MusicBrainz
        alternatives before giving up. Language-mismatch hits count as
        misses so the loop keeps trying.
        """
        if self._aligner is None:
            return None
        if not info.get("track") or not info.get("artist"):
            return None

        def _fetch(cand: dict) -> str | None:
            text = _fetch_genius(cand["track"], cand["artist"])
            if not text:
                return None
            if self._is_genius_language_mismatch(song_path, text):
                return None
            return text

        genius_text, _winner = self._try_with_candidates(_fetch, info, song_path, label="Genius")
        if not genius_text:
            return None
        return self._align_plain_text_to_ass(song_path, genius_text, source_label="Genius")

    def _align_plain_text_to_ass(
        self, song_path: str, plain_text: str, source_label: str
    ) -> str | None:
        """Word-align plain lyrics against vocals and render word-level ASS.

        Shared back-end for any source whose upstream returns timestamp-free
        text (Genius, tekstowo.pl). The aligner gives us per-token timings;
        ``_lrc_from_aligned_lines`` rebuilds a synthetic LRC by consuming
        one line's worth of tokens at a time and stamping each line's first
        word as the line's LRC time. Returns None on missing aligner,
        unknown language, alignment failure, or empty word output.

        ``source_label`` shows up in the warning log only — it doesn't
        change behaviour, so callers can pass any human-readable name.
        """
        if self._aligner is None:
            return None
        try:
            _prewarm_stems(song_path)
            audio_path = _wait_for_alignment_audio(song_path)
            lines = [ln for ln in plain_text.splitlines() if ln.strip()]
            if not lines:
                return None
            plain = "\n".join(lines)
            song_id = self._db.get_song_id_by_path(song_path) if self._db else None
            db_lang = None
            if self._db is not None and song_id is not None:
                row = self._db.get_song_by_id(song_id)
                db_lang = row["language"] if row is not None else None
            language = db_lang or _detect_language(plain)
            if not language:
                return None
            words = self._aligner.align(audio_path, plain, language=language)
            if not words:
                return None
            synthetic_lrc = _lrc_from_aligned_lines(words, lines)
            if not synthetic_lrc:
                return None
            bpm = self._cached_estimate_bpm(song_path, audio_path)
            return _words_to_ass_with_k_tags(words, synthetic_lrc, params=_anim_params_for_bpm(bpm))
        except Exception:
            logger.warning(
                "_align_plain_text_to_ass: %s alignment failed for %s",
                source_label,
                song_path,
                exc_info=True,
            )
            return None

    def _render_tekstowo_word_ass(self, song_path: str, info: dict) -> str | None:
        """Tekstowo.pl lyrics + wav2vec2 → word-level ASS. None on miss.

        Iterates through ``_metadata_candidates`` so a Tekstowo miss on
        the DB credit retries with the alternative-credits list.
        """
        if self._aligner is None:
            return None
        if not info.get("track") or not info.get("artist"):
            return None

        def _fetch(cand: dict) -> str | None:
            text = _fetch_tekstowo(cand["track"], cand["artist"])
            if not text:
                return None
            if self._is_lyrics_language_mismatch(song_path, text, source_label="Tekstowo"):
                return None
            return text

        text, _winner = self._try_with_candidates(_fetch, info, song_path, label="Tekstowo")
        if not text:
            return None
        return self._align_plain_text_to_ass(song_path, text, source_label="Tekstowo")

    def _render_spotify_native_ass(self, song_path: str, info: dict) -> str | None:
        """Spotify Color Lyrics → ASS using Spotify's own timings. No aligner.

        Picks the highest-precision render Spotify itself ships:
          * ``SYLLABLE_SYNCED`` → word-level karaoke ASS with per-syllable
            ``\\kf`` parts. Rare on Spotify (mostly Apple Music feed) but
            preferred when present.
          * ``LINE_SYNCED`` → line-level ASS, same shape as the
            ``lrclib`` source.
          * ``UNSYNCED`` or no payload → ``None``.

        Iterates through ``_metadata_candidates`` so a Spotify miss on
        the DB credit retries with the alternative-credits list.
        Companion to the wav2vec2-driven ``spotify-sync`` variant: this
        one is faster and aligner-free, but lower granularity for the
        99% of tracks Spotify returns line-level.
        """
        if not info.get("track") or not info.get("artist"):
            return None

        def _fetch(cand: dict) -> dict | None:
            return self._fetch_spotify_lyrics_payload(
                cand["track"], cand["artist"], isrc=cand.get("isrc")
            )

        lyrics_block, _winner = self._try_with_candidates(
            _fetch, info, song_path, label="Spotify-native"
        )
        if not lyrics_block:
            return None
        sync_type = lyrics_block.get("syncType")
        lines = lyrics_block.get("lines") or []
        if not lines:
            return None

        if sync_type == "SYLLABLE_SYNCED":
            ass = _spotify_syllable_lines_to_ass(lines)
            if ass:
                return ass
            # Heuristic mapping failed (syllables don't reconstruct words);
            # fall through to line-level so the operator still gets subtitles.
        if sync_type in ("LINE_SYNCED", "SYLLABLE_SYNCED"):
            lrc = _spotify_lines_to_lrc(lines)
            if not lrc:
                return None
            if self._is_lyrics_language_mismatch(
                song_path, _lrc_plain_text(lrc), source_label="Spotify"
            ):
                return None
            return _lrc_to_ass_line_level(lrc)
        return None

    def _render_spotify_word_ass(self, song_path: str, info: dict) -> str | None:
        """Spotify Color Lyrics LRC + wav2vec2 → word-level ASS. None on miss.

        Spotify returns line-synced timing (LRC equivalent), so we route
        through the existing LRC-windowed aligner pipeline used by LRCLib.
        ISRC from the songs table gives us a deterministic Spotify track
        match when available; otherwise we fall back to artist+title
        search and iterate ``_metadata_candidates`` on miss.
        """
        if self._aligner is None:
            return None
        if not info.get("track") or not info.get("artist"):
            return None

        def _fetch(cand: dict) -> str | None:
            lrc = self._fetch_spotify_lrc(cand["track"], cand["artist"], isrc=cand.get("isrc"))
            if not lrc:
                return None
            if self._is_lyrics_language_mismatch(
                song_path, _lrc_plain_text(lrc), source_label="Spotify"
            ):
                return None
            return lrc

        lrc, _winner = self._try_with_candidates(_fetch, info, song_path, label="Spotify-sync")
        if not lrc:
            return None
        return self._render_lrclib_word_ass(song_path, lrc)

    # --- Spotify Color Lyrics auth + fetch -------------------------------

    def _get_spotify_sp_dc(self) -> str:
        """Read sp_dc cookie from the live preference store. Empty when unset."""
        if self._preferences is None:
            return ""
        try:
            return (self._preferences.get_or_default("spotify_sp_dc") or "").strip()
        except Exception:
            logger.exception("preference read failed for spotify_sp_dc")
            return ""

    def _get_spotify_access_token(self) -> str | None:
        """Mint or reuse a short-lived Spotify access token.

        Cached on the LyricsService for the token's full lifetime minus a
        30s safety window. Returns None on missing cookie, network failure,
        anonymous token (cookie expired or wrong account), or unavailable
        TOTP secret. ``open.spotify.com/api/token`` requires a TOTP code
        derived from a rotating secret published by xyloflake/spot-secrets-go.
        """
        from pikaraoke.lib.lyrics_spotify_totp import SpotifyTOTP, SpotifyTOTPError

        sp_dc = self._get_spotify_sp_dc()
        if not sp_dc:
            return None
        with self._spotify_token_lock:
            cache = self._spotify_token_cache
            if cache is not None and cache[1] > time.time() + 30:
                return cache[0]
            try:
                st = requests.get(
                    SPOTIFY_SERVER_TIME_URL,
                    headers={
                        "User-Agent": SPOTIFY_USER_AGENT,
                        "Accept": "application/json",
                    },
                    cookies={"sp_dc": sp_dc},
                    timeout=SPOTIFY_TIMEOUT,
                )
                if st.status_code != 200:
                    logger.warning("Spotify server-time HTTP %s", st.status_code)
                    return None
                server_time_s = st.json()["serverTime"]
            except (requests.RequestException, ValueError, KeyError, TypeError) as e:
                logger.warning("Spotify server-time fetch failed: %s", e)
                return None
            server_time_ms = int(server_time_s) * 1000

            try:
                totp_code, totp_ver = SpotifyTOTP.singleton().generate(server_time_ms)
            except SpotifyTOTPError as e:
                logger.warning("Spotify TOTP unavailable: %s", e)
                return None

            try:
                r = requests.get(
                    SPOTIFY_TOKEN_URL,
                    params={
                        "reason": "init",
                        "productType": "web-player",
                        "totp": totp_code,
                        "totpVer": str(totp_ver),
                        "ts": str(server_time_ms),
                    },
                    headers={
                        "User-Agent": SPOTIFY_USER_AGENT,
                        "Accept": "application/json",
                    },
                    cookies={"sp_dc": sp_dc},
                    timeout=SPOTIFY_TIMEOUT,
                )
                if r.status_code != 200:
                    logger.warning("Spotify token fetch HTTP %s", r.status_code)
                    return None
                payload = r.json()
            except (requests.RequestException, ValueError) as e:
                logger.warning("Spotify token fetch failed: %s", e)
                return None
            if payload.get("isAnonymous"):
                logger.warning("Spotify token is anonymous — sp_dc cookie expired or invalid")
                return None
            token = payload.get("accessToken")
            exp_ms = payload.get("accessTokenExpirationTimestampMs")
            if not token or not exp_ms:
                return None
            self._spotify_token_cache = (token, float(exp_ms) / 1000.0)
            return token

    def _resolve_spotify_track_id(self, track: str, artist: str, isrc: str | None) -> str | None:
        """Search Spotify for a matching track and return its ID, or None.

        Memoized per ``(track, artist, isrc)`` key — repeated requests for
        the same song hit cache instead of the rate-limited search API.
        On 429 the caller sleeps the Retry-After window and retries once
        in-place rather than failing — Spotify's search throttle is short-
        lived (seconds) and bubbling the failure up turns into a multi-day
        orchestrator backoff. Concurrent callers see the global cooldown
        gate and fast-fail so only one thread eats the sleep.
        """
        cache_key = (track, artist, isrc or "")
        if cache_key in self._spotify_search_cache:
            return self._spotify_search_cache[cache_key]
        if time.time() < self._spotify_rate_limited_until:
            return None

        token = self._get_spotify_access_token()
        if not token:
            return None

        if isrc:
            query = f"isrc:{isrc}"
        else:
            query = f'track:"{track}" artist:"{artist}"'

        def _do_search() -> requests.Response:
            return requests.get(
                SPOTIFY_SEARCH_URL,
                params={"q": query, "type": "track", "limit": 5},
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": SPOTIFY_USER_AGENT,
                },
                timeout=SPOTIFY_TIMEOUT,
            )

        def _retry_after(resp: requests.Response) -> float:
            try:
                return float(resp.headers.get("Retry-After", "60"))
            except ValueError:
                return 60.0

        try:
            r = _do_search()
            if r.status_code == 429:
                retry_after = _retry_after(r)
                if retry_after > SPOTIFY_RATE_LIMIT_LONG_S:
                    # Spotify is plainly locked out for the day. Don't
                    # sleep any of it — set the cooldown gate so future
                    # calls fast-fail and return immediately.
                    self._spotify_rate_limited_until = time.time() + retry_after
                    logger.warning(
                        "Spotify search locked out for %.0fs; deferring without sleep",
                        retry_after,
                    )
                    return None
                # Cap the in-thread sleep so a pathological Retry-After
                # can't park a worker for hours; if Spotify really wants
                # more, the second 429 below punts to orchestrator backoff.
                cooldown = min(retry_after, SPOTIFY_RATE_LIMIT_SLEEP_CAP)
                self._spotify_rate_limited_until = time.time() + cooldown
                logger.warning("Spotify search rate-limited; sleeping %.0fs and retrying", cooldown)
                time.sleep(cooldown)
                r = _do_search()
                if r.status_code == 429:
                    cooldown2 = _retry_after(r)
                    self._spotify_rate_limited_until = time.time() + cooldown2
                    logger.warning(
                        "Spotify search still rate-limited after retry; giving up for %.0fs",
                        cooldown2,
                    )
                    return None
            if r.status_code != 200:
                logger.warning("Spotify search HTTP %s for %r", r.status_code, query)
                return None
            items = (r.json().get("tracks") or {}).get("items") or []
        except (requests.RequestException, ValueError) as e:
            logger.warning("Spotify search failed: %s", e)
            return None

        track_id: str | None = None
        if items:
            if isrc:
                track_id = items[0].get("id")
            else:
                for item in items:
                    artists = item.get("artists") or []
                    primary = (artists[0] if artists else {}).get("name", "")
                    others = [a.get("name", "") for a in artists[1:]]
                    if _artist_matches(artist, primary, others):
                        track_id = item.get("id")
                        break
        # Cache the result (including misses) to avoid retrying the rate-
        # limited search endpoint for songs Spotify simply doesn't index.
        self._spotify_search_cache[cache_key] = track_id
        return track_id

    def _fetch_spotify_lyrics_payload(
        self, track: str, artist: str, isrc: str | None = None
    ) -> dict | None:
        """Return the raw ``lyrics`` block from Spotify Color Lyrics, or None.

        Caller dispatches on ``syncType`` (``LINE_SYNCED`` or
        ``SYLLABLE_SYNCED``). ``UNSYNCED`` flows back unchanged so the
        caller can decide whether to drop it.
        """
        token = self._get_spotify_access_token()
        if not token:
            return None
        track_id = self._resolve_spotify_track_id(track, artist, isrc)
        if not track_id:
            return None

        try:
            lr = requests.get(
                SPOTIFY_LYRICS_URL.format(track_id=track_id),
                headers={
                    "Authorization": f"Bearer {token}",
                    "App-Platform": "WebPlayer",
                    "User-Agent": SPOTIFY_USER_AGENT,
                },
                timeout=SPOTIFY_TIMEOUT,
            )
            if lr.status_code == 404:
                return None
            if lr.status_code != 200:
                logger.warning(
                    "Spotify lyrics HTTP %s for track %s (Premium required for free accounts)",
                    lr.status_code,
                    track_id,
                )
                return None
            payload = lr.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("Spotify lyrics fetch failed: %s", e)
            return None
        return payload.get("lyrics") or None

    def _fetch_spotify_lrc(self, track: str, artist: str, isrc: str | None = None) -> str | None:
        """LINE_SYNCED Spotify lyrics as LRC text, or None on miss.

        Used by the wav2vec2-aligned ``spotify-sync`` variant.
        ``UNSYNCED`` and ``SYLLABLE_SYNCED`` are dropped here — the
        former has no timing info, the latter is consumed natively
        by ``_render_spotify_native_ass`` instead.
        """
        lyrics_block = self._fetch_spotify_lyrics_payload(track, artist, isrc=isrc)
        if not lyrics_block or lyrics_block.get("syncType") != "LINE_SYNCED":
            return None
        return _spotify_lines_to_lrc(lyrics_block.get("lines") or [])

    def _render_whisper_word_ass(self, song_path: str) -> str | None:
        """Whisper ASR → word-level ASS. Returns ASS string or None.

        Variant-fetch entry point (``fetch_variant_sync(song_path, "AI")``).
        Distinct from the canonical ``_try_whisper_fallback`` — this one
        renders ``<stem>.AI.ass`` (the operator-pinnable variant), the
        other writes ``<stem>.ass`` (the canonical auto file). Both share
        the ``(audio_sha256, model)`` transcript cache so when one path
        already paid the transcribe cost, the other reads it back.
        """
        try:
            _prewarm_stems(song_path)
            audio_path = _wait_for_alignment_audio(song_path)
            model_name = _resolve_whisper_model()
            audio_sha = self._audio_sha_for_song(song_path)

            cached = None
            if audio_sha and self._db is not None:
                cached = _read_cached_whisper_transcript(
                    self._db.get_metadata, audio_sha, model_name
                )
            if cached is not None:
                logger.info(
                    "whisper_transcript: cache hit (variant) sha=%s model=%s",
                    audio_sha[:12],
                    model_name,
                )
                words = list(cached.words)
                lrc = cached.lrc
            else:
                model = _get_whisper_model()
                if model is None:
                    return None
                segments_iter, info = model.transcribe(
                    audio_path, word_timestamps=True, vad_filter=True
                )
                segments = list(segments_iter)
                if not segments:
                    return None
                lrc = _lrc_from_whisper_segments(segments)
                lang = getattr(info, "language", None)
                words = []
                for seg in segments:
                    for w in seg.words or []:
                        text = (getattr(w, "word", "") or "").strip()
                        if not text or w.start is None or w.end is None:
                            continue
                        start = float(w.start)
                        end = float(w.end)
                        parts = _syllable_parts(text, lang, start, end)
                        words.append(Word(text=text, start=start, end=end, parts=parts))
                if not words or not lrc:
                    return None
                if audio_sha and self._db is not None:
                    _write_cached_whisper_transcript(
                        self._db.set_metadata,
                        audio_sha,
                        model_name,
                        language=lang,
                        lrc=lrc,
                        words=words,
                    )
            bpm = self._cached_estimate_bpm(song_path, audio_path)
            return _words_to_ass_with_k_tags(words, lrc, params=_anim_params_for_bpm(bpm))
        except Exception:
            logger.exception("_render_whisper_word_ass: failed for %s", song_path)
            return None

    def _render_vtt_ass(self, song_path: str) -> str | None:
        """Render a line-level ASS from the song's VTT captions, if any.

        Pure-ish: returns the rendered ASS string (or None on miss). Persists
        the inferred VTT language as a side effect — language detection is
        free here (filename has the lang code) and feeds wav2vec2 model
        selection downstream.
        """
        vtt_path = _pick_best_vtt(song_path, preferred_lang=self._db_language(song_path))
        if not vtt_path:
            return None
        try:
            with open(vtt_path, encoding="utf-8") as f:
                vtt = f.read()
        except OSError as e:
            logger.warning("failed to read %s: %s", vtt_path, e)
            return None
        ass = _vtt_to_ass(vtt)
        if not ass:
            return None
        self._persist_vtt_language(song_path, vtt_path)
        return ass

    def _worker_vtt(self, song_path: str) -> None:
        """Write a line-level .ass from the downloaded VTT captions, if any.

        Runs in parallel with the main thread's LRCLib fetch so the
        splash gets subtitles at ~100ms when captions exist, instead of
        waiting on a 5-20s network round-trip. Tier-gated at
        ``_TIER_LINE_VTT`` so a later LRC (T2) or aligned (T3) write
        upgrades the .ass cleanly.
        """
        try:
            ass = self._render_vtt_ass(song_path)
            if not ass:
                return
            wrote = self._try_write_ass_tiered(
                song_path,
                _TIER_LINE_VTT,
                ass,
                lyrics_source=SUBTITLE_SOURCE_YOUTUBE_VTT,
                aligner_model=None,
                lyrics_sha=None,
            )
            if wrote:
                logger.info(
                    "Wrote .ass from YouTube VTT for %s",
                    os.path.basename(song_path),
                )
        except Exception:
            logger.exception("VTT worker crashed for %s", song_path)

    def _warmup_aligner_async(self, song_path: str) -> None:
        """Preload the wav2vec2 model in parallel with Demucs + LRC fetch.

        Language is pulled from the DB (populated by the classifier).
        No-op when the aligner isn't configured or language is unknown.
        Saves ~13s of cold-start on the first alignment per language
        per process.
        """
        if self._aligner is None:
            return
        language = self._db_language(song_path)
        if not language:
            return
        Thread(
            target=self._warmup_aligner,
            args=(language,),
            name=f"lyrics-warmup-{os.path.basename(song_path)}",
            daemon=True,
        ).start()

    def _warmup_aligner(self, language: str) -> None:
        try:
            ensure = getattr(self._aligner, "_ensure_align_model", None)
            if ensure is None:
                return
            ensure(_lang_base(language) or language)
        except Exception:
            logger.warning("wav2vec2 warmup failed for lang=%s", language, exc_info=True)

    def _read_metadata_for_lrclib(self, song_path: str) -> dict | None:
        """Return ``{"track", "artist", "duration"}`` from the songs table.

        The DB is authoritative for lyrics: ``register_download`` seeds
        artist/title from yt-dlp's info.json immediately after download,
        enrichment (iTunes/MusicBrainz) may later refine them in-place, and
        scanner-discovered songs get the same backfill. Either raw or
        enriched values feed LRCLib here; if the first query misses,
        ``_fetch_lrc_with_itunes_fallback`` re-canonicalises via iTunes.

        Returns None when artist or title is empty — ``_fetch_lrclib`` has
        no useful query without both, so we skip straight to the "no
        lyrics source" warning upstream.
        """
        if self._db is None:
            return None
        try:
            song_id = self._db.get_song_id_by_path(song_path)
        except Exception:
            logger.exception("failed to look up song_id for %s", song_path)
            return None
        if song_id is None:
            return None
        row = self._db.get_song_by_id(song_id)
        if row is None:
            return None
        track = (row["title"] or "").strip()
        artist = (row["artist"] or "").strip()
        if not track or not artist:
            return None
        # ISRC is optional — present when MusicBrainz enrichment hit. Spotify
        # variant uses it for deterministic ``q=isrc:<code>`` lookups; LRCLib /
        # Genius paths ignore the field.
        try:
            isrc = (row["isrc"] or "").strip() or None
        except (KeyError, IndexError):
            isrc = None
        return {
            "track": track,
            "artist": artist,
            "duration": row["duration_seconds"],
            "isrc": isrc,
        }

    def _metadata_candidates(self, info: dict | None, song_path: str | None) -> list[dict]:
        """Ordered, deduped (track, artist, duration, isrc) candidates.

        Builds an alternative-credits list to retry against any lyrics
        source whose primary lookup missed on the DB defaults. Sources,
        in priority order:

          1. ``info`` itself (DB defaults — usually right).
          2. Each collaborator from the artist credit as a solo artist
             (``"Gibbs & Kiełas"`` -> tries ``"Gibbs"`` and ``"Kiełas"``).
          3. ``regex_tidy`` of the filename split on ``" - "`` — catches
             cases where info.json's ``track``/``artist`` fields disagree
             with the filename a human typed.
          4. Top iTunes hits for ``"<artist> - <track>"`` — surfaces
             canonical artist credits when the indexer files the song
             under a different name (e.g. studio version under the
             label-credited artist instead of the YouTube uploader's).
          5. Top MusicBrainz hits — same shape, complementary catalogue.

        ``isrc`` propagates from the DB row to every candidate so Spotify's
        ISRC fast-path keeps working across the alternative credits.
        Service failures are independent — one timing out doesn't abort
        the others. Capped at ``_CANDIDATE_LIMIT`` total entries to bound
        worst-case cost on all-miss songs.
        """
        if not info:
            return []
        track = (info.get("track") or "").strip()
        artist = (info.get("artist") or "").strip()
        if not track or not artist:
            # The DB row is empty (a metadata-bug row, or a never-enriched
            # scanner import). Re-seed from the filename so the candidate
            # ladder has *something* to query against — without this every
            # downstream lyrics lookup short-circuits to "no candidates"
            # and the song stays caption-less even though the filename
            # spells out "Artist - Title" right there on disk.
            if not song_path:
                return []
            try:
                tidied = regex_tidy(_title_from_filename(song_path))
            except Exception:
                logger.exception("candidate gen: filename regex_tidy crashed for %s", song_path)
                return []
            if not has_artist_title_separator(tidied):
                return []
            f_artist, _, f_track = tidied.partition(" - ")
            track = f_track.strip()
            artist = f_artist.strip()
            if not track or not artist:
                return []
            info = {**info, "track": track, "artist": artist}

        seen: set[tuple[str, str]] = set()
        out: list[dict] = []

        def _add(t: str, a: str) -> None:
            t = (t or "").strip()
            a = (a or "").strip()
            if not t or not a:
                return
            key = _candidate_dedup_key(t, a)
            if key in seen:
                return
            seen.add(key)
            out.append({**info, "track": t, "artist": a})

        # 1. Original DB values first.
        _add(track, artist)

        # 2. Each collaborator solo. "Gibbs & Kiełas" -> Gibbs, Kiełas.
        for solo in _split_artist_credits(artist):
            if len(out) >= _CANDIDATE_LIMIT:
                break
            _add(track, solo)

        # 3. Filename regex_tidy (zero-cost local cleanup). Strip the
        # 11-char YouTube ID suffix first so it doesn't leak into the
        # candidate's track field.
        if song_path and len(out) < _CANDIDATE_LIMIT:
            try:
                tidied = regex_tidy(_title_from_filename(song_path))
                if has_artist_title_separator(tidied):
                    f_artist, _, f_track = tidied.partition(" - ")
                    _add(f_track, f_artist)
            except Exception:
                logger.exception("candidate gen: regex_tidy crashed for %s", song_path)

        # 4. iTunes top hits. Use ``_search_itunes_cached`` directly (the
        # already-imported, monkeypatch-friendly entry point used by the
        # language classifier) rather than the ``search_itunes`` wrapper
        # in music_metadata — keeps unit tests hermetic without an extra
        # patch site, and the LRU cache means the classifier's earlier
        # call is reused for free.
        if len(out) < _CANDIDATE_LIMIT:
            try:
                query = normalize_title(f"{artist} - {track}")
                rows = _search_itunes_cached(query, 5)
                for row in rows:
                    if len(out) >= _CANDIDATE_LIMIT:
                        break
                    hit = _itunes_row_to_dict(row)
                    _add(hit.get("trackName", ""), hit.get("artistName", ""))
            except Exception:
                logger.exception("candidate gen: iTunes search crashed for %r", track)

        # 5. MusicBrainz top hits (LRU-cached, separate catalogue).
        if len(out) < _CANDIDATE_LIMIT:
            try:
                query = f"{artist} {track}"
                for hit in search_musicbrainz(query, limit=3):
                    if len(out) >= _CANDIDATE_LIMIT:
                        break
                    _add(hit.get("track", ""), hit.get("artist", ""))
            except Exception:
                logger.exception("candidate gen: MusicBrainz search crashed for %r", track)

        return out

    def _try_with_candidates(
        self,
        fetcher,
        info: dict | None,
        song_path: str | None,
        *,
        label: str,
    ):
        """Call ``fetcher(candidate)`` for each candidate until one returns truthy.

        Returns ``(result, winning_candidate)``. Both are ``None`` when
        every candidate missed. The first candidate is always the DB
        defaults, so a song whose tag is correct pays no extra cost
        beyond the original single-shot fetch. Per-candidate exceptions
        are logged and skipped so a transient crash on one alternative
        never aborts the search.
        """
        candidates = self._metadata_candidates(info, song_path)
        if not candidates:
            return None, None
        for index, cand in enumerate(candidates):
            try:
                result = fetcher(cand)
            except Exception:
                logger.exception(
                    "%s candidate %d crashed for track=%r artist=%r",
                    label,
                    index,
                    cand.get("track"),
                    cand.get("artist"),
                )
                continue
            if result:
                if index > 0:
                    logger.info(
                        "%s rescued by candidate %d: track=%r artist=%r",
                        label,
                        index,
                        cand.get("track"),
                        cand.get("artist"),
                    )
                return result, cand
        return None, None

    def _run_language_classifier(self, song_path: str, info: dict | None) -> None:
        """Collect Tier 1 language signals and persist each at its rung.

        Every signal source is data we already fetched: yt-dlp's info.json
        (if still on disk — register_download usually consumes it, but a
        scanner-registered song may still have it), the
        ``_search_itunes_cached`` LRU populated by the enricher, and the
        ``_search_musicbrainz_cached`` LRU from the same enrichment pass.
        Cold caches return ``None`` and the extractor silently skips;
        we never fire a fresh HTTP request from this path.

        The classifier writes independently for each signal; the per-rung
        ladder in ``METADATA_SOURCE_CONFIDENCE`` handles winner selection.
        """
        if self._db is None:
            return
        try:
            song_id = self._db.get_song_id_by_path(song_path)
        except Exception:
            logger.exception("classifier: song_id lookup failed for %s", song_path)
            return
        if song_id is None:
            return

        yt_info = _read_info_json(song_path)
        itunes_hit: dict | None = None
        mb_signals: dict | None = None
        if info and info.get("artist") and info.get("track"):
            # Match the enricher's query shape (`_query_from_song` + iTunes'
            # internal ``normalize_title``) so both paths share the same LRU
            # entry and pay at most one iTunes round-trip per song.
            query = normalize_title(f"{info['artist']} - {info['track']}")
            try:
                rows = _search_itunes_cached(query, 1)
                if rows:
                    itunes_hit = _itunes_row_to_dict(rows[0])
            except Exception:
                logger.exception("classifier: iTunes lookup failed for %s", song_path)
            try:
                mb_signals = fetch_musicbrainz_language_signals(info["artist"], info["track"])
            except Exception:
                logger.exception("classifier: MusicBrainz lookup failed for %s", song_path)

        try:
            _signals, verdict = _classify_language(
                self._db,
                song_id,
                song_path=song_path,
                yt_info=yt_info,
                itunes_hit=itunes_hit,
                mb_signals=mb_signals,
                db_title=(info or {}).get("track"),
                db_artist=(info or {}).get("artist"),
            )
        except Exception:
            logger.exception("classifier: classify_and_persist crashed for %s", song_path)
            return

        # Re-enrich whenever Tier 1 produced a verdict — the enricher
        # short-circuits if ``language_at_enrich`` already matches, so a
        # consensus that confirmed the existing language costs only the
        # idempotency check.
        if verdict is not None:
            _dispatch_reenrich(self._db, song_id, song_path)

        # Tier 2a (US-43): when Tier 1 couldn't reach consensus, run a
        # Whisper language-ID probe on the raw audio. The probe writes
        # under ``whisper_probe_raw`` (rung 22), which beats every Tier 1
        # signal — text consensus abstained, so acoustic ground truth
        # takes over. No-op when consensus already landed, keeping the
        # happy path at ~50ms.
        if verdict is None:
            self._run_tier2a_probe(song_path, song_id)

    def _run_tier2a_probe(self, song_path: str, song_id: int) -> None:
        """Tier 2a Whisper language-ID probe on raw audio (US-43).

        Runs synchronously on the download-worker thread. Budget is 1-5s
        warm / 5-15s cold; the LRC fetch behind it is a 5s HTTP call, so
        the thread-handoff overhead to parallelise the two would cost
        more than the probe itself on a warm model. Inline is fine.

        Only fires when the Tier 1 text-consensus classifier returned no
        verdict (caller responsibility). Writes under ``whisper_probe_raw``
        (rung 22), which beats every Tier 1 text rung but sits below the
        stems-based ``whisper_probe_stems`` that Tier 2b will write later.
        """
        if self._db is None or not _whisper_fallback_enabled():
            return
        from pikaraoke.lib.audio_fingerprint import ensure_audio_fingerprint
        from pikaraoke.lib.demucs_processor import resolve_audio_source

        audio_path = resolve_audio_source(song_path)
        if not os.path.exists(audio_path):
            return
        try:
            audio_sha = ensure_audio_fingerprint(self._db, song_id, audio_path)
        except Exception:
            logger.exception("tier2a probe: fingerprint failed for %s", song_path)
            return
        if not audio_sha:
            return
        row = self._db.get_song_by_id(song_id)
        try:
            duration = row["duration_seconds"] if row is not None else None
        except (KeyError, IndexError):
            duration = None

        logger.info(
            "US-43 tier2a: %s starting sha=%s duration=%s",
            os.path.basename(song_path),
            audio_sha[:12],
            duration,
        )
        try:
            lang = _probe_audio_language(
                audio_path=audio_path,
                audio_sha256=audio_sha,
                duration_seconds=duration,
                get_model=_get_whisper_model,
                cache_get=self._db.get_metadata,
                cache_set=self._db.set_metadata,
            )
        except Exception:
            logger.exception("tier2a probe: probe_language crashed for %s", song_path)
            return
        if not lang:
            return
        try:
            applied = self._db.update_track_metadata_with_provenance(
                song_id, "whisper_probe_raw", {"language": lang}
            )
        except Exception:
            logger.exception("tier2a probe: failed to persist lang=%s for %s", lang, song_path)
            return
        logger.info(
            "US-43 tier2a: %s lang=%s applied=%s provenance=whisper_probe_raw",
            os.path.basename(song_path),
            lang,
            bool(applied),
        )
        if applied:
            _dispatch_reenrich(self._db, song_id, song_path)

    def _apply_cached_stems_probe(self, song_path: str) -> None:
        """Apply a previously cached ``whisper_probe_stems`` verdict, if any.

        The stems probe (Tier 2b) only runs after Demucs completes, which
        is long after the LRCLib/Genius fetch. But once it's run for a
        given ``audio_sha256``, the verdict is cached in ``db.metadata``
        and survives across sessions. On a re-dispatch (or a replay of an
        already-processed song), that cache is hot before the pipeline
        starts — so we can consult it up front and flip the DB language
        early, saving a wasted LRCLib fetch + dub-guard discard when the
        cached verdict disagrees with Tier-1 consensus.

        No-op when the cache has no entry (first-ever run), when Whisper
        isn't available, or when the probe was inconclusive. Writes under
        ``whisper_probe_stems`` rung, so the ladder still respects a
        sticky ``manual`` language.
        """
        if self._db is None:
            return
        try:
            song_id = self._db.get_song_id_by_path(song_path)
        except Exception:
            return
        if song_id is None:
            return
        row = self._db.get_song_by_id(song_id)
        if row is None:
            return
        audio_sha = row["audio_sha256"]
        if not audio_sha:
            return
        try:
            cached_lang, hit = _read_cached_probe_verdict(
                self._db.get_metadata, audio_sha, prefix="whisper_probe_stems"
            )
        except Exception:
            logger.exception("cached stems probe: read failed for %s", song_path)
            return
        if not hit or not cached_lang:
            return
        current_lang = row["language"]
        if _lang_base(current_lang or "") == _lang_base(cached_lang):
            return
        try:
            applied = self._db.update_track_metadata_with_provenance(
                song_id, "whisper_probe_stems", {"language": cached_lang}
            )
        except Exception:
            logger.exception("cached stems probe: persist failed for %s", song_path)
            return
        if applied:
            logger.info(
                "US-43 cached stems probe: %s applied lang=%s (was %s) before LRCLib fetch",
                os.path.basename(song_path),
                cached_lang,
                current_lang,
            )
            _dispatch_reenrich(self._db, song_id, song_path)

    def _run_tier2b_probe(self, song_path: str, song_id: int, stem_path: str) -> bool:
        """Tier 2b Whisper language-ID re-probe on the vocals stem (US-43).

        Returns ``True`` when the probe *flipped* the DB language (caller
        should abort the current alignment pass — the ``.ass`` +
        ``lyrics_sha`` have been invalidated so the next pipeline run
        re-fetches LRC in the corrected language, and the wav2vec2 model
        currently loaded is for the wrong language anyway).

        Returns ``False`` when the probe agrees with the current DB
        language (provenance is bumped to ``whisper_probe_stems``,
        language value unchanged), when the probe is inconclusive, when
        the ladder blocks the write (e.g. a ``manual`` language is
        sticky), or when Whisper isn't available at all — callers treat
        False as "proceed with alignment as normal".

        Unlike Tier 2a, this probe runs the whole song through
        ``detect_language`` with VAD filtering. The vocals stem is
        already clean (instruments gone, silences shortened), so
        averaging language probabilities across every sung segment gives
        meaningfully higher confidence than a single 30s window.
        """
        if self._db is None or not _whisper_fallback_enabled():
            return False

        row = self._db.get_song_by_id(song_id)
        if row is None:
            return False
        audio_sha = row["audio_sha256"]
        if not audio_sha:
            return False
        current_lang = row["language"]

        logger.info(
            "US-43 tier2b: %s starting sha=%s stem=%s current_db_lang=%s",
            os.path.basename(song_path),
            audio_sha[:12],
            os.path.basename(stem_path),
            current_lang,
        )
        try:
            stem_lang = _probe_audio_language_whole_song(
                audio_path=stem_path,
                audio_sha256=audio_sha,
                get_model=_get_whisper_model,
                cache_get=self._db.get_metadata,
                cache_set=self._db.set_metadata,
            )
        except Exception:
            logger.exception("tier2b probe: probe_language_whole_song crashed for %s", song_path)
            return False

        if not stem_lang:
            return False

        try:
            applied = self._db.update_track_metadata_with_provenance(
                song_id, "whisper_probe_stems", {"language": stem_lang}
            )
        except Exception:
            logger.exception("tier2b probe: failed to persist lang=%s for %s", stem_lang, song_path)
            return False
        if applied:
            _dispatch_reenrich(self._db, song_id, song_path)

        same_lang = _lang_base(current_lang or "") == stem_lang
        if same_lang:
            logger.info(
                "US-43 tier2b: %s agrees lang=%s applied=%s (provenance -> whisper_probe_stems)",
                os.path.basename(song_path),
                stem_lang,
                bool(applied),
            )
            return False

        if not applied:
            # Current language comes from a higher rung than
            # whisper_probe_stems — practically only ``manual``. Respect it.
            logger.info(
                "US-43 tier2b: %s disagrees (stems=%s, db=%s) but ladder blocked "
                "the write; keeping db value",
                os.path.basename(song_path),
                stem_lang,
                current_lang,
            )
            return False

        # Disagreement, and the write landed. Invalidate the auto ``.ass``
        # + ``lyrics_sha`` + ``aligner_model`` so the next
        # ``_do_fetch_and_convert`` treats the LRC cache as stale and
        # re-fetches in the corrected language. The currently-rendering
        # session keeps whatever line-level ``.ass`` already landed —
        # US-43's "write fast, fix later" path.
        logger.info(
            "US-43 tier2b: %s FLIP stems=%s db=%s; invalidating .ass for re-fetch",
            os.path.basename(song_path),
            stem_lang,
            current_lang,
        )
        try:
            from pikaraoke.lib.audio_fingerprint import invalidate_auto_ass

            invalidate_auto_ass(self._db, song_id)
        except Exception:
            logger.exception("tier2b probe: failed to invalidate .ass for %s", song_path)

        # Re-dispatch the pipeline now. Waiting for the "next"
        # ``song_downloaded`` is a dead-letter promise: that event only
        # fires on the first download, so replays of an existing row
        # would stay caption-less forever after a flip. Running on a
        # daemon thread so the caller (``_upgrade_to_word_level``) can
        # still unwind cleanly. The second pass sees the flipped DB
        # language, rejects the wrong-language LRC via
        # ``_is_lrc_language_mismatch``, and falls through to Genius /
        # VTT / Whisper ASR. The 2b probe on the second pass hits the
        # per-sha cache and agrees, so no infinite re-dispatch loop.
        Thread(
            target=self.fetch_and_convert,
            args=(song_path,),
            name=f"lyrics-refetch-{os.path.basename(song_path)}",
            daemon=True,
        ).start()
        return True

    def _db_language(self, song_path: str) -> str | None:
        """Return the DB-stored language for a song (e.g. "en", "pl-PL"), or None."""
        if self._db is None:
            return None
        try:
            song_id = self._db.get_song_id_by_path(song_path)
        except Exception:
            return None
        if song_id is None:
            return None
        row = self._db.get_song_by_id(song_id)
        if row is None:
            return None
        try:
            return row["language"]
        except (KeyError, IndexError):
            return None

    def _is_lrc_language_mismatch(self, song_path: str, lrc: str) -> bool:
        """True when the DB-stored audio language disagrees with the LRC's."""
        return self._is_lyrics_language_mismatch(
            song_path, _lrc_plain_text(lrc), source_label="LRCLib"
        )

    def _is_genius_language_mismatch(self, song_path: str, genius_text: str) -> bool:
        """True when the DB-stored audio language disagrees with Genius text.

        Genius, like LRCLib, is indexed by canonical song title — so the
        Polish dub of a Disney song can return the English original
        lyrics. Without this guard we'd align EN words against PL vocals
        (Whisper flipped the DB lang earlier, but Genius search is title-
        based and doesn't see that flip). Mirrors the LRCLib guard.
        """
        return self._is_lyrics_language_mismatch(song_path, genius_text, source_label="Genius")

    def _is_lyrics_language_mismatch(
        self, song_path: str, plain_text: str, *, source_label: str
    ) -> bool:
        """Core dub-trap guard shared by LRCLib + Genius paths.

        Catches the dub trap: both sources index by canonical song name,
        so a Polish dub of an English original (e.g. Edyta Górniak's
        "Kolorowy wiatr") gets the English "Colors of the Wind" lyrics
        served back. The pipeline would then render English text timed
        against Polish vocals, and sync looks permanently "off".

        Compares primary subtags only (``pl`` vs ``pl-PL`` counts as a
        match). NULL-cached DB language means "no ground truth yet" —
        trust the source in that case, because we have no better signal
        without adding a Whisper audio probe. Once Whisper writes its
        detected language back to the DB, the next run will enforce
        consistency.
        """
        db_lang = self._db_language(song_path)
        if not db_lang:
            return False
        lyrics_lang = _detect_language(plain_text)
        if not lyrics_lang:
            return False
        if _lang_base(db_lang) == _lang_base(lyrics_lang):
            return False
        logger.warning(
            "%s language mismatch for %s: DB=%s, lyrics=%s — dropping to "
            "avoid mis-synced subs; falling through to next source",
            source_label,
            os.path.basename(song_path),
            db_lang,
            lyrics_lang,
        )
        return True

    def _persist_vtt_language(self, song_path: str, vtt_path: str) -> None:
        """Write the chosen VTT's lang code to songs.language so subsequent
        runs (and whisperx alignment) skip audio-based language detection.
        US-14 P1.
        """
        if self._db is None:
            return
        lang = _vtt_lang_from_filename(song_path, vtt_path)
        if not lang:
            return
        try:
            song_id = self._db.get_song_id_by_path(song_path)
        except Exception:
            logger.exception("failed to look up song_id to persist VTT language")
            return
        if song_id is None:
            return
        try:
            row = self._db.get_song_by_id(song_id)
            if row is not None and row["language"]:
                return
        except (KeyError, IndexError):
            pass
        try:
            self._db.update_track_metadata_with_provenance(song_id, "scanner", {"language": lang})
        except Exception:
            logger.exception("failed to persist VTT language for song_id=%s", song_id)

    def _fetch_lrc_with_itunes_fallback(
        self, info: dict | None, song_path: str | None = None
    ) -> tuple[str | None, dict | None]:
        """Query LRCLib across candidate ``(track, artist)`` pairs.

        First candidate is ``info`` (DB defaults). On miss, retries with
        each alternative credit produced by ``_metadata_candidates`` —
        artist-token splits, filename regex_tidy, and iTunes/MusicBrainz
        top hits. Returns ``(lrc_or_None, info_with_winning_fields)`` so
        downstream log lines and DB writes see the canonical names that
        actually hit.
        """
        if not info:
            return None, info

        def _fetch(cand: dict) -> str | None:
            return _fetch_lrclib(cand["track"], cand["artist"], cand.get("duration"))

        lrc, winner = self._try_with_candidates(_fetch, info, song_path, label="LRCLib")
        if lrc and winner is not None:
            return lrc, {**info, "artist": winner["artist"], "track": winner["track"]}
        return lrc, info

    def _upgrade_to_word_level(self, song_path: str, lrc: str, lyrics_sha: str | None) -> None:
        if self._aligner is None:
            return
        try:
            # Whisperx alignment waits on stems and then runs per-word forced
            # alignment; easily the longest stage after Demucs. Surface it so
            # the splash shows progress beyond "Lyrics ready".
            self._emit_stage_notification(song_path, "Aligning words")
            audio_path = _wait_for_alignment_audio(song_path)
            # Stems weren't ready within the 120s budget; whisperx falls back
            # to the raw mix. Surface a song_warning so the operator can
            # correlate poor word-timing with the degraded source. US-9 P2.
            from pikaraoke.lib.demucs_processor import CACHE_DIR as _CACHE_DIR

            if not audio_path.startswith(_CACHE_DIR):
                try:
                    self._events.emit(
                        "song_warning",
                        {
                            "message": "Aligned on raw mix",
                            "detail": (
                                "Stems were not ready within "
                                f"{int(_STEM_WAIT_TIMEOUT_S)}s; word-level timing "
                                "may be less accurate than when vocals are isolated."
                            ),
                            "song": os.path.basename(song_path),
                            "severity": "warning",
                        },
                    )
                except Exception:
                    logger.exception("failed to emit raw-mix fallback song_warning")
            plain = _lrc_plain_text(lrc)
            # Language is required for wav2vec2 forced alignment (models are
            # per-language; the aligner no longer runs whisper ASR so it can't
            # detect from audio). Order of preference:
            #   1. Cached on the song row (info.json, enricher, prior run, or
            #      manual edit — all authoritative).
            #   2. Detected from the LRC text. Lyrics are clean prose, hundreds
            #      of words; text-detection is reliable and essentially free.
            song_id = self._db.get_song_id_by_path(song_path) if self._db else None

            # Tier 2b (US-43): re-validate language on the isolated vocals
            # stem. Fires only when we actually got stems (not the raw-mix
            # timeout fallback at line 747) — probing on the raw mix here
            # would just duplicate Tier 2a on a noisier input. If 2b flips
            # the DB language, abort this alignment pass: wav2vec2 is
            # per-language, aligning with the wrong model is wasted work.
            # ``_run_tier2b_probe`` both invalidates the stale .ass and
            # re-dispatches the pipeline so the corrected-language LRC
            # gets fetched immediately (waiting for a future
            # ``song_downloaded`` would leave replays caption-less).
            if song_id is not None and audio_path.startswith(_CACHE_DIR):
                if self._run_tier2b_probe(song_path, song_id, audio_path):
                    return

            db_lang = None
            if self._db is not None and song_id is not None:
                row = self._db.get_song_by_id(song_id)
                db_lang = row["language"] if row is not None else None
            language = db_lang or _detect_language(plain)
            if not language:
                logger.info(
                    "Skipping word-level alignment for %s: language unknown "
                    "(LRC too short or langdetect missing)",
                    song_path,
                )
                return
            words = self._aligner.align(
                audio_path,
                plain,
                lrc_lines=lrc_line_windows(lrc),
                language=language,
                vad_cache=self._vad_cache_for_song(song_path),
            )
            if self._db is not None and song_id is not None and not db_lang:
                # Persist the text-detected language so future runs and UI
                # lookups skip the detection step. ``lrc_heuristic`` sits
                # below every other rung: LRCLib records are occasionally
                # mislabelled (see US-43 Kolorowy wiatr), so any later
                # classifier/enricher signal must be able to overwrite it.
                self._db.update_track_metadata_with_provenance(
                    song_id, "lrc_heuristic", {"language": language}
                )
            if not words:
                return
            bpm = self._cached_estimate_bpm(song_path, audio_path)
            anim_params = _anim_params_for_bpm(bpm)
            # The aligner returns words in audio-true time space. When it
            # also detected per-line LRC->audio shifts, the LRC string
            # still carries the original (drifted) timestamps - the
            # renderer would emit Dialogue events ahead of audio unless
            # we rewrite the tags by the same per-line mapping.
            line_starts = getattr(self._aligner, "last_line_starts", None)
            if isinstance(line_starts, dict) and line_starts:
                render_lrc = _shift_lrc_per_line(lrc, line_starts)
            else:
                render_lrc = lrc
            aligner_id = self._aligner.model_id if self._aligner else None
            ass = _words_to_ass_with_k_tags(words, render_lrc, params=anim_params)
            if ass:
                from pikaraoke.lib.demucs_processor import CACHE_DIR

                wrote = self._try_write_ass_tiered(
                    song_path,
                    _TIER_WORD,
                    ass,
                    lyrics_source=SUBTITLE_SOURCE_LRCLIB_SYNC,
                    aligner_model=aligner_id,
                    lyrics_sha=lyrics_sha,
                )
                if wrote:
                    logger.info(
                        "Upgraded to per-word .ass for %s (audio=%s)",
                        song_path,
                        "vocals stem" if audio_path.startswith(CACHE_DIR) else "raw mix",
                    )
                    self._events.emit(
                        "notification",
                        f"Synced lyrics ready: {_title_from_filename(song_path)}",
                        "success",
                    )
                    # Symmetry with the consensus path: persist the
                    # prior-reliability score so a future quality
                    # dashboard, replay-on-realignment, and the
                    # consensus orchestrator's replay short-circuit see
                    # a populated value regardless of which code path
                    # produced the .ass. Residuals are populated by
                    # ``WhisperXAligner.align`` whenever ``lrc_lines``
                    # was passed (always in this branch via
                    # ``lrc_line_windows(lrc)``).
                    if self._db is not None and song_id is not None:
                        try:
                            from pikaraoke.lib.lyrics_align import (
                                _grade_priors,
                                _probe_audio_duration,
                            )

                            parsed = _parse_lrc(lrc)
                            audio_duration_s = _probe_audio_duration(audio_path)
                            implied_s = float(parsed[-1][0]) if parsed else None
                            score = _grade_priors(
                                audio_duration_s=audio_duration_s,
                                lrc_lines=[(s, s, t) for s, t in parsed],
                                lrc_implied_duration_s=implied_s,
                                dp_residuals=getattr(self._aligner, "last_dp_residuals", None),
                            )
                            self._db.update_lyrics_confidence(song_id, score)
                        except Exception:
                            logger.exception(
                                "legacy: failed to persist lyrics_confidence for %s",
                                os.path.basename(song_path),
                            )
        except Exception as e:
            logger.warning(
                "word-level alignment failed for %s, keeping line-level",
                song_path,
                exc_info=True,
            )
            try:
                self._events.emit(
                    "song_warning",
                    {
                        "message": "Word-level alignment failed",
                        "detail": f"{type(e).__name__}: {e}",
                        "song": os.path.basename(song_path),
                        "severity": "warning",
                    },
                )
            except Exception:
                logger.exception("failed to emit song_warning for alignment failure")

    def _try_genius_fallback(self, song_path: str, info: dict) -> bool:
        """Fetch plain lyrics from Genius, align them, and write a word-level .ass.

        Runs synchronously inside the ``fetch_and_convert`` worker thread
        (caller already runs there). Returns True on success so the caller
        can skip VTT fallback and the "no lyrics found" warning; False on
        any miss (no Genius match, no stems, no language, aligner failure)
        and the caller falls through to VTT.

        Genius lyrics are plain text — no timestamps — so we align the
        whole song in one pass (``lrc_lines=None``) then synthesise an LRC
        from the aligned word times and reuse the existing word-level ASS
        builder.
        """
        if self._aligner is None:
            return False
        track = info.get("track")
        artist = info.get("artist")
        if not track or not artist:
            return False
        logger.info("Genius: querying track=%r artist=%r", track, artist)

        def _fetch(cand: dict) -> str | None:
            text = _fetch_genius(cand["track"], cand["artist"])
            if not text:
                return None
            if self._is_genius_language_mismatch(song_path, text):
                return None
            return text

        genius_text, _winner = self._try_with_candidates(
            _fetch, info, song_path, label="Genius (fallback)"
        )
        if not genius_text:
            logger.info("Genius: miss track=%r artist=%r", track, artist)
            return False
        logger.info(
            "Genius: hit track=%r artist=%r (%d chars)",
            track,
            artist,
            len(genius_text),
        )

        self._emit_stage_notification(song_path, "Aligning Genius lyrics")
        _prewarm_stems(song_path)
        audio_path = _wait_for_alignment_audio(song_path)

        lines = [ln for ln in genius_text.splitlines() if ln.strip()]
        plain = "\n".join(lines)

        song_id = self._db.get_song_id_by_path(song_path) if self._db else None
        db_lang = None
        if self._db is not None and song_id is not None:
            row = self._db.get_song_by_id(song_id)
            db_lang = row["language"] if row is not None else None
        language = db_lang or _detect_language(plain)
        if not language:
            logger.info("Skipping Genius alignment for %s: language unknown", song_path)
            return False

        try:
            words = self._aligner.align(audio_path, plain, language=language)
        except Exception as e:
            logger.warning("Genius alignment failed for %s", song_path, exc_info=True)
            try:
                self._events.emit(
                    "song_warning",
                    {
                        "message": "Genius alignment failed",
                        "detail": f"{type(e).__name__}: {e}",
                        "song": os.path.basename(song_path),
                        "severity": "warning",
                    },
                )
            except Exception:
                logger.exception("failed to emit song_warning for Genius alignment failure")
            return False
        if not words:
            return False

        synthetic_lrc = _lrc_from_aligned_lines(words, lines)
        if not synthetic_lrc:
            return False

        bpm = self._cached_estimate_bpm(song_path, audio_path)
        aligner_id = self._aligner.model_id if self._aligner else None
        ass = _words_to_ass_with_k_tags(
            words,
            synthetic_lrc,
            params=_anim_params_for_bpm(bpm),
        )
        if not ass:
            return False

        lyrics_sha = _lrc_sha(synthetic_lrc)
        wrote = self._try_write_ass_tiered(
            song_path,
            _TIER_WORD,
            ass,
            lyrics_source=SUBTITLE_SOURCE_GENIUS_SYNC,
            aligner_model=aligner_id,
            lyrics_sha=lyrics_sha,
        )
        if not wrote:
            return False
        if self._db is not None and song_id is not None and not db_lang:
            # Genius plain lyrics are text-only; same upstream-mislabel risk
            # as LRCLib, so this shares the ``lrc_heuristic`` rung.
            self._db.update_track_metadata_with_provenance(
                song_id, "lrc_heuristic", {"language": language}
            )
        logger.info("Genius: wrote word-level .ass for %s - %s", artist, track)
        return True

    def _audio_sha_for_song(self, song_path: str) -> str | None:
        """Resolve the source-audio sha256 for cache keys.

        Used by callers that key into ``metadata`` cache prefixes
        (whisper_transcript, audio_bpm, audio_vad_onsets, etc.). Returns
        None when the DB is unwired, the song row is missing, or the
        source audio path can't be resolved — caller treats that as
        "no cache available, run uncached".
        """
        if self._db is None:
            return None
        try:
            from pikaraoke.lib.audio_fingerprint import ensure_audio_fingerprint
            from pikaraoke.lib.demucs_processor import resolve_audio_source

            song_id = self._db.get_song_id_by_path(song_path)
            if song_id is None:
                return None
            return ensure_audio_fingerprint(self._db, song_id, resolve_audio_source(song_path))
        except Exception:
            logger.exception("audio_sha lookup failed for %s", song_path)
            return None

    def _vad_cache_for_song(self, song_path: str):
        """Build a ``VadCacheRef`` for ``song_path`` or None when DB unwired.

        Used by every site that calls the aligner with ``lrc_lines`` (and
        thus triggers the per-line shift detector's VAD probe). One
        helper keeps the lookup symmetric across call sites.
        """
        if self._db is None:
            return None
        audio_sha = self._audio_sha_for_song(song_path)
        if not audio_sha:
            return None
        from pikaraoke.lib.lyrics_align import VadCacheRef

        return VadCacheRef(
            audio_sha256=audio_sha,
            cache_get=self._db.get_metadata,
            cache_set=self._db.set_metadata,
        )

    def _cached_estimate_bpm(self, song_path: str, audio_path: str) -> float | None:
        """BPM estimation with disk-persistent caching by ``audio_sha256``.

        ``_estimate_bpm`` already keeps a process-local memo, but that
        doesn't survive restarts and is keyed by file path (not content).
        This wrapper persists the verdict in ``metadata`` so a re-run on
        the same audio bytes is free across restarts. ``None`` (BPM
        couldn't be detected) is also cached — same audio would just
        fail again.
        """
        from pikaraoke.lib.audio_feature_cache import CACHE_MISS, read_bpm, write_bpm

        if self._db is None:
            return _estimate_bpm(audio_path)
        audio_sha = self._audio_sha_for_song(song_path)
        if not audio_sha:
            return _estimate_bpm(audio_path)
        cached = read_bpm(self._db.get_metadata, audio_sha)
        if cached is not CACHE_MISS:
            return cached  # type: ignore[return-value]
        bpm = _estimate_bpm(audio_path)
        write_bpm(self._db.set_metadata, audio_sha, bpm)
        return bpm

    def _try_whisper_fallback(self, song_path: str) -> None:
        """Last-resort ASR: transcribe the vocals stem with faster-whisper.

        Runs only when LRCLib / Genius / YouTube VTT all missed. We already
        fired off "No lyrics source" in the caller; this thread writes a
        word-level .ass tagged ``lyrics_source="whisper"`` so the splash
        badge can flag these as machine-transcribed (lower trust than a
        curated LRC / user-authored .ass).

        Uses Whisper's own word timestamps rather than re-aligning through
        wav2vec2. Whisper's timings are a touch coarser (~200ms) than
        forced alignment but the text it emits is a phoneme-level fiction
        anyway — pushing hallucinated words back through wav2vec2 would
        only hide the errors, not fix them.

        Cache: the transcript itself is keyed by ``(audio_sha256, model)``
        and shared with ``_run_whisper_for_consensus`` — when consensus
        already paid the transcribe cost on the same vocals stem, this
        path reads it back instead of re-running the model.
        """
        try:
            self._emit_stage_notification(song_path, "Transcribing (Whisper)")
            _prewarm_stems(song_path)
            audio_path = _wait_for_alignment_audio(song_path)
            model_name = _resolve_whisper_model()
            audio_sha = self._audio_sha_for_song(song_path)

            cached = None
            if audio_sha and self._db is not None:
                cached = _read_cached_whisper_transcript(
                    self._db.get_metadata, audio_sha, model_name
                )
            if cached is not None:
                logger.info(
                    "whisper_transcript: cache hit (fallback) sha=%s model=%s",
                    audio_sha[:12],
                    model_name,
                )
                words = list(cached.words)
                lrc = cached.lrc
                lang = cached.language
            else:
                model = _get_whisper_model()
                if model is None:
                    return
                segments_iter, info = model.transcribe(
                    audio_path,
                    word_timestamps=True,
                    vad_filter=True,
                )
                segments = list(segments_iter)
                if not segments:
                    logger.info("Whisper fallback: empty transcription for %s", song_path)
                    self._emit_whisper_failure(
                        song_path, "empty transcription", error_code="whisper_empty"
                    )
                    return
                lrc = _lrc_from_whisper_segments(segments)
                lang = getattr(info, "language", None)
                words = []
                for seg in segments:
                    for w in seg.words or []:
                        text = (getattr(w, "word", "") or "").strip()
                        if not text or w.start is None or w.end is None:
                            continue
                        start = float(w.start)
                        end = float(w.end)
                        parts = _syllable_parts(text, lang, start, end)
                        words.append(Word(text=text, start=start, end=end, parts=parts))
                if not words or not lrc:
                    logger.info("Whisper fallback: no usable words for %s", song_path)
                    self._emit_whisper_failure(
                        song_path, "no usable word timings", error_code="whisper_no_words"
                    )
                    return
                if audio_sha and self._db is not None:
                    _write_cached_whisper_transcript(
                        self._db.set_metadata,
                        audio_sha,
                        model_name,
                        language=lang,
                        lrc=lrc,
                        words=words,
                    )
            bpm = self._cached_estimate_bpm(song_path, audio_path)
            ass = _words_to_ass_with_k_tags(
                words,
                lrc,
                params=_anim_params_for_bpm(bpm),
            )
            if not ass:
                logger.info("Whisper fallback: ASS conversion failed for %s", song_path)
                self._emit_whisper_failure(
                    song_path, "ASS conversion failed", error_code="whisper_ass_failed"
                )
                return
            wrote = self._try_write_ass_tiered(
                song_path,
                _TIER_WORD,
                ass,
                lyrics_source=SUBTITLE_SOURCE_AI,
                aligner_model=f"whisper-{model_name}",
                lyrics_sha=_lrc_sha(lrc),
            )
            if not wrote:
                return
            if lang and self._db is not None:
                song_id = self._db.get_song_id_by_path(song_path)
                if song_id is not None:
                    try:
                        # Whisper ASR's language-ID is acoustic ground truth
                        # on the vocals stem; ranks above every text-derived
                        # signal but below the dedicated pre-alignment probes
                        # (whisper_probe_raw / _stems).
                        self._db.update_track_metadata_with_provenance(
                            song_id, "whisper_asr", {"language": lang}
                        )
                    except Exception:
                        logger.exception("failed to persist whisper language for %s", song_path)
            logger.info(
                "Whisper: wrote word-level .ass for %s (lang=%s, model=%s)",
                os.path.basename(song_path),
                lang or "?",
                model_name,
            )
            try:
                self._events.emit(
                    "notification",
                    f"Auto-lyrics ready: {_title_from_filename(song_path)}",
                    "info",
                )
            except Exception:
                logger.exception("failed to emit whisper success notification")
        except Exception as e:
            logger.exception("Whisper fallback crashed for %s", song_path)
            self._emit_whisper_failure(
                song_path, f"{type(e).__name__}: {e}", error_code="whisper_crashed"
            )

    def _emit_whisper_failure(
        self, song_path: str, detail: str, *, error_code: str | None = None
    ) -> None:
        """Emit ``song_warning`` and persist a pipeline-failure marker.

        ``error_code`` is the stable token recorded in the failure cache
        so the integrity scan can read back why the pipeline last failed
        (``whisper_empty``, ``whisper_no_words``, ``whisper_ass_failed``,
        ``whisper_crashed``). The free-form ``detail`` string still rides
        the toast for human readers.
        """
        try:
            self._events.emit(
                "song_warning",
                {
                    "message": "No lyrics found",
                    "detail": f"Whisper fallback: {detail}.",
                    "song": os.path.basename(song_path),
                    "severity": "warning",
                },
            )
        except Exception:
            logger.exception("failed to emit whisper-failure song_warning")
        if error_code is None:
            return
        audio_sha = self._audio_sha_for_song(song_path)
        if audio_sha and self._db is not None:
            _record_pipeline_failure(
                self._db.get_metadata,
                self._db.set_metadata,
                audio_sha,
                error_code=error_code,
            )

    def _run_whisper_for_consensus(self, song_path: str) -> tuple[list["Word"], str | None]:
        """Transcribe vocals with Whisper, return ``(words, language)``.

        Same model load + segments-iter walk as ``_try_whisper_fallback``,
        but skips the ASS write. Used by the consensus engine as an
        always-parallel audio-reference contributor — its tokens score
        title-matched sources and its words can scaffold a T3 LRC when
        every synced source got rejected. Catches every internal Whisper
        exception (OOM, decode failures, model load) and returns
        ``([], None)`` so the consensus pool just runs without it.

        Cache: shares the ``(audio_sha256, model)`` transcript cache
        with ``_try_whisper_fallback``. Whichever path runs first pays
        the transcribe cost once; the other gets the words for free.
        """
        try:
            audio_path = _wait_for_alignment_audio(song_path)
            model_name = _resolve_whisper_model()
            audio_sha = self._audio_sha_for_song(song_path)

            if audio_sha and self._db is not None:
                cached = _read_cached_whisper_transcript(
                    self._db.get_metadata, audio_sha, model_name
                )
                if cached is not None:
                    logger.info(
                        "whisper_transcript: cache hit (consensus) sha=%s model=%s",
                        audio_sha[:12],
                        model_name,
                    )
                    return list(cached.words), cached.language

            model = _get_whisper_model()
            if model is None:
                return [], None
            segments_iter, info = model.transcribe(
                audio_path, word_timestamps=True, vad_filter=True
            )
            segments = list(segments_iter)
            if not segments:
                return [], None
            lrc = _lrc_from_whisper_segments(segments)
            lang = getattr(info, "language", None)
            words: list[Word] = []
            for seg in segments:
                for w in seg.words or []:
                    text = (getattr(w, "word", "") or "").strip()
                    if not text or w.start is None or w.end is None:
                        continue
                    start = float(w.start)
                    end = float(w.end)
                    parts = _syllable_parts(text, lang, start, end)
                    words.append(Word(text=text, start=start, end=end, parts=parts))
            if audio_sha and self._db is not None and words and lrc:
                _write_cached_whisper_transcript(
                    self._db.set_metadata,
                    audio_sha,
                    model_name,
                    language=lang,
                    lrc=lrc,
                    words=words,
                )
            return words, lang
        except Exception:
            logger.exception("whisper-for-consensus crashed for %s", song_path)
            return [], None

    def _upgrade_via_consensus(
        self,
        song_path: str,
        info: dict | None,
        lrclib_lrc: str | None,
        lyrics_sha: str | None,
    ) -> None:
        """T3 path via multi-source consensus (gated by LYRICS_CONSENSUS_ENABLED).

        Fans out Musixmatch/Megalobiz/Genius/Whisper in parallel, collects
        VTT + LRCLib already on hand, builds an audio reference (VTT +
        Whisper), runs the consensus voter, aligns the consensus text once
        through wav2vec2, and writes T3. Drops every safety guard on the
        same blocks the legacy path uses (tier gate, atomic write, sha
        invalidation), so a partial pipeline failure leaves the existing
        T1/T2 .ass alone.
        """
        with _get_consensus_semaphore():
            self._upgrade_via_consensus_locked(song_path, info, lrclib_lrc, lyrics_sha)

    def _upgrade_via_consensus_locked(
        self,
        song_path: str,
        info: dict | None,
        lrclib_lrc: str | None,
        lyrics_sha: str | None,
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from pikaraoke.lib import lyrics_consensus as lc

        basename = os.path.basename(song_path)
        track = (info or {}).get("track")
        artist = (info or {}).get("artist")

        sources: list[lc.SourceResult] = []
        vtt_source: lc.SourceResult | None = None
        whisper_source: lc.SourceResult | None = None

        # 1. VTT (free, on-disk if present)
        try:
            vtt_path = _pick_best_vtt(song_path, preferred_lang=self._db_language(song_path))
            if vtt_path:
                with open(vtt_path, encoding="utf-8") as f:
                    vtt_text = f.read()
                vtt_lrc = _vtt_to_lrc(vtt_text)
                if vtt_lrc:
                    vtt_source = lc.SourceResult(
                        name="vtt", kind="source_matched", lrc=vtt_lrc, is_synced=True
                    )
                    sources.append(vtt_source)
        except Exception:
            logger.warning("consensus: VTT load failed for %s", basename, exc_info=True)

        # 2. LRCLib (already fetched on the main thread)
        if lrclib_lrc:
            sources.append(
                lc.SourceResult(name="lrclib", kind="title_matched", lrc=lrclib_lrc, is_synced=True)
            )

        # 3. Parallel fan-out: MXM, Megalobiz, Genius, Whisper.
        # Each text-source fetcher iterates ``_metadata_candidates`` so an
        # artist-tag drift on the DB row doesn't deny the consensus voter
        # an opinion from a source that would otherwise have one.
        providers = _consensus_providers()
        fetchers: dict[str, callable] = {}  # type: ignore[type-arg]
        if track and artist:

            def _wrap(label: str, fn):
                def _runner():
                    result, _winner = self._try_with_candidates(
                        lambda c: fn(c["track"], c["artist"]),
                        info,
                        song_path,
                        label=label,
                    )
                    return result

                return _runner

            if "musixmatch" in providers:
                fetchers["musixmatch"] = _wrap("Musixmatch (consensus)", _fetch_musixmatch)
            if "megalobiz" in providers:
                fetchers["megalobiz"] = _wrap("Megalobiz (consensus)", _fetch_megalobiz)
            if GENIUS_ACCESS_TOKEN:
                fetchers["genius"] = _wrap("Genius (consensus)", _fetch_genius)
        if _whisper_fallback_enabled():
            fetchers["whisper"] = lambda: self._run_whisper_for_consensus(song_path)

        t_start = time.monotonic()
        completed: list[str] = []
        if fetchers:
            with ThreadPoolExecutor(max_workers=max(1, len(fetchers))) as ex:
                futures = {ex.submit(fn): name for name, fn in fetchers.items()}
                try:
                    for fut in as_completed(futures, timeout=180):
                        name = futures[fut]
                        try:
                            r = fut.result()
                        except Exception:
                            logger.warning("consensus: fetcher %s raised", name, exc_info=True)
                            self._emit_consensus_decision(song_path, name, "error", 0.0)
                            continue
                        if r is None:
                            continue
                        if name == "whisper":
                            words, _lang = r
                            if not words:
                                continue
                            whisper_source = lc.SourceResult(
                                name="whisper",
                                kind="source_matched",
                                words=words,
                                is_synced=False,
                            )
                            sources.append(whisper_source)
                        elif name in ("musixmatch", "megalobiz") and r:
                            sources.append(
                                lc.SourceResult(
                                    name=name,
                                    kind="title_matched",
                                    lrc=r,
                                    is_synced=True,
                                )
                            )
                        elif name == "genius" and r:
                            sources.append(
                                lc.SourceResult(
                                    name="genius",
                                    kind="title_matched",
                                    plain_text=r,
                                    is_synced=False,
                                )
                            )
                        completed.append(name)
                        # Early-stop: ≥3 results AND ≥30s since executor entry.
                        if len(completed) >= 3 and time.monotonic() - t_start >= 30:
                            for f in futures:
                                f.cancel()
                            break
                except TimeoutError:
                    logger.warning(
                        "consensus: as_completed timeout, %d sources collected",
                        len(completed),
                    )

        if not sources:
            logger.info("consensus: no sources for %s", basename)
            return

        audio_ref = lc.build_audio_reference(vtt_source, whisper_source)
        # Persist scores BEFORE the consensus result decision: when every
        # title-matched source is rejected ``build_consensus`` returns
        # None and discards its own ``source_scores``. The variant gate
        # downstream still wants those numbers — the Kolorowy wiatr
        # mislabel survives by being rejected, then re-fetched on the
        # next picker click. Score-first means the second click sees
        # the same verdict the consensus engine reached.
        score_map = lc.score_sources_against_reference(sources, audio_ref)
        self._persist_consensus_scores(song_path, score_map)
        consensus = lc.build_consensus(sources, audio_ref)
        if consensus is None:
            logger.info("consensus: build returned None for %s", basename)
            try:
                self._events.emit(
                    "song_warning",
                    {
                        "message": "Lyrics consensus failed",
                        "detail": "Could not establish lyrics consensus across sources.",
                        "song": basename,
                        "severity": "info",
                    },
                )
            except Exception:
                logger.exception("failed to emit consensus failure warning")
            return

        for name in consensus.sources_used:
            self._emit_consensus_decision(song_path, name, "accepted", consensus.confidence)
        for name, _reason in consensus.sources_rejected:
            self._emit_consensus_decision(song_path, name, "rejected", 0.0)

        if self._aligner is None:
            logger.info("consensus: no aligner, skipping T3 write for %s", basename)
            return

        try:
            audio_path = _wait_for_alignment_audio(song_path)
        except Exception:
            logger.exception("consensus: alignment audio missing for %s", basename)
            return

        # Pre-grade the consensus LRC priors against audio *before*
        # alignment so the routing decision drives the aligner itself,
        # not just the post-hoc line template. ``grade_lrc_priors_against_audio``
        # runs the same VAD-onset probe + DP that the line-windowed
        # aligner uses, so the score reflects all three reliability
        # signals (duration mismatch + DP shift + DP rejection) instead
        # of duration alone. Replay-aware: a persisted score above the
        # gate skips the probe entirely; below the gate we re-grade so
        # an improved upstream LRC has a chance to flip the routing.
        from pikaraoke.lib.lyrics_align import (
            _RELIABILITY_GATE,
            VadCacheRef,
            grade_lrc_priors_against_audio,
        )

        consensus_lrc_windows = lrc_line_windows(consensus.lrc)
        song_id = self._db.get_song_id_by_path(song_path) if self._db else None
        replay_score: float | None = None
        if song_id is not None and self._db is not None:
            persisted = self._db.get_lyrics_confidence(song_id)
            if persisted is not None and persisted >= _RELIABILITY_GATE:
                replay_score = persisted
                logger.info(
                    "consensus: replaying persisted confidence %.2f for %s " "(skipping re-grade)",
                    replay_score,
                    basename,
                )

        # Disk-persistent VAD cache shared by the grader pass below and
        # the aligner's per-line shift detector — both probe the same
        # audio, so the second call hits cache instead of re-running
        # silero + silencedetect.
        vad_cache: VadCacheRef | None = None
        audio_sha = self._audio_sha_for_song(song_path)
        if audio_sha and self._db is not None:
            vad_cache = VadCacheRef(
                audio_sha256=audio_sha,
                cache_get=self._db.get_metadata,
                cache_set=self._db.set_metadata,
            )

        if replay_score is not None:
            score = replay_score
        else:
            try:
                score, _residuals = grade_lrc_priors_against_audio(
                    audio_path, consensus_lrc_windows, vad_cache=vad_cache
                )
            except Exception:
                logger.exception(
                    "consensus: pre-alignment grading crashed for %s; "
                    "falling back to whole-song alignment",
                    basename,
                )
                score = 0.0

        # Routing: high-confidence priors get line-windowed alignment
        # (faster, more accurate, line template = consensus.lrc).
        # Low-confidence priors get whole-song alignment (escapes bad
        # LRC timestamps) and the line template is rebuilt from aligned
        # words. The Genius fallback path uses the same synthetic-LRC
        # pattern.
        align_with_lrc_lines = score >= _RELIABILITY_GATE and bool(consensus_lrc_windows)

        try:
            language = self._db_language(song_path)
            if align_with_lrc_lines:
                aligned = self._aligner.align(
                    audio_path,
                    consensus.text,
                    lrc_lines=consensus_lrc_windows,
                    language=language,
                    vad_cache=vad_cache,
                )
            else:
                aligned = self._aligner.align(
                    audio_path, consensus.text, language=language, vad_cache=vad_cache
                )
        except Exception:
            logger.exception("consensus: aligner crashed for %s", basename)
            try:
                self._events.emit(
                    "song_warning",
                    {
                        "message": "Lyrics alignment failed",
                        "detail": "Consensus established but wav2vec2 alignment crashed.",
                        "song": basename,
                        "severity": "warning",
                    },
                )
            except Exception:
                logger.exception("failed to emit alignment failure warning")
            return

        if not aligned:
            logger.info("consensus: empty aligner output for %s", basename)
            return

        bpm = self._cached_estimate_bpm(song_path, audio_path)

        line_template = consensus.lrc
        if not align_with_lrc_lines:
            # Source the line breaks from the consensus LRC's parsed text
            # rows. ``consensus.text`` is space-joined voted tokens (one
            # logical line) so ``splitlines()`` would collapse the song
            # into a single Dialogue event.
            text_lines = [text for _start, text in _parse_lrc(consensus.lrc) if text.strip()]
            synthetic = _lrc_from_aligned_lines(aligned, text_lines)
            if synthetic:
                line_template = synthetic
                logger.info(
                    "consensus: grader=%.2f < %.2f, using synthetic LRC for %s",
                    score,
                    _RELIABILITY_GATE,
                    basename,
                )
        elif getattr(self._aligner, "last_line_starts", None):
            # Line-windowed alignment may have shifted the LRC tags to
            # match audio (intro padding + per-verse drift); apply the
            # same per-line mapping the legacy path uses so the rendered
            # Dialogue events match what the aligner anchored against.
            line_template = _shift_lrc_per_line(consensus.lrc, self._aligner.last_line_starts)

        try:
            if song_id is not None and self._db is not None:
                self._db.update_lyrics_confidence(song_id, score)
        except Exception:
            logger.exception("consensus: failed to persist lyrics_confidence for %s", basename)

        ass = _words_to_ass_with_k_tags(
            aligned,
            line_template,
            params=_anim_params_for_bpm(bpm),
        )
        if not ass:
            logger.info("consensus: ASS conversion failed for %s", basename)
            return

        consensus_sha = _lrc_sha(consensus.lrc) or lyrics_sha
        aligner_model = getattr(self._aligner, "model_name", None)
        if not isinstance(aligner_model, str):
            aligner_model = "wav2vec2"
        wrote = self._try_write_ass_tiered(
            song_path,
            _TIER_WORD,
            ass,
            lyrics_source=SUBTITLE_SOURCE_CONSENSUS,
            aligner_model=aligner_model,
            lyrics_sha=consensus_sha,
        )
        if not wrote:
            return
        # Persist the input-set hash so subsequent variant landings can
        # short-circuit the rerun trigger when no new input has arrived.
        try:
            audio_sha_persist = self._audio_sha_for_song(song_path)
            if audio_sha_persist and self._db is not None:
                final_hash = self._compute_consensus_input_hash(song_path)
                if final_hash:
                    self._db.set_metadata(
                        f"consensus_input_hash:{audio_sha_persist}", final_hash
                    )
        except Exception:
            logger.exception("consensus: failed to persist input hash for %s", basename)
        logger.info(
            "consensus: wrote T3 for %s (sources=%s, confidence=%.2f, rejected=%s)",
            basename,
            consensus.sources_used,
            consensus.confidence,
            [n for n, _ in consensus.sources_rejected],
        )

    def _persist_consensus_scores(
        self, song_path: str, score_map: dict[str, tuple[float, bool]]
    ) -> None:
        """Write per-source coverage to ``subtitle_jobs.coverage``.

        Maps consensus-engine source names to canonical subtitle-source
        keys via ``_CONSENSUS_SOURCE_TO_VARIANT``. Sources without a
        variant chip (Musixmatch, Megalobiz) are silently skipped — the
        consensus engine still uses their scores internally for confidence,
        we just don't surface them in the picker. Per-source failures are
        logged and swallowed so a missing subtitle_jobs row (orchestrator
        hasn't dispatched yet) doesn't tank the rest.
        """
        if self._db is None or not score_map:
            return
        try:
            song_id = self._db.get_song_id_by_path(song_path)
        except Exception:
            logger.exception(
                "consensus persist: get_song_id_by_path failed for %s",
                os.path.basename(song_path),
            )
            return
        if song_id is None:
            return
        for consensus_name, (coverage, uncertain) in score_map.items():
            variant_source = _CONSENSUS_SOURCE_TO_VARIANT.get(consensus_name)
            if variant_source is None:
                continue
            try:
                self._db.update_subtitle_job_score(song_id, variant_source, coverage, uncertain)
            except Exception:
                logger.exception(
                    "consensus persist: update_subtitle_job_score failed for %s/%s",
                    os.path.basename(song_path),
                    variant_source,
                )

    def _emit_consensus_decision(
        self, song_path: str, source: str, decision: str, confidence: float
    ) -> None:
        try:
            self._events.emit(
                "consensus_decision",
                {
                    "song": os.path.basename(song_path),
                    "source": source,
                    "decision": decision,
                    "confidence": round(confidence, 3),
                },
            )
        except Exception:
            logger.exception("failed to emit consensus_decision event")

    def reprocess_library(self, song_paths: list[str]) -> int:
        """Upgrade existing line-level auto-lyrics to word-level in the background.

        Candidates are songs with an auto-generated ``.ass`` (carries the marker)
        that lacks ``\\k`` tags - i.e. files produced before whisperx was
        available. No-op when no aligner is configured or nothing qualifies.

        Returns the number of songs scheduled for upgrade. Processing runs
        serially in a single daemon thread so the aligner doesn't thrash CPU/GPU.
        """
        if self._aligner is None:
            return 0
        candidates = [p for p in song_paths if _needs_word_level_upgrade(p)]
        if not candidates:
            return 0
        logger.info(
            "Reprocessing %d song(s) to word-level karaoke captions in the background",
            len(candidates),
        )
        Thread(
            target=self._reprocess_batch,
            args=(candidates,),
            name="lyrics-reprocess",
            daemon=True,
        ).start()
        return len(candidates)

    def _reprocess_batch(self, song_paths: list[str]) -> None:
        for song_path in song_paths:
            try:
                self._reprocess_one(song_path)
            except Exception:
                logger.exception("reprocess failed for %s", song_path)

    def _reprocess_one(self, song_path: str) -> None:
        """Re-fetch LRCLib from the filename-derived title, then align to word-level."""
        if self._aligner is None:
            return
        if not _needs_word_level_upgrade(song_path):
            return  # raced with another update
        title = _title_from_filename(song_path)
        if not title:
            logger.debug("reprocess: could not extract title from %s", song_path)
            return
        meta = resolve_metadata(title)
        if not meta:
            logger.debug("reprocess: iTunes had no match for %r", title)
            return
        lrc = _fetch_lrclib(meta["track"], meta["artist"], None)
        if not lrc:
            logger.debug(
                "reprocess: LRCLib had no match for %r / %r", meta["artist"], meta["track"]
            )
            return
        _prewarm_stems(song_path)
        self._upgrade_to_word_level(song_path, lrc, _lrc_sha(lrc))


def _needs_word_level_upgrade(song_path: str) -> bool:
    """True when <stem>.ass is auto-generated AND has no \\k tags yet."""
    path = _ass_path(song_path)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return False
    if ASS_MARKER not in content:
        return False  # user-owned Aegisub file
    # Any \k tag means it's already word-level.
    return "\\k" not in content


def _lrc_sha(lrc: str) -> str:
    """Stable content fingerprint for an LRC payload.

    Used as the cache key for whisper alignment output: same input lyrics ->
    same alignment, so a matching sha lets us keep the existing .ass across
    re-downloads. A changed sha (LRCLib updated the lyrics) invalidates it.
    """
    return hashlib.sha256(lrc.encode("utf-8")).hexdigest()


def _is_word_level_auto_ass(song_path: str) -> bool:
    """True when <stem>.ass is auto-generated AND already word-level (\\k tags present)."""
    path = _ass_path(song_path)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return False
    return ASS_MARKER in content and "\\k" in content


def _title_from_filename(song_path: str) -> str:
    """Strip the 11-char YouTube ID suffix (both ``---ID`` and ``[ID]`` forms).

    Lightweight replacement for SongManager.filename_from_path so lyrics.py
    stays free of the SongManager dependency.
    """
    stem = os.path.splitext(os.path.basename(song_path))[0]
    # Triple-dash PiKaraoke form
    m = re.search(r"---([A-Za-z0-9_-]{11})$", stem)
    if m:
        return stem[: m.start()].strip()
    # yt-dlp brackets form
    m = re.search(r"\s*\[([A-Za-z0-9_-]{11})\]$", stem)
    if m:
        return stem[: m.start()].strip()
    return stem.strip()


def _ass_path(song_path: str) -> str:
    stem, _ext = os.path.splitext(song_path)
    return f"{stem}.ass"


def variant_ass_path(song_path: str, source: str) -> str:
    """Return the per-source variant ASS path: ``<stem>.<source>.ass``.

    Variants coexist on disk with the canonical ``<stem>.ass``; the
    operator's source-picker pin in ``songs.subtitle_source_override``
    decides which file ``/subtitle/<id>`` serves. ``user`` and ``off``
    have no variant file (callers should not invoke this for them).
    """
    stem, _ext = os.path.splitext(song_path)
    return f"{stem}.{source}.ass"


# Backwards-compatible alias for internal callers within this module.
_variant_ass_path = variant_ass_path


# ----- LRCLib client -----


def _strip_variant_markers(title: str) -> str:
    """Trim trailing `(Instrumental)` / `[Karaoke]` / etc. from a track title.

    LRCLib/Genius index lyrics once per song regardless of mix; querying with
    a variant suffix drops otherwise-good matches. Called on the query only;
    never mutates the DB title. Returns the original string when no marker
    matches or stripping would yield an empty result.
    """
    stripped = _VARIANT_RE.sub("", title).strip()
    return stripped or title


# Letters that Unicode NFKD won't decompose because they aren't accented
# base letters: Polish ``ł``/``Ł`` (slashed L) and Scandinavian ``ø``/``Ø``
# (slashed O). ``remove_accents`` leaves them alone, so a query like
# ``Kielas`` would never substring-match ``Kiełas`` even after folding
# both sides. Apply this fold *after* ``remove_accents``.
_LATIN_FOLD = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O"})


def _fold_for_match(text: str) -> str:
    """Accent-strip + Latin extension flatten + lowercase. Comparison-only key."""
    return remove_accents(text).translate(_LATIN_FOLD).lower()


# Delimiters that split a multi-artist credit. ``&`` and ``,`` always
# split because they're never part of an artist name; the others
# (``/``, ``x``, ``feat``, ``ft``, ``vs``, ``with``) require surrounding
# whitespace to preserve names like ``AC/DC``, ``Malcolm X``,
# ``X Ambassadors``, and ``Living With Lions``.
_ARTIST_SPLIT_RE = re.compile(
    r"""
        \s*&\s*                              # &
      | \s*,\s*                              # ,
      | \s+/\s+                              # /  (only with surrounding ws)
      | \s+(?:feat\.?|ft\.?|vs\.?|with)\s+   # word delimiters
      | \s+x\s+                              # x  (only with surrounding ws)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _artist_tokens(artist: str) -> list[str]:
    """Folded lowercase tokens from a multi-artist credit string.

    ``"Gibbs & Kiełas"`` -> ``["gibbs", "kielas"]``;
    ``"Eminem feat. Rihanna"`` -> ``["eminem", "rihanna"]``.
    Tokens shorter than two chars are dropped to avoid spurious matches
    against single-letter aliases.
    """
    if not artist:
        return []
    parts = _ARTIST_SPLIT_RE.split(artist.strip())
    return [t for t in (_fold_for_match(p.strip()) for p in parts if p) if len(t) >= 2]


def _artist_matches(artist: str, primary: str, others: list[str] | None = None) -> bool:
    """True when ``artist`` plausibly matches a result's credit list.

    Accepts on (a) folded equality with ``primary``, or (b) any token of
    ``artist`` matching ``primary`` or any name in ``others``. Token
    splitting on ``&``/``,``/``/``/``feat``/``ft``/``vs``/``x``/``with``
    means a query for ``"Gibbs & Kiełas"`` matches a hit credited as
    primary=``"Gibbs"`` with ``"Kiełas"`` featured -- the common shape on
    Genius and Spotify when collaborator metadata is split between
    primary and featured arrays.
    """
    primary_key = _fold_for_match((primary or "").strip())
    if not primary_key:
        return False
    artist_key = _fold_for_match((artist or "").strip())
    if artist_key and artist_key == primary_key:
        return True
    tokens = set(_artist_tokens(artist))
    if not tokens:
        return False
    candidates = {primary_key}
    for other in others or ():
        other_key = _fold_for_match((other or "").strip())
        if other_key:
            candidates.add(other_key)
    return bool(tokens & candidates)


def _split_artist_credits(artist: str) -> list[str]:
    """Return each collaborator from a multi-artist credit as a standalone string.

    ``"Gibbs & Kiełas"`` -> ``["Gibbs", "Kiełas"]``. Casing and diacritics
    are preserved (the splitter is for generating alternative search
    queries, not for comparison). Tokens shorter than two chars are
    dropped to match the matcher's behaviour. Single-artist strings
    return a single-element list, which the candidate generator dedups
    against the original.
    """
    if not artist:
        return []
    parts = _ARTIST_SPLIT_RE.split(artist.strip())
    out: list[str] = []
    for part in parts:
        token = (part or "").strip()
        if len(token) >= 2:
            out.append(token)
    return out


def _candidate_dedup_key(track: str, artist: str) -> tuple[str, str]:
    """Folded (track, artist) tuple used as the dedup key for candidates."""
    return (_fold_for_match(track or ""), _fold_for_match(artist or ""))


# Per-song candidate count cap. Holds total cost of an all-miss song to
# at most ``_CANDIDATE_LIMIT`` rounds against each source. iTunes and
# MusicBrainz are LRU-cached per query; Genius/Spotify/Tekstowo searches
# pay a network round-trip per unique (track, artist) pair.
_CANDIDATE_LIMIT = 8


def _fetch_lrclib(track: str, artist: str, duration: int | float | None) -> str | None:
    """Query LRCLib for syncedLyrics; None when none found or request failed.

    Tries ``/api/get`` first (exact match) and falls back to ``/api/search``
    when /get misses, errors, *or* times out. The timeout fallback matters:
    when the artist tag drifts (yt-dlp's "Gibbs & Kiełas" vs LRCLib's
    "Gibbs"), /api/get hangs rather than returning a fast 404, so isolating
    the call lets /search rescue an otherwise-pinned-as-miss song.
    """
    track = _strip_variant_markers(track)
    get_params: dict[str, str | int] = {"track_name": track, "artist_name": artist}
    if duration:
        get_params["duration"] = int(duration)
    try:
        r = requests.get(f"{LRCLIB_BASE}/api/get", params=get_params, timeout=LRCLIB_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            synced = data.get("syncedLyrics")
            if synced:
                logger.info(
                    "LRCLib: hit /api/get track=%r artist=%r duration=%s",
                    track,
                    artist,
                    duration,
                )
                return synced
    except (requests.RequestException, ValueError) as e:
        logger.info("LRCLib /api/get failed (%s); trying /api/search", e)
    try:
        r = requests.get(
            f"{LRCLIB_BASE}/api/search",
            params={"track_name": track, "artist_name": artist},
            timeout=LRCLIB_TIMEOUT,
        )
        if r.status_code == 200:
            for item in r.json():
                synced = item.get("syncedLyrics")
                if not synced:
                    continue
                if duration is not None:
                    item_duration = item.get("duration")
                    if isinstance(item_duration, (int, float)):
                        delta = abs(float(item_duration) - float(duration))
                        if delta > _LRCLIB_DURATION_TOLERANCE_S:
                            logger.info(
                                "LRCLib: /api/search skip duration mismatch "
                                "(track=%r artist=%r item=%ss target=%ss)",
                                track,
                                artist,
                                item_duration,
                                duration,
                            )
                            continue
                logger.info("LRCLib: hit /api/search track=%r artist=%r", track, artist)
                return synced
    except (requests.RequestException, ValueError) as e:
        logger.warning("LRCLib /api/search failed: %s", e)
        return None
    logger.info("LRCLib: miss track=%r artist=%r duration=%s", track, artist, duration)
    return None


# ----- syncedlyrics-backed providers (Musixmatch, Megalobiz) -----


def _fetch_via_syncedlyrics(track: str, artist: str, providers: list[str]) -> str | None:
    """Common wrapper for syncedlyrics provider lookups.

    Returns synced LRC text on hit, ``None`` when the lib is missing,
    the network call fails, or the providers return nothing.
    """
    if not _SYNCEDLYRICS_AVAILABLE or _syncedlyrics is None:
        return None
    if not track or not artist:
        return None
    query = f"{track} {artist}"
    try:
        result = _syncedlyrics.search(query, providers=providers, save_path=None)
    except Exception:
        logger.warning("syncedlyrics %s: lookup failed", providers, exc_info=True)
        return None
    if not result:
        return None
    text = str(result).strip()
    return text or None


def _fetch_musixmatch(track: str, artist: str) -> str | None:
    """Synced lyrics from Musixmatch via syncedlyrics. Returns LRC or None."""
    return _fetch_via_syncedlyrics(track, artist, ["Musixmatch"])


def _fetch_megalobiz(track: str, artist: str) -> str | None:
    """Synced lyrics from Megalobiz via syncedlyrics. Returns LRC or None."""
    return _fetch_via_syncedlyrics(track, artist, ["Megalobiz"])


# ----- VTT -> LRC bridge for the consensus pool -----


def _vtt_to_lrc(vtt: str) -> str | None:
    """Convert YouTube VTT into an LRC string.

    The VTT-to-ASS path drops the timing precision needed for consensus
    voting (line text only, no per-line cue timestamps), so we re-emit
    the parsed cues as ``[mm:ss.xx]text`` lines. Same dedup rules as
    ``_vtt_to_ass`` apply via ``_parse_vtt_cues``.
    """
    cues = _parse_vtt_cues(vtt)
    if not cues:
        return None
    lines: list[str] = []
    for start, _end, text in cues:
        mm = int(start // 60)
        ss = start - mm * 60
        lines.append(f"[{mm:02d}:{ss:05.2f}]{text}")
    return "\n".join(lines) if lines else None


# ----- Genius client -----


def _fetch_genius(track: str, artist: str) -> str | None:
    """Return plain-text lyrics from Genius, or None on miss / missing token.

    Flow:
      1. GET /search?q=<artist> <track>  (Bearer auth).
      2. Pick the first hit whose ``primary_artist.name`` case-insensitively
         matches ``artist``.
      3. Scrape the public song page and extract text from the lyrics
         containers (``div[data-lyrics-container="true"]``), preserving line
         breaks from ``<br>`` tags and dropping annotation markup.

    Returns None when ``GENIUS_ACCESS_TOKEN`` is empty (opt-in feature), on
    any HTTP failure, or when no artist-matched hit is found.
    """
    if not GENIUS_ACCESS_TOKEN or not track or not artist:
        return None
    query = f"{artist} {_strip_variant_markers(track)}".strip()
    try:
        r = requests.get(
            f"{GENIUS_BASE}/search",
            params={"q": query},
            headers={"Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}"},
            timeout=GENIUS_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        hits = r.json().get("response", {}).get("hits", [])
    except (requests.RequestException, ValueError) as e:
        logger.warning("Genius search failed: %s", e)
        return None
    # Accent-fold both sides so yt-dlp metadata like "Edyta Gorniak"
    # matches Genius's "Edyta Górniak". ``_artist_matches`` also tokenizes
    # the query on "&"/"feat"/"ft"/etc. so a search for "Gibbs & Kiełas"
    # matches a hit credited primary="Gibbs" with featured=["Kiełas"] --
    # the standard shape for Polish rap collabs on Genius.
    url = None
    for hit in hits:
        result = hit.get("result") or {}
        primary = (result.get("primary_artist") or {}).get("name", "")
        featured = [a.get("name", "") for a in (result.get("featured_artists") or [])]
        if _artist_matches(artist, primary, featured):
            url = result.get("url")
            break
    if not url:
        candidates = [
            ((h.get("result") or {}).get("primary_artist") or {}).get("name", "") for h in hits[:3]
        ]
        logger.info("Genius: no artist match for %r (saw primaries: %r)", artist, candidates)
        return None
    try:
        page = requests.get(url, timeout=GENIUS_TIMEOUT)
        if page.status_code != 200:
            return None
    except requests.RequestException as e:
        logger.warning("Genius page fetch failed: %s", e)
        return None
    return _extract_genius_lyrics(page.text)


_GENIUS_SECTION_HEADER_RE = re.compile(r"^\s*\[[^\]]*\]\s*$", re.MULTILINE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Genius occasionally A/B-tests the page layout so a header block —
# "4 Contributors", "Translations", language menus, "Read More", etc. —
# leaks into the lyrics container. Stripping the HTML tags concatenates
# those inline spans into a single nonsense "word"
# (``4 ContributorsTranslationsEnglish``) that wav2vec2 happily pretends
# to align, producing timestamps beyond the song duration that then
# crash libass. Drop any line that looks like page chrome.
#
# The CamelCase boundary `(?=\W|[A-Z]|$)` is deliberate: `\b` only fires
# between a word char and a non-word char, so it misses the glued
# transition `Contributors`→`Translations` where both sides are letters.
_GENIUS_JUNK_LINE_RE = re.compile(
    r"""
    ^\s*
    (?:
        \d+\s*Contributors?(?=\W|[A-Z]|$).*       # "4 Contributors", "4 ContributorsTranslations..."
      | (?:Translations?|Read\s+More)(?=\W|[A-Z]|$).*
      | (?:                                       # bare language-name menu entries
            English|Polski|Polish|Français|French|Español|Spanish|
            Deutsch|German|Italiano|Italian|Português|Portuguese|
            Русский|Russian|日本語|Japanese|中文|Chinese
        )\s*$
    )
    """,
    re.VERBOSE,
)


class _GeniusLyricsParser(HTMLParser):
    """Capture every ``<div data-lyrics-container="true">`` block.

    Genius wraps inline annotations in nested ``<div>`` elements. A naive
    non-greedy regex (``<div ...>(.*?)</div>``) closes on the first nested
    ``</div>`` and silently drops the rest of the verse. We track div depth
    explicitly so the container only closes when its matching ``</div>``
    arrives. Inner element markup is preserved as text so the existing
    post-processing chain (``<br>`` -> newline, tag strip, header drop)
    operates exactly as before.
    """

    def __init__(self):
        super().__init__()
        self._depth = 0
        self._in_container = False
        self._chunks: list[str] = []
        self.containers: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            if not self._in_container and dict(attrs).get("data-lyrics-container") == "true":
                self._in_container = True
                self._depth = 1
                self._chunks = []
                return
            if self._in_container:
                self._depth += 1
                self._chunks.append(self.get_starttag_text() or "")
            return
        if not self._in_container:
            return
        if tag == "br":
            self._chunks.append("\n")
            return
        self._chunks.append(self.get_starttag_text() or "")

    def handle_startendtag(self, tag, attrs):
        if not self._in_container:
            return
        if tag == "br":
            self._chunks.append("\n")
            return
        self._chunks.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag):
        if not self._in_container:
            return
        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self.containers.append("".join(self._chunks))
                self._in_container = False
                self._chunks = []
                return
        self._chunks.append(f"</{tag}>")

    def handle_data(self, data):
        if self._in_container:
            self._chunks.append(data)


def _extract_genius_lyrics(html: str) -> str | None:
    """Pull plain-text lyrics out of a Genius song page.

    Genius wraps lyric blocks in ``data-lyrics-container="true"`` divs and
    uses ``<br>`` for line breaks inside each block. Section headers like
    ``[Verse 1]`` are dropped; they aren't part of the sung content.
    Returns None when no container is found.
    """
    parser = _GeniusLyricsParser()
    parser.feed(html)
    if not parser.containers:
        return None
    lines: list[str] = []
    for raw in parser.containers:
        text = _HTML_TAG_RE.sub("", raw)
        text = _GENIUS_SECTION_HEADER_RE.sub("", text)
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if _GENIUS_JUNK_LINE_RE.match(stripped):
                logger.info("Genius: dropping header junk line %r", stripped[:80])
                continue
            lines.append(stripped)
    return "\n".join(lines) if lines else None


# ----- Tekstowo client -----


# Tekstowo uses the same Polish-ł-aware folding as the artist matcher;
# keep the alias for callers below.
_tekstowo_fold = _fold_for_match


def _fetch_tekstowo(track: str, artist: str) -> str | None:
    """Return plain-text lyrics from tekstowo.pl, or None on miss / network failure.

    Tekstowo.pl indexes >1.6M lyrics with strong coverage of Polish music
    that LRCLib and Genius routinely miss. There is no public API; we
    fetch the general search page (``/szukaj,X.html``), pick the first
    ``<a class="title">`` whose ``title`` attribute matches both the
    searched artist and (substring) the searched track — both folded for
    accents and case — then scrape ``div.inner-text`` from the song page.

    Bot-detector workaround: tekstowo serves a placeholder page when the
    User-Agent is missing or scriptable-looking, so we present a desktop
    Chrome UA. Section headers like ``[Refren]`` / ``[Zwrotka 1]`` are
    dropped via the existing Genius header regex (same syntax).
    """
    if not track or not artist:
        return None
    track_clean = _strip_variant_markers(track)
    query = f"{artist} {track_clean}".strip()
    # ``/szukaj,X.html`` is the general search (redirects to ``/szukaj/X``).
    # The narrower ``/szukaj,wykonawca,X.html`` form currently returns no
    # lyric anchors at all — only the artist landing page.
    search_url = f"{TEKSTOWO_BASE}/szukaj,{quote_plus(_tekstowo_fold(query))}.html"
    try:
        r = requests.get(
            search_url,
            headers={"User-Agent": TEKSTOWO_USER_AGENT},
            timeout=TEKSTOWO_TIMEOUT,
        )
        if r.status_code != 200:
            return None
    except requests.RequestException as e:
        logger.warning("Tekstowo search failed: %s", e)
        return None

    track_key = _tekstowo_fold(track_clean.strip())
    parser = _TekstowoSearchParser(artist, track_key)
    try:
        parser.feed(r.text)
    except Exception:
        logger.exception("Tekstowo search parse crashed")
        return None
    if not parser.match_href:
        return None

    page_url = TEKSTOWO_BASE + parser.match_href
    try:
        page = requests.get(
            page_url,
            headers={"User-Agent": TEKSTOWO_USER_AGENT},
            timeout=TEKSTOWO_TIMEOUT,
        )
        if page.status_code != 200:
            return None
    except requests.RequestException as e:
        logger.warning("Tekstowo page fetch failed: %s", e)
        return None
    return _extract_tekstowo_lyrics(page.text)


class _TekstowoSearchParser(HTMLParser):
    """Pick the first search-result anchor that matches both artist and track.

    Tekstowo's current layout serves song results as
    ``<a class="title" href="/artist-slug/track-slug">Artist - Track</a>``
    — the ``Artist - Track`` label lives in the anchor text, not in a
    ``title`` attribute (which is sometimes present, sometimes not, and
    we don't rely on it). We anchor on the semantic ``class="title"``
    marker rather than the href shape because the URL pattern has shifted
    several times (``/piosenka,artist,track,123.html`` →
    ``/artist-slug/track-slug``).

    Title substring match avoids the failure mode where a query like
    "Edyta Górniak Halo" would otherwise grab the first Górniak song on
    the page even when "Halo" doesn't appear in any result.
    """

    def __init__(self, artist: str, track_key: str) -> None:
        super().__init__()
        self._artist = artist
        self._track_key = track_key
        self._capturing = False
        self._text_buf: list[str] = []
        self._candidate_href: str | None = None
        self.match_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.match_href is not None or tag != "a" or self._capturing:
            return
        attr_dict = dict(attrs)
        cls = (attr_dict.get("class") or "").split()
        if "title" not in cls:
            return
        href = attr_dict.get("href") or ""
        if not href.startswith("/"):
            return
        self._capturing = True
        self._text_buf = []
        self._candidate_href = href

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._text_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._capturing:
            return
        text = " ".join("".join(self._text_buf).split()).strip()
        href = self._candidate_href or ""
        self._capturing = False
        self._text_buf = []
        self._candidate_href = None
        if " - " not in text:
            return
        result_artist, result_track = text.split(" - ", 1)
        if not _artist_matches(self._artist, result_artist.strip()):
            return
        if self._track_key and self._track_key not in _tekstowo_fold(result_track.strip()):
            return
        self.match_href = href


class _TekstowoLyricsParser(HTMLParser):
    """Capture every ``<div class="inner-text">`` block, **skipping the
    translation panel**.

    Same depth-counted approach as ``_GeniusLyricsParser`` because tekstowo
    nests inline annotations (translation toggles, advert blocks) inside
    the lyrics container. ``<br>`` becomes ``\\n``; nested tags are
    flattened to their text content. The container only closes when its
    matching ``</div>`` arrives.

    Translation skip: tekstowo song pages render the lyrics original under
    ``<div class="song-text">`` and (for many songs) an English translation
    under ``<div class="tlumaczenie">``. Both inner contents reuse the
    ``inner-text`` class. Without scoping, a ``<langdetect>`` over the
    concatenated output sees a Polish-then-English mix and the
    ``_is_lyrics_language_mismatch`` guard discards the result on every
    Polish song that happens to have a translation — see Kolorowy wiatr.
    Tracking ``_in_translation`` and ignoring ``inner-text`` while inside
    keeps just the original lyrics text.
    """

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0
        self._in_container = False
        self._chunks: list[str] = []
        # Tekstowo wraps the (optional) translation block in a div with
        # ``class="tlumaczenie"`` (id ``songTranslation``). We track depth
        # so a stray nested div inside the translation panel doesn't
        # accidentally close the outer guard.
        self._in_translation = False
        self._translation_depth = 0
        self.containers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            attr_dict = dict(attrs)
            cls = (attr_dict.get("class") or "").split()
            attr_id = attr_dict.get("id") or ""
            # Enter translation scope: any inner-text below us is a
            # translation, not the original lyrics.
            if not self._in_translation and ("tlumaczenie" in cls or attr_id == "songTranslation"):
                self._in_translation = True
                self._translation_depth = 1
                return
            if self._in_translation:
                self._translation_depth += 1
                return
            if not self._in_container and "inner-text" in cls:
                self._in_container = True
                self._depth = 1
                self._chunks = []
                return
            if self._in_container:
                self._depth += 1
                self._chunks.append(self.get_starttag_text() or "")
            return
        if not self._in_container:
            return
        if tag == "br":
            self._chunks.append("\n")
            return
        self._chunks.append(self.get_starttag_text() or "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._in_container:
            return
        if tag == "br":
            self._chunks.append("\n")
            return
        self._chunks.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._in_translation:
            self._translation_depth -= 1
            if self._translation_depth == 0:
                self._in_translation = False
            return
        if not self._in_container:
            return
        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self.containers.append("".join(self._chunks))
                self._in_container = False
                self._chunks = []
                return
        self._chunks.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._in_container:
            self._chunks.append(data)


def _extract_tekstowo_lyrics(html: str) -> str | None:
    """Pull plain-text lyrics out of a tekstowo.pl song page.

    Drops section headers (``[Refren]``, ``[Zwrotka 1]``, ``[Bridge]``)
    via the same regex Genius uses — the bracket-only-line pattern is
    universal across lyric sites. Returns None when no lyrics container
    is found (404 page, age-gate, or layout drift).
    """
    parser = _TekstowoLyricsParser()
    try:
        parser.feed(html)
    except Exception:
        logger.exception("Tekstowo lyrics parse crashed")
        return None
    if not parser.containers:
        return None
    lines: list[str] = []
    for raw in parser.containers:
        text = _HTML_TAG_RE.sub("", raw)
        text = _GENIUS_SECTION_HEADER_RE.sub("", text)
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lines.append(stripped)
    return "\n".join(lines) if lines else None


# ----- Spotify Color Lyrics client -----
#
# Spotify's lyrics endpoint is a private API powered by Musixmatch and
# requires a user-context access token, which itself requires the
# ``sp_dc`` cookie of a logged-in Premium account. The token is short-
# lived (~1h); we cache it on the LyricsService instance and refresh
# lazily. Operators paste sp_dc into the settings UI; it's read on every
# fetch so a cookie refresh takes effect without a server restart.


def _spotify_lines_to_lrc(lines: list[dict]) -> str | None:
    """Convert Spotify Color Lyrics lines to LRC format.

    Spotify returns ``[{"startTimeMs": "1234", "words": "Hej..."}, ...]``.
    LRC tags are ``[mm:ss.cc]`` (centiseconds), which our parser already
    accepts. Empty ``words`` lines are skipped — Spotify uses them as
    musical-interlude markers; we don't render those.
    """
    out: list[str] = []
    for line in lines:
        try:
            start_ms = int(line.get("startTimeMs") or 0)
        except (TypeError, ValueError):
            continue
        words = (line.get("words") or "").strip()
        if not words:
            continue
        mm = start_ms // 60_000
        ss = (start_ms % 60_000) / 1000.0
        out.append(f"[{mm:02d}:{ss:05.2f}]{words}")
    return "\n".join(out) or None


def _group_spotify_syllables(line_text: str, syllables: list[dict]) -> list[Word] | None:
    """Glue Spotify per-syllable timings into per-token ``Word`` objects.

    Spotify's ``SYLLABLE_SYNCED`` payload lists syllables independently of
    word boundaries. We walk the line tokens left-to-right, consuming
    syllables until their concatenated text covers the current token.
    Each token becomes one ``Word`` with sub-syllable ``WordPart`` parts
    so the ASS renderer can emit one ``\\kf`` per syllable.

    Returns ``None`` when reconstruction fails (token / syllable text
    mismatch) — caller falls back to line-level rendering.
    """
    tokens = line_text.split()
    if not tokens:
        return None
    out: list[Word] = []
    syl_idx = 0
    for tok in tokens:
        accumulated = ""
        token_syls: list[dict] = []
        target = "".join(tok.split())  # whitespace-stripped target
        while syl_idx < len(syllables) and len(accumulated) < len(target):
            syl = syllables[syl_idx]
            text = (syl.get("text") or "").strip()
            if not text:
                syl_idx += 1
                continue
            accumulated += text
            token_syls.append(syl)
            syl_idx += 1
        if not token_syls or accumulated[: len(target)] != target:
            return None
        try:
            ws = int(token_syls[0].get("startTimeMs") or 0) / 1000.0
            we = int(token_syls[-1].get("endTimeMs") or 0) / 1000.0
        except (TypeError, ValueError):
            return None
        if we <= ws:
            return None
        parts: list[WordPart] = []
        for syl in token_syls:
            try:
                ps = int(syl.get("startTimeMs") or 0) / 1000.0
                pe = int(syl.get("endTimeMs") or 0) / 1000.0
            except (TypeError, ValueError):
                continue
            text = (syl.get("text") or "").strip()
            if text and pe > ps:
                parts.append(WordPart(text=text, start=ps, end=pe))
        out.append(
            Word(
                text=tok,
                start=ws,
                end=we,
                parts=tuple(parts) if len(parts) > 1 else None,
            )
        )
    return out


def _spotify_syllable_lines_to_ass(lines: list[dict]) -> str | None:
    """Render SYLLABLE_SYNCED Spotify lines as word-level karaoke ASS.

    Builds a synthetic LRC for line positioning and a flat ``Word`` list
    for ``_words_to_ass_with_k_tags``. Returns ``None`` when no line
    reconstructs cleanly — the caller falls back to ``LINE_SYNCED``-
    style line-level rendering.
    """
    all_words: list[Word] = []
    lrc_lines: list[str] = []
    for line in lines:
        line_text = (line.get("words") or "").strip()
        if not line_text:
            continue
        try:
            start_ms = int(line.get("startTimeMs") or 0)
        except (TypeError, ValueError):
            continue
        syllables = line.get("syllables") or []
        if not syllables:
            return None
        line_words = _group_spotify_syllables(line_text, syllables)
        if line_words is None:
            return None
        all_words.extend(line_words)
        mm = start_ms // 60_000
        ss = (start_ms % 60_000) / 1000.0
        lrc_lines.append(f"[{mm:02d}:{ss:05.2f}]{line_text}")
    if not all_words or not lrc_lines:
        return None
    lrc = "\n".join(lrc_lines)
    return _words_to_ass_with_k_tags(all_words, lrc, params=_anim_params_for_bpm(None))


# ----- LRC parser -----


def _parse_lrc(lrc: str) -> list[tuple[float, str]]:
    """Parse LRC into sorted [(start_seconds, text), ...].

    Handles multi-time lines like `[00:12.34][00:25.67]chorus` by duplicating
    the text for each timestamp. Fractional seconds are interpreted as a
    decimal fraction (so `.45` = 0.45s, `.450` = 0.450s).
    """
    entries: list[tuple[float, str]] = []
    for raw in lrc.splitlines():
        tags = _LRC_TAG.findall(raw)
        if not tags:
            continue
        text = _LRC_TAG.sub("", raw).strip()
        if not text:
            continue
        for mm, ss, frac in tags:
            frac_s = int(frac) / (10 ** len(frac)) if frac else 0.0
            start = int(mm) * 60 + int(ss) + frac_s
            entries.append((start, text))
    entries.sort(key=lambda e: e[0])
    return entries


def _lrc_plain_text(lrc: str) -> str:
    """Tags stripped; one line per LRC entry. For forced-alignment reference."""
    return "\n".join(text for _start, text in _parse_lrc(lrc))


def _format_lrc_tag(t: float) -> str:
    """Render seconds as ``[mm:ss.cc]`` with negative values clamped to zero."""
    t = max(0.0, t)
    mm = int(t) // 60
    ss = int(t) - mm * 60
    cs = int(round((t - int(t)) * 100))
    if cs >= 100:
        ss += 1
        cs = 0
    return f"[{mm:02d}:{ss:02d}.{cs:02d}]"


def _shift_lrc(lrc: str, offset_s: float) -> str:
    """Return ``lrc`` with every ``[mm:ss.cc]`` timestamp shifted by ``offset_s``.

    Used when the aligner detects that LRCLib timestamps sit ahead of the
    actual YouTube audio - shifting the LRC string is the only way to move
    the renderer's ``Dialogue`` events too, since ``_words_to_ass_with_k_tags``
    derives them from ``_parse_lrc(lrc)``. Negative results clamp to zero
    rather than wrap; we'd rather show subs at song start than emit invalid
    LRC tags. Non-tag lines pass through unchanged.
    """
    if not offset_s:
        return lrc

    def replace(match: "re.Match[str]") -> str:
        mm, ss, frac = match.group(1), match.group(2), match.group(3)
        frac_s = int(frac) / (10 ** len(frac)) if frac else 0.0
        return _format_lrc_tag(int(mm) * 60 + int(ss) + frac_s + offset_s)

    return _LRC_TAG.sub(replace, lrc)


def _shift_lrc_per_line(lrc: str, mapping: dict[float, float]) -> str:
    """Rewrite ``[mm:ss.cc]`` tags using a per-orig-time replacement map.

    Each LRC tag whose decoded time matches a key in ``mapping`` (with a
    small epsilon for floating-point round-trips) is replaced by the
    mapped value. Tags without a match pass through unchanged - that
    handles things like ``[ti:Title]`` headers and any LRC times the
    aligner didn't see (e.g. multi-tag lines where only some times got
    shifted upstream).

    Used when the aligner produces a per-line shift table (silence-based
    anchoring with per-verse drift), as a richer cousin of ``_shift_lrc``.
    """
    if not mapping:
        return lrc

    def replace(match: "re.Match[str]") -> str:
        mm, ss, frac = match.group(1), match.group(2), match.group(3)
        frac_s = int(frac) / (10 ** len(frac)) if frac else 0.0
        t = int(mm) * 60 + int(ss) + frac_s
        new_t = next(
            (v for k, v in mapping.items() if abs(k - t) < 0.01),
            None,
        )
        if new_t is None:
            return match.group(0)
        return _format_lrc_tag(new_t)

    return _LRC_TAG.sub(replace, lrc)


def lrc_line_windows(lrc: str) -> list[tuple[float, float, str]]:
    """Parse LRC into ``(line_start, line_end, text)`` triples.

    ``line_end`` is the next line's start; the final line uses
    ``_LAST_LINE_HOLD_S``. Used by the aligner to confine per-line
    SequenceMatcher so repeated phrases can't steal anchors across
    lines.
    """
    entries = _parse_lrc(lrc)
    windows: list[tuple[float, float, str]] = []
    for i, (start, text) in enumerate(entries):
        end = entries[i + 1][0] if i + 1 < len(entries) else start + _LAST_LINE_HOLD_S
        windows.append((start, end, text))
    return windows


def _lrc_from_aligned_lines(words: list[Word], lines: list[str]) -> str | None:
    """Build an LRC from aligned word timings + known line structure.

    Used on the Genius fallback path: Genius gives us plain lyrics with line
    breaks but no timestamps, so after wav2vec2 returns per-word timings
    (1:1 with the reference tokens), we consume one line's worth of tokens
    at a time and use the first aligned word's start as the line's LRC time.
    Returns None when the aligner dropped so many words we can't scaffold
    any line.
    """
    entries: list[str] = []
    idx = 0
    for line in lines:
        tokens = line.split()
        if not tokens:
            continue
        end_idx = min(idx + len(tokens), len(words))
        line_words = words[idx:end_idx]
        idx = end_idx
        if not line_words:
            continue
        start = max(0.0, line_words[0].start)
        mm = int(start // 60)
        ss = start - mm * 60
        entries.append(f"[{mm:02d}:{ss:05.2f}]{line}")
    return "\n".join(entries) if entries else None


_LANGDETECT_MIN_CHARS = 30


def _detect_language(text: str) -> str | None:
    """Best-effort 2-letter language code from text. None on failure.

    Lyrics are an ideal input for text-based detection (hundreds of words of
    clean prose), so a successful classification here lets the aligner skip
    whisperx's slow audio-based detection pass. Returns None when langdetect
    is not installed (optional ``[align]`` extra) or the input is too short
    to classify confidently.
    """
    text = text.strip()
    if len(text) < _LANGDETECT_MIN_CHARS:
        return None
    try:
        import langdetect
    except ImportError:
        return None
    langdetect.DetectorFactory.seed = 0  # deterministic across calls
    try:
        return langdetect.detect(text)
    except langdetect.lang_detect_exception.LangDetectException:
        return None


# First minute is plenty for tempo classification and keeps CPU well under a
# second on the CI/RPi box.
_BPM_ANALYSIS_DURATION_S = 60.0

# Process-lifetime cache for BPM estimates. Multiple render paths
# (line/word/whisper/genius/lrclib variants) each ask for the BPM of the
# same vocals.mp3, which used to repeat the librosa.load + beat_track
# pipeline 3-7x per song. The vocals.mp3 parent directory is the audio
# sha256, so the path is content-addressable and stable for the process
# lifetime. None entries are cached too, so repeated failures don't keep
# retrying.
_bpm_cache: dict[str, float | None] = {}
_bpm_cache_lock = threading.Lock()


def _estimate_bpm(audio_path: str) -> float | None:
    """Best-effort song tempo in BPM, or None if detection fails.

    Used only to pick decorative animation parameters - never in a timing
    path - so any failure falls through to a plain (un-pulsed) render.
    Cached for the process lifetime; see ``_bpm_cache``.
    """
    with _bpm_cache_lock:
        if audio_path in _bpm_cache:
            return _bpm_cache[audio_path]
    try:
        y, sr = librosa.load(audio_path, sr=None, mono=True, duration=_BPM_ANALYSIS_DURATION_S)
        tempo, _beats = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo[0]) if hasattr(tempo, "__len__") else float(tempo)
        logger.info("Estimated BPM %.1f for %s", bpm, audio_path)
        result = bpm if bpm > 0 else None
    except Exception:
        logger.warning("BPM estimation failed for %s", audio_path, exc_info=True)
        result = None
    with _bpm_cache_lock:
        _bpm_cache[audio_path] = result
    return result


# ----- ASS builders -----


@dataclass(frozen=True)
class _AnimParams:
    """Per-word decorative animation knobs, driven by song tempo.

    ``pulse_pct`` is the scale peak as a whole-number percent (100 disables
    the pulse entirely). ``pulse_rise_frac`` is the fraction of the word's
    duration spent scaling up; the remainder eases back to 100%.
    """

    pulse_pct: int
    pulse_rise_frac: float


def _anim_params_for_bpm(bpm: float | None) -> _AnimParams:
    """Map tempo to pulse shape. Unknown tempo = no pulse (plain \\kf fill).

    Classification is deliberately coarse: the pulse is decorative, so being
    one tier off is imperceptible. Fast songs get a bigger, snappier pop
    (smaller rise fraction = sharper attack); ballads get a gentler rise.
    """
    if bpm is None or bpm <= 0:
        return _AnimParams(pulse_pct=100, pulse_rise_frac=0.0)
    if bpm < 80:
        return _AnimParams(pulse_pct=102, pulse_rise_frac=0.35)
    if bpm < 130:
        return _AnimParams(pulse_pct=103, pulse_rise_frac=0.25)
    return _AnimParams(pulse_pct=104, pulse_rise_frac=0.15)


# Colors are &HAABBGGRR. PrimaryColour = unsung (bright white — what you read
# next); SecondaryColour = the \kf wipe target for sung words (mid-grey — the
# "already sang it" fade). Outline/shadow softened vs. the old spec so the
# glyphs feel less chromed.
_ASS_STYLE = (
    "Style: Default,Arial,64,&H00FFFFFF,&H00AAAAAA,&H00000000,&HB0000000,"
    "0,0,0,0,100,100,0,0,1,2,1,2,40,40,80,1"
)


def _ass_header() -> str:
    """Produce the ASS [Script Info]/[V4+ Styles]/[Events] preamble."""
    return (
        "[Script Info]\n"
        f"Title: {ASS_MARKER}\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{_ASS_STYLE}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _format_ass_time(seconds: float) -> str:
    """ASS uses H:MM:SS.cc (centiseconds)."""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


_LAST_LINE_HOLD_S = 5.0

# Multi-line context window: show up to 2 past lines + current + up to 2 future
# lines per Dialogue, with the future cap limited to 5s so a long pause between
# verses doesn't leak spoilers onto the screen.
_CONTEXT_BEFORE = 2
_CONTEXT_AFTER = 2
_CONTEXT_FORWARD_WINDOW_S = 5.0


def _context_window_texts(entries: list[tuple[float, str]], i: int) -> tuple[list[str], list[str]]:
    """Pick the past / future lines visible alongside ``entries[i]``."""
    past = [text for _t, text in entries[max(0, i - _CONTEXT_BEFORE) : i]]
    start_t = entries[i][0]
    future: list[str] = []
    for j in range(i + 1, min(i + 1 + _CONTEXT_AFTER, len(entries))):
        t_j, text_j = entries[j]
        if t_j - start_t > _CONTEXT_FORWARD_WINDOW_S:
            break
        future.append(text_j)
    return past, future


def _render_context_block(past_ass: list[str], current_ass: str, future_ass: list[str]) -> str:
    """Compose the centered multi-line Dialogue body.

    Current line is opaque + bold; past/future are dimmed (alpha 0x80). Middle-
    center alignment (``\\an5``) stacks the block vertically on-screen. Callers
    pass already-escaped / ``\\k``-tagged strings so this helper never re-escapes.
    """
    dim = r"{\alpha&H80&\b0}"
    hot = r"{\alpha&H00&\b1}"
    segments = [f"{dim}{t}" for t in past_ass]
    segments.append(f"{hot}{current_ass}")
    segments.extend(f"{dim}{t}" for t in future_ass)
    return r"{\an5}" + r"\N".join(segments)


def _lrc_to_ass_line_level(lrc: str) -> str | None:
    """Convert LRC to ASS with a centered 5-line context window per entry."""
    entries = _parse_lrc(lrc)
    if not entries:
        return None
    out = [_ass_header()]
    for i, (start, text) in enumerate(entries):
        end = entries[i + 1][0] if i + 1 < len(entries) else start + _LAST_LINE_HOLD_S
        past_raw, future_raw = _context_window_texts(entries, i)
        body = _render_context_block(
            [_escape_ass(t) for t in past_raw],
            _escape_ass(text),
            [_escape_ass(t) for t in future_raw],
        )
        out.append(
            f"Dialogue: 0,{_format_ass_time(start)},{_format_ass_time(end)},"
            f"Default,,0,0,0,,{body}\n"
        )
    return "".join(out)


# Accept words whose timing drifts up to this far outside the LRC line window
# before we distrust the alignment and fall back to static text.
_ALIGNMENT_TOLERANCE_S = 2.0


def _words_to_ass_with_k_tags(
    words: list[Word],
    lrc: str,
    params: _AnimParams | None = None,
) -> str | None:
    """Rebuild ASS with \\kf karaoke tags on the current line, plain text on context lines.

    Aligner output is 1:1 with reference-text tokens (see
    ``map_whisper_to_reference``), so we assign words to LRC entries by
    position - not by timestamp. Time-based matching collapses badly when
    whisper mis-times a region of the song: hundreds of later-line words end
    up stuffed into a single LRC entry's time window. Lines whose aligned
    times don't overlap the LRC window fall back to static text.

    ``params`` controls the decorative per-word pulse; when ``None`` the
    words render as plain \\kf fills with no scaling effect.
    """
    entries = _parse_lrc(lrc)
    if not entries:
        return None
    out = [_ass_header()]
    word_idx = 0
    for i, (start, text) in enumerate(entries):
        end = entries[i + 1][0] if i + 1 < len(entries) else start + _LAST_LINE_HOLD_S
        expected = len(text.split())
        line_words = words[word_idx : word_idx + expected]
        word_idx += expected
        if line_words and _words_overlap_window(line_words, start, end):
            current_ass = " ".join(_k_token(w, start, params) for w in line_words)
        else:
            # Whisper's per-word timings for this line drifted too far from
            # the LRC window to trust their absolute values. Keep per-word
            # highlighting by re-anchoring the line's tokens to the LRC
            # window with uniform durations - sync accuracy falls back to
            # line-level granularity (same as pre-whisperx baseline), but
            # the user still sees a smooth wipe instead of a frozen line.
            current_ass = _uniform_k_tokens(text.split(), start, end, params)
        past_raw, future_raw = _context_window_texts(entries, i)
        body = _render_context_block(
            [_escape_ass(t) for t in past_raw],
            current_ass,
            [_escape_ass(t) for t in future_raw],
        )
        out.append(
            f"Dialogue: 0,{_format_ass_time(start)},{_format_ass_time(end)},"
            f"Default,,0,0,0,,{body}\n"
        )
    return "".join(out)


def _words_overlap_window(words: list[Word], start: float, end: float) -> bool:
    """True when the aligned words' span overlaps the LRC line window."""
    first = words[0].start
    last = words[-1].end
    return last >= start - _ALIGNMENT_TOLERANCE_S and first <= end + _ALIGNMENT_TOLERANCE_S


def _uniform_k_tokens(
    tokens: list[str], start: float, end: float, params: "_AnimParams | None" = None
) -> str:
    """Render ``tokens`` as \\kf tags spread evenly across ``[start, end]``.

    Fallback path when whisper's per-word timings can't be trusted for a
    line. The line itself is still time-accurate (LRC window), only the
    intra-line word wipe speed is estimated uniformly.
    """
    if not tokens:
        return ""
    duration = max(end - start, 0.01)
    per = duration / len(tokens)
    return " ".join(
        _k_token(Word(text=t, start=start + per * i, end=start + per * (i + 1)), start, params)
        for i, t in enumerate(tokens)
    )


def _k_token(word: Word, line_start_s: float = 0.0, params: _AnimParams | None = None) -> str:
    """ASS karaoke tags for one word.

    Emits ``\\kf`` (smooth left-to-right color wipe) instead of the older
    ``\\k`` (instant flip) so sung words fade into the secondary colour
    rather than popping. When ``word.parts`` is set we emit one ``\\kf``
    per part (per-char on the WhisperX path, per-syllable on the Whisper
    fallback path); otherwise a single ``\\kf`` covers the whole word.

    When ``params.pulse_pct`` exceeds 100 we also wrap the first glyph
    group in a ``\\t`` scale transform that pulses up and releases
    across the word's full time window - one pulse per word, not per
    part, so multi-syllable words don't strobe. ``\\t`` offsets are in
    milliseconds from the enclosing Dialogue event's start, hence the
    ``line_start_s`` argument.
    """
    pulse_tag = _pulse_tag(word, line_start_s, params)
    fills = _kf_fills(word)
    # Splice the pulse override into the first fill's override block so
    # that \kf and \t sit inside a single {...} group - libass parses this
    # cleanly and it keeps the tag count down for long lines.
    if pulse_tag and fills:
        fills = fills.replace("{", "{" + pulse_tag, 1)
    return fills


def _kf_fills(word: Word) -> str:
    """Sequence of ``{\\kfN}text`` groups for ``word`` (one per part)."""
    parts = word.parts
    if not parts:
        dur_cs = max(1, int(round((word.end - word.start) * 100)))
        return f"{{\\kf{dur_cs}}}{_escape_ass(word.text)}"
    out = []
    for p in parts:
        dur_cs = max(1, int(round((p.end - p.start) * 100)))
        out.append(f"{{\\kf{dur_cs}}}{_escape_ass(p.text)}")
    return "".join(out)


def _pulse_tag(word: Word, line_start_s: float, params: _AnimParams | None) -> str:
    """Build the ``\\t`` scale-pulse override for one word, empty when disabled.

    The pulse spans the whole word (not per-part) so multi-part words
    get a single scale bump that lines up with the word's onset instead
    of strobing once per character.
    """
    if params is None or params.pulse_pct <= 100:
        return ""
    dur_cs = max(1, int(round((word.end - word.start) * 100)))
    total_ms = dur_cs * 10
    off_ms = max(0, int(round((word.start - line_start_s) * 1000)))
    rise_ms = max(1, int(total_ms * params.pulse_rise_frac))
    rise_end = off_ms + rise_ms
    fall_end = off_ms + total_ms
    pct = params.pulse_pct
    return (
        f"\\t({off_ms},{rise_end},\\fscx{pct}\\fscy{pct})"
        f"\\t({rise_end},{fall_end},\\fscx100\\fscy100)"
    )


def _escape_ass(text: str) -> str:
    # ASS override blocks are delimited by curly braces; backslash introduces
    # special sequences (e.g. \\N forces a hard line break inside a Dialogue
    # event). Third-party lyrics text reaches this path via Genius/Musixmatch/
    # Megalobiz scrapes, so escape both the brace and the backslash to avoid
    # rendering injection.
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


# ----- atomic write -----


def _write_ass_atomic(song_path: str, ass_content: str, *, target_path: str | None = None) -> None:
    """Write an ASS file atomically so a concurrent read never sees partial data.

    Default target is the canonical ``<stem>.ass``; pass ``target_path`` to
    write a per-source variant (``<stem>.<source>.ass``) without changing
    the canonical file. The temporary file is created in the same
    directory as the target so ``os.replace`` stays a true rename (atomic
    on the same filesystem; would degrade to a copy across mounts).
    """
    target = target_path or _ass_path(song_path)
    directory = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(suffix=".ass", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(ass_content)
        os.replace(tmp, target)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


# ----- VTT conversion -----


def _try_write_ass_from_vtt_path(song_path: str, vtt_path: str) -> bool:
    """Convert a specific VTT file to ASS. Returns True on success."""
    try:
        with open(vtt_path, encoding="utf-8") as f:
            vtt = f.read()
    except OSError as e:
        logger.warning("failed to read %s: %s", vtt_path, e)
        return False
    ass = _vtt_to_ass(vtt)
    if not ass:
        return False
    _write_ass_atomic(song_path, ass)
    return True


def _vtt_lang_from_filename(song_path: str, vtt_path: str) -> str | None:
    """Extract the language code segment from <stem>.<lang>.vtt, or None."""
    stem, _ext = os.path.splitext(song_path)
    basename = os.path.basename(stem)
    name = os.path.basename(vtt_path)
    if not name.startswith(basename + ".") or not name.endswith(".vtt"):
        return None
    return name[len(basename) + 1 : -len(".vtt")] or None


def _lang_base(lang: str | None) -> str:
    """Normalize a language tag to its primary subtag (e.g. `pl-PL` -> `pl`)."""
    if not lang:
        return ""
    return lang.split("-", 1)[0].split("_", 1)[0].lower()


def _pick_best_vtt(song_path: str, preferred_lang: str | None = None) -> str | None:
    """Return the most suitable <stem>*.vtt path, or None if none exist.

    Preference order:
      1. Files whose primary lang subtag matches ``preferred_lang`` (typically
         the track's DB-stored ``language``) — US-14 P1.
      2. Manual uploads (no `-orig` / `-auto` suffix).
      3. Shorter language codes (e.g. `pl` beats `pl-PL`).
      4. Alphabetical as a final tiebreaker.
    """
    stem, _ext = os.path.splitext(song_path)
    directory = os.path.dirname(stem) or "."
    basename = os.path.basename(stem)
    preferred_base = _lang_base(preferred_lang)
    candidates = []
    for name in os.listdir(directory):
        if not name.endswith(".vtt"):
            continue
        if not name.startswith(basename + "."):
            continue
        lang = name[len(basename) + 1 : -len(".vtt")]
        is_auto = "-orig" in lang or lang.endswith("-auto") or "auto" in lang
        # False sorts before True, so "lang matches preferred" goes first.
        lang_matches = bool(preferred_base) and _lang_base(lang) == preferred_base
        candidates.append(
            (not lang_matches, is_auto, len(lang), lang, os.path.join(directory, name))
        )
    if not candidates:
        logger.info(
            "VTT: no candidates for %s (preferred_lang=%s)",
            basename,
            preferred_lang,
        )
        return None
    candidates.sort()
    chosen = candidates[0]
    logger.info(
        "VTT: picked lang=%s from %d candidate(s) for %s (preferred_lang=%s, auto=%s)",
        chosen[3],
        len(candidates),
        basename,
        preferred_lang,
        chosen[1],
    )
    return chosen[4]


def _parse_vtt_cues(vtt: str) -> list[tuple[float, float, str]]:
    """Parse WEBVTT into [(start_s, end_s, text)]. Inline tags stripped."""
    cues: list[tuple[float, float, str]] = []
    lines = vtt.splitlines()
    i = 0
    while i < len(lines):
        m = _VTT_CUE.search(lines[i])
        if not m:
            i += 1
            continue
        start = _vtt_ts_to_s(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _vtt_ts_to_s(m.group(5), m.group(6), m.group(7), m.group(8))
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            cleaned = _VTT_TAG.sub("", lines[i]).strip()
            if cleaned:
                text_lines.append(cleaned)
            i += 1
        if text_lines:
            cues.append((start, end, " ".join(text_lines)))
    return _dedup_rolling_cues(cues)


def _dedup_rolling_cues(
    cues: list[tuple[float, float, str]],
) -> list[tuple[float, float, str]]:
    """YouTube auto-captions repeat each line in a sliding window.

    If cue N's text starts with cue N-1's text, drop cue N-1 and keep only the
    fullest version. This collapses the sliding-window noise back into one line.
    """
    if not cues:
        return cues
    out: list[tuple[float, float, str]] = []
    for cue in cues:
        if out and cue[2].startswith(out[-1][2]):
            # Replace previous with the more complete version.
            out[-1] = (out[-1][0], cue[1], cue[2])
        else:
            out.append(cue)
    return out


def _vtt_ts_to_s(hh: str, mm: str, ss: str, ms: str) -> float:
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000


def _vtt_to_ass(vtt: str) -> str | None:
    cues = _parse_vtt_cues(vtt)
    if not cues:
        return None
    out = [_ass_header()]
    for start, end, text in cues:
        out.append(
            f"Dialogue: 0,{_format_ass_time(start)},{_format_ass_time(end)},"
            f"Default,,0,0,0,,{_escape_ass(text)}\n"
        )
    return "".join(out)


# ----- ownership check + cleanup -----


def _user_owned_ass(song_path: str) -> bool:
    """True when <stem>.ass exists but was NOT produced by PiKaraoke."""
    path = _ass_path(song_path)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(512)
    except OSError:
        return True  # safer to assume user-owned if unreadable
    return ASS_MARKER not in head


def _cleanup_yt_vtt(song_path: str, db=None) -> None:
    """Remove <stem>*.vtt after conversion and drop the matching DB rows.

    info.json is intentionally preserved here: it's the canonical YouTube
    provenance record (registered as an ``info_json`` artifact) and the
    only signal the backfill reseed path can use without re-hitting
    YouTube. The lyrics pipeline has no business touching it.
    """
    stem, _ext = os.path.splitext(song_path)
    directory = os.path.dirname(stem) or "."
    basename = os.path.basename(stem)
    try:
        entries = os.listdir(directory)
    except OSError:
        entries = []
    for name in entries:
        if not name.startswith(basename + "."):
            continue
        if name.endswith(".vtt"):
            try:
                os.unlink(os.path.join(directory, name))
            except OSError as e:
                logger.warning("failed to remove %s: %s", name, e)

    if db is None:
        return
    try:
        song_id = db.get_song_id_by_path(song_path)
    except Exception:
        logger.exception("failed to look up song_id for artifact cleanup: %s", song_path)
        return
    if song_id is None:
        return
    try:
        db.delete_artifacts_by_role(song_id, "vtt")
    except Exception:
        logger.exception("failed to unregister vtt artifacts for song_id=%s", song_id)


# ----- Demucs stem coupling -----
#
# Whisper alignment quality improves materially when fed clean vocals instead
# of the full mix. When whisper is configured, LyricsService triggers a Demucs
# prewarm at download time (see `_prewarm_stems`) and waits briefly for the
# vocals stem to appear before running the aligner.

_STEM_WAIT_TIMEOUT_S = 120.0
_STEM_WAIT_POLL_S = 2.0


def _alignment_audio_path(song_path: str) -> str | None:
    """Return vocals MP3 path when Demucs has finished encoding, else None.

    Cache is keyed by ``resolve_audio_source`` (sibling ``.m4a`` when present),
    matching how ``prewarm`` populates it. Querying with the raw mp4 would miss.

    Only the MP3 tier is returned; the WAV tier is short-lived (removed as
    soon as MP3 encoding finishes) and returning it can cause whisperx to
    open a file that is deleted moments later. Waiting for MP3 is safe:
    whisperx accepts it transparently.
    """
    try:
        from pikaraoke.lib.demucs_processor import (
            get_cache_key,
            get_cached_stems,
            resolve_audio_source,
        )

        cached = get_cached_stems(get_cache_key(resolve_audio_source(song_path)))
    except Exception as e:
        logger.warning("stem lookup failed for %s: %s", song_path, e)
        return None
    if not cached:
        return None
    vocals_path, _instr_path, fmt = cached
    if fmt != "mp3":
        return None
    return vocals_path


def _wait_for_alignment_audio(song_path: str) -> str:
    """Poll for a cached vocals stem up to `_STEM_WAIT_TIMEOUT_S`, else fall back.

    Fallback is the audio-only sibling (``resolve_audio_source``) so we don't
    feed whisperx a video-only mp4 from the split-streams download flow.
    """
    stem = _alignment_audio_path(song_path)
    if stem is not None:
        return stem
    deadline = time.monotonic() + _STEM_WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(_STEM_WAIT_POLL_S)
        stem = _alignment_audio_path(song_path)
        if stem is not None:
            return stem
    logger.info(
        "stems not ready within %.0fs for %s; aligning on raw mix",
        _STEM_WAIT_TIMEOUT_S,
        os.path.basename(song_path),
    )
    from pikaraoke.lib.demucs_processor import resolve_audio_source

    return resolve_audio_source(song_path)


def _prewarm_stems(song_path: str) -> None:
    """Fire-and-forget Demucs prewarm so alignment has vocals ready."""
    try:
        from pikaraoke.lib.demucs_processor import prewarm

        prewarm(song_path)
    except Exception as e:
        logger.warning("Demucs prewarm failed for %s: %s", song_path, e)


def _whisper_fallback_enabled() -> bool:
    """Honour WHISPER_FALLBACK_MODEL opt-out. Default: enabled."""
    return _resolve_whisper_model().lower() not in _WHISPER_OPT_OUT


def _get_whisper_model():
    """Lazy-load faster-whisper once per process. Returns None if unavailable.

    Import is deferred so a missing ``faster-whisper`` install doesn't
    crash the rest of the app — songs just fall through to the
    "no lyrics source" warning instead.
    """
    if not _whisper_fallback_enabled():
        return None
    with _whisper_model_lock:
        if _whisper_model_cache[0] is not None:
            return _whisper_model_cache[0]
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            logger.warning(
                "Whisper fallback: faster-whisper not installed; "
                "no auto-lyrics will be generated for songs missing curated captions."
            )
            return None
        model_name = _resolve_whisper_model()
        try:
            model = WhisperModel(model_name, device="auto", compute_type="int8")
        except Exception:
            logger.exception(
                "Whisper fallback: failed to load model %r on device=auto; disabling",
                model_name,
            )
            return None
        _whisper_model_cache[0] = model
        logger.info("Whisper fallback: loaded model=%s", model_name)
        return model


# Whisper language codes -> pyphen locale. Pyphen needs region-qualified
# codes for some locales ("pl_PL" not "pl"). For languages pyphen doesn't
# ship a dictionary for (e.g. Japanese/Chinese - handled differently
# anyway), _syllabify returns None and the renderer falls back to a
# single \kf per word.
_PYPHEN_LANG_MAP = {
    "pl": "pl_PL",
    "en": "en_US",
    "de": "de_DE",
    "es": "es_ES",
    "fr": "fr_FR",
    "it": "it_IT",
    "pt": "pt_PT",
    "nl": "nl_NL",
    "sv": "sv",
    "no": "nb_NO",
    "nn": "nn_NO",
    "da": "da_DK",
    "fi": "fi_FI",
    "cs": "cs_CZ",
    "sk": "sk_SK",
    "ru": "ru_RU",
    "uk": "uk_UA",
    "hu": "hu_HU",
    "ro": "ro_RO",
    "hr": "hr_HR",
    "sl": "sl_SI",
    "lt": "lt_LT",
    "lv": "lv_LV",
    "et": "et_EE",
    "ca": "ca",
    "gl": "gl",
    "eu": "eu",
    "bg": "bg",
    "el": "el_GR",
    "tr": "tr_TR",
}

_pyphen_cache: dict[str, object] = {}


def _syllabify(word: str, language: str | None) -> list[tuple[int, int]] | None:
    """Return syllable spans ``[(start_char_idx, end_char_idx), ...]`` for ``word``.

    Uses pyphen (Hunspell hyphenation dictionaries). Returns ``None`` if
    pyphen isn't installed, the language has no dictionary, or the word
    has no internal hyphenation point (monosyllabic / too short / all
    non-alphabetic). Callers treat ``None`` as "render this word as a
    single ``\\kf``".

    Spans are half-open: ``word[start:end]`` is the syllable's text. This
    keeps the caller arithmetic consistent with Python slicing and lets
    us reconstruct the full word via concatenation.
    """
    if not word or not language:
        return None
    try:
        import pyphen
    except ImportError:
        return None
    locale = _PYPHEN_LANG_MAP.get(language.lower(), language)
    dic = _pyphen_cache.get(locale)
    if dic is None:
        try:
            if locale not in pyphen.LANGUAGES:
                # Try the bare language code as a last-ditch fallback.
                short = language.lower().split("_")[0]
                if short in pyphen.LANGUAGES:
                    locale = short
                else:
                    _pyphen_cache[locale] = False  # sentinel: no dict
                    return None
            dic = pyphen.Pyphen(lang=locale)
            _pyphen_cache[locale] = dic
        except (KeyError, OSError):
            _pyphen_cache[locale] = False
            return None
    if dic is False:
        return None
    positions = dic.positions(word)  # type: ignore[union-attr]
    if not positions:
        return None
    spans: list[tuple[int, int]] = []
    prev = 0
    for p in positions:
        spans.append((prev, p))
        prev = p
    spans.append((prev, len(word)))
    return spans


def _syllable_parts(
    word: str, language: str | None, start: float, end: float
) -> tuple["WordPart", ...] | None:
    """Build per-syllable ``WordPart`` spans for ``word``.

    Used by the Whisper-ASR fallback where we only have word-level
    timings: pyphen splits the word, then the word's duration is sliced
    proportionally to each syllable's character length. Returns ``None``
    for monosyllabic words or unsupported languages so the renderer
    falls back to a single ``\\kf`` per word (same UX as before).
    """
    spans = _syllabify(word, language)
    if not spans or len(spans) < 2:
        return None
    total_chars = spans[-1][1] - spans[0][0]
    if total_chars <= 0:
        return None
    duration = max(end - start, 0.01)
    parts: list[WordPart] = []
    cursor = start
    for i, (a, b) in enumerate(spans):
        text = word[a:b]
        if not text:
            continue
        if i == len(spans) - 1:
            part_end = end
        else:
            part_end = cursor + duration * (b - a) / total_chars
        parts.append(WordPart(text=text, start=cursor, end=part_end))
        cursor = part_end
    return tuple(parts) if len(parts) >= 2 else None


def _lrc_from_whisper_segments(segments) -> str:
    """Build a synthetic LRC (one line per whisper segment).

    ``_words_to_ass_with_k_tags`` needs an LRC string to locate line
    boundaries and per-line start times; whisper's segments approximate
    spoken lines well enough for that. Text is stripped; empty segments
    are dropped so a leading silence doesn't produce a blank LRC line
    that would offset word-to-line assignment.
    """
    lines = []
    for seg in segments:
        text = (getattr(seg, "text", "") or "").strip()
        if not text:
            continue
        start = float(getattr(seg, "start", 0.0) or 0.0)
        minutes = int(start // 60)
        seconds = start - minutes * 60
        lines.append(f"[{minutes:02d}:{seconds:05.2f}]{text}")
    return "\n".join(lines)
