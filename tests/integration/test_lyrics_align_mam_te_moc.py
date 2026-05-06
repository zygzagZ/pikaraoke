"""End-to-end alignment regression for Katarzyna Łaska - Mam tę moc.

Pins per-verse drift behaviour: the YouTube version of this Polish
ballad slows down across verses, so a single global shift would
over-correct line 1 or under-correct line 6. The DP solver must anchor
each verse's first line to its own onset and only inherit cumulative
shifts within continuous singing inside a verse.
"""

import json
from pathlib import Path

import pytest

from pikaraoke.lib import lyrics_align, vad_probe
from pikaraoke.lib.lyrics import lrc_line_windows

FIXTURES = Path(__file__).parent.parent / "fixtures" / "mam_te_moc"
LEAD_IN_S = 0.25


@pytest.fixture
def mam_te_moc_inputs(monkeypatch):
    onsets = json.loads((FIXTURES / "vocal_onsets.json").read_text())["onsets"]
    lrc = (FIXTURES / "lyrics.lrc").read_text()
    monkeypatch.setattr(
        vad_probe,
        "list_vocal_onsets",
        lambda _path: [(e["onset"], e["next_onset"]) for e in onsets],
    )
    return lrc_line_windows(lrc)


def _starts(result):
    if result is None:
        return None
    starts, _residuals = result
    return starts


def test_first_line_snaps_to_first_onset(mam_te_moc_inputs):
    """The first non-empty LRC line snaps near the first VAD onset."""
    out = _starts(lyrics_align._detect_per_line_starts("/dev/null", mam_te_moc_inputs))
    assert out is not None
    first_text_idx = next(i for i, (_, _, t) in enumerate(mam_te_moc_inputs) if t.strip())
    onsets = json.loads((FIXTURES / "vocal_onsets.json").read_text())["onsets"]
    first_onset = onsets[0]["onset"]
    # The first non-empty LRC line should land within ~2 s of the first
    # onset (allowing for the karaoke lead-in subtraction).
    assert abs(out[first_text_idx] - (first_onset - LEAD_IN_S)) < 2.0


def test_per_verse_shifts_are_not_a_global_offset(mam_te_moc_inputs):
    """Cumulative shift varies meaningfully across verses, ruling out a
    flat global offset.

    A single global shift would produce identical per-line drift across
    the entire song. Mam Tę Moc's YouTube version drifts non-uniformly
    relative to LRClib's canonical timestamps, so the DP's job is to
    anchor multiple verses independently. The exact per-verse trend is
    not strictly monotonic (this fixture's drift goes +1 → +12 → -5 →
    +30 across verses depending on which onsets the DP picks up), but
    a global-offset implementation would collapse to span ≈ 0.
    """
    out = _starts(lyrics_align._detect_per_line_starts("/dev/null", mam_te_moc_inputs))
    assert out is not None
    shifts = [
        out[i] - mam_te_moc_inputs[i][0]
        for i, (_, _, t) in enumerate(mam_te_moc_inputs)
        if t.strip()
    ]
    span = max(shifts) - min(shifts)
    assert span >= 5.0, (
        f"per-verse drift collapsed (span={span:.3f}s); a flat global "
        f"shift would produce span ≈ 0, but real per-verse anchoring on "
        f"this fixture spans tens of seconds"
    )
    # Catch the "all lines clustered tight around one shift value"
    # failure mode: at least 30% of lines must differ from the median
    # shift by more than 1.0s.
    median = sorted(shifts)[len(shifts) // 2]
    far_from_median = sum(1 for s in shifts if abs(s - median) > 1.0)
    assert far_from_median >= 0.3 * len(shifts), (
        f"only {far_from_median}/{len(shifts)} lines drift > 1s from "
        f"the median shift {median:.2f}s; per-verse anchoring lost"
    )


def test_no_large_monotonicity_inversions(mam_te_moc_inputs):
    """Shifted times must not jump backward by more than 0.5 s between lines.
    A backward jump is a wrong-anchor signal; the DP's tempo-jump cost
    should keep the assignment monotonic across verses."""
    out = _starts(lyrics_align._detect_per_line_starts("/dev/null", mam_te_moc_inputs))
    assert out is not None
    inversions = [(i, out[i - 1], out[i]) for i in range(1, len(out)) if out[i] + 0.5 < out[i - 1]]
    assert not inversions, (
        f"{len(inversions)} inversion(s) in shifted starts; first three: " f"{inversions[:3]}"
    )
