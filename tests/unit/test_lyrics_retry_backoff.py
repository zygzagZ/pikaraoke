"""Unit tests for ``pikaraoke.lib.lyrics_retry_backoff``.

The backoff schedule is shared between the per-source orchestrator
(Phase 2) and the canonical-pipeline failure cache (Phase 3); both
expect identical day counts for a given attempt count, so the schedule
itself is locked in here.
"""

import datetime

import pytest

from pikaraoke.lib.lyrics_retry_backoff import (
    _BACKOFF_DAYS,
    compute_next_retry_at,
    is_in_backoff,
)

UTC = datetime.timezone.utc


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "attempt,expected_days",
    [
        (1, 1),
        (2, 3),
        (3, 7),
        (4, 30),
        (5, 30),
        (10, 30),
    ],
)
def test_schedule_steps(attempt, expected_days):
    out = compute_next_retry_at(attempt, now=_now())
    delta = datetime.datetime.fromisoformat(out) - _now()
    assert delta == datetime.timedelta(days=expected_days)


def test_attempt_zero_or_negative_clamps_to_first_window():
    # A miscount must never yield a past timestamp.
    delta = datetime.datetime.fromisoformat(compute_next_retry_at(0, now=_now())) - _now()
    assert delta == datetime.timedelta(days=_BACKOFF_DAYS[0])
    delta = datetime.datetime.fromisoformat(compute_next_retry_at(-3, now=_now())) - _now()
    assert delta == datetime.timedelta(days=_BACKOFF_DAYS[0])


def test_returns_iso_format_with_second_precision():
    out = compute_next_retry_at(1, now=_now())
    # Round-trips with no microseconds component.
    parsed = datetime.datetime.fromisoformat(out)
    assert parsed.microsecond == 0
    assert parsed.tzinfo is not None


def test_is_in_backoff_future_true():
    next_retry = compute_next_retry_at(1, now=_now())
    assert is_in_backoff(next_retry, now=_now()) is True


def test_is_in_backoff_past_false():
    past = (_now() - datetime.timedelta(seconds=1)).isoformat(timespec="seconds")
    assert is_in_backoff(past, now=_now()) is False


def test_is_in_backoff_missing_value_false():
    assert is_in_backoff(None, now=_now()) is False
    assert is_in_backoff("", now=_now()) is False


def test_is_in_backoff_unparseable_false():
    # Corrupted row degrades to "not gated" so retries self-heal.
    assert is_in_backoff("not-a-date", now=_now()) is False


def test_is_in_backoff_naive_timestamp_assumed_utc():
    # Legacy rows without a tz suffix should still be honored.
    naive = datetime.datetime(2026, 5, 6, 12, 0, 0).isoformat(timespec="seconds")
    assert is_in_backoff(naive, now=_now()) is True
