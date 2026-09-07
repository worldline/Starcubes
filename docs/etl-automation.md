# ETL automation — preconditions, watchdogs, self-healing

What has to be true *around* a load for it to be correct, which of those
conditions the engine guarantees itself today, and which ones still hang on
somebody remembering. [`etl-engine.md`](etl-engine.md) describes how a run
works; this document is about everything that must hold before, between and
after runs.

**Read this before adding anything periodic**, and before wiring a new
maintenance action into the GUI or the CLI.

## Why this exists

The forever session is the only periodic engine in the system. There is no
cron, no systemd timer, no Celery — `docker/entrypoint.sh` runs
`bootstrap` → `etl_resume_forever` → nginx → uwsgi and nothing else. A
datasource in production is one `etl_run --forever N` process, tracked in
`starcubes_meta.etl_forever`, revived on container start.

That has a consequence worth stating plainly: **anything that must happen
regularly either happens inside a load or does not happen at all.** The
partition lifecycle already learned this the hard way — `ensure_partitions_for`
runs inline on every `import_fact` (`core/etl.py:1072`) precisely so that
partition coverage "never depends on a cron job again".

The failure modes collected here share a second property, which is what makes
them worth a document rather than a backlog line each: **they are silent.**
A missing index does not fail a run, it just makes every chart slow. A source
that stopped producing does not fail a run, it produces `rows=0` forever. A
watermark left standing after a delete does not fail a run — it quietly
guarantees that the deleted window is never reloaded. The engine's existing
defences (the window clamp, `EtlStageFailed`, the progress-writer's refusal to
stamp `progress_at` without real progress) all exist because someone found the
silent version of a bug first.

## Already solved — do not build these twice

| Concern | Status |
|---|---|
| Partitions created ahead of the window | ✅ `core/schema.py:ensure_partitions_for` gap-fills on **every** `import_fact` (`back=1, forward=1`, capped at 512). The `--create-partitions N` flag and its GUI button are redundant for the normal case. |
| Dimensions populated before a load | ✅ `import_dimensions` runs on every `import_fact` (`core/etl.py:873`, `refresh_dimensions=True` by default). If anything, too often — see U4. |
| `create_indices` re-runnable | ✅ Idempotent: FK existence is probed against `pg_constraint` (`core/schema.py:293`) and every index is `CREATE INDEX IF NOT EXISTS`. `docs/internals.md` claimed the opposite ("Step 4 is one-shot") until this document was written. |
| A killed run lands as `failed` | ✅ `EtlAborted` / `EtlStageFailed`, see [`etl-engine.md`](etl-engine.md). |
| Partition coverage for a backfill window | ✅ same `ensure_partitions_for` call, gap-filling backwards too. |

## Evaluation

Necessity = damage if nothing is done. Effort: S = a day, M = several days,
L = a project of its own. "Where" is one of the three places the logic may
live: **preflight** (inside `import_fact`), **housekeeping** (a separate
command the forever loop triggers on a long interval), **config** (a metadata
column plus GUI field).

| # | Scenario | Today | Necessity | Effort | Where | What the fix costs you |
|---|---|---|---|---|---|---|
| U1 | HWM wrong after a delete | `delete_fact_from` (`core/etl.py:1572`), `truncate_partition` (`:1534`), `drop_partition` (`:1552`), `truncate_last_partition` (`:1513`) never touch `etl_run.last_end_time` | **critical** | M | delete path + preflight | a reload without dedupe duplicates rows (no unique index on `fact`); a retention delete would be undone immediately |
| U2 | Schema objects missing | `create_fact`/`create_dim_tables` are idempotent but only called at creation time; a missing partition raises in `ensure_partitions_for` | medium | S | preflight | an ETL that runs DDL turns a typo in `schema_name` into a second empty schema instead of an error |
| U3 | Partitions created | ✅ solved | — | — | — | — |
| U3b | Partitions retired | missing entirely (backlog: "Partition retirement") | medium | M | housekeeping | irreversible; the Audit tab reports retired windows as gaps; the plot cache serves frozen emptiness |
| U4 | Dimensions populated | ✅ — arguably too eager: a full lookup scan against the source on *every* iteration | low (inverted) | S | config | throttling delays new dim values (absorbed by auto-dims and the `__unknown__` sentinel) |
| U5a | Warehouse schema drift | `sync_schema` produces a dry-run diff (`core/schema.py:340`) but is only ever called by hand | high | M | preflight (diagnosis only) | its execute phase is `drop_indices` → ALTER → `create_indices` — hours on a billion rows. Applying automatically is **not** acceptable |
| U5b | Source schema drift | unchecked (backlog); a renamed column fails mid-run | high | S | preflight | almost nothing: one `WHERE 1=0` round trip |
| U6 | VACUUM / ANALYZE | manual only (`core/schema.py:900`); insert-only load barely triggers autovacuum, so **BRIN ranges stay unsummarised** and statistics go stale | high | S | housekeeping | ANALYZE on the partitioned parent samples across every partition — target the newest partition instead |
| U7 | Wedged forever ETL | `progress_at` correctly stops advancing, but the stale sweep only runs **when somebody opens a GUI page** (`sources/views.py:842`, `:1261`); no timeout on the source connection | **critical** | M | preflight + a process decision | a watchdog inside the watched process is not a watchdog; a runtime cap must not shoot down a legitimate first load |
| U8 | Alarm phase too slow | real: `fact_max_time` runs **per alarm**, not per datasource (`core/alarm.py:363`); no `statement_timeout`; notification is synchronous at up to 10s SMTP + 40s XMPP + 10s PagerDuty **per transition** | high | S → M | ETL hook | an outbox needs a deliverer — the same process problem as U7 |
| U9 | Source reachable, data stopped | invisible: alarms cannot see it by construction (`docs/alarm.md`), the ETL logs `rows=0` and carries on | **critical** | M | pipeline alarm | false alarms during legitimate quiet periods; time-of-day profiles are a bottomless pit |
| U10 | Indices never created | nothing checks; the target list is right there in `source.indices` | medium-high | S | preflight (report only) | building them automatically is out: no `CONCURRENTLY` anywhere in the repo, ACCESS EXCLUSIVE, blocks COPY for hours |
| N11 | Two ETL processes at once | a CLI single run has **no** interlock; only the GUI (409) and forever mode (`ux_etl_forever_active`) are protected. Auto-dim resolution assumes single occupancy | high | S | preflight | an advisory lock is session-scoped — it needs a connection held for the whole run; backfill needs an explicit rule |
| N12 | Source clock skew | the clamp and its warning exist; the measured skew is computed per run but never stored | medium | S | falls out of U9 | none worth naming |
| N13 | A dimension fills up | `smallserial`, hard stop at 32767; a mis-configured auto-dim explodes mid-run | medium | S | housekeeping | none |
| N14 | Disk growth | unobserved; unlogged, no retention | medium | S | housekeeping | none |
| N15 | Cache after a retroactive change | backfill calls `bump_cache_epoch` (`core/etl.py:1504`); an HWM rewind (U1) and a retention drop (U3b) must too | medium | S | same functions | easy to forget — which is the reason to write it down |
| N16 | Migrations not applied | `bootstrap` only runs at container start | low | S | preflight | none |
| N17 | Unbounded first load | an empty fact table makes `_resume_state` ignore the HWM and load "from the beginning" — potentially days, blocking the forever loop | medium | M | config | chunking changes the window semantics the audit and the cache assume |

## Three principles the rest of this document follows

**1. The forever loop is the scheduler.** Without cron there is no second
engine, so everything periodic either goes into the load (preflight) or gets
triggered by the loop on a long interval (housekeeping). The one thing that
cannot live there is the supervision of the loop itself: a wedged process
cannot report that it is wedged. That is not an implementation detail to be
solved later, it is a boundary — see U7 for the two honest options.

**2. Deleting means two opposite things.** `--delete-from` is a *repair*
gesture: the data is wrong, throw it away, load it again. `drop-partition` /
`truncate-partition` are *disposal* gestures: reclaim the space, the data is
not coming back. An HWM rewind is exactly right for the first and exactly
wrong for the second. Any automation that treats them alike breaks one of
them. Disposal therefore gets a **retention floor** in the metadata — a lower
bound on what this datasource still holds, which the audit and the alarms
respect — instead of touching the watermark.

**3. Detecting and repairing are different decisions.** Catalogue queries are
free and change nothing: does the table exist, does the index exist, how full
is this dimension, do the source columns still resolve. Everything that takes
ACCESS EXCLUSIVE or moves rows is neither free nor safe to start unattended.
The rule this document applies throughout:

> **Preflight detects and blocks. It only repairs what is idempotent and
> finishes in milliseconds. Everything else is reported and waits for a
> human gesture.**

That is the same line `ensure_partitions_for` already draws: creating a
missing partition is in scope, a DEFAULT partition as a silent safety net is
deliberately out.

---

## U1 — The high watermark after a delete

**Today.** Four operations remove fact rows, and not one of them tells the
watermark:

| Operation | Effect | HWM |
|---|---|---|
| `delete_fact_from(from_time)` (`core/etl.py:1572`) | `DELETE … WHERE partition_column >= from_time` | untouched |
| `truncate_partition(name)` (`:1534`) | `TRUNCATE` one partition | untouched |
| `drop_partition(name)` (`:1552`) | DETACH + DROP | untouched |
| `truncate_last_partition()` (`:1513`) | truncates the *oldest* partition | untouched |

`_resume_state` reads `last_end_time` from the newest `status='ok'`,
`kind='incremental'` run (`core/etl.py:731`). The fact-side fallback only
applies when the fact table is empty *or* no `ok` row exists at all — and even
then it takes `max(watermark_fact_expr)` over the **last minute** of
`partition_column`, not a full scan. So after a partial delete the metadata
watermark simply stands, and the next run's window starts above the hole. The
deleted rows are never requested again. Nothing logs it, nothing shows it; the
Audit tab would find it, but only if a human runs it over exactly that window.

This is the same class of bug as the late-arrival hole the window bound was
built to close, and it deserves the same seriousness.

**Fix.** Give the repair gesture a watermark rewind, and give the disposal
gesture a retention floor.

- `delete_fact_from(T)` writes a rewind: a new `etl_run` row with
  `last_end_time = min(current_hwm, T)`, same mechanism as
  `set_highwatermark` (`core/etl.py:1605`) but with an explicit target rather
  than `max(partition_column)`. A new row, never an UPDATE of an old one —
  the run history stays truthful.
- `drop_partition` / `truncate_partition` / `truncate_last_partition` write
  `datasource.retention_floor` instead: "rows before this instant are gone on
  purpose". `core/audit.py` clamps its windows to it, so a retired month stops
  reading as a million-row gap.
- Both call `bump_cache_epoch` (see N15).
- Preflight adds the cheap detection for everything that bypassed the GUI —
  psql, a restore, a manual `DELETE`: compare `max(partition_column)` (BRIN,
  partition-pruned, cheap) against the stored HWM. A fact tip materially
  *below* the watermark means rows disappeared without the watermark
  following. Report it; block only on request.

**What it costs you.**

- **Duplicates.** A rewind reloads a window, and there is no unique index on
  `fact` to absorb it. As long as `partition_column == watermark_column` the
  reload is clean — the delete removed exactly what will be re-inserted. When
  they differ (the example Oracle source watermarks on `end_time` but
  partitions on `create_time`), rows with `create_time < T ≤ end_time` were
  *not* deleted and *will* be reloaded. The engine already owns the answer —
  `_overlap_pks` + the per-chunk PK filter (`core/etl.py:660`) — but that path
  arms itself only when `late_arrival_overlap_s > 0` or the previous run
  ended badly. A rewind needs to arm it explicitly for the affected span,
  which means a flag the next run reads and clears. Without it, a rewind on a
  source with a derived watermark trades a silent hole for silent duplicates.
- **The two gestures must never be confused.** If retention ever routes
  through `delete_fact_from`, the rewind makes the ETL reload everything the
  operator just deleted — on the next iteration, at full throughput. Worth a
  guard: refuse a rewind that would move the HWM below the retention floor.
- **`_MAX_OVERLAP_PKS`** (5M) caps how wide a deduplicated reload can be. A
  rewind over a month is not a dedupable operation, it is a backfill.

## U2 — Schema objects missing

**Today.** `create_fact` and `create_dim_tables` are fully idempotent
(`CREATE … IF NOT EXISTS` throughout) but are only invoked when a datasource
is created or edited. `import_fact` assumes they ran. The nearest thing to a
check is `ensure_partitions_for`, which raises `"{schema}.fact has no
partitions — run create_fact first"` (`core/schema.py:862`). A dropped dim
table surfaces later and worse: as a foreign-key violation or a KeyError deep
in the transform.

**Fix.** A preflight that probes the catalogue for `{schema}.fact` and every
primary dimension's `dim_table`, and calls the existing idempotent creators
for whatever is missing. When everything exists — the overwhelmingly common
case — the cost is one `pg_class` query.

**What it costs you.** An ETL that creates schema objects on demand loses the
ability to complain about a *wrong* configuration. Rename `schema_name` by
accident and today you get a loud failure; with auto-create you get a second,
empty schema and a run that loads zero rows into it. Two mitigations, both
cheap: log every creation at WARNING (creating a fact table mid-life is never
routine), and record it in `etl_run.stats` so the GUI can show that this run
had to repair its own schema.

## U3b — Retiring partitions

**Today.** Forward coverage is automatic, backward disposal is not: old
partitions are never dropped or archived. `etl/drop-partition/` and
`etl/truncate-partition/` exist as manual GUI actions. Fact partitions are
`UNLOGGED`, so the storage is cheap to write and completely unbounded.

**Fix.** `datasource.retention_periods` (keep N periods), enforced by the
housekeeping command: list partitions, drop everything whose upper bound is
older than the floor, write `retention_floor`, bump the cache epoch.

**What it costs you.** Irreversibility, and three second-order effects that
all trace back to the same root — *other parts of the system assume fact
covers everything since the beginning of time*:

- `core/audit.py` compares source counts against fact counts. Below the
  retention floor that comparison reports 100% loss. It has to clamp.
- The plot cache holds frozen responses for ranges it considers final. A drop
  invalidates them with no event unless one is raised.
- Alarms with a long `window_s` that reaches below the floor silently evaluate
  a truncated window.

Retention is therefore not "a drop loop"; it is a floor concept that three
subsystems have to learn about. That is why it is M, not S.

## U4 — Dimension refresh is too eager, not too lazy

**Today.** `import_dimensions` runs on every `import_fact`. At a 60-second
forever interval that is a full `SELECT source_key, source_name,
source_description FROM <lookup>` per regular dimension, 60 times an hour,
against the source database — to discover changes that arrive maybe weekly.

**Fix.** `datasource.dim_refresh_interval_s` (0 = every run, the current
behaviour). The last refresh timestamp goes in the metadata; `import_fact`
skips the import when the interval has not elapsed. `--import-dims` and the
GUI button always force it.

**What it costs you.** A new value in a *regular* dimension is now mapped to
`__unknown__` until the next refresh instead of on the next run. That is a
visible, wrong-looking series in the chart for up to one interval. Auto-dims
are unaffected — they resolve during the extract (`_resolve_auto_dim_chunk`)
and never depended on the periodic import. Keep the default at 0 so nobody
inherits the trade-off without choosing it.

## U5 — Schema drift, on both sides

### U5a — Warehouse side

**Today.** `sync_schema` (`core/schema.py:340`) already does the hard part: it
reads `pg_attribute` for the real column types, diffs them against
`source.fact_columns`, and returns
`{add_columns, alter_columns, unsafe_type_changes, drop_columns, create_dims,
drop_dims}` without changing anything. It is only ever called from the GUI.

**Fix.** Call the **dry-run half** from preflight. A non-empty
`unsafe_type_changes` or `drop_columns` blocks the run with a message naming
the columns; `add_columns` warns. Both land in `etl_run.stats`.

**What it costs you.** Almost nothing — *provided the execute half stays out
of the preflight*. That is the trap here, and it is worth being explicit
about: `sync_schema`'s execute phase runs `drop_indices` → ALTER →
`create_indices`. On a billion-row fact table that is hours of ACCESS
EXCLUSIVE, started unattended, inside what the operator thinks is a routine
60-second load. Even the "harmless" `add_columns` case goes through that
index rebuild today, so it is not eligible for automation either — despite
`ADD COLUMN … NULL` itself being metadata-only since PG 11. If automatic
schema repair is ever wanted, the prerequisite is an `add_columns` path that
does not touch indices; until then, preflight reports and stops.

### U5b — Source side

**Today.** Nothing validates that the upstream columns behind `source_expr` /
`base_table` still exist. A renamed column fails in the middle of the extract
loop, after the run row is written and the workers are up. This is an open
backlog item.

**Fix.** Before the worker pool starts, run `source.import_query` wrapped as
`SELECT * FROM (<query>) WHERE 1=0` and compare the returned column names
against the expected projection. One round trip, no rows, no plan cost worth
measuring, and it converts a mid-run crash into a refusal with a column name
in it.

**What it costs you.** Essentially nothing. The only subtlety is that a source
whose `import_query` is expensive to *plan* (a large view, an Oracle query
with hints) pays that planning cost twice per run. Measure it once on the real
source; if it matters, gate it behind the same interval mechanism as U4.

## U6 — VACUUM, ANALYZE and BRIN summarisation

**Today.** `vacuum_analyze_fact` (`core/schema.py:900`) exists and is reachable
from `--vacuum-analyze` and a GUI button. Nothing calls it on a schedule.

The interesting part is *why* relying on autovacuum is not enough here, and it
is not the usual reason. The load is COPY-only: rows are inserted, never
updated, never deleted. Dead tuples barely accumulate, so
`autovacuum_vacuum_scale_factor` is rarely reached — and
`autovacuum_analyze_scale_factor` on a partition that already holds hundreds
of millions of rows needs an enormous absolute number of new rows to trigger.
Two things degrade quietly as a result:

- **Planner statistics** go stale on the partition currently being written —
  exactly the one every chart query touches.
- **BRIN ranges are not summarised.** A BRIN index only covers page ranges
  that have been summarised, and summarisation happens during VACUUM (or via
  an explicit call). Freshly COPY'd blocks are therefore *not covered by the
  BRIN index* until something summarises them. The index the whole storage
  design rests on silently stops applying to the newest data — the data the
  chart is looking at most of the time.

**Fix.** In the housekeeping command, and deliberately narrower than
`VACUUM ANALYZE fact`:

- `ANALYZE {schema}.{newest_partition}` — targeted, so the sample does not
  span every partition.
- `SELECT brin_summarize_new_values('{schema}.ix_<time_col>')` for each BRIN
  index — far cheaper than a VACUUM and it addresses the actual problem.

Full `VACUUM ANALYZE` stays a manual action for after a large delete or
backfill.

**What it costs you.** ANALYZE takes SHARE UPDATE EXCLUSIVE, which does not
conflict with COPY, so correctness is not at risk — but it does cost I/O on a
box that is already streaming rows. Run it on the housekeeping cadence
(hours), not per load. `brin_summarize_new_values` takes only a row-level
lock and is safe alongside an active load.

## U7 — Detecting a wedged forever ETL

**Today.** The building blocks are in place and the wiring is not:

- `progress_at` only advances when rows actually moved (`core/etl.py:1196`) —
  written specifically so that a wedged pipeline stops looking alive.
- `sources/views.py:842` marks `running` rows older than an hour as failed.
- `sources/views.py:1261` reports `stale=True` when the forever session's
  progress is older than 3× its interval.
- `etl_resume_forever` heals orphaned sessions with dead PIDs at container
  start.

Every one of those runs **only when someone opens a page** (or restarts the
container). Overnight, a wedged loop is indistinguishable from a healthy one
until a human looks. And there is no timeout anywhere on the source
connection: `_get_cached_engine` sets `pool_pre_ping` and `pool_timeout=30`
for *checkout*, not for statement execution. A source that accepts the
connection and then never answers blocks the extract indefinitely.

**Fix, in two independent halves.**

The half that works without a supervisor — make the run bound its own time:

- `statement_timeout` (or the dialect equivalent) on the source connection for
  the max-watermark query and the extract, sized generously.
- `datasource.max_run_s`: the parent raises `EtlAborted` past that budget. The
  existing `except`/`finally` reaps the workers and writes `status='failed'`,
  the forever loop continues, and the next iteration retries a smaller window.
  Must be off for `--backfill` and for the first load (see N17), or it turns a
  legitimate long run into an infinite retry loop.

The half that needs a supervisor — noticing that the loop stopped making
progress at all. **This cannot be solved inside the process being watched.**
The two honest options:

1. **A third process in the container.** `docker/entrypoint.sh` already
   starts nginx and uwsgi; a small Python loop that sweeps `etl_run` /
   `etl_forever` and raises pipeline alarms (U9) is maybe fifty lines. It is
   the only option that keeps working when nobody is using the app. The cost
   is a process to supervise, restart and reason about — the very thing the
   architecture has avoided so far.
2. **Evaluate in the request path.** Move the stale sweep out of the two view
   functions into middleware with a throttle, so *any* request performs it.
   Cheap, no new process, fits the existing shape — and honestly weak:
   notification depends on somebody using the app. An unattended weekend is
   exactly when it fails, and exactly when it matters.

There is no third option that avoids the trade-off; a document that pretends
otherwise would be worse than one that names it. Recommendation: option 2
first because it is nearly free and strictly better than today, with option 1
as the follow-up once pipeline alarms exist and there is something worth
delivering promptly.

**What it costs you.** A runtime cap that is too tight kills healthy long
runs, and because the loop retries, it does so repeatedly. Default it to off
and let operators set it from observed run durations — the same discipline
`late_arrival_overlap_s` follows: measured, not guessed.

## U8 — Can the alarm phase delay the ETL? Yes.

**Today.** `run_alarm_cycle` runs synchronously after every `import_fact`
(`sources/management/commands/etl_run.py:323`), inside its own try/except so
it cannot colour the run's outcome. Three measurable costs:

- **`fact_max_time` runs per alarm, not per datasource** (`core/alarm.py:363`).
  Twenty alarms on one datasource issue twenty identical
  `SELECT max(watermark_fact_sql) FROM fact` queries per load. When the
  watermark is a derived expression there is no BRIN and no partition pruning
  for it — the exact case `docs/etl-audit.md` flags as expensive.
- **No `statement_timeout` on the aggregate query.** The chart's equivalent
  query has one; the alarm evaluator, which builds the same SQL through the
  same `_build_where`, does not. A pathological filter blocks the ETL loop
  with no upper bound.
- **Notification is synchronous and slow by design.** Per transition:
  SMTP `timeout=10`, XMPP `asyncio.wait_for(..., 30)` wrapped in an outer
  `30 + 10 = 40`, PagerDuty `urlopen(timeout=10)`
  (`sources/alarm_notify.py:45`, `:186`, `:230`). Worst case ≈ 60 s **per
  transition**. Five episodes opening at once — which is what a real incident
  looks like — is five minutes inside a loop configured for 60-second
  iterations.

The last one has a feedback loop worth spelling out: the delay holds back the
next load, the HWM stops advancing, and alarm evaluation (which is HWM-driven)
falls further behind precisely while an incident is unfolding.

**Fix, cheapest first.**

1. Compute `fact_max` **once per datasource** in `run_alarm_cycle` and pass it
   into `evaluate_alarm`. The cycle already groups alarms by datasource
   (`core/alarm.py:500`), so this is a parameter, not a redesign. N queries
   become 1.
2. `statement_timeout` on the alarm aggregate, reusing the console's existing
   mechanism.
3. A **time budget for the whole alarm phase**. When it is exhausted, stop and
   return. The remaining alarms keep their old `eval_end`, so the next load
   picks them up — the existing HWM-skip semantics already make a deferred
   evaluation correct rather than lost.
4. Same for dispatch: a budget, with anything beyond it written to an outbox
   table and sent later.

**What it costs you.** Steps 1–3 cost nothing conceptually; step 3 makes
"which alarms were evaluated this load" non-deterministic under load, which is
a real semantic change and needs to be logged per run. Step 4 reintroduces
the U7 problem — an outbox needs a deliverer, and the only candidates are the
next ETL iteration (fine, mostly) or the third process. Do not build an outbox
before that decision is made.

## U9 — Source reachable, data stopped

**Today.** Nothing sees it. `docs/alarm.md` states the limitation outright: a
total standstill freezes the HWM, so the trailing window stops sliding and
`count(*) < 1` never becomes true. The ETL logs `rows=0` and sleeps. The load
worker, the source connection, the schema — all healthy. This is the failure
mode most likely to go unnoticed for a full day, and it is the one an operator
would most want to hear about within minutes.

**Fix.** Watch the *pipeline*, not the data. Every run already computes both
numbers it needs: `max(watermark)` and the source wall clock `src_now`, read
together on the same query (`core/etl.py:896-1013`). Two things are missing:

- **Persist them.** `etl_run.max_wm` and `etl_run.src_now`. `docs/etl-audit.md`
  already wishes for the first ("recording `max_wm` per run would
  disambiguate that at the price of a column"). One column, two consumers.
- **A threshold.** `datasource.max_data_lag_s`: when
  `src_now − max_wm` exceeds it, the source has stopped producing. Note this
  is a source-internal comparison — both operands come from the source's own
  clock, the discipline `docs/etl-audit.md` insists on for `needed_lag_s`.

**What it costs you.** False alarms during legitimate quiet periods — nights,
weekends, a batch source that produces twice a day. The temptation is
time-of-day profiles; resist it. A per-datasource absolute threshold,
documented as "for sources with continuous traffic, leave unset otherwise", is
honest and finishes. Profiles are a calendar engine in disguise: holidays,
timezones, maintenance windows, and an ever-growing table of exceptions.

The second cost is delivery. There is no mechanism today for "the pipeline
has a problem, tell somebody" — which is what the next section is for.

## U10 — Indices never created

**Today.** The first load deliberately runs without indices (fastest COPY),
and the operator is supposed to run `create_indices` afterwards. Nothing
checks that they did. The symptom is not an error: charts are slow, the audit
is slow, and because it was always like that, nobody connects it to a
forgotten step. The inverse case is just as real — `drop_indices` before a
bulk reload, then forgetting to rebuild.

**Fix.** A preflight comparison of the expected set (`source.indices` plus one
partial btree per dimension column, exactly what `create_indices` builds)
against `pg_index`, and the FK set against `pg_constraint`. One catalogue
query. Report; never build.

Make it *time-aware*, because that is what the real complaint is: "the data
has been syncing near real-time for a while and the indices are still not
there". Suppress the warning while the fact table is small or the datasource
is young, and escalate once N rows have been loaded across M hours without
indices. That distinguishes the legitimate initial-load state from a forgotten
step, without asking the operator to acknowledge anything.

**What it costs you.** Nothing on the detection side. The discipline is on the
other side: **never make it self-repairing.** No `CONCURRENTLY` exists anywhere
in this repo, `create_indices` takes ACCESS EXCLUSIVE on the partitioned
parent, and on a billion-row unlogged fact table an unattended index build
would block every COPY for hours while looking, from the outside, exactly like
U7.

## N11 — Two ETL processes on one datasource

**Today.** Forever mode is protected by `ux_etl_forever_active` (one active
session per datasource) and the GUI refuses a second run with a 409
(`_etl_busy_response`). A plain `manage.py etl_run --name x` from a shell has
**no interlock at all** and will happily run alongside a live forever session.

That is not a theoretical concern. Auto-dimension resolution states single
occupancy as a precondition — "Concurrency assumption: one ETL process per
datasource (enforced for forever-mode by `ux_etl_forever_active`)"
([`etl-engine.md`](etl-engine.md)) — because two parents inserting into the
same dim table race on the `smallserial`. Add overlapping windows with no
unique index on `fact`, and a debugging session run by hand can silently
duplicate rows in production.

**Fix.** A Postgres advisory lock keyed on the datasource id, taken for the
whole run: `pg_try_advisory_lock`, refuse and exit if it is held. It is
cross-process, cross-host and released automatically when the connection dies
— no PID file, no stale state to reap.

**What it costs you.** An advisory lock is session-scoped, so the run must
hold one connection open for its entire duration, outside the pool's normal
churn. And backfill needs an explicit rule: a backfill writes behind the HWM
and does not conflict with an incremental load in the same way, so either it
takes a separate key or it is deliberately serialised too. Serialising is the
safer default — both paths write the same dim tables.

## N12 — Source clock skew

The window bound already clamps and warns when `src_now` runs ahead of the
data. Once `etl_run.max_wm` and `src_now` are stored (U9), a persistent skew
parked near a whole-hour offset while rows keep arriving becomes visible as a
trend rather than a log line nobody read — the exact diagnosis
[`etl-engine.md`](etl-engine.md) describes by hand. Free once U9 lands.

## N13 — A dimension filling up

Dim tables are keyed `smallserial`: 32767 values, hard. An auto-dimension
pointed at something high-cardinality (an id column mistaken for a category)
climbs towards that ceiling and then fails inside `_resolve_auto_dim_chunk`,
in the parent, mid-extract. Housekeeping reports fill level per dim table and
warns past ~80%. One `count(*)` per dim table, on tables of at most 32767
rows.

## N14 — Disk growth

Fact partitions are UNLOGGED and unbounded. `pg_total_relation_size` per
partition, recorded by housekeeping over time, turns "the disk filled up
overnight" into a trend somebody can see coming. It also gives U3b the
evidence base to size a retention policy instead of guessing one.

## N15 — Cache invalidation after retroactive changes

`is_cacheable` shortens the cacheable horizon by `late_arrival_overlap_s`, and
backfill calls `bump_cache_epoch` (`core/etl.py:1504`) because it writes
behind the HWM. An HWM rewind (U1) and a retention drop (U3b) do exactly the
same thing — they change data the cache considers final — and must bump the
epoch too. Small, easy to forget, listed here so the next person implementing
either one finds it.

## N16 — Migrations not applied

`ensure_starcubes_meta` is idempotent and runs from `bootstrap` at container
start. A code update without a restart, or a deployment that skips bootstrap,
leaves the metadata schema behind the code — surfacing as an
`UndefinedColumn` from an unrelated query. A preflight probe for one
recently-added column is cheap insurance. Low priority: the deployment path
makes this rare.

## N17 — The unbounded first load

`_resume_state` treats an empty fact table as an initial load and ignores the
stored watermark, so the first window is "everything the source has". On a
large source that is a single run of many hours or days, during which the
forever loop does nothing else — no alarm evaluation, no housekeeping, and
(with U7's runtime cap naively applied) an infinite abort-retry loop.

A `datasource.initial_window_s` that walks the first load forward in bounded
steps would fix it, but it changes the meaning of a run window, which the
audit and the cache both build on. Left as a config-level item to be designed
with those two in mind, not as part of the preflight work.

---

## The three building blocks

### `core/preflight.py`

Django-free (like everything in `core/`), returning a structured report rather
than raising: one entry per check with `ok` / `warn` / `block`, a message, and
a machine-readable code. Called from `import_fact` before
`ensure_partitions_for`; a `block` aborts the run before the worker pool
starts, exactly where the existing overlap guards abort.

Checks, in cost order: schema objects (U2), migration probe (N16), advisory
lock (N11), index/FK coverage (U10), warehouse drift dry-run (U5a), source
projection probe (U5b), watermark-vs-fact-tip sanity (U1), data staleness
(U9). Every one is a catalogue query or a single round trip.

The report goes into `etl_run.stats` — already `jsonb`, already surfaced by
the GUI — so no new table is needed to display it.

### `manage.py etl_housekeeping`

Everything too expensive for the load path: targeted ANALYZE plus BRIN
summarisation (U6), dim fill levels (N13), partition sizes (N14), optional
retention (U3b). Triggered by the forever loop on a long interval
(`datasource.housekeeping_interval_s`, hours) and runnable by hand. It takes
the same advisory lock discipline as the ETL where it touches partitions.

### Pipeline alarms

The largest piece, and the one that makes U7, U9, U10, N12, N13 and N14
*deliverable* rather than merely detected. The alarm subsystem already owns
everything hard about notification: channels, per-user opt-in, PagerDuty
severity and dedup keys, and — most importantly — the episode semantics in
`alarm_firing` (one row per incident, opened and closed, at most one open per
rule). A pipeline condition has exactly that shape: it starts, it persists, it
ends.

What it is not is a `measure`/`threshold` over fact rows. A second alarm kind,
sharing `alarm_firing`, the notification path and the GUI's Firings view, but
whose condition is evaluated over pipeline state (`etl_run`, `etl_forever`,
the preflight report) instead of over the fact table. It is deliberately
scoped *after* the cheap preflight work: the checks produce the signal, and
this turns the signal into a message.

## Recommended order

1. **Free wins, no new concepts** — U8 step 1 (`fact_max` once per
   datasource), U8 step 2 (`statement_timeout`), U5b (source projection
   probe), U10 (index detection), U2 (schema preflight), N16. Together this is
   `core/preflight.py` plus a two-line change in `core/alarm.py`.
2. **N11** (advisory lock) — small, and it removes a way to corrupt data by
   hand.
3. **U1** (HWM rewind + retention floor) — the highest-necessity item, but it
   needs the dedupe-arming decision made first, so it follows the groundwork.
4. **U6 + N13 + N14** — the housekeeping command, small and self-contained.
5. **U9 + N12** (`max_wm`/`src_now` columns + threshold) and **U7 half one**
   (statement timeout, runtime cap).
6. **Pipeline alarms**, and with them the process decision from U7.
7. **U3b** (retention) and **N17** (first-load chunking) last — both change
   assumptions other subsystems hold.

## Deliberately not

- **Time-of-day profiles for quiet periods.** A calendar engine grows
  holidays, timezones and maintenance windows and never finishes. A
  per-datasource absolute threshold, left unset for batch sources, is the
  honest version.
- **Automatic index creation.** No `CONCURRENTLY`, ACCESS EXCLUSIVE on the
  parent, hours on a large fact table. Detect and report only.
- **Automatic `sync_schema` execute.** Its index rebuild makes even the
  "harmless" `add_columns` case unsafe to start unattended.
- **A DEFAULT partition** as a catch-all. Already rejected for three
  independent reasons in `ensure_partitions_for`'s docstring; nothing here
  changes them.
- **An external scheduler.** The forever loop plus a housekeeping interval
  covers the periodic work. The one thing it cannot cover — supervising
  itself — is a process question (U7), not a scheduling one.
- **A unique index on `fact`.** It would make every reload idempotent and
  solve U1 outright, and it costs the `copy_from` fast path plus a btree on an
  unlogged billion-row table. Same answer as in
  [`etl-engine.md`](etl-engine.md): no.
