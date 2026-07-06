# UX audit follow-up 2026-07-06 — runda 2 (zamknięta)

## Plan

- \[x\] **US-55 P2: picker źródeł czytelny dla gościa** — partition
  primary/secondary, fold "Niedostępne (N)", kropka severity zamiast
  "3/7" na triggerze. Vitest.
- \[x\] **US-54 P2: kolejka pokazuje przygotowanie** — pill "przygotowuję
  wokal… X%" (demucs_progress → stems_ready), zweryfikowane E2E.
- \[x\] **US-53 P2: primary CTA "Dodaj"** na karcie wyniku.
- \[x\] **i18n P2: toasty z workerów po polsku** — app-context w workerze
  i wątkach split-download, has_request_context() w get_locale,
  preferred_language=pl w configu usera. E2E: "Zaczynam pobieranie",
  "Pobrano i dodano do kolejki" z workera po polsku.
- \[x\] **i18n P3: pełny katalog pl** — pot zregenerowany (był stale),
  331/331 stringów.
- \[x\] **BONUS (zgłoszenie usera): "Drive" lrclib+sync rozjechane** —
  root cause: wariant omijał \_RELIABILITY_GATE (fałszywy anchor VAD
  -3.4s + kompresja linii do 0.6s okien). Fix: replay/regrade score,
  poniżej bramki whole-song alignment z fence'ami z LRC. Zepsuty
  wariant Drive usunięty i przerenderowany — timestampy 1:1 z lrclib
  (0:10.33). Testy: TestLrclibSyncReliabilityGate (5).
- \[x\] Testy: 1912 pytest + 88 vitest, pre-commit czysty, E2E na 5556.

## Review

Commity rundy 2: `93e91354` (picker), `ad3e104b` (Drive gate),
`bee9827d` (prep pill), `5672f08c` (CTA), `ea3cabc8` (i18n), + docs.
Otwarte końcówki w USER_STORIES_TODO.md: lyrics-stage na telefonie,
gettext dla \_emit_stage_notification, post-hoc max_anchor_shift check,
Phase-3 scoring.
