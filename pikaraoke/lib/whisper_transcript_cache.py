"""Whisper ASR transcript cache for the lyrics pipeline.

The lyrics pipeline runs Whisper twice for the same vocals stem when the
canonical path falls all the way through to the ASR fallback: once inside
the consensus voter (``_run_whisper_for_consensus``), once again as the
last-ditch ``_try_whisper_fallback``. Library scans replay this on every
startup for songs whose .ass never landed. This module caches the
transcript itself, keyed by ``(audio_sha256, model_name)``, so the second
caller and every future scan get a free hit.

Cache shape mirrors the language-ID probe cache (``lyrics_audio_probe``):
free functions taking ``cache_get`` / ``cache_set`` callables, decoupled
from ``KaraokeDatabase``. Stored as a small JSON blob in the ``metadata``
KV table under prefix ``whisper_transcript:``. Compact JSON keys
(``l``/``lrc``/``w``/``t``/``s``/``e``) keep value size sane for songs
with thousands of words.

``parts`` (per-syllable sub-spans) are NOT cached - they're cheap to
re-derive via ``_syllable_parts(text, lang, start, end)`` and would
roughly double the cached blob.

Implicit invalidation: model bumps (``tiny`` -> ``base``,
``large-v3-turbo`` low-RAM auto-downgrade) change the ``model_name``
component of the key, so cached transcripts from a different model never
return as hits.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pikaraoke.lib.lyrics import Word

logger = logging.getLogger(__name__)

_PREFIX = "whisper_transcript"


def _cache_key(audio_sha256: str, model_name: str) -> str:
    return f"{_PREFIX}:{audio_sha256}:{model_name}"


@dataclass(frozen=True)
class CachedTranscript:
    language: str | None
    lrc: str
    words: tuple["Word", ...]


def read_cached_transcript(
    cache_get: Callable[[str], str | None],
    audio_sha256: str,
    model_name: str,
) -> CachedTranscript | None:
    """Return the cached transcript or ``None`` on miss / corruption.

    Word ``parts`` are re-derived from ``(text, language, start, end)``
    via ``_syllable_parts`` on the read side - they were not cached.
    """
    raw = cache_get(_cache_key(audio_sha256, model_name))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    lang = data.get("l")
    lrc = data.get("lrc")
    raw_words = data.get("w")
    if not isinstance(lrc, str) or not isinstance(raw_words, list):
        return None

    from pikaraoke.lib.lyrics import Word, _syllable_parts

    words: list[Word] = []
    for entry in raw_words:
        if not isinstance(entry, dict):
            return None
        text = entry.get("t")
        start = entry.get("s")
        end = entry.get("e")
        if (
            not isinstance(text, str)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
        ):
            return None
        parts = _syllable_parts(
            text, lang if isinstance(lang, str) else None, float(start), float(end)
        )
        words.append(Word(text=text, start=float(start), end=float(end), parts=parts))
    return CachedTranscript(
        language=lang if isinstance(lang, str) else None,
        lrc=lrc,
        words=tuple(words),
    )


def write_cached_transcript(
    cache_set: Callable[[str, str], None],
    audio_sha256: str,
    model_name: str,
    *,
    language: str | None,
    lrc: str,
    words: "list[Word] | tuple[Word, ...]",
) -> None:
    """Persist the transcript. Failures log + swallow - cache writes are
    best-effort and must never fail the pipeline."""
    payload = {
        "l": language,
        "lrc": lrc,
        "w": [{"t": w.text, "s": w.start, "e": w.end} for w in words],
    }
    try:
        cache_set(_cache_key(audio_sha256, model_name), json.dumps(payload))
    except Exception:
        logger.exception(
            "%s: failed to cache sha=%s model=%s",
            _PREFIX,
            audio_sha256[:12],
            model_name,
        )
