# Reactive enrichment driven by language consensus — todo

iTunes enrichment today fires eagerly on `register_download` and takes
the top-1 hit blind. The "Don Pedalini" failure (id 96) is the canonical
case: query "Don Pedalini" → iTunes' fuzzy match was a Brazilian Santo
Daime hymn (surname "Pedalino"), and the row got artist/title/album/genre
clobbered to Portuguese while whisper had already cached `pl` with conf
0.99 against the same audio sha. The eager path has no way to consult
language signals — they arrive over time (yt-dlp info → title langdetect
→ iTunes country → MusicBrainz → whisper raw probe → whisper stems
probe), and today the enricher fires once at the very start.

## New model: enrichment reacts to language consensus updates

- `register_download` no longer enriches eagerly.
- Instead, after the info.json seed, run a **pre-iTunes classifier**
  using only the signals that don't depend on iTunes (yt-dlp info.json,
  yt-dlp subtitle keys, yt-dlp title langdetect, DB title heuristic).
  If consensus achieved → persist `songs.language` → dispatch
  `enrich_song`. If no consensus → leave `metadata_status='awaiting_language'`.
- Every later language-write site (Tier 1 classifier in `lyrics.py`,
  Tier 2a `whisper_probe_raw`, Tier 2b `whisper_probe_stems`) gets a
  re-enrich dispatch immediately after persisting the new language.
- `enrich_song` is **self-idempotent**: if `songs.metadata_status ==
  'enriched'` and `songs.language_at_enrich == songs.language`, it
  returns without touching iTunes. Otherwise it re-runs the iTunes
  search (LRU-cached, so repeated calls are free), picks the first hit
  whose derived language matches `songs.language`, and stamps
  `language_at_enrich`.

That gives Pedalini the right outcome: at register_download there are
no language signals (short title, no diacritics) → status
`awaiting_language`, no iTunes write. After whisper raw probe runs in
the lyrics pipeline → writes `songs.language='pl'` → dispatches
re-enrich → iTunes top-5 reranked, no hit is `pl` → status
`language_mismatch`, no textual fields written.

## Statuses (simplified)

- `awaiting_language` — no language signal yet, iTunes not consulted.
- `enriched` — iTunes hit applied, language matched at the time.
- `language_mismatch` — language known, no iTunes top-5 hit matched.
- `not_found` — iTunes returned zero hits at all.
- `error` — uncaught exception during enrichment (existing).

The previous `pending` and the proposed `top1_no_signal` are gone —
the new flow never enriches without a language signal.

## Files

### Schema

- [ ] `pikaraoke/lib/karaoke_database.py`
  - `ALTER TABLE songs ADD COLUMN language_at_enrich TEXT;` (idempotent
    migration block, same pattern as the existing `audio_sha256` /
    `metadata_sources` columns).
  - No new index — `language_at_enrich` is read only when enriching a
    specific row by id, never queried in bulk.

### Modified

- [ ] `pikaraoke/lib/lyrics_language_classifier.py`
  - Expose `COUNTRY_TO_LANG` (drop leading underscore) and
    `signal_itunes_text` (renamed from `_signal_itunes_text`) — both
    needed by the enricher's per-hit language derivation. Keep all
    other helpers private.
  - Add `pre_itunes_signals(*, yt_info, db_title, db_artist) ->
    list[LanguageSignal]`: thin wrapper around `collect_signals` with
    `itunes_hit=None, mb_signals=None`. Documents the
    "no-iTunes-yet" use case.
  - Existing `classify_and_persist` unchanged.
- [ ] `pikaraoke/lib/music_metadata.py`
  - Extract `_project_full_hit(raw_full_hit) -> dict` from
    `fetch_itunes_track`. `fetch_itunes_track` becomes a thin
    `search_itunes_full(..., limit=1) → _project_full_hit` wrapper.
    Both the enricher and existing callers use the same projection.
- [ ] `pikaraoke/lib/song_enricher.py`
  - Add `_hit_language(itunes_full_hit) -> str | None`: runs
    `signal_itunes_text` on the full hit, falls back to
    `COUNTRY_TO_LANG.get(country)`. Returns the primary subtag or None.
  - Add `_pick_hit_for_language(candidates, expected_lang) ->
    tuple[dict|None, str]`: returns `(chosen_hit, reason)` where
    reason is `lang_match` or `no_match`. With `expected_lang=None`
    this isn't called — the enricher early-returns to
    `awaiting_language`.
  - Rewrite `_enrich_song_inner`:
    1. Read `row`. If `row['metadata_status'] == 'enriched'` and
       `row['language_at_enrich'] == row['language']`, return early
       (idempotency).
    2. Pull `expected_lang = row['language']`. If empty, stamp
       `awaiting_language`, emit info event, return.
    3. `candidates = search_itunes_full(query, limit=5)`. Empty →
       `not_found` (existing path).
    4. `chosen, reason = _pick_hit_for_language(candidates, expected_lang)`.
       If `no_match` → stamp `language_mismatch`, persist
       `language_at_enrich = expected_lang` (so we don't re-attempt
       until language changes), emit warning event, return without
       writing textual fields. `itunes_id` is NOT written either —
       the match was wrong wholesale.
    5. Match path: project chosen hit → existing
       `_itunes_adds_variant` guard → existing
       `update_track_metadata_with_provenance` → existing MB
       lookup + cover download. Stamp
       `language_at_enrich = expected_lang`.
  - Drop `fetch_itunes_track` import (replaced by
    `search_itunes_full` + `_project_full_hit`).
- [ ] `pikaraoke/lib/song_manager.py`
  - In `register_download`, after info.json seeding and before
    `_start_enrichment`:
    - Read info.json into a dict (use the existing
      `read_info_json` helper from `lyrics_language_classifier`).
    - Pull `db_title`/`db_artist` from the row.
    - Call `pre_itunes_signals` + `consensus`. On consensus →
      persist via `update_track_metadata_with_provenance` under
      the winning rung.
  - `_start_enrichment` always fires (even with no consensus) — the
    enricher handles `awaiting_language` itself.
- [ ] `pikaraoke/lib/lyrics.py`
  - Around `classify_and_persist` (line ~1806): if it returned a
    verdict, dispatch `enrich_song` in a daemon thread.
  - Around the Tier-2a `whisper_probe_raw` persist (line ~1873):
    after the `update_track_metadata_with_provenance` call, dispatch
    re-enrich.
  - Around the Tier-2b `whisper_probe_stems` persists (lines ~1931,
    ~2001): same.
  - Factor the dispatch into one helper
    (`_dispatch_reenrich(song_id, song_path)`) at module scope —
    same daemon-thread pattern song_manager already uses, with
    deferred import to avoid cycles.
- [ ] `pikaraoke/templates/edit.html`
  - Add `<section class="pk-edit-metadata">` between `pk-edit-sources`
    and `pk-events`. Renders only when `metadata_status` is one of
    `awaiting_language` / `language_mismatch`.
  - `awaiting_language`: amber tone "Metadane czekają na rozpoznanie
    języka audio. Po analizie pojawi się tu match albo informacja o
    konflikcie."
  - `language_mismatch`: red tone "Metadane odrzucone — język audio
    ({{ row_language }}) nie pasuje do żadnego z 5 wyników iTunes.
    Ustaw ręcznie."
  - CSS chips reuse `--pk-amber` / `--pk-amber-2` and the existing
    `pk-event.is-warning` / `is-error` border-left pattern.
- [ ] `pikaraoke/routes/files.py`
  - In `edit_file`, fetch `row = k.db.get_song_by_id(song_id)` once;
    pass `metadata_status` and `row_language` (`row["language"]`) to
    the template.

### New

- (none — extend existing modules only)

## Tests

- [ ] `tests/unit/test_song_enricher.py`
  - `test_skips_when_no_language_signal_yet` — no whisper cache, no
    `songs.language`, mock `search_itunes_full` (must NOT be called).
    Assert `metadata_status='awaiting_language'`, no fields written.
  - `test_picks_hit_matching_language` — `songs.language='pl'`, mock
    returns `[pt_hit, pl_hit, en_hit]`. Assert `pl_hit` fields land,
    status `enriched`, `language_at_enrich='pl'`.
  - `test_rejects_when_no_top5_hit_matches` — `songs.language='pl'`,
    all 5 hits are `pt`. No textual fields written, no `itunes_id`,
    status `language_mismatch`, `language_at_enrich='pl'`.
  - `test_idempotent_when_language_unchanged` — second call with same
    `songs.language` and `metadata_status='enriched'` is a no-op
    (mock `search_itunes_full` not called).
  - `test_re_enriches_when_language_changes` — first call with
    `language='en'` writes en_hit; second call after row's language
    is updated to `pl` re-runs and writes pl_hit, updates
    `language_at_enrich`.
  - `test_pedalini_regression` — query "Don Pedalini",
    `songs.language='pl'` (set as if whisper just wrote it),
    top-5 are all Brazilian/Portuguese hymns. Assert no textual
    fields written, `metadata_status='language_mismatch'`.
  - `test_keeps_variant_guard` — `_itunes_adds_variant` still fires
    after the language filter passes.
- [ ] `tests/unit/test_song_manager.py` (or extend if exists)
  - `test_register_download_runs_pre_itunes_classifier` — info.json
    with Polish title, no other signals. Assert `songs.language`
    is set to `pl` from `yt_title_lang` consensus, enrich is
    dispatched.
  - `test_register_download_no_consensus_no_language_write` — short
    info.json title, no info.json language, no manual subs. Assert
    `songs.language` stays empty, enricher dispatched anyway and
    stamps `awaiting_language`.
- [ ] `tests/unit/test_music_metadata.py`
  - `test_fetch_itunes_track_uses_shared_projection` — extracted
    helper produces the same shape `fetch_itunes_track` returns
    today.
- [ ] `tests/unit/test_files_routes.py` (create or extend)
  - `test_edit_page_chip_for_language_mismatch` — seed row with
    `metadata_status='language_mismatch'`, assert chip in HTML.
  - `test_edit_page_chip_for_awaiting_language` — same for the
    awaiting state.
  - `test_edit_page_no_chip_for_enriched` — assert chip absent.
- [ ] `tests/unit/test_lyrics_language_classifier.py`
  - `test_pre_itunes_signals_excludes_itunes_and_mb` — assert it's
    `collect_signals` minus the iTunes/MB extractors.

## Out of scope

- Re-enrich button on the edit page. Implicit re-enrich from whisper
  hooks should cover the realistic cases. Manual button can come
  later.
- Live chip updates over the socket. The static server-render +
  page reload is fine — re-enrich from whisper completes in seconds
  and the user is rarely watching the edit page for that whole
  window.
- Backfill cleanup of the existing Pedalini row. We'll SQL-flip its
  `language_at_enrich` to NULL post-deploy so the next dispatch
  re-runs and lands the new flow naturally.
- Re-enrichment threading model upgrade. Daemon threads (the current
  pattern) are fine — bursts of language changes for a single song
  are rare, and the inner enricher is idempotent.

## Implementation order

1. Schema migration + enricher idempotency check (no behaviour
   change yet — `language_at_enrich` is just stamped).
2. Refactor `_project_full_hit` out of `music_metadata`.
3. `signal_itunes_text` / `COUNTRY_TO_LANG` exposure +
   `pre_itunes_signals` in classifier.
4. Enricher rewrite (top-5, language match, three statuses).
   Run new + existing enricher tests.
5. `register_download` → pre-iTunes classifier wiring.
6. `lyrics.py` → re-enrich dispatch at three sites
   (Tier 1, Tier 2a, Tier 2b).
7. Edit-page chip (route + template + CSS).
8. Manual smoke test on Pedalini:
   - `UPDATE songs SET language_at_enrich=NULL, metadata_status='awaiting_language' WHERE id=96;`
   - SQL-clear the bogus `artist/title/album/genre` so they don't
     poison anything before the next enrichment runs.
   - Trigger lyrics pipeline / enrichment via a Python REPL call.
   - Confirm `language_mismatch` + chip on the edit page.
9. Pre-commit clean.

## Verification checklist

- [ ] All existing `tests/unit/test_song_enricher.py` cases still
  pass (no regression in the happy path).
- [ ] New language-driven cases pass.
- [ ] Pedalini ends up at `language_mismatch` with no textual
  iTunes fields.
- [ ] A known-good Polish song (e.g. id=95 Edyta Górniak) ends up
  `enriched` with the same iTunes fields it has today.
- [ ] A known-good English song (e.g. id=86 Jessie J) ends up
  `enriched` with English iTunes fields.
- [ ] Edit page renders the right chip variant for each non-enriched
  state.
- [ ] Pre-commit clean.
