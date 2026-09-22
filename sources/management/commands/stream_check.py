"""Measure whether a datasource carries online traffic or batch loads.

    manage.py stream_check --name payments
    manage.py stream_check --name payments --hours 48 --window-days 3
    manage.py stream_check --name payments --from 2026-09-01 --to 2026-09-08
    manage.py stream_check --name payments --json

The gap overlay (`core/query.py:detect_gaps`) calls a silence an outage
when the day profile says more than −ln(α) ≈ 20.76 transactions should
have fallen into it. That is only calibrated while the arrivals are
scattered at random over time. A source fed by a batch job breaks it: the
quiet between two bursts is normal, and every one of them is reported as a
6σ outage.

This command tests that assumption and prints what follows from the
answer — a dispersion factor for the threshold, or a minimum gap length
when the stream turns out to run on a schedule. It writes nothing; see
`docs/stream-character.md` for the method and its limits.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from django.conf import settings
from django.core.management.base import BaseCommand
from sqlalchemy.exc import OperationalError, ProgrammingError

from core import stream_stats as ss
from core.alarm import fact_max_time
from core.query import (GAP_PROFILE_DAYS, GAP_THRESHOLD_EVENTS,
                        gap_day_profile, gap_profile_bounds)
from core.source import Source, _get_cached_engine
from core.utils import _parse_dt


def _ts(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def _p(value) -> str:
    """A p-value an operator can read at a glance.

    `None` is a real value here, not a missing key: a scale too coarse to
    detrend (900 s over 300 s bins is three bins, not a trend) stores None
    rather than omitting the field, and `dict.get(k, default)` hands back
    the stored None rather than the default.
    """
    if value is None or value != value:      # None or NaN
        return "   n/a"
    if value < 1e-99:
        return " <1e-99"
    if value < 0.001:
        return f"{value:7.1e}"
    return f"{value:7.3f}"


class Command(BaseCommand):
    help = ("Characterise a datasource's transaction stream: online traffic, "
            "a batch schedule, irregular bursts or a fixed clock.")

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None, help="datasource name")
        parser.add_argument("--id", type=int, default=None,
                            help="datasource id (alternative to --name)")
        parser.add_argument("--postgres-dsn", default=None,
                            help="warehouse DSN; defaults to the setting")
        parser.add_argument("--hours", type=int,
                            default=ss.MAX_SAMPLE_HOURS_CLI,
                            help=f"hours to sample, spread across the hours of "
                                 f"the day (default {ss.MAX_SAMPLE_HOURS_CLI}). "
                                 "This is the cost knob.")
        parser.add_argument("--window-days", type=float, default=1.0,
                            help="how far back from the watermark to sample "
                                 "(default 1). Ignored when --from is given.")
        parser.add_argument("--from", dest="t_from", default=None,
                            help="window start (ISO 8601, UTC)")
        parser.add_argument("--to", dest="t_to", default=None,
                            help="window end (ISO 8601, UTC)")
        parser.add_argument("--scales", default=None,
                            help="comma-separated bin widths in seconds "
                                 f"(default {','.join(str(s) for s in ss.DEFAULT_SCALES)})")
        parser.add_argument("--json", action="store_true",
                            help="emit the report as JSON")

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

        # A datasource configured but never loaded is an ordinary state, not
        # a crash: `starcubes_<name>.fact` simply is not there yet. The chart
        # endpoint translates that through `_friendly_db_error`; here it would
        # otherwise surface as a raw SQLAlchemy traceback over a missing
        # relation, which says nothing an operator can act on.
        try:
            hwm_dt = fact_max_time(source)
        except (ProgrammingError, OperationalError) as exc:
            detail = str(getattr(exc, "orig", exc)).splitlines()[0]
            self.stderr.write(
                f"Error: cannot read {source.name}'s fact table — {detail}\n"
                f"       Has this datasource been loaded yet? "
                f"(manage.py etl_run --name {source.name})")
            raise SystemExit(1)
        if hwm_dt is None:
            self.stderr.write(f"Error: {source.name} has no rows in fact.")
            raise SystemExit(1)
        hwm = int(hwm_dt.timestamp())

        if options["t_from"]:
            t_from = int(_parse_dt(options["t_from"]).timestamp())
            t_to = (int(_parse_dt(options["t_to"]).timestamp())
                    if options["t_to"] else hwm)
        else:
            t_to = hwm
            t_from = t_to - int(options["window_days"] * 86400)
        t_from, t_to, win_notes = ss.stream_window(source, t_from, t_to, hwm)

        # λ̂(h) is the detector's own profile, anchored at the watermark —
        # never at the sample window. See `stream_window`'s docstring.
        prof_from, prof_to = gap_profile_bounds(
            source, t_to, hwm, GAP_PROFILE_DAYS)
        profile = gap_day_profile(source, prof_from=prof_from,
                                  prof_to=prof_to, dest_engine=dest)
        rate = {int(h): float(r) for h, r in profile["hours"].items()}
        if not any(rate.values()):
            self.stderr.write(
                f"Error: the day profile for {source.name} is empty over "
                f"{_ts(prof_from)} → {_ts(prof_to)} — nothing to measure "
                "against.")
            raise SystemExit(1)

        scales = ss.DEFAULT_SCALES
        if options["scales"]:
            scales = tuple(int(x) for x in options["scales"].split(",") if x.strip())

        report = ss.characterize_stream(
            source, rate_per_s=rate, t_from=t_from, t_to=t_to,
            scales=scales, max_hours=options["hours"], dest_engine=dest)
        report["datasource"] = source.name
        report["window"]["notes"] = win_notes
        report["profile"] = {"from": prof_from, "to": prof_to,
                             "days": profile["days"],
                             "source": profile["source"]}

        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        self._render(report)

    # -- rendering ---------------------------------------------------------

    def _render(self, r: dict) -> None:
        w = self.stdout.write
        win = r["window"]
        smp = r["sampled"]
        w("")
        w(self.style.MIGRATE_HEADING(f"Stream character — {r['datasource']}"))
        w(f"  window    {_ts(win['from'])} → {_ts(win['to'])}"
          + (f"  ({', '.join(win['notes'])})" if win.get("notes") else ""))
        w(f"  sampled   {smp.get('hours_usable', smp['hours'])} of "
          f"{smp['hours']} hours, {smp['rows']:,} rows")
        prof = r.get("profile") or {}
        w(f"  profile   {_ts(prof['from'])} → {_ts(prof['to'])} "
          f"from {prof.get('source')}")
        w("")

        if not r["scales"]:
            for note in r["notes"]:
                w(self.style.WARNING(f"  ! {note}"))
            w("")
            return

        w("  Dispersion — D = 1 is Poisson, D > 1 clumped, D < 1 too regular")
        w("    bin     E[n]    D shape   D detrended   D model     p (detr.)")
        for s in r["scales"]:
            det = s.get("d_detrended")
            mdl = s.get("d_model")
            mark = "  (weak)" if s["weak"] else ""
            w(f"    {s['bin_s']:>4}s {s['mean_expected']:>8.2f} "
              f"{s['d_shape']:>9.2f} "
              f"{(f'{det:>13.2f}' if det is not None else '            -')} "
              f"{(f'{mdl:>9.2f}' if mdl is not None else '        -')} "
              f"{_p(s.get('p_detrended', float('nan')))}{mark}")
        w("")

        z = r.get("zero_bins")
        if z:
            w(f"  Empty bins @{z['bin_s']}s   observed {z['observed']:>7.3%}   "
              f"expected {z['expected']:>7.3%}   z = {z['z']:.1f}")
        ks = r.get("ks")
        if ks:
            w(f"  Uniformity (KS)     D = {ks['d_median']:.4f} (median over "
              f"{ks['hours']} hours)   p = {_p(ks['p']).strip()}")
        per = r.get("periodicity") or {}
        if per:
            if r.get("period_s"):
                w(f"  Periodicity         period {ss._fmt_period(r['period_s'])}"
                  f"   ACF peak {per.get('acf_peak', 0):.2f} at lag "
                  f"{per.get('acf_peak_lag')}   "
                  f"{per['hours_significant']}/{per['hours_tested']} hours")
            else:
                w(f"  Periodicity         none consistent across "
                  f"{per['hours_tested']} hours")
        w("")

        style = (self.style.SUCCESS if r["verdict"] == ss.VERDICT_ONLINE
                 else self.style.WARNING)
        w(style(f"  Verdict: {r['verdict'].replace('_', ' ')}"))
        w(f"    {r['verdict_reason']}")
        w("")

        for note in r["notes"]:
            w(f"  ! {note}")
        if r["notes"]:
            w("")

        factor = r.get("dispersion_factor")
        if factor:
            primary = r.get("primary_recommendation")
            w("  Recommendation")
            star = "→" if primary == "threshold" else " "
            w(f"   {star} threshold   {GAP_THRESHOLD_EVENTS:.1f} × "
              f"{factor:.1f} = {r['recommended_threshold_events']:.0f} expected "
              f"events (read at {r.get('dispersion_factor_scale_s')}s)")
            if r.get("recommended_min_gap_s"):
                w(f"   → min gap     "
                  f"{ss._fmt_period(r['recommended_min_gap_s'])} — a silence of "
                  "one batch period is not an outage")
            if r["verdict"] == ss.VERDICT_ONLINE:
                w("     Nothing to change: the detector is calibrated for this "
                  "stream.")
            elif primary == "min_gap":
                w("     The length floor is the real answer here; the factor "
                  "above is the crude one.")
            w("")
