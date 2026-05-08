"""Multi-source lyrics consensus — line-merge edition.

Cross-validates lyrics sources (LRCLib, Genius, Musixmatch, Megalobiz, VTT)
against an audio reference (Whisper transcript) and emits a per-line merge:
each line of the output picks the *whole* phrasing from whichever source
best matches the corresponding Whisper time window. No token-level voting,
no surgery inside lines — words from a chosen line are kept verbatim.

Drop / keep rules for lines:
  * scaffold-only line + Whisper hears nothing in its window → drop
    (handles "extra zwrotka" present in only one fetcher).
  * non-scaffold synced source has a line at a timestamp the scaffold
    skips → keep iff ≥1 other source agrees on that timestamp AND
    Whisper's window contains a matching token block (≥0.55 ratio).
  * everything else → keep, picking the candidate with the highest
    SequenceMatcher ratio against the Whisper window.

State machine::

    IDLE -> SCORE_SOURCES -> MERGE_LINES ----> WRITE T3
                                |
                                +-- no synced + no whisper -> FALLBACK
                                +-- empty audio_ref -> SINGLE_BEST_TITLE
                                +-- confidence < 0.5 -> SKIP

Pure Python, zero I/O. Threading and aligner orchestration live in
:mod:`pikaraoke.lib.lyrics`; this module only computes the consensus.
"""

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pikaraoke.lib.lyrics import Word

logger = logging.getLogger(__name__)


_REJECT_THRESHOLDS: dict[str, float] = {
    "lrclib": 0.70,
    "musixmatch": 0.70,
    "megalobiz": 0.70,
    "genius": 0.55,
}
_DEFAULT_THRESHOLD = 0.55

# Ranking used to pick the timing scaffold (which source's line timestamps
# drive the consensus output) and to break "no audio reference" ties.
# Lower is better. ``vtt`` ranks above whisper because human-curated YouTube
# captions are almost always cleaner than ASR. ``whisper`` is last-resort.
_SCAFFOLD_RANK: dict[str, int] = {
    "lrclib": 0,
    "musixmatch": 1,
    "megalobiz": 2,
    "vtt": 3,
    "whisper": 99,
}

_CONFIDENCE_MIN = 0.5
_CONFIDENCE_PENALTY_NO_AUDIO_REF = 0.7
_CONTIGUITY_MIN = 0.4

# Two synced lines from different sources are considered "the same line" when
# their timestamps are within this many seconds. LRCLib and Musixmatch often
# disagree by 0.5–1.5s on the same line; 2.5s gives slack without merging
# adjacent lines of fast verses.
_LINE_MATCH_WINDOW_S = 2.5

# Threshold for inserting an "extra" line (one present in non-scaffold sources
# but not the scaffold) — Whisper window must score at least this against the
# candidate text. Lower than the source-rejection threshold because we already
# require multi-source agreement on top.
_EXTRA_LINE_WHISPER_GATE = 0.55

_SECTION_HEADER_RE = re.compile(r"\[[^\]]+\]")
_PAREN_RE = re.compile(r"\([^)]*\)")
_PUNCT_RE = re.compile(r"[^\w'\s]", re.UNICODE)
_LRC_TAG_RE = re.compile(r"^\[(\d+):(\d+)(?:\.(\d+))?\](.*)$")


@dataclass
class SourceResult:
    """Output of one fetcher, fed into the consensus pool."""

    name: str  # "vtt" | "whisper" | "lrclib" | "musixmatch" | "megalobiz" | "genius"
    kind: str  # "source_matched" | "title_matched"
    lrc: str | None = None
    plain_text: str | None = None
    words: "list[Word] | None" = None
    is_synced: bool = False


@dataclass
class TimedLine:
    """A line of lyrics with its starting timestamp (seconds)."""

    start: float
    text: str
    source_name: str = ""


@dataclass
class ConsensusResult:
    """Result of cross-validating sources against the audio reference."""

    text: str
    lrc: str
    sources_used: list[str]
    sources_rejected: list[tuple[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    # ``source_name -> (coverage, order_uncertain)`` for every non-audio-ref
    # source seen, including rejected ones. Audio-ref contributors (vtt,
    # whisper) are excluded — they ARE the reference, so coverage against
    # themselves is meaningless. Persisted to ``subtitle_jobs.coverage`` so
    # the variant fetch path can refuse to render a known-bad source and
    # the picker chip can show a per-source badge.
    source_scores: dict[str, tuple[float, bool]] = field(default_factory=dict)


# ---------- Tokenization ----------


def normalize_tokens(text: str | None) -> list[str]:
    """Lowercase, strip section headers / parens / punctuation, drop tiny tokens.

    Apostrophes preserved so contractions ("it's", "don't") survive.
    Tokens shorter than 2 chars dropped — they are usually OCR noise or
    LRC metadata fragments.
    """
    if not text:
        return []
    s = _SECTION_HEADER_RE.sub(" ", text)
    s = _PAREN_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = s.lower()
    return [t for t in s.split() if len(t) >= 2]


def _strip_lrc_tags(line: str) -> str:
    return re.sub(r"\[\d+:\d+(?:\.\d+)?\]", " ", line)


def _source_tokens(source: SourceResult) -> list[str]:
    """Pull a token list from whichever field a source populated."""
    if source.lrc:
        body = _strip_lrc_tags(source.lrc)
        return normalize_tokens(body)
    if source.plain_text:
        return normalize_tokens(source.plain_text)
    if source.words:
        return normalize_tokens(" ".join(w.text for w in source.words))
    return []


# ---------- Audio reference ----------


def build_audio_reference(vtt: SourceResult | None, whisper: SourceResult | None) -> list[str]:
    """Token sequence representing what the audio actually contains.

    VTT tokens (when present) come first since YouTube captions are usually
    closer to lead vocals. Whisper tokens follow to cover anything VTT missed.
    Adjacent identical tokens are collapsed so repeated words at the seam
    don't double-count in the SequenceMatcher comparison.
    """
    tokens: list[str] = []
    if vtt is not None:
        tokens.extend(_source_tokens(vtt))
    if whisper is not None:
        tokens.extend(_source_tokens(whisper))
    out: list[str] = []
    for t in tokens:
        if not out or out[-1] != t:
            out.append(t)
    return out


# ---------- Per-source coverage scoring ----------


def score_against_reference(tokens: list[str], ref: list[str]) -> tuple[float, bool]:
    """Coverage ratio + order_uncertain flag.

    Returns ``(0.0, True)`` when either side is empty. ``order_uncertain``
    fires when the longest matching block is small relative to total
    matched tokens — a permuted-verse cover version, where the source's
    tokens overlap the reference but in scrambled order.
    """
    if not tokens or not ref:
        return 0.0, True
    matcher = SequenceMatcher(None, ref, tokens, autojunk=False)
    coverage = matcher.ratio()
    longest = matcher.find_longest_match(0, len(ref), 0, len(tokens))
    matched = sum(b.size for b in matcher.get_matching_blocks())
    if matched <= 0:
        return coverage, True
    contiguity = longest.size / matched
    return coverage, contiguity < _CONTIGUITY_MIN


def _threshold_for(name: str) -> float:
    return _REJECT_THRESHOLDS.get(name, _DEFAULT_THRESHOLD)


def score_sources_against_reference(
    sources: list[SourceResult], audio_ref: list[str]
) -> dict[str, tuple[float, bool]]:
    """Compute ``(coverage, order_uncertain)`` for every non-audio-ref source.

    Pure helper extracted so the consensus persister can record scores
    even when ``build_consensus`` decides to abort. Audio-reference owners
    (``vtt``, ``whisper``) are skipped — they define the reference, scoring
    them against themselves is meaningless. Returns ``{}`` when ``audio_ref``
    is empty (no signal to score against).
    """
    if not audio_ref:
        return {}
    out: dict[str, tuple[float, bool]] = {}
    audio_ref_owners = {"vtt", "whisper"}
    for source in sources:
        if source.name in audio_ref_owners:
            continue
        coverage, uncertain = score_against_reference(_source_tokens(source), audio_ref)
        out[source.name] = (coverage, uncertain)
    return out


# ---------- Scaffold (timing source) ----------


def select_scaffold(
    survivors: list[SourceResult], order_uncertain: set[str]
) -> SourceResult | None:
    """Pick the source whose timestamps drive the consensus LRC.

    Title-matched lyric sources rank highest (LRCLib > MXM > Megalobiz),
    then human-curated VTT captions, with Whisper ASR sitting at the
    bottom of the rank table — its words only earn the scaffold seat
    when no other survivor is eligible.
    Order-uncertain sources are excluded — their tokens still vote, but
    their timestamps would mis-place lines.
    """
    eligible: list[SourceResult] = []
    for s in survivors:
        if s.name in order_uncertain:
            continue
        if s.is_synced or s.words:
            eligible.append(s)
    if not eligible:
        for s in survivors:
            if s.name == "vtt" and s.is_synced:
                return s
        return None
    eligible.sort(key=lambda s: _SCAFFOLD_RANK.get(s.name, 99))
    return eligible[0]


# ---------- Line extraction ----------


def _extract_timed_lines(source: SourceResult) -> list[TimedLine]:
    """Return ``[(start_sec, line_text), ...]`` for synced sources, else ``[]``.

    Skips empty / metadata-only lines so an LRC with stray ``[ti:]`` or
    blank ``[mm:ss]`` placeholders doesn't pollute the merge with empty
    candidates.
    """
    if not source.lrc:
        return []
    out: list[TimedLine] = []
    for raw in source.lrc.splitlines():
        m = _LRC_TAG_RE.match(raw.strip())
        if not m:
            continue
        mm_s, ss_s, frac_s, text = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            t = int(mm_s) * 60 + int(ss_s)
            if frac_s:
                t += float("0." + frac_s)
        except ValueError:
            continue
        text = text.strip()
        if not text:
            continue
        out.append(TimedLine(start=float(t), text=text, source_name=source.name))
    out.sort(key=lambda x: x.start)
    return out


def _whisper_window_tokens(
    whisper_words: "list[Word]", t_start: float, t_end: float
) -> list[str]:
    """Return normalized whisper tokens whose start time is in [t_start, t_end)."""
    out: list[str] = []
    for w in whisper_words:
        ws = getattr(w, "start", None)
        if ws is None:
            continue
        if t_start <= ws < t_end:
            out.extend(normalize_tokens(getattr(w, "text", "")))
    return out


def _line_score(audio_window: list[str], line_text: str) -> float:
    """SequenceMatcher ratio of normalized line tokens against an audio window."""
    line_tokens = normalize_tokens(line_text)
    if not line_tokens or not audio_window:
        return 0.0
    return SequenceMatcher(None, audio_window, line_tokens, autojunk=False).ratio()


# ---------- Line merge ----------


def merge_lines_per_window(
    synced_sources: list[SourceResult],
    whisper_words: "list[Word]",
) -> list[TimedLine]:
    """Build a per-line consensus by picking the best phrasing per scaffold line.

    Two passes:

    1. Walk scaffold lines. For each line, compute the Whisper window
       ``[scaffold[i].start, scaffold[i+1].start)``. Collect candidate
       texts: the scaffold line + the closest line (within
       ``_LINE_MATCH_WINDOW_S``) from every other synced source. Pick the
       candidate with the highest score against the window. Drop the line
       when only the scaffold has it AND the window is empty.

    2. Walk every non-scaffold synced source. For each line whose start
       isn't within the match-window of any scaffold line ("extra"
       candidate), keep it iff ≥1 other non-scaffold source has a
       co-located line AND Whisper supports it (window score ≥
       ``_EXTRA_LINE_WHISPER_GATE``).

    Returns sorted-by-time ``TimedLine`` list. Empty list when no scaffold
    can be selected or scaffold has no usable lines.
    """
    if not synced_sources:
        return []

    by_source: dict[str, list[TimedLine]] = {}
    rank_order: list[SourceResult] = sorted(
        synced_sources, key=lambda s: _SCAFFOLD_RANK.get(s.name, 99)
    )
    for s in rank_order:
        lines = _extract_timed_lines(s)
        if lines:
            by_source[s.name] = lines

    if not by_source:
        return []

    scaffold_name = next(iter(by_source))  # first by rank
    scaffold_lines = by_source[scaffold_name]
    # Drop rule for scaffold-only lines (Whisper hears nothing) only fires
    # when ≥2 synced sources are available — otherwise every line is
    # "scaffold-only" by construction and we'd silently delete legitimate
    # content that Whisper just happened to miss (quiet bridges, backing
    # vocals masking the lead, dropped final consonants). The user's
    # "extra zwrotka" rule needs a non-scaffold source to disagree with.
    multi_source_drop_active = len(by_source) >= 2

    # End-of-song bound for the last scaffold line's whisper window.
    audio_end = 0.0
    if whisper_words:
        for w in whisper_words:
            we = getattr(w, "end", None)
            if we is not None and we > audio_end:
                audio_end = we
    if audio_end <= 0.0:
        audio_end = scaffold_lines[-1].start + 12.0

    out_lines: list[TimedLine] = []

    # ---- Pass 1: scaffold-driven line selection ----
    for i, sline in enumerate(scaffold_lines):
        if i + 1 < len(scaffold_lines):
            window_end = scaffold_lines[i + 1].start
        else:
            window_end = max(sline.start + 8.0, audio_end)
        window = _whisper_window_tokens(whisper_words, sline.start, window_end)

        candidates: list[TimedLine] = [sline]
        for src_name, src_lines in by_source.items():
            if src_name == scaffold_name:
                continue
            best: TimedLine | None = None
            best_dt = _LINE_MATCH_WINDOW_S
            for cand in src_lines:
                dt = abs(cand.start - sline.start)
                if dt < best_dt:
                    best_dt = dt
                    best = cand
            if best is not None:
                candidates.append(best)

        only_scaffold = len(candidates) == 1
        if multi_source_drop_active and only_scaffold and not window:
            logger.info(
                "consensus: dropped scaffold-only line @ %.2fs (silent window): %r",
                sline.start,
                sline.text[:60],
            )
            continue

        if window:
            scored = [
                (_line_score(window, c.text), c) for c in candidates
            ]
            scored.sort(key=lambda x: x[0], reverse=True)
            chosen = scored[0][1]
        else:
            # No whisper window but multiple sources agree on the position —
            # keep scaffold's text (its ranking already preferred it).
            chosen = sline

        out_lines.append(
            TimedLine(start=sline.start, text=chosen.text, source_name=chosen.source_name)
        )

    # ---- Pass 2: extras (lines absent from scaffold) ----
    inserted_keys: set[tuple[float, str]] = set()
    for src_name, src_lines in by_source.items():
        if src_name == scaffold_name:
            continue
        for cand in src_lines:
            close_to_scaffold = any(
                abs(cand.start - sl.start) < _LINE_MATCH_WINDOW_S for sl in scaffold_lines
            )
            if close_to_scaffold:
                continue

            supporters = 0
            for other_name, other_lines in by_source.items():
                if other_name in (scaffold_name, src_name):
                    continue
                for olin in other_lines:
                    if abs(olin.start - cand.start) < _LINE_MATCH_WINDOW_S:
                        supporters += 1
                        break
            if supporters < 1:
                logger.info(
                    "consensus: dropped extra line (single source) @ %.2fs from %s: %r",
                    cand.start,
                    src_name,
                    cand.text[:60],
                )
                continue

            window = _whisper_window_tokens(
                whisper_words, max(0.0, cand.start - 0.5), cand.start + 6.0
            )
            score = _line_score(window, cand.text)
            if score < _EXTRA_LINE_WHISPER_GATE:
                logger.info(
                    "consensus: dropped extra line (whisper score %.2f < %.2f) @ %.2fs: %r",
                    score,
                    _EXTRA_LINE_WHISPER_GATE,
                    cand.start,
                    cand.text[:60],
                )
                continue

            key = (round(cand.start, 1), cand.text)
            if key in inserted_keys:
                continue
            inserted_keys.add(key)
            out_lines.append(
                TimedLine(start=cand.start, text=cand.text, source_name=src_name)
            )

    out_lines.sort(key=lambda x: x.start)
    return out_lines


def lines_to_lrc(timed_lines: list[TimedLine]) -> str:
    """Emit a standard LRC string from the merged TimedLine sequence."""
    out: list[str] = []
    for tl in timed_lines:
        mm = int(tl.start // 60)
        ss = tl.start - mm * 60
        out.append(f"[{mm:02d}:{ss:05.2f}]{tl.text}")
    return "\n".join(out)


# ---------- Top-level ----------


def _aggregate_text(timed_lines: list[TimedLine]) -> str:
    """Lowercase token sequence joined by spaces — what the aligner consumes."""
    tokens: list[str] = []
    for tl in timed_lines:
        tokens.extend(normalize_tokens(tl.text))
    return " ".join(tokens)


def _compute_confidence(
    sources: list[SourceResult],
    survivors: list[SourceResult],
    coverages: list[float],
    *,
    audio_ref_present: bool,
    confidence_penalty: float,
) -> float:
    title_total = sum(1 for s in sources if s.kind == "title_matched")
    title_surviving = sum(1 for s in survivors if s.kind == "title_matched")
    title_rate = title_surviving / title_total if title_total else 1.0
    title_cov = sum(coverages) / len(coverages) if coverages else 1.0
    base = 0.6 if audio_ref_present else 0.0
    return (base + (1.0 - base) * title_rate * title_cov) * confidence_penalty


def build_consensus(sources: list[SourceResult], audio_ref: list[str]) -> ConsensusResult | None:
    """Cross-validate sources against the audio reference, emit a line-merge LRC.

    Three paths:

    * **Empty audio_ref** — no Whisper / no VTT. Fall back to the highest-
      ranked title-matched source's LRC verbatim, with a confidence
      penalty so downstream knows to trust it less.
    * **Synced survivors + Whisper words** — main path: walk scaffold
      lines, pick best phrasing per Whisper window, drop scaffold-only
      lines on silent windows, optionally insert extras when ≥2 sources
      agree.
    * **No synced + no Whisper but audio_ref present** — fall back to the
      best-ranked scaffold's LRC unchanged.

    Returns ``None`` when:

    * no sources at all,
    * no audio_ref AND no title-matched candidates,
    * every title-matched source falls below its rejection threshold and
      no audio-ref-derived scaffold survives,
    * resulting confidence below ``_CONFIDENCE_MIN``.
    """
    if not sources:
        return None

    # ---- Empty audio_ref ----
    if not audio_ref:
        title_matched = [s for s in sources if s.kind == "title_matched"]
        if not title_matched:
            return None
        title_matched.sort(key=lambda s: _SCAFFOLD_RANK.get(s.name, 99))
        chosen = title_matched[0]
        chosen_tokens = _source_tokens(chosen)
        if not chosen_tokens:
            return None
        return ConsensusResult(
            text=" ".join(chosen_tokens),
            lrc=chosen.lrc or "",
            sources_used=[chosen.name],
            sources_rejected=[],
            confidence=_CONFIDENCE_PENALTY_NO_AUDIO_REF,
            source_scores={},
        )

    # ---- Score sources, reject low coverage ----
    survivors: list[SourceResult] = []
    rejected: list[tuple[str, str]] = []
    source_scores: dict[str, tuple[float, bool]] = {}
    coverages: list[float] = []
    audio_ref_owners = {"vtt", "whisper"}

    for source in sources:
        if source.name in audio_ref_owners:
            survivors.append(source)
            continue
        tokens = _source_tokens(source)
        coverage, uncertain = score_against_reference(tokens, audio_ref)
        source_scores[source.name] = (coverage, uncertain)
        threshold = _threshold_for(source.name)
        if coverage < threshold:
            rejected.append((source.name, f"coverage {coverage:.2f} < {threshold:.2f}"))
            continue
        survivors.append(source)
        coverages.append(coverage)

    if not survivors:
        return None

    whisper_source = next(
        (s for s in sources if s.name == "whisper" and s.words), None
    )
    synced_survivors = [
        s for s in survivors if s.is_synced and s.lrc and s.name != "whisper"
    ]

    # ---- Main path: line-merge ----
    if synced_survivors and whisper_source is not None and whisper_source.words:
        merged = merge_lines_per_window(synced_survivors, whisper_source.words)
        if merged:
            confidence = _compute_confidence(
                sources, survivors, coverages,
                audio_ref_present=True, confidence_penalty=1.0,
            )
            if confidence < _CONFIDENCE_MIN:
                logger.info(
                    "consensus: confidence %.2f below %.2f gate, skipping T3",
                    confidence,
                    _CONFIDENCE_MIN,
                )
                return None
            return ConsensusResult(
                text=_aggregate_text(merged),
                lrc=lines_to_lrc(merged),
                sources_used=[s.name for s in survivors],
                sources_rejected=rejected,
                confidence=confidence,
                source_scores=source_scores,
            )

    # ---- Fallback path: pick best scaffold's LRC verbatim ----
    scaffold = select_scaffold(survivors, set())
    if scaffold is None:
        return None

    if scaffold.lrc:
        lrc_out = scaffold.lrc
        body_text = _strip_lrc_tags(scaffold.lrc)
        text_out = " ".join(normalize_tokens(body_text))
    elif scaffold.words:
        lines: list[str] = []
        for w in scaffold.words:
            ws = getattr(w, "start", None)
            if ws is None:
                continue
            mm = int(ws // 60)
            ss = ws - mm * 60
            lines.append(f"[{mm:02d}:{ss:05.2f}]{w.text}")
        lrc_out = "\n".join(lines)
        text_out = " ".join(normalize_tokens(" ".join(w.text for w in scaffold.words)))
    else:
        return None

    if not lrc_out or not text_out:
        return None

    confidence = _compute_confidence(
        sources, survivors, coverages,
        audio_ref_present=True, confidence_penalty=1.0,
    )
    if confidence < _CONFIDENCE_MIN:
        logger.info(
            "consensus: confidence %.2f below %.2f gate (fallback path)",
            confidence,
            _CONFIDENCE_MIN,
        )
        return None

    return ConsensusResult(
        text=text_out,
        lrc=lrc_out,
        sources_used=[s.name for s in survivors],
        sources_rejected=rejected,
        confidence=confidence,
        source_scores=source_scores,
    )
