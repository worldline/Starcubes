"""Prove the rollup answers exactly what `fact` answers.

    manage.py rollup_verify --name payments
    manage.py rollup_verify --name payments --verbose
    manage.py rollup_verify --name payments --interval 86400

Runs a matrix of chart queries twice — once against `fact`, once against the
rollup `pick_rollup` chose — and compares the resulting plot data cell by
cell. This is the sharpest check in the whole feature: everything else tests
that the machinery runs, this tests that it is *right*.

The matrix covers what the read path actually varies: each group_by (plain,
and grouped where a group set exists), each filter form `_filter_sql` can
emit, both interval steps a rollup can serve, and every derivable measure.

Cases the rollup legitimately cannot serve — a filter on a column outside the
spec, a quantile measure, a window reaching past `built_through` — are counted
as skipped, not as failures. A skip is the read path correctly falling back to
`fact`.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from sqlalchemy import text

from core import rollup_run
from core.query import LADDER_STYLES, timeseries_multibar
from core.rollup import (floor_to_bucket, measure_expr, pick_rollup,
                         read_rollup_states, rollup_specs)
from core.source import Source, _get_cached_engine

# Anything below this relative difference is rounding, not a defect. Only
# `avg` can land here: fact computes avg(col) in one step while the rollup
# divides two stored sums, and Postgres numeric division truncates at a
# scale the two paths do not have to agree on.
_TOL = 1e-9


class Command(BaseCommand):
    help = "Compare rollup-served chart queries against the same queries on fact."

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None)
        parser.add_argument("--id", type=int, default=None)
        parser.add_argument("--interval", type=int, default=None,
                            help="only this interval (default: 3600 and 86400)")
        parser.add_argument("--days", type=float, default=3.0,
                            help="how much of the built range to compare over "
                                 "(default 3 days). Correctness does not "
                                 "depend on window size, but every case runs "
                                 "the fact query too, so a wide window makes "
                                 "the matrix slow for no extra assurance. "
                                 "0 = the whole built range")
        parser.add_argument("--verbose", action="store_true",
                            help="print every case, not just failures")
        parser.add_argument("--postgres-dsn", default=None)

    def handle(self, *args, **options):
        dsn = options["postgres_dsn"] or settings.STARCUBES_POSTGRES_DSN
        name_or_id = options["id"] or options["name"]
        if not dsn or not name_or_id:
            self.stderr.write("Error: --name (or --id) and a DSN are required")
            raise SystemExit(1)

        meta = _get_cached_engine(dsn)
        source = Source.load(meta, dest_dsn=dsn, name_or_id=name_or_id)
        specs = rollup_specs(source)
        if not specs:
            self.stderr.write(f"{source.name}: no rollup configured")
            raise SystemExit(1)
        states = read_rollup_states(meta, source.id)

        window = self._window(source, states, options["days"])
        if window is None:
            self.stderr.write(
                f"{source.name}: no built range to verify against — run "
                f"`manage.py rollup_build --backfill` first")
            raise SystemExit(1)

        intervals = ([options["interval"]] if options["interval"]
                     else [3600, 86400])
        cases = list(self._cases(source, intervals, window))

        rollup_run.install_abort_handler()
        run_id = rollup_run.start_run(
            meta, source.id, "verify",
            args={k: options[k] for k in ("interval", "days", "verbose")})
        try:
            # One deliberately misaligned window per interval, alongside the
            # aligned matrix. `pick_rollup` requires *both* window edges on a
            # bucket boundary (rollup.py:688), so a day-bin request starting at
            # 06:00 UTC is not served — the case comes back skipped, fact on
            # both sides. The chart never asks that way (its blocks are
            # calendar-aligned), but a hand-written URL, a bookmark or
            # query_multibar can, and the skip proves the fallback is the
            # expected answer there too.
            cases += [dict(c, offset_s=3600,
                           label=c["label"] + " [misaligned +1h]")
                      for c in cases if c["interval"] == 86400
                      and c["group_by"] in (None, "response")
                      and c["measure"] is None]

            self.stdout.write(
                f"{source.name}: {len(cases)} case(s) over "
                f"[{window[0]:%Y-%m-%d %H:%M} .. {window[1]:%Y-%m-%d %H:%M})")

            passed = skipped = failed = 0
            # Collected as well as printed: the GUI shows this matrix, and a
            # failure the operator has to act on must survive the log file it was
            # printed into.
            failures: list[dict] = []
            done = 0
            # Per interval, so a run whose window was too narrow to hold a single
            # bucket of some interval says so. Without this, `--days 1` silently
            # skips every day-bin case and still reports "0 failed", which reads
            # like a complete pass over a matrix that was half untested.
            tested_intervals: set[int] = set()
            for case in cases:
                done += 1
                t_from, t_to = self._align(window, case["interval"])
                off = case.get("offset_s")
                if off:
                    t_from += timedelta(seconds=off)
                    t_to += timedelta(seconds=off)
                if t_to <= t_from:
                    skipped += 1
                    continue
                tested_intervals.add(case["interval"])
                graph_style = source.graph_style_for(case["measure"] or "count(*)")
                if graph_style in LADDER_STYLES:
                    skipped += 1
                    continue

                spec = pick_rollup(
                    source, states,
                    group_by=case["group_by"], filter_tree=case["filter_tree"],
                    measure=case["measure"], graph_style=graph_style,
                    interval=case["interval"], t_from=t_from, t_to=t_to,
                    ladder_styles=LADDER_STYLES)
                if spec is None:
                    skipped += 1
                    if options["verbose"]:
                        self.stdout.write(f"  skip  {case['label']}")
                    continue

                common = dict(
                    group_by=case["group_by"] or "",
                    t_from=t_from, t_to=t_to, interval=case["interval"],
                    filter_tree=case["filter_tree"] or None,
                    measure=case["measure"], series_label="source_id",
                    graph_style=graph_style,
                )
                on_fact = timeseries_multibar(source, **common)
                on_roll = timeseries_multibar(
                    source, **common,
                    rollup_table=spec.name,
                    rollup_value_expr=measure_expr(case["measure"], spec))

                diff = self._compare(on_fact["data"], on_roll["data"])
                if diff is None:
                    passed += 1
                    if options["verbose"]:
                        self.stdout.write(
                            f"  ok    {case['label']}  via {spec.name}")
                else:
                    failed += 1
                    failures.append({"label": case["label"], "table": spec.name,
                                     "diff": diff})
                    self.stdout.write(self.style.ERROR(
                        f"  FAIL  {case['label']}  via {spec.name}\n"
                        f"        {diff}"))
                rollup_run.progress(
                    meta, run_id,
                    f"{done}/{len(cases)} cases — "
                    f"{passed} passed, {skipped} skipped, {failed} failed")

            self.stdout.write("")
            untested = [iv for iv in intervals if iv not in tested_intervals]
            if untested:
                self.stdout.write(self.style.WARNING(
                    f"  interval(s) {', '.join(str(i) for i in untested)} were not "
                    f"tested at all: the window is narrower than one bucket. "
                    f"Widen it with --days."))
            line = f"{passed} passed, {skipped} skipped (fact fallback), {failed} failed"
            # The per-case updates stop at the last case that actually ran a
            # query, so a matrix ending in skips would leave the row reading
            # like it stalled part-way. This is the closing count.
            rollup_run.progress(meta, run_id, f"{len(cases)} cases — {line}")
            # `ok` even with failures: the command ran to completion and the
            # answer it produced is the report. A `failed` row means the verify
            # itself broke down, which is a different thing to look at.
            rollup_run.finish_run(meta, run_id, "ok", report={
                "passed": passed, "skipped": skipped, "failed": failed,
                "cases": len(cases), "failures": failures,
                "intervals": intervals, "untested_intervals": untested,
                "window_from": window[0], "window_to": window[1],
            })
            if failed:
                self.stdout.write(self.style.ERROR(line))
                raise SystemExit(1)
            self.stdout.write(self.style.SUCCESS(line))
        except KeyboardInterrupt:
            rollup_run.finish_run(meta, run_id, "failed",
                                  error="killed by user")
            raise
        except SystemExit:
            # The failure exit below is the command's own verdict, not a
            # breakdown: the row is already closed as 'ok' with the matrix
            # on it, and re-closing it would overwrite that with an error.
            raise
        except Exception as exc:
            rollup_run.finish_run(meta, run_id, "failed", error=str(exc))
            raise

    # ── matrix ──────────────────────────────────────────────────────────

    def _window(self, source, states, days):
        """A built, bucket-aligned span to run the comparison over.

        Intersects every spec's built range: a case may be routed to any of
        them, and a window one spec does not hold would turn into a skip that
        looks like a coverage gap rather than what it is.
        """
        lows, highs = [], []
        for spec in rollup_specs(source):
            st = states.get(spec.name)
            if st and st.built_from and st.built_through:
                lows.append(st.built_from)
                highs.append(st.built_through)
        if not lows:
            return None
        lo, hi = max(lows), min(highs)
        if days and hi - timedelta(days=days) > lo:
            lo = hi - timedelta(days=days)
        return (lo, hi) if hi > lo else None

    def _align(self, window, interval):
        """Clamp the window inward to boundaries of `interval`.

        Condition 2 of `pick_rollup` requires both ends on a bucket boundary;
        for the day step that means giving up the partial days at each end.
        """
        lo, hi = window
        lo_a = floor_to_bucket(lo, interval)
        if lo_a < lo:
            lo_a += timedelta(seconds=interval)
        hi_a = floor_to_bucket(hi, interval)
        return lo_a, hi_a

    def _cases(self, source, intervals, window):
        """The comparison matrix, built from what this datasource actually has."""
        core = next((s for s in rollup_specs(source) if not s.is_marginal), None)
        marginals = [s for s in rollup_specs(source) if s.is_marginal]
        dims_by_fact = {d.fact_column: d for d in source.dimensions}

        group_bys = [None]
        if core:
            group_bys += list(core.columns)
        group_bys += [s.columns[0] for s in marginals]

        measures = [None] + list(source.rollup_measures)

        # Filter values sampled from the dim tables so the filters actually
        # match rows; an empty result would compare two empty dicts and prove
        # nothing.
        filter_col = None
        sample_ids: list[str] = []
        if core:
            for col in core.columns:
                dim = dims_by_fact.get(col)
                if dim and dim.dim_table:
                    with source.dest_engine().connect() as conn:
                        sample_ids = [str(v) for v in conn.execute(text(
                            f"SELECT source_id FROM "
                            f"{source.schema_name}.{dim.dim_table} "
                            f" WHERE source_id IS NOT NULL "
                            f" ORDER BY count DESC NULLS LAST LIMIT 3"
                        )).scalars().all()]
                    if sample_ids:
                        filter_col = col
                        break

        def row(col, op, vals):
            return [{"andOr": "and", "row": {"col": col, "op": op,
                                             "vals": vals}, "children": []}]

        filters = [("nofilter", None)]
        if filter_col and sample_ids:
            filters += [
                (f"in({filter_col})", row(filter_col, "in", sample_ids)),
                (f"not_in({filter_col})", row(filter_col, "not_in", sample_ids)),
                (f"starts_with({filter_col})",
                 row(filter_col, "starts_with", [sample_ids[0][:1]])),
            ]
            # Two conditions on one column, to exercise the tree walk that
            # `filter_tree_columns` mirrors.
            filters.append((f"and2({filter_col})",
                            row(filter_col, "in", sample_ids)
                            + [{"andOr": "and",
                                "row": {"col": filter_col, "op": "not_in",
                                        "vals": sample_ids[-1:]},
                                "children": []}]))
        # A filter on a column no rollup holds — must be skipped, never wrong.
        outside = next((c.name for c in source.fact_columns
                        if c.role in ("dimension", "auto-dimension")
                        and (not core or c.name not in core.columns)
                        and c.name not in [s.columns[0] for s in marginals]),
                       None)
        if outside:
            filters.append((f"outside({outside})", row(outside, "in", ["1"])))

        # A grouped group_by token where a group set exists.
        grouped_tokens = []
        for d in source.dimensions:
            for gs in d.group_sets:
                if gs.groups:
                    grouped_tokens.append(f"{d.fact_column}:grouped:{gs.slug}")
                    break

        for interval in intervals:
            for gb in group_bys + grouped_tokens:
                for mname, ftree in filters:
                    for measure in measures:
                        yield {
                            "group_by": gb,
                            "filter_tree": ftree,
                            "measure": measure,
                            "interval": interval,
                            "label": (f"iv={interval} gb={gb or '(all)'} "
                                      f"flt={mname} m={measure or 'count(*)'}"),
                        }

    # ── comparison ──────────────────────────────────────────────────────

    def _compare(self, a: dict, b: dict) -> str | None:
        """None when the two plot payloads agree, else the first disagreement."""
        if set(a) != set(b):
            only_a = sorted(set(a) - set(b))[:3]
            only_b = sorted(set(b) - set(a))[:3]
            return (f"slot sets differ: {len(a)} vs {len(b)} "
                    f"(fact-only {only_a}, rollup-only {only_b})")
        for slot in a:
            sa, sb = a[slot], b[slot]
            if set(sa) != set(sb):
                return (f"slot {slot}: series differ "
                        f"(fact-only {sorted(set(sa) - set(sb))[:3]}, "
                        f"rollup-only {sorted(set(sb) - set(sa))[:3]})")
            for series in sa:
                va, vb = float(sa[series] or 0), float(sb[series] or 0)
                if va == vb:
                    continue
                scale = max(abs(va), abs(vb), 1.0)
                if abs(va - vb) / scale > _TOL:
                    return (f"slot {slot} series {series}: "
                            f"fact={sa[series]!r} rollup={sb[series]!r}")
        return None
