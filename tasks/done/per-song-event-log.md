# Per-song download/processing event log in the edit view

## Status (2026-05-04)

**SHIPPED** — commit `f8e09b37 feat(events): per-song processing timeline on edit view`. The `song_events` ledger, mirror handler,
emit points, JSON endpoint, and edit-view rendering all landed as
designed. Out-of-scope items (live SocketIO on edit view, raw
subprocess log capture) remain out of scope.

______________________________________________________________________

## Goal

Surface a per-song timeline of download + processing milestones (and
existing warnings) on the edit song view. Operator opens
`/files/edit?song=...` and sees what happened to that file: when it
was downloaded, whether enrichment ran, what lyrics source landed,
whether Demucs separated successfully, plus any warnings/errors.

## Design

### Unified ledger

A new `song_events` buffer in `karaoke.Karaoke`, modeled on the
existing `song_warnings` infrastructure:

- DB key `song_events`, capped at 1000 entries (vs 200 for warnings —
  milestones are denser).
- Lock-protected list of dicts:
  `{timestamp, phase, severity, message, detail, song, youtube_id}`.
  - `phase`: `"download" | "enrichment" | "lyrics" | "demucs" | "encode"`
  - `severity`: `"info" | "warning" | "error"`
  - `song`: basename when known, else `""`
  - `youtube_id`: 11-char id when known, else `""`
- New event channel `song_event` handled by `_handle_song_event`:
  appends to the buffer, persists, and broadcasts via SocketIO.
- `_handle_song_warning` ALSO appends a parallel record to
  `_song_events` (severity carried over). Single source of truth for
  the edit view; the existing admin warnings dashboard reads from
  the original `_song_warnings` and is untouched.
- New getter `get_song_events_for(song_basename, youtube_id) -> list`
  matches by either field — covers the "download starting" event
  emitted before the basename is known.

### Emit points

| Phase | Where | When |
|---|---|---|
| download | download_manager `queue_download` cache-hit branch | "Cache hit (already downloaded)" |
| download | download_manager `_execute_download` start | "Download starting" |
| download | download_manager `_execute_download` success | "Download completed" |
| enrichment | song_enricher `_enrich_song_inner` start | "Metadata enrichment starting" |
| enrichment | song_enricher `_enrich_song_inner` end | "Metadata enrichment finished" + status |
| lyrics | lyrics `_register_ass` success | "Lyrics fetched" + source |
| demucs | karaoke `_forward_progress` first call | "Vocal separation started" |
| demucs | karaoke `_forward_ready` | "Vocal separation completed" |

Failure paths (download, demucs, lyrics, encode) already emit
`song_warning`. Mirror handler picks them up automatically — no new
emit calls needed for the negative path.

### Hook plumbing

`song_enricher` is a module of free functions, not a class with an
events handle. Follows the existing `demucs_processor.set_warning_hook`
pattern: module-level `_event_hook` + `set_event_hook`. karaoke.py
registers a forwarder at startup that calls `events.emit("song_event", ...)` for every hook invocation.

`demucs_processor` already has `set_progress_hook` and `set_ready_hook`
that karaoke.py wires into SocketIO. The `_forward_progress` and
`_forward_ready` bridges grow a parallel `events.emit("song_event")`.
First-progress-per-stream is detected with a per-song-basename gate
on the karaoke side so we don't spam.

### UI

`/files/edit` renders the events server-side. Below the form, a
"Historia przetwarzania" section listing entries oldest-first:

- Phase badge (color-coded: download=blue, enrichment=purple,
  lyrics=green, demucs=orange, encode=teal)
- Severity stripe (info=neutral, warning=amber, error=red)
- ISO timestamp (local time)
- Message + collapsible details for `detail`

A small "Refresh" button calls a new JSON endpoint
`/admin/song_events?song=<basename>&youtube_id=<id>` and re-renders
the list client-side. No SocketIO live wiring on this view — the edit
view is short-lived; manual refresh is sufficient.

### Edge cases

- Renaming a song changes the basename: events emitted under the old
  basename stay attached. The edit view extracts the youtube_id from
  the path so cross-keying still finds them. Acceptable trade-off
  vs. mass-rewriting the buffer on every rename.
- Songs with no youtube_id (uploaded files): only basename match.
  Buffer entries before this song's basename existed will not match.
  Acceptable.
- Buffer at cap: oldest events drop. Edit view shows a hint
  "(history truncated)" when count == cap.

## Files touched

- `pikaraoke/karaoke.py` — `_song_events` ledger, handler, getter, hook bridges
- `pikaraoke/lib/download_manager.py` — milestone emits
- `pikaraoke/lib/song_enricher.py` — event hook + emits
- `pikaraoke/lib/lyrics.py` — emit on `_register_ass` success
- `pikaraoke/routes/files.py` — pass events into edit context
- `pikaraoke/routes/admin.py` — JSON endpoint
- `pikaraoke/templates/edit.html` — render timeline + refresh button
- `tests/unit/test_karaoke_*.py` — ledger + mirror + getter tests
- `tests/unit/test_download_manager.py` — milestone emits
- `tests/unit/test_song_enricher.py` — event hook
- `tests/unit/test_files_routes.py` (new or extend existing) — edit view renders events

## Out of scope

- Live SocketIO updates on the edit view (manual refresh is enough).
- Capturing raw `logging.info`/`.debug` lines from yt-dlp/demucs
  subprocess output — only structured milestones land in the buffer.
- Per-song log retention beyond the global 1000-entry cap.
