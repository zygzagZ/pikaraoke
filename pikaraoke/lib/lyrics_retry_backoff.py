"""Retry-backoff schedule shared by the per-source orchestrator and the
canonical-pipeline failure cache.

Both gates need to answer "when can this fail again?" with the same
ladder so an operator's intuition holds across both surfaces. The
schedule escalates 1d -> 3d -> 7d -> 30d, capped: a song that has
failed four or more times is re-attempted at most monthly. A manual
"Sync Now" / per-source retry button bypasses the gate.

ISO-8601 strings (UTC, second precision) are used for ``next_retry_at``
columns and metadata KV values to match the format already in
``subtitle_jobs.next_retry_at`` and other cache shapes in the codebase.
"""

import datetime

# Per-attempt backoff window, indexed by ``attempt_count - 1``.
# ``attempt_count`` is the post-increment count after the failed run,
# so ``attempt_count == 1`` means "this song failed once" and waits 1
# day. Last entry is the cap (every attempt past index N reuses 30d).
_BACKOFF_DAYS: tuple[int, ...] = (1, 3, 7, 30)


def compute_next_retry_at(attempt_count: int, *, now: datetime.datetime | None = None) -> str:
    """Return ``next_retry_at`` ISO-8601 (UTC) for the given attempt count.

    ``attempt_count`` is the count AFTER the failed attempt (>= 1).
    Values <= 0 fall back to the first-attempt window (1 day) so a
    miscount can't yield a past timestamp.

    ``now`` injection is for tests; production callers omit it.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    idx = max(0, min(attempt_count - 1, len(_BACKOFF_DAYS) - 1))
    return (now + datetime.timedelta(days=_BACKOFF_DAYS[idx])).isoformat(timespec="seconds")


def is_in_backoff(next_retry_at: str | None, *, now: datetime.datetime | None = None) -> bool:
    """True when the recorded ``next_retry_at`` is still in the future.

    Returns False on missing / unparseable timestamps - those degrade to
    "not gated" so a corrupted row self-heals on the next attempt
    instead of permanently silencing retries.
    """
    if not next_retry_at:
        return False
    try:
        target = datetime.datetime.fromisoformat(next_retry_at)
    except ValueError:
        return False
    if target.tzinfo is None:
        target = target.replace(tzinfo=datetime.timezone.utc)
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    return target > now
