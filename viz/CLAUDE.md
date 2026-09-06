# `viz` — file notes

File-by-file notes for `viz`, split out of the root `CLAUDE.md` so they
load only when work touches this directory. The project-wide rules — git
workflow, code conventions and the verification checklist — stay in the
root [`CLAUDE.md`](../CLAUDE.md); read that one too.

| File | Purpose |
|------|---------|
| `viz/views.py` | Chart page, plot/drill/txn JSON endpoints, `slides` (the blueprint slide deck at `/slides`, streamed from repo-root `presentation.html`) and `deck_asset` (streams the deck's assets — its own under `presentation/`, the screenshots under `docs/img/` and the logo under `viz/static/viz/` — behind resolve-guards; the deck opens standalone from the filesystem too, same relative paths). `plot_json` consults `core.cache` for cacheable `count(*)`. The four `query_*` console endpoints gate themselves with `_login_required_json` (403 JSON, not a 302) and answer user SQL errors with HTTP 200. |
| `viz/templates/viz/chart.html` | Multi-source chart UI — see [`docs/chart-app.md`](../docs/chart-app.md) and [`docs/chart-rendering.md`](../docs/chart-rendering.md). The drill-down's Alert form (`openAlarmView`/`alarmContextRows`/`saveAlarmForm`) creates real alarms with the active filters + clicked series as context; grouped **filter** columns are blocked up front, a grouped `group_by` only drops the series. The three toolbar links into the configuration — Group Dims (`#source-groupdims-link`), Measures (`#source-measures-link`) and the bell (`#alarms-link`) — are built together in `updateSourceLinks` (refreshed from `gatherParams`) and all carry the live filter as `?context=`, plus `back=` (the chart's own query string) so the sources pages' brand link returns to this exact view; only the bell adds `new-alarm=1` and opens the add form. `alarmContextParam` decides once for all three how the context travels (URL vs. the shared `sessionStorage` key). The boot IIFE calls `applySourceState` when the URL has no `group_by`, before `initTs()` — see [`docs/chart-app.md`](../docs/chart-app.md). |
