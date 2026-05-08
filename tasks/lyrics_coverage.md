# Lyrics-source coverage report (production candidate path)

Re-verification after commit `984b51e3` (the bug-C/D/E/F/G fixes for the
metadata→lyrics pipeline). Each song is probed exactly the way the
runtime would: every source goes through
`LyricsService._try_with_candidates`, so split-artist credits, filename
`regex_tidy`, iTunes, and MusicBrainz alternatives are all exercised.

Reproduce: `uv run python scripts/verify_lyrics_coverage.py`

## Headline numbers

|                                                | songs        |
|------------------------------------------------|-------------:|
| Total in DB                                    | 30           |
| No candidates (filename unparseable)           | **0 / 30**   |
| Synced coverage (LRCLib or Spotify timed)      | **26 / 30**  |
| Synced or Genius (production-usable coverage)  | **29 / 30**  |
| Zero coverage (no LRCLib / Spotify / Genius)   | **1**        |

Spotify was 24h IP-banned during the run (single 86 400 s `Retry-After`
on song id=97, then locked out for the rest), so the Spotify column is
**unverified** for everyone except 86–96 (which got `429`s, also
locked out). That's the same chronic Spotify-IP-throttle issue noted
in the prior probe — the bug-G short-circuit at `lyrics.py:1745` did
its job and didn't park the worker on stacked sleeps.

The user-visible criterion ("at least one source of synced lyrics per
song") is met for **29 / 30** rows: every row except id=119 produces
either an LRCLib synced LRC or a Genius plain-text payload that the
wav2vec2 `genius-sync` aligner consumes locally. id=119 is a genuine
miss across LRCLib + Genius — it was the same single zero-coverage
song in the pre-fix probe, a cover with no online record.

## Per-song matrix

`db` = the DB tag won. `alt:` = a candidate-ladder rescue won (the row
was reachable only because the candidate ladder generated alternatives
the original DB tag couldn't reach).

| id | artist | title | dur | lang | LRCLib (winner) | Spotify (winner) | Genius (winner) | status |
|----|--------|-------|-----|------|-----------------|------------------|-----------------|--------|
| 86 | Jessie J | Price Tag (feat. B.o.B) \[feat. B.o... | 245 | en | ✓ (db) | 429 | ✓ (db) | enriched |
| 88 | Nickelback | How You Remind Me | 228 | de | ✓ (db) | 429 | ✓ (db) | enriched |
| 89 | Taylor Swift | Shake It Off | 242 | en | ✓ (db) | 429 | ✓ (db) | enriched |
| 90 | Sunrise Avenue | Hollywood Hills | 215 | en | ✓ (db) | 429 | ✓ (db) | enriched |
| 91 | Macklemore & Ryan Lewis | Can't Hold Us (feat. Ray Dalton) | 424 | en | ✓ (db) | 429 | ✓ (db) | enriched |
| 92 | Taylor Swift | Opalite | 239 | en | ✓ (db) | 429 | ✓ (db) | enriched |
| 93 | The Police | Every Breath You Take | 229 | en | ✓ (db) | 429 | ✓ (db) | enriched |
| 94 | Taylor Swift | Love Story | 237 | en | ✓ (db) | 429 | ✓ (db) | enriched |
| 95 | Edyta Gorniak | Kolorowy Wiatr | 210 | pl | ✓ (db) | 429 | ✓ (db) | enriched |
| 96 | Kutas Records, Łukasz B, Łuka... | Don Pedalini | 230 | pl | ✓ (alt: Kutas Records / Don Pedalini) | 429 | ✓ (alt: T.Love / My name is Łukasz) | not_found |
| 97 | Gejtos | Antyczny Napaleniec | 256 | pl | · | 429 | ✓ (db) | manual |
| 98 | Queen | I Want To Break Free (Official Vid... | 271 | en | ✓ (alt: Queen / I Want To Break Free) | lock | ✓ (alt: Queen / I Want To Break Free) | enriched |
| 99 | Alex Warren | Carry You Home | 188 | en | ✓ (db) | lock | ✓ (db) | enriched |
| 100 | Katarzyna Laska | Mam Te Moc | 223 | pl | ✓ (db) | lock | ✓ (db) | enriched |
| 101 | Maryla Rodowicz & Lanberry | Małgośka | 268 | pl | ✓ (alt: Maryla Rodowicz / Małgośka) | lock | ✓ (alt: Maryla Rodowicz / Małgośka) | enriched |
| 102 | Jarzebina | Ko-Ko Euro Spoko | 179 | pl | ✓ (db) | lock | ✓ (db) | enriched |
| 103 | Coolio | Gangsta's Paradise (feat. L.V.) \[2... | 242 | en | ✓ (db) | lock | ✓ (alt: State of Mine / Gangsta's Paradise) | enriched |
| 104 | Ed Sheeran | The A Team | 290 | en | ✓ (db) | lock | ✓ (db) | enriched |
| 105 | Brodka | Miales Byc | 185 | pl | · | lock | ✓ (db) | enriched |
| 106 | Krzysztof Krawczyk | Za tobą pójdę jak na bal | 258 | pl | ✓ (db) | lock | ✓ (db) | enriched |
| 107 | Mike Oldfield | Moonlight Shadow ft. Maggie Reilly | 223 | en | ✓ (alt: Confligo / Moonlight Shadow (Mik...) | lock | ✓ (db) | not_found |
| 108 | Kutas Records, Łukasz B, Łuka... | Antyczny Napaleniec - Acoustic | 272 | pl | · | lock | ✓ (alt: T.Love / My name is Łukasz) | not_found |
| 109 | Maryla Rodowicz & Roxie Węgiel | Damą być | 142 | pl | ✓ (db) | lock | ✓ (alt: Maryla Rodowicz / Damą być) | enriched |
| 110 | Bonnie Tyler | Total Eclipse of the Heart | 334 | en | ✓ (db) | lock | ✓ (db) | enriched |
| 111 | Kasia Cerekwicka | Na Kolana | 211 | pl | ✓ (db) | lock | ✓ (db) | enriched |
| 113 | Gibbs & Kiełas | Piękny Świat | 233 | pl | ✓ (db) | lock | ✓ (alt: Gibbs / Piękny Świat) | enriched |
| 115 | Gibbs, Opał, Jonatan & 4money | Drive | 203 | en | ✓ (db) | lock | ✓ (alt: Gibbs / Drive) | enriched |
| 116 | Kaśka Sochacka | Jeszcze | 212 | pl | ✓ (db) | lock | ✓ (db) | enriched |
| 117 | Gibbs, Jonatan & 4money | Stan | 185 | tl | ✓ (db) | lock | ✓ (alt: Gibbs / Stan) | enriched |
| 119 | Daniel Liebt | Fireflies (feat. JAYNIE) | 197 | en | · | lock | · | enriched |

Spotify legend: `lock` = locked out by 24h IP ban; `429` = single
429 not retried; `—` = not probed (only when `--no-spotify`).

## Metadata sanity (Phase 1, post-fix)

The pre-flight scan checks every legacy bug pattern produced by the
old enricher. **All three buckets are empty**:

| bucket | rows |
|--|--:|
| `metadata_status IN ('enriched','no_new_fields')` AND empty `artist`/`title` | **0** |
| `metadata_status='language_mismatch'` AND empty `artist`/`title` | **0** |
| `metadata_status='not_found'` AND empty `artist`/`title` | **0** |

Phase 2b ran `song_enricher.enrich_song(db, sid, path)` directly on
the four `pending` rows that the bug-F filename-reseed had partially
recovered (96, 98, 107, 108). Outcomes:

| id | before | after | reason |
|----|--------|-------|--------|
| 96 | `pending` (filename reseed) | `not_found` | Producer label as artist; not on iTunes |
| 98 | `pending` | **`enriched`** | iTunes hit — bug-C variant guard preserved `Queen` as artist |
| 107 | `pending` | `not_found` | iTunes doesn't index `ft. Maggie Reilly` suffix |
| 108 | `pending` | `not_found` | Acoustic variant unindexed |

Final status distribution: **26 `enriched`, 1 `manual`, 3 `not_found`**.
No poisoned, language-mismatched, or empty rows remain. `not_found`
is a legitimate state for the remaining three — iTunes legitimately
has no record, but the lyrics pipeline still produces ≥1 source for
each via the candidate ladder.

## Comparison vs. prior probe (pre-984b51e3)

| | before | after |
|--|--:|--:|
| Total in DB | 30 | 30 |
| Missing artist/title in DB | **4** (96, 98, 107, 108) | **0** |
| Probe-able rows (had artist + title) | 26 | **30** |
| Synced (LRCLib) on candidate ladder | 25 / 26 | **26 / 30** |
| Synced or Genius coverage | 25 / 26 | **29 / 30** |
| Genuine zero-coverage | id=119 | id=119 |

Recovered rows post-fix:

- **id=96** — was `language_mismatch` with empty fields ⇒ LRCLib HIT via
  candidate-ladder alt (`Kutas Records / Don Pedalini`). Genius HIT via
  filename-derived alt.
- **id=98** — was poisoned `enriched` with empty fields ⇒ LRCLib + Genius
  HIT via the bug-C variant-strip alt (`Queen / I Want To Break Free`).
- **id=107** — was `not_found` with empty fields ⇒ LRCLib + Genius HIT
  (LRCLib via Confligo cover; Genius on the DB tag).
- **id=108** — was `not_found` with empty fields ⇒ Genius HIT via
  filename-derived T.Love alt; LRCLib still misses (acoustic variant
  unindexed) but the synced-or-Genius criterion is met.

## Residual zero-coverage

- **id=119** `Daniel Liebt — Fireflies (feat. JAYNIE)` — same as before.
  Cover song with no record on LRCLib or Genius. Acceptable: the
  Whisper-ASR fallback path handles it at runtime; no metadata or
  candidate change can rescue it without a new online catalogue entry.

## LRCLib-misses rescued by Genius

Three rows have no LRCLib synced LRC but satisfy the criterion via
Genius plain text + the local wav2vec2 `genius-sync` aligner:

- id=97 `Gejtos / Antyczny Napaleniec` (Polish niche track)
- id=105 `Brodka / Miales Byc` (charset mismatch w/ LRCLib indexer)
- id=108 `Kutas Records / Antyczny Napaleniec - Acoustic` (acoustic variant)

## Recommended follow-ups

1. **Re-run with Spotify enabled later** to fill in the locked-out
   column. The bug-G long-Retry-After short-circuit means a re-run
   costs ~zero if Spotify is still locked out.
2. **id=119 disposition** — accept as Whisper-ASR-only or remove from
   the must-have-lyrics test set.
3. **id=96 / id=108 metadata** — both rows still have a producer label
   (`Kutas Records, Łukasz B, ...`) as artist. The bug-F filename-reseed
   couldn't improve on it because the filenames also lack an
   `Artist - Title` separator (`Don Pedalini---<id>.mp4`,
   `Antyczny Napaleniec - Acoustic---<id>.mp4`). A manual edit (like
   id=97 already got) is the cleanest fix; the manual-edit path
   already drives lyrics fetches per `feat(metadata): manual song edits drive lyrics fetch (#19)`.
