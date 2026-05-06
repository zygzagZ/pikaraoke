"""Unit tests for ``pikaraoke.lib.audio_feature_cache``."""

import json

from pikaraoke.lib.audio_feature_cache import (
    CACHE_MISS,
    read_bpm,
    read_vad_onsets,
    write_bpm,
    write_vad_onsets,
)


class _FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


# ----- BPM -----


def test_read_bpm_miss_returns_sentinel():
    kv = _FakeKV()
    assert read_bpm(kv.get, "sha") is CACHE_MISS


def test_round_trip_positive_bpm():
    kv = _FakeKV()
    write_bpm(kv.set, "abc", 120.5)
    assert read_bpm(kv.get, "abc") == 120.5


def test_round_trip_cached_none_is_distinct_from_miss():
    # ``_estimate_bpm`` returns None for instrumentals / decode failures —
    # caching that result short-circuits future re-attempts.
    kv = _FakeKV()
    write_bpm(kv.set, "abc", None)
    assert read_bpm(kv.get, "abc") is None
    # Different sha is still a miss.
    assert read_bpm(kv.get, "different") is CACHE_MISS


def test_bpm_corrupted_json_returns_miss():
    kv = _FakeKV()
    kv.store["audio_bpm:abc"] = "{not valid json"
    assert read_bpm(kv.get, "abc") is CACHE_MISS


def test_bpm_unexpected_type_returns_miss():
    kv = _FakeKV()
    kv.store["audio_bpm:abc"] = json.dumps({"bpm": "fast"})
    assert read_bpm(kv.get, "abc") is CACHE_MISS


def test_bpm_write_swallows_cache_set_exceptions():
    def bad_set(_key: str, _value: str) -> None:
        raise OSError("disk full")

    write_bpm(bad_set, "abc", 120.0)


# ----- VAD onsets -----


def test_read_vad_miss_returns_sentinel():
    kv = _FakeKV()
    assert read_vad_onsets(kv.get, "sha") is CACHE_MISS


def test_round_trip_vad_onsets():
    kv = _FakeKV()
    onsets = [(0.5, 1.2), (3.0, 0.8), (10.5, 2.0)]
    write_vad_onsets(kv.set, "abc", onsets)
    assert read_vad_onsets(kv.get, "abc") == onsets


def test_round_trip_empty_vad_onsets():
    # An empty list is a valid cached "no vocals detected" result; must
    # round-trip distinct from CACHE_MISS so we don't re-probe.
    kv = _FakeKV()
    write_vad_onsets(kv.set, "abc", [])
    out = read_vad_onsets(kv.get, "abc")
    assert out == []
    assert out is not CACHE_MISS


def test_vad_corrupted_json_returns_miss():
    kv = _FakeKV()
    kv.store["audio_vad_onsets:abc"] = "}}"
    assert read_vad_onsets(kv.get, "abc") is CACHE_MISS


def test_vad_malformed_entry_returns_miss():
    kv = _FakeKV()
    kv.store["audio_vad_onsets:abc"] = json.dumps({"onsets": [[0.0]]})
    assert read_vad_onsets(kv.get, "abc") is CACHE_MISS


def test_vad_non_numeric_timings_returns_miss():
    kv = _FakeKV()
    kv.store["audio_vad_onsets:abc"] = json.dumps({"onsets": [["zero", 1.0]]})
    assert read_vad_onsets(kv.get, "abc") is CACHE_MISS


def test_vad_write_swallows_cache_set_exceptions():
    def bad_set(_key: str, _value: str) -> None:
        raise OSError("disk full")

    write_vad_onsets(bad_set, "abc", [(0.0, 1.0)])
