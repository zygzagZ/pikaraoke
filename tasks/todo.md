# UX audit follow-up — runda 3 (zamknięta)

## Plan

- \[x\] **(1) US-54: etap napisów na kolejce** — song_event mirror ze
  stage'ów + pill ("szukam napisów…" / "dopasowuję napisy…" /
  "transkrybuję (AI)…"), demucs-% ma pierwszeństwo, TTL 15 min.
- \[x\] **(2) i18n stage-toastów** — LyricsService.\_translate pod
  app-contextem (fabryka z app.py), markery \_STAGE_MSGIDS dla pybabel,
  katalog pl uzupełniony ("Szukam napisów" itd.). E2E: toast po polsku
  z wątku pipeline'u.
- \[x\] **(3) post-hoc guard max_anchor_shift** — przy przesunięciu
  ≥ \_GRADE_SHIFT_KILL_S (30 s) wynik line-windowed odrzucany,
  re-align whole-song; legalne globalne offsety (≤10 s) nietknięte.
- \[x\] Testy: 1916 pytest + 88 vitest (w tym TestStageNotification i
  2 nowe przypadki guardu), pre-commit czysty, E2E na 5556.

## Review

Commit: feat(lyrics): stage visibility on phone, translated stage
toasts, post-hoc anchor guard. Kolejka testowa wyczyszczona; w
bibliotece przybył testowy "Together Forever" (Rick Astley / Zoom
Karaoke). Z audytu został już tylko duży projekt Phase-3 (scoring
jakości + guard same-tier w \_try_write_ass_tiered).
