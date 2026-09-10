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

**A window reaching below the retention floor is not evaluated at all.**
Below the floor the partitions were disposed of on purpose, so the aggregate
silently measures a shorter span than the rule asked for — and the error is
not symmetric. `count(*) <` and `sum(col) <`, which is exactly what people
write to detect an outage, would report an outage that retention itself
caused; `>` and `>=` merely miss breaches. The evaluator returns `None`, the
same "not answerable" this codebase uses for the pipeline conditions: no
window counted, no episode opened, and a standing episode deliberately left
open rather than closed on missing evidence. The `alarm_windows` preflight
check names the affected rules so the silence is explained.

**Trade-offs (accepted):**

- A load that fails or is delayed takes its evaluation with it. The window
  slides with the HWM instead of being chained to the previous one, so a
  breach living entirely between two loads is never seen.
- Same philosophy as the audit: rows arriving late into a window the HWM has
  already slid past are never re-evaluated.
- A total data standstill is invisible *to a threshold alarm*: the HWM
  stops, the window stops sliding, and `count(*) < 1` never comes true.
  That is structural and cannot be fixed here — but it is no longer
  unwatched. **Pipeline alarms** (below) evaluate over ETL state instead of
  fact rows and see exactly this case.

## Who evaluates

The ETL does, and nothing else. `sources/management/commands/etl_run.py`
calls `run_alarm_cycle(meta, dsn, datasource_id=source.id, source=source)`
after every `import_fact`, inside its own `try/except` so an alarm problem
can never colour the ETL's outcome. It runs after a caught ETL failure and
under `--backfill` too — the HWM skip decides what actually gets evaluated,
and the cost of a skipped evaluation is now one `max()` per *cycle*.

Two things keep that cost from leaning on the ETL loop, both from U8 in
[`etl-automation.md`](etl-automation.md):

- **The fact clock is read once per datasource, not once per alarm.**
  `run_alarm_cycle` already groups the alarms by datasource; `fact_max_time`
  runs there and the value is passed into every `evaluate_alarm` on that
  datasource. Twenty alarms used to mean twenty identical
  `max(watermark_fact_sql)` scans per load, and with a derived watermark
  expression there is neither a BRIN nor partition pruning behind that max.
  `evaluate_alarm` still reads it itself when no caller passes one — hence
  the `_UNSET` sentinel, since `None` already means "the fact table is
  empty".
- **Both fact-side queries carry a `statement_timeout`**
  (`STARCUBES_ALARM_TIMEOUT_MS`, default 30 s), through the same
  `SELECT set_config('statement_timeout', :ms, true)` form `core/audit.py`
  uses. The chart's equivalent query — same SQL, same `_build_where` — has
  had one all along. Unbounded here does not merely stall an answer: the
  alarm phase is synchronous inside the forever loop, so it holds back the
  next load, which stops the HWM, which starves the evaluation that would
  have noticed.

An alarm *phase* budget (U8 step 3) and a notification outbox (step 4) are
still open; the outbox waits on the process decision in U7.

**The limitation this evaluator cannot fix itself** — a total standstill
freezes the HWM, so the trailing window stops sliding and `count(*) < 1`
never becomes true — is now at least *detected* elsewhere:
`etl_run.max_wm` / `src_now` and `datasource.max_data_lag_s` see a source
that is up and has stopped producing, and the preflight reports it (U9 in
[`etl-automation.md`](etl-automation.md)). Turning that detection into a
message is the pipeline-alarm work, which will share `alarm_firing` and the
notification path with this module but evaluate over pipeline state rather
than over fact rows.

There is deliberately **no forever loop**. A load is the only thing that
moves the HWM, so it is the only moment an alarm's answer can have changed;
a second, independently timed evaluator would race the ETL for the same
episodes and re-ask questions whose data had not moved. `alarm_run --forever`,
`alarm_resume_forever` and the `alarm_forever` session table are all gone
(`0029_alarm_hwm_semantics.sql`).


## Pipeline alarms — the second kind

A threshold alarm asks a question about *data*. A pipeline alarm asks one
about the *ETL*: has the source stopped producing, are runs failing, is any
run completing at all, is the preflight unhappy, is a dimension filling up.
Everything else it shares — `alarm_firing` and its episode semantics, the
notification path, the recipients, the severity, the Firings view.

It is deliberately not a new subsystem. The alarm machinery already owned
everything hard about not being seen, and a pipeline condition has the same
shape as a threshold breach: it starts, it persists, it ends.

### The rows

`alarm.rule_kind` is `'threshold'` or `'pipeline'`; `alarm.condition` names
the condition for the latter and is NULL for the former, both enforced by
CHECK constraints (migration 0036). A pipeline row uses `cond_op` and
`threshold` exactly as a threshold row does, and ignores `measure`,
`window_s` and `context_filter`, which keep their defaults.

**The column is `rule_kind` and not `kind` for a reason worth remembering.**
The transition dicts handed to `sources/alarm_notify.py` already carry a key
called `kind`, whose values are `opened` and `closed`, and the stamp loop
copies alarm columns onto those dicts wholesale. An alarm column named
`kind` would have overwritten it and turned every notification into a
resolution — a bug you find by reading a mail, not by reading a test.

### The five conditions

Each answers with a *number*, which is what let `cond_op`, `threshold` and
the whole episode machine be reused rather than reimplemented.

| condition | value | reads |
|---|---|---|
| `data_stale` | seconds of `src_now − max_wm` | newest `etl_run` with both (U9) |
| `run_failed` | failed runs since the last `ok` | `etl_run` |
| `no_recent_run` | seconds since the last `ok` finished | `etl_run` (U7) |
| `preflight_warn` | findings at warn or block | newest `etl_run.stats.preflight` |
| `dim_full_pct` | fullest dimension, % of 32767 | newest `housekeeping_run.report` (N13) |

`None` means *not answerable yet* — no run has happened, no housekeeping
report exists — and is treated as "no breach we can assert". It deliberately
does **not** close a standing episode: silence is not recovery.

All five read `starcubes_meta` only. No `Source.load`, no source connection,
no fact query — which is the property that lets an evaluator outside the ETL
judge a datasource whose source database is down, one of the states it
exists to report.

### The clock, and why there is no cursor

Threshold episodes are stamped with the fact HWM. Pipeline episodes are
stamped with the **wall clock**, and that is not a compromise: a threshold
alarm asks about data and must answer in the data's own time, while "how
long has nothing arrived" has no answer in data time at all.

For the same reason the `eval_end` skip — "the HWM did not move, so the
verdict would be identical" — does not apply: a pipeline condition changes
without any new data. `eval_end` is still written, informationally. No
replay cursor is needed either, because the episode machine is idempotent
(a second evaluation extends rather than re-opens) and `ux_alarm_firing_open`
allows one open episode per rule.

### Who evaluates, and the boundary behind it

Three evaluators, and they are layers rather than alternatives:

1. **The ETL loop**, after every load — `run_alarm_cycle` covers both kinds.
   Catches everything a run produces.
2. **The request path** — `starcubes/middleware.py:PipelineAlarmMiddleware`,
   throttled by a process-local timestamp and then by the advisory lock, with
   the work on a daemon thread so a dispatch (up to a minute per transition)
   never holds a page open.
3. **A process of its own** — `manage.py pipeline_watch`, started by
   `docker/entrypoint.sh`.

Two and three exist because of a boundary, not an oversight: **a wedged loop
cannot report that it is wedged**, and a datasource whose ETL stopped
produces no loads for an in-loop evaluator to ride on. `docs/etl-automation.md`
(U7) sets out the trade-off; both options were taken, and the price of the
third is written into the entrypoint — nothing supervises it, exactly as
nothing supervises nginx there.

A non-blocking advisory lock (`core.pipeline_alarm.pipeline_eval_lock`) keeps
the three from racing to open one episode and counting the loser's
`IntegrityError` as an error.

### Notification: unchanged

`sources/alarm_notify.py` needed no change to carry a second kind of alarm.
It subscripts `measure` to render the condition, so the pipeline evaluator
puts a readable label with its unit in that key:

```
[starcubes] OPENED: payments / no data — source data lag (s) > 3600 (value 14,400)
```

The PagerDuty dedup key stays `starcubes-alarm-<alarm_id>#<episode_id>` and
cannot collide, because both kinds live in one table with one id sequence.

### Defaults

A new datasource is created with two pipeline alarms — `run_failed >= 1` and
`no_recent_run > 3600` — enabled and **without channels**, from
`sources/profile_store.py:insert_default_pipeline_alarms`, shared by the CLI
seed and the GUI create form. They open and close episodes in the Firings
tab from the first load, and send nothing until somebody picks recipients.


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

## Notification channels

An episode's open/close transitions are announced by the evaluator
(`core/alarm.py` returns transition dicts in its counts, produced in the
same transaction that wrote the episode row) and dispatched by
`sources/alarm_notify.py` — a Django-side module, because recipient
resolution reads `starcubes_meta.auth_user`, a Django-managed table
`core/` must never touch. The dispatch runs right after the cycle in
`etl_run` / `alarm_run`, in its own try/except: a notification failure
neither colours the ETL's exit status nor suppresses the other channels.

`alarm.notify_channels` is the channel switch each alarm selects; the
channels are:

| Channel | Address source | Enabled when |
|---|---|---|
| `email` | the selected accounts' `auth_user.email` | `STARCUBES_SMTP_HOST` + `STARCUBES_SMTP_FROM` set |
| `xmpp` | the selected accounts' `auth_user.xmpp_jid` | `STARCUBES_XMPP_JID` + `STARCUBES_XMPP_PASSWORD` set |
| `pagerduty` | the datasource's `pagerduty_routing_key` | per datasource, no global switch |

The **log side is always on** and not selectable: every episode is written
to `alarm_firing` and logged by the evaluator (`core/alarm.py`) regardless
of `notify_channels`. It used to be a channel in its own right; legacy rows
may still carry `log` in the stored array — `_parse_alarm_fields` drops it
silently on the next edit.

**Recipients are the login users.** Each alarm picks recipients per channel
from the accounts; the addresses live on the accounts themselves (e-mail on
`auth_user.email`, XMPP on the new `auth_user.xmpp_jid`, maintained on the
settings page). There is deliberately **no free-form recipient list** — the
accepted limitation is that no external or distribution-list recipients are
possible. `int[]` cannot carry a foreign key, so a deleted user's id is
scrubbed from both arrays by `settings_page`'s delete branch.

**Receiving is the user's decision (migration 0032, default off).** Being
listed on an alarm is the alarm author's side of the contract; the account
itself must also have opted in to the channel — `auth_user.notify_email` /
`notify_xmpp`, maintained on **My Profile**. Dispatch drops a listed account
that has not opted in (logged as WARNING, so a quiet alarm is diagnosable);
PagerDuty has no user preference. Because the default is off,
existing alarms fall silent for users who never opt in — deliberate.

The save-time rules (enforced in `_validate_alarm_notify`): `email`/`xmpp`
channel ⇒ at least one recipient of that kind chosen; `pagerduty` channel
⇒ the datasource has a routing key. The inline row editor sends channels
only — recipients keep their stored value, and enabling a channel with
nothing stored is refused.

PagerDuty speaks Events API v2: **trigger** on open, **resolve** on close,
dedup key `starcubes-alarm-<alarm_id>#<episode_id>` — per episode, so a
re-opened episode produces a fresh incident instead of being silently
deduplicated into the resolved one. **Severity is per alarm**
(`alarm.severity`, migration 0032): the four PagerDuty values
critical/error/warning/info, NULL/empty falls back to the global
`STARCUBES_PAGERDUTY_SEVERITY`. The Transports page's test button always
sends the global value — it exercises the transport wiring, not an alarm's
routing choice.

The messages (subject/body, shared by e-mail, XMPP and the PagerDuty
summary) are rendered by `sources/alarm_notify.py:render_message` — English,
like every other user-visible string. A channel selected but not configured
(unset env vars, a user without an address, a missing routing key) is
skipped with a WARNING and counted as an error; it never fails the ETL.
`slack` stays reserved vocabulary without a transport.
## Export and import

Alarms are the one curated configuration an installation keeps only in its
database, so they get a backup file of their own:

```
manage.py alarm_export --name payments --out alarms.yaml
manage.py alarm_export --all --out alarms-all.yaml     # stdout without --out
manage.py alarm_import alarms.yaml                     # dry run
manage.py alarm_import alarms.yaml --confirm
```

The same two operations sit on the Alarms tab as a **Backup** card
(`↓ Export alarms` / `↑ Import alarms…`), and the import there is
two-phase: the file is posted once without `confirm` to produce the plan
the operator reads, then once with it.

### Why this is not part of the source profile

A profile describes what a datasource *is* and applies to an empty schema.
An alarm is operating state *over* a datasource that already exists: it
hangs off `datasource_id`, names dimension values by id, and names its
recipients as `auth_user` ids — none of which exist when a profile is
rolled out.

The mechanical reason is sharper. `profile_apply` is a **replace**: it
deletes every child row in `sources/profile_store.py:_CHILD_TABLES` and
re-inserts the profile's. An `alarm` entry there would make every Apply
Profile take `alarm_history` and `alarm_firing` with it through
`ON DELETE CASCADE`. That list is deliberately left alone.

### What travels

Configuration only — `name`, `rule_kind`, `condition`, `measure`,
`cond_op`, `threshold`, `window_s`, `context_filter`, `comment`,
`enabled`, `severity`, `notify_channels` and the two recipient lists.

Not `id`, not `datasource_id` (the datasource travels as its **name**, in
the header), not `created_at`/`updated_at`, and above all not `eval_end`,
for the same reason the history snapshots leave it out: it is the
evaluator's runtime high-watermark, not something a user configured.
`alarm_history` and `alarm_firing` do not travel either — an upsert leaves
the target's own intact.

A pipeline rule is written without `measure`, `window_s` and
`context_filter`. Those columns carry defaults it never reads, and
exporting them would make the row indistinguishable from a threshold rule
that had been mis-saved.

`log` is dropped on the way out. It stopped being a selectable channel
long ago (the episode row and the evaluator's log line are always on);
legacy rows still store it, and carrying it would export a channel nobody
can choose.

### The file

```yaml
format: starcubes-alarms/1
exported_at: 2026-09-10T12:00:00Z
datasources:
  - datasource: payments
    alarms:
      - name: Peer 9999 stalled
        rule_kind: threshold
        measure: count(*)
        cond_op: "<"
        threshold: 1
        window_s: 300
        enabled: true
        severity: critical
        notify_channels: [email, pagerduty]
        notify_email_recipients: [alice, bob]
        notify_xmpp_recipients: []
        context_filter:
          - {col: peer_id, op: in, vals: ["9999"], andOr: and, indent: 0}
        context_labels:
          peer_id: {"9999": "acme-gw-1"}
```

Unlike a profile, the file carries an explicit `format:` header. It has no
catalogue filename to be recognised by, so it says what it is and an
upload of the wrong YAML is refused rather than half-parsed.

`context_filter` is copied **verbatim** — it is exactly the flat filter
rows `chart.html:gatherParams()` serialises and the jsonb column already
stores.

`context_labels` is the one derived block, and it is documentation plus a
check, never configuration: the context stores dimension **ids**, and a
`dim_index` is an import position rather than a stable identifier — the
caveat [`docs/source-profiles.md`](source-profiles.md) records for
`group_dims.members` applies here word for word. The importer re-resolves
the ids and **warns** when the name no longer matches; it never writes the
labels back.

### Import semantics

**Upsert on `(datasource_id, name)`, never a replace.** A rule already
present is updated and keeps its `eval_end`, its history and its firing
episodes; a rule the file does not name is left alone. Replacing would
cascade the history away, and a restore that destroys the operating record
is not a restore. Upsert is also the only thing that works on a fresh
datasource, which already carries the two default pipeline alarms from
`insert_default_pipeline_alarms`: a file naming them updates them instead
of colliding with the unique index.

Only added and changed rules are written, so a second import moves no
`updated_at` and writes no `alarm_history` row. Every write still records
its snapshot with the importing account's username, exactly like a form
save.

**A malformed file writes nothing at all**, `--confirm` or not: a bad
`format:` header, a `window_s` of 0, a `<col>:grouped:<slug>` filter
column. The grouped-token ban is checked without loading the Source,
precisely so the tolerant branch below cannot let one through.

**A rule that merely does not fit this installation is skipped, not
fatal**, and named in the report with its reason:

| situation | what happens |
|---|---|
| recipient username has no account | dropped, warned; the channel goes too if it loses its last recipient |
| `pagerduty` selected, datasource has no routing key | channel dropped, warned |
| measure or filter column not in the target schema | rule skipped, warned |
| the datasource cannot be `Source.load`ed yet | context left unverified, warned, rule imported |
| stored rule is a different `rule_kind` | rule skipped — an alarm cannot change its kind |
| exported `context_labels` name no longer matches | warned; ids are imported unchanged |

The asymmetry is deliberate. What is wrong about *the file* refuses
everything; what is wrong about *this installation* is reported and worked
around. Refusing a whole backup because one rule references a column
dropped since it was taken would make it unrestorable exactly when it is
needed — and a freshly rebuilt installation whose source is not connected
yet is the ordinary case for a restore, not the exception.

The CLI import is a dry run by default and prints the same plan the GUI
renders: `+` created, `~` changed (with the fields), `=` untouched, `!`
skipped.

### Files

| File | Role |
|------|------|
| `core/alarm_io.py` | the file format: `dump_alarms`, `parse_alarms_text`, the size cap |
| `sources/alarm_store.py` | reading, username resolution, the diff and the upsert |
| `sources/alarm_fields.py` | the field validation, shared with the CRUD — one truth about a valid alarm |
| `sources/management/commands/alarm_export.py` / `alarm_import.py` | the CLI |
| `sources/templates/sources/_alarms_panel.html` | the Backup card |
