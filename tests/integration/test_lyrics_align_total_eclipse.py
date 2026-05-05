"""End-to-end alignment regression for Bonnie Tyler - Total Eclipse of the Heart.

Pins behaviour against captured silero+silencedetect onsets and the
LRClib syncedLyrics for the song. The bug this guards: ghost LRC lines
highlighted during the 2:50-3:28 instrumental and a 2 s lateness
cascade through the post-solo verse until 4:17. The DP solver in
``lyrics_align`` must interpolate the unanchored lines into the audio
window after the post-solo onset, not into the dead solo.
"""

import json
from pathlib import Path

import pytest

from pikaraoke.lib import lyrics_align, vad_probe
from pikaraoke.lib.lyrics import lrc_line_windows

FIXTURES = Path(__file__).parent.parent / "fixtures" / "total_eclipse"
SOLO_START_S = 170.0  # last pre-solo phrase decays around 2:50
SOLO_END_S = 207.0  # post-solo first audio onset is 207.84
EXPECTED_FIRST_POST_SOLO_S = 207.84
LEAD_IN_S = 0.25  # _KARAOKE_LEAD_IN_S


@pytest.fixture
def total_eclipse_inputs(monkeypatch):
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


def test_no_line_renders_during_solo(total_eclipse_inputs):
    """No LRC line's shifted start time may fall inside 2:50-3:28."""
    out = _starts(lyrics_align._detect_per_line_starts("/dev/null", total_eclipse_inputs))
    assert out is not None, "alignment should not bail on Total Eclipse"

    in_solo = [
        (i, t, total_eclipse_inputs[i][2])
        for i, t in enumerate(out)
        if SOLO_START_S < t < SOLO_END_S and total_eclipse_inputs[i][2].strip()
    ]
    assert not in_solo, (
        f"{len(in_solo)} ghost line(s) render during the instrumental "
        f"solo (170s-207s). First three: {in_solo[:3]}"
    )


def test_first_post_solo_line_snaps_to_real_onset(total_eclipse_inputs):
    """The first line at or after 3:28 starts within 2 s of the real onset."""
    out = _starts(lyrics_align._detect_per_line_starts("/dev/null", total_eclipse_inputs))
    assert out is not None
    post_solo = [t for t in out if t >= SOLO_END_S]
    first_post = min(post_solo)
    expected = EXPECTED_FIRST_POST_SOLO_S - LEAD_IN_S
    assert abs(first_post - expected) < 2.0, (
        f"first post-solo line at {first_post:.2f}s, " f"expected within 2s of {expected:.2f}s"
    )


def test_post_solo_lines_spread_across_multiple_anchors(total_eclipse_inputs):
    """The post-solo window 207.84..257.56 must contain lines distributed
    across multiple distinct anchor regions, not collapsed onto one anchor.

    The original bug compressed every post-solo line onto a single wrong
    anchor (the spurious silencedetect marker inside the solo). The
    smoking gun was N consecutive lines with identical or near-identical
    shifted timestamps. This pins three checks against that cascade:

    * minimum gap between consecutive shifted lines >= 0.04s (the DP's
      ``_interpolate_unanchored`` instrumental-gap branch deliberately
      spreads cluster lines by ~0.05s; anything tighter is collision)
    * at least 5 distinct anchor clusters (gaps > 0.5s between adjacent
      shifted times) inside the window — multiple vocal entries got
      their own anchor, didn't all land on one
    * lines never compressed below ~80% of the line count being unique
      timestamps — duplicates are the symptom shape
    """
    out = _starts(lyrics_align._detect_per_line_starts("/dev/null", total_eclipse_inputs))
    assert out is not None
    in_window = sorted(t for t in out if 207.84 <= t <= 257.56)
    assert len(in_window) >= 10, (
        f"expected ≥10 lines in the post-solo window for a meaningful "
        f"cascade check; got {len(in_window)}. Fixture may have changed."
    )
    consecutive_gaps = [b - a for a, b in zip(in_window, in_window[1:])]
    assert min(consecutive_gaps) >= 0.04, (
        f"post-solo lines collide at one anchor: " f"min gap = {min(consecutive_gaps):.3f}s"
    )
    cluster_breaks = sum(1 for g in consecutive_gaps if g > 0.5)
    assert cluster_breaks >= 5, (
        f"post-solo lines spread across only {cluster_breaks + 1} anchor "
        f"clusters (need ≥6); compression cascade has returned"
    )
    unique_ratio = len(set(round(t, 3) for t in in_window)) / len(in_window)
    assert unique_ratio >= 0.8, (
        f"only {unique_ratio:.0%} of post-solo timestamps are unique; "
        f"duplicates are the original wrong-anchor symptom"
    )


def test_full_pipeline_smoke(total_eclipse_inputs):
    """Sanity: every line gets a shifted timestamp, all in [0, audio + slack]."""
    out = _starts(lyrics_align._detect_per_line_starts("/dev/null", total_eclipse_inputs))
    assert out is not None
    assert len(out) == len(total_eclipse_inputs)
    assert all(0 <= t <= 340 for t in out)
    inversions = sum(1 for a, b in zip(out, out[1:]) if b + 0.5 < a)
    assert inversions == 0, f"{inversions} large monotonicity inversions in shifted starts"


def test_grader_routes_short_edit_to_fallback(total_eclipse_inputs):
    """Hypothesis from the plan: LRCLib returns long-version timestamps
    for the YouTube short edit. The fixture LRC happens to match the
    audio (the DP-pinning tests above use that matched case), so this
    assertion replays the grader against a synthetic off-by-one input
    derived from the same fixture: the LRC lines shifted +90s as if
    they came from a longer master. The grader must score the song
    below ``_RELIABILITY_GATE`` so the consensus orchestrator routes
    it through the synthetic-LRC fallback rather than re-stuffing the
    off-by-one upstream timestamps into the line fence.
    """
    audio_duration_s = 333.949  # short-edit fixture audio
    long_version_shift_s = 90.0  # plausible long-version → short-edit drift
    shifted_lrc = [
        (s + long_version_shift_s, e + long_version_shift_s, t) for s, e, t in total_eclipse_inputs
    ]
    last_lrc_start = max(start for start, _end, text in shifted_lrc if text.strip())
    score = lyrics_align._grade_priors(
        audio_duration_s=audio_duration_s,
        lrc_lines=shifted_lrc,
        lrc_implied_duration_s=last_lrc_start,
        dp_residuals=None,
    )
    assert score < lyrics_align._RELIABILITY_GATE, (
        f"grader scored {score:.2f} ≥ gate {lyrics_align._RELIABILITY_GATE} "
        "for the Total Eclipse short-edit hypothesis; the synthetic-LRC "
        "fallback would not engage and off-by-one upstream timestamps "
        "would bleed into the rendered ASS"
    )
