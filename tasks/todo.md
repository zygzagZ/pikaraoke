# Invalidate subs on artist/title metadata change

## Cel
Cache napisów (auto `.ass` + variant `.ass` + `subtitle_jobs`) ma być
invalidowany za KAŻDYM razem gdy `artist` lub `title` faktycznie zmienia
wartość w `songs`, niezależnie od źródła zmiany (manual edit, auto-enricher,
scanner backfill, consensus rerun, lyrics pipeline writeback).

## Dlaczego
Tylko `_apply_metadata_edit` (manual UI) wywołuje
`invalidate_for_metadata_change`. Wszystkie inne writy do
`update_track_metadata_with_provenance` (14+ call-sites) zostawiają stary
.ass na dysku — napisy odpowiadają poprzednim wartościom artist/title, a
kolejne lookupy LRCLib/Genius i tak nie startują, bo `subtitle_jobs` mówi
"już próbowałem".

## Plan

- [x] **Detekcja value-change w `update_track_metadata_with_provenance`**
      (`pikaraoke/lib/karaoke_database.py:1010`).
      Czytać stare wartości pól razem z `metadata_sources`. Po commitcie
      zwracać dwa zbiory: `applied` (jak dziś — zapisane pola) oraz fire
      hooka jeśli `{"artist","title"} & changed_values != ∅`. Hook woła
      się PO zwolnieniu locka, w tym samym wątku.

- [x] **Listener slot na `KaraokeDatabase`.**
      `set_track_metadata_change_listener(callback)` — pojedynczy callback,
      sygnatura `(song_id: int, changed: frozenset[str])`. Wyjątki w
      callbacku łapane i logowane, nigdy nie zwalają writeu DB.

- [x] **Wpięcie w `Karaoke.__init__`** (`pikaraoke/karaoke.py:519`).
      Po stworzeniu `lyrics_service`, `subtitle_orchestrator` rejestrujemy
      `self._on_track_metadata_change`:
      1. `song_path = self.db.get_song_by_id(song_id)["file_path"]`
      2. `self.lyrics_service.invalidate_for_metadata_change(path)`
      3. `self._dispatch_lyrics_fetch_async(path)`
      4. `self.subtitle_orchestrator.kickoff(path, force=True)`

- [x] **Sprzątanie `_apply_metadata_edit`** (`pikaraoke/routes/files.py:346`).
      Hook robi już invalidate+dispatch+kickoff dla artist/title. Zostawiamy
      ścieżkę manualną tylko dla zmian `language` (do których hook celowo
      nie strzela, żeby nie pętlić auto-detekcji języka z lyrics pipeline).

- [x] **Testy:**
  - listener fires gdy artist się zmienia
  - listener fires gdy title się zmienia
  - listener NIE fires gdy tylko source upgrade (ta sama wartość)
  - listener NIE fires gdy zmieniają się pola spoza setu (np. itunes_id)
  - integracja: `_apply_metadata_edit` → DB write → listener → invalidate
  - integracja: enricher write → listener → invalidate

- [x] Uruchomić cały test suite (`pytest tests/unit/test_karaoke_database.py
      tests/unit/test_files_routes.py`).

## Tradeoffs
- Wybrałem callback DB-level zamiast threadowania per-call-site, bo:
  - 14+ call-sites (lyrics pipeline, scanner, enricher, consensus, manual)
  - Zero ryzyka że nowy call-site zapomni invalidować
  - Pojedynczy punkt do testowania
- Zawężam set invalidujący do `{artist, title}` (nie `language`), żeby
  uniknąć potencjalnej pętli z auto-detekcji języka. Manual edit dalej
  invaliduje na język explicit.

## Nie w scope
- Filtr similarity na iTunes/MB candidates (osobny PR — patrz
  poprzednia rozmowa o "Mam moc Trzyha").
- Spotify rate-limit handling.

## Review
**Pliki zmienione:**
- `pikaraoke/lib/karaoke_database.py` — `_INVALIDATING_FIELDS`, slot
  listenera, `set_track_metadata_change_listener()`, value-change detection
  w `update_track_metadata_with_provenance` (pre-read starych wartości,
  fire listenera po lock release z try/except).
- `pikaraoke/karaoke.py` — `_on_track_metadata_change(song_id, changed)` +
  rejestracja w `__init__` po stworzeniu `lyrics_service` /
  `subtitle_orchestrator`.
- `pikaraoke/routes/files.py` — `_apply_metadata_edit` bez zmian
  funkcjonalnych (idempotentny z listenerem); dopisany docstring.

**Testy:**
- `tests/unit/test_karaoke_database.py::TestTrackMetadataChangeListener`
  — 9 testów (first write, value change, no-fire on same-value upgrade,
  no-fire when lower confidence blocked, non-invalidating fields, mixed
  write reports tylko changed, exception swallowed, clear listener,
  reentrancy bez deadlocka).
- `tests/unit/test_karaoke.py::TestOnTrackMetadataChange` — 5 testów
  (full cascade, missing song id no-op, invalidate exception nie blokuje
  reszty, end-to-end DB write -> listener -> cascade, same-value rewrite
  nie fires).

**Wynik suite:** `1875 passed, 1 skipped` (cały tests/unit).

**Behaviour delta:**
- *Przed:* Tylko manual edit (`_apply_metadata_edit`) wywoływał
  `invalidate_for_metadata_change`. Auto-enricher / scanner / consensus
  rerun zostawiały stare `.ass` i `subtitle_jobs`.
- *Po:* Każdy zapis do `update_track_metadata_with_provenance` z
  zmieniającą się wartością `artist` lub `title` automatycznie:
  1) usuwa cached `.ass` (auto + variants), 2) czyści `subtitle_jobs`,
  3) dispatchuje świeży lyrics fetch, 4) `kickoff(force=True)` na
  orchestratorze. Same-value source upgrade nie fires (no work).
