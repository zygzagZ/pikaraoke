# Lessons

Self-corrections after user feedback. Read at session start for this project.

## fork-vs-upstream PR target — 2026-05-05

**Mistake:** ran `gh pr create --base master` from this repo. `gh` defaulted the
target repo to **`vicwomg/pikaraoke`** (the upstream parent of the fork) and
opened a PR there, not on the user's fork.

**Why it happens:** when the local repo was created with `gh repo fork`, gh
remembers that lineage and `gh pr create` without `--repo` picks the parent.
The `origin` remote points at the fork (`zygzagZ/pikaraoke`), but `gh` ignores
that — it goes by fork lineage, not git remote.

**Why it matters here:** `zygzagZ/pikaraoke` master has diverged heavily from
`vicwomg/pikaraoke` master (custom Polish UI, themes, Phase 1+ subtitle work).
A PR against upstream shows hundreds of unrelated commits and a public diff
the user never intended to send to upstream maintainers.

**Permanent fix applied 2026-05-05:** removed the `upstream` git remote
(was `https://github.com/vicwomg/pikaraoke.git`). After removal, only
`origin → git@github.com:zygzagZ/pikaraoke.git` exists. `gh pr create`
should now default to the fork.

**Belt-and-braces rule:** still pass `--repo zygzagZ/pikaraoke` to
`gh pr create` explicitly. Don't assume gh's default is correct just
because the remote is gone — the fork lineage on github.com still says
`vicwomg/pikaraoke` is the parent (`gh repo view --json parent`).

**Symptom to watch for:** `gh pr create` exits with a URL pointing at
`vicwomg/pikaraoke/pull/...`. If the URL host/owner is not the user's
fork, the create is wrong — close the upstream PR with a comment and
re-create with `--repo zygzagZ/pikaraoke`.

This has happened more than once. Memorize.

## 2026-07-06: Flask cache'uje szablony poza debug — restart przed weryfikacją

Podczas weryfikacji poprawek w `templates/search.html` (headless Chrome,
serwer `uv run pikaraoke`) dwie kolejne iteracje "fixów" wyglądały na
nieskuteczne, bo serwer wciąż serwował szablon sprzed edycji — Flask bez
`debug=True` nie przeładowuje Jinja templates (auto_reload = debug).

**Reguła:** po KAŻDEJ edycji pliku w `pikaraoke/templates/` restartuj
serwer testowy zanim uznasz, że zmiana (nie) działa. Objaw rozjazdu:
w przeglądarce działa kod, którego commit dopiero co zmienił, albo
`window.PK.*` nie zawiera świeżo dodanych symboli mimo twardego reloadu.
