"""Self-check for `core.stream_stats`: synthetic streams with a known answer.

    manage.py check_stream_stats

No database and no datasource — `core.stream_stats.characterize_counts`
takes arrays and no connection, which is exactly what makes this possible.
Each fixture is a seven-day, one-second series built to a known batch share,
batch size and beat; the command runs the real estimator over it and checks
that what comes back is what went in.

It exists because every number in this module is an inference and most of
them were wrong once. The four that this command would have caught, all
recorded in `docs/stream-character.md`: a smoother that edge-padded a
Λ-wide window and so replicated one busy opening second fifteen thousand
times; a trend charged one degree of freedom per `DETREND_EVENTS` rows
instead of one per smoothing window; a periodogram that reported the
strongest ordinate, which for a comb is an arbitrary harmonic (a clean
300 s schedule read as 50 s); and a detrended expectation that replaced the
day profile instead of multiplying it.

Tolerances are deliberately loose — this is a regression guard, not a
precision claim. The estimator is a moment inversion over a finite sample;
it is meant to say "half of this arrives in batches of a dozen", not to
resolve the dozen to three digits.
"""
from __future__ import annotations

import numpy as np
from django.core.management.base import BaseCommand

from core import stream_stats as ss

DAYS = 7
N = DAYS * 86400
# Midnight-aligned so the hour-of-day profile sees whole days; the estimator
# does not require it, but a fixture that drifts against the day boundary
# would make a failure ambiguous.
T0 = 1700000000 - (1700000000 % 86400)

SHARE_TOL = 0.06          # absolute, on a 0…1 share
SIZE_TOL = 0.20           # relative, on the batch size
PERIOD_TOL = 0.05         # relative, on the beat


def _fixtures():
    """(label, counts, expected) — built once, deterministically seeded."""
    rng = np.random.default_rng(7)
    out = []

    c = np.zeros(N, dtype=np.int64)
    c[::300] = 900
    out.append(("pure batch, 900 every 300 s", c,
                dict(share=1.00, size=900, period=300,
                     verdict=ss.VERDICT_BATCH_REGULAR)))

    out.append(("pure online, 1/s", rng.poisson(1.0, N).astype(np.int64),
                dict(share=0.00, period=None, verdict=ss.VERDICT_ONLINE)))

    c = rng.poisson(0.65, N).astype(np.int64)
    c[::300] += 105
    out.append(("mixed: 35% in batches of 105", c,
                dict(share=0.35, size=105, period=300,
                     verdict=ss.VERDICT_BATCH_REGULAR)))

    c = rng.poisson(0.0033, N).astype(np.int64)
    c[::3600] += 12
    out.append(("thin mixed, ~300/day, hourly batch", c,
                dict(share=0.50, size=12, period=3600,
                     verdict=ss.VERDICT_BATCH_REGULAR)))

    c = np.zeros(N, dtype=np.int64)
    c[::900] = 40
    out.append(("thin batch, 40 every 900 s", c,
                dict(share=1.00, size=40, period=900,
                     verdict=ss.VERDICT_BATCH_REGULAR)))

    out.append(("thin online, 0.005/s", rng.poisson(0.005, N).astype(np.int64),
                dict(share=0.00, period=None, verdict=ss.VERDICT_ONLINE)))
    return out


def _check(report: dict, want: dict) -> list[str]:
    bad = []
    got_share = report.get("batch_share")
    if got_share is None:
        bad.append("no batch share was produced at all")
    elif abs(got_share - want["share"]) > SHARE_TOL:
        bad.append(f"share {got_share:.3f}, wanted {want['share']:.2f} "
                   f"± {SHARE_TOL}")
    if want.get("size"):
        got = report.get("batch_size")
        if got is None or abs(got - want["size"]) / want["size"] > SIZE_TOL:
            bad.append(f"batch size {got}, wanted {want['size']} "
                       f"± {SIZE_TOL:.0%}")
    got_period = report.get("period_s")
    if want.get("period") is None:
        if got_period is not None:
            bad.append(f"found a beat of {got_period:.0f} s where there is none")
    elif got_period is None:
        bad.append(f"no beat found, wanted ~{want['period']} s")
    elif abs(got_period - want["period"]) / want["period"] > PERIOD_TOL:
        bad.append(f"beat {got_period:.1f} s, wanted {want['period']} s "
                   f"± {PERIOD_TOL:.0%}")
    if report.get("verdict") != want["verdict"]:
        bad.append(f"verdict {report.get('verdict')!r}, "
                   f"wanted {want['verdict']!r}")
    return bad


class Command(BaseCommand):
    help = ("Run core.stream_stats over synthetic streams whose batch share, "
            "batch size and beat are known. No database.")

    def handle(self, *args, **options):
        slot_t = np.arange(T0, T0 + N, dtype=np.int64)
        failures = 0
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "core.stream_stats — synthetic fixtures"))
        self.stdout.write(
            "  %-36s   share      size      beat" % "stream")
        for label, counts, want in _fixtures():
            # An empty profile on purpose: these fixtures carry no stored
            # λ̂, which also exercises the blind-profile path the chart hits
            # when a filter matches nothing in the profile week.
            report = ss.characterize_counts(slot_t, counts, {})
            bad = _check(report, want)
            share = report.get("batch_share")
            size = report.get("batch_size")
            period = report.get("period_s")
            line = ("  %-36s %s  %s  %s" % (
                label,
                ("%6.3f" % share) if share is not None else "     –",
                ("%8.1f" % size) if size is not None else "       –",
                ("%8.0fs" % period) if period else "        –"))
            if bad:
                failures += 1
                self.stdout.write(self.style.ERROR(line))
                for b in bad:
                    self.stdout.write(self.style.ERROR(f"      ✗ {b}"))
            else:
                self.stdout.write(self.style.SUCCESS(line))
        self.stdout.write("")
        if failures:
            self.stderr.write(
                f"{failures} of 6 fixtures do not match. See "
                "docs/stream-character.md for what each number means.")
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(
            "All 6 fixtures match: batch share, batch size and beat."))
        self.stdout.write("")
