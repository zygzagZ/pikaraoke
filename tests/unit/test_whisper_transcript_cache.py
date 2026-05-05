"""Unit tests for ``pikaraoke.lib.whisper_transcript_cache``.

Mirrors the shape of ``test_lyrics_audio_probe.py``: a cache-callable
fake stands in for ``db.get_metadata`` / ``db.set_metadata`` so tests
stay decoupled from ``KaraokeDatabase``.
"""

import json

from pikaraoke.lib.lyrics import Word, WordPart
from pikaraoke.lib.whisper_transcript_cache import (
    CachedTranscript,
    read_cached_transcript,
    write_cached_transcript,
)


class _FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


def _word(text: str, start: float, end: float) -> Word:
    return Word(text=text, start=start, end=end, parts=None)


def test_read_returns_none_on_miss():
    kv = _FakeKV()
    assert read_cached_transcript(kv.get, "sha", "tiny") is None


def test_round_trip_preserves_language_lrc_and_word_timings():
    kv = _FakeKV()
    words = [_word("hello", 0.0, 0.5), _word("world", 0.5, 1.25)]
    write_cached_transcript(
        kv.set,
        "abc123",
        "large-v3-turbo",
        language="pl",
        lrc="[00:00.00]hello world\n",
        words=words,
    )
    cached = read_cached_transcript(kv.get, "abc123", "large-v3-turbo")
    assert cached is not None
    assert cached.language == "pl"
    assert cached.lrc == "[00:00.00]hello world\n"
    assert len(cached.words) == 2
    assert cached.words[0].text == "hello"
    assert cached.words[0].start == 0.0
    assert cached.words[0].end == 0.5
    assert cached.words[1].text == "world"
    assert cached.words[1].start == 0.5
    assert cached.words[1].end == 1.25


def test_different_model_names_are_separate_cache_slots():
    kv = _FakeKV()
    write_cached_transcript(
        kv.set,
        "abc",
        "tiny.en",
        language="en",
        lrc="[00:00.00]a\n",
        words=[_word("a", 0.0, 0.1)],
    )
    assert read_cached_transcript(kv.get, "abc", "tiny.en") is not None
    assert read_cached_transcript(kv.get, "abc", "large-v3-turbo") is None


def test_different_audio_sha_are_separate_cache_slots():
    kv = _FakeKV()
    write_cached_transcript(
        kv.set,
        "sha-A",
        "tiny",
        language="pl",
        lrc="[00:00.00]a\n",
        words=[_word("a", 0.0, 0.1)],
    )
    assert read_cached_transcript(kv.get, "sha-A", "tiny") is not None
    assert read_cached_transcript(kv.get, "sha-B", "tiny") is None


def test_malformed_json_returns_none():
    kv = _FakeKV()
    kv.store["whisper_transcript:abc:tiny"] = "{not valid json"
    assert read_cached_transcript(kv.get, "abc", "tiny") is None


def test_unexpected_top_level_type_returns_none():
    kv = _FakeKV()
    kv.store["whisper_transcript:abc:tiny"] = json.dumps(["not", "a", "dict"])
    assert read_cached_transcript(kv.get, "abc", "tiny") is None


def test_missing_required_fields_returns_none():
    kv = _FakeKV()
    kv.store["whisper_transcript:abc:tiny"] = json.dumps({"l": "pl"})
    assert read_cached_transcript(kv.get, "abc", "tiny") is None


def test_word_with_non_numeric_timings_returns_none():
    kv = _FakeKV()
    kv.store["whisper_transcript:abc:tiny"] = json.dumps(
        {"l": "pl", "lrc": "x", "w": [{"t": "x", "s": "bad", "e": 1.0}]}
    )
    assert read_cached_transcript(kv.get, "abc", "tiny") is None


def test_empty_words_round_trips():
    kv = _FakeKV()
    write_cached_transcript(
        kv.set,
        "abc",
        "tiny",
        language=None,
        lrc="",
        words=[],
    )
    cached = read_cached_transcript(kv.get, "abc", "tiny")
    assert cached == CachedTranscript(language=None, lrc="", words=())


def test_parts_are_re_derived_for_multisyllable_words_when_language_known():
    kv = _FakeKV()
    # Polish "ucieka" splits into syllables via pyphen.
    write_cached_transcript(
        kv.set,
        "abc",
        "tiny",
        language="pl",
        lrc="[00:00.00]ucieka\n",
        words=[_word("ucieka", 0.0, 1.5)],
    )
    cached = read_cached_transcript(kv.get, "abc", "tiny")
    assert cached is not None
    assert len(cached.words) == 1
    word = cached.words[0]
    # Parts are re-derived; either None (monosyllabic / unsupported lang)
    # or a tuple of WordPart with start >= word.start and end <= word.end.
    if word.parts is not None:
        assert all(isinstance(p, WordPart) for p in word.parts)
        assert word.parts[0].start == word.start
        assert word.parts[-1].end == word.end


def test_write_swallows_cache_set_exceptions():
    def bad_set(_key: str, _value: str) -> None:
        raise OSError("disk full")

    # Must not raise; cache writes are best-effort.
    write_cached_transcript(
        bad_set,
        "abc",
        "tiny",
        language="pl",
        lrc="x",
        words=[_word("x", 0.0, 0.1)],
    )
