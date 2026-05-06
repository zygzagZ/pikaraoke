"""Canonical lyrics-pipeline failure cache.

When the LRCLib -> consensus -> Whisper-fallback ladder runs end-to-end
and produces no .ass, today nothing is recorded. The next library scan
sees no ``ass_auto`` artifact, flags the song for reprocess, and runs
the same ladder over (LRCLib timeout, Megalobiz refused, Whisper ~1x
realtime CPU) — every time the app starts.

This module persists "we tried, it didn't work" in the ``metadata`` KV
table keyed by ``audio_sha256`` (so re-importing the same audio file
hits the cache; bit-flipping the source breaks the key and re-runs).
``library_scanner._verify_integrity`` consults the cache before adding
a song to ``reprocess_paths``; ``_try_write_ass_tiered`` clears it on
the first successful canonical write so a song that newly succeeds
doesn't carry a stale failure marker.

Sibling of ``subtitle_jobs.next_retry_at``: same backoff schedule
(``lyrics_retry_backoff``), one source of truth per surface. The
orchestrator's per-source backoff and this whole-pipeline backoff are
independent — variant fetches can keep running while the canonical
``<stem>.ass`` waits out its window, and vice versa.
"""

import datetime
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from pikaraoke.lib.lyrics_retry_backoff import compute_next_retry_at, is_in_backoff

logger = logging.getLogger(__name__)

_PREFIX = "lyrics_pipeline_failed"


def _cache_key(audio_sha256: str) -> str:
    return f"{_PREFIX}:{audio_sha256}"


@dataclass(frozen=True)
class PipelineFailure:
    failed_at: str
    next_retry_at: str
    attempts: int
    code: str | None


def read_failure(
    cache_get: Callable[[str], str | None], audio_sha256: str
) -> PipelineFailure | None:
    """Return the cached failure record or ``None`` on miss / corruption."""
    raw = cache_get(_cache_key(audio_sha256))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    failed_at = data.get("failed_at")
    next_retry_at = data.get("next_retry_at")
    attempts = data.get("attempts")
    if not isinstance(failed_at, str) or not isinstance(next_retry_at, str):
        return None
    if not isinstance(attempts, int):
        return None
    code = data.get("code")
    return PipelineFailure(
        failed_at=failed_at,
        next_retry_at=next_retry_at,
        attempts=attempts,
        code=code if isinstance(code, str) else None,
    )


def record_failure(
    cache_get: Callable[[str], str | None],
    cache_set: Callable[[str, str], None],
    audio_sha256: str,
    *,
    error_code: str | None,
    now: datetime.datetime | None = None,
) -> None:
    """Bump attempt count and reschedule ``next_retry_at`` for ``audio_sha256``.

    Reads the prior record (if any), increments ``attempts``, and writes
    the new entry. Failures of the cache write itself are logged and
    swallowed — the retry gate degrades open rather than crashing the
    pipeline thread that's already on its failure path.
    """
    existing = read_failure(cache_get, audio_sha256)
    attempts = (existing.attempts if existing else 0) + 1
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "failed_at": now.isoformat(timespec="seconds"),
        "next_retry_at": compute_next_retry_at(attempts, now=now),
        "attempts": attempts,
        "code": error_code,
    }
    try:
        cache_set(_cache_key(audio_sha256), json.dumps(payload))
    except Exception:
        logger.exception(
            "%s: failed to persist failure record sha=%s",
            _PREFIX,
            audio_sha256[:12],
        )


def clear_failure(cache_set: Callable[[str, str], None], audio_sha256: str) -> None:
    """Drop the failure marker so the next scan can re-attempt freely.

    Implemented as a write of an empty-string sentinel rather than a
    DELETE: ``set_metadata`` is the only KV write API exposed, and
    ``read_failure`` already treats empty strings as miss. Cheaper than
    growing a ``delete_metadata`` API for one caller.
    """
    try:
        cache_set(_cache_key(audio_sha256), "")
    except Exception:
        logger.exception(
            "%s: failed to clear failure record sha=%s",
            _PREFIX,
            audio_sha256[:12],
        )


def is_failure_in_backoff(
    failure: PipelineFailure | None, *, now: datetime.datetime | None = None
) -> bool:
    """True when the recorded failure's ``next_retry_at`` is still in the future."""
    if failure is None:
        return False
    return is_in_backoff(failure.next_retry_at, now=now)
