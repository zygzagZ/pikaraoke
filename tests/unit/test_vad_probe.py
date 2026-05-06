"""Unit tests for pikaraoke.lib.vad_probe.

The module wraps two probes (silero VAD + ffmpeg silencedetect) and
merges their outputs into the ``[(onset, next_onset), ...]`` shape the
DP solver in ``lyrics_align`` consumes. Tests stub both probes at the
internal helper boundary so we don't run torch or ffmpeg in CI.
"""

from unittest.mock import patch

import pytest

from pikaraoke.lib import vad_probe


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Each test starts with an unloaded model so mocked load paths don't leak."""
    saved_model = vad_probe._model
    saved_unavailable = vad_probe._model_unavailable
    vad_probe._model = None
    vad_probe._model_unavailable = False
    yield
    vad_probe._model = saved_model
    vad_probe._model_unavailable = saved_unavailable


class TestVADProbe:
    def test_returns_sorted_monotonic_pairs(self, monkeypatch):
        # silero finds three phrases; silencedetect finds none new.
        monkeypatch.setattr(
            vad_probe,
            "_silero_onset_starts",
            lambda _p: ([10.0, 20.0, 30.0], 60.0),
        )
        monkeypatch.setattr(vad_probe, "_silencedetect_onset_pairs", lambda _p: [])
        out = vad_probe.list_vocal_onsets("/tmp/song.mp3")
        # Each entry's next_onset is the following onset; final entry's
        # next_onset is the audio duration.
        assert out == [(10.0, 20.0), (20.0, 30.0), (30.0, 60.0)]
        # Strictly monotonic onsets.
        for a, b in zip(out, out[1:]):
            assert b[0] > a[0]

    def test_collapses_adjacent_speech_segments_within_threshold(self, monkeypatch):
        # silero's per-phrase 10.0 and silencedetect's silence_end at 10.3
        # describe the same phrase - merged dedup collapses them.
        monkeypatch.setattr(
            vad_probe,
            "_silero_onset_starts",
            lambda _p: ([10.0, 30.0], 60.0),
        )
        # silencedetect adds a 10.3 onset (sustain 5s) and a real 50.0.
        monkeypatch.setattr(
            vad_probe,
            "_silencedetect_onset_pairs",
            lambda _p: [(10.3, 15.3), (50.0, 60.0)],
        )
        out = vad_probe.list_vocal_onsets("/tmp/song.mp3")
        starts = [o for o, _ in out]
        # 10.0 and 10.3 collapse; 30.0 stays; 50.0 added from silencedetect.
        assert starts == [10.0, 30.0, 50.0]

    def test_filters_silencedetect_microspike(self, monkeypatch):
        # silencedetect can emit silence_end[i] just before silence_start[i+1]
        # ("0.001s of audio between two silences"). The candidate-filter
        # would treat that as a long-sustain anchor because the next merged
        # onset sits seconds away. Drop these at the source.
        monkeypatch.setattr(vad_probe, "_silero_onset_starts", lambda _p: ([], 60.0))
        monkeypatch.setattr(
            vad_probe,
            "_silencedetect_onset_pairs",
            # First pair's audio sustain is 1ms - synthetic noise spike.
            lambda _p: [(20.0, 20.001), (40.0, 50.0)],
        )
        out = vad_probe.list_vocal_onsets("/tmp/song.mp3")
        starts = [o for o, _ in out]
        assert 20.0 not in starts
        assert 40.0 in starts

    def test_falls_back_to_silencedetect_when_silero_import_fails(self, monkeypatch):
        # _ensure_model returns None (silero not installed). list_vocal_onsets
        # still returns silencedetect-derived anchors with the silencedetect
        # last audio_end as the duration upper bound.
        monkeypatch.setattr(vad_probe, "_ensure_model", lambda: None)
        monkeypatch.setattr(
            vad_probe,
            "_silero_onset_starts",
            lambda _p: ([], None),
        )
        monkeypatch.setattr(
            vad_probe,
            "_silencedetect_onset_pairs",
            lambda _p: [(5.0, 15.0), (25.0, 40.0)],
        )
        out = vad_probe.list_vocal_onsets("/tmp/song.mp3")
        assert out == [(5.0, 25.0), (25.0, 40.0)]

    def test_uses_module_level_model_singleton(self, monkeypatch):
        # _ensure_model loads silero at most once per process. A second
        # call returns the cached model without re-importing.
        load_calls = {"n": 0}

        class _StubModel:
            def reset_states(self):
                pass

        def _fake_load_silero_vad():
            load_calls["n"] += 1
            return _StubModel()

        # Patch silero_vad import inside _ensure_model.
        import sys
        from types import SimpleNamespace

        fake_silero = SimpleNamespace(load_silero_vad=_fake_load_silero_vad)
        monkeypatch.setitem(sys.modules, "silero_vad", fake_silero)
        first = vad_probe._ensure_model()
        second = vad_probe._ensure_model()
        assert first is not None
        assert first is second
        assert load_calls["n"] == 1

    def test_silero_chunk_loop_dedups_overlap_boundary(self, monkeypatch):
        # Pin the chunk-loop logic in _silero_onset_starts: 60 s windows
        # with 5 s overlap mean an onset near the *end* of chunk N (in
        # the last 5 s) reappears at the *start* of chunk N+1 (in the
        # first 5 s). The loop must drop the duplicate from chunk N+1
        # via the `local < _VAD_CHUNK_OVERLAP_S / 2` guard, and must
        # call ``model.reset_states()`` once per chunk so silero's
        # continuous-state inference doesn't carry across chunk
        # boundaries (the bug Pivot 2's commit message calls out).
        import sys
        from types import SimpleNamespace

        reset_calls = {"n": 0}

        class _StubModel:
            def reset_states(self):
                reset_calls["n"] += 1

        monkeypatch.setattr(vad_probe, "_ensure_model", lambda: _StubModel())

        # Fake librosa.load → 90s of zeros at 16 kHz.
        import numpy as np

        audio_samples = np.zeros(90 * vad_probe._VAD_SAMPLE_RATE, dtype=np.float32)
        fake_librosa = SimpleNamespace(
            load=lambda *_a, **_kw: (audio_samples, vad_probe._VAD_SAMPLE_RATE)
        )
        monkeypatch.setitem(sys.modules, "librosa", fake_librosa)

        # Fake torch.from_numpy → return something with __len__ matching.
        class _FakeTensor:
            def __init__(self, arr):
                self._arr = arr

            def __len__(self):
                return len(self._arr)

            def __getitem__(self, key):
                return _FakeTensor(self._arr[key])

            def float(self):
                return self

        fake_torch = SimpleNamespace(from_numpy=lambda a: _FakeTensor(a))
        monkeypatch.setitem(sys.modules, "torch", fake_torch)

        # Fake get_speech_timestamps: per-chunk responses keyed on call
        # count. Chunk 0 emits an onset at local 56s (inside the
        # overlap region); chunk 1 emits the same onset at local 1s
        # (which is absolute 56s) plus a unique one at local 30s
        # (absolute 85s). The loop must keep chunk 0's 56s, drop chunk
        # 1's 56s duplicate, and keep chunk 1's 85s.
        chunk_responses = [
            [{"start": 10.0}, {"start": 50.0}, {"start": 56.0}],
            [{"start": 1.0}, {"start": 30.0}],
        ]
        call_count = {"n": 0}

        def _fake_get_speech_timestamps(_chunk, _model, **_kw):
            i = call_count["n"]
            call_count["n"] += 1
            if i < len(chunk_responses):
                return chunk_responses[i]
            return []

        fake_silero = SimpleNamespace(get_speech_timestamps=_fake_get_speech_timestamps)
        monkeypatch.setitem(sys.modules, "silero_vad", fake_silero)

        starts, duration = vad_probe._silero_onset_starts("/tmp/song.mp3")
        assert starts == [
            10.0,
            50.0,
            56.0,
            85.0,
        ], f"chunk-loop dedup failed: got {starts}, expected [10, 50, 56, 85]"
        assert duration == pytest.approx(90.0, abs=0.01)
        # 90 s audio with 60 s chunks stepped by 55 s: chunks at offsets
        # 0, 55. The chunk at offset 110 has 0 audio left. Two chunks =
        # two reset_states calls.
        assert (
            reset_calls["n"] == 2
        ), f"expected reset_states once per chunk; got {reset_calls['n']} calls"

    def test_ensure_model_idempotent(self, monkeypatch):
        # Belt-and-braces: explicit prewarm-then-prewarm doesn't re-load.
        load_calls = {"n": 0}

        class _StubModel:
            def reset_states(self):
                pass

        def _fake_load_silero_vad():
            load_calls["n"] += 1
            return _StubModel()

        import sys
        from types import SimpleNamespace

        monkeypatch.setitem(
            sys.modules, "silero_vad", SimpleNamespace(load_silero_vad=_fake_load_silero_vad)
        )
        for _ in range(5):
            vad_probe._ensure_model()
        assert load_calls["n"] == 1


class TestVadOnsetCache:
    """``list_vocal_onsets`` writes through to ``audio_feature_cache`` when
    ``audio_sha256`` + cache callables are supplied; cache hits skip the
    silero / silencedetect probe entirely."""

    def test_cache_hit_skips_probe(self, monkeypatch):
        kv: dict[str, str] = {"audio_vad_onsets:abc": '{"onsets": [[1.5, 3.0], [3.0, 60.0]]}'}
        # Probe must NOT be called on a hit.
        monkeypatch.setattr(
            vad_probe,
            "_silero_onset_starts",
            lambda _p: pytest.fail("silero probed on cache hit"),
        )
        monkeypatch.setattr(
            vad_probe,
            "_silencedetect_onset_pairs",
            lambda _p: pytest.fail("silencedetect probed on cache hit"),
        )
        out = vad_probe.list_vocal_onsets(
            "/tmp/song.mp3",
            audio_sha256="abc",
            cache_get=kv.get,
            cache_set=lambda k, v: kv.update({k: v}),
        )
        assert out == [(1.5, 3.0), (3.0, 60.0)]

    def test_cache_miss_writes_through(self, monkeypatch):
        kv: dict[str, str] = {}
        monkeypatch.setattr(
            vad_probe,
            "_silero_onset_starts",
            lambda _p: ([10.0, 20.0], 30.0),
        )
        monkeypatch.setattr(vad_probe, "_silencedetect_onset_pairs", lambda _p: [])
        out = vad_probe.list_vocal_onsets(
            "/tmp/song.mp3",
            audio_sha256="abc",
            cache_get=kv.get,
            cache_set=lambda k, v: kv.update({k: v}),
        )
        assert out == [(10.0, 20.0), (20.0, 30.0)]
        # Verify the result was persisted under the audio sha key.
        assert "audio_vad_onsets:abc" in kv

    def test_cache_args_omitted_runs_uncached(self, monkeypatch):
        # Without cache callables the function runs the probe and never
        # touches the cache (back-compat path for CLI / tests).
        monkeypatch.setattr(
            vad_probe,
            "_silero_onset_starts",
            lambda _p: ([5.0], 10.0),
        )
        monkeypatch.setattr(vad_probe, "_silencedetect_onset_pairs", lambda _p: [])
        out = vad_probe.list_vocal_onsets("/tmp/song.mp3")
        assert out == [(5.0, 10.0)]
