# Alarm evaluation

Threshold alarms over the loaded fact tables: what a stored alarm means,
how the background loop evaluates it, and how to drive it by hand.

## Schema (`migrations/0028_alarm_context.sql`)

| table | role |
|-------|------|
| `starcubes_meta.alarm` | one rule: `measure`, `cond_op` (`> < = >= <=`), `threshold`, trailing `window_s`, `enabled`, `comment`, `context_filter` jsonb, `eval_end` (evaluation cursor) |
| `starcubes_meta.alarm_history` | change history: `kind` (`created`/`updated`), `username`, `changed_at`, old/new row snapshots (jsonb). No `deleted` kind — everything CASCADEs with the alarm. |
| `starcubes_meta.alarm_firing` | one row per episode: `first_breach_at`, `last_breach_at`, `closed_at` (NULL = open), `first_value`/`last_value`. `ux_alarm_firing_open` pins at most one open episode per alarm. |
| `starcubes_meta.alarm_forever` | the global loop's session table; `ux_alarm_forever_active` pins at most one active session world-wide. |

`alarm.eval_end` is written by the evaluator only, and never touches
`updated_at` — runtime progress must not masquerade as a user
configuration change in `alarm_history`.

## The context: the chart's filter expression

`alarm.context_filter` holds the chart's flat filter rows verbatim —

```json
[{"col": "peer_id", "op": "in", "vals": ["9999"], "andOr": "and", "indent": 0}]
```

— the exact array `chart.html:gatherParams()` serializes. The nested tree
is derived at evaluation time (`core.query.build_filter_tree`) and compiled
by the same `_build_where` the chart endpoints use, so the evaluated slice
is identical by construction to what the user saw. The chart drill-down's
Alert form merges the clicked series into the context as an extra `in`
row (same mechanics as `drillAddFilter`). `NULL`/`[]` = whole datasource.

**Grouped tokens are forbidden.** Group sets are user-editable in the
Group Dims tab (rename, delete, re-group — any logged-in user), so a
stored `<col>:grouped:<slug>` reference would break silently, or worse:
keep working while its meaning changes. The CRUD rejects grouped tokens
at save time (`core.alarm.check_no_grouped_filters`), the chart's Alert
form blocks them before the POST, and the evaluator checks again
defensively. Fan-out tokens (`dimension.filter_group`) stay allowed —
they are admin-configured, and a broken one fails validation loudly in
the loop's log.

## Evaluation — the cursor model (`core/alarm.py`)

The loop's clock is the **fact clock**: `max(watermark_fact_sql)` per
datasource, not the wall clock (fact data lags behind by the watermark
lag). `alarm.eval_end` marks the end of the last fully evaluated window.
Each cycle advances in `window_s`-sized steps from the cursor to the fact
clock — no window is skipped when the loop interval exceeds `window_s`,
none is counted twice.

The **first** evaluation checks exactly one trailing window
`[fact_max − window_s, fact_max)`: a freshly created alarm can fire on
current data immediately, but history before its creation is not
replayed.

A window breaches when its aggregate satisfies `cond_op threshold`. A SQL
NULL aggregate (empty window under `sum(col)`) is *not* a breach;
`count(*)` over an empty window is a genuine 0, so `count(*) < 1` is a
valid "no data" alarm.

Episodes: opened on the first breaching window (`first_breach_at` =
window start), extended by every further breaching window
(`last_breach_at`), closed on the first non-breaching window
(`closed_at`). One record spans the first to the last breach — a long
outage is one row, not one per cycle.

**Trade-off (accepted, same philosophy as the audit):** rows arriving
late into an already-evaluated window are never re-evaluated — an alarm
can miss a breach that late data would have created.

## The loop

`sources/management/commands/alarm_run.py` — the `etl forever` skeleton
without per-datasource scoping:

```
manage.py alarm_run --once          # one cycle (also the default)
manage.py alarm_run --forever 60    # global loop
```

SIGTERM/SIGINT set a flag (stop between cycles, never mid-cycle); the
loop polls `stop_requested` each iteration; per-cycle and per-alarm
failures are logged and skipped. `docker/entrypoint.sh` starts it at
every container boot via `alarm_resume_forever` (fail-before-insert on
the singleton index, then a detached `spawn_etl_subprocess` with
`datasource_name="alarm-loop"` so the log rides `prune_etl_logs` without
appearing in a per-source ETL Log tab).

The loop interval (default 60 s) only bounds how *soon* a breach is
noticed — the cursor drains every completed window each cycle, so a
slower loop changes nothing about which windows are evaluated.

## Manual driving

```
manage.py alarm_run --once        # evaluate every enabled alarm now
psql … "SELECT * FROM starcubes_meta.alarm_firing ORDER BY id DESC"
psql … "SELECT id, name, eval_end FROM starcubes_meta.alarm WHERE enabled"
```

The Alarms tab on `detail.html` shows the context summary, comment and
per-alarm history/firing episodes (ⓘ button); the chart drill-down's
Alert form creates alarms with a filter context. The global episode
overview (`/sources/alarms/firings/`, `alarm_firings_page`) takes
`?show=open` (live breaches only) and `?ds=<id>` (one datasource — the
chart toolbar's firings button links here pre-filtered that way).