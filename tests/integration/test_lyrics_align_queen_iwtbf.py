"""End-to-end alignment regression for Queen - I Want To Break Free.

The song has a long instrumental break around 2:50-3:30 and repeated
backing-vocal "I want to break free" entries scattered through the
song. Pins the DP solver's behaviour on a third musical pattern (a
saxophone-led break with vocal callbacks) so weight retunes that
break this song fail CI.

NOTE: this fixture has a ~46s instrumental intro before the first
vocal onset (45.774s), which exceeds ``_LEADING_SILENCE_MAX_S = 30s``.
The orchestrator (``_detect_per_line_starts``) deliberately bails on
that to avoid wrong-anchor cascades; that bail itself is the contract
checked by ``test_orchestrator_bails_on_long_leading_instrumental``
below. The DP solver tests call ``_align_lines_to_anchors_dp``
directly to pin the third musical pattern without tripping the
leading-silence gate.
"""

import json
from pathlib import Path

import pytest

from pikaraoke.lib import lyrics_align, vad_probe
from pikaraoke.lib.lyrics import lrc_line_windows

FIXTURES = Path(__file__).parent.parent / "fixtures" / "queen_iwtbf"


@pytest.fixture
def queen_iwtbf_inputs(monkeypatch):
    onsets = json.loads((FIXTURES / "vocal_onsets.json").read_text())["onsets"]
    lrc = (FIXTURES / "lyrics.lrc").read_text()
    monkeypatch.setattr(
        vad_probe,
        "list_vocal_onsets",
        lambda _path: [(e["onset"], e["next_onset"]) for e in onsets],
    )
    return lrc_line_windows(lrc)


@pytest.fixture
def queen_iwtbf_dp_inputs():
    onset_data = json.loads((FIXTURES / "vocal_onsets.json").read_text())
    onsets = [(e["onset"], e["next_onset"]) for e in onset_data["onsets"]]
    lrc = lrc_line_windows((FIXTURES / "lyrics.lrc").read_text())
    return lrc, onsets, onset_data["audio_duration_s"]


def test_orchestrator_bails_on_long_leading_instrumental(queen_iwtbf_inputs):
    """The orchestrator must return None for a song whose first vocal
    onset sits past ``_LEADING_SILENCE_MAX_S = 30s``. This pins the
    leading-silence gate against a real fixture; if a refactor lets
    Queen IWTBF reach the DP via the orchestrator, the wrong-anchor
    risk that the gate exists to prevent has returned.
    """
    result = lyrics_align._detect_per_line_starts("/dev/null", queen_iwtbf_inputs)
    assert result is None, (
        "orchestrator should bail on Queen IWTBF (first onset 45.774s > "
        "_LEADING_SILENCE_MAX_S=30s); got a non-None result"
    )


def test_dp_anchors_most_lines_on_third_pattern(queen_iwtbf_dp_inputs):
    """DP run directly on Queen IWTBF must anchor the bulk of the LRC
    lines. This pins DP behaviour on a saxophone-break + repeated-
    backing-vocal pattern that neither Total Eclipse nor Mam Tę Moc
    exercises. Threshold: at least 80% of lines anchored.
    """
    lrc, onsets, _ = queen_iwtbf_dp_inputs
    result = lyrics_align._align_lines_to_anchors_dp(lrc, onsets)
    assert result is not None, "DP must not bail on the Queen fixture"
    assignment, _residuals = result
    anchored = sum(1 for a in assignment if a is not None)
    text_lines = sum(1 for _, _, t in lrc if t.strip())
    assert anchored / text_lines >= 0.8, (
        f"DP only anchored {anchored}/{text_lines} text lines "
        f"({anchored / text_lines:.0%}); expected ≥80% on this fixture"
    )


def test_dp_assignments_within_audio_duration(queen_iwtbf_dp_inputs):
    """Every DP-assigned anchor time must land inside the audio file."""
    lrc, onsets, duration = queen_iwtbf_dp_inputs
    result = lyrics_align._align_lines_to_anchors_dp(lrc, onsets)
    assert result is not None
    assignment, _ = result
    anchored = [a for a in assignment if a is not None]
    assert all(0 <= t <= duration for t in anchored), (
        f"anchor outside audio bounds [0, {duration}]: "
        f"{[t for t in anchored if not (0 <= t <= duration)]}"
    )


def test_dp_assignments_are_monotonic(queen_iwtbf_dp_inputs):
    """Anchored timestamps must be monotonically non-decreasing in LRC
    order (line N+k cannot be sung before line N). The DP's tempo-jump
    cost should make backwards anchoring unaffordable.
    """
    lrc, onsets, _ = queen_iwtbf_dp_inputs
    result = lyrics_align._align_lines_to_anchors_dp(lrc, onsets)
    assert result is not None
    assignment, _ = result
    last = -1.0
    for i, a in enumerate(assignment):
        if a is None:
            continue
        assert a >= last - 0.01, (
            f"DP anchored line {i} at {a:.2f}s, before previous "
            f"anchored line at {last:.2f}s — monotonicity violated"
        )
        last = a


def test_grader_keeps_clean_song_on_fast_path(queen_iwtbf_dp_inputs):
    """The Queen IWTBF fixture has clean LRC priors that match the audio
    duration. The grader must score it ``>= _RELIABILITY_GATE`` so the
    consensus orchestrator keeps it on the fast LRC-windowed path. This
    is the regression gate for the model_id bump — if a future tweak
    pushes well-aligned songs into the synthetic-LRC fallback, the
    fallback's whole-song wav2vec2 drift could regress songs that align
    well today.
    """
    lrc, _, audio_duration_s = queen_iwtbf_dp_inputs
    last_lrc_start = max(start for start, _end, text in lrc if text.strip())
    score = lyrics_align._grade_priors(
        audio_duration_s=audio_duration_s,
        lrc_lines=lrc,
        lrc_implied_duration_s=last_lrc_start,
        dp_residuals=None,
    )
    assert score >= lyrics_align._RELIABILITY_GATE, (
        f"grader scored {score:.2f} < gate {lyrics_align._RELIABILITY_GATE} "
        "for the Queen IWTBF fixture; routing this song through the "
        "synthetic-LRC fallback would risk whole-song wav2vec2 drift "
        "on a song the LRC-windowed path already aligns cleanly"
    )
