# UX audit 2026-07-06 — dokumentacja + poprawki

## Cel

Udokumentować audyt UX (dogfooding przez headless Chrome na viewport iPhone),
dopisać brakujące scenariusze do `docs/USER_STORIES.md` + compliance-wpisy do
`docs/USER_STORIES_TODO.md`, naprawić potwierdzone pain pointy.

## Plan

- \[ \] **Dokument audytu** `docs/UX_AUDIT_2026-07-06.md` — metodologia, co
  sprawdzono, obserwacje z dowodami (file:line), priorytety.
- \[ \] **User stories**: US-53 (interakcja z wynikiem YouTube / preview),
  US-54 (widoczność postępu pobierania i pipeline z telefonu),
  US-55 (jakość doboru źródła napisów w Auto + czytelny picker),
  US-56 (kontrolka offsetu napisów na mobile).
- \[ \] **USER_STORIES_TODO.md**: wpisy compliance dla US-53..56 z priorytetami.
- \[ \] **Fix: stepper offsetu** — `touch-action: manipulation`,
  `user-select: none`, hit-area ≥44px, hold-to-repeat z akceleracją
  (pointer events), obsługa minusa mimo klawiatury iOS bez "−".
- \[ \] **Fix: postęp pobierania na karcie wyniku** — nasłuch
  `download_progress`/`download_started`/`download_stopped` na stronie
  szukaj; trwały stan karty (pobieranie % → w kolejce), przycisk nie
  wraca do stanu neutralnego po 2,5 s.
- \[ \] **Fix: preview sheet** — spinner podczas resolve, komunikat błędu
  zamiast cichego zamknięcia, CTA "Dodaj do kolejki" w arkuszu.
- \[ \] **Fix: Auto-napisy** — root cause "consensus: no sources" mimo hitu
  LRCLib (agent bada); preferencja LRCLib nad transkryptem AI.
- \[ \] **Fix drobne**: toast po polsku ("Song added to the queue"),
  `--log-level info` (int() crash), nakładanie się kółka na tytuł
  w wierszu "gra teraz".
- \[ \] **Testy**: vitest dla logiki hold-to-repeat/parsera offsetu (pure),
  pytest dla zmian w lib (jeśli dotknięte).
- \[ \] **Pre-commit + commity** per logiczna jednostka.

## Review

(uzupełnić po zakończeniu)
