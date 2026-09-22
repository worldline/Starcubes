"""Measure how much of a datasource's stream arrives as batches.

    manage.py stream_check --name payments
    manage.py stream_check --name payments --window-days 3
    manage.py stream_check --name payments --from 2026-09-01 --to 2026-09-08
    manage.py stream_check --name payments --json

The gap overlay (`core/query.py:detect_gaps`) calls a silence an outage
when the day profile says more than −ln(α) ≈ 20.76 transactions should
have fallen into it. That is only calibrated while the arrivals are
scattered at random over time. A source fed by a batch job breaks it: the
quiet between two bursts is normal, and every one of them is reported as a
6σ outage.

This command tests that assumption and prints what follows from the
answer — the share of rows that arrive in batches and how big they are, a
dispersion factor for the threshold, or a minimum gap length when the
stream turns out to run on a schedule. It writes nothing; see
`docs/stream-character.md` for the method and its limits.

Every scale here is a **μ**, an expected row count, not a number of
seconds: that is what lets the same command say something useful about a
source running a thousand rows a second and about one filtered down to
four hundred rows a day.
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
                        build_filter_tree, gap_day_profile,
                        gap_profile_bounds)
from core.source import Source, _get_cached_engine
from core.utils import _parse_dt


def _ts(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def _p(value) -> str:
    """A p-value an operator can read at a glance.

    `None` is a real value here, not a missing key: a scale with no model
    to compare against stores None rather than omitting the field, and
    `dict.get(k, default)` hands back the stored None rather than the
    default.
    """
    if value is None or value != value:      # None or NaN
        return "   n/a"
    if value < 1e-99:
        return " <1e-99"
    if value < 0.001:
        return f"{value:7.1e}"
    return f"{value:7.3f}"


def _num(value, fmt: str, dash: str = "-") -> str:
    if value is None or value != value:
        return dash
    return format(value, fmt)


def _parse_filters(specs) -> list:
    """`COL=V1,V2` strings into the nested tree `_gap_where` takes.

    The same `in` semantics the chart's filter box has, which is the point:
    a stream character measured behind a different filter than the chart
    draws would be describing a different stream.
    """
    rows = []
    for spec in specs or []:
        if "=" not in spec:
            raise ValueError(
                f"--filter must be COL=VALUE[,VALUE…], got {spec!r}")
        col, _, vals = spec.partition("=")
        col = col.strip()
        values = [v.strip() for v in vals.split(",") if v.strip()]
        if not col or not values:
            raise ValueError(
                f"--filter must be COL=VALUE[,VALUE…], got {spec!r}")
        rows.append({"col": col, "op": "in", "vals": values, "indent": 0})
    return build_filter_tree(rows) if rows else None


def _wall(mu, rate) -> str:
    if not mu or not rate:
        return "-"
    return ss._fmt_period(mu / rate)


class Command(BaseCommand):
    help = ("Characterise a datasource's transaction stream: how much of it "
            "is online traffic, how much arrives as batches, and on what beat.")

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
                                 "This is the cost knob, and it does not apply "
                                 "to a thin stream: below "
                                 f"{ss.CONTIGUOUS_MAX_RATE_PER_S} rows/s the "
                                 "window is scanned whole, which costs nothing "
                                 "and buys a contiguous series.")
        parser.add_argument("--window-days", type=float, default=1.0,
                            help="how far back from the watermark to sample "
                                 "(default 1, widened automatically for a thin "
                                 "stream). Ignored when --from is given.")
        parser.add_argument("--from", dest="t_from", default=None,
                            help="window start (ISO 8601, UTC)")
        parser.add_argument("--to", dest="t_to", default=None,
                            help="window end (ISO 8601, UTC)")
        parser.add_argument("--filter", dest="filters", action="append",
                            default=[], metavar="COL=V1[,V2…]",
                            help="restrict to rows matching a fact column, "
                                 "repeatable and ANDed. Values match a "
                                 "dimension's name or its source id, exactly "
                                 "as the chart's filter box does. This is the "
                                 "normal way to ask the question — a stream is "
                                 "usually one interface or one customer, not "
                                 "the whole source.")
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

        try:
            filter_tree = _parse_filters(options["filters"])
        except ValueError as exc:
            self.stderr.write(f"Error: {exc}")
            raise SystemExit(1)

        # λ̂(h) first, and anchored at the **watermark** — never at the sample
        # window. `stream_window` needs the rate to know whether the stream is
        # thin enough to widen for, so the profile has to exist before the
        # window is decided. See `stream_window`'s docstring for why the two
        # have different clocks.
        prof_from, prof_to = gap_profile_bounds(
            source, hwm, hwm, GAP_PROFILE_DAYS)
        try:
            profile = gap_day_profile(source, prof_from=prof_from,
                                      prof_to=prof_to,
                                      filter_tree=filter_tree,
                                      dest_engine=dest)
        except ValueError as exc:
            self.stderr.write(f"Error: {exc}")
            raise SystemExit(1)
        rate = {int(h): float(r) for h, r in profile["hours"].items()}

        if options["t_from"]:
            t_from = int(_parse_dt(options["t_from"]).timestamp())
            t_to = (int(_parse_dt(options["t_to"]).timestamp())
                    if options["t_to"] else hwm)
        else:
            t_to = hwm
            t_from = t_to - int(options["window_days"] * 86400)
        t_from, t_to, win_notes = ss.stream_window(source, t_from, t_to, hwm,
                                                   rate_per_s=rate)

        report = ss.characterize_stream(
            source, rate_per_s=rate, t_from=t_from, t_to=t_to,
            filter_tree=filter_tree, max_hours=options["hours"],
            dest_engine=dest)
        report["datasource"] = source.name
        report["filters"] = options["filters"]
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
        rate = smp.get("rate_per_s") or 0.0
        w("")
        w(self.style.MIGRATE_HEADING(f"Stream character — {r['datasource']}"))
        w(f"  window    {_ts(win['from'])} → {_ts(win['to'])}"
          + (f"  ({', '.join(win['notes'])})" if win.get("notes") else ""))
        how = ("contiguous" if smp.get("contiguous")
               else "spread over the hours of the day")
        w(f"  sampled   {smp['hours']} hours, {how}, {smp['rows']:,} rows"
          + (f" ({rate:.4g}/s)" if rate else ""))
        if r.get("filters"):
            w(f"  filter    {'  AND  '.join(r['filters'])}")
        prof = r.get("profile") or {}
        if prof:
            w(f"  profile   {_ts(prof['from'])} → {_ts(prof['to'])} "
              f"from {prof.get('source')}")
        w("")

        self._render_headline(r, rate)
        self._render_scales(r, rate)
        self._render_tests(r)

        style = (self.style.SUCCESS if r["verdict"] == ss.VERDICT_ONLINE
                 else self.style.WARNING)
        w(style(f"  Verdict: {r['verdict'].replace('_', ' ')}"))
        if r.get("verdict_reason"):
            w(f"    {r['verdict_reason']}")
        w("")

        for note in r["notes"]:
            w(f"  ! {note}")
        if r["notes"]:
            w("")

        self._render_recommendation(r)

    def _render_headline(self, r: dict, rate: float) -> None:
        w = self.stdout.write
        share = r.get("batch_share")
        if share is None:
            return
        read = r.get("reading") or {}
        w("  Character")
        w(f"    batch share   {share:>7.1%} of rows arrive as part of a batch")
        if r.get("batch_meaningful"):
            w(f"    batch size    {_num(r.get('batch_size'), '7.0f'):>7} rows "
              f"per batch (mean cluster {_num(r.get('cluster_size'), '.2f')})")
        else:
            w(f"    cluster size  {_num(r.get('cluster_size'), '7.2f'):>7} rows "
              "— too small to call a batch")
        if r.get("period_s"):
            w(f"    beat          {ss._fmt_period(r['period_s']):>7}")
        w(f"    read at       μ = {_num(read.get('mu'), '.3g')} "
          f"({_wall(read.get('mu'), rate)} of wall clock) — "
          f"{r.get('reading_reason')}")
        w("")

    def _render_scales(self, r: dict, rate: float) -> None:
        w = self.stdout.write
        scales = r.get("mu_scales") or []
        if not scales:
            return
        w("  Scales — μ is expected rows per bin; D = 1 is Poisson, "
          "> 1 clumped, < 1 too regular")
        w("        μ      wall     bins   D shape  D detr.  D model   "
          "empty obs/exp     E[C]   batch")
        read = r.get("reading")
        for s in scales:
            mark = " <" if s is read else "  "
            w(f"   {s['mu']:>8.3g} {_wall(s['mu'], rate):>8} "
              f"{s['bins']:>8,} "
              f"{_num(s.get('d_shape'), '9.2f'):>9} "
              f"{_num(s.get('d_detrended'), '8.2f'):>8} "
              f"{_num(s.get('d_model'), '8.2f'):>8}   "
              f"{_num(s.get('p0_observed'), '6.1%'):>7}/"
              f"{_num(s.get('p0_poisson'), '<6.1%'):<7} "
              f"{_num(s.get('cluster_size'), '8.2f'):>8} "
              f"{_num(s.get('batch_share'), '6.1%'):>7}{mark}")
        w("")

    def _render_tests(self, r: dict) -> None:
        w = self.stdout.write
        ks = r.get("ks")
        if ks:
            w(f"  Uniformity (KS)  D = {ks['d']:.4f} over {ks['rows']:,} rows"
              f"   p = {_p(ks['p']).strip()}")
        per = r.get("periodicity") or {}
        if per.get("blocks"):
            res = per.get("resolution_s")
            span = (f"{per['blocks']} blocks of "
                    f"{ss._fmt_period(per['block_s'])}")
            if r.get("period_s"):
                w(f"  Periodicity      {ss._fmt_period(r['period_s'])}"
                  f"  (±{res:.3g}s, {span}, in "
                  f"{per.get('block_share', 0):.0%} of them, "
                  f"p = {_p(per.get('p')).strip()})")
            else:
                w(f"  Periodicity      none significant ({span})")
        elif per.get("note"):
            w(f"  Periodicity      {per['note']}")
        w("")

    def _render_recommendation(self, r: dict) -> None:
        w = self.stdout.write
        factor = r.get("dispersion_factor")
        if not factor:
            return
        primary = r.get("primary_recommendation")
        w("  Recommendation")
        star = "→" if primary == "threshold" else " "
        at = _num(r.get("dispersion_factor_mu"), ".3g")
        w(f"   {star} threshold   {GAP_THRESHOLD_EVENTS:.1f} × "
          f"{factor:.2f} = {r['recommended_threshold_events']:.0f} expected "
          f"events (read at μ = {at})")
        if r.get("detector_scale_s"):
            w(f"                  the detector decides at μ = "
              f"{GAP_THRESHOLD_EVENTS:.1f}, which is "
              f"{ss._fmt_period(r['detector_scale_s'])} of silence here")
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
