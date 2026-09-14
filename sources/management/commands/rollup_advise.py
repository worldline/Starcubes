"""Measure what a rollup would cost for a datasource.

    manage.py rollup_advise --name payments
    manage.py rollup_advise --name payments --interval 3600 --window-buckets 24
    manage.py rollup_advise --name payments --extrapolate-to 833000
    manage.py rollup_advise --name payments --json

Reports, per dimension column, its cardinality, its marginal cost (what
including it multiplies the rollup by) and how often it has actually been
asked for; then a greedy ladder of candidate column sets with rows/day, size
and speed-up factor; then which catalogued measures a rollup could serve.

The output ends with a ready-to-paste `rollup_config` line. Nothing here
writes anything — see `manage.py rollup_config` for that.
"""
from __future__ import annotations

import dataclasses
import json

from django.conf import settings
from django.core.management.base import BaseCommand

from core.preflight import _check_indices
from core.rollup import interval_label
from core.rollup_advise import advise
from core import rollup_run
from core.source import Source, _get_cached_engine


def _fmt(n: float) -> str:
    """Thousands-separated integer, or a short float below 10."""
    if n and abs(n) < 10:
        return f"{n:.1f}"
    return f"{n:,.0f}"


def _size(n_bytes: float) -> str:
    for unit, div in (("TB", 1024 ** 4), ("GB", 1024 ** 3),
                      ("MB", 1024 ** 2), ("kB", 1024)):
        if n_bytes >= div:
            return f"{n_bytes / div:.1f} {unit}"
    return f"{n_bytes:.0f} B"


class Command(BaseCommand):
    help = ("Measure the cost of a rollup for a datasource: per-dimension "
            "marginal cost, candidate column sets, measure derivability.")

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None, help="datasource name")
        parser.add_argument("--id", type=int, default=None,
                            help="datasource id (alternative to --name)")
        parser.add_argument("--interval", type=int, default=3600,
                            help="bucket width in seconds (default 3600)")
        parser.add_argument("--window-buckets", type=int, default=24,
                            help="how many buckets to measure over "
                                 "(default 24 = one day at hourly buckets)")
        parser.add_argument("--extrapolate-to", type=float, default=None,
                            help="rows per hour to project onto; omit when "
                                 "the datasource already carries its real "
                                 "volume, since the measurement is then the "
                                 "answer")
        parser.add_argument("--target-factor", type=float, default=20.0,
                            help="how much smaller than fact the rollup "
                                 "should be (default 20). The suggested "
                                 "config is the *widest* column set reaching "
                                 "it, i.e. the most coverage for the size "
                                 "you asked for")
        parser.add_argument("--stage", type=int, default=None,
                            help="suggest this ladder line instead of "
                                 "choosing by --target-factor")
        parser.add_argument("--json", action="store_true",
                            help="emit the full report as JSON")
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
        dest = source.dest_engine()
        as_json = options["json"]

        # Recorded even for a shell-started run: the GUI reads this
        # table, and an advise the operator ran by hand is exactly the
        # report they will want to look at from a browser afterwards.
        rollup_run.install_abort_handler()
        run_id = rollup_run.start_run(
            meta, source.id, "advise",
            args={k: options[k] for k in
                  ("interval", "window_buckets", "extrapolate_to",
                   "target_factor", "stage")})

        # Missing indices make every measurement here scan the whole partition
        # instead of the window, which is both slow and misleading: the
        # operator would be comparing a rollup against a fact table in its
        # worst possible state. This is exactly the state a datasource is in
        # right after its initial load, which is when this command gets run.
        try:
            ix = _check_indices(source, dest, meta)
            missing = (ix.get("detail") or {}).get("missing_indices") or []
        except Exception:
            missing = []
        if missing and not as_json:
            self.stdout.write(self.style.WARNING(
                f"warning: {len(missing)} index/indices missing on "
                f"{source.schema_name}.fact — measurements below will be slow "
                f"and the speed-up factors optimistic.\n"
                f"         run `manage.py create_indices --name {source.name}` "
                f"first.\n"))

        # One callback, two consumers: the terminal (suppressed under
        # --json, which must emit nothing but the report) and the run row the
        # GUI polls. The measurement has no other progress to offer.
        def say(message: str) -> None:
            if not as_json:
                self.stdout.write(f"  … {message}")
            rollup_run.progress(meta, run_id, message)

        try:
            report = advise(
                source, dest, meta,
                interval_s=options["interval"],
                window_buckets=options["window_buckets"],
                target_factor=options["target_factor"],
                extrapolate_to=options["extrapolate_to"],
                progress=say,
            )
        except KeyboardInterrupt:
            rollup_run.finish_run(meta, run_id, "failed",
                                  error="killed by user")
            raise
        except Exception as exc:
            rollup_run.finish_run(meta, run_id, "failed", error=str(exc))
            raise
        rollup_run.finish_run(meta, run_id, "ok",
                              report=dataclasses.asdict(report))

        if as_json:
            self.stdout.write(json.dumps(dataclasses.asdict(report),
                                         indent=2, default=str))
            return

        label = interval_label(report.interval_s)
        w = self.stdout.write
        w("")
        w(f"{source.name} ({source.schema_name}) — {label} buckets")
        w(f"  window   {report.window_from:%Y-%m-%d %H:%M} .. "
          f"{report.window_to:%Y-%m-%d %H:%M}  "
          f"({report.buckets_in_window} buckets, "
          f"{_fmt(report.rows_in_window)} rows)")
        w(f"  density  {_fmt(report.rows_per_bucket)} rows/bucket measured "
          f"= {_fmt(report.rows_per_day_measured)} rows/day")
        if abs(report.rows_per_day_assumed - report.rows_per_day_measured) > 1:
            w(f"           projected onto "
              f"{_fmt(report.rows_per_day_assumed)} rows/day")
        if report.saturation_exponent is not None:
            w(f"  fit      saturation exponent "
              f"{report.saturation_exponent:.2f}")
        for note in report.notes:
            w(f"  note     {note}")

        w("")
        w("Dimensions                    distinct   marginal cost   group_by hits")
        for d in sorted(report.dims, key=lambda d: -d.marginal_cost):
            w(f"  {d.column:<26} {d.distinct:>8}   "
              f"{d.marginal_cost:>10.1f}x   {d.gb_hits:>13}")
        w("  (group_by hits come from query_cache, which only holds count(*) "
          "requests —")
        w("   a breakdown used only with a measure shows as 0 here.)")

        # `recommended_stage` is computed in `advise` and travels in the
        # report, so the Rollup tab marks the same line this does. `--stage`
        # overrides the *display* only — the report keeps the recommendation.
        target = report.target_factor
        if options["stage"] is not None:
            best_idx = max(0, min(options["stage"], len(report.stages) - 1))
        else:
            best_idx = report.recommended_stage

        w("")
        w("Candidate column sets")
        w("   #  cols  rows/day        size/day     factor   dropped")
        for i, st in enumerate(report.stages):
            dropped = ", ".join(st.dropped) if st.dropped else "—"
            mark = "<<" if i == best_idx else "  "
            w(f"  {i:>2}  {len(st.columns):>4}  {_fmt(st.rows_per_day):>14}  "
              f"{_size(st.bytes_per_day):>10}  {st.factor:>8.0f}x {mark} {dropped}")
        why = (f"line {best_idx} chosen explicitly"
               if options["stage"] is not None
               else f"widest set reaching factor {int(target)}")
        w(f"  ({why}; dropped columns become marginals, so an *unfiltered* "
          f"breakdown on them stays fast —")
        w("   only a filter on one of them falls back to fact.)")

        w("")
        w("Measures")
        for m in report.measures:
            mark = "yes" if m.derivable else "NO "
            extra = f"+{m.bytes_per_row} B/row" if m.bytes_per_row else m.reason
            w(f"  {m.name:<28} {m.agg:<18} {mark}   {extra}")

        derivable = [m.agg for m in report.measures
                     if m.derivable and m.agg != "count(*)"]
        best = report.stages[best_idx]
        w("")
        w(f"To configure the set on line {best_idx} above:")
        w(f"  manage.py rollup_config --name {source.name} "
          f"--interval {report.interval_s} \\")
        w(f"      --columns {','.join(best.columns)} \\")
        if best.dropped:
            w(f"      --marginals {','.join(best.dropped)} \\")
        w(f"      --measures {','.join(derivable) if derivable else ''}")
        w("")
