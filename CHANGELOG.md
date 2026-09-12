# Changelog

Notable changes per release. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[semantic versioning as this project reads it](docs/releases.md) — where
"breaking" means *the operator has to do something by hand to upgrade*.

## [Unreleased]

## [2.0.0-alpha.8] — 2026-09-12

### Added

- **Viewport history — Back/Forward buttons and keys `b`/`B`.** timeseries.js
  0.10.7 files every window a navigation leaves and lets you step back through
  them, browser-style: the chart toolbar gains Back and Forward buttons in the
  time row, right of the range chips (icon: corner arrow), the keyboard pair
  `b`/`B` does the same from a focused chart. The keys panel lists them under
  Navigation, and the Follow button moved out of the right-hand button cluster
  into the time row beside them — Follow, too, is time navigation. All three
  buttons name their shortcut in the tooltip (`Back (b)`, `Forward (B)`,
  `Follow mode (f)`), as Clamp already did with `c`.

- **Clamp outliers — new toolbar button, new key `c`.** timeseries.js
  0.10.6 adds outlier clamping (`clampOutliers`, `toggleClampOutliers()`): one
  or a few extreme values stop owning the whole y-axis, the axis is rescaled
  from the bulk and the cut marks get an arrowhead at the plot edge. The chart
  toolbar gains a Clamp button (icon: a vertical bar cut by two
  oblique strokes), the keyboard key `c` does the same; the key that centred
  "now" moved from `c` to `n`, and the keys panel and the HOWTO keyboard tables
  say so. Clamping is session-only — off again on the next load.

- **`extent(col)` measures — min/avg/max bands that a rollup can serve.** A
  second aggregation form for the ladder renderers: where `quantile(col)`
  draws a percentile ladder, `extent(col)` draws a band from the bin's
  minimum through its mean to its maximum, with the rungs chosen in the
  measure's `Percentiles` field (`min,avg,max`, or `min,max` for a plain
  envelope). Available for `quantile-bands`, `quantile-steps` and
  `quantile-steps-open` — `error-bars` and `candlestick` read a ladder as
  nested pairs around a centre, which min/avg/max is not.

  The point is where the numbers come from. The extremes are re-derived at
  every resolution from the raw values, never from the averages of a finer
  tier — and because `min`, `max` and a count-weighted `avg` all fold across
  buckets exactly, this is the **first ladder form a rollup can answer**.
  Tick `extent(col)` in the Rollup tab's measure list and the band is served
  from `rollup_1h_…` like any count, including below `retention_floor` where
  the fact rows are gone and a percentile ladder is necessarily empty. No
  change to `@hgruber/timeseries.js` was needed: it has accepted a string
  rung ladder since 0.10.

- **New `graph_style` `multistep` for simple measures.** A simple
  measurement (count/avg/min/max/sum) can now be drawn as a staircase —
  each bin holds its value across the bin it represents, rendered by the
  library's `multiline` renderer with the step-after flag it has carried
  since 0.10. No library change and no coalesce registration needed: an
  after-step run extends one bin past its last slot, which is exactly where
  the next fetch block begins, so the staircase stays continuous at
  fetch-block margins (where a plain polyline leaves a one-bin gap).

### Fixed

- **The Clamp button now clamps the chart it advertises.** The timeseries.js
  0.10.6 detection rule was narrower than its documentation: it looked for a
  3× jump between neighbouring values *inside* the top 5 % of the visible
  samples, so a dominant spike over a dense tail — every multi-series chart,
  in practice — was never detected and the toggle visibly did nothing. The
  fixed 0.10.7 rule measures the bulk below the top 5 % directly; on the
  `payments` window the 1 h tier now clamps to the bulk (axis ~91 where it
  read ~364 before), the minute tier stays untouched as it should. Bundled
  with the next container build.

- **Two measures over the same aggregation now draw in their own graph
  style.** The measure catalog's identity is `(datasource, name)`, so an
  operator may list `quantile(stay)` several times to offer the same
  percentiles as bands, as steps, as error bars. The chart sent only the agg
  form, and the server resolved the catalog row by matching that string —
  which always found the first row. Every one of those measures therefore
  drew as whichever style happened to be configured first (and read that
  row's `Percentiles` field too). The chart now sends the measure's name
  alongside the agg (`?measure_name=`), and `Source.measure_row` resolves the
  row by it; a request without the parameter — an old bookmark, an external
  caller, `rollup_verify` — keeps the historical agg-only match, so a catalog
  with no duplicates is unaffected. The dropdown, the shared link, the stored
  per-source state and the copy-chart-SQL button all carry the name.

### Changed

- **Toolbar and shortcuts polish.** The range chips (Today, Week, Month) and
  the Legend button name their keyboard shortcut in the tooltip (`t`, `w`,
  `m`, `l`) — Yesterday has none and says so by having none; the query console
  gains the shortcut `q` (page key like `?`, toggles the console from anywhere
  outside a text field, logged-in users only; tooltip and the keys panel's
  Page group say so). The from/to inputs lose the slack between the value and
  the calendar icon — the input was sized for 16.5 characters where the
  rendered 16-char value needs 15.6.

- **The copy-chart-SQL button serves its statement from the rollup when the
  chart is.** The copied SQL used to read `fact` unconditionally, even when
  the chart displayed the same numbers from `rollup_1h_…`. It now routes
  through the same `pick_rollup` conditions as the chart's own data path,
  and the status line names the table it reads. Its time window is also
  snapped to whole bars, so the statement reproduces the legend totals
  exactly — including the half-visible bars at the viewport edges. Quantile
  graphs keep reading `fact` (percentiles do not fold from per-bucket
  aggregates); `extent(col)` bands, added above, do fold and are served from
  the rollup like any scalar measure.

- **The pre-aggregation note below the retention floor no longer says "counts
  only".** It never was only counts — every derivable measure works there —
  and with `extent(col)` bands it would have been actively misleading. The
  note now reports what is actually gone: drill-down for every measure, plus
  the values themselves for a `quantile(col)` ladder.

- **`manage.py rollup_verify` checks ladder measures instead of skipping
  them.** `extent(col)` cases are now run against both `fact` and the rollup
  and compared rung by rung; only `quantile(col)` is still skipped, since
  there is nothing to compare it to. Its value comparison also stopped
  treating a NULL as a zero — a fact-NULL against a rollup-0.0 is a real
  derivation bug and now fails instead of rounding to equal.

Candidates live in [`docs/backlog.md`](docs/backlog.md), and nothing there is
committed to a release until it is listed here. The road to a final 2.0.0
runs through further pre-release tags — see
[`docs/releases.md`](docs/releases.md).

## [2.0.0-alpha.7] — 2026-09-11

### Added

- **Alarms can be backed up and restored.** `manage.py alarm_export` writes a
  datasource's rules — or every datasource's — to a YAML file, and
  `manage.py alarm_import` reads one back; the Alarms tab carries the same
  pair as a **Backup** card, where the import shows its plan before it
  writes anything. Recipients travel as usernames rather than `auth_user`
  ids, which mean nothing outside the installation that issued them.

  The file holds configuration only: no ids, no `eval_end`, no firing
  history. The import is an **upsert on the alarm name**, never a replace,
  so a restored rule keeps the episodes and the change history it already
  had, and a rule the file does not name is left where it is. A second
  import writes nothing at all.

  This is not part of the source profile, and not only because an alarm is
  operating state rather than structure: Apply Profile is a replace over
  the child tables, so an `alarm` entry there would cascade
  `alarm_history` and `alarm_firing` away on every apply.

  A malformed file is refused whole; a rule that merely does not fit this
  installation — a deleted recipient account, a missing PagerDuty key, a
  column the schema no longer has — is skipped with a warning, because a
  backup that cannot be restored after one rule went stale is not worth
  having. The alarm field validation moved to `sources/alarm_fields.py`
  so an imported rule passes through exactly the checks a form save does.

- **The documentation is in the app, at `/help/`.** Every Markdown file the
  project ships — this changelog, the README, the how-to and all of `docs/` —
  is rendered at request time and cross-linked, so a question that arises
  mid-configuration is answered without leaving the GUI. Reachable from the
  main menu in both page shells, from the About dialog, from the login page
  (it needs no account) and from a `?` on the cards it explains. Search is
  over the whole corpus and links straight to the matching section.

  The files stay the single source of truth: `core/docs.py` reads them from
  the repo, rewrites their relative links and screenshots onto URLs the app
  serves, and caches on mtime. `manage.py check_help_docs` verifies that
  every link, anchor, image and template slug still resolves.

  Two things came with it. `LICENSE` and `THIRD-PARTY-NOTICES` are now
  readable from a running container, which is where the MIT notice
  `timeseries.js` obliges us to ship has been invisible until now. And
  `.dockerignore` no longer drops the documentation from the image — its
  `*.md` never matched a subdirectory, so what it actually excluded was the
  four repo-root files, while every `docs/*.md` and the per-app `CLAUDE.md`
  shipped. Both halves of that are fixed.

## [2.0.0-alpha.6] — 2026-09-10

### Added

- **The fact table is pre-aggregated, and the charts read from it.** Every
  chart request used to re-aggregate raw rows, and the query cache only ever
  helped a query someone had already asked — the first look at any window
  paid the full scan. A datasource can now keep hourly rollup tables beside
  its fact table: one row per bucket per combination of a chosen set of
  dimension columns, plus narrower marginals. The chart picks the narrowest
  rollup that covers the question and falls back to `fact` when none does, so
  nothing has to be configured for the charts to keep working. On the example
  data the reference query drops from 1073 ms to 44 ms, and the whole HTTP
  read path from 1360 ms to 105 ms, against a rollup a tenth the size of the
  fact table. Off by default.
- **The rollup has a tab, not just a shell prompt.** Its column set is not a
  setting you type once — it is measured, then narrowed, in a loop of advise,
  configure, build, verify — and every number feeding that loop (fact size,
  measure catalogue, dimension cardinalities, ETL history) already lives in
  the browser. The new **Rollup** tab runs the whole loop: **Advise** measures
  a real window and offers a ladder of column sets with the space each would
  cost, a click fills the config form from the stage you pick, **Build**
  backfills, **Verify** answers a matrix of chart queries from both the
  rollup and `fact` and compares them, and **Drop** removes the tables. The
  three long-running ones are spawned as detached processes on the ETL's own
  machinery and report progress into a run history, so a run started from a
  shell shows up in the GUI exactly like one started from a button — a web
  request cannot hold them (uwsgi kills a worker at 120 s).
- **Five `manage.py` commands for the same thing without a browser:**
  `rollup_advise`, `rollup_config`, `rollup_build`, `rollup_verify`,
  `rollup_drop`.

### Changed

- **Aggregates outlive the rows they were computed from.** Retention
  deliberately does not clamp a rollup: its numbers were correct when they
  were computed and stay correct after the raw rows are disposed of, and at
  two orders of magnitude smaller, keeping them is close to free. The price
  is that below the retention floor the rollup is the only copy — so
  `rollup_drop` and `rollup_build --rebuild` refuse that span unless forced,
  the chart offers the wider range the rollup can still answer, and marks
  where the answers get thinner: counts yes, percentiles and drill-down no.
- **Reconfiguring a rollup degrades to `fact` rather than lying.** The stored
  configuration is hashed; rows built under a different shape are never read,
  so a changed column set costs a rebuild instead of returning numbers
  computed under the old one. Percentiles are rejected at configuration time
  for the same reason — they do not sum, so a rollup can never serve them.
- Two additive, idempotent migrations (`0038`, `0039`) apply themselves at
  start; nothing an operator has to do by hand.

### Fixed

- Written down at last: **a day bin is a UTC day**, so for a reader in Berlin
  it runs 02:00 to 02:00 and "Monday's transactions" is not Monday. Wrong
  since the first chart, and not fixable with an offset — the backlog entry
  names the dozen sites across five files that hold it at UTC. Pre-aggregation
  does not worsen it, but it adds a way to cement it, which is why hourly
  buckets are the default and day buckets are not.

## [2.0.0-alpha.5] — 2026-09-08

### Changed

- **The datasource page is one page now.** `detail.html` (lean, login-only)
  and `manage.html` (admin-only) had drifted apart — Alarms and Firings
  existed only on one, the six configuration tabs only on the other, and the
  only way across was a ⚙ button with no way back. Both render as
  `source.html` now, with a **Basic/Expert** toggle in the sub-tab bar:
  Basic keeps the five tabs a non-admin needs, Expert shows all eleven, and
  what a non-superuser receives at all is decided server-side as before.
  `/sources/<pk>/manage/` still exists and still forces Expert, so the
  profile hand-off and the edit form's redirects keep working.
- **The four ETL tabs are one tab.** ETL Commands, Audit, ETL History and
  ETL Log were a quarter of the tab bar under one subject; they are a single
  **ETL** tab now, with six sub-tabs — Run, Maintenance, Schema, Audit,
  History, Log — under the same sub-tab row Dim Data already had. Nine
  entries left in the top row.

### Fixed

- The alarm UI no longer says "open" twice. On the Firings tab the amber
  status pill is the only signal (the blinking red dot at the start of the
  row is gone); on the Alarms tab it is the other way round — a rule with an
  open episode leads with the bare blinking dot instead of the pill. The
  alarm titles on the Firings tab also show the rule's comment as a tooltip,
  and the My Profile page drops its heading in favour of the tab title.

## [2.0.0-alpha.4] — 2026-09-08

### Added

- **The preflight also compares the fact table against its own
  configuration.** A column the configuration expects but the table does not
  have now refuses the run by name, instead of failing later inside the bulk
  load with a column-count error.
- **Every ETL run checks its assumptions before acting on them.** A new
  preflight runs at the start of each load: the fact and dimension tables
  exist (and are created, loudly, if not), `starcubes_meta` is as new as the
  code, the indices and foreign keys are in place, the source still projects
  every configured column, and the stored high watermark has not been left
  standing above a fact table that shrank underneath it. Findings land in the
  run's `stats`, and a **Preflight** card in the ETL tab asks the
  same questions as a dry run without starting a load. Missing indices are
  reported, never built — an unattended index build on a large fact table
  would block every load for hours. A renamed source column now refuses the
  run with the column named, instead of failing mid-extract.
- **Old data can finally be thrown away.** A retention setting per
  datasource keeps the last N months (or weeks, or days) and lets the
  housekeeping drop the rest. Fact partitions have been unbounded since the
  beginning — cheap to write, never reclaimed. Off by default and
  irreversible, so it is limited on purpose: at most a couple of partitions
  go per run, which turns a mistyped number into something that walks
  visibly over hours instead of clearing the table in one pass. Everything
  that assumed the data went back forever now knows better — the Audit tab,
  both caches, the chart's advertised time range, and alarms, whose windows
  stop being evaluated rather than quietly reporting an outage that
  retention itself caused.
- **A first load no longer blocks everything else for a day.** Setting an
  initial window makes a catching-up datasource load in steps, so alarms are
  evaluated and housekeeping runs in between, instead of one run of many
  hours during which nothing else happens. Off by default.
- **The dimension import can be throttled.** It runs on every load, which at
  a one-minute interval means re-reading every lookup table sixty times an
  hour to find values that arrive weekly. Off by default, because throttling
  costs freshness: a brand-new dimension value shows as unknown until the
  next refresh.
- **Alarms can now watch the ETL itself, not just the data in it.** A second
  kind of alarm rule — set up in the Alarms tab, in its own table below the
  threshold rules — fires on the things that were previously only ever
  detected and written to a log: the source has stopped producing, runs are
  failing, no run is completing at all, the preflight is reporting problems,
  a dimension is filling up. They use the same e-mail, XMPP and PagerDuty
  channels, the same recipients and severities, and appear in the same
  Firings view as threshold alarms.
  Crucially they are also evaluated **from outside the ETL**, by a small
  process of its own in the container and by the web application itself: a
  hung ETL cannot report that it is hung, and "no data has loaded since
  Friday" is exactly the message that must not depend on the thing that
  stopped. Every new datasource is created with two of these rules already
  in place — "runs are failing" and "no run is completing" — active but
  without recipients, so they record episodes from day one and start sending
  the moment somebody picks a channel.
- **Periodic maintenance now happens without anyone remembering it.** A new
  `manage.py etl_housekeeping` — also a **Housekeeping** card in the ETL
  Commands tab, and run automatically by a forever session every six hours —
  updates planner statistics on the partitions actually being written and
  summarises BRIN index ranges. Neither happened before: the load only ever
  inserts, so autovacuum's thresholds are effectively never reached, which
  left statistics stale on exactly the partition every chart reads and left
  the newest data outside the BRIN index altogether. Nothing about that was
  visible; everything was just slower. The same run reports how close each
  dimension is to its hard 32767-value ceiling — a mis-configured automatic
  dimension otherwise fails mid-load with no warning — and records partition
  sizes over time, so growth is something you can see coming rather than
  discover. The interval is per datasource on the edit form; 0 turns the
  automatic run off. Safe to run during a live load.
- **A source that is up but has stopped sending data is now visible.** It
  was the one failure nothing could see: alarms cannot fire during a
  standstill by construction — the watermark stops moving, so their window
  stops sliding — and the ETL simply logs that it loaded nothing and sleeps.
  Every run now records how far the newest row in the source lags the
  source's *own* clock, shown per run in the ETL History table, and a **Max
  Data Lag** on the edit form turns that into a preflight warning. Both
  numbers come from the source, so the figure is unaffected by clock
  differences between machines — and a negative one is reported for what it
  is, a watermark column that is not in UTC, rather than as a small lag.
- **A run can be given a time limit.** **Max Run Time** aborts a run that
  overruns it and records it as failed, and bounds the queries it sends to
  the source — a source that accepts the connection and then never answers
  used to block the load indefinitely. Never applied to a backfill, an
  explicit Start/End window, or the first load. It is a circuit breaker
  rather than a repair: the next window is wider, not smaller.
- **A datasource can only be loaded by one process at a time.** A hand-run
  `manage.py etl_run` used to run happily alongside a live forever session,
  which can duplicate rows and race the dimension tables. It refuses now, and
  so do Delete From, Truncate Partition and Drop Partition while a load is
  running.

### Fixed

- **E-mail and XMPP alarm notifications could never have been delivered.**
  The dispatcher looked its recipients up with a query the database driver
  refused to accept, so every notification with a recipient failed inside
  that lookup and was recorded as an error — taking PagerDuty down with it,
  since the same failure discarded the routing keys. Anyone who had
  configured a transport would have seen alarms fire and nothing arrive.
- **Deleting fact rows no longer leaves the high watermark lying.** Delete
  From rewinds the watermark to the delete boundary, so the window is
  actually loaded again — previously the deleted rows were never requested
  again, silently. Dropping or truncating a partition does the opposite, as
  it should: the watermark stays put and the datasource records how far back
  its data still goes, which the Audit tab now respects instead of reporting
  a retired window as a total loss. Where the watermark and the partition key
  are different columns, the reload is de-duplicated by primary key so the
  repair cannot introduce duplicates. All four also invalidate the plot
  cache, which they previously did only from the GUI.
- **The chart could keep showing data that no longer exists.** Its in-page
  cache had no way of knowing the server's data had changed underneath it,
  so a backfill, a watermark correction or a retention drop stayed invisible
  until the page was reloaded.
- **Large numbers in alert subjects were unreadable.** A count of 8729883
  arrived as "8.72988e+06"; it now reads 8,729,883.
- **The alarm phase no longer scales with the number of alarms.** The fact
  table's clock was read once per alarm; it is read once per load now.
  Both alarm queries also carry a statement timeout, so a pathological filter
  can no longer hold up the ETL loop indefinitely.

## [2.0.0-alpha.3] — 2026-09-07

### Added

- **Alarms can notify by e-mail, XMPP and PagerDuty.** Each alarm picks
  recipients per channel from the login users (e-mail from the account, XMPP
  via a new per-account JID field on the settings page); PagerDuty speaks
  Events API v2 with a per-datasource integration key (routing key, source
  edit page) and closes its incidents when an episode resolves. Transport
  configuration is env-var based — see `docs/deployment-config.md`. Purely
  additive schema, self-applying at container start (minor release).
- **The PagerDuty severity is per alarm.** An alarm sets one of
  critical/error/warning/info; unset means the global
  `STARCUBES_PAGERDUTY_SEVERITY` applies at dispatch. Settable on both alarm
  forms and inline in the Alarms tab, which shows it in its own column.
- **Receiving alarm messages is the user's decision.** Accounts opt in to
  e-mail and XMPP per channel on My Profile — **default off**, so being
  listed on an alarm alone no longer delivers anything; dispatch drops
  accounts that never opted in (logged as WARNING). Existing alarms fall
  silent for users who do not opt in — deliberate.
- **The notification configuration is reachable in the GUI.** A global
  configuration tab (in the tab strip and both hamburger menus) shows the
  transport status, the per-datasource routing keys masked, and a
  test-send button per source; the per-source routing key sits under
  "Basics" on the edit page; the own profile page shows and edits the
  account's XMPP JID.
- **Global configuration is DB-editable.** One key-value table
  (`starcubes_meta.app_config`, key = the env-var name) backs the
  notification transports and the SSO/OIDC login; resolution is
  environment-first everywhere (`core/config.py`), so env vars still win
  and the GUI edits only what the environment does not set. The global
  tab is now called **Transports** (was "PagerDuty") and edits SMTP, XMPP,
  the PagerDuty endpoint and the SSO trio in place; transports apply
  live, SSO on/off on restart.
- My Profile appears as the last tab of the global tab strip, after Users,
  and carries the notification opt-in.
- The About dialog's **timeseries.js** row now links to the project on
  GitHub.

### Changed

- **The `log` channel is no longer selectable** — the firing-episode table
  and the evaluator's log line are always on, so the checkbox would have
  been dead weight. Legacy alarms that still store `log` keep working; the
  value is dropped silently on their next edit. Nothing to migrate.
- The Alarms list names the active channels as a plain comma-separated
  list; the recipient names moved into the edit view (which also flags
  recipients who have not opted in).
- Both hamburger menus are rendered from one shared template
  (`sources/_nav_menu.html`) — the same rows appear in the chart and in
  the sources app by construction, not by keeping two copies in sync.

### Fixed

- The channel checkboxes in the Alarms list were always visible; they now
  appear only while a row is being edited, and the alarm table fits
  again (abbreviated "Thresh." header, red blinking dot at the start of
  open firing rows next to the amber "open" badge, which also shows on
  the Alarms list and refreshes every 20 s).
- **The Users page's "Edit user" form persisted nothing** — a duplicated
  `update_user` branch shadowed the real one (regression of the alarm
  notification change; e-mail, XMPP JID and flags were silently dropped).
- A duplicated validation block in the alarm save path refused a
  channels-only inline edit with "no recipient chosen".
- The chart page no longer grows a horizontal scrollbar.
- `manage.py check_migration_mirror` machine-checks the mirror contract
  between the per-step migrations and the consolidated
  `migrations/starcubes_meta.sql`.

## [2.0.0-alpha.2] — 2026-09-07

### Fixed

- **A build that bypasses `docker/build.sh` is now refused** instead of
  quietly producing an image that reports its version as `unknown`. The
  version cannot be discovered from inside the image — `.dockerignore` keeps
  `.git` out of the build context — so a bare `podman build` left it empty
  and the About dialog had nothing to name. Building without git on purpose
  still works, it just has to say so:
  `--build-arg STARCUBES_VERSION=unknown`.
- **Builds on a host with Python 3.6 work again.** `docker/build.sh` reads
  the version by running `starcubes/version.py` with the build machine's
  `python3`, and that module opened with a 3.7-only `__future__` import, so
  the build died on a `SyntaxError` before reaching podman. The module is
  now held to Python 3.6 — it is the only file in the tree that is.
- **A source archive names its version too.** Code handed over as a
  `git archive` zip carries no `.git` and gets no build argument, so the
  About dialog there reported `unknown`. `git archive` now fills the version
  and the commit in as it packs — the same command as before, no extra flag.
- **The copyright notices are right, and complete.** starcubes is
  Copyright © 2026 Worldline. The vendored `@hgruber/timeseries.js` is MIT,
  Copyright © 2022–2026 Holger Gruber, and its notice now actually ships:
  the published bundle is minified with no banner, so the image carries the
  package's own `LICENSE` next to it, the About dialog names both, and
  `THIRD-PARTY-NOTICES` reproduces the text in full.

## [2.0.0-alpha.1] — 2026-09-07

First tagged version. Everything before it shipped unversioned — the history
is in `git log`, not here, so this entry describes what the release *is*
rather than what changed inside it.

An alpha, not a finished 2.0.0: the feature set below is in place, but the
schema, the environment variables and the profile format may still move
before 2.0.0 is final.

### Added

- **Version identity.** The git tag *is* the version — `starcubes/version.py`
  derives it from `git describe`, so a tag and a version cannot disagree.
  `docker/build.sh` resolves it, tags the image with it and stamps the
  commit and build time in; the GUI's **About starcubes** menu row opens a
  dialog naming all of it, with a **pre-release** pill on versions like this
  one. See [`docs/releases.md`](docs/releases.md).

### The feature set

- **Configurable multi-source ETL.** A source is a `Source` dataclass loaded
  from `starcubes_meta` — base table, watermark column, fact columns,
  dimensions — with nothing hard-coded. Incremental loads run on a high
  watermark, resumable, with a `COPY FROM` load path.
  ([`docs/etl-engine.md`](docs/etl-engine.md))
- **Source profiles.** A configured source can be saved as a YAML profile
  and re-applied to the next install in one click.
  ([`docs/source-profiles.md`](docs/source-profiles.md))
- **Compact star schema on Postgres.** Smallint-keyed dimensions, range
  partitions, BRIN on the time column.
  ([`docs/star-schema.md`](docs/star-schema.md))
- **Row-count audit** of every loaded window against the source.
  ([`docs/etl-audit.md`](docs/etl-audit.md))
- **Chart GUI.** Stacked-bar timeseries with filters, grouping, drill-down
  into single transactions, ladder plots and quantiles.
  ([`docs/chart-app.md`](docs/chart-app.md),
  [`docs/chart-rendering.md`](docs/chart-rendering.md))
- **Alarms.** Per-datasource thresholds evaluated against the ETL high
  watermark, with a firings list in the GUI. ([`docs/alarm.md`](docs/alarm.md))
- **SQL console** against the warehouse, with per-user history.
- **One container.** Single image, Postgres alongside it, local login or
  Keycloak/OIDC SSO. ([`docs/deployment-config.md`](docs/deployment-config.md))
