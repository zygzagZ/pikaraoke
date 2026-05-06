"""Disk-persistent cache for deterministic audio features.

BPM estimation and VAD onset detection are pure functions of the audio
bytes, but the lyrics pipeline recomputes them every alignment / fallback
attempt — costing librosa load + beat-tracking and silero / silencedetect
passes that can take seconds per song. This module persists results in
the ``metadata`` KV table keyed by ``audio_sha256`` so a re-run on the
same audio is free.

Same callable-injection shape as ``lyrics_audio_probe`` and
``whisper_transcript_cache``: free functions taking ``cache_get`` /
``cache_set``, decoupled from ``KaraokeDatabase``.

Cached ``None`` for BPM is meaningful — ``_estimate_bpm`` returns None
when librosa can't track a beat (instrumental, percussion-only, decode
failure), and re-running on the same audio would just fail again. The
read API distinguishes "no entry" from "cached None" via a
``CACHE_MISS`` sentinel — same pattern the probe cache uses.
"""

import json
import logging
from collections.abc import Callable
from typing import Final

logger = logging.getLogger(__name__)

_BPM_PREFIX = "audio_bpm"
_VAD_PREFIX = "audio_vad_onsets"


class _Sentinel:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<CACHE_MISS>"


CACHE_MISS: Final = _Sentinel()


def _bpm_key(audio_sha256: str) -> str:
    return f"{_BPM_PREFIX}:{audio_sha256}"


def _vad_key(audio_sha256: str) -> str:
    return f"{_VAD_PREFIX}:{audio_sha256}"


def read_bpm(
    cache_get: Callable[[str], str | None], audio_sha256: str
) -> "float | None | _Sentinel":
    """Return the cached BPM, ``None`` for a cached "couldn't detect"
    verdict, or ``CACHE_MISS`` when nothing is cached yet."""
    raw = cache_get(_bpm_key(audio_sha256))
    if not raw:
        return CACHE_MISS
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return CACHE_MISS
    if not isinstance(data, dict) or "bpm" not in data:
        return CACHE_MISS
    bpm = data["bpm"]
    if bpm is None:
        return None
    if isinstance(bpm, (int, float)):
        return float(bpm)
    return CACHE_MISS


def write_bpm(
    cache_set: Callable[[str, str], None],
    audio_sha256: str,
    bpm: float | None,
) -> None:
    """Persist a BPM verdict (``None`` is a meaningful negative cache).

    Failures of the cache write itself are logged and swallowed."""
    try:
        cache_set(_bpm_key(audio_sha256), json.dumps({"bpm": bpm}))
    except Exception:
        logger.exception("%s: failed to cache sha=%s", _BPM_PREFIX, audio_sha256[:12])


def read_vad_onsets(
    cache_get: Callable[[str], str | None], audio_sha256: str
) -> "list[tuple[float, float]] | _Sentinel":
    """Return cached VAD onsets ``[(start, duration), ...]`` or
    ``CACHE_MISS``. An empty list is a valid cached result.

    On corruption returns ``CACHE_MISS`` so the row self-heals on the
    next compute pass.
    """
    raw = cache_get(_vad_key(audio_sha256))
    if not raw:
        return CACHE_MISS
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return CACHE_MISS
    if not isinstance(data, dict) or not isinstance(data.get("onsets"), list):
        return CACHE_MISS
    out: list[tuple[float, float]] = []
    for entry in data["onsets"]:
        if not isinstance(entry, list) or len(entry) != 2:
            return CACHE_MISS
        start, dur = entry
        if not isinstance(start, (int, float)) or not isinstance(dur, (int, float)):
            return CACHE_MISS
        out.append((float(start), float(dur)))
    return out


def write_vad_onsets(
    cache_set: Callable[[str, str], None],
    audio_sha256: str,
    onsets: list[tuple[float, float]],
) -> None:
    """Persist VAD onsets. Failures are logged and swallowed."""
    payload = {"onsets": [[s, d] for s, d in onsets]}
    try:
        cache_set(_vad_key(audio_sha256), json.dumps(payload))
    except Exception:
        logger.exception("%s: failed to cache sha=%s", _VAD_PREFIX, audio_sha256[:12])
