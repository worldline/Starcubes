"""Fill a datasource's rollup tables.

    manage.py rollup_build --name payments                # forward only
    manage.py rollup_build --name payments --backfill      # plus the history
    manage.py rollup_build --name payments --rebuild       # discard and redo
    manage.py rollup_build --name payments --backfill --from 2026-01-01

Three things happen here, in this order:

  * **forward** — every bucket that has become final since the last run, i.e.
    everything below `hwm - late_arrival_overlap_s`. This is also what the ETL
    hook does after each load, so on a running datasource it is usually a
    no-op.
  * **backfill** — the history, *backwards* from the present in batches. The
    newest windows become usable first, which is what makes a run over a large
    history worth starting: charts get faster from the first batch instead of
    after the last one.
  * **rebuild** — the same as backfill, but after discarding what is there.
    Needed when the column set or the measure list changed, because the stored
    rows describe a different shape.

The run is resumable and interruptible. Each batch commits and advances
`rollup_state.built_from`, so Ctrl-C leaves a smaller but entirely valid
rollup, and starting again continues where it stopped. It holds the
datasource advisory lock throughout so it cannot interleave with an ETL run.
"""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand
from sqlalchemy import text

from core.preflight import datasource_lock
from core import rollup_run
from core.rollup import (RollupState, build_window, final_through,
                         floor_to_bucket, last_ok_hwm, read_rollup_states,
                         refresh_rollups, rollup_specs, spec_hash,
                         unreconstructable_span, write_rollup_state)
from core.source import Source, _get_cached_engine


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _fmt(n: float) -> str:
    return f"{n:,.0f}"


class Command(BaseCommand):
    help = "Fill the pre-aggregation tables for a datasource."

    # Set by handle() before any _say(); the helpers below run only from
    # there, but a None run_id is a no-op in core.rollup_run either way.
    _meta = None
    _run_id = None

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None)
        parser.add_argument("--id", type=int, default=None)
        parser.add_argument("--backfill", action="store_true",
                            help="also build the history, newest batch first")
        parser.add_argument("--rebuild", action="store_true",
                            help="discard existing rows first, then backfill "
                                 "— what a changed column set needs")
        parser.add_argument("--from", dest="t_from", default=None,
                            help="stop the backfill here instead of at the "
                                 "oldest fact row (ISO 8601)")
        parser.add_argument("--batch-buckets", type=int, default=168,
                            help="buckets per batch (default 168 = one week "
                                 "of hourly buckets)")
        parser.add_argument("--max-batches", type=int, default=0,
                            help="stop after this many batches; 0 = no limit")
        parser.add_argument("--force", action="store_true",
                            help="allow --rebuild to discard the span below "
                                 "the retention floor, which it cannot "
                                 "rebuild")
        parser.add_argument("--postgres-dsn", default=None)

    def handle(self, *args, **options):
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        if not dsn:
            self.stderr.write(
                "Error: --postgres-dsn or STARCUBES_POSTGRES_DSN is required")
            raise SystemExit(1)
        name_or_id = options["id"] or options["name"]
        if not name_or_id:
            self.stderr.write("Error: --name or --id is required")
            raise SystemExit(1)

        meta = _get_cached_engine(dsn)
        source = Source.load(meta, dest_dsn=dsn, name_or_id=name_or_id)
        specs = rollup_specs(source)
        if not specs:
            self.stderr.write(
                f"{source.name}: no rollup configured — run "
                f"`manage.py rollup_config --name {source.name} …` first")
            raise SystemExit(1)

        dest = source.dest_engine()
        iv = int(source.rollup_interval_s)

        # Started before the lock on purpose: `datasource_lock` raises when an
        # ETL already holds it, and "refused because the datasource was busy"
        # is exactly the outcome the history should carry.
        self._meta = meta
        rollup_run.install_abort_handler()
        self._run_id = rollup_run.start_run(
            meta, source.id, "build",
            args={k: options[k] for k in
                  ("backfill", "rebuild", "t_from", "batch_buckets",
                   "max_batches", "force")})
        try:
            self._build(source, dest, meta, specs, iv, dsn, options)
        except KeyboardInterrupt:
            # Inside the backfill loop this never fires — _backfill treats an
            # interrupt as a clean stop, because every batch has committed and
            # what exists is a valid rollup over a shorter range. This is a
            # kill anywhere else: the forward pass, the rebuild's discard, or
            # while waiting for the datasource lock.
            rollup_run.finish_run(self._meta, self._run_id, "failed",
                                  error="killed by user")
            self.stderr.write("killed by user")
            raise SystemExit(1)
        except Exception as exc:
            rollup_run.finish_run(self._meta, self._run_id, "failed",
                                  error=str(exc))
            raise
        rollup_run.finish_run(self._meta, self._run_id, "ok")
        self.stdout.write(f"{source.name}: done")

    def _build(self, source, dest, meta, specs, iv, dsn, options) -> None:
        with datasource_lock(dsn, source.id, holder="rollup_build"):
            hwm = last_ok_hwm(meta, source.id)
            through = final_through(source, hwm, iv)
            if through is None:
                self.stderr.write(
                    f"{source.name}: no successful ETL run yet — there is no "
                    f"watermark to decide which buckets are final")
                raise SystemExit(1)

            if options["rebuild"]:
                # A rebuild reads `fact`, and `fact` stops at the retention
                # floor. Anything the rollup holds below it would be
                # discarded and could not come back.
                span = unreconstructable_span(
                    source, read_rollup_states(meta, source.id))
                if span and not options["force"]:
                    oldest, floor = span
                    self.stderr.write(
                        f"{source.name}: refusing to rebuild.\n"
                        f"  The rollups hold {oldest:%Y-%m-%d %H:%M} .. "
                        f"{floor:%Y-%m-%d %H:%M}, below the retention floor. "
                        f"A rebuild reads fact, which no longer has those "
                        f"rows, so that span would be lost.\n"
                        f"  Pass --force to discard it, or reconfigure "
                        f"without --rebuild and let the forward path and a "
                        f"--backfill fill what fact still covers.")
                    raise SystemExit(1)
                self._discard(source, dest, meta, specs)

            # Forward first, always: it is cheap, and it is what gives a
            # freshly configured rollup a built_through at all. Without one
            # the backfill would have no upper anchor to walk down from.
            fwd = refresh_rollups(source, dest, meta, hwm=hwm, quiet=True)
            for b in fwd["built"]:
                self._say(
                    f"  forward  {b['rollup']:<24} +{_fmt(b['rows']):>12} rows "
                    f"[{b['from']:%Y-%m-%d %H:%M} .. {b['through']:%Y-%m-%d %H:%M})")
            if not fwd["built"]:
                self.stdout.write("  forward  nothing to do (already current)")

            if options["backfill"] or options["rebuild"]:
                self._backfill(source, dest, meta, specs, iv, through, options)

    # ── helpers ─────────────────────────────────────────────────────────

    def _say(self, line: str) -> None:
        """Print a progress line and put it on the run row.

        A backfill works backwards through the whole history one batch at a
        time and can run for hours. Without the line on the row, the only
        thing separating slow progress from a hung process is the log file.
        """
        self.stdout.write(line)
        rollup_run.progress(self._meta, self._run_id, line.strip())

    def _discard(self, source, dest, meta, specs) -> None:
        """Empty the tables and forget the bounds, keeping the tables.

        TRUNCATE rather than DROP: the partitions and the BRIN index are still
        correct — only the contents describe the wrong shape.
        """
        with dest.begin() as conn:
            for spec in specs:
                conn.execute(text(
                    f"TRUNCATE {source.schema_name}.{spec.name}"))
        for spec in specs:
            write_rollup_state(meta, source.id, spec,
                               built_from=None, built_through=None)
        self.stdout.write(
            f"  rebuild  discarded {len(specs)} table(s); "
            f"charts read fact until the backfill catches up")

    def _oldest(self, source, dest) -> datetime | None:
        """Oldest fact row, never below the retention floor.

        Below the floor the partitions were dropped on purpose; aggregating
        what is left there would produce buckets that look like a collapse in
        traffic rather than an absence of data.
        """
        pc = source.partition_column
        with dest.connect() as conn:
            oldest = conn.execute(text(
                f"SELECT min({pc}) FROM {source.schema_name}.fact")).scalar()
        if oldest is None:
            return None
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        floor = source.retention_floor
        if floor is not None:
            if floor.tzinfo is None:
                floor = floor.replace(tzinfo=timezone.utc)
            oldest = max(oldest, floor)
        return oldest

    def _backfill(self, source, dest, meta, specs, iv, through, options) -> None:
        lo_limit = (_parse_dt(options["t_from"]) if options["t_from"]
                    else self._oldest(source, dest))
        if lo_limit is None:
            self.stdout.write("  backfill nothing to do (fact is empty)")
            return
        lo_limit = floor_to_bucket(lo_limit, iv)
        batch = timedelta(seconds=iv * max(1, options["batch_buckets"]))

        states = read_rollup_states(meta, source.id)
        cursors: dict[str, datetime] = {}
        for spec in specs:
            st = states.get(spec.name)
            if st and st.spec_hash == spec_hash(spec) and st.built_from:
                cursors[spec.name] = st.built_from
            else:
                cursors[spec.name] = through

        total = max(1.0, (through - lo_limit).total_seconds())
        n_batches = 0
        t0 = time.monotonic()
        try:
            while any(c > lo_limit for c in cursors.values()):
                if options["max_batches"] and n_batches >= options["max_batches"]:
                    self.stdout.write(
                        f"  backfill stopped after {n_batches} batch(es) "
                        f"(--max-batches); re-run to continue")
                    break
                hi_batch = max(c for c in cursors.values() if c > lo_limit)
                lo_batch = max(lo_limit, hi_batch - batch)
                rows = 0
                for spec in specs:
                    cur = cursors[spec.name]
                    if cur <= lo_batch:
                        continue
                    spec_rows = build_window(source, dest, meta, spec,
                                             lo_batch, cur)
                    rows += spec_rows
                    cursors[spec.name] = lo_batch
                    st = states.get(spec.name)
                    built_through = st.built_through if st else through
                    rows_built = (st.rows_built if st else 0) + spec_rows
                    write_rollup_state(
                        meta, source.id, spec,
                        built_from=lo_batch,
                        built_through=built_through,
                        rows_built=rows_built,
                    )
                    # Keep the local view in step instead of re-reading: the
                    # only fields that move are the ones just written.
                    states[spec.name] = replace(
                        st, built_from=lo_batch, rows_built=rows_built
                    ) if st else RollupState(
                        spec_hash=spec_hash(spec), built_from=lo_batch,
                        built_through=built_through, rows_built=rows_built)
                n_batches += 1
                done = (through - lo_batch).total_seconds()
                self._say(
                    f"  backfill [{lo_batch:%Y-%m-%d %H:%M} .. "
                    f"{hi_batch:%Y-%m-%d %H:%M})  +{_fmt(rows):>12} rows  "
                    f"{100.0 * done / total:5.1f}%")
        except KeyboardInterrupt:
            # Every batch has already committed and moved built_from, so what
            # exists is a valid rollup over a shorter range. Nothing to undo.
            msg = (f"backfill interrupted after {n_batches} batch(es). "
                   f"What was built is usable; re-run to continue.")
            self.stdout.write(self.style.WARNING(f"\n  {msg}"))
            rollup_run.progress(self._meta, self._run_id, msg)
            return
        self.stdout.write(
            f"  backfill {n_batches} batch(es) in "
            f"{time.monotonic() - t0:.1f}s")
