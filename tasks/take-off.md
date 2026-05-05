# Take-off — PiKaraoke Phase 2 subtitle UI

## Where you're picking up

Repo: `/Users/zygzagz/github/pikaraoke` (branch `master`, no PR yet, latest
commit `2a2b78ca` shipped Phase 1). Working tree has uncommitted plan files
in `tasks/` — those are mine, leave them alone except for the ones you'll
touch (see "Plan source of truth" below).

A previous session ran `/autoplan` Phase 1 (CEO review with two voices —
Plan agent + independent Claude subagent; Codex unavailable, tagged
`[subagent-only]`). Findings drove a major scope revision. The user
reviewed real HTML mockups (locations below) and **locked the UI direction**.
Implementation has not started.

## Plan source of truth

Read these in order, then stop reading:
1. `/Users/zygzagz/github/pikaraoke/tasks/todo.md` — current operational
   plan with locked UI direction, 6-file scope, decision audit trail,
   resolved taste decisions. THIS is canonical.
2. `/Users/zygzagz/github/pikaraoke/tasks/subtitle-status-chips-phase-2.md`
   — original Phase 2 design doc (superseded by todo.md but useful as
   background).
3. `/Users/zygzagz/github/pikaraoke/tasks/done/subtitle-orchestrator-phase-1.md`
   — what shipped in Phase 1 (so you know what infrastructure exists).
4. `/Users/zygzagz/.gstack/projects/zygzagZ-pikaraoke/master-ceo-review-20260504-143641.md`
   — full CEO review artifact (only if you need to dig into a specific
   finding).
5. `/Users/zygzagz/github/pikaraoke/CLAUDE.md` + `~/.claude/CLAUDE.md` —
   project + global conventions.

Mockup the user signed off on (open in a browser if you want to see what
was approved):
- `~/.gstack/projects/zygzagZ-pikaraoke/master-phase2-mockup-20260504.html`
  (durable copy)
- `/tmp/pikaraoke-mockups/index.html` (working copy from the previous
  session — may be gone after reboot)

Do NOT read the other plan docs in `tasks/` — they are unrelated phases.

## What's locked (do NOT re-litigate)

- **Splash:** single corner badge at **top-left**. Shows only the active
  source + status. 3 states: ready (green dot, steady), downloading
  (amber spinner, brief), error (red, "brak napisów"). Replaces existing
  `#lyrics-source` element in `splash.html:65-68`.
- **Phone now-playing-bar full panel:** custom popover dropdown in the
  existing `pk-tool-subtitle-src` slot. Slot moves to be **below**
  Wokal + Podkład sliders. Trigger shows `[● LRCLib + sync] [5/7 ✓] [▼]`.
  Popover **auto-flips upward** when no room below. `max-height: 320px`
  + scroll. Disabled rows for failed/running/rate_limited (visible but
  unpickable).
- **Queue rosette + edit-view chip row:** DEFERRED to Phase 2.5 — gated
  on resolving the structural gap that queue items lack `song_id`.
- **Approach:** C-like split. Phase 2 ships splash + remote + bulk
  endpoint (built for 2.5's use). Phase 2.5 ships queue + edit + the
  `queue_manager.py` `song_id` change.
- **Component naming:** `subtitle-source-picker.js` (not
  `subtitle-chips.js` — chips aren't the primary primitive anymore).

## Critical scope gaps to resolve during implementation

These came out of the CEO review. They are non-negotiable — the
implementation must address each:

1. Pre-Phase-1 songs have empty `subtitle_jobs`. Picker/badge MUST
   render `DEFAULT_AUTO_SOURCES` as `na/skipped` placeholders, not blank.
2. Embed `label` field in `subtitle_job_update` socket payload
   (`subtitle_orchestrator.py:307`) so the picker doesn't need a
   separate label-registry fetch. One-line change.
3. Embed `label` per source in the `GET /api/songs/<id>/subtitles`
   response.
4. Edit view (when Phase 2.5 lands) MUST use `window.socket`, NOT
   create a second `io()` connection. `base.html:103` already creates
   the global socket.
5. JS-load fallback: if `subtitle-source-picker.js` fails to mount,
   fall back to the native `<select>` (keep it hidden in DOM as a
   safety net). Operator must never lose the source picker.
6. `MAX_BULK = 100` enforced on `POST /api/songs/subtitles/bulk`.
   Return 400 if exceeded.
7. Component lifecycle: explicit `destroy()` MUST be called before
   re-mounting on song change in now-playing-bar (otherwise socket
   listeners leak).
8. JS tests must follow the project's vitest constraint (CLAUDE.md):
   pure functions only, no DOM mocking. Extract
   `computeReadySummary`, `deriveOptionState`, `computeCountdown`,
   `sortSourcesCanonically` as named exports and test those. The DOM
   wiring is manual e2e.
9. Bulk endpoint contract: even though Phase 2 doesn't consume it for
   the queue (2.5 does), document whether the response is a dict
   `{<song_id>: {...}}` or a list `[{song_id, ...}]`. The dict form
   silently dedupes if the same song is queued twice — not a bug for
   the rest endpoint, but the queue consumer in 2.5 must be aware.
10. Render `error_message` via `textContent` / `setAttribute('title')`,
    NEVER `innerHTML` (XSS).

## Files in scope (Phase 2 = 6 files)

NEW:
- `pikaraoke/static/js/subtitle-source-picker.js` — module exporting
  `mountCornerBadge(songIdGetter, options)` + `mountSmartPicker(...)`
  factory functions returning `{ el, update(data), destroy() }` lifecycle
  objects, plus pure helpers as named exports.
- `pikaraoke/static/css/subtitle-source-picker.css` — styles using
  `var(--pk-*)` tokens. Both Mazury + Late Show themes.
- `pikaraoke/routes/subtitle_jobs.py` — new blueprint:
  - `GET /api/songs/<song_id>/subtitles` (moved from `admin.py:115`)
  - `POST /api/songs/subtitles/bulk` (built now, consumed in 2.5)
- `tests/unit/test_subtitle_jobs_routes.py` — bulk endpoint tests
  (admin gate, MAX_BULK, missing ids, partial-found, label embedded,
  empty list returns empty dict, no-rows-yet song returns empty
  sources).
- `tests/js/subtitle-source-picker.test.js` — pure-function vitest
  tests only.

MODIFIED:
- `pikaraoke/templates/splash.html` — replace `#lyrics-source` with
  the corner badge mount point.
- `pikaraoke/templates/base.html` — keep `pk-tool-subtitle-src` slot,
  REORDER it to be after Wokal/Podkład, swap `<select>` for picker
  mount.
- `pikaraoke/static/js/now-playing-bar.js` — drop
  `updateSubtitleSourceSelect` and dropdown change handler; mount
  smart picker; click → `POST /subtitle_source` (existing endpoint
  unchanged).
- `pikaraoke/static/js/splash.js` — drop `updateLyricsSourceBadge` +
  `LYRICS_SOURCE_LABELS`; mount corner badge with current `song_id`;
  re-mount on song change.
- `pikaraoke/lib/subtitle_orchestrator.py` — embed `label` in
  `subtitle_job_update` event payload (one-line change at line 307).
- `pikaraoke/karaoke.py` — register `subtitle_jobs_bp`. Move
  `_SUBTITLE_SOURCE_LABELS` to module-level (or to
  `karaoke_database.py` — see todo.md taste decision; user hasn't
  ruled).
- `pikaraoke/routes/admin.py` — remove `song_subtitles` route (moved).

## What to do next

The user invoked `/autoplan` and Phase 1 is done. Phase 2 (Design
review) and Phase 3 (Eng review) of /autoplan are still pending.

Given the UI is now concretely locked from a real mockup the user
signed off on, the design review's scope is much smaller — just
verify corner badge legibility on arbitrary video backgrounds,
confirm 44px tap targets in the popover, sanity-check the auto-flip
logic, and check theme contrast. The eng review still has full value
(architecture, lifecycle, MAX_BULK, socket filtering, test plan).

**Recommended next move:** ask the user whether to continue
`/autoplan` Phase 2 (Design) + Phase 3 (Eng) or skip straight to
implementation. The user's CLAUDE.md prefers Plan Mode for non-trivial
tasks — this qualifies. They invoked /autoplan voluntarily, so default
to continuing the pipeline unless they redirect.

If you continue /autoplan, the protocol is:
- Phase 2: Design review — fork a Plan agent + an independent design
  subagent (Codex unavailable). Both review on the LOCKED direction
  only — do not re-evaluate "chip row vs dropdown". Build a design
  consensus table. No premise gate.
- Phase 3: Eng review — fork a Plan agent + an independent eng
  subagent. Produce: architecture ASCII diagram, test plan artifact
  to `~/.gstack/projects/zygzagZ-pikaraoke/`, error/failure registries.
- Phase 4: Final approval gate — present taste decisions + scores;
  AskUserQuestion A/B/C/D/E.

Then on approval: implement per the file list above. Commit
autonomously per logical unit. Do NOT merge the PR autonomously
(user's CLAUDE.md is explicit about this — incidents on PR #228 and
#242).

## Tone

The user is opinionated and senior. No flattery, no recap-prose,
no apologies. Show diffs, not narration. If you disagree with a
locked decision, push back ONCE and concretely with cited evidence;
otherwise execute. The user values demand-elegance — pause and ask
"is there a more elegant way" for non-trivial changes.

Telemetry note: the previous session armed a `/loop` ScheduleWakeup
that may still fire. If it wakes you, treat it as a no-op poll —
the actual work is gated on the user's next instruction, not
elapsed time.
