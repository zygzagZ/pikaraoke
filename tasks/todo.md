# Consensus jako osobne źródło — line-level merge

## Cel
Przepisać consensus z token-voting (psuje "Mam tę moc") na **line-merge**:
wybiera per-linia która z dostępnych tekstowych wersji najlepiej pasuje do
okna whispera. Brak wycinania słów. Consensus staje się normalnym wariantem
napisów (`<stem>.consensus.ass`, label "Auto") widocznym w pickerze.

## Plan

### A. Line-merge engine (rdzeń)
- [ ] `lyrics_consensus.py`: dodać `Word`-aware audio reference z timestampami
- [ ] `lyrics_consensus.py`: nowa funkcja `merge_lines_per_window` —
  iteruje po liniach scaffolda timing-owego, dla każdej linii oblicza
  `score_S` przeciw oknu whispera, wybiera tekst całą linią z najwyższego
  score'u (no token vote)
- [ ] Logika "extra zwrotek": linia obecna tylko w 1 źródle + okno whispera
  puste → drop; ≥2 źródła → keep
- [ ] Linie z innych źródeł nieobecne w scaffoldzie — wstaw między linie
  scaffolda gdy ≥2 źródła + whisper potwierdza
- [ ] Usunąć `vote_tokens`, stary `build_consensus_lrc`, `_index_voted_text`

### B. Gating + re-run
- [ ] Consensus run iff ≥1 external source AND whisper dostępny
  (cache hit liczy się jako "dostępny")
- [ ] Whisper cache weryfikacja — jest, sprawdzam że jest used przed model load
- [ ] Re-run trigger: po `_write_and_register_variant` wywołać
  `_recompute_consensus_if_changed(song_path)` z hash-em zestawu źródeł
  zapamiętanym w `metadata` (`consensus_sources_hash:<sha>`)

### C. Picker / DB
- [ ] `karaoke_database.py`: `SUBTITLE_SOURCE_CONSENSUS = "consensus"`,
  dodać do `VALID_SUBTITLE_SOURCES`, `VARIANT_FILE_SOURCES`,
  `SUBTITLE_SOURCE_LABELS["consensus"] = "Auto"`
- [ ] `karaoke.py`: dodać `SUBTITLE_SOURCE_CONSENSUS` do
  `_SUBTITLE_SOURCE_ORDER` (po `user`, przed `lrclib`),
  capability gate (≥1 external survived consensus + whisper dostępny)
- [ ] `subtitle-source-picker.js`: dodać `'consensus'` do `canonicalOrder`
  i (NIE do `ALARM_SOURCES_DEFAULT` — to nie surowe źródło, to mix)

### D. Variant write
- [ ] `_try_write_ass_tiered`: gdy `lyrics_source == "consensus"`, pisz też
  variant `<stem>.consensus.ass` (przedtem był wykluczony)

### E. Verify
- [ ] Lokalnie: usunąć cached canonical .ass dla "Mam Tę Moc", odpalić
  pipeline, sprawdzić że tekst per-linia zgadza się z lrclib variant
  i że nie ma "lat coś objęcia chłodu mnie" tylko pełne
  "Od lat coś w objęcia chłodu mnie pcha"
- [ ] Picker pokazuje "Auto" jako aktywne źródło (nie em-dash)

## Decyzje

- Próg confirmation per-linia: 0.55 (odziedziczone z `_DEFAULT_THRESHOLD`)
- Chunking unit: linia LRC
- Default canonical: dalej consensus (gdy się powiedzie); fallback do
  najlepszego pojedynczego źródła gdy nie
- Label PL: "Auto"
- Pozycja w pickerze: 3 (po off, user; przed lrclib)

## Decyzje porzucone

- Token-level voting — usunięte całkowicie, było źródłem regresji
- Whisper dorzucany do tekstu — Whisper TYLKO scoring window i k-tag timing

## Review (2026-05-08)

**Co działa:**
- "Auto" jako osobne źródło w pickerze; em-dash bug zniknął przy okazji
  bo `consensus` wszedł do `_SUBTITLE_SOURCE_ORDER`
- Line-merge zamiast token-vote: każda linia bierze CAŁY tekst z
  najlepszego (vs whisper window) źródła. Żadnego cięcia słów.
- "Mam Tę Moc" weryfikacja: wszystkie 47 linii LRClib zachowane
  poprawnie, włącznie z "Co tam burzy gniew?", "Od lat coś **w** objęcia
  chłodu mnie **pcha**", "Wszystkim wbrew", "Wreszcie ja, zostawię ślad".
- Whisper transcript czytany z metadata cache (key
  `whisper_transcript:<sha>:<model>`); nigdy nie odpala modelu drugi raz.
- Re-run trigger: nowy variant landuje → hash inputs + porównanie z
  `consensus_input_hash:<sha>` → dispatch jeśli różny. Self-write
  guarded.
- Drop reguła scaffold-only line tylko gdy ≥2 synced sources (inaczej
  każda linia byłaby scaffold-only i traciliśmy quiet bridges).
- 1883 testów zielonych.

**Iteracja w trakcie:**
- Pierwsza wersja drop reguły była za agresywna — gubiła "Wszystkim
  wbrew" gdy whisper nie usłyszał quiet sekcji. Naprawione: drop
  aktywny tylko gdy ≥2 synced sources dają baseline porównania.

**Co dalej (poza scope):**
- Genius (plain_text) jako voter per linia — wymaga line-mappingu z
  scaffold tokens; obecnie dolicza tylko do source-rejection scoringu.
- Persisting consensus output do DB jako "lyrics_provenance=consensus"
  zamiast `auto_word` — drobiazg, kosmetyka.
