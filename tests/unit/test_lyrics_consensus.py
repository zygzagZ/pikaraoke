"""Unit tests for the multi-source lyrics consensus engine."""

import pytest

from pikaraoke.lib.lyrics import Word
from pikaraoke.lib.lyrics_consensus import (
    _CONFIDENCE_MIN,
    ConsensusResult,
    SourceResult,
    TimedLine,
    build_audio_reference,
    build_consensus,
    merge_lines_per_window,
    normalize_tokens,
    score_against_reference,
    score_sources_against_reference,
    select_scaffold,
)


def _vtt(lrc: str) -> SourceResult:
    return SourceResult(name="vtt", kind="source_matched", lrc=lrc, is_synced=True)


def _whisper(words: list[Word]) -> SourceResult:
    return SourceResult(name="whisper", kind="source_matched", words=words, is_synced=False)


def _lrclib(lrc: str | None) -> SourceResult:
    return SourceResult(name="lrclib", kind="title_matched", lrc=lrc, is_synced=lrc is not None)


def _musixmatch(lrc: str | None) -> SourceResult:
    return SourceResult(name="musixmatch", kind="title_matched", lrc=lrc, is_synced=lrc is not None)


def _megalobiz(lrc: str | None) -> SourceResult:
    return SourceResult(name="megalobiz", kind="title_matched", lrc=lrc, is_synced=lrc is not None)


def _genius(text: str) -> SourceResult:
    return SourceResult(name="genius", kind="title_matched", plain_text=text, is_synced=False)


_MOONLIGHT_CORRECT_LRC = "\n".join(
    [
        "[00:13.50]The last that ever she saw him",
        "[00:18.20]Carried away by a moonlight shadow",
        "[00:23.10]He passed on worried and warning",
        "[00:27.80]Carried away by a moonlight shadow",
        "[00:32.40]Lost in a riddle that Saturday night",
        "[00:37.10]Far away on the other side",
    ]
)

_MOONLIGHT_WRONG_GENIUS = (
    "Four A.M. in the morning, carried away by a moonlight shadow\n"
    "I watched your vision forming, carried away by a moonlight shadow\n"
    "Stars roll slow in a silvery night\n"
    "Far away on the other side"
)


# ---------------- T2 normalize_tokens ----------------


class TestNormalizeTokens:
    def test_strips_section_headers(self):
        assert normalize_tokens("[Verse 1] hello world") == ["hello", "world"]

    def test_strips_parens(self):
        assert normalize_tokens("hello (refrain) world") == ["hello", "world"]

    def test_lowercases(self):
        assert normalize_tokens("HELLO World") == ["hello", "world"]

    def test_drops_punctuation(self):
        assert normalize_tokens("hello, world!") == ["hello", "world"]

    def test_preserves_apostrophes(self):
        assert normalize_tokens("it's don't") == ["it's", "don't"]

    def test_drops_short_tokens(self):
        assert normalize_tokens("a hello b world") == ["hello", "world"]

    def test_empty_input(self):
        assert normalize_tokens("") == []
        assert normalize_tokens(None) == []


# ---------------- T3 build_audio_reference ----------------


class TestBuildAudioReference:
    def test_vtt_only(self):
        ref = build_audio_reference(_vtt(_MOONLIGHT_CORRECT_LRC), None)
        assert "the" in ref and "last" in ref and "saw" in ref

    def test_whisper_only(self):
        words = [
            Word(text="hello", start=0.0, end=0.5),
            Word(text="world", start=0.5, end=1.0),
        ]
        ref = build_audio_reference(None, _whisper(words))
        assert ref == ["hello", "world"]

    def test_both_concatenate_with_dedup(self):
        vtt = _vtt("[00:00.00]hello world")
        whisper = _whisper(
            [Word(text="world", start=0.0, end=0.5), Word(text="extra", start=0.5, end=1.0)]
        )
        ref = build_audio_reference(vtt, whisper)
        assert ref == ["hello", "world", "extra"]

    def test_both_none_returns_empty(self):
        assert build_audio_reference(None, None) == []


# ---------------- T4 score_against_reference ----------------


class TestScoreAgainstReference:
    def test_full_match(self):
        ref = ["a", "b", "c", "d"]
        cov, uncertain = score_against_reference(ref, ref)
        assert cov == pytest.approx(1.0)
        assert uncertain is False

    def test_zero_overlap(self):
        cov, uncertain = score_against_reference(["x", "y"], ["a", "b"])
        assert cov == pytest.approx(0.0)
        assert uncertain is True

    def test_partial_match(self):
        ref = ["a", "b", "c", "d"]
        src = ["a", "b", "x", "d"]
        cov, _uncertain = score_against_reference(src, ref)
        assert 0.5 < cov < 1.0

    def test_empty_inputs(self):
        cov, uncertain = score_against_reference([], ["a"])
        assert cov == 0.0 and uncertain is True
        cov, uncertain = score_against_reference(["a"], [])
        assert cov == 0.0 and uncertain is True


# ---------------- T5 contiguity flag ----------------


class TestContiguityFlag:
    def test_contiguous_match_not_uncertain(self):
        ref = ["a", "b", "c", "d", "e"]
        cov, uncertain = score_against_reference(ref, ref)
        assert uncertain is False

    def test_permuted_match_flagged_order_uncertain(self):
        # Every adjacent pair swapped -> all matches are 1-token blocks, no
        # contiguous run survives. Real-world analogue: a cover version with
        # heavily-shuffled verse order.
        ref = list("abcdefghij")
        src = list("badcfehgji")
        cov, uncertain = score_against_reference(src, ref)
        assert cov > 0.3
        assert uncertain is True


# ---------------- T6 merge_lines_per_window ----------------


class TestMergeLinesPerWindow:
    """Line-level merge that replaced token-level voting.

    Picks the *whole* phrasing of each line from whichever source best
    matches the Whisper time window for that line. No surgery inside lines.
    """

    def _ws(self, *triples: tuple[str, float, float]) -> list[Word]:
        return [Word(text=t, start=s, end=e) for t, s, e in triples]

    def test_lrclib_full_line_wins_over_truncated_whisper(self):
        # Real bug pattern: Whisper drops a function word ("w") and a final
        # word ("pcha"); LRCLib has the full line. Old token-vote produced
        # "lat coś objęcia chłodu mnie"; new line-merge keeps the LRCLib
        # phrasing whole.
        lrclib = _lrclib("[00:00.00]od lat coś w objęcia chłodu mnie pcha")
        whisper_words = self._ws(
            ("od", 0.0, 0.2),
            ("lat", 0.2, 0.4),
            ("coś", 0.4, 0.6),
            ("objęcia", 0.6, 1.0),
            ("chłodu", 1.0, 1.4),
            ("mnie", 1.4, 1.6),
        )
        merged = merge_lines_per_window([lrclib], whisper_words)
        assert len(merged) == 1
        # Full LRCLib line preserved verbatim — "w" and "pcha" survive.
        assert merged[0].text == "od lat coś w objęcia chłodu mnie pcha"

    def test_better_phrasing_wins_against_scaffold(self):
        # Scaffold (LRCLib) has a slightly off line; Genius/MXM has the
        # phrasing that actually matches Whisper. Tie-broken by score.
        # Use musixmatch (synced) since merge_lines only consumes synced.
        lrclib = _lrclib("[00:00.00]hello strange world here")
        mxm = _musixmatch("[00:00.00]hello dear world friend")
        whisper_words = self._ws(
            ("hello", 0.0, 0.2),
            ("dear", 0.2, 0.4),
            ("world", 0.4, 0.6),
            ("friend", 0.6, 0.8),
        )
        merged = merge_lines_per_window([lrclib, mxm], whisper_words)
        assert len(merged) == 1
        assert merged[0].text == "hello dear world friend"

    def test_drops_scaffold_only_line_when_whisper_silent(self):
        # Two synced sources agree on the first line; the second line is
        # unique to LRClib and Whisper hears nothing. Drop fires because
        # multi-source baseline lets us trust the disagreement signal.
        lrclib = _lrclib("[00:00.00]hello world\n" "[00:10.00]ghost line nobody sings")
        mxm = _musixmatch("[00:00.00]hello world")
        whisper_words = self._ws(
            ("hello", 0.0, 0.3),
            ("world", 0.3, 0.6),
        )
        merged = merge_lines_per_window([lrclib, mxm], whisper_words)
        assert len(merged) == 1
        assert merged[0].text == "hello world"

    def test_single_source_keeps_lines_even_when_whisper_silent(self):
        # With only one synced source, "scaffold-only line" is every line
        # by construction. Drop rule disabled — keep the LRClib content
        # so quiet bridges aren't silently deleted.
        lrclib = _lrclib("[00:00.00]hello world\n[00:10.00]quiet bridge here")
        whisper_words = self._ws(
            ("hello", 0.0, 0.3),
            ("world", 0.3, 0.6),
        )
        merged = merge_lines_per_window([lrclib], whisper_words)
        assert len(merged) == 2
        assert merged[1].text == "quiet bridge here"

    def test_keeps_scaffold_only_line_when_two_sources_agree(self):
        # Both LRCLib and MXM agree the second line exists, even though
        # Whisper missed it. No drop — multi-source agreement wins over
        # silent window.
        lrclib = _lrclib("[00:00.00]hello world\n[00:10.00]quiet refrain we keep")
        mxm = _musixmatch("[00:00.00]hello world\n[00:10.00]quiet refrain we keep")
        whisper_words = self._ws(
            ("hello", 0.0, 0.3),
            ("world", 0.3, 0.6),
        )
        merged = merge_lines_per_window([lrclib, mxm], whisper_words)
        assert len(merged) == 2
        assert merged[1].text == "quiet refrain we keep"

    def test_extra_line_inserted_when_two_sources_agree_and_whisper_supports(self):
        # Scaffold (LRCLib) skips a line; both MXM and Megalobiz have it
        # at the same timestamp; Whisper hears its tokens. Insert the
        # extra line into the merged output.
        lrclib = _lrclib("[00:00.00]first line\n[00:10.00]third line")
        mxm = _musixmatch("[00:00.00]first line\n[00:05.00]middle extra line\n[00:10.00]third line")
        meg = _megalobiz("[00:00.00]first line\n[00:05.00]middle extra line\n[00:10.00]third line")
        whisper_words = self._ws(
            ("first", 0.0, 0.3),
            ("line", 0.3, 0.6),
            ("middle", 5.0, 5.2),
            ("extra", 5.2, 5.4),
            ("line", 5.4, 5.6),
            ("third", 10.0, 10.3),
            ("line", 10.3, 10.6),
        )
        merged = merge_lines_per_window([lrclib, mxm, meg], whisper_words)
        texts = [tl.text for tl in merged]
        assert "middle extra line" in texts

    def test_extra_line_dropped_when_only_one_source_has_it(self):
        # Only MXM has the extra; Megalobiz doesn't. No multi-source
        # support → drop even if Whisper hears something close.
        lrclib = _lrclib("[00:00.00]first line\n[00:10.00]third line")
        mxm = _musixmatch("[00:00.00]first line\n[00:05.00]middle extra line\n[00:10.00]third line")
        whisper_words = self._ws(
            ("first", 0.0, 0.3),
            ("middle", 5.0, 5.3),
            ("extra", 5.3, 5.6),
            ("line", 5.6, 5.9),
            ("third", 10.0, 10.3),
        )
        merged = merge_lines_per_window([lrclib, mxm], whisper_words)
        texts = [tl.text for tl in merged]
        assert "middle extra line" not in texts


# ---------------- T7 select_scaffold ----------------


class TestSelectScaffold:
    def test_synced_outranks_whisper(self):
        lrclib = _lrclib("[00:00.00]a")
        whisper = _whisper([Word(text="a", start=0.0, end=0.5)])
        s = select_scaffold([lrclib, whisper], order_uncertain=set())
        assert s is lrclib

    def test_lrclib_outranks_musixmatch(self):
        lrclib = _lrclib("[00:00.00]a")
        mxm = _musixmatch("[00:00.00]a")
        s = select_scaffold([mxm, lrclib], order_uncertain=set())
        assert s is lrclib

    def test_vtt_outranks_whisper(self):
        # AI demotion: human-curated VTT now beats Whisper ASR for the
        # scaffold seat. Whisper only earns scaffold when nothing else
        # is eligible — see ``test_whisper_when_truly_alone``.
        whisper = _whisper([Word(text="a", start=0.0, end=0.5)])
        vtt = _vtt("[00:00.00]a")
        s = select_scaffold([whisper, vtt], order_uncertain=set())
        assert s is vtt

    def test_whisper_when_truly_alone(self):
        # When VTT is missing and no title-matched survives, Whisper is
        # the last-resort scaffold ("AI tylko gdy nic innego").
        whisper = _whisper([Word(text="a", start=0.0, end=0.5)])
        s = select_scaffold([whisper], order_uncertain=set())
        assert s is whisper

    def test_vtt_line_only_fallback(self):
        vtt = _vtt("[00:00.00]a")
        s = select_scaffold([vtt], order_uncertain=set())
        assert s is vtt

    def test_order_uncertain_excludes_lrclib_falls_to_vtt(self):
        # With Whisper demoted, an order-uncertain LRClib + a VTT now
        # falls through to VTT (rank 3) rather than Whisper (rank 99).
        lrclib = _lrclib("[00:00.00]a")
        vtt = _vtt("[00:00.00]a")
        whisper = _whisper([Word(text="a", start=0.0, end=0.5)])
        s = select_scaffold([lrclib, vtt, whisper], order_uncertain={"lrclib"})
        assert s is vtt

    def test_order_uncertain_lrclib_falls_to_whisper_only_if_no_vtt(self):
        lrclib = _lrclib("[00:00.00]a")
        whisper = _whisper([Word(text="a", start=0.0, end=0.5)])
        s = select_scaffold([lrclib, whisper], order_uncertain={"lrclib"})
        assert s is whisper

    def test_returns_none_when_empty(self):
        assert select_scaffold([], order_uncertain=set()) is None


# ---------------- T1 Moonlight Shadow regression (CRITICAL) ----------------


class TestMoonlightShadowRegression:
    """Genius returned a wrong-version stub starting with "Four A.M.";
    VTT and Whisper both agree on the correct text. Consensus must
    reject Genius and emit a result whose timestamps come from VTT.
    """

    def test_genius_rejected_by_audio_ref(self):
        vtt = _vtt(_MOONLIGHT_CORRECT_LRC)
        whisper = _whisper(
            [
                Word(text="The", start=13.5, end=13.8),
                Word(text="last", start=13.8, end=14.1),
                Word(text="that", start=14.1, end=14.4),
                Word(text="ever", start=14.4, end=14.7),
                Word(text="she", start=14.7, end=15.0),
                Word(text="saw", start=15.0, end=15.3),
                Word(text="him", start=15.3, end=15.6),
                Word(text="Carried", start=18.2, end=18.6),
                Word(text="away", start=18.6, end=19.0),
                Word(text="by", start=19.0, end=19.2),
                Word(text="a", start=19.2, end=19.4),
                Word(text="moonlight", start=19.4, end=20.0),
                Word(text="shadow", start=20.0, end=20.6),
            ]
        )
        genius = _genius(_MOONLIGHT_WRONG_GENIUS)
        ref = build_audio_reference(vtt, whisper)
        consensus = build_consensus([vtt, whisper, genius], ref)
        assert consensus is not None
        # Genius must be rejected with an explicit coverage reason.
        rejected_names = [name for name, _reason in consensus.sources_rejected]
        assert "genius" in rejected_names
        # Consensus text starts with the correct verse.
        assert consensus.text.startswith("the last that ever she saw him")
        # No "four" hallucination.
        assert "four" not in consensus.text.split()

    def test_lrclib_agreement_lifts_confidence(self):
        vtt = _vtt(_MOONLIGHT_CORRECT_LRC)
        lrclib = _lrclib(_MOONLIGHT_CORRECT_LRC)
        ref = build_audio_reference(vtt, None)
        consensus = build_consensus([vtt, lrclib], ref)
        assert consensus is not None
        assert "lrclib" in consensus.sources_used
        assert consensus.confidence > 0.85


# ---------------- T9 build_consensus integration ----------------


class TestBuildConsensus:
    def test_single_source_lrclib(self):
        vtt = _vtt(_MOONLIGHT_CORRECT_LRC)
        lrclib = _lrclib(_MOONLIGHT_CORRECT_LRC)
        ref = build_audio_reference(vtt, None)
        result = build_consensus([vtt, lrclib], ref)
        assert isinstance(result, ConsensusResult)
        assert result.lrc

    def test_no_sources_returns_none(self):
        assert build_consensus([], []) is None

    def test_whisper_only_pool_returns_none(self):
        # US-55: raw ASR must never become the consensus output on its
        # own — with no synced source vouching for it, a whisper-only
        # pool is refused instead of serving the transcript verbatim
        # (the Toto - Africa garbled-lyrics incident, 2026-07-06 audit).
        whisper = _whisper(
            [
                Word(text="do", start=0.0, end=0.4),
                Word(text="bless", start=0.5, end=0.9),
                Word(text="the", start=1.0, end=1.2),
                Word(text="rains", start=1.3, end=1.8),
            ]
        )
        ref = build_audio_reference(None, whisper)
        assert build_consensus([whisper], ref) is None


# ---------------- T10 all-rejected fallback ----------------


class TestAllRejected:
    def test_all_title_matched_rejected_returns_none(self):
        vtt = _vtt(_MOONLIGHT_CORRECT_LRC)
        # LRCLib returns garbage that doesn't match.
        bad = _lrclib("[00:00.00]totally\n[00:01.00]different\n[00:02.00]words\n[00:03.00]here")
        ref = build_audio_reference(vtt, None)
        result = build_consensus([vtt, bad], ref)
        # VTT alone (source_matched) survives but consensus uses VTT as scaffold.
        assert result is not None
        assert "lrclib" in [r[0] for r in result.sources_rejected]


# ---------------- T11 empty audio_ref guard ----------------


class TestEmptyAudioRefGuard:
    def test_empty_audio_ref_falls_back_to_best_title_matched(self):
        # No VTT, no Whisper; just LRCLib + Genius.
        lrclib = _lrclib(_MOONLIGHT_CORRECT_LRC)
        genius = _genius(_MOONLIGHT_CORRECT_LRC)
        result = build_consensus([lrclib, genius], audio_ref=[])
        assert result is not None
        # Confidence penalized.
        assert result.confidence < 1.0

    def test_empty_audio_ref_no_title_matched_returns_none(self):
        whisper = _whisper([Word(text="hi", start=0.0, end=0.5)])
        result = build_consensus([whisper], audio_ref=[])
        assert result is None


# ---------------- T13 confidence gate ----------------


class TestConfidenceGate:
    def test_below_threshold_returns_none(self):
        # Single weak title-matched source vs partial audio_ref.
        # 1 surviving / 1 total = 1.0 multiplier; coverage must drop it below 0.5.
        # Construct: ref has 10 tokens, lrclib matches only 4 of them.
        ref_text = " ".join(f"word{i}" for i in range(10))
        ref = normalize_tokens(ref_text)
        # Title-matched source matches 4 of 10 ref tokens (below 0.70 threshold,
        # gets rejected, no survivors of title kind, no audio-ref source).
        weak_lrc = " ".join(f"[00:0{i}.00]word{i}" for i in range(4))
        weak = _lrclib(weak_lrc)
        result = build_consensus([weak], ref)
        # Only 1 source, gets rejected, no survivors -> None.
        assert result is None


# ---------------- T25 group failure mode ----------------


class TestGroupFailureMode:
    """Documented limitation: if every title-matched source returns the same
    wrong-version (cover indexed under original title), consensus depends
    entirely on VTT/Whisper. With strong audio reference, all wrong-version
    title-matched are rejected.
    """

    def test_strong_audio_ref_rejects_unanimous_wrong_version(self):
        vtt = _vtt(_MOONLIGHT_CORRECT_LRC)
        # 3 title-matched all returning the same wrong-version.
        wrong = (
            "[00:00.00]four am in the morning\n"
            "[00:05.00]i watched your vision forming\n"
            "[00:10.00]stars roll slow"
        )
        a = _lrclib(wrong)
        b = _musixmatch(wrong)
        c = _megalobiz(wrong)
        ref = build_audio_reference(vtt, None)
        result = build_consensus([vtt, a, b, c], ref)
        assert result is not None
        rejected_names = [name for name, _ in result.sources_rejected]
        assert {"lrclib", "musixmatch", "megalobiz"} <= set(rejected_names)
        assert result.text.startswith("the last")


# ---------------- T27 source scores attached + score_sources helper ----------------


class TestSourceScoresAttached:
    def test_consensus_result_carries_per_source_coverage(self):
        # Build a fixture where the wrong-version source is unambiguously
        # below the genius threshold (0.55). The audio_ref tokens here are
        # the Moonlight chorus; the bad source talks about completely
        # different lyrics so coverage is near zero.
        vtt = _vtt(_MOONLIGHT_CORRECT_LRC)
        good_lrclib = _lrclib(_MOONLIGHT_CORRECT_LRC)
        unrelated_genius = _genius(
            "We are the champions of the world\nNo time for losers\nWe'll keep on fighting"
        )
        ref = build_audio_reference(vtt, None)
        result = build_consensus([vtt, good_lrclib, unrelated_genius], ref)
        assert result is not None
        assert "lrclib" in result.source_scores
        assert "genius" in result.source_scores
        # Surviving source scored above the threshold.
        lrclib_cov, _ = result.source_scores["lrclib"]
        assert lrclib_cov >= 0.7
        # Rejected source recorded its (low) coverage too.
        genius_cov, _ = result.source_scores["genius"]
        assert 0.0 <= genius_cov < 0.55
        rejected_names = [name for name, _reason in result.sources_rejected]
        assert "genius" in rejected_names

    def test_score_sources_helper_runs_independently_of_build(self):
        # Even when consensus aborts (e.g. all title-matched rejected),
        # the persister can still recover the scores it would've computed.
        vtt = _vtt(_MOONLIGHT_CORRECT_LRC)
        bad = _lrclib("[00:00.00]totally unrelated jazz fusion phrase")
        ref = build_audio_reference(vtt, None)
        scores = score_sources_against_reference([vtt, bad], ref)
        # vtt is audio-ref owner — excluded.
        assert "vtt" not in scores
        # lrclib (title-matched) gets recorded, even though it'd be rejected.
        assert "lrclib" in scores
        cov, _ = scores["lrclib"]
        assert cov < 0.55

    def test_score_sources_empty_audio_ref_returns_empty(self):
        bad = _lrclib("[00:00.00]hello")
        assert score_sources_against_reference([bad], []) == {}
