"""Unit tests for ``pikaraoke.lib.lyrics_pipeline_failure_cache``."""

import datetime
import json

from pikaraoke.lib.lyrics_pipeline_failure_cache import (
    PipelineFailure,
    clear_failure,
    is_failure_in_backoff,
    read_failure,
    record_failure,
)

UTC = datetime.timezone.utc


class _FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


def _fixed_now() -> datetime.datetime:
    return datetime.datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


def test_read_returns_none_on_miss():
    kv = _FakeKV()
    assert read_failure(kv.get, "sha") is None


def test_record_failure_writes_initial_attempt():
    kv = _FakeKV()
    record_failure(kv.get, kv.set, "abc", error_code="whisper_no_words", now=_fixed_now())
    rec = read_failure(kv.get, "abc")
    assert rec is not None
    assert rec.attempts == 1
    assert rec.code == "whisper_no_words"
    # next_retry_at == failed_at + 1 day (first attempt)
    delta = datetime.datetime.fromisoformat(rec.next_retry_at) - _fixed_now()
    assert delta == datetime.timedelta(days=1)


def test_record_failure_increments_attempts():
    kv = _FakeKV()
    record_failure(kv.get, kv.set, "abc", error_code="whisper_no_words", now=_fixed_now())
    record_failure(kv.get, kv.set, "abc", error_code="whisper_no_words", now=_fixed_now())
    record_failure(kv.get, kv.set, "abc", error_code="whisper_no_words", now=_fixed_now())
    rec = read_failure(kv.get, "abc")
    assert rec is not None
    assert rec.attempts == 3
    # third attempt: 7 days
    delta = datetime.datetime.fromisoformat(rec.next_retry_at) - _fixed_now()
    assert delta == datetime.timedelta(days=7)


def test_clear_failure_makes_subsequent_read_return_none():
    kv = _FakeKV()
    record_failure(kv.get, kv.set, "abc", error_code="x", now=_fixed_now())
    assert read_failure(kv.get, "abc") is not None
    clear_failure(kv.set, "abc")
    assert read_failure(kv.get, "abc") is None


def test_different_audio_sha_are_isolated():
    kv = _FakeKV()
    record_failure(kv.get, kv.set, "sha-A", error_code="x", now=_fixed_now())
    assert read_failure(kv.get, "sha-A") is not None
    assert read_failure(kv.get, "sha-B") is None


def test_malformed_json_returns_none():
    kv = _FakeKV()
    kv.store["lyrics_pipeline_failed:abc"] = "{not valid json"
    assert read_failure(kv.get, "abc") is None


def test_missing_required_fields_returns_none():
    kv = _FakeKV()
    kv.store["lyrics_pipeline_failed:abc"] = json.dumps({"failed_at": "2026-01-01T00:00:00+00:00"})
    assert read_failure(kv.get, "abc") is None


def test_is_failure_in_backoff_future_true():
    now = _fixed_now()
    rec = PipelineFailure(
        failed_at=now.isoformat(timespec="seconds"),
        next_retry_at=(now + datetime.timedelta(days=1)).isoformat(timespec="seconds"),
        attempts=1,
        code="x",
    )
    assert is_failure_in_backoff(rec, now=now) is True


def test_is_failure_in_backoff_past_false():
    now = _fixed_now()
    rec = PipelineFailure(
        failed_at=(now - datetime.timedelta(days=2)).isoformat(timespec="seconds"),
        next_retry_at=(now - datetime.timedelta(days=1)).isoformat(timespec="seconds"),
        attempts=1,
        code="x",
    )
    assert is_failure_in_backoff(rec, now=now) is False


def test_is_failure_in_backoff_none_false():
    assert is_failure_in_backoff(None, now=_fixed_now()) is False


def test_record_failure_swallows_cache_set_exceptions():
    def bad_set(_key: str, _value: str) -> None:
        raise OSError("disk full")

    def empty_get(_key: str) -> str | None:
        return None

    # Must not raise; cache writes are best-effort.
    record_failure(empty_get, bad_set, "abc", error_code="x", now=_fixed_now())


def test_clear_failure_swallows_cache_set_exceptions():
    def bad_set(_key: str, _value: str) -> None:
        raise OSError("disk full")

    clear_failure(bad_set, "abc")
