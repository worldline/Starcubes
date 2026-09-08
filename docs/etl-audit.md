# Audit modes — deep reference

The three audit modes in `core/audit.py` and the rationale for why each
one exists. Read before changing `core/audit.py` or the Audit sub-tab of
the ETL tab in `sources/templates/sources/manage.html`.

**Highest-value section for the Audit sub-tab** — read before changing
audit semantics. The ~46 B/id memory measurement in particular is the
reason `MAX_AUDIT_IDS` is what it is.

## Three modes

- **The audit has three modes, because each one sees what the previous
  cannot.**
  `audit_window` compares row *counts* per bucket: cheap (both DBs
  aggregate, only totals cross the wire) and unbounded in window size, so it
  is the sweep that says *where* to look. But equal counts do not prove a
  bucket is intact — a window that lost 100 rows and had 100 deleted
  upstream nets to zero and reads as clean, and duplicate PKs in fact (there
  is no unique constraint to prevent them) are invisible to it entirely.
  `audit_ids` pulls both PK sets and takes the set difference, so it names
  the exact rows and separates the three cases: *missing* (in the source,
  never loaded), *surplus* (in fact, no longer in the source) and
  *duplicate* (same PK stored twice). The count mode stays the default entry
  point because the ID mode is bounded by RAM; every bucket row in the GUI
  carries an `IDs` button, deliberately including the ones the counts call
  clean.
  `audit_id_details` is the third: a *targeted* follow-up on ids `audit_ids`
  has already named — kept separate precisely so the set difference stays at
  the ~46 B/id that lets it run over a wide window. It fetches each row's
  source watermark (one `WHERE <pk_expr> IN (…)` per 1000 ids, with the time
  window kept in the predicate because `<pk_expr>` is an expression and only
  the watermark column is indexed) and pairs it with the `etl_run` whose
  window covered that watermark:

      needed_lag_s = etl_run.last_end_time − the transaction's watermark value

  `last_end_time` is exactly the upper bound of the WHERE that run read the
  source with (`import_fact` stores the same `end_time` it put in the
  clause; `first_end_time` is the lower bound), so the difference reads:
  *the window's upper edge had already moved this far past the transaction,
  and it still was not visible.* That is the measurement `watermark_lag_s`
  is sized from instead of guessed.
  **The single most important property is that both operands come from the
  source's own watermark column.** The obvious-looking `started_at −
  watermark` — which this first shipped as — subtracts two different clocks:
  the watermark is source wall-clock time that `_as_utc` only *labels* UTC,
  while `etl_run.started_at` is `datetime.now(timezone.utc)` on the
  starcubes host. Against a source writing in `Europe/Berlin` it is off by
  the full UTC offset and goes negative. Worse, the end-to-end test *hid*
  it by forcing `timezone=UTC` on both connections — a test that normalises
  away the very axis it is checking. `started_at` is still returned and
  shown, muted and labelled as the local clock, purely as "when did this
  actually run"; it must never re-enter the subtraction.
  Three consequences worth keeping straight:
    - **It cannot be negative**, since a run is only attributed when
      `txn_wm < last_end_time`. The clock-mixing version would have needed a
      sanity check; this one is correct by construction.
    - **It sizes an *increase*.** With lag `L_old` in force,
      `last_end_time = M − L_old`, so catching the row needs
      `L_new ≥ M − w = (last_end_time − w) + L_old`. In the normal case —
      measuring *before* setting a lag, `L_old = 0` — the difference is the
      target outright, which is why the GUI says "raise by", not "set to".
    - **The *last* covering run is used, not the first.** Every covering run
      missed the row, and the latest has the largest `last_end_time`, hence
      the tightest bound. With `late_arrival_overlap_s = 0` the windows do
      not overlap and there is only one anyway.
  It stays a **lower bound** — the row may have remained invisible far
  longer — and rows that fall in *no* run window are counted separately and
  kept out of the percentiles, since those are a hole in the run history, a
  different failure entirely. When the wall-clock term set the bound, the
  figure is still valid (it is what the WHERE compared against either way),
  it just no longer maps onto one specific knob; recording `max_wm` per run
  would disambiguate that at the price of a column, without making the
  figure itself any sounder.

## Memory bound

- **Reading a column of ids costs ~46 B/id, not the 8 B an int64 suggests —
  and `Result.partitions()` does not stream.** Measured over 1M int64 PKs,
  peak RSS per id: `pd.read_sql` 264 B, buffered `.all()` 193 B,
  `stream_results` + `partitions()` 193 B, `stream_results` +
  `fetchmany()` 46 B, raw psycopg2 named cursor + `fetchmany` 31.5 B.
  `core/audit.py:_fetch_id_array` therefore puts `stream_results` on the
  *connection* and pulls batches with `fetchmany`, folding each batch
  straight into an int64 array via `np.fromiter` — one batch of driver
  tuples alive at a time, plus the array's own 8 B/id and a transient
  doubling at the final `concatenate`. The named cursor is faster still but
  is psycopg2-only, and the same function reads the (possibly Oracle) source
  side, where `stream_results` maps onto the driver's arraysize.
  `MAX_AUDIT_IDS` (2M/side, ~190 MB across both) is set from that measured
  46 B, not from the nominal 8. A Python `set` of the same ids measures
  73 B/id on its own, which is why this path uses arrays — while the ETL's
  overlap filter keeps its set, because there the access pattern is repeated
  per-chunk membership tests and a hash table earns its overhead.

## Counting strategy

- **`core/audit.py` counts the two sides differently on purpose.** Any
  *bounded* overlap can be exceeded, so the Audit sub-tab compares source and
  fact row counts per time bucket — and is also how an operator measures
  what `late_arrival_overlap_s` should be in the first place. The fact side
  is one query bucketed server-side by integer division on the epoch; the
  source side is N plain `count(*)` range predicates on `watermark_column`,
  deliberately *not* `date_trunc`/`TRUNC`, which would drag the open
  multi-dialect item in [`docs/backlog.md`](backlog.md) into a new place. When
  `watermark_fact_expr` is derived, the fact-side count can use neither BRIN
  nor partition pruning and scans the range — hence the bucket cap (200) and
  the `statement_timeout`, since uWSGI only has 8 request slots.