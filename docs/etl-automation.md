# ETL automation — reference

Everything that has to be true *around* a load, and the machinery that makes
it true. [`etl-engine.md`](etl-engine.md) describes how a run works; this
document is about what happens before, between and after runs, what each
mechanism decides on its own, and which knob changes it.

**Read this before adding anything periodic**, and before wiring a new
maintenance action into the GUI or the CLI.

This began as an evaluation of seventeen ways a load could go quietly wrong.
All of them are addressed now; the [identifier index](#identifier-index) at
the end maps the old `U`/`N` numbers — still cited from a dozen source files
and migrations — to the mechanism that implements each.

## Why any of this exists

The forever session is the only periodic engine in the system. There is no
cron, no systemd timer, no Celery. `docker/entrypoint.sh` runs
`bootstrap` → `etl_resume_forever` → `pipeline_watch &` → `node_agent &` → nginx → uwsgi, and
a datasource in production is one `etl_run --forever N` process tracked in
`starcubes_meta.etl_forever`, revived on container start.

That has a consequence worth stating plainly: **anything that must happen
regularly either happens inside a load, or is triggered by the loop between
loads, or does not happen at all.**

The second thing these failures share is what made them worth a document:
**they are silent.** A missing index does not fail a run, it makes every
chart slow. A source that stopped producing does not fail a run, it produces
`rows=0` forever. A watermark left standing after a delete does not fail a
run — it quietly guarantees the deleted window is never reloaded. Every
defence described here exists because somebody found the silent version of a
bug first.

## Three principles

**1. The forever loop is the scheduler — except for watching itself.**
Everything periodic goes into the load (preflight) or is triggered by the
loop on a long interval (housekeeping). The one thing that cannot live there
is supervision of the loop: a wedged process cannot report that it is
wedged. That is a boundary, not a gap, and it is why pipeline alarms are
evaluated from three places (see [Alarms](#alarms)).

**2. Deleting means two opposite things.** `--delete-from` is a *repair*
gesture: the data is wrong, throw it away, load it again.
`drop-partition` / `truncate-partition` and retention are *disposal*
gestures: reclaim the space, the data is not coming back. A watermark rewind
is exactly right for the first and exactly wrong for the second. Disposal
therefore records a **retention floor** — a lower bound on what this
datasource still holds — instead of touching the watermark.

**3. Detecting and repairing are different decisions.** Catalogue queries are
free and change nothing. Anything that takes ACCESS EXCLUSIVE or moves rows
is neither free nor safe to start unattended:

> **Preflight detects and blocks. It repairs only what is idempotent and
> finishes in milliseconds. Everything else is reported and waits for a
> human gesture.**

That is the same line `ensure_partitions_for` already drew: creating a
missing partition is in scope, a DEFAULT partition as a silent safety net is
deliberately out.

---

## What runs when

| When | What | Where |
|---|---|---|
| Start of every load, before anything else | **Preflight** — eight checks; a `block` fails the run before the worker pool starts | `core/preflight.py`, called from `import_fact` |
| Start of every load | **Datasource advisory lock**, held for the whole run | `core/preflight.py:acquire_datasource_lock` |
| Every load | `import_dimensions` (subject to `dim_refresh_interval_s`), `ensure_partitions_for`, the window bound, the run budget | `core/etl.py:import_fact` |
| End of every load | `max_wm` / `src_now` and the preflight report recorded on the `etl_run` row | `core/etl.py` |
| After every load | **Alarms**, both kinds | `core/alarm.py:run_alarm_cycle`, from `etl_run.py` |
| After a load, on `housekeeping_interval_s` (default 6 h) | **Housekeeping** — retention, targeted ANALYZE, BRIN summarisation, dimension fill, partition sizes, partition correlation | `core/housekeeping.py` |
| After a load, on `audit_interval_s` (default off) | **Audit** — two stages; records a row and, when it finds a gap, the ids to repair it with | `core/audit.py:run_audit` |
| Any HTTP request, throttled to `STARCUBES_PIPELINE_ALARM_INTERVAL_S` | **Pipeline alarms**, on a daemon thread | `starcubes/middleware.py:PipelineAlarmMiddleware` |
| Continuously, in its own container process | **Pipeline alarms** | `manage.py pipeline_watch` |
| By hand | `manage.py etl_housekeeping`, `manage.py etl_audit`, `manage.py alarm_run`, the ETL tab's Preflight, Housekeeping and Audit buttons | — |

Nothing else is periodic. If you are about to add something that must happen
regularly, it belongs in one of those rows or it will not happen.

---

## Configuration reference

Everything below lives on `starcubes_meta.datasource` and is editable on the
source edit form. **Every one defaults to the behaviour that existed before
it was added**, with a single deliberate exception, marked.

| Column | Default | Unit | Set it when |
|---|---|---|---|
| `late_arrival_overlap_s` | 0 | s | Measured out-of-order arrival exceeds what `watermark_lag_s` covers. See [`etl-engine.md`](etl-engine.md). |
| `watermark_lag_s` | 0 | s | Rows become visible a bounded time after their watermark. |
| `wallclock_lag_s` | 0 | s | Traffic pauses and the window must keep moving anyway. Must exceed `watermark_lag_s`. |
| `max_data_lag_s` | 0 = off | s | The source produces *continuously*. Above this much `src_now − max_wm` the source counts as stopped. Leave unset for a batch source — a nightly gap would be a nightly false alarm. |
| `max_run_s` | 0 = off | s | You know from observed durations how long a run should take. Aborts the run past the budget and bounds every source statement inside it. Never applied to a backfill, an explicit window, or the initial load. |
| `initial_window_s` | 0 = one unbounded window | s | The first load would otherwise be hours or days in a single run. Caps the width of each catch-up step. |
| `housekeeping_interval_s` | **21600 (6 h) — on by default** | s | Almost never. This is the exception: what it fixes rots identically on every installation and is invisible by construction. 0 disables the automatic run. |
| `retention_periods` | 0 = keep everything | partition periods | You have decided, deliberately, that old data may go. **Irreversible.** |
| `dim_refresh_interval_s` | 0 = every run | s | The per-run dimension import is measurably expensive and new dimension values are rare. Costs freshness — see [Dimensions](#dimensions). |
| `audit_interval_s` | 0 = off | s | Late arrivals land beyond `late_arrival_overlap_s` and you want them found rather than reported by whoever notices. Off by default because an audit reads the source and the warehouse across a window; unlike housekeeping, it is not free. |
| `audit_window_s` | 0 = fall back to `audit_interval_s` | s | The trailing span each audit examines. Normally *wider* than the interval — late arrivals are the thing being hunted, and they land behind the tip. The fallback exists so setting only the interval cannot leave an unexamined strip between runs. |
| `audit_autofill_max` | 0 = off | rows | You have decided that a handful of stragglers is cheaper to re-fetch than to prevent. Up to this many missing rows, a forever-mode audit repairs itself with the targeted backfill instead of only alarming. |

Three columns on the same table are **state, not configuration** — written by
the engine, never edited by hand:

| Column | Meaning |
|---|---|
| `retention_floor` | Rows below this instant were disposed of on purpose. Written by the disposal gestures; read by the audit, by the alarm evaluator and by the chart's advertised extent. |
| `dedupe_from` | A watermark rewind is pending; the next incremental run must filter the reloaded span by primary key. Cleared by the run that honours it. |
| `dim_refreshed_at` | When `import_dimensions` last actually ran. |

The thresholds and ceilings behind these checks — `STARCUBES_INDEX_WARN_*`,
`STARCUBES_HWM_GAP_WARN_S`, `STARCUBES_MAX_RETENTION_DROPS` and the rest —
are environment variables with sane defaults, none normally set. They are
listed with every other variable in the
[environment variable reference](deployment-config.md#environment-variable-reference).

---

## Preflight

`core/preflight.py:run_preflight` runs at the top of `import_fact`'s `try:`
block — before `import_dimensions`, which already assumes the dim tables
exist, and well before `ensure_partitions_for`. The `etl_run` row is written
by then, so a `block` lands as `status='failed'` with the reason in `error`
through the same `except`/`finally` as any other startup failure. The whole
report is merged into `etl_run.stats.preflight`, including for a run that
never started a worker.

`repair` is a parameter, not a policy. The load runs with `repair=True`; the
GUI's **Preflight** button runs with `repair=False`, reports the same
findings and changes nothing.

Nine checks, in cost order:

| Check | Asks | Levels |
|---|---|---|
| `schema_objects` | Do `{schema}.fact` and every primary dimension's table exist? | Creates them when `repair=True` (WARNING + a `stats.preflight.repairs` entry); blocks otherwise |
| `meta_migrations` | Is `starcubes_meta` at least as new as the code? | Blocks, naming `manage.py bootstrap` |
| `indices` | Are the indices and foreign keys `create_indices` would build present? | Warns, never blocks, **never builds** |
| `warehouse_drift` | Does `fact` match the configured columns? (`sync_schema` dry run) | Blocks on a missing column or an unsafe type; warns on a safe widening or an orphan column |
| `source_projection` | Does the source still project every fact column? (`WHERE 1=0`) | Blocks on a rejected query; warns on an unreachable source |
| `watermark_tz` | Is the watermark column naive, the shape the load window binds against? (one indexed `max()`, handed forward to the load) | Blocks an aware column on a dialect whose session cannot be pinned to UTC; otherwise OK |
| `watermark_tip` | Is the stored watermark standing above a fact tip that moved down? | Warns past `STARCUBES_HWM_GAP_WARN_S` |
| `data_staleness` | `src_now − max_wm` against `max_data_lag_s` | Warns; a *negative* value is reported as a non-UTC watermark column |
| `alarm_windows` | Do any alarm windows reach below the retention floor? | Warns, naming them |

**Why the source session is pinned to UTC.** `Source.source_engine()` puts
every source connection on a UTC session clock — `SET TIME ZONE 'UTC'`,
`ALTER SESSION SET TIME_ZONE = 'UTC'`, `SET time_zone = '+00:00'`,
installed as a connect event in `core/source.py:_pin_session_utc`. The rest
of the engine already *asserts* that source timestamps are naive and hold
UTC; this makes the assertion true at the connection instead of hoping for
it, the same move `source_now_sql()` made when it stopped reading
`LOCALTIMESTAMP`.

It is not tidiness. The load window binds its bounds as naive UTC, and a
predicate mixing a naive bind with a timezone-*aware* watermark column is
resolved by converting one side with the session clock — so the window
slides by the session offset, silently. Measured on Postgres, session
`Europe/Berlin`, `timestamptz` watermark: an incremental run with nothing
to do re-extracted two hours it already held and inserted 16665 duplicate
rows; binding without the session pin lost the same 16665 instead. Under a
UTC session the one bind matches a naive and an aware column alike, which
is what `watermark_tz` reports as OK.

**The check's round trip is not an extra one.** It reads
`max(watermark_column)` plus the source clock through
`Source.watermark_probe_sql()` — the same statement `import_fact` needs to
bound the load window — and parks the reading under
`report["watermark_probe"]` as ISO strings, which `core/etl.py` then uses
instead of asking again. Measured against a Postgres source: three source
statements for the window before, two after; on a 60-second forever loop
that is 1440 needless indexed `max()` a day not issued.

Two consequences worth knowing. The value is read slightly *earlier* than
before — preflight runs ahead of `import_dimensions` — which moves the
window's upper bound marginally back, the conservative direction, with the
remainder loaded next run; `src_now − max_wm` is unaffected because both
still come out of one statement. And an import_query-only datasource is
skipped by the check on purpose, because `max()` over that subquery is a
table scan: there the engine reads for itself, exactly as before.

`mssql` is the exception that keeps the check alive: SQL Server has no
session time zone at all (`AT TIME ZONE` is per-expression), so an aware
watermark there is refused rather than loaded on an unknown clock. The
other residue is a naive column that does *not* hold UTC — no probe can see
that, because a naive value carries no evidence about its zone, and N12
(`src_now − max_wm`, the ETL History's "Data lag") stays the only detector.

**Why the index check never builds.** There is no `CONCURRENTLY` anywhere in
this repository, `create_indices` takes ACCESS EXCLUSIVE on the partitioned
parent, and on a billion-row unlogged fact table an unattended build would
block every COPY for hours while looking, from the outside, exactly like a
wedged ETL.

**Why it is time-aware.** The first load deliberately runs without indices
(fastest COPY), so "no indices" is the normal initial state. The real
complaint is "this has been syncing for weeks and they are still missing",
which needs a row count *and* an age to express.

---

## The periodic audit, and repairing what it finds

`late_arrival_overlap_s` re-reads a trailing span on every load, so a row
that arrives out of order *within* that span is caught. A row slower than
that is already below the high watermark when it appears, and no load will
ever look there again. Nothing notices: the run says `status='ok'`, the
watermark keeps moving, and the gap is permanent until somebody compares the
two sides by hand.

`core/audit.py:run_audit` is that comparison, on `audit_interval_s`, in the
forever loop beside housekeeping — **the only scheduler this system has**.

**Two stages, and the second one usually does not run.** Stage one is
`audit_window`: counts per time bucket, one cheap query per side. Only when a
bucket disagrees does stage two (`audit_ids`) pull both sides' primary keys
and name the individual rows. The normal answer to "did we lose anything" is
no, and `gap_span` narrows stage two to the buckets that actually disagree —
measured on a seven-hour window with one gap, that was 17.5 minutes of
expensive read instead of seven hours.

**It never repairs.** Findings land in `starcubes_meta.audit_run`, the
`audit_gap` pipeline alarm turns them into a notification, and the repair is
a gesture. That follows the same rule as preflight: detect and report,
repair only what is idempotent and finishes in milliseconds.

**The row is also the repair's input.** `etl_run --backfill --audit-run <id>`
reads `audit_run.missing_ids` and extracts exactly those rows, chunked at
1000 for Oracle's `IN`-list limit, instead of re-scanning the window.
Measured on a 37-row gap: **45 source rows read instead of 2439**. This is
why the two things are one feature — the id list is far too long for a CLI
argument, and a table to park it in was the missing piece for both.

Two refusals are deliberate:

- Past `STARCUBES_BACKFILL_IDS_MAX` the run is marked `ids_truncated` and the
  targeted backfill refuses, naming the window backfill instead. Repairing
  part of a gap and reporting success is worse than re-reading the window,
  which repairs all of it.
- An audit whose *every* bucket was unreadable is recorded `failed` with
  NULL counts, not `ok` with zero. `audit_window` answers one unreadable
  bucket with `None` so the other 23 survive; a run where all of them failed
  compared nothing, and recording that as "nothing is missing" would let the
  alarm close a standing episode on a run that never saw the source. Silence
  is not recovery.

### What the gap says about the lag

Stage three answers the question the gap raises: *why* were those rows
missing. A row is absent because it arrived after the high watermark had
already passed its watermark value, and `audit_id_details` measures exactly
that distance:

```
needed_lag_s = etl_run.last_end_time − the row's own watermark value
```

Both operands come from the source's watermark column. `etl_run.started_at`
must never enter that subtraction — it is the starcubes host clock, and on a
source writing in another zone the result goes negative.

**It proposes both knobs, by the same amount, at different prices.** With
`end_time_N = max_wm_N − watermark_lag_s`, raising that lag by Δ puts the
window's tip at `end_time_N − Δ`; for run N not to swallow the row it takes
**Δ ≥ needed_lag_s**. `late_arrival_overlap_s` takes the same number
(`needed_lag_s ≤ overlap` is its exact re-read criterion). What differs is
the cost: `watermark_lag_s` holds **every** row back, so the whole datasource
is that much less fresh; the overlap re-reads a trailing span instead and
leaves freshness alone. Which price to pay is an operator's decision, which
is why the `needed_lag_p99` alarm reports the raw seconds and carries the
ladder as a note rather than prescribing a setting.

Measured against ground truth on a fixture with deliberate lateness
(50 rows 30 s late, 40 rows 300 s, 10 rows 1200 s):

| `watermark_lag_s` | stragglers caught |
|---|---|
| 0 | 0 of 100 |
| 1199 | 90 of 100 |
| **1200** (the measured maximum) | **100 of 100** |

**The ladder reads forwards, not as percentiles.** `+30s → 50%, +5min →
90%, +60min → 100%` is the shape of the decision; "the 90th percentile is
300 s" is the same fact stated backwards.

**The sample is spread, not the head of the list.** `np.setdiff1d` returns
sorted, so `missing[:200]` is the 200 *lowest* primary keys — with the usual
monotonic key, the 200 oldest missing rows. On the same fixture the head
sample reports a maximum lateness of 30 s where the truth is 1200 s, a 40×
underestimate that would have recommended adding half a minute. The display
list still uses the head; only the statistic is spread.

### Repairing small gaps without asking

`audit_autofill_max` is the other half of the same trade-off. Keeping
`watermark_lag_s` low means accepting a few stragglers, and accepting them
should not mean a human presses a button for 37 rows. Up to that many
missing rows, a forever-mode audit repairs itself with the targeted backfill
and records `audit_run.autofilled`.

A cap rather than a switch, for two reasons. Past it a gap is not a handful
of stragglers but a symptom, and it deserves a person. And it costs heap
order: measured on a 500 000-row table, backfilling 37 rows moves
`pg_stats.correlation` from 1.0000 to 0.9997, while backfilling 10% of it
moves it to 0.8263 — and BRIN range exclusion rests on that number (see
[`bitmap-indexes.md`](bitmap-indexes.md)). A truncated id list is never
auto-repaired regardless of the cap.

**The de-dupe is narrowed, not dropped.** A targeted backfill asks which of
*those* ids are already in fact — against the PK index, not the watermark
range. Skipping it would be wrong: a regular load may have picked some up
between the audit and the repair, and there is no unique index on `fact`.

---

## The datasource lock

`core/preflight.py:acquire_datasource_lock` takes `pg_try_advisory_lock` keyed
on the datasource id, before the `etl_run` INSERT, and releases it after the
terminal UPDATE. A refusal raises `DatasourceLocked` and writes **no** run
row — a refusal is not a run, and in forever mode a failed row per iteration
would bury the history.

**The connection is the whole point.** Nothing else in `import_fact` holds
one across a run: every touch is a short `engine.begin()`, and the extract
loop disposes the source pool outright. A session-scoped lock on a pooled
connection would be silently worthless — the fork-safety hook invalidates
records across pids, `pool_recycle` drops the session after thirty minutes,
and a `pool_pre_ping` reconnect would carry on lockless without a word. So
the lock opens a dedicated `NullPool` connection and keeps exactly that one,
committing straight after acquisition (a session lock survives the commit; an
open transaction would pin the snapshot for hours).

Backfill serialises on the same key rather than getting its own — both paths
write the same dim tables, and auto-dim resolution assumes single occupancy.
`delete_fact_from`, `truncate_partition`, `drop_partition` and
`truncate_last_partition` take the same lock briefly, which is what closes the
hole the GUI's row-based 409 never covered: no maintenance view ever called
it.

A second, independent advisory lock
(`core/pipeline_alarm.py:pipeline_eval_lock`) keeps the three pipeline-alarm
evaluators from racing. Every key in use is listed in `core/pglock.py`, whose
dedicated connections also set TCP keepalives: a lock held from a host that
vanished is released in about a minute, not the kernel's two hours.

**The lock says who may write; the lease says who is alive.** The datasource
lock is held only while a load runs, so it cannot tell a forever loop sleeping
between runs from a dead one. That is the process lease (`core/lease.py`,
migration 0042): each managed process keeps one connection of its own for its
whole life and writes its `(pg_backend_pid, backend_start)` onto every run row
it owns. The row's process is alive exactly as long as that pair is in
`pg_stat_activity`, which every container sees alike — unlike a PID, which
means nothing outside its own namespace. It replaced both `os.kill(pid, 0)`
and the one-hour "stale run" age, which failed every load that legitimately
ran longer. A process whose lease connection breaks while it keeps running
(a network partition) is dead to everyone else, so it stops at the next chunk
boundary instead of writing on. Rows from before 0042 have no lease and keep
the old rules.

**A stop request travels on the row** (migration 0043), because a signal only
reaches a process in its own container. `control = 'term'` is read by the
process itself — the ETL's progress thread every 5 s, a rollup command at each
progress report, a forever loop every 5 s of its sleep — and ends it the way
SIGTERM does, at the next chunk boundary. A run blocked inside one long
source query reaches no boundary until the query returns; that is what
`control = 'kill'` is for: the owning container's `node_agent`, woken by
`NOTIFY starcubes_control`, sends SIGKILL after verifying the pid against the
lease, and closes the row itself.

**A forever session outlives its container** (migration 0044). There are two
ways a session becomes an orphan, and `sources/forever_adopt.py` handles both
the same way whether a starting container (`etl_resume_forever`) or a running
one (`node_agent`, on NOTIFY and once a minute) finds it:

- *Released.* On SIGTERM a container runs `manage.py node_drain` before it
  stops uwsgi: it stops its own `node_agent` (which would otherwise adopt the
  session straight back), then signals its runs. A forever loop finishes its
  current load and writes `released` instead of `stopped`, and another
  container adopts it within a second.
- *Dead.* A container killed outright leaves `running` rows whose lease has
  ended; they are adopted on the next minute tick, with the interrupted load
  failed.

`stopped` is never adopted — it is written by Stop Forever and by SIGINT from
a terminal, and an operator's Stop must not be undone by another container.
Two containers adopting at once serialise on a per-datasource transaction lock
and re-read the candidate inside it; the loser skips.

Two rules make adoption survive a partition, where the old process is still
running when its session is taken over. **A forever loop that meets
`DatasourceLocked` waits for its next iteration** instead of ending — the
holder is usually the old owner finishing the load it was in (a single run
still refuses and exits). **A loop's final status only lands on a row that is
still active**, so the old process leaving cannot overwrite the `failed` the
adopting container recorded.

---

## Deleting: repair, disposal, and the dedupe flag

Four operations remove fact rows. Which one you used decides what happens to
the watermark.

**Repair — `delete_fact_from(T)`.** The data is wrong; it will be loaded
again. So it:

1. deletes `WHERE partition_column >= T`;
2. **rewinds the watermark** to `T` by inserting a *new* `etl_run` row —
   never updating an old one, so the run history stays truthful, and
   `_resume_state` orders by `finished_at DESC`, so a fresh row wins even
   carrying a lower `last_end_time`;
3. arms `datasource.dedupe_from` where the two clocks make it necessary;
4. bumps the cache epoch.

A rewind below the retention floor is refused: routing disposal through the
repair gesture would have the ETL reload, at full throughput, exactly what
was just disposed of.

**Why `dedupe_from` exists.** A rewind reloads a window and there is no unique
index on `fact`. While `partition_column` and `watermark_fact_sql()` are the
same column, the reload is clean — the delete removed exactly what comes
back. When they differ (the example Oracle source watermarks on `end_time`
and partitions on `create_time`), rows below the boundary on one clock and
above it on the other survived the delete and *would* return as duplicates.
`dedupe_from` arms the existing `_overlap_pks` filter for that span, even with
`late_arrival_overlap_s` at 0, and the run that honours it clears the flag.
Without a `role='pk'` fact column nothing can match the rows, so that case
warns instead of pretending.

`import_fact` reads the flag fresh from the metadata rather than off the
`Source`, because a forever session loads its `Source` once, before the loop.

**Disposal — the three partition gestures and retention.** The watermark
stays put; `datasource.retention_floor` moves instead. The floor only
advances when the *oldest* partition goes: a partition removed from the
middle leaves a hole no single lower bound can describe, and that case warns
and leaves the floor alone.

All four bump the cache epoch, because they change data the plot cache
considers final.

---

## Retention

`datasource.retention_periods` keeps N partition periods; the housekeeping
drops what has aged out. Off by default and **irreversible** — fact
partitions are UNLOGGED and nothing backs them up.

Three limits, none of them about performance:

- **At most `STARCUBES_MAX_RETENTION_DROPS` (2) partitions per run.** The
  difference between `retention_periods = 12` and a mistyped `1` is then a
  mistake that walks forward visibly through the log and
  `housekeeping_run.report` over hours, rather than one irreversible pass.
- **Never more than half the existing partitions**, and never the one holding
  the newest data.
- **It stops when a load holds the datasource lock.** Retention does not take
  the lock itself — `drop_partition` already does, per partition, on its own
  connection, so holding it first would refuse the very drops it protects.
  Catching `DatasourceLocked` from the drop keeps whatever was already
  disposed of and defers the rest; the rest of the housekeeping runs either
  way.

Retention also **clears** the plot cache rather than only invalidating it.
The epoch bump makes stale rows unreachable without deleting them, and a
policy running every six hours would grow `query_cache` without bound.

### Four subsystems know about the floor

Because each of them otherwise assumes `fact` covers everything since the
beginning of time:

1. **The audit** (`core/audit.py:_clamp_to_floor`) raises every window's start
   to the floor; a window entirely below it is refused. Unclamped, the
   comparison reports 100 % loss for a window in which nothing was lost.
2. **The server-side plot cache** carries `cache_epoch` inside its key, so a
   bump makes every pre-drop key unreachable.
3. **The chart's in-page cache** holds 50 MB keyed on the query string alone.
   A window below the floor is "final, not trailing" — the most cacheable
   shape there is — so an open tab would keep painting dropped data. Every
   plot response carries the epoch and the page drops its cache when the
   value moves.
4. **Alarms.** The most dangerous one. The evaluator's window is
   `[hwm − window_s, hwm)` filtered on `partition_column`, the same clock the
   floor is written in. Truncating its older half does not fail — it returns
   a confident smaller number, and the error is not symmetric: `count(*) <`
   and `sum(col) <`, exactly what people write to detect an outage, report an
   outage that retention itself caused. `>` and `>=` merely miss breaches. So
   a window reaching below the floor is **not evaluated at all**, and the
   `alarm_windows` preflight check explains the resulting silence.

`api/source-meta` also clamps its advertised extent to the floor, or the chart
offers a range where fact is empty on purpose and a user reads it as loss.

---

## Budgets: run time and source statements

`datasource.max_run_s` (off by default) gives the parent a wall-clock budget.
Past it, `_RunBudget.check` raises `EtlAborted`, which the existing reaping
and `status='failed'` machinery already handles. It is checked between chunks
*and* inside `_raise_if_failed`, through which both bounded queue helpers
poll — so a run wedged on backpressure gives up as reliably as one that is
merely slow.

What a deadline in the parent **cannot** catch is a source that accepts the
connection and then never answers: the parent is blocked inside the DBAPI,
and `pool_pre_ping`/`pool_timeout` bound the checkout, not the execution. So
the same value becomes a per-dialect statement timeout on a separate source
engine — a libpq startup option for Postgres, a connect-event hook setting
`call_timeout` for Oracle, and a warning that statements are unbounded for a
dialect with no equivalent. One knob rather than two: a statement may not
outlive the run it belongs to.

Three run shapes are exempt, all legitimately long: a backfill, an explicit
`--start/--end` window, and the initial load.

**It is a circuit breaker, not a repair.** An aborted run leaves the watermark
where it was, so the next window starts in the same place and ends later — it
is *wider*, not smaller. What it buys is that one wedged run stops holding the
forever loop and lands as a failed row with a reason in it, instead of a
process that merely looks alive. To narrow the work, use `--limit` or
`initial_window_s`.

---

## Window width while catching up

An empty fact table makes `_resume_state` ignore the stored watermark, so the
first window is "everything the source has" — literally, with no lower bound
in the WHERE clause. On a large source that is one run of many hours or days,
and for its whole duration the forever loop does nothing else.

`datasource.initial_window_s` caps the width and the loop gets its turn
between the steps. Two things about the shape:

- It applies to **every step of the catch-up**, not only the literally first
  run. Capping step one alone would have step two load the entire remainder.
  A datasource that has caught up never reaches the cap, because its window
  is narrower anyway.
- It measures from the **high watermark**, not from `extract_from`. The
  late-arrival overlap re-reads ground that is already loaded, so capping from
  there would shrink each window by the overlap — and with a cap smaller than
  the overlap it would produce an `end_time` below the watermark, which the
  monotonicity clamp would push straight back to the tip.

Stepping the first load needs a lower bound to start from, so the engine asks
the source for `min(watermark_column)` — the one question it never had to ask
before, since every window after the first begins at the stored watermark.
Asked only when the cap is set and the fact table is empty.

---

## The two clocks a run records

Every run persists `etl_run.max_wm` (the data tip, `max(watermark_column)` as
read from the source) and `etl_run.src_now` (the source database's own wall
clock at that moment, read as UTC). Both come from the source, so their
difference carries no clock skew of its own.

`src_now` is read on **every** run, not only when `wallclock_lag_s` is set: it
rides along as a second column on the two generated query shapes and costs
nothing there; only a hand-written `watermark_max_query` pays one extra
trivial round trip. The window arithmetic is unchanged — the wall-clock term
is still gated on the lag being configured.

Their difference is the only thing in the system that can see **a source that
is up and has stopped producing**: a standstill freezes the watermark, so a
threshold alarm's trailing window stops sliding and can never fire. The
preflight compares it against `max_data_lag_s`, the ETL History sub-tab shows it
per run, and the `data_stale` pipeline alarm delivers it.

A **negative** value is not a small lag. A clock cannot be behind its own
data, so it means the watermark column does not hold UTC — which the rest of
the pipeline assumes it does. The window arithmetic already refuses the
wall-clock bound in that case; the report says why.

---

## Housekeeping

`manage.py etl_housekeeping`, triggered by the forever loop every
`housekeeping_interval_s` and runnable by hand or from the ETL tab.
One row per run in `starcubes_meta.housekeeping_run`.

| Step | What | Why it cannot wait for autovacuum |
|---|---|---|
| Retention | Drops aged-out partitions (above) | — |
| ANALYZE | The newest partitions that hold rows | The load is COPY-only: no dead tuples, so the scale factors are effectively unreachable on a large partition, and statistics rot on exactly the partition every chart reads |
| BRIN | `brin_summarize_new_values` on every fact-partition BRIN index | A BRIN index only covers summarised ranges, and summarisation happens during VACUUM. Freshly COPY'd blocks are **not covered by the index at all** — the index the whole storage design rests on silently stops applying to the newest data |
| Dimension fill | `max(index)` against the 32767 `smallserial` ceiling | At 100 % the next new value fails inside `_resolve_auto_dim_chunk`, in the parent, mid-extract |
| Partition sizes | `pg_total_relation_size` *and* `pg_relation_size` per partition | The Storage tab already shows one measurement; the series is what makes growth visible in advance |

Full `VACUUM ANALYZE` stays a manual action for after a large delete or
backfill — the targeted pair above addresses the actual problem for a
fraction of the I/O.

**The interval is on by default**, which breaks this schema's usual habit.
The lags all default to the old behaviour because the right value is
deployment-specific and must be measured; this one is different, because what
it fixes rots identically everywhere and is invisible by construction. An
operator cannot measure a BRIN index quietly not covering the newest data.
Off by default would have shipped a command nobody runs.

---

## Dimensions

`import_dimensions` runs on every `import_fact`. At a 60-second forever
interval that is a full lookup scan per regular dimension, sixty times an
hour, against the source, to discover values that arrive maybe weekly.

`datasource.dim_refresh_interval_s` throttles it, default 0 (every run).
Unlike the housekeeping interval, this default is not timidity: throttling
**costs** something visible. A new value in a *regular* dimension maps to
`__unknown__` until the next refresh, which is a wrong-looking series in the
chart for up to one interval. Auto-dimensions are unaffected — they resolve
during the extract and never depended on the periodic import.

`--import-dims` and the GUI button always import, and both reset
`dim_refreshed_at`.

---

## Alarms

Two kinds of rule, one episode machine, one notification path, one Firings
view. [`alarm.md`](alarm.md) is the deep reference; what belongs here is the
operational shape.

**Threshold alarms** ask a question about *data*: a measure over fact rows in
a trailing window, evaluated after every load at the fact clock, stamped with
the data high watermark.

**Pipeline alarms** ask about the *ETL*. Five conditions, each answering with
a number so `cond_op` and `threshold` decide the breach identically:

| Condition | Value |
|---|---|
| `data_stale` | seconds of `src_now − max_wm` |
| `run_failed` | failed runs since the last `ok` one |
| `no_recent_run` | seconds since the last `ok` run finished |
| `preflight_warn` | preflight findings at warn or block |
| `dim_full_pct` | fullest dimension, % of the 32767 ceiling |

They read `starcubes_meta` only — no `Source.load`, no source connection, no
fact query — which is what lets an evaluator outside the ETL judge a
datasource whose source database is down, one of the states it exists to
report. Their episodes are stamped with the **wall clock**: a threshold alarm
asks about data and must answer in the data's own time, while "how long has
nothing arrived" has no answer in data time at all.

A new datasource is created with two of them, `run_failed >= 1` and
`no_recent_run > 3600`, enabled and without channels — they record episodes
from the first load and send nothing until somebody picks recipients.

### Three evaluators

1. **The ETL loop**, after every load. Catches everything a run produces.
2. **The request path**, throttled, on a daemon thread — a dispatch can take
   a minute per transition (SMTP 10 s, XMPP 40 s, PagerDuty 10 s) and must
   not hold a page open. A worker recycled mid-send loses that one message;
   the episode row is already committed, so the state stays right.
3. **`manage.py pipeline_watch`**, its own process in the container.

Two and three exist because of a boundary: **a wedged loop cannot report that
it is wedged**, and a datasource whose ETL stopped produces no loads for an
in-loop evaluator to ride on. The request path alone is honestly weak —
notification would depend on somebody using the app, and an unattended
weekend is exactly when that fails and exactly when it matters. Hence the
process, with its price stated in `docker/entrypoint.sh`: nothing supervises
it, and if it dies nothing says so, exactly as for the `nginx &` beside it.

All three go through `pipeline_eval_lock`, so they cooperate instead of racing
to open one episode and counting the loser's `IntegrityError` as an error.

The alarm phase itself is bounded: `fact_max_time` is read once per
datasource per cycle rather than once per alarm, and both fact-side queries
carry `STARCUBES_ALARM_TIMEOUT_MS`. Unbounded there does not merely stall an
answer — the phase is synchronous inside the forever loop, so it holds back
the next load, which stops the watermark, which starves the evaluation that
would have noticed.

---

## Traps

Each of these cost a rewrite, or produced a feature that silently did
nothing. They are written down because the next person will otherwise
re-derive them.

- **An alarm column must not be called `kind`.** The transition dicts handed
  to the notification dispatcher already carry a `kind` meaning
  opened/closed, and the stamp loop copies alarm columns onto them wholesale.
  A column of that name turns every notification into a resolution — a bug
  you find by reading a mail, not by reading a test. Hence `rule_kind`.
- **"ANALYZE the newest partition" analyses an empty one.**
  `ensure_partitions_for` keeps a period of runway ahead, so the newest
  partition is normally in the future. "The partition covering *now*" is no
  better: the first real datasource this ran against had all 3.9 M of its rows
  four months back, because the source had stopped producing.
- **Partition size must be measured as heap, not total.**
  `pg_total_relation_size` counts index pages, so once `create_indices` has
  run, *every* partition looks populated — which put the first ANALYZE two
  weeks into the future.
- **An unreachable source and a rejected query raise identically** and need
  opposite answers: the first only warns (the extract reports it better a
  moment later), the second must block, because it is the drift the check
  exists for. Open the connection first, execute the query inside it.
- **A negative data lag is not a small lag.** It means the watermark column is
  not UTC.
- **An aborted run retries a *wider* window, not a smaller one.** The
  watermark did not move; the next window starts in the same place and ends
  later.
- **`= ANY(:ids)` needs a list.** psycopg2 adapts a list to an ARRAY and
  refuses a `set`. One collection in the notification dispatcher was a set,
  so every dispatch with a recipient failed inside the lookup and was
  reported as an error — e-mail and XMPP had never been delivered.
- **`None` must never close an episode.** "No run has happened yet" and "the
  condition cleared" are different answers; treating the first as the second
  reports a recovery that did not occur.
- **A width cap measured from `extract_from` is eaten by the monotonicity
  clamp** when it is smaller than the late-arrival overlap. Measure from the
  watermark.
- **Retention must not hold the datasource lock** it expects `drop_partition`
  to take: `datasource_lock` opens its own connection, so the outer hold
  refuses every drop. The first version skipped every run.
- **A cache epoch is not enough on its own** when a second cache keys on
  something else. The chart's in-page LRU keyed on the query string and never
  saw the epoch at all.

---

## Deliberately not done

- **Time-of-day profiles for quiet periods.** A calendar engine grows
  holidays, timezones and maintenance windows and never finishes. A
  per-datasource absolute threshold, left unset for batch sources, is the
  honest version.
- **Automatic index creation.** No `CONCURRENTLY`, ACCESS EXCLUSIVE on the
  parent, hours on a large fact table. Detect and report only.
- **Automatic `sync_schema` execute.** Its index rebuild makes even the
  "harmless" `add_columns` case unsafe to start unattended. The preflight
  reads the dry-run diff and never touches the execute half.
- **A DEFAULT partition** as a catch-all. Rejected for three independent
  reasons in `ensure_partitions_for`'s docstring; nothing here changes them.
- **A unique index on `fact`.** It would make every reload idempotent and
  solve the rewind problem outright, and it costs the `copy_from` fast path
  plus a btree on an unlogged billion-row table. Same answer as in
  [`etl-engine.md`](etl-engine.md): no.
- **An alarm outbox.** Notification is synchronous and bounded by per-channel
  timeouts. An outbox needs a deliverer, which is a second process question;
  the pipeline-alarm supervisor is deliberately not repurposed for it.

---

## Identifier index

The evaluation this document began as numbered seventeen concerns. They are
cited from source files, migrations and the other docs, so the numbers stay
resolvable here.

| # | The concern | Where it lives now |
|---|---|---|
| U1 | Watermark wrong after a delete | `core/etl.py:rewind_highwatermark`, `datasource.dedupe_from`, the `watermark_tip` preflight check |
| U2 | Schema objects missing | `preflight._check_schema_objects` — creates them |
| U3 | Partitions created ahead of the window | `core/schema.py:ensure_partitions_for`, on every load |
| U3b | Partitions retired | `datasource.retention_periods` — [Retention](#retention) |
| U4 | Dimension refresh too eager | `datasource.dim_refresh_interval_s` — [Dimensions](#dimensions) |
| U5 / U5a | Warehouse schema drift | `preflight._check_warehouse_drift`, dry run only |
| U5b | Source schema drift | `preflight._check_source_projection` |
| U6 | VACUUM / ANALYZE / BRIN summarisation | [Housekeeping](#housekeeping) |
| U7 | Wedged forever ETL | `datasource.max_run_s` plus the source statement timeout; and the three pipeline-alarm evaluators for the half that needs a supervisor |
| U8 | Alarm phase too slow | `fact_max_time` once per cycle, `STARCUBES_ALARM_TIMEOUT_MS` |
| U9 | Source reachable, data stopped | `etl_run.max_wm`/`src_now`, `max_data_lag_s`, the `data_stale` pipeline alarm |
| U10 | Indices never created | `preflight._check_indices` — reports, never builds |
| N11 | Two ETL processes at once | [The datasource lock](#the-datasource-lock) |
| N12 | Source clock skew | `etl_run.src_now`, the ETL History "Data lag" column, the negative-lag report |
| N13 | A dimension filling up | Housekeeping's fill measurement, the `dim_full_pct` pipeline alarm |
| N14 | Disk growth | Housekeeping's per-partition sizes, recorded as a series |
| N15 | Cache invalidation after retroactive changes | All four delete gestures bump the epoch; retention also clears the cache; the chart drops its own on an epoch change |
| N16 | Migrations not applied | `preflight._check_meta_migrations` |
| N17 | The unbounded first load | `datasource.initial_window_s` — [Window width](#window-width-while-catching-up) |
