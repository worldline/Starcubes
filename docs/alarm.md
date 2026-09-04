# Alarm evaluation

Threshold alarms over the loaded fact tables: what a stored alarm means,
how the background loop evaluates it, and how to drive it by hand.

## Schema (`migrations/0028_alarm_context.sql`)

| table | role |
|-------|------|
| `starcubes_meta.alarm` | one rule: `measure`, `cond_op` (`> < = >= <=`), `threshold`, trailing `window_s`, `enabled`, `comment`, `context_filter` jsonb, `eval_end` (the HWM last evaluated at) |
| `starcubes_meta.alarm_history` | change history: `kind` (`created`/`updated`), `username`, `changed_at`, old/new row snapshots (jsonb). No `deleted` kind — everything CASCADEs with the alarm. |
| `starcubes_meta.alarm_firing` | one row per episode: `first_breach_at`, `last_breach_at`, `closed_at` (NULL = open), `first_value`/`last_value` — all three times are the **data high watermark**, not the wall clock. `ux_alarm_firing_open` pins at most one open episode per alarm. |

`alarm.eval_end` is written by the evaluator only, and never touches
`updated_at` — runtime progress must not masquerade as a user
configuration change in `alarm_history`. (`alarm_forever`, the retired
global loop's session table, is dropped by `0029_alarm_hwm_semantics.sql`.)

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

Three ways in. The drill-down's Alert form (filters plus the clicked series);
the toolbar's bell, which opens the Alarms tab with the live filters and no
series (`alarmContextRows(false)`); and **Clone** on an existing alarm, which
copies its context into the add form so only the metric needs changing. The
Alarms tab offers no context-less add button — a global alarm is created
deliberately, by pressing the bell on a chart with no filters.

The bell also carries the chart's own query string as `back=`, which the brand
link on the sources pages uses to return to exactly the view that was left.

The rows store dimension **ids**, because an id is what the fact table joins
on and what survives a rename. `core.alarm.resolve_context_labels` maps them
to names for display only — one query per dim table for a whole list, never
written back, so a rename shows through the next time the list is opened. An
id whose dimension row is gone is shown raw and flagged rather than quietly
passing for a name.

`core.alarm.normalize_context_rows` gives a context a canonical form (values
sorted and stringified; row order kept, because it encodes the tree together
with `indent`/`andOr`). `sources:alarm_match_context` uses it to point out
alarms already watching the same context **with the same measure** — advisory
only, since a warning threshold and a critical one over one slice are the
normal case.

**Grouped tokens are forbidden — in filter columns.** Group sets are
user-editable in the Group Dims tab (rename, delete, re-group — any
logged-in user), so a stored `<col>:grouped:<slug>` reference would break
silently, or worse: keep working while its meaning changes. The CRUD
rejects grouped filter columns at save time
(`core.alarm.check_no_grouped_filters`), the chart's Alert form blocks them
before the POST, and the evaluator checks again defensively.

A grouped **group_by** is a different matter and does *not* block anything:
it only decides how the chart splits its series and is never stored. The
clicked series is simply left out of the context, the alarm scopes to the
filter rows alone, and the form says so (`alarmIgnoredGrouping()`) rather
than leaving the narrower context unexplained. The chart used to refuse both
cases; the server only ever checked filter columns.

Fan-out tokens (`dimension.filter_group`) stay allowed — they are
admin-configured, and a broken one fails validation loudly in the loop's
log.

## Evaluation — one sliding window per load

The clock is the **fact clock**: `max(watermark_fact_sql)` per datasource —
the data's own high watermark, not the wall clock (fact data lags behind by
the watermark lag).

Each ETL load evaluates exactly **one trailing window ending at the HWM**:
`[hwm − window_s, hwm)`. Consecutive loads overlap, and the question asked is
always the same: *over the last `window_s` of data we have, does the measure
breach?* `alarm.eval_end` records the HWM of the last evaluation; a load that
did not move the HWM is skipped outright, because the window — and therefore
the verdict — would be identical.

A window breaches when its aggregate satisfies `cond_op threshold`. A SQL
NULL aggregate (empty window under `sum(col)`) is *not* a breach;
`count(*)` over an empty window is a genuine 0, so `count(*) < 1` is a
valid "no data" alarm.

Episodes are stamped with the HWM at each transition — never a window
boundary, never the wall clock. Opened at the HWM of the load that first saw
the breach (`first_breach_at`), extended to the HWM of every further
breaching load (`last_breach_at`), closed at the HWM of the first load that
saw the value back under the threshold (`closed_at`). One record spans the
first to the last breach — a long outage is one row, not one per load. An
episode seen by a single load has span 0.

**Trade-offs (accepted):**

- A load that fails or is delayed takes its evaluation with it. The window
  slides with the HWM instead of being chained to the previous one, so a
  breach living entirely between two loads is never seen.
- Same philosophy as the audit: rows arriving late into a window the HWM has
  already slid past are never re-evaluated.
- A total data standstill is invisible: the HWM stops, the window stops
  sliding, and `count(*) < 1` never comes true. Watching the pipeline itself
  (`etl_run` status, `watermark_lag`) is the ETL monitor's job, not an
  alarm's.

## Who evaluates

The ETL does, and nothing else. `sources/management/commands/etl_run.py`
calls `run_alarm_cycle(meta, dsn, datasource_id=source.id, source=source)`
after every `import_fact`, inside its own `try/except` so an alarm problem
can never colour the ETL's outcome. It runs after a caught ETL failure and
under `--backfill` too — the HWM skip decides what actually gets evaluated,
and the cost of a skipped evaluation is one `max()` per alarm.

There is deliberately **no forever loop**. A load is the only thing that
moves the HWM, so it is the only moment an alarm's answer can have changed;
a second, independently timed evaluator would race the ETL for the same
episodes and re-ask questions whose data had not moved. `alarm_run --forever`,
`alarm_resume_forever` and the `alarm_forever` session table are all gone
(`0029_alarm_hwm_semantics.sql`).

## Manual driving

```
manage.py alarm_run               # evaluate every enabled alarm now
psql … "SELECT * FROM starcubes_meta.alarm_firing ORDER BY id DESC"
psql … "SELECT id, name, eval_end FROM starcubes_meta.alarm WHERE enabled"
```

`alarm_run` is the manual entry point only; it evaluates every datasource
once and exits. Running it while an ETL load is in flight is harmless — both
paths take the same HWM skip and the same `ux_alarm_firing_open` guard.

The Alarms tab on `detail.html` shows the context summary (names, not ids),
comment and per-alarm history/firing episodes (ⓘ button). Its add form takes
a context too — carried in, never typed, since a filter expression is a UI of
its own: `?tab=alarms&new-alarm=1&context=<json>` from the chart's bell, with
`context=session` (sessionStorage) as the fallback for a context too long for
a URL and `context-blocked=grouped|toolong` when it cannot be carried at all.
The chart drill-down's Alert form is the other creating path.

The global episode overview (`/sources/alarms/firings/`,
`alarm_firings_page`) takes `?show=open` (live breaches only) and `?ds=<id>`
(one datasource). The datasource page shows the same list as its Firings
tab (`?tab=firings`), from the same reader and the same template partial —
see [`docs/sources-app.md`](sources-app.md).