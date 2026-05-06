<!-- /autoplan restore point: /Users/zygzagz/.gstack/projects/zygzagZ-pikaraoke/master-autoplan-restore-20260504-163101.md -->
<!-- prior restore point: /Users/zygzagz/.gstack/projects/zygzagZ-pikaraoke/master-autoplan-restore-20260504-143641.md -->
# Phase 2: subtitle status chips — todo

Scoped-down implementation of `subtitle-status-chips-phase-2.md`.
Pushes back on speculative config; defers retry-stub to Phase 4.

## Scope decisions (vs. original plan)

- **Drop** `SUBTITLE_CHIPS_DENSITY` and `SUBTITLE_CHIPS_AUTOCOLLAPSE_MS`
  preferences. Hardcode: compact via mobile media query, full on desktop;
  4s autocollapse on splash, none elsewhere.
- **Drop** long-press → 501 retry stub. Failed chips show error in tooltip;
  retry click + endpoint comes with Phase 4 (when retry actually works).
- **Move** `GET /api/songs/<id>/subtitles` from `routes/admin.py` to a new
  `routes/subtitle_jobs.py` blueprint, add `POST /api/songs/subtitles/bulk`
  there. Keeps `admin.py` from sprawling further.
- **Centralize source labels** server-side. `_SUBTITLE_SOURCE_LABELS` in
  `karaoke.py` becomes the source of truth; the API endpoints embed
  `label` per source in the response. Splash.js's `LYRICS_SOURCE_LABELS`
  becomes a fallback used only when the API hasn't responded yet.
- **Canonical chip order** = `DEFAULT_AUTO_SOURCES` from
  `subtitle_orchestrator.py` (`youtube-vtt` first, fast wins on the
  left), with `user` prepended. The dropdown's `_SUBTITLE_SOURCE_ORDER`
  in `karaoke.py` keeps its order for backward-compat where still used.

## Files

### New

- [ ] `pikaraoke/static/js/subtitle-chips.js` — shared component
  (`createChipRow(songId, options)` returning `{ el, update(data), destroy() }`).
- [ ] `pikaraoke/static/css/subtitle-chips.css` — chip styles using
  `var(--pk-*)` tokens, both themes.
- [ ] `pikaraoke/routes/subtitle_jobs.py` — new blueprint:
  - `GET /api/songs/<song_id>/subtitles` (moved from admin.py)
  - `POST /api/songs/subtitles/bulk` `{ song_ids: [int, ...] }` →
    `{ <song_id>: { active_source, sources: [...] } }` for queue rosettes.
- [ ] `tests/js/subtitle-chips.test.js` — render states from fixtures
  (success/active, success/inactive, running, queued, failed, rate_limited).
- [ ] `tests/unit/test_subtitle_jobs_routes.py` — bulk endpoint tests
  (admin gate, missing ids, partial-found, malformed body, label embedded).

### Modified

- [ ] `pikaraoke/karaoke.py` — register `subtitle_jobs_bp`. Embed `label`
  field per source in payloads (or expose `/api/subtitle_sources/labels`).
- [ ] `pikaraoke/routes/admin.py` — remove the `song_subtitles` route
  (moved to new blueprint).
- [ ] `pikaraoke/templates/splash.html` — replace `#lyrics-source` badge
  with `<div id="subtitle-chips" data-pk-chips></div>`.
- [ ] `pikaraoke/static/js/splash.js` — drop `updateLyricsSourceBadge`,
  fetch initial state from API, mount chip row, subscribe to
  `subtitle_job_update`. Autocollapse during playback, full when idle.
- [ ] `pikaraoke/templates/base.html` — replace `data-pk-subtitle-src`
  `<select>` with `<div data-pk-subtitle-chips>`. Include
  `subtitle-chips.css`/`.js`.
- [ ] `pikaraoke/static/js/now-playing-bar.js` — drop
  `updateSubtitleSourceSelect` and dropdown change handler; mount chip
  row, click → `POST /subtitle_source`.
- [ ] `pikaraoke/templates/edit.html` — add `<section
  class="pk-subtitle-state"><h3>Aktualny stan napisów</h3><div
  data-pk-chips></div></section>` above the `.pk-events` block.
  Initialize chip row + `io()` socket connection in inline script.
- [ ] `pikaraoke/templates/home.html` — extend `renderQueue()` to add
  rosette `<div class="pk-queue-rosette" data-song-id></div>` inside
  `.pk-queue-body`. After render, batch-fetch via bulk endpoint, mount
  chip rows in compact (rosette) mode. Subscribe to
  `subtitle_job_update` and update affected rosettes.

## Implementation order

1. **Bulk endpoint + new blueprint** (server-side, low risk, easy to test).
2. **Move existing single endpoint to new blueprint** (compatibility — no
   URL change, just file move).
3. **`subtitle-chips.js` component** + CSS, with vitest tests covering
   render of fixture payloads.
4. **Splash wiring** (most visible, simplest UI — single chip row).
5. **Now-playing-bar wiring** (replaces working dropdown — verify regression).
6. **Edit view wiring** (additive, no replacement).
7. **Queue rosettes** (most layout work, depends on bulk endpoint).
8. Pre-commit + `uv run pytest` + `npm run test:js`.
9. Manual e2e: open splash + remote on phone, queue a song, watch chips
   light up; click a chip on remote, splash switches.

## Out of scope (Phase 3/4)

- Long-press / right-click context menu on chips.
- Retry button on failed chip.
- Drift correction `<` / `>`.
- A/B compare split-screen.
- Auto-promotion of best source.

## Acceptance (from plan)

- Splash: mid-playback, `spotify-sync` finishes → chip shimmers.
- Remote: tap `tekstowo-sync` chip → splash subtitle source switches
  without page reload (verifies `POST /subtitle_source` path).
- Queue row: shows `5/7` rosette; expand reveals which two sources
  failed with reasons in tooltip.
- Edit view: auto-updates as enrichment progresses, no Refresh click
  needed for chip state (timeline still uses Refresh for the log).

---

# /autoplan — Phase 1 (CEO) findings

Voices: Plan agent (primary, full plan-ceo-review methodology) + Claude subagent
(independent CEO/strategist, cold read). Codex unavailable. Tag: **[subagent-only]**.
Full CEO artifact: `~/.gstack/projects/zygzagZ-pikaraoke/master-ceo-review-20260504-143641.md`.

## CEO Dual Voices — Consensus Table

| Dimension | Plan agent | Subagent | Consensus |
|-----------|-----------|----------|-----------|
| 1. Premises valid? | Mostly valid; chip-vs-dropdown caveats on 320px phone | DISAGREES — operator/guest conflated, dropdown better on phone | **DISAGREE** → premise gate |
| 2. Right problem to solve? | YES with caveats | DISAGREES — replacing dropdown solves wrong pain | **DISAGREE** → premise gate |
| 3. Scope calibration? | NO — 11 files >8 threshold; recommend Approach C | NO — kill new blueprint, kill remote chip row | **CONFIRMED — too wide, Approach C** |
| 4. Alternatives explored? | NO — surfaced A/B/C; weren't in plan | NO — badge-per-option not considered | **CONFIRMED — gap** |
| 5. Bulk API contract? | Queue items lack `song_id` (resolution gap) | Dict-by-song_id silently dedupes duplicates | **CONFIRMED — broken from two angles** |
| 6. 6-month trajectory? | Sound with gaps | Multiple gaps — bulk dedupe, blueprint sprawl | **DISAGREE on severity** |

CONFIRMED across both voices: scope is too wide; alternatives missing; bulk endpoint
contract is broken; new blueprint is questionable in single-owner repo.

## Critical scope gaps to resolve before implementation

1. **Queue items lack `song_id`** (Plan agent §0A, §0B). The bulk endpoint as
   designed cannot serve queue rosettes without resolving `file → song_id`.
   Either add `song_id` to queue items in `queue_manager.py`, or change bulk API
   to accept `{files: [str]}`. Outside chip blast radius — Approach C defers.
2. **Edit view second-`io()` bug** (Plan agent §0A). `edit.html` extends
   `base.html`, which already creates `window.socket` at line 103. Plan as
   written would create a second socket connection. Use `window.socket`.
3. **No JS-load fallback** (Plan agent §7). If `subtitle-chips.js` fails to load
   or throw on mount, operator loses source picker entirely. Keep `<select>`
   hidden as fallback; show it if chip mount fails.
4. **Pre-Phase-1 songs have empty `subtitle_jobs`** (Plan agent §0E Hour 1).
   Chip row must render `DEFAULT_AUTO_SOURCES` as `na/skipped` chips when API
   returns empty `sources` — not blank.
5. **`label` missing from `subtitle_job_update` socket payload** (Plan agent
   §0B). One-line `orchestrator.py:307` change to embed `label` so chip
   component doesn't need separate label-registry fetch.
6. **MAX_BULK undefined** (Plan agent §5). DoS risk on Pi 3 with large
   `{song_ids: [...]}`. Enforce MAX_BULK=100, return 400 if exceeded.
7. **Component `destroy()` lifecycle not explicit** (Plan agent §1). Song
   change in now-playing-bar without `destroy()` = leaked socket listener.
8. **JS tests violate CLAUDE.md** (Plan agent §3). Plan listed
   "render states from fixtures" — DOM testing. CLAUDE.md prohibits DOM
   mocking in vitest. Extract pure functions: `computeRosetteLabel`,
   `deriveChipState`, `computeCountdown`, `sortSourcesCanonically`.
9. **Bulk API contract: dict-by-song_id silently dedupes** (Subagent #3).
   Same song queued twice → only one entry returned. Either return list keyed
   by queue position, or document the dedup behavior explicitly.
10. **`error_message` rendered as innerHTML risks XSS** (Plan agent §5).
    Use `textContent` or `setAttribute('title', ...)`.

## Expansion items added to scope (auto-decided per P1+P2)

1. Rate-limited countdown tooltip ("Dostępne za 47m") — `next_retry_at` already
   in API. ~5 lines JS. **IN SCOPE.**
2. Shimmer animation (`@keyframes pk-chip-shimmer`) — ~10 lines CSS. **IN SCOPE.**
3. `aria-live="polite"` region for screen-reader chip transitions — ~8 lines.
   **IN SCOPE.**
4. Skeleton loading state (placeholder chips before initial fetch) — ~20 lines.
   **IN SCOPE.**
5. Keyboard focus on active chip on mount — ~2 lines. **IN SCOPE.**
6. `label` field added to `subtitle_job_update` socket payload — one-line
   orchestrator change. **IN SCOPE (CRITICAL prerequisite).**

## Deferred to TODOS / later phases

- Per-source latency badge in tooltip → Phase 3 (data exists, design unsettled)
- Long-press context menu / Retry button → Phase 4
- Drift correction `<` `>` → Phase 4
- A/B compare → Phase 3
- Auto-promotion → Phase 3
- (Conditional — pending premise gate) Queue rosettes → Phase 2.5 if Approach C
- (Conditional — pending premise gate) Edit view chip wiring → Phase 2.5 if Approach C

## TASTE DECISIONS — surfaced at Phase 4 final gate

- **Approach A vs C:** Both voices recommend C (scope split). Plan agent's
  Approach C drops Phase 2 to 7 files (within complexity threshold).
- **New blueprint vs inline in admin.py:** Plan agent recommends new blueprint
  (separation of concerns); subagent recommends inline (single-owner simplicity).
  CLAUDE.md tilts subagent's way.
- **Source order:** hard-coded in JS vs returned canonically-ordered from API.
  Plan agent prefers API (DRY); my original plan prefers JS constant.
- **`subtitle-chips.js` module type:** ES module (testable via vitest import) vs
  classic global script (current project pattern).
- **Subagent's "keep `<select>` with status badges" alternative** — was this
  given fair consideration?

## Decision Audit Trail (auto-decided this phase)

| # | Phase | Decision | Principle | Rationale |
|---|-------|----------|-----------|-----------|
| 1 | CEO | Add rate-limited countdown tooltip to scope | P1, P2 | In blast radius; data already in API |
| 2 | CEO | Add shimmer animation to scope | P1 | Plan mentions it; formalize as keyframe |
| 3 | CEO | Add `aria-live` region to scope | P1 | A11y completeness; ~8 lines |
| 4 | CEO | Add skeleton loading state to scope | P2 | Prevents layout jump; in blast radius |
| 5 | CEO | Add keyboard focus on active chip | P1 | 2 lines; no reason to defer |
| 6 | CEO | Add `label` to socket event payload | P1, prerequisite | Without this, chip component needs separate label fetch |
| 7 | CEO | Defer per-source latency badge to Phase 3 | P3 | Data exists but tooltip design unsettled |
| 8 | CEO | Recommend Approach C over A | P5, P3 | Explicit over clever; queue payload `song_id` change is outside chip blast radius |
| 9 | CEO | Mark "Approach A vs C" as taste decision for Phase 4 | n/a | User judgment on PR-splitting |
| 10 | CEO | Mark "new blueprint vs inline admin.py" as taste decision | n/a | Both voices disagree |
| 11 | CEO | Source order returned from API canonically (not hard-coded JS) | P4 (DRY) | Single source of truth for ordering |

---

# /autoplan — premise gate RESOLVED (2026-05-04)

User locked in the UI direction after reviewing real HTML mockups
(`/tmp/pikaraoke-mockups/index.html`). The original "chip row everywhere"
plan is REJECTED in favor of a more conservative, context-appropriate
treatment per surface.

## LOCKED — Phase 2 UI direction

### Splash (TV)
- **Single corner badge** at **top-left** (top-right is `#top-container`,
  bottom corners are `#bottom-container` / up-next).
- Shows **only the active source + status**. No row of chips, no shimmer
  on other sources, no autocollapse.
- Three states: **ready** (green dot, steady), **downloading** (amber
  spinner, brief), **error** (red border, "brak napisów" if no source
  available).
- Replaces existing `#lyrics-source` element in `splash.html:65-68`.
  Same data feed (`now_playing.now_playing_lyrics_source` +
  `subtitle_job_update` socket for status overlay), simpler render.
- Operator monitors and switches from the remote, not the splash.

### Phone / now-playing-bar (full panel)
- **Smart dropdown** in the **existing `pk-tool-subtitle-src` slot**
  inside the now-playing-bar full panel (`base.html:529`). DO NOT MOVE
  the slot.
- **Reorder slot position: BELOW Wokal + Podkład sliders.** Source
  switching is less common than volume tweaks, so it sinks. Order in
  `npb-tools` becomes: Wokal → Podkład → Źródło.
- Native `<select>` is replaced by a custom popover button:
  - Trigger: `[● LRCLib + sync]   [5/7 ✓]   [▼]` — active glyph + label
    + ready-count summary + arrow.
  - Popover: each row is `[glyph] [label] [status suffix]` with
    `running`/`failed`/`rate_limited`/`queued` rows disabled
    (visible-but-unpickable).
  - 40px row height for tap targets; native popover dismiss on
    outside-tap.
  - **Auto-flip placement:** popover opens upward (above the trigger)
    when there's no room below — required because the slot now sits
    below Wokal/Podkład, near the bottom of the panel. Detect space
    via `getBoundingClientRect()` on open; flip class chooses
    `top: auto; bottom: 100%+4px` vs default `top: 100%+4px`.
  - `max-height: 320px` + `overflow-y: auto` on the popover for
    safety if there are ever more than 8 sources.
  - Signature-based re-render guard like the existing `pkSig` pattern
    in `now-playing-bar.js:489`.

### Queue list
- **Rosette `N/7` per row** at the right edge of each `.pk-queue-row`
  in `home.html:473-491`. Color-coded count: green (≥4), amber (1–3),
  red (0), spinner (any source running).
- Tap to expand a compact mini-chip-row inline. Fed by bulk endpoint.
- **DEFERRED** to Phase 2.5 if `song_id` gap (queue items lack
  `song_id`) is not resolved in this PR.

### Edit view
- Live-updated **chip row** above the existing `Historia przetwarzania`
  block. Read-only display of all sources with state. Uses
  `window.socket` (NOT a second `io()`).
- This is the only surface where the full chip-row component is shown.

## Resolved decisions (moved out of TASTE → CONFIRMED)

| Original taste decision | Resolution |
|---|---|
| Chip row vs dropdown on remote | **DROPDOWN WINS** — smart popover, kept in current slot, reordered below sliders |
| Chip row vs single badge on splash | **BADGE WINS** — top-left corner, 3 states only |
| Approach A vs C scope split | **C-LIKE** — splash badge + remote dropdown ship in Phase 2; queue rosette + edit chip row in Phase 2.5 (gated on `song_id` resolution) |
| Subagent's "keep `<select>` with badges" alternative | **VALIDATED** — was the right call |

## File count (revised)

Phase 2 (now): **6 files** (within 8-file threshold ✓)
- NEW: `pikaraoke/static/js/subtitle-source-picker.js` — smart dropdown
  for now-playing-bar + corner badge for splash. Single module, two
  exported render functions.
- NEW: `pikaraoke/static/css/subtitle-source-picker.css` — popover +
  badge styles using `--pk-*` tokens.
- NEW: `pikaraoke/routes/subtitle_jobs.py` — blueprint with
  `GET /api/songs/<id>/subtitles` (moved from admin.py) +
  `POST /api/songs/subtitles/bulk` (will be used in Phase 2.5).
- MODIFIED: `pikaraoke/templates/splash.html` — replace
  `#lyrics-source` with the corner badge mount point.
- MODIFIED: `pikaraoke/templates/base.html` — keep
  `pk-tool-subtitle-src` slot, REORDER to be after Wokal/Podkład,
  swap `<select>` for the smart-picker mount.
- MODIFIED: `pikaraoke/static/js/now-playing-bar.js` — drop
  `updateSubtitleSourceSelect`, mount the smart picker.

Plus required Phase 1 amendments (one-line each):
- `pikaraoke/lib/subtitle_orchestrator.py:307` — embed `label` in
  `subtitle_job_update` payload.
- `pikaraoke/karaoke.py` — register `subtitle_jobs_bp`.
- `pikaraoke/routes/admin.py` — remove `song_subtitles` (moved).

Phase 2.5 (later, if `song_id` gap resolves): **4 files** (queue
rosette, edit view chip row, queue_manager.py change to embed
`song_id`, JS mini-chip-row helper).

## Implementation order (revised)

1. Move `GET /api/songs/<id>/subtitles` from `admin.py` to new
   `routes/subtitle_jobs.py` blueprint. Add `POST /api/songs/subtitles/bulk`
   with `MAX_BULK=100` enforcement. Tests in
   `tests/unit/test_subtitle_jobs_routes.py`.
2. Add `label` to `subtitle_job_update` socket payload
   (`subtitle_orchestrator.py:307`). Update existing test asserts.
3. Embed `label` per source in `GET /api/songs/<id>/subtitles` response.
4. Create `subtitle-source-picker.js` (smart dropdown) +
   `subtitle-source-picker.css`. Pure-function exports for
   `computeReadySummary`, `deriveOptionState`, `computeCountdown`,
   `sortSourcesCanonically` — vitest tests in
   `tests/js/subtitle-source-picker.test.js`.
5. Add corner-badge render function to same module (small variant of
   the picker logic; just active source + status).
6. Wire splash: replace `#lyrics-source` with the corner badge mount.
   Listen for `subtitle_job_update` filtered by current `song_id`.
7. Wire now-playing-bar: REORDER `pk-tool-subtitle-src` to be after
   Wokal/Podkład in `base.html`. Replace `<select>` with smart picker.
   Drop `updateSubtitleSourceSelect`. Click on enabled option →
   `POST /subtitle_source` (existing endpoint, no change).
8. Pre-commit + `uv run pytest tests/` + `npm run test:js`.
9. Manual e2e: open splash + remote on phone (admin), queue a song.
   Verify badge updates state. Verify smart picker switches source
   without page reload. Verify mobile tap targets feel right.

## Out of scope (deferred to Phase 2.5 / 3 / 4)

- Queue rosette + bulk endpoint wiring (Phase 2.5; bulk endpoint is
  built in Phase 2 but not consumed until queue items get `song_id`).
- Edit view live chip row (Phase 2.5).
- Long-press / context menu / retry button (Phase 4).
- Auto-promotion (Phase 3).
- A/B compare (Phase 3).
- Drift correction (Phase 4).

## Updated decision audit trail

| # | Phase | Decision | Principle | Rationale |
|---|-------|----------|-----------|-----------|
| 12 | CEO→user | **Splash:** single corner badge, top-left, 3 states. NOT chip row. | User direction after mockup review | Splash is ambient status, not control surface. Operator monitors via remote. |
| 13 | CEO→user | **Remote:** smart dropdown in existing `pk-tool-subtitle-src` slot. NOT chip row. | User direction; matches subagent #1 / #2 / #4 findings | Phone tap targets, parallel state via summary, no horizontal scroll. |
| 14 | CEO→user | **Slot order:** Subtitle source moves BELOW Wokal + Podkład sliders. | User direction | Source switching is less common than volume tweaks. |
| 15 | CEO→user | **Approach:** C-like split (Phase 2 = splash + remote + bulk endpoint; Phase 2.5 = queue + edit). | Plan agent recommendation, accepted | Queue payload `song_id` change is outside chip blast radius; clean PR separation. |
| 16 | CEO→user | **Component naming:** `subtitle-source-picker.js` (not `subtitle-chips.js`) since chips are not the primary primitive anymore. | P5 explicit | Picker is the dominant UI; chips appear only in queue rosette expand + edit view (Phase 2.5). |
| 17 | CEO→user | **Subagent #6 LOW (new blueprint bureaucracy):** OVERRIDDEN. Keep `routes/subtitle_jobs.py` because it'll house bulk endpoint + Phase 2.5's queue endpoints. Consolidating in admin.py would force another move later. | P3 pragmatic | Two endpoints today, four in Phase 2.5+. Worth the file move now. |

---

# /autoplan — Phase 2 (Design review) on the LOCKED direction

The original Phase 2 design review scope was "chip row everywhere" — that's
gone. The locked direction is much narrower: corner badge + smart dropdown
in existing slot. Design review focuses on:

- Corner badge: visibility against arbitrary video backgrounds (the splash
  shows YouTube videos as background — backdrop-filter is the proposed
  mitigation; verify it works).
- Smart dropdown: 44px tap targets, popover dismiss UX, what happens
  when active source becomes invalid mid-popover-open.
- Error state alarm-ness: "brak napisów" red badge — does the operator
  notice it from across the room? Is this the right alarm?
- Theme contrast: badge legibility in both Mazury (light) + Late Show (dark).

Will dispatch the Phase 2 design review next.

---

# /autoplan — Phase 2 (Design review) findings

Voices: primary review (this agent, plan-design-review methodology) + Claude
design subagent (independent cold read of plan + mockup). **Codex unavailable** —
tag `[subagent-only]`. Both reviewers focused only on the LOCKED direction
(corner badge + smart popover) — premise gate not re-litigated.

## Design Litmus Scorecard — Consensus

| Dimension | Primary | Subagent | Consensus |
|---|---|---|---|
| 1. Information hierarchy | 7/10 — slot reorder good; alarm→action gap | 7/10 — same gap | **CONFIRMED 7/10** |
| 2. Specificity (UI vs generic) | 6/10 — pkSig composition + flip fallback unspecified | 6/10 — same gaps | **CONFIRMED 6/10** |
| 3. Missing states coverage | 5/10 — null/pending state, stale popover, POST-fail | 4/10 — same + skeleton mismatch | **CONFIRMED 4-5/10** |
| 4. Accessibility | 4/10 — 40px taps, 9px status, no keyboard spec | 4/10 — same | **CONFIRMED 4/10** |
| 5. Visual contrast strategy | 5/10 — backdrop-filter alone insufficient on bright frames | 5/10 — 5 specific WCAG fails | **CONFIRMED 5/10** |
| 6. Motion / transitions | 7/10 — open/close has no animation | 7/10 — same | **CONFIRMED 7/10** |
| 7. Error & recovery UX | 4/10 — POST-fail unspecified, alarm too quiet | 3/10 — same + optimistic UI missing | **CONFIRMED 3-4/10** |

**Cross-voice agreement: high.** Both reviewers landed on the same structural
gaps independently. Subagent went deeper on specific WCAG colour pairs (verified
against mockup `tokens.css` copies); primary went deeper on splash-overlay layout
collision (`#ap-container` already at top-left).

## Findings — auto-decide rules

Per /autoplan Phase 2 rules:
- **Structural** (missing states, broken hierarchy, a11y) → auto-fix (P5)
- **Aesthetic/taste** → mark TASTE DECISION
- **Design-system alignment** with obvious fix → auto-fix

### Pass 1: Layout & spatial

**P1.1 — `#ap-container` ↔ corner badge collision at top-left** *(primary)*
`splash.html:95-100` already has `#ap-container` (clock + hostap info) at top-left.
Mockup positions badge at `top: 18px; left: 22px` — same airspace. Plan didn't
acknowledge the existing element. **Severity: HIGH (regression risk).**
**Auto-fix (P5):** Position badge at `top: 18px; left: 22px` and shift
`#ap-container` down via `padding-top: 56px` when badge is mounted (or stack:
badge above ap-container). Implementation note: keep badge `z-index` above
`ap-container`; verify ap-container's clock font isn't clipped by the badge's
shadow on small TVs (720p edge case).

**P1.2 — Trigger row visual height vs interactive height** *(both)*
Mockup `.pk-tool-subtitle-src { padding: 12px 14px }` outer + `.smart-trigger
{ min-height: 40px }` inner = 64px visible row but only 40px clickable hit area.
**Severity: HIGH (a11y).**
**Auto-fix (P1+P5):** Set `.smart-trigger` to `min-height: 44px` and bump option
rows to `min-height: 44px`. Total width unaffected. iOS HIG compliant.

### Pass 2: Visual contrast & legibility

**P2.1 — Badge contrast against bright video frames** *(both)*
`rgba(0,0,0,0.45)` + `backdrop-filter: blur(6px)` is insufficient on white
title cards / bright concert footage. Subagent: text-shadow pattern already
exists on `.sp-now-title` (`text-shadow: 0 2px 16px rgba(0,0,0,0.8)`). **Severity: HIGH.**
**Auto-fix (P5):** Adopt the same pattern — add `text-shadow: 0 1px 4px
rgba(0,0,0,0.9)` to badge text, bump background to `rgba(0,0,0,0.58)`, add
`box-shadow: 0 0 0 1px rgba(0,0,0,0.4)` for outer ring. (DESIGN.md doesn't
exist; aligning with established splash overlay pattern instead.)

**P2.2 — `.opt-status` 9px text fails AA at any colour** *(subagent)*
9px is below WCAG AA size threshold for normal text. **Severity: HIGH (a11y).**
**Auto-fix:** Bump to 11px minimum.

**P2.3 — Specific WCAG-AA failures** *(subagent, cross-checked vs mockup tokens)*
Five pairings flagged: Mazury `--pk-success: #6a9080` on badge bg (≈3.1:1);
Mazury `--pk-amber: #3a86ad` summary pill on `--pk-bg: #f5ecd4` (≈3.6:1);
Mazury `.opt-status` `#94825e` on `#f5ecd4` (≈3.2:1); Late Show
`.opt-glyph.rate` `#ff7a45` at 9px; both themes 9px uppercase text.
**Severity: HIGH (a11y).**
**Auto-fix:** P2.2 fixes the size issue. For the colour issues: use the
already-existing `--pk-lime: #d5ff5e` (Late Show success) and bump Mazury
`--pk-success` glyph contrast via `text-shadow: 0 0 6px var(--pk-success)` glow.
For status chips on disabled rows, switch to `--pk-ink-2` (darker than `--pk-ink-3`).

### Pass 3: Missing states

**P3.1 — Null/pending source on mount** *(both)*
`now_playing.now_playing_lyrics_source` is null between song-start and first
subtitle load. Plan defines only ready/downloading/error. **Severity: HIGH.**
**Auto-fix (P1):** Add 4th state: `pending` — neutral grey dot, label "czekaj…".
Show when `active_source` is null OR API hasn't responded yet. Also covers the
splash skeleton requirement carried over from Phase 1 CEO.

**P3.2 — Stale popover during song change** *(subagent)*
Operator opens popover; song changes mid-open; rendered rows now reflect previous
song. **Severity: HIGH.**
**Auto-fix (P5):** On song change (`now_playing_update` with different basename),
close popover immediately and re-render trigger. Don't silently rewrite open rows.

**P3.3 — POST `/subtitle_source` failure** *(subagent)*
On server 4xx/5xx the popover closes silently and trigger shows old source —
operator has no signal. **Severity: HIGH.**
**Auto-fix (P5):** On row tap → optimistic spinner glyph on tapped row + disable
trigger. On 2xx → close popover, wait for socket update. On 4xx/5xx → restore
row, call `showNotification(message, 'is-error')` (existing helper at
`base.html:112`), keep popover closed.

### Pass 4: Accessibility — keyboard

**P4.1 — Keyboard navigation unspecified** *(primary)*
Plan calls out `aria-haspopup="listbox"` but doesn't specify Esc/Arrow/Enter
behaviour. **Severity: MEDIUM.**
**Auto-fix:** Specify in `subtitle-source-picker.js`:
- `Enter`/`Space` on trigger → open popover, focus active row
- `ArrowUp`/`ArrowDown` → cycle enabled rows (skip disabled)
- `Enter`/`Space` on row → trigger tap path
- `Escape` → close popover, return focus to trigger
- `Tab` → close popover and continue tab order

### Pass 5: Error-state alarm-ness

**P5.1 — Error badge too quiet at distance** *(subagent)*
Hot pink (Late Show `--pk-danger: #ff4d8f`) reads as "accent" not "alarm" from
2-3m. Static border colour change is insufficient. **Severity: MEDIUM.**
**Decision:** TASTE — competing principles. P1 (completeness) says add a pulse;
P3 (pragmatic) says the operator knows the splash is decorative and watches the
remote. Recommended: subtle 2s background pulse `rgba(danger, 0.15) ↔
rgba(danger, 0.35)` — closer to "ambient indicator" than "klaxon". Surface to
user at gate.

**P5.2 — "brak napisów" used as both label and status** *(subagent)*
On error, what fills the badge's source-name slot? Plan implies "brak napisów"
goes there — looks like a fictional source. **Severity: MEDIUM.**
**Auto-fix (P5):** Structure: `source-name = "—"` (em-dash) + `status = "brak
napisów"`. Keeps slot semantics consistent across all 4 states.

### Pass 6: Specificity gaps

**P6.1 — `pkSig` re-render guard composition unspecified** *(subagent)*
Existing pattern guards on `source+status`. New picker needs to also guard on
`ready_count` for the summary pill. **Severity: MEDIUM (subtle bug surface).**
**Auto-fix (P5):** Specify
`pkSig = ${active_source}|${ready_count}|${sources.map(s=>s.id+':'+s.state).sort().join(',')}`.

**P6.2 — Auto-flip fallback when neither direction fits** *(subagent)*
`getBoundingClientRect()` flip works for binary above/below, but if popover
height (320px max) exceeds both clearances, what happens? **Severity: MEDIUM.**
**Auto-fix:** Open upward with `max-height: max(160px, viewport_top - 16px)`,
capped at 320px. Internal scroll absorbs overflow.

**P6.3 — Mount inside `hidden` container breaks `getBoundingClientRect`** *(subagent)*
`pk-tool-subtitle-src` uses `hidden` attribute to hide when no song. If picker
mounts during hidden state, layout calls return zeros. **Severity: HIGH.**
**Auto-fix (P5):** Picker's `mount()` checks `container.offsetParent === null`
and defers measurement until first `update()` after container becomes visible.
Document in module header.

### Pass 7: Trigger format (aesthetic)

**P7.1 — `[5/7 ✓]` redundant glyph** *(subagent)*
`5/7` already conveys "ready count"; `✓` is redundant and conflicts with the
opt-glyph vocabulary (●, ✕, ⟳). **Severity: LOW.**
**Decision:** TASTE — both work. Subagent recommends drop `✓`, colour-code
the fraction (green ≥4, amber 1-3, red 0). Surface to user at gate.

### Pass 8: Hierarchy / alarm→action gap

**P8.1 — No connection from "badge shows error" → "fix it on phone"** *(subagent)*
When badge is `error` or `downloading`, operator looking at TV has no signal
which slot on phone fixes it. **Severity: LOW.**
**Decision:** TASTE — scope expansion. Subagent recommends 3s pulse outline on
`pk-tool-subtitle-src` when badge enters error/downloading. ~10 lines. In blast
radius (P2). Recommended IN SCOPE; surface to user at gate.

## Phase 2 Decision Audit Trail

| # | Phase | Decision | Principle | Rationale |
|---|-------|----------|-----------|-----------|
| 18 | Design | Add `padding-top: 56px` shift to `#ap-container` when badge mounted | P5 | `splash.html` collision is a real regression risk plan didn't acknowledge |
| 19 | Design | Bump `.smart-trigger` + opt rows to `min-height: 44px` | P1, a11y | iOS HIG compliance; one CSS change |
| 20 | Design | Add `text-shadow` + `box-shadow` ring + bump bg to `rgba(0,0,0,0.58)` on badge | P5 | Match existing splash overlay pattern (`.sp-now-title`) |
| 21 | Design | Bump `.opt-status` from 9px → 11px | P1, a11y | WCAG size requirement |
| 22 | Design | Use `--pk-lime` (Late Show) and `text-shadow` glow (Mazury) for success colour | P5 | Cited 3 specific Mazury AA failures |
| 23 | Design | Add 4th badge state `pending` (grey dot, "czekaj…") | P1 | Covers null source + initial fetch gap |
| 24 | Design | Close popover on song change | P5 | Avoids stale rows |
| 25 | Design | Optimistic spinner on row tap; restore + showNotification on POST fail | P5 | Closes feedback dead-zone |
| 26 | Design | Specify keyboard nav (Esc/Arrows/Enter/Tab) | P1, a11y | Implementation will fall back to bad defaults otherwise |
| 27 | Design | Error badge: `source-name = "—"`, `status = "brak napisów"` | P5 | Avoids fictional-source confusion |
| 28 | Design | Specify `pkSig = ${active_source}\|${ready_count}\|${sources.map(...).sort().join(',')}` | P5 | Subtle render-guard bug otherwise |
| 29 | Design | Auto-flip fallback: `max-height: max(160px, viewport_top - 16px)` | P5 | Catches ultra-narrow phone |
| 30 | Design | Picker defers `getBoundingClientRect()` until container is visible | P5 | Hidden-attribute mount otherwise breaks flip |
| 31 | Design | Mark error pulse (P5.1) as TASTE — surface at gate | n/a | Aesthetic taste decision |
| 32 | Design | Mark trigger `✓` glyph (P7.1) as TASTE — surface at gate | n/a | Aesthetic taste decision |
| 33 | Design | Mark alarm→action pulse (P8.1) as TASTE — surface at gate | n/a | Scope expansion taste decision |

## Mandatory outputs

**NOT in scope (Phase 2):**
- Animated entry/exit for popover (open/close currently `display: none` toggle).
  Defer to Phase 3 if user wants polish (P3 pragmatic — current is shippable).
- DESIGN.md authoring (none exists; deferred — out of scope).

**What already exists:**
- `text-shadow: 0 2px 16px rgba(0,0,0,0.8)` pattern on `.sp-now-title`.
- `showNotification(message, categoryClass, timeout)` in `base.html:112`.
- `pkSig` re-render guard pattern in `now-playing-bar.js:489`.
- `--pk-lime`, `--pk-danger`, `--pk-success` tokens in both themes.
- Active source data feeds (`now_playing_update.subtitle_sources` for the
  current dropdown — partial overlap with new `/api/songs/<id>/subtitles`).

**Phase 2 design completion summary:**
- Status: **REVIEW COMPLETE** with 13 auto-fixes folded into spec, 3 taste
  decisions surfaced for user.
- Critical structural gaps closed: ap-container collision, 4th badge state,
  POST-fail recovery, keyboard nav, hidden-mount layout bug.
- Cross-voice agreement on all critical findings (consensus 7/7 dimensions).

**PHASE 2 COMPLETE.** Codex: unavailable. Subagent: 9 issues. Primary: 1
additional (#ap-container collision). Consensus: 7/7 confirmed. 3 taste
decisions surfaced at gate. Passing to Phase 3.

---

# /autoplan — Phase 3 (Eng review) findings

Voices: primary review (this agent, plan-eng-review methodology) + Claude eng
subagent (independent cold read of plan + actual code). **Codex unavailable** —
tag `[subagent-only]`.

## Architecture — ASCII dependency graph

```
EXISTING                                                NEW IN THIS PR
────────                                                ──────────────

routes/admin.py                                         routes/subtitle_jobs.py
  GET /api/songs/<id>/subtitles ─── moved ───────────►    GET /api/songs/<id>/subtitles
                                                          POST /api/songs/subtitles/bulk
                                                          │
karaoke.py (Karaoke class)                                │
  ├─ _SUBTITLE_SOURCE_LABELS (class attr) ─── MOVED ──┐  │
  └─ register_blueprint(admin_bp)                     │  │
     register_blueprint(subtitle_jobs_bp) ◄── ADDED ──┼──┘
                                                      │
karaoke_database.py                                   │
  ├─ SUBTITLE_SOURCE_* constants                      ▼
  ├─ _SUBTITLE_SOURCE_LABELS ◄── lifted to module-level (NEW location)
  ├─ get_subtitle_jobs(song_id)
  └─ get_subtitle_jobs_bulk(song_ids: list[int]) ◄── NEW (single IN(...) query)

lib/subtitle_orchestrator.py                              static/js/subtitle-source-picker.js
  ├─ DEFAULT_AUTO_SOURCES                                 │  exports:
  └─ _emit_subtitle_job_update():307                      │    mountCornerBadge(getSongId, opts)
       └─ embed `label` ◄── ADDED ───────────────────────►│    mountSmartPicker(getSongId, opts)
            (uses karaoke_database._SUBTITLE_SOURCE_LABELS)│    (factories returning
                                                          │     {el, update(data), destroy()})
templates/splash.html                                     │  pure helpers (named exports for vitest):
  ├─ #ap-container (clock+hostap)                         │    computeReadySummary
  └─ #lyrics-source ───── replaced ─────────────────────► │    deriveOptionState
       └─ corner badge mount inside #ap-container ◄─── ▼  │    computeCountdown
                                                       │  │    sortSourcesCanonically
templates/base.html                                    │  │    deriveCornerBadgeState ◄── ADDED (F13)
  ├─ window.socket = io() (line 103)                   │  │
  └─ pk-tool-subtitle-src slot ──────── consumes ──────┴──┤  static/css/subtitle-source-picker.css
                                                          │
static/js/now-playing-bar.js                              │
  ├─ updateSubtitleSourceSelect ─── DROPPED              │
  └─ mounts mountSmartPicker on song change ◄── REWIRED ──┘
       and calls .destroy() before re-mount

static/js/splash.js
  ├─ updateLyricsSourceBadge ─── DROPPED
  ├─ LYRICS_SOURCE_LABELS ─── DROPPED
  └─ mounts mountCornerBadge on song change ◄── REWIRED

DELETED: routes/admin.py:115 song_subtitles route (moved)
```

**Coupling assessment:**
- One module exporting two factory functions (badge + picker) is defensible but borderline. They share ~20% of pure-function logic (`sortSourcesCanonically`, `deriveOptionState`) and ~0% of DOM/lifecycle code. Recommended IN SCOPE; keep as a single module for Phase 2 (small enough). If Phase 2.5's queue rosette adds a third factory, split then.
- The `_SUBTITLE_SOURCE_LABELS` lift to `karaoke_database.py` resolves the otherwise-circular orchestrator → karaoke.py path that subagent F1 flagged.

## ENG Dual Voices — Consensus Table

| Dimension | Primary | Subagent | Consensus |
|---|---|---|---|
| 1. Architecture sound? | OK once F1 fixed | OK once F1 fixed | **CONFIRMED — fix F1** |
| 2. Test coverage sufficient? | Pure-fn list incomplete (no badge fn) | Same (F13) + bulk admin gate + XSS test | **CONFIRMED gaps** |
| 3. Performance risks addressed? | Partial — bulk endpoint N+1 SQL | Same (F6) + Pi 3 socket-storm risk | **CONFIRMED — fix F6** |
| 4. Security threats covered? | Plan flags XSS but not surfaces | Same (F9) + IDOR test missing | **CONFIRMED — fix F9 surfaces** |
| 5. Error paths handled? | Disconnect gap, popover-during-destroy | Same (F7, F8) | **CONFIRMED — fix F7** |
| 6. Deployment risk manageable? | Low — URL stable | Low | **CONFIRMED — clean** |

Cross-voice agreement on all 6 dimensions. Three HIGH severity findings: F1
(circular import), F4 (badge collision — already fixed in Phase 2 design), F9
(XSS surface). All have concrete fixes; none block the locked direction.

## Findings (consolidated, ranked by severity)

### HIGH

**F1 — Circular import risk: orchestrator → karaoke._SUBTITLE_SOURCE_LABELS**
Verified: `karaoke.py:509` already does deferred (in-method) import of
`SubtitleOrchestrator` to dodge the cycle. `subtitle_orchestrator.py:28`
already imports from `karaoke_database`. Fix: lift `_SUBTITLE_SOURCE_LABELS`
from `karaoke.py` (class attribute, line 1477) to `karaoke_database.py`
module-level. Both orchestrator and the new route can import it.
**Auto-fix (P5).**

**F2 — Two incompatible source DTOs (`status` vs `state`)**
- `now_playing_update.subtitle_sources[*]` shape (existing, computed by
  `_get_subtitle_sources_for_now_playing` at karaoke.py:1489): `{source,
  label, status: ready|downloading|na, downloadable}`
- `/api/songs/<id>/subtitles` `sources[*]` shape (new): `{id, state:
  queued|running|success|failed|rate_limited, tier, error_code,
  error_message, ...}`
- Picker has to consume both. Subagent's recommended fix: translate
  `state → status` at the route level so the frontend has one vocabulary.
**Auto-fix (P5):** route layer translates DB `state` to UI `status` and
embeds `label`. Frontend never sees raw DB enum. Apply in both
`subtitle_jobs.py` (GET single + POST bulk) and `subtitle_orchestrator.py`
emit (line 307). **One canonical UI DTO.**

**F4 — Corner badge collides with `#ap-container` (top-left)**
Already covered by Phase 2 P1.1 (consensus 2/2). Subagent's fix —
mount badge INSIDE `#ap-container` as third child after `#clock` /
`#hostap` — is more elegant than my "shift padding-top" fix. **Override
Phase 2 D#18:** mount badge inside `#ap-container`, inheriting flex
column layout. Updates Phase 2 design fix accordingly.
**Auto-fix (P5).**

**F9 — XSS via `error_message` and any other DB-sourced text**
Verified: `admin.py:150` returns `error_message` straight from DB row;
some sources (Genius/Tekstowo) put third-party error bodies in there.
**Auto-fix (P5):** every dynamic-text render in `subtitle-source-picker.js`
must use `el.textContent = ...` or `el.setAttribute('title', ...)`.
Bonus test: vitest case injecting `<script>alert(1)</script>` into
`error_message` and asserting the text is preserved literally. Add
`renderText` helper to module that wraps `textContent` to make XSS
violations visible in code review.

### MEDIUM

**F5 — Offset slider (napisy) lost in slot reorder spec**
Plan says reorder to `Wokal → Podkład → Źródło`, omits the offset slider
between Muzyka and Subtitle-src in `base.html:521-532`. **Auto-fix (P5):**
explicit final order: `Wokal → Podkład → Napisy (offset) → Źródło`.
Source-picker stays last (least-frequent operation), offset stays adjacent
to source semantically.

**F6 — Bulk endpoint N+1 SQL queries**
`get_subtitle_jobs(song_id)` issues one query each. Loop over 100 song_ids
on Pi 3 SD-card SQLite is measurable. **Auto-fix (P3+P5):** add
`get_subtitle_jobs_bulk(song_ids: list[int]) -> dict[int, list[sqlite3.Row]]`
to `karaoke_database.py` using parameterized `WHERE song_id IN (...)`.
Single round-trip. Add test for empty list, single-id, max-100.

**F7 — `destroy()` race when popover open mid-song-change**
Phase 2 D#24 already says "close popover on song change". Subagent
F7 hardens it: `destroy()` must call `close()` synchronously before
removing socket listeners and DOM element, otherwise stale `focusout`/
`outside-click` handlers fire on a removed element. **Auto-fix (P5):**
`destroy()` invokes internal `close()` first, then removes listeners,
then removes element.

**F12 — Pre-Phase-1 songs return empty `sources` (no synthesis)**
CEO findings #4 said the picker must render `DEFAULT_AUTO_SOURCES` as
`na/skipped` placeholders. But the plan didn't say WHERE: client-side
or server-side? Subagent's recommendation: synthesize at the route
level so the frontend stays dumb. **Auto-fix (P5):** in
`subtitle_jobs.py`, when DB returns no rows for a song, synthesize
placeholder records from `DEFAULT_AUTO_SOURCES` with `state="na"`
+ `tier=null` + nulls for all other fields. Same for the bulk endpoint.

**F13 — Corner badge has no pure-function test export**
Plan listed 4 pure helpers; all are dropdown-related. Badge mapping
`(active_source, sources) → {glyph, cssClass, text, status}` is also
pure and trivially testable — but absent. **Auto-fix (P1):** add
`deriveCornerBadgeState` as a 5th named export. Vitest cases:
ready / downloading / error / pending / no-active-source.

### LOW

**F8 — Socket reconnect gap**
Picker mounts → GET API → socket disconnects → no `subtitle_job_update`
during gap. **Mitigation already exists:** `base.html:104` socket
connect/reconnect handler should refire `now_playing` which re-renders.
**Action:** verify in implementation. Add an integration note in test
plan: manually disconnect Wi-Fi for 10s, reconnect, confirm picker
state is fresh. **Auto-fix (P5):** picker exposes `refresh()` method
that re-runs initial GET; bind to `socket.on('connect')` after first
mount. Documented; verify manual e2e.

**F10 — Bulk endpoint admin-gate test missing**
Plan listed "admin gate" for the GET test but not the POST bulk test.
**Auto-fix:** add to test plan.

**F11 — MAX_BULK=100 response size on Pi 3**
100 songs × ~9 sources × ~200 bytes = ~180KB JSON. Fine on Pi 3 RAM
but `sqlite3.Row` allocation overhead is non-trivial. F6 fix collapses
to one query so this is a non-issue post-fix. No action.

### TASTE decisions (surface at gate)

**T1 — Picker DTO source: extend `now_playing` payload vs. dedicated GET**
*Primary observation, not in subagent output.* The picker on now-playing-bar
already receives `data.subtitle_sources` via the existing `now_playing_update`
socket event. Adding a new GET (`/api/songs/<id>/subtitles`) for the picker
is a second source of truth for the same data, just with `state/tier/
error_*` added.
- Option A: keep new GET endpoint (current plan). Pro: bulk version is
  reused for Phase 2.5 queue. Con: 2 sources of truth for picker data;
  one extra HTTP round-trip per song change.
- Option B: extend `_get_subtitle_sources_for_now_playing` to include
  `state/tier/error_code/error_message/next_retry_at`. Picker consumes
  ONLY `now_playing.subtitle_sources`. New endpoint becomes BULK-ONLY
  (Phase 2.5 queue). Pro: 1 source of truth; fewer round-trips. Con:
  `now_playing` payload grows by ~2KB per song.
- Recommendation: **B** (P5 explicit, P4 DRY). Surface for user.

## Test diagram — codepath → test coverage

```
NEW UX FLOWS
────────────
[A] Pilot opens panel → picker mounts → first render
    └─ test: deriveOptionState({sources:[]}) → all 'na' rows (vitest, pure)
    └─ test: deriveOptionState({sources:[partial]}) → mix of na/ready/running (vitest, pure)
    └─ test: deriveCornerBadgeState({active_source:'lrclib-sync',...}) → green dot, label (vitest, pure)
    └─ manual: cold-load on Pi 4 phone, verify <500ms paint to first render
[B] Pilot taps row in popover → POST /subtitle_source → confirmation
    └─ test: existing route /subtitle_source unchanged (covered)
    └─ test: optimistic spinner glyph appears on tapped row (manual e2e — DOM)
    └─ test: 4xx response → restore row + showNotification (manual e2e)
    └─ test: 2xx response → close popover, wait for socket (manual e2e)
[C] Background enrichment fires `subtitle_job_update` → picker re-renders
    └─ test: handleSubtitleJobUpdate({song_id:current, ...}) → re-render (vitest, pure)
    └─ test: handleSubtitleJobUpdate({song_id:other, ...}) → no re-render (vitest, pure)
    └─ test: pkSig dedupe — same payload twice → 1 render (vitest, pure)
[D] Song change mid-popover-open
    └─ test: destroy() invokes close() before removing element (manual e2e)
[E] Splash badge state transitions (ready/downloading/error/pending)
    └─ test: deriveCornerBadgeState for all 5 cases (vitest, pure)
    └─ manual: visual on TV — bright video, dark video, busy video
[F] POST /api/songs/subtitles/bulk
    └─ unit: admin gate (403 for non-admin)
    └─ unit: empty body / missing song_ids → 400
    └─ unit: > MAX_BULK → 400
    └─ unit: partial-found (some ids exist, some don't) → returns mix + nulls
    └─ unit: pre-Phase-1 song → synthesized DEFAULT_AUTO_SOURCES placeholders
    └─ unit: label embedded in response
    └─ unit: dedup behavior on duplicate ids → documented (returns dict, dedupes)
[G] GET /api/songs/<id>/subtitles (moved + extended)
    └─ unit: same body as before (regression)
    └─ unit: label embedded
    └─ unit: state translated to status (DB-enum-not-leaked test)
    └─ unit: pre-Phase-1 song → synthesized placeholders
[H] subtitle_orchestrator emits subtitle_job_update
    └─ unit: payload contains label (existing test asserts updated)
    └─ unit: payload contains status (translated from state)

DATA FLOWS
──────────
DB ──► route ──► HTTP ──► picker.mount() (initial)
DB ──► orchestrator ──► socket ──► picker.update() (live)
                                   ─► splash.badge.update() (live)

SECURITY SURFACES
─────────────────
- error_message rendering → textContent test (XSS vector test)
- admin gate on POST bulk → 403 test
- song_id IDOR → admin gate covers (admin-only)
- popover focus/keyboard → manual e2e (a11y)
```

**Test plan artifact:** written to
`~/.gstack/projects/zygzagZ-pikaraoke/master-test-plan-20260504-163101.md`
(see separate artifact).

## Failure Modes Registry

| # | Failure | Severity | Mitigation | Test |
|---|---------|----------|------------|------|
| 1 | Picker JS fails to load | HIGH | Keep `<select>` hidden as DOM fallback; show on JS-mount-error | Manual: throw in module, refresh, verify select appears |
| 2 | Socket disconnects mid-session | MEDIUM | `socket.on('connect')` triggers picker.refresh() | Manual: airplane-mode toggle |
| 3 | Pre-Phase-1 song queried | MEDIUM | Route synthesizes placeholders (F12) | Unit test in subtitle_jobs_routes |
| 4 | Race: song change while popover open | MEDIUM | destroy() calls close() first (F7) | Manual e2e |
| 5 | XSS via error_message | HIGH | textContent only (F9) | Vitest XSS injection test |
| 6 | Bulk endpoint DoS via huge song_ids | LOW | MAX_BULK=100 → 400 | Unit test |
| 7 | Bulk endpoint N+1 SQL on Pi 3 | MEDIUM | get_subtitle_jobs_bulk() (F6) | Unit + perf manual |
| 8 | Auto-flip popover doesn't fit either way | LOW | max-height: max(160px, viewport_top - 16px) | Manual on small phone |
| 9 | Hidden-attribute mount returns 0 from getBoundingClientRect | HIGH | Defer measurement until visible | Vitest: deferred measure helper |
| 10 | Two DTO shapes confuse picker | HIGH | Single canonical UI DTO; route translates state→status (F2) | Unit: GET response shape |
| 11 | Subtitle source picker's slot becomes hidden mid-popover-open | LOW | Treat hidden as "close popover" | Manual e2e |

## Phase 3 Decision Audit Trail

| # | Phase | Decision | Principle | Rationale |
|---|-------|----------|-----------|-----------|
| 34 | Eng | **Move `_SUBTITLE_SOURCE_LABELS` from `karaoke.py` (class) to `karaoke_database.py` (module-level)** | P5, P4 (DRY) | Resolves circular import; database file already canonical for source constants |
| 35 | Eng | **Translate DB `state` → UI `status` at route layer** (single canonical UI DTO) | P5, P4 (DRY) | Frontend never handles raw DB enum |
| 36 | Eng | **Mount badge INSIDE `#ap-container` as third child** (overrides Phase 2 D#18) | P5 (explicit) + subagent F4 | Inherits flex column; no z-index fights |
| 37 | Eng | **Add `get_subtitle_jobs_bulk(song_ids)` to `karaoke_database.py`** (single IN(...) query) | P3 (pragmatic) | Pi 3 latency; collapses N+1 to 1 |
| 38 | Eng | **Add `deriveCornerBadgeState` as 5th vitest export** | P1 (completeness) | Symmetric with picker's pure helpers |
| 39 | Eng | **`destroy()` calls internal `close()` before removing element** | P5 (explicit) | Prevents stale handler refs |
| 40 | Eng | **Slot order: Wokal → Podkład → Napisy (offset) → Źródło** | P5 (explicit) | Plan didn't account for offset slider |
| 41 | Eng | **Synthesize `DEFAULT_AUTO_SOURCES` placeholders at route layer** for pre-Phase-1 songs | P5, P4 | Frontend stays dumb |
| 42 | Eng | **`textContent`-only render contract** + XSS vitest test | P5, security | F9 closes the worst-case render-side surface |
| 43 | Eng | **`refresh()` method on picker, bound to `socket.on('connect')`** | P5 (explicit error path) | Closes reconnect gap |
| 44 | Eng | **Mark T1 (DTO source: now_playing extend vs new GET) as TASTE** | n/a | Real architectural taste decision; surface at gate |

## NOT in scope (Phase 3 / Phase 2.5 / future)

- Bulk endpoint consumer wiring (queue rosettes) — Phase 2.5
- Edit view live chip row — Phase 2.5
- `queue_manager.py` `song_id` change to feed bulk endpoint — Phase 2.5
- Auto-flip on popover during keyboard-open mobile — verify manual; defer fix
- Animated open/close transition for popover — Phase 3
- DESIGN.md authoring — out of scope
- Removing dead `_get_subtitle_sources_for_now_playing` if T1=B chosen — Phase 2.5

## What already exists (re-confirmed)

- `splash.html:95-100` `#ap-container` (top-left container — badge target)
- `base.html:103` global `window.socket = io()` — picker uses this, not new io()
- `base.html:112` `showNotification(msg, category, timeout)` — picker uses this for POST-fail
- `base.html:528` `pk-tool-subtitle-src` slot — picker mounts here
- `now-playing-bar.js:489` `pkSig` re-render guard pattern
- `karaoke_database.py:251-258` `SUBTITLE_SOURCE_*` constants (single source of truth)
- `karaoke_database.py:960` `get_subtitle_jobs(song_id)` (existing single-id query)
- `subtitle_orchestrator.py:45` `DEFAULT_AUTO_SOURCES` (canonical source order)
- `subtitle_orchestrator.py:307` `subtitle_job_update` event emit point

## Phase 3 completion summary

- Status: **REVIEW COMPLETE.** 13 findings (3 HIGH, 5 MEDIUM, 4 LOW + 1 TASTE).
- 11 auto-fixes folded into spec; 1 taste decision surfaced for user.
- Cross-voice agreement: 6/6 dimensions confirmed.
- Critical gaps closed: circular import, DTO mismatch, badge collision, XSS,
  N+1 SQL, lifecycle race, pre-Phase-1 synthesis.
- Ship-readiness: GREEN once auto-fixes are applied during implementation.

**PHASE 3 COMPLETE.** Codex: unavailable. Subagent: 13 findings. Primary: 1
additional taste (T1: now_playing-extend vs new GET). Consensus: 6/6 confirmed.
1 taste decision surfaced at gate.

## Cross-phase themes

**Theme: data-shape unification.** Surfaced in CEO #5 (bulk contract dedup),
Phase 3 F2 (state vs status), Phase 3 T1 (now_playing extend vs new GET).
All three say: there are too many shapes for "subtitle sources" data; pick
one canonical UI DTO and stick to it. Recommended fix already folded in
(D#34, D#35, T1 surfaces as user taste).

**Theme: render-time safety.** CEO #10 (innerHTML XSS), Phase 2 P3.3 (POST-fail
silent), Phase 3 F9 (error_message XSS). All three converge on: every dynamic
text render goes through `textContent` and every error path has explicit user
feedback. Folded in (D#25 + D#42).

**Theme: lifecycle correctness.** CEO #7 (destroy() lifecycle), Phase 2 P3.2
(stale popover), Phase 3 F7 (popover-open destroy race). All three converge on
the same teardown sequence: close popover synchronously, remove listeners,
remove element. Folded in (D#24 + D#39).

---

# /autoplan — Phase 4 (Final approval gate) — APPROVED 2026-05-04 16:43

User approved as-is. All 4 taste decisions accepted at recommended option:

| ID | Decision | Resolution | Impact on spec |
|---|---|---|---|
| T1 | Picker DTO source | **B — extend `now_playing.subtitle_sources`** with `state/tier/error_code/error_message/next_retry_at`. New `/api/songs/<id>/subtitles` GET becomes BULK-ONLY (deferred for Phase 2.5 queue consumer). Picker on now-playing-bar reads ONLY from `now_playing` event. | Drops the single-song GET endpoint from Phase 2; only POST bulk ships. `karaoke.py:_get_subtitle_sources_for_now_playing` extends. One fewer file in scope but a payload growth. |
| T2 | Error-state alarm | **B — subtle 2s background pulse** `rgba(danger,0.15) ↔ rgba(danger,0.35)`. | Add `@keyframes pk-badge-error-pulse` in `subtitle-source-picker.css`. ~6 lines. |
| T3 | Trigger glyph | **B — drop `✓`, colour-code fraction** (green ≥4, amber 1-3, red 0). | `computeReadySummary` returns `{ready, total, label, severity}`. CSS classes per severity. |
| T4 | Alarm → action linkage | **A — IN SCOPE.** 3s pulse outline on `pk-tool-subtitle-src` when badge enters error/downloading. | Splash badge triggers a `subtitle_alarm` socket event when state transitions to error/downloading. Now-playing-bar listens → adds `is-alarmed` class for 3s. ~12 lines. |

## Final scope (post-approval)

Phase 2 ships **5 files** (down from 6 — T1 dropped the single-song GET):

NEW:
- `pikaraoke/static/js/subtitle-source-picker.js` — exports `mountCornerBadge`,
  `mountSmartPicker` factories + 5 pure helpers (`computeReadySummary`,
  `deriveOptionState`, `computeCountdown`, `sortSourcesCanonically`,
  `deriveCornerBadgeState`).
- `pikaraoke/static/css/subtitle-source-picker.css` — popover + badge styles
  with 4 badge states (ready/downloading/error/pending) + error pulse + alarm
  pulse keyframes.
- `pikaraoke/routes/subtitle_jobs.py` — new blueprint, BULK-ONLY:
  - `POST /api/songs/subtitles/bulk` (consumed by Phase 2.5 queue rosettes)

MODIFIED:
- `pikaraoke/templates/splash.html` — remove `#lyrics-source` from
  `#top-container`; add badge mount as third child of `#ap-container`
  (top-left, after `#clock` and `#hostap`).
- `pikaraoke/templates/base.html` — keep `pk-tool-subtitle-src` slot, swap
  `<select>` for picker mount. Slot order: Wokal → Podkład → Napisy
  (offset) → Źródło. Keep `<select>` hidden as fallback.
- `pikaraoke/static/js/now-playing-bar.js` — drop `updateSubtitleSourceSelect`,
  mount `mountSmartPicker` on song change with explicit `destroy()` lifecycle.
- `pikaraoke/static/js/splash.js` — drop `updateLyricsSourceBadge` +
  `LYRICS_SOURCE_LABELS`; mount `mountCornerBadge` on song change.

AMENDMENTS (one-line each):
- `pikaraoke/lib/subtitle_orchestrator.py:307` — embed `label` + translated
  `status` in `subtitle_job_update` payload.
- `pikaraoke/karaoke.py` — register `subtitle_jobs_bp`. `_SUBTITLE_SOURCE_LABELS`
  removed (lifted to `karaoke_database.py`). Extend
  `_get_subtitle_sources_for_now_playing` to include `state/tier/error_*` (T1=B).
- `pikaraoke/lib/karaoke_database.py` — add module-level `_SUBTITLE_SOURCE_LABELS`
  dict + `get_subtitle_jobs_bulk(song_ids)` method using `IN(...)`.

NEW TESTS:
- `tests/unit/test_subtitle_jobs_routes.py` — bulk endpoint suite (admin gate,
  MAX_BULK, malformed body, partial-found, dedupe, label embedded, pre-Phase-1
  synthesis, single-query assertion).
- `tests/js/subtitle-source-picker.test.js` — pure-function suite covering all
  5 named helpers + XSS regression test.
- AMEND `tests/unit/test_subtitle_orchestrator.py` — assert `label` + `status`
  in emitted payload.
- AMEND `tests/unit/test_karaoke_database.py` — `get_subtitle_jobs_bulk` cases.

## Implementation order (final)

1. Lift `_SUBTITLE_SOURCE_LABELS` to `karaoke_database.py`. Update both
   `karaoke.py` (remove class attr, import module-level) and
   `subtitle_orchestrator.py:307` to embed `label` + translated `status`.
2. Add `karaoke_database.get_subtitle_jobs_bulk(song_ids)` with `IN(...)`.
   Tests in `tests/unit/test_karaoke_database.py`.
3. Create `pikaraoke/routes/subtitle_jobs.py` with POST bulk endpoint.
   Synthesize `DEFAULT_AUTO_SOURCES` placeholders for pre-Phase-1 songs.
   Translate state→status. Tests in `tests/unit/test_subtitle_jobs_routes.py`.
   Register blueprint in `karaoke.py`.
4. Extend `_get_subtitle_sources_for_now_playing` to include
   `state/tier/error_code/error_message/next_retry_at`.
5. Create `subtitle-source-picker.js` + `.css` with 5 pure helpers, both
   factory functions, all 4 badge states (ready/downloading/error/pending),
   `destroy()` lifecycle calling internal `close()`. Vitest tests.
6. Wire splash: mount badge inside `#ap-container`, drop old badge logic.
7. Wire now-playing-bar: replace `<select>` (kept hidden as fallback), mount
   smart picker, slot order Wokal/Podkład/Napisy/Źródło. Alarm pulse on slot
   when badge transitions to error/downloading.
8. Pre-commit + `uv run pytest tests/` + `npm run test:js`.
9. Manual e2e per test plan artifact at
   `~/.gstack/projects/zygzagZ-pikaraoke/master-test-plan-20260504-163101.md`.

## Phase 4 audit trail rows

| # | Phase | Decision | Principle | Rationale |
|---|-------|----------|-----------|-----------|
| 45 | Gate | T1 = B (extend `now_playing`, drop single-song GET) | User-resolved | DRY + fewer round-trips |
| 46 | Gate | T2 = B (subtle 2s error pulse) | User-resolved | Better alarm-ness without klaxon |
| 47 | Gate | T3 = B (drop `✓`, colour-code fraction) | User-resolved | Less glyph noise |
| 48 | Gate | T4 = A (alarm→action slot pulse) | User-resolved | Closes ambient→action gap |

**STATUS: APPROVED.** Implementation can start. Commits per logical unit.
**DO NOT merge PR autonomously** (per `~/.claude/CLAUDE.md` rule §8 — incidents
on PR #228 and #242).

Next steps:
- `/ship` when ready to create the PR.
- Implement step-by-step per "Implementation order" above; commit after
  each numbered step.


