# Log files as a source

The counterpart of [`etl-engine.md`](etl-engine.md) for the second kind of
datasource. Read it when editing `core/logsource.py`,
`sources/management/commands/check_logsource.py`, the log branches in
`core/etl.py` / `core/preflight.py` / `core/source.py`, or the Log Source card
in `sources/templates/sources/_logfile_card.html` /
`_logfile_js.html`.

A datasource with `source_dialect = 'logfile'` has no query, no cursor and no
`WHERE` clause. What it has is a regular expression whose **named groups** are
the fact columns, and a pile of files. Everything downstream of that —
transform, `COPY`, auto-dimensions, partitions, rollups, alarms, the chart —
is untouched and never learns that log files exist.

## Why this exists

A great deal of operationally interesting timeseries data never reaches a
database: access logs, HAProxy logs, syslog. Nobody wants to stand up an ETL
into Postgres first just to ask *how many of these, by that, filtered on this,
over the last month?* — which is the one question this app is for.

The claim that made it worth building is narrower than "read log files": the
configuration is **easier** than a SQL source's, not merely possible. A SQL
datasource has no schema introspection at all — its fact columns come from a
profile or are typed in one by one. A log can be *read*, so the app offers the
columns instead of asking for them. See "The probe" below.

## Where the seam is

`core/etl.py:import_fact` produces `chunks` as an iterable of
`pandas.DataFrame`s and hands them to the transform queue. That boundary was
already there — `_targeted_chunks` (the audit's backfill) proved an
alternative producer leaves the rest of the pipeline alone — so a log source
is a **third branch** at that one place:

```python
if source.is_logfile:
    chunks = log_reader.iter_chunks(extract_from, end_time, source.chunksize,
                                    skip_at_start=skip_at_start)
elif backfill_ids is not None:
    chunks = _targeted_chunks(...)
else:
    chunks = pd.read_sql(text(query + where), src_conn, ...)
```

The DataFrame's columns are `Source.fact_column_order()` and its time column
is tz-aware UTC, exactly what `parse_dates={pc: {"utc": True}}` produces. Six
further places branch on `Source.is_logfile`: the source engine (there is
none), the watermark probe, `min(watermark)`, the small-window hint, the
`WHERE` clause and `source_engine.dispose()`. `import_dimensions` needed
nothing — its source-DB branch already skips a null engine, so inline and
auto-dimensions carry a log source by themselves.

## The transports

`source_dsn` carries the transport rather than a column of its own. Reusing
that field is what keeps DSN masking, the connection test and the whole
profile machinery working unchanged.

| Scheme | Lists | mtime | Tail | Notes |
|---|---|---|---|---|
| `file://` | glob | `stat` | `seek` | A path or glob in this container. Covers a bind mount, a host-mounted NFS/CIFS share, and a sidecar sharing a volume. |
| `sftp://` | `listdir_attr` + fnmatch | `st_mtime` | `seek` | The log on the machine that wrote it. Needs `paramiko`. Host keys **verified**. |
| `https://`, `http://` | one resource, **no glob** | `Last-Modified` | suffix `Range` | A URL names one thing, so there are no generations. |
| `exec://` | one stream | — | — | A command's stdout. **Off** unless the deployer allows it. |

`.gz`, `.bz2`, `.xz` and `.lzma` are unpacked on the fly, which is what makes
the history reach past the current generation: logrotate's usual shape is
`access.log`, `access.log.1`, `access.log.2.gz`, and without the `.gz` a
window can only ever go back a day or two.

Generations are read **oldest mtime first**, ties broken by name, so the order
is deterministic — which the crash-resume de-duplication below depends on.

### The mtime prefilter

A file whose mtime sits before the window start is not opened at all. The
premise is that no line carries a timestamp later than its own file's mtime:
true for anything that appends as events happen, and true for a
rotated-then-compressed generation, whose mtime is the compression time. The
premise gets an hour of slack (`STARCUBES_LOG_MTIME_SKEW_S`) because skipping
wrongly is silent data loss while reading one file too many only costs time —
enough to absorb clock skew between a remote writer and the host holding the
file.

### `streaming`, and what it costs

`file://` and `sftp://` give the reader all three things it wants: a list of
generations, an mtime per generation, and a tail it can read without reading
the file. `https://` gets the last two from `Last-Modified` and a suffix
`Range` (and declines a `200` answer to a range request — treating the whole
body as a tail would mis-parse its first line). Where a server offers neither,
the resource is re-read per run, which is worth knowing before pointing this
at a gigabyte.

`exec://` has none of them, and is marked `streaming = True`. Two consequences:

- **The tip is the reader's clock**, not a measured data tip, because
  measuring one would mean reading the whole stream on every run. So the
  window claims up to now, and a line written into it while the run works
  falls below the watermark and is never loaded. That is precisely what
  `watermark_lag_s` / `wallclock_lag_s` cover, so the answer is to use them
  rather than to invent a second mechanism — and `preflight`'s `log_source`
  check **warns** when a streaming transport has both at 0.
- **`min(watermark)` is declined.** Reading a whole stream to find its oldest
  line is the one thing `initial_window_s` exists to avoid, so the first
  window stays unbounded below there.

### `exec://` in detail

```
exec:///usr/bin/journalctl -u app --since {from} --until {until} -o short-iso
```

`{from}` and `{until}` are substituted with ISO-8601 UTC, and they are what
make this transport efficient rather than merely possible: the **source** does
the windowing, so an incremental run asks journald for two minutes instead of
a week. Without them the whole stream is read every run and filtered here. An
absent lower bound (the initial load) becomes `1970-01-01T00:00:00Z`, which
every tool taking a `--since` reads as "everything".

The command is always an argv list (`shlex.split`), never a shell string — no
globbing, no pipes, no `;`. A non-zero exit **raises**, with the tail of
stderr: a wrong unit name must not look like an empty log.

## The pattern

Every fact column's `source_expr` holds the name of a named group instead of a
SQL expression. Same column, same role, a different extraction vocabulary.

Three rules worth knowing:

**`log_line_regex` is compiled with `re.MULTILINE`.** A pattern here describes
a log *line*, so an author writing `^` and `$` means line boundaries — and for
a single-line record that is what they get either way. It starts to matter
once `log_record_start` joins a stack trace onto the record before it: without
the flag `$` anchors at the end of the whole joined text, which `.*` can never
reach because it does not cross a newline, and the record silently fails to
match. Measured: three well-formed records became two rows and one unparsed
line. An operator who wants to capture across the newlines writes `(?s)` in
their own pattern.

**`log_record_start` is optional and usually empty.** A line matching it
begins a new record; one matching neither it nor `log_line_regex` is appended
to the record before it. That is how a multi-line stack trace stays a single
row instead of N unparsed lines. Every access-log format leaves it blank.

**A group name is not automatically a column name.** `user` is reserved in
PostgreSQL, and column names are interpolated **unquoted** into the warehouse
DDL and every query built around them — the same bargain
`core/schema.py:validate_schema_name` strikes, and rejected as a quoting
problem for the same reason ([`backlog.md`](backlog.md): ~70 consistently
unquoted f-string sites). nginx's combined format has a `user` field, so this
is not hypothetical: `create_fact` failed with *syntax error at or near user*
long after the datasource looked configured.
`core/logsource.py:safe_column_name` derives a usable name (`user_`) while
`source_expr` keeps the group, which is what the two separate fields were
always for — nothing in the regex has to change.
`sources/views.py:_check_fact_column_name` enforces the same rule on the
inline editor for **every** source kind, since a hand-typed `user` breaks a
SQL datasource identically.

## The value transform

`fact_column.transform_pattern` / `transform_replace` are a `re.sub` applied
to the captured value **after** the match and **before** the type cast. That
order is the whole design, and it lets one mechanism do two jobs that both
turn out to be routine rather than exotic:

- **Collapsing an unbounded value** so it can be a dimension at all. A dim
  table's key is `smallserial` — 32767 values, hard — and an HTTP path with
  coordinates in it (a proxy in front of map tiles) reaches that in minutes.
  `^(/[^/?]*).*$` → `\1` keeps the first segment and drops the query string.
- **Mapping a format's "no value" onto a number.** nginx writes `-` for the
  response size when there was no body; HAProxy writes `-1` for a timing
  phase it never reached. Without a transform every such line fails its cast
  and lands in `etl_unparsed`; with the `-1` left in place it drags every
  `avg` and `min` with it.

A transform applied after the cast could do neither. Two columns rather than
one `s/…/…/` string because paths contain slashes and every delimiter choice
eventually collides with the data.

## Time

One timestamp is interpreted in exactly one place,
`core/logsource.py:_parse_time`. That is the file-side counterpart of
`core/source.py:_pin_session_utc`: a SQL source gets its session pinned to
UTC, a file has no session, so the zone is configuration and the conversion is
code. The reason it is one place rather than three is that a naive value
carries no evidence about its zone, so a mistake here is invisible afterwards.

| Field | Meaning |
|---|---|
| `log_time_format` | `strptime`. Empty = ISO-8601 auto-detected. `@epoch`, `@epoch_ms`, `@epoch_us`, `@epoch_ns` for numeric stamps. |
| `log_time_zone` | IANA name, applied only when the value carries no offset. Empty = UTC. |

Precedence, deliberately: an offset in the line (`%z`) always wins — the line
knows better than the configuration. Otherwise `log_time_zone`.

**Nothing can detect a wrong `log_time_zone`.** A log written in local time
and read as UTC is silently off by the offset, and no probe can see it. Set it
to the zone the *logging host* runs in.

A format without a year (syslog RFC 3164) takes the year from the file's
mtime, and then walks it back if the result sits more than a day ahead of that
file — a line reading `Dec 31 23:59:00` in a file last written on `Jan 02`
belongs to the *previous* year, and taking the mtime's year unconditionally
dates it eleven months into the future.

The ambiguous hour of a DST fall-back resolves to the first pass (`fold=0`).
That is not a correctness claim: a naive local timestamp in that hour is
genuinely ambiguous and no parser can fix it. The point is that the behaviour
is defined rather than accidental.

### Sub-second precision, and why `_ceil_second` is skipped

`core/etl.py:_ceil_second` rounds a watermark with a sub-second part up to the
next whole second. `Source.ceil_watermark` is the one seam every call site
goes through, and it **passes through for a log source**.

Two things about the rounding are easy to misread, and one of them cost a
wrong diagnosis while planning this feature.

It opens **no gap**. `max_wm = end_time` is assigned *after* the rounding, so
the clamp `if end_time > max_wm` compares against the already-rounded value
and cannot pull it back. The stored watermark *is* the rounded bound and the
next window starts exactly there, so windows chain seamlessly whether or not
the source keeps sub-second times.

What it does do is let the window **claim up to a second past the tip it
measured**. On a source whose time index has second granularity that sliver is
empty by construction — which is the argument in the docstring, and it holds.
On a sub-second source it is not empty: a row written into it after the probe
read the tip falls below the stored watermark and is never loaded. For a log
at 1000 lines/s and a run lasting seconds to minutes that is up to ~345 lines
per run.

A log source has nothing to gain from the claim — its reader filters in Python
at full precision, so there is no second-granularity index to spare — and
everything to lose, so it is skipped. The same exposure exists for a **SQL**
source with a `timestamp(3)` watermark, where `late_arrival_overlap_s` is
exactly the cover; `preflight._check_watermark_tz` warns when a sub-second
watermark has it at 0.

## The watermark is a timestamp, not a position

The natural cursor into a file is `(file, byte offset)`, and it would be
faster. But `etl_run.last_end_time` is a `timestamptz`, and backfill,
`--start` / `--end`, retention, rewind/repair, the rollups, the alarms and the
gap overlay all read it. So the reader takes a window `[from, until)` and
filters on the parsed timestamp: that costs a scan of the files the window
touches and keeps every one of those features working unchanged. The mtime
prefilter is what makes it affordable.

### There is no primary key, and none is needed

A log source must not have a `role='pk'` fact column; `Source.load` refuses
one. Log lines carry no stable key — and the window arithmetic alone is exact,
because `last_end_time` is the **upper**, half-open bound, so the row carrying
that exact timestamp was never loaded and `[last_end, until)` joins on with
nothing read twice. That holds because a log file is kept in timestamp order.

Consequences, all deliberate:

- `late_arrival_overlap_s` must be 0. The overlap re-fetch de-duplicates by
  primary key; `Source.load` and the edit form both refuse a non-zero value.
- The chart's drill-down transaction list is empty.
  `core/query.py:list_transactions` returns `[], []` without a PK on its own.
- `delete_fact_from` warns and skips arming `dedupe_from`.
- The **audit is switched off** — see below.

### The upper bound is exclusive, so the newest line waits

`end_time` is the measured tip and the window is half-open, so the line *at*
the tip stays pending until the log grows. On a growing log that is one round
of latency. On a static or archived log the last timestamp never loads, and
`manage.py etl_run --end <past the tip>` picks it up.

This is the safe direction — the window never claims what it did not read —
and it is why the ceiling is dropped here rather than kept for its
side effect of making the tip loadable.

### The crash resume

One path does need help. After an interrupted run there is no `ok` row to read
a watermark from, so `_resume_state` rebuilds it as `max(ts)` **in fact** — and
that timestamp belongs to a row that *was* loaded, unlike a stored
`last_end_time`. A `[max(ts), until)` window therefore hands back every record
sharing it.

Solved without a key: count the fact rows carrying exactly that timestamp and
have the reader skip that many (`skip_at_start`). Exact, because the read order
is deterministic — generations by mtime, lines in file order — and a log is
kept in timestamp order. `date_trunc('second', …)` is dropped from that query
for a log source too; rounding down would re-read the whole truncated second
with nothing to drop it with.

If fewer records are found than fact holds on that timestamp, a WARNING says
so: it means the file was rewritten rather than appended to, and the resume
could not be made exact.

## What is switched off, and why

**The audit.** `audit_interval_s` is ignored, the ETL tab's Audit sub-tab
explains itself instead of offering buttons, and the `audit_gap` alarm answers
`None` — never zero, so it stays quiet rather than asserting health. Both of
the audit's questions need something a log file cannot give. The bucket sweep
asks the source to *count* rows per time bucket, which here means a second
full scan of every file the window touches — the same work as the load itself,
for a comparison against the load that just ran. The exact half asks which
primary keys are missing, and there are none.

What watches a log source instead: the **Unparsed** tab (lines the pattern
stopped matching), `log_min_match_pct` (which fails a run outright), and the
pipeline alarms on `data_stale` and `run_failed`.

**`small_window_hint`**, `watermark_max_query`, `base_table`, `import_query`
and `watermark_fact_expr` stay NULL. `pk_format` has nothing to format.
`estimate_cardinality` answers with a sentence naming the probe, which
measures the same thing for every group at once and does it before a single
row is loaded.

## The probe and the column generator

This is what stands in for the schema introspection a SQL source never got.
`core/logsource.py:sample()` reads a little of the log, applies the pattern,
and answers with the match rate, every named group's example values, its
distinct count, a guessed type and a proposed role, plus the lines that did
not match. The GUI renders that as a table of checkboxes; one button writes
the ticked groups as fact columns and their dimension rows, in one
transaction.

It is bounded three ways — lines, bytes **and** wall clock — because it runs
inside a web request, where uwsgi's `harakiri = 120` kills the worker rather
than the request. That is the same constraint that keeps
`rollup_advise`/`rollup_build` out of a request; the probe is allowed because
it reads a tail, not a file.

Five of its judgements were each wrong first and are worth leaving alone:

- **A dimension is proposed as `smallint`**, whatever the value looks like: it
  holds the dim table's surrogate index, not the source value. Proposing
  `text` produced a text dimension column that loaded fine and then failed the
  dim-count UPDATE with `operator does not exist: smallint = text`, after 699
  rows were already in.
- **A measure is never proposed as `smallint`**, even when the whole sample
  fits one. The sample's maximum is a lower bound on the real maximum, and
  `_transform_worker` *clips* an out-of-range measure instead of failing — so
  guessing narrow is silent corruption rather than a tight fit. A 1 kB
  response body in a 300-line sample says nothing about next week's 100 kB
  one.
- **A numeric group is a dimension when it repeats.** Few distinct values
  *and* a small share of the lines read; both tests, because the share alone
  calls a 400-distinct field over 10 000 lines a dimension and a fixed cap
  alone misreads a small sample. Nobody sums status codes — they group by
  them, and only a dimension can be grouped by.
- **Null tokens are skipped and counted**, not treated as text. One `-` in
  fifty nginx lines was enough to turn `bytes` from a measure into a
  high-cardinality *dimension*; now it stays a measure and the pane reports
  how many values need a transform.
- **`distinct_after` is computed over the full distinct set**, not over the 50
  stored examples, or the pane reports "295 → 50 after transform" for a
  transform that collapses nothing.

A group whose distinct count approaches the line count is flagged and left
**unticked**, with the 32767 ceiling named and a transform suggested. That is a
recommendation rather than a lock: a group with 487 distinct values in 500
lines may still converge on 200, and the operator can tick it.

### The cardinality net

Independently of the generator, `core/etl.py:AUTO_DIM_MAX`
(`STARCUBES_AUTO_DIM_MAX`, default 30000) stops `_resolve_auto_dim_chunk`
inserting below the 32767 wall. Hitting the wall is not a graceful failure:
the INSERT raises mid-chunk inside a load that has already `COPY`ed rows, and
the run dies with a sequence error naming nothing actionable.

Capped values map to **`__unknown__`**, never to the `-1` sentinel. The two
mean different things and conflating them is the kind of wrong that looks
right: `-1` says the source had no value, `__unknown__` says it had one we
could not map. Measured on the first run of the cap — 623 requests that all
carried a path were filed under "no path".

The net is **not log-specific**: a SQL auto-dimension pointed at something
unbounded has always been able to do this. Log sources merely make it likely.

## Unparsed lines and the match threshold

`starcubes_meta.etl_unparsed` is `etl_unmapped`'s exact counterpart: that
table holds the dimension values a run could not resolve, this one the lines
it could not parse. Capped per run (`STARCUBES_UNPARSED_MAX`) and the raw line
truncated — the purpose is to show *which shape* stopped matching, not to
mirror the log into the warehouse. The aggregate figures live in
`etl_run.stats.extract`: `parsed_rows`, `unparsed_rows`, `match_pct`,
`files_read`.

**An unparsed line is dated by its neighbour.** It has no timestamp of its
own, so the window filter cannot reach it — and counting it regardless is
wrong in a way that bites immediately: a quiet incremental pass re-read the
two malformed lines in a file, reported `0 parsed, 2 unparsed (0.00% match)`,
appended them to `etl_unparsed` again, and with `log_min_match_pct` set would
have failed every run that legitimately had nothing to do. A log is kept in
timestamp order, so the last record parsed before it dates it well enough.
Where nothing has been parsed yet in a file the line cannot be placed and is
**not** counted: refusing a run over a line we cannot date would be worse than
missing it.

`log_min_match_pct` refuses a run **only while nothing has been loaded yet** —
before the first chunk is handed to a worker, or after a loop that produced
none. Failing later would be worse than the condition it reports: the terminal
UPDATE writes `last_end_time` only for a run that ended `ok`, so a failed run
leaves the watermark behind and the next one re-reads the same window, which
with no primary key means duplicates. A rate that only falls later is a
WARNING plus `stats.match_pct` plus the samples; a visibly recorded low rate
beats silent duplication.

## Preflight

Two checks run for a log source, and they sit *beside* the two SQL ones rather
than replacing them — `source_projection` and `watermark_tz` answer "not
applicable", which keeps the report's shape stable for a GUI that renders
whatever codes come back.

**`log_source`** — the transport is usable, the guards are satisfied, the glob
matches something. It borrows U5b's asymmetry and that is the whole design: a
**rejected configuration blocks** (an unparseable URI, an unknown scheme, a
guard's refusal, a missing program, an empty glob), an **unreachable source
warns** (DNS, TCP, SSH authentication, HTTP, a missing host key). The extract
reports the second kind better than a preflight can, and blocking every run
over a refused connection would be harsher than the SQL side is.
`core/logsource.py:LogTransportUnreachable` is the distinction, a subclass so
every existing `except LogSourceError` still catches it.

An **empty glob blocks**, which could plausibly go the other way. It is
indistinguishable from a typo, and a load that reads nothing reports success —
which is how a datasource sits "healthy" for a week with no data in it.

**`log_pattern`** — the regex compiles, the time group exists, every fact
column names a group it captures. A missing group **blocks**, because the
alternative is zero rows and no error, the least debuggable outcome available.
A **low match rate only warns**: refusing the load is what
`log_min_match_pct` is for, that number belongs to the operator, and a
preflight sample is a tail rather than the window. The check verifies the
transport itself before sampling and stays quiet where that is the problem —
`log_source` has already said it, and doubling a refusal for one cause is the
trap `_check_source_projection` documents for the SQL side.

## The two guards

Both are read from `os.environ` **directly and never through
`core/config.py`**, although everything else global goes through it. That
resolver falls back to `starcubes_meta.app_config`, which the GUI can edit —
routing these through it would let an administrator unlock command execution
for themselves, which is the one thing they exist to prevent. They are a
deployer decision, so they live only in the environment.

| Variable | Default | Effect |
|---|---|---|
| `STARCUBES_LOG_ROOTS` | empty | Colon-separated allowed prefixes for `file://`. Empty = any path. Symlinks resolved. |
| `STARCUBES_LOG_EXEC` | empty | Comma-separated allowed programs. **Empty = `exec://` refused**, which is what the shipped image does. |
| `STARCUBES_LOG_SFTP_AUTOADD` | unset | `1` relaxes host-key verification to trust-on-first-use. |

`sftp://` verifies host keys by default (paramiko's `RejectPolicy`). With
`AUTOADD` it is genuinely trust-on-*first*-use rather than trust-always:
paramiko persists the accepted key to `~/.ssh/known_hosts`, so a later changed
key is refused even with the variable still set.

One trap worth not rediscovering: paramiko's `load_host_keys` sets
`_host_keys_filename` *before* reading the file, so swallowing the read error
still leaves it set — and `AutoAddPolicy` then calls `save_host_keys`, which
re-reads it and raises `FileNotFoundError`. A container has no
`~/.ssh/known_hosts`, so the first `sftp://` connection in the shipped image
failed with that instead of connecting. The file is created empty first, which
is what `ssh` effectively does.

The remaining environment variables are tuning knobs and are listed in
[`deployment-config.md`](deployment-config.md).

## The shipped profiles

Four, under `profiles/examples/`. Each **demonstrates** the transform rather
than describing it, because the two cases are what these formats do every day.

| Profile | Notes |
|---|---|
| `nginx_combined` | The smoke test: nearly every machine has one of these files. Path reduced to its first segment, `-` byte counts to 0, user agent to its first token. |
| `apache_combined` | Nearly the same regex — nginx took the format from Apache — with the referer and agent fields optional so the shorter "common" variant matches too. |
| `syslog_rfc3164` | The format with no year, so it is the proof that the year-from-mtime logic works. Needs `log_time_zone`: RFC 3164 carries no offset at all. `msg` is deliberately **not** a dimension — a free-text message is close to unique per line. |
| `haproxy_httplog` | The richest, and the one to point a new installation at. Five timings per request is exactly the shape the ladder and candlestick renderers were built for, and the accept date carries milliseconds, so it exercises sub-second watermarks end to end. Every timing maps `-1` → 0. |

See [`source-profiles.md`](source-profiles.md) for the YAML schema. The
required-key list is kind-dependent: a log profile needs `log_line_regex`
where a SQL one needs `base_table`.

## Verification

**`manage.py check_logsource`** is the machine check for this whole document:
seventeen fixture scenarios, 81 assertions, no database. It writes log files
whose correct answer was computed independently and drives the reader over
them.

It exists for the same reason as `check_stream_stats`: **every** failure mode
in `core/logsource.py` is silent. A zone applied in the wrong direction, a
year taken from the wrong end of a December/January boundary, a sub-second
watermark rounded up, a transform that never fires — none of them raises, none
appears in a run log, and all of them produce a chart that looks entirely
plausible and is wrong. Reading the code cannot tell you which way round an
offset went.

Two of its scenarios are load-bearing and were written *because* they failed.
The multi-line one found the missing `re.MULTILINE`. The sub-second boundary
one appends to the file **between** the two runs, because on a static fixture
a ceiled and an unceiled watermark are both green — and it asserts that a
ceiled watermark *would* lose the appended line, so the check cannot quietly
stop testing anything.

Expected values are written out as literals, never derived, so the test cannot
agree with a broken implementation by construction. Extend it rather than
trusting a manual check when adding a transport or a time format.
