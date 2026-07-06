# UX audit 2026-07-06 — dokumentacja + poprawki

## Cel

Udokumentować audyt UX (dogfooding przez headless Chrome na viewport iPhone),
dopisać brakujące scenariusze do `docs/USER_STORIES.md` + compliance-wpisy do
`docs/USER_STORIES_TODO.md`, naprawić potwierdzone pain pointy.

## Plan

- \[x\] **Dokument audytu** `docs/UX_AUDIT_2026-07-06.md`.
- \[x\] **User stories** US-53..US-56 w `USER_STORIES.md`.
- \[x\] **USER_STORIES_TODO.md**: wpisy compliance z priorytetami.
- \[x\] **Fix: stepper offsetu** — touch-action/user-select, hit ≥44px,
  hold-to-repeat z akceleracją, debounce POST, przycisk ±, parser
  (przecinek, trailing minus). `offset-stepper.js` + vitest.
- \[x\] **Fix: postęp pobierania na karcie wyniku** — nasłuch
  `download_progress`/`song_event`/`song_warning`, trwały lifecycle
  karty (requested → % → w kolejce / błąd), rejestracja przed POST
  (race z cache-hitem), binding w jQuery ready (race z connectSocket).
- \[x\] **Fix: preview sheet** — spinner, komunikat błędu, CTA
  "Dodaj do kolejki" przejmujące flow karty.
- \[x\] **Fix: Auto-napisy (P0)** — engine sam re-fetchuje LRCLib gdy
  caller nie przekaże LRC (recompute przekazywał None); odmowa
  whisper-only fallbacku w `build_consensus`. Testy regresyjne.
- \[x\] **Fix drobne** — katalog pl (toasty), `--log-level info`,
  avsync jako float (naprawia pre-existing fail
  `test_play_file_avsync_...`), kółko nachodzące na tytuł.
- \[x\] **Testy**: 1907 passed (pytest), 84 passed (vitest), pre-commit OK.
- \[x\] **Weryfikacja E2E** w headless Chrome (viewport iPhone):
  stepper (klik/hold/±/parser), cache-hit → "w kolejce", świeży
  download z żywym %, preview spinner + CTA, polski toast, wiersz
  "gra teraz" bez nakładania.

## Review

Commity: `49e0ccf9` (docs), `071dd175` (US-56), `049ffc31` (US-53/54),
`f0c9185c` (US-55 P0), `ab5f9bb0` (drobne). Pozostałe otwarte pozycje
(picker guest-readable, post-processing na telefonie, worker-thread
toasty, pełny katalog pl, Phase-3 scoring) — w `USER_STORIES_TODO.md`.

Lekcja: serwer poza debug **cache'uje szablony** — przy weryfikacji
zmian w `templates/` restart serwera jest obowiązkowy, inaczej testuje
się starą wersję (kosztowało dwie fałszywe iteracje debugowania).
