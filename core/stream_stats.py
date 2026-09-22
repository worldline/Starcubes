"""Stream characterisation: how much of this stream arrives as batches?

`core/query.py:detect_gaps` marks a stretch of silence as an outage when
μ = ∫λ̂ dt exceeds −ln(α) ≈ 20.76 — the one-sided 6σ tail of
P(no event) = e^{−μ}. That arithmetic is only calibrated if the arrivals
inside the gap are **scattered at random over time**. A source fed by a
batch job — a burst every fifteen minutes, silence in between — breaks it:
the silence between two bursts is normal, and the detector calls every one
of them a 6σ outage. The same error hits `count(*) < 1` threshold alarms
with a short `window_s`.

This module answers the question an operator actually asks — *how much of
this stream is online traffic and how much arrives as batches* — and it
answers it at **any rate**, because the filters the chart carries are what
make a stream thin: a source at 1 000 rows/s becomes 450 rows/day the
moment you look at one interface, one customer or one transaction type,
and that is the normal way to arrive here, not an exotic case.

## The axis is Λ, not seconds

Everything here is measured in **expected events**, never in wall-clock
seconds. Time-rescaling is why: with u = Λ(t) = ∫λ̂ dt, an inhomogeneous
Poisson process of intensity λ̂ becomes a homogeneous one of rate 1 in u.
A scale μ is then a pure number, and the same ladder measures a stream at
1 000/s exactly as it measures one at 300/day. The λ̂(h) that `model` is
built from is the very profile `core.query._profile_integral` integrates
for the detector — read here per scan slot at the slot's UTC hour-of-day,
which is the same piecewise-constant function discretised at 1 s — so this
tests the detector's own model rather than a second one built here.

That one decision removes the whole family of rate-dependent constants the
first version of this module carried (a minimum of 60 rows per hour, a
minimum expectation per bin, a cap on which second-widths may vote, a
detrending window in seconds). Each of them independently refused to
measure a thin stream, and each was calibrated against a source running
above 1 row/s.

It also puts the **detector's own scale** inside reach. `detect_gaps`
decides at μ = 20.76; that is 12 s on unfiltered payment traffic and
4 320 s on one transaction type of it. A ladder fixed in seconds cannot
show both, and the one that matters is always the one it cannot reach.

**Every window follows the axis, not just the bin width** — the detrending
window is a count of expected events, the sample minimum is a total, the
periodicity blocks are runs of contiguous scan. A smoothing window of
900 s over a stream of 0.0048/s holds four expected events: the "trend"
then absorbs the signal itself and the dispersion collapses to 0.12, which
measures nothing but the smoother.

## Three expectations, and the differences are the information

μ can be had three ways, and each answers a different question:

- `shape` — λ̂ re-estimated from the **sample's own** hour-of-day profile.
  The conditional test: given what this stretch carried, was it spread the
  way a Poisson process spreads things? Immune to a level error in the
  stored profile (a quiet Sunday is not a batch), and it decides the
  verdict.
- `model` — λ̂(h) as stored, the profile `detect_gaps` actually integrates.
  This answers "does the detector's own model hold", level included.
- `detrended` — a smooth trend through the counts, absorbing whatever
  moves slower than `DETREND_EVENTS` expected events. Dispersion alone
  cannot tell a stream that *clumps* from one that merely *drifts*; this
  is what separates them.

Reporting only `model` would call every ordinary day-to-day swing a batch;
reporting only `shape` would never notice the profile had drifted; without
`detrended` an ordinary intraday ramp reads as a batch job.

## What comes out: a share, not a table

The stream is read as a cluster process — clusters arrive Poisson, each
carries C rows. Two statistics already in hand identify C's first two
moments (`cluster_estimate`), and the two-point inversion turns them into
the two numbers an operator can act on: **what share of rows arrives as a
batch, and how big the batches are.** A table of dispersion values at
hard-wired second-widths is the derivation, not the answer.

No scipy: it is not in `requirements.txt` and does not belong there for
five formulas. `numpy` arrives with pandas and is already used in
`core/audit.py`. No Django imports, like everything under `core/`.

See `docs/stream-character.md` for the derivation and the limits.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional, Sequence

import numpy as np
from sqlalchemy import text

from .query import GAP_THRESHOLD_EVENTS, _gap_where
from .source import Source

log = logging.getLogger(__name__)

# The scan granularity. Fixed at 1 s like `GAP_SCAN_INTERVAL_S`, and for the
# same reason: it is the finest resolution the fact table can answer, and
# every coarser scale is a sum over these counts rather than another query.
#
# It is also **the one rate dependence that survives the Λ axis**, and it is
# a real one rather than a calibration: a cluster is only resolvable if a bin
# can be narrower than it, so the finest honest scale is μ_floor = λ·1 s. On
# unfiltered payment traffic (1.68/s) that is μ ≈ 3.4 — clusters below three
# rows cannot be seen; on one transaction type of it (0.0048/s) there is
# effectively no floor at all. Below the floor the binning degenerates
# (measured: D = 30.9 with 15% empty bins against 78% expected), so the
# ladder starts above it and the report says where it started.
SCAN_INTERVAL_S = 1
MU_FLOOR_FACTOR = 2.0

# The μ ladder: geometric, √2 per step, from the resolution floor upwards.
# It is not a list of constants because μ_floor depends on the rate; what is
# fixed is the *shape* of the ladder, which is what makes two streams at
# different rates comparable row by row.
MU_LADDER_STEP = math.sqrt(2.0)
MU_LADDER_MAX_STEPS = 34

# A scale needs this many bins before its dispersion means anything, and a
# bin needs at least one scan slot or the binning is below the resolution
# floor. Right at the floor a bin is about two slots wide, so an intensity
# that varies at all will collapse a few percent of them onto a single
# slot; those carry no expectation and drop out of every sum, which is why
# a few percent is tolerable and a fixed 1% was not (it rejected the one
# scale that estimated a known 35% batch share exactly).
MIN_BINS = 100
BIN_DEGENERATE_SHARE = 0.05

# The empty-bin share is how E[C] is read (see `cluster_estimate`), and
# what it has to be is **precise**, not mid-range. A fixed band cannot
# express that: a heavily batched stream legitimately leaves 99.3% of its
# bins empty, and with a hundred thousand bins that reads E[C] to within
# 4% — rejecting it as "too close to 1" threw away the only scales where
# the estimate was right. So the test is on the standard error of
# −ln p̂₀ itself, which is what the estimate actually divides by.
LOG_P0_MAX_RSE = 0.10

# The reading is taken at the **plateau** of the estimate, not at the first
# scale that happens to be usable. A bin must be wider than the burst for a
# cluster to count as one object; below that the burst is split across bins
# and the share reads low. Measured on a synthetic 35%-in-batches-of-105
# stream, the share runs 0.352 / 0.364 / 0.372 over three steps — flat, and
# right; on a thin stream it was still climbing (0.38 → 0.44 → 0.49) and the
# ladder has to go further up before it settles.
PLATEAU_TOL = 0.05

# The detrending window, in **expected events**. Removing a trend of this
# width turns dispersion into a high-pass test: variation slower than the
# window is the ramp and is not the question, variation faster is clumping
# and is. A batch beat well inside the window survives untouched, which is
# what makes the two separable at all.
DETREND_EVENTS = 200.0
DETREND_MIN_SLOTS = 15
# ...and never narrower than this in wall clock, which is the half of the
# rule the Λ axis does **not** replace. A window has to be wide compared to
# the batch beat it must leave alone: 200 expected events on a stream of
# 1/s is 200 s, and smoothing a 300 s comb over 200 s tracks the comb
# instead of the trend — measured, that pulled a known 35% batch share up
# to 40% and the batch size down from 105 to 92. The Λ rule exists because
# 900 s is too *short* at low rates, not because it is too long at high
# ones, so both bounds apply and the wider wins.
DETREND_MIN_WINDOW_S = 900

# Below this the sample says nothing at all — not per hour, over the whole
# measured window, which is the entire point of the rewrite.
MIN_EVENTS_TOTAL = 200

# The measuring window. A day is the floor because the hour-of-day profile
# needs several hours before it means anything; seven days is the cap.
MIN_WINDOW_S = 86400
MAX_WINDOW_S = 7 * 86400

# Sampled hours, not scanned seconds, is the cost knob. The web path is
# bounded well below uwsgi's `harakiri = 120` (which kills the whole worker,
# taking its other in-flight requests with it); the CLI hangs off no worker.
MAX_SAMPLE_HOURS_WEB = 24
MAX_SAMPLE_HOURS_CLI = 168

# A stream this thin is scanned **contiguously** over the whole window
# instead of sampled hour by hour: at 450 rows/day seven days are some three
# thousand rows and one `GROUP BY`, so sampling buys nothing and costs the
# contiguity that the periodicity search and the detrender both want.
CONTIGUOUS_MAX_RATE_PER_S = 0.5

# Periodicity. The search is a periodogram read against the **absolute**
# Poisson noise floor, summed incoherently over contiguous blocks — never
# Fisher's g = max/Σ, which is what the first implementation got wrong: a
# batch is a near-Dirac comb, a comb spreads its power over every harmonic
# (a 300 s beat in a 3600 s hour lights up 150 ordinates), so a statistic
# normalised by the total power collapses to ~1/150 and lands the sharpest
# possible schedule at p ≈ 0.01. Against a known noise floor the fundamental
# still stands out by orders of magnitude, because nothing divides it by the
# power the harmonics took.
# The longest beat that can be claimed. The old 1800 s came from every test
# running inside one hour, where anything approaching that was the window
# describing itself; blocks are up to a day now, so the real constraint is
# `block / 4` (enforced below) and this is only the outer bound. An hourly
# batch is an entirely ordinary schedule and used to be unreportable.
MAX_PERIOD_S = 6 * 3600
PERIOD_BLOCK_MAX_S = 86400
MIN_PERIOD_BLOCK_S = 1800
# A period is only called one when the blocks agree on it, not merely when
# one block finds a peak.
PERIOD_MIN_BLOCK_SHARE = 0.5
PERIOD_ALPHA = 1e-4
# The comb bites a second time here, and from the other side. Against an
# absolute noise floor every harmonic of a batch beat is wildly
# significant, so the *strongest* ordinate is an arbitrary one of them —
# measured, a clean 300 s schedule reported 50 s (the 6th harmonic) and an
# hourly one reported 8.4 s. The fundamental is the **lowest** significant
# frequency, and it is only believable if the rest of the significant set
# sits on its multiples; that check is what separates a comb from a few
# unrelated peaks.
PERIOD_HARMONIC_SHARE = 0.6

# A "batch" has to be big enough to mean the word. Near B = 1 the two-point
# inversion divides by (E[C] − 1) and swings wildly on noise — measured on
# unfiltered payment traffic, the share bounced 0.47 / 0.35 / 0.26 / 0.38
# across four neighbouring scales while E[C] sat calmly at 1.14…1.25. What
# that stream actually has is *pairs*: two rows inside the two seconds the
# 1-second scan can just about resolve, which is a coincidence rather than
# a batch job. So the share only carries a verdict once the clusters are
# this big, and E[C] is what gets reported below it.
MIN_MEANINGFUL_BATCH = 3.0

# Verdict bands on the batch share. The codes are unchanged — the CLI and
# the chart both key on them — but they are read off a share now instead of
# off a dispersion threshold.
BATCH_SHARE_ONLINE = 0.05
BATCH_SHARE_MOSTLY = 0.30
BATCH_SHARE_HEAVY = 0.70
# Under-dispersion: more regular than chance, which makes the detector
# conservative rather than wrong.
CLOCKED_D_MAX = 0.7

VERDICT_ONLINE = "online"
VERDICT_BATCH_REGULAR = "batch_regular"
VERDICT_BURSTY = "bursty"
VERDICT_CLOCKED = "clocked"
VERDICT_UNKNOWN = "unknown"


# ── p-values, without scipy ────────────────────────────────────────────────

def _norm_sf(z: float) -> float:
    """P(Z > z) for a standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _chi2_sf(x: float, df: int) -> float:
    """P(χ²_df > x).

    Exact for an even `df` up to 200 — the closed form is a finite series,
    and that is also the shape `Gamma(n)` takes for the periodicity sum
    (df = 2n for n pooled blocks), precisely the range where
    Wilson–Hilferty is at its worst. Everything else uses Wilson–Hilferty,
    which is excellent for the large df the dispersion statistic produces.
    """
    if df <= 0 or not math.isfinite(x):
        return float("nan")
    if x <= 0:
        return 1.0
    if df % 2 == 0 and df <= 200:
        m = df // 2
        half = x / 2.0
        term = 1.0
        total = 1.0
        for k in range(1, m):
            term *= half / k
            total += term
        return min(1.0, max(0.0, math.exp(-half) * total))
    t = (x / df) ** (1.0 / 3.0)
    mean = 1.0 - 2.0 / (9.0 * df)
    sd = math.sqrt(2.0 / (9.0 * df))
    return min(1.0, max(0.0, _norm_sf((t - mean) / sd)))


def _kolmogorov_q(lam: float) -> float:
    """Q(λ) = 2 Σ_{k≥1} (−1)^{k−1} e^{−2k²λ²} — the KS tail."""
    if lam <= 0:
        return 1.0
    if lam < 0.2:            # the series is useless here; the tail is 1
        return 1.0
    total = 0.0
    for k in range(1, 101):
        term = math.exp(-2.0 * k * k * lam * lam)
        total += (-1.0) ** (k - 1) * term
        if term < 1e-15:
            break
    return min(1.0, max(0.0, 2.0 * total))


def _bonferroni(p: float, trials: int) -> float:
    """The chance of seeing `p` or better anywhere in `trials` searches."""
    if not math.isfinite(p) or trials <= 1:
        return p
    if p * trials < 1e-6:                    # the regime that matters
        return p * trials
    return 1.0 - (1.0 - p) ** trials


def _fmt_period(seconds: float) -> str:
    """A period as an operator would say it."""
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


# ── the measuring window and what gets scanned ─────────────────────────────

def mean_rate(rate_per_s: dict[int, float]) -> float:
    """Mean rows per second implied by the day profile, 0 if it is blind."""
    vals = [float(v) for v in rate_per_s.values() if v and v > 0]
    if not vals:
        return 0.0
    return sum(float(rate_per_s.get(h, 0.0) or 0.0) for h in range(24)) / 24.0


def plan_scan(
    rate_per_s: dict[int, float],
    t_from: int,
    t_to: int,
    *,
    max_hours: int,
) -> tuple[list[int], bool]:
    """Which whole hours to scan. Returns (hour_starts, contiguous).

    Two regimes, and the rate decides which — **not** a branch in the
    statistics, only in what gets read off disk. Above
    `CONTIGUOUS_MAX_RATE_PER_S` the scan is sampled round-robin across
    hours-of-day, newest first within each, so a budget of six hours buys
    six different times of day rather than six consecutive ones: a batch
    beat that is obvious at 03:00 is invisible inside midday traffic.
    Below it the whole window is read contiguously, because at 450
    rows/day seven days are three thousand rows and one `GROUP BY` —
    sampling would buy nothing and would cost the contiguity that the
    detrender and the periodicity blocks both want.

    An hour whose hour-of-day carries no traffic in the profile is skipped
    in the sampled regime for the same reason the detector never marks one.
    If the profile is blind (every λ̂ zero — a filter matching nothing in
    the seven days before the watermark) every hour is a candidate: `shape`
    and `detrended` normalise to the sample's own totals and never read λ̂,
    so the verdict survives its absence and only the scan gets wider.

    Deterministic on purpose: the CLI and the chart must be able to report
    the same number for the same window, and a random sample could not.
    """
    if t_to <= t_from:
        return [], False
    first = ((t_from + 3599) // 3600) * 3600
    last = (t_to // 3600) * 3600            # exclusive end of the last full hour
    all_hours = list(range(first, last, 3600))
    if not all_hours:
        return [], False

    if 0.0 < mean_rate(rate_per_s) <= CONTIGUOUS_MAX_RATE_PER_S:
        # Bounded by the window cap rather than by `max_hours`: the cost
        # knob exists for busy sources, and applying it here would throw
        # away exactly the rows a thin stream has too few of.
        return all_hours[-(MAX_WINDOW_S // 3600):], True

    blind = not any(v and v > 0 for v in rate_per_s.values())
    by_hod: dict[int, list[int]] = {}
    for start in all_hours:
        hod = (start % 86400) // 3600
        if not blind and (rate_per_s.get(hod, 0.0) or 0.0) <= 0:
            continue
        by_hod.setdefault(hod, []).append(start)
    for starts in by_hod.values():
        starts.sort(reverse=True)           # newest first: the current regime
    picked: list[int] = []
    order = sorted(by_hod)
    round_i = 0
    while len(picked) < max_hours:
        added = False
        for hod in order:
            starts = by_hod[hod]
            if round_i < len(starts):
                picked.append(starts[round_i])
                added = True
                if len(picked) >= max_hours:
                    break
        if not added:
            break
        round_i += 1
    picked.sort()
    return picked, len(picked) == len(all_hours)


def stream_window(
    source: Source,
    t_from: Optional[int],
    t_to: Optional[int],
    hwm: Optional[int],
    *,
    rate_per_s: Optional[dict[int, float]] = None,
) -> tuple[int, int, list[str]]:
    """The measuring window, clamped and widened. Returns (from, to, notes).

    **This is the authority, not the client.** The chart proposes a window
    from its viewport, but a request may ask for anything, and a window
    below a day cannot be measured honestly: the hour-of-day profile has to
    be spread over hours-of-day or a beat that is plain at three in the
    morning stays invisible inside midday traffic.

    A **thin** stream is widened all the way to `MAX_WINDOW_S`, because the
    sample minimum is a count of rows and a day of a thin stream does not
    reach it. That costs nothing at those rates and is the difference
    between an answer and a refusal.

    Note the asymmetry at the fresh edge: a window that runs past the high
    watermark is **moved back**, not cut short. Cutting it would hand back
    less than a day of data exactly when the operator is looking at the
    newest traffic, which is the most common way to arrive here.

    This is also where the split from the day profile lives. λ̂(h) is
    anchored at the watermark by `gap_profile_bounds` and does *not* follow
    the viewport — the detector's model must stay the same wherever it is
    looked at, or the same gap scores differently depending on the slice
    that was asked for. The **sample** may follow the viewport, because
    which stretch of the stream to characterise is a legitimate question.
    """
    notes: list[str] = []
    floor = getattr(source, "retention_floor", None)
    floor_epoch = None
    if floor is not None:
        if floor.tzinfo is None:
            floor = floor.replace(tzinfo=timezone.utc)
        floor_epoch = int(floor.timestamp())

    upper = hwm if hwm is not None else (t_to or 0)
    if t_to is None:
        t_to = upper
    if t_from is None:
        t_from = t_to - MIN_WINDOW_S

    t_from, t_to = int(t_from), int(t_to)
    if t_to > upper:
        t_to = upper
        notes.append("clipped to the high watermark")

    want = MIN_WINDOW_S
    rate = mean_rate(rate_per_s or {})
    thin = 0.0 < rate <= CONTIGUOUS_MAX_RATE_PER_S
    if thin and t_to - t_from < MAX_WINDOW_S:
        want = MAX_WINDOW_S

    if t_to - t_from > MAX_WINDOW_S:
        t_from = t_to - MAX_WINDOW_S
        notes.append(f"limited to {MAX_WINDOW_S // 86400} days")
    elif t_to - t_from < want:
        if thin:
            t_from = t_to - want             # a thin stream needs the length
            notes.append(f"widened to {want // 86400} days — the stream is "
                         "thin and a shorter window holds too few rows")
        else:
            mid = (t_from + t_to) // 2
            t_from = mid - want // 2
            t_to = mid + want // 2
            notes.append("widened to one day")
        if t_to > upper:                     # move back, never cut short
            t_from -= t_to - upper
            t_to = upper

    if floor_epoch is not None and t_from < floor_epoch:
        t_from = floor_epoch
        # The floor supersedes a length cap applied a moment ago: saying
        # "limited to 7 days" about a window that ended up 48 hours long is
        # worse than saying nothing, and these notes are read by a person
        # wondering why the answer is not about what they were looking at.
        notes = [n for n in notes if not n.startswith(("limited to", "widened"))]
        notes.append("raised to the retention floor")
        if t_to - t_from < MIN_WINDOW_S:
            t_to = min(upper, t_from + MIN_WINDOW_S)
            notes.append("less than a day is available above the floor")
    return t_from, max(t_to, t_from), notes


def bucket_counts(
    source: Source,
    *,
    hour_starts: Sequence[int],
    filter_tree=None,
    filter_groups=None,
    interval_s: int = SCAN_INTERVAL_S,
    statement_timeout_ms: Optional[int] = None,
    dest_engine=None,
) -> tuple[np.ndarray, np.ndarray]:
    """Second-resolution counts for `hour_starts`. Returns (slot_t, counts).

    One query for all of them — `include_time_range=False` on `_gap_where`
    plus the hour ranges, the same shape `gap_day_profile` uses to bring its
    own window. A contiguous run becomes one `BETWEEN` rather than an OR
    chain of 168 terms; sharing `_gap_where` is what keeps this test behind
    exactly the filters the chart, the scan and the profile use, since a
    second filter builder here would eventually describe a different stream
    than the one the detector runs on.

    Both arrays are **dense and time-ordered**: `slot_t[i]` is the epoch
    second the slot starts at, `counts[i]` what fell into it, zeros
    included. The empty bins are half the finding, and a `GROUP BY` alone
    would drop every one of them.

    **There is deliberately no `_utc_ts` here**, and it is not an omission.
    That helper exists because `extract(hour from …)` over a `timestamptz`
    is evaluated in the *session* time zone, so `gap_day_profile` has to
    force the frame before grouping. This query groups on
    `extract(epoch from …)`, which is Unix seconds and carries no time zone
    at all; the hour-of-day is then derived in Python as `(sec % 86400) //
    3600`, which is UTC by construction and the same convention
    `_profile_integral` reads λ̂ with. The gap scan groups the same way, for
    the same reason. Adding a conversion here would not fix anything and
    would be one more place for the two frames to drift apart.
    """
    hours = sorted(int(h) for h in hour_starts)
    if not hours:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    pc = source.partition_column
    per_hour = 3600 // interval_s

    slot_t = np.concatenate([
        np.arange(h, h + 3600, interval_s, dtype=np.int64) for h in hours])
    counts = np.zeros(len(slot_t), dtype=np.int64)

    flt_parts, params = _gap_where(
        source, filter_tree=filter_tree, filter_groups=filter_groups,
        include_time_range=False)

    contiguous = all(b - a == 3600 for a, b in zip(hours, hours[1:]))
    if contiguous:
        ranges = [f"(f.{pc} >= :h0a AND f.{pc} < :h0b)"]
        params["h0a"] = datetime.fromtimestamp(hours[0], tz=timezone.utc)
        params["h0b"] = datetime.fromtimestamp(hours[-1] + 3600,
                                               tz=timezone.utc)
    else:
        ranges = []
        for i, start in enumerate(hours):
            ranges.append(f"(f.{pc} >= :h{i}a AND f.{pc} < :h{i}b)")
            params[f"h{i}a"] = datetime.fromtimestamp(start, tz=timezone.utc)
            params[f"h{i}b"] = datetime.fromtimestamp(start + 3600,
                                                      tz=timezone.utc)
    where = [f"({' OR '.join(ranges)})"] + flt_parts

    sql = f"""
        SELECT floor(extract(epoch from f.{pc}) / :ival)::bigint AS slot,
               count(*) AS n
          FROM {source.schema_name}.fact f
         WHERE {" AND ".join(where)}
         GROUP BY 1
    """
    params["ival"] = interval_s

    with (dest_engine or source.dest_engine()).connect() as conn:
        if statement_timeout_ms:
            conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"),
                         {"ms": str(int(statement_timeout_ms))})
        rows = conn.execute(text(sql), params).mappings().all()

    base = {h: i * per_hour for i, h in enumerate(hours)}
    for row in rows:
        sec = int(row["slot"]) * interval_s
        off = base.get((sec // 3600) * 3600)
        if off is None:                      # a row outside every asked range
            continue
        counts[off + (sec % 3600) // interval_s] = int(row["n"])
    return slot_t, counts


def contiguous_runs(slot_t: np.ndarray, interval_s: int = SCAN_INTERVAL_S
                    ) -> list[tuple[int, int]]:
    """Index ranges of `slot_t` that are unbroken in wall-clock time.

    The sample is a union of whole hours, which may or may not touch. Every
    test here is legitimate across a break — after rescaling, a Poisson
    process restricted to a disjoint union of intervals is still a rate-1
    process on [0, Λ_tot] — but two of them care anyway: the detrender must
    not smooth 03:00 into 14:00, and the periodicity search needs a real
    span to resolve a period against.
    """
    if slot_t.size == 0:
        return []
    breaks = np.nonzero(np.diff(slot_t) != interval_s)[0]
    edges = [0, *(int(b) + 1 for b in breaks), int(slot_t.size)]
    return [(a, b) for a, b in zip(edges, edges[1:]) if b > a]


# ── the Λ axis ─────────────────────────────────────────────────────────────

def _smooth(x: np.ndarray, win: int) -> np.ndarray:
    """Moving average of `x` over `win` slots, length kept.

    Via `cumsum`, not `convolve`: the window is a count of *expected
    events*, so on a thin stream it is tens of thousands of slots wide and
    a naive convolution is O(len·win) — measured, that ran into the
    prototype's timeout on a single 7-day series.

    The window **shrinks at the edges** rather than the series being
    padded, and that is not a detail. Edge-padding replicates the first
    and last value `win//2` times; at the 900 s window this module used to
    run on that was a mild distortion, but a Λ-sized window is tens of
    thousands of slots wide, and one busy opening second then became a
    15 000-fold repeat — measured, it lifted the intensity to 6.0 rows/s
    on a stream carrying 0.0065 and inflated the detrended dispersion from
    7 to 51. A partial window at the edge is unbiased and costs nothing.
    """
    n = len(x)
    if n == 0:
        return x.astype(float)
    win = max(1, int(win))
    if win % 2 == 0:
        win += 1
    if win <= 1 or win >= n:
        return np.full(n, float(x.mean()))
    pad = win // 2
    cs = np.concatenate(([0.0], np.cumsum(x.astype(float))))
    i = np.arange(n)
    lo = np.clip(i - pad, 0, n)
    hi = np.clip(i + pad + 1, 0, n)
    return (cs[hi] - cs[lo]) / (hi - lo)


def build_expectations(
    slot_t: np.ndarray,
    counts: np.ndarray,
    rate_per_s: dict[int, float],
    *,
    runs: Sequence[tuple[int, int]],
) -> tuple[dict[str, np.ndarray], dict]:
    """The three per-slot intensities λ(t) the whole module rests on.

    Returns `({"shape": …, "model": …, "detrended": …}, info)`, each
    intensity an array of expected rows per slot parallel to `counts`.
    `model` is missing when the stored profile is blind — everything else
    still works, because only `model` reads λ̂. `info` carries what the
    expectations *cost*, which the dispersion has to subtract.

    `shape` is the sample's own hour-of-day profile. That is what makes the
    test **conditional**: it asks whether this stretch was spread the way a
    Poisson process spreads things, not whether it matched a profile
    measured somewhere else, so an ordinary quiet Sunday cannot read as a
    batch job.

    `detrended` smooths **within each contiguous run** over a window of
    `DETREND_EVENTS` expected rows. Both halves of that matter: smoothing
    across a break would mix 03:00 into 14:00, and a window fixed in
    seconds holds four expected events on a thin stream, where the trend
    stops being a trend and becomes the data (measured: D collapses to
    0.12, which describes the smoother and nothing else).
    """
    out: dict[str, np.ndarray] = {}
    info: dict = {"detrend_window_s": None, "df": {"shape": 24.0,
                                                   "model": 0.0}}
    total = float(counts.sum())
    n_slots = len(counts)
    if n_slots == 0 or total <= 0:
        return out, info

    hod = ((slot_t % 86400) // 3600).astype(np.int64)
    shape = np.zeros(n_slots, dtype=float)
    for h in range(24):
        m = hod == h
        k = int(m.sum())
        if k:
            shape[m] = float(counts[m].sum()) / k
    # A slot whose hour-of-day carried nothing in the sample would make Λ
    # flat there and put an infinitely wide bin over it. The floor is small
    # enough to change no answer and large enough to keep Λ strictly
    # increasing, which every searchsorted below relies on.
    lam_bar = total / n_slots
    shape = np.maximum(shape, lam_bar * 1e-6)
    shape *= total / shape.sum()
    out["shape"] = shape

    if any(v and v > 0 for v in rate_per_s.values()):
        model = np.array([rate_per_s.get(int(h), 0.0) or 0.0 for h in hod],
                         dtype=float)
        if model.sum() > 0:
            out["model"] = np.maximum(model, lam_bar * 1e-6)

    win = max(DETREND_MIN_WINDOW_S // SCAN_INTERVAL_S,
              int(DETREND_EVENTS / lam_bar) if lam_bar > 0 else 0)
    det = np.empty(n_slots, dtype=float)
    ok = False
    for a, b in runs:
        seg = counts[a:b]
        if len(seg) >= max(DETREND_MIN_SLOTS, 3):
            # **The trend multiplies the day profile, it does not replace
            # it.** A plain moving average over a Λ-sized window is many
            # hours wide on a thin stream, so it irons the hour-of-day
            # shape flat and then reports the missing shape as clumping —
            # measured, that alone put D = 2.0 at the scale the detector
            # decides on, on a stream that is Poisson there. Fitting only
            # the slow *level* on top of `shape` keeps the hours and still
            # absorbs the drift, which is all the detrender was ever for.
            ref = shape[a:b]
            lvl = _smooth(seg, win) / np.maximum(_smooth(ref, win),
                                                 lam_bar * 1e-6)
            det[a:b] = ref * lvl
            ok = True
        else:
            det[a:b] = float(seg.mean()) if len(seg) else 0.0
    if ok:
        det = np.maximum(det, lam_bar * 1e-6)
        det *= total / det.sum()
        out["detrended"] = det
        info["detrend_window_s"] = win * SCAN_INTERVAL_S
        # **The trend costs one parameter per smoothing window, not one per
        # `DETREND_EVENTS` rows.** Those two are the same number only when
        # the window happens to hold that many rows; on unfiltered payment
        # traffic the first reading was 715 against 646 bins, which drove
        # the dof to 1 and reported D = 2945 for a stream sitting at 9.
        info["df"]["detrended"] = max(1.0, n_slots / float(max(win, 1)))
    return out, info


def equal_mu_bins(lam_edges: np.ndarray, mu: float) -> Optional[np.ndarray]:
    """Slot indices cutting the sample into bins of expectation `mu`.

    The bins are equal in Λ, not in seconds — that is the whole point of
    the axis. Returns the K+1 boundary indices, or None when fewer than
    `MIN_BINS` bins fit.
    """
    lam_cum = np.concatenate(([0.0], np.cumsum(lam_edges)))
    total = float(lam_cum[-1])
    if mu <= 0 or total <= 0:
        return None
    k = int(total // mu)
    if k < MIN_BINS:
        return None
    idx = np.searchsorted(lam_cum, np.arange(k + 1) * mu, side="left")
    return np.clip(idx, 0, len(lam_edges))


def _binned(counts_cum: np.ndarray, lam: np.ndarray, idx: np.ndarray
            ) -> tuple[np.ndarray, np.ndarray]:
    """(observed, expected) per bin for one intensity, on given edges."""
    lam_cum = np.concatenate(([0.0], np.cumsum(lam)))
    n = counts_cum[idx[1:]] - counts_cum[idx[:-1]]
    mu = lam_cum[idx[1:]] - lam_cum[idx[:-1]]
    return n.astype(float), mu


def dispersion(n: np.ndarray, mu: np.ndarray, *, df_cost: float = 0.0
               ) -> tuple[float, float, int]:
    """X² = Σ (n−μ)²/μ, its dof and D = X²/df.

    `df_cost` is what the expectation itself cost: 24 for the hour-of-day
    profile `shape` fits, one per independent smoothing window for
    `detrended`, nothing at all for `model`, which was measured elsewhere.
    Bins with no expectation — a zero-width bin below the resolution
    floor — drop out on `mu > 0`.
    """
    with np.errstate(invalid="ignore"):
        ok = mu > 0
    live = int(ok.sum())
    if live < 2:
        return float("nan"), float("nan"), 0
    resid = n[ok] - mu[ok]
    chi2 = float(np.sum(resid * resid / mu[ok]))
    df = max(1, live - int(min(df_cost, live - 1)))
    return chi2 / df, chi2, df


def _log_p0_rse(p0: float, bins: int) -> Optional[float]:
    """Relative standard error of −ln p̂₀ — the quantity E[C] divides by.

    p̂₀ is a binomial proportion over `bins`, so SE(p̂₀) = √(p₀(1−p₀)/K) and
    the delta method carries it through the logarithm as √((1−p₀)/(K p₀)).
    """
    if bins < 2 or not (0.0 < p0 < 1.0):
        return None
    denom = -math.log(p0)
    if denom <= 0:
        return None
    return math.sqrt((1.0 - p0) / (bins * p0)) / denom


def cluster_estimate(d: float, p0: float, mu: float) -> dict:
    """Batch share and batch size, from the first two moments of C.

    Read the stream as a cluster process: clusters arrive Poisson, each
    carries C rows. Two statistics that are already in hand pin down C's
    first two moments, and neither needs a new query:

    - the **empty-bin share**. The *clusters* in a bin are Poisson with
      mean μ/E[C], so P(bin empty) = e^{−μ/E[C]} and E[C] = μ / −ln p̂₀.
    - the **dispersion**. For a compound Poisson count,
      D = Var/Mean = E[C²]/E[C].

    With a two-point cluster size — a share of clusters carries B rows, the
    rest carry one — those two moments identify the model exactly:

        m₁ = E[C],  m₂ = D·m₁
        B = (m₂−1)/(m₁−1) − 1,  p = (m₁−1)/(B−1),  f_batch = p·B/m₁

    `f_batch` is the number the operator asked for: **the share of rows
    that arrive as part of a batch.** Verified against synthetic streams
    with a known answer — a pure batch of 900 every 300 s reads
    f = 1.00 / B = 908, a 35%-in-batches-of-105 mixture reads
    f = 0.352 / B = 105.5, pure Poisson reads f = 0.00.

    Two points to watch, both handled by the caller. The dispersion must be
    the **detrended** one, or a drifting rate is counted as clustering; and
    the bin must be wider than the burst, which is what the plateau rule in
    `pick_reading` is for.
    """
    out = {"cluster_size": None, "batch_size": None, "batch_share": None}
    if not (math.isfinite(d) and 0.0 < p0 < 1.0 and mu > 0):
        return out
    m1 = mu / -math.log(p0)
    if not math.isfinite(m1) or m1 <= 0:
        return out
    out["cluster_size"] = m1
    m2 = d * m1
    if m1 <= 1.02 or not math.isfinite(m2):
        out["batch_size"], out["batch_share"] = 1.0, 0.0
        return out
    b = (m2 - 1.0) / (m1 - 1.0) - 1.0
    if not math.isfinite(b) or b <= 1.02:
        out["batch_size"], out["batch_share"] = 1.0, 0.0
        return out
    p = (m1 - 1.0) / (b - 1.0)
    out["batch_size"] = b
    out["batch_share"] = max(0.0, min(1.0, p * b / m1))
    return out


# ── the ladder and where to read it ────────────────────────────────────────

def mu_ladder(counts: np.ndarray, lams: dict[str, np.ndarray],
              *, mu_floor: float, df_costs: Optional[dict] = None
              ) -> list[dict]:
    """One row per scale, ascending in μ, from the resolution floor up.

    **One binning per row, shared by all three expectations.** The edges
    come from `detrended` (the best local rate estimate available) and the
    other two are integrated over those same edges, so the three D values
    in a row describe the same bins and can be subtracted from each other —
    which is exactly what the drift note does.
    """
    if counts.size == 0 or counts.sum() <= 0:
        return []
    counts_cum = np.concatenate(([0.0], np.cumsum(counts.astype(float))))
    edge_kind = "detrended" if "detrended" in lams else "shape"
    lam_edges = lams[edge_kind]
    df_costs = dict(df_costs or {"shape": 24.0, "model": 0.0})

    entries: list[dict] = []
    mu = max(mu_floor, 1e-9)
    for _ in range(MU_LADDER_MAX_STEPS):
        idx = equal_mu_bins(lam_edges, mu)
        if idx is None:
            break
        width = np.diff(idx)
        degenerate = float(np.mean(width <= 0))
        n_edge, mu_edge = _binned(counts_cum, lam_edges, idx)
        live = mu_edge > 0
        entry: dict = {
            "mu": float(mu),
            "bins": int(len(width)),
            "degenerate_share": degenerate,
        }
        for kind, lam in lams.items():
            n, mu_i = _binned(counts_cum, lam, idx)
            d, chi2, df = dispersion(n, mu_i, df_cost=df_costs.get(kind, 0.0))
            entry[f"d_{kind}"] = d if math.isfinite(d) else None
            entry[f"p_{kind}"] = _chi2_sf(chi2, df) if math.isfinite(d) else None
        if live.any():
            p0 = float(np.mean(n_edge[live] == 0))
            mu_bar = float(np.mean(mu_edge[live]))
            entry["p0_observed"] = p0
            entry["p0_poisson"] = math.exp(-mu_bar)
            entry["mu_effective"] = mu_bar
            clump = entry.get("d_detrended")
            if clump is None:
                clump = entry.get("d_shape")
            entry.update(cluster_estimate(clump if clump is not None
                                          else float("nan"), p0, mu_bar))
            entry["p0_rse"] = _log_p0_rse(p0, int(live.sum()))
            entry["usable"] = bool(
                degenerate <= BIN_DEGENERATE_SHARE
                and entry["p0_rse"] is not None
                and entry["p0_rse"] <= LOG_P0_MAX_RSE
                and entry.get("batch_share") is not None)
        else:
            entry.update({"p0_observed": None, "p0_poisson": None,
                          "p0_rse": None,
                          "mu_effective": None, "cluster_size": None,
                          "batch_size": None, "batch_share": None,
                          "usable": False})
        entries.append(entry)
        mu *= MU_LADDER_STEP
    return entries


def pick_reading(entries: Sequence[dict]) -> tuple[Optional[dict], str]:
    """The scale the headline is read at. Returns (entry, why).

    The **plateau**, not the first usable scale. A bin has to be wider than
    the burst before a cluster counts as one object; below that the burst
    is split across bins and the share reads low. So walk up until the
    estimate stops moving — measured on a 35%-in-batches-of-105 stream the
    share settles immediately (0.352 / 0.364 / 0.372), on a thin one it is
    still climbing three steps in (0.38 → 0.44 → 0.49) and the ladder has
    to go further before it means anything.

    Above the plateau the estimate goes wrong again in the other
    direction — a bin holding several bursts merges them — which is why
    this takes the *first* flat pair rather than the last.
    """
    usable = [e for e in entries if e.get("usable")]
    if not usable:
        return None, "no scale in the ladder was measurable"
    for a, b in zip(usable, usable[1:]):
        if abs(b["batch_share"] - a["batch_share"]) <= PLATEAU_TOL:
            return a, "plateau"
    # No plateau: read the **finest** usable scale rather than the last
    # one. Drift only grows with μ, so the coarsest row is the most
    # contaminated of the lot and picking it is the worst of the options,
    # not the safest.
    return usable[0], "no plateau — read at the finest usable scale"


def ks_uniform(counts: np.ndarray, lam: np.ndarray) -> Optional[dict]:
    """KS of the arrivals against the intensity, over the whole sample.

    Conditioned on N arrivals, an inhomogeneous Poisson process places them
    so that Λ(t)/Λ_tot is uniform — so the empirical CDF read off the
    cumulative counts must lie on the cumulative intensity. One test over
    every row in the sample, not one per hour with the thin hours thrown
    away: that per-hour split is exactly what used to refuse a filtered
    stream, and the pooling bought nothing the single test does not have.

    KS catches slow **drift** where dispersion catches **clumping**, so on
    a real source it is usually significant — intraday traffic ramps. That
    is a fact about the profile being one rate per hour, not a fault, and
    `_verdict` says which of the two it is seeing.
    """
    n = float(counts.sum())
    if n < MIN_EVENTS_TOTAL or counts.size < 2:
        return None
    lam_cum = np.cumsum(lam)
    total = float(lam_cum[-1])
    if total <= 0:
        return None
    emp = np.cumsum(counts.astype(float)) / n
    theo = lam_cum / total
    d_plus = float(np.max(emp - theo))
    d_minus = float(np.max(theo - np.concatenate(([0.0], emp[:-1]))))
    d = max(d_plus, d_minus, 0.0)
    return {"d": d, "rows": int(n), "p": _kolmogorov_q(math.sqrt(n) * d)}


def _periodicity(slot_t: np.ndarray, counts: np.ndarray, lam: np.ndarray,
                 runs: Sequence[tuple[int, int]]) -> dict:
    """Find a batch beat: the periodogram against the Poisson noise floor.

    The residual r = n − μ has Var(r_s) = μ_s under H₀, so the periodogram
    ordinates |FFT(r)_k|² have expectation Λ_block and, divided by it, are
    Exp(1). Summing that over blocks gives Gamma(n_blocks) — a closed form
    `_chi2_sf` already computes exactly, at df = 2·n_blocks.

    **The noise floor is absolute, and that is the whole difference.** The
    first implementation used Fisher's g = max/Σ and failed on precisely
    the signal it was wanted for: a batch is a near-Dirac comb, a comb
    spreads its power over every harmonic of 1/T — a 300 s beat inside a
    3600 s block lights up 150 ordinates — so dividing by the total power
    dragged the sharpest imaginable schedule down to p ≈ 0.01. Measured
    against a known floor instead, the fundamental of that same comb stands
    out by orders of magnitude, because nothing divides it by the power its
    own harmonics took.

    Blocks are runs of **contiguous** scan, which is why the thin path
    scans the window whole: a 3600 s block resolves a 300 s period only to
    ±25 s, an 86 400 s block to ±1 s.
    """
    out: dict = {"period_s": None, "blocks": 0, "block_s": 0}
    if counts.size == 0:
        return out
    longest = max((b - a for a, b in runs), default=0)
    block = min(PERIOD_BLOCK_MAX_S // SCAN_INTERVAL_S, longest)
    if block < MIN_PERIOD_BLOCK_S // SCAN_INTERVAL_S:
        out["note"] = "no contiguous stretch long enough to resolve a period"
        return out

    specs: list[np.ndarray] = []
    starts: list[int] = []
    for a, b in runs:
        for s in range(a, b - block + 1, block):
            resid = counts[s:s + block].astype(float) - lam[s:s + block]
            weight = float(lam[s:s + block].sum())
            if weight <= 0:
                continue
            spec = np.abs(np.fft.rfft(resid)) ** 2 / weight
            specs.append(spec[1:])           # drop DC
            starts.append(s)
    if not specs:
        return out
    stack = np.vstack(specs)
    n_blocks = stack.shape[0]
    pooled = stack.sum(axis=0)

    k = np.arange(1, stack.shape[1] + 1)
    period = (block * SCAN_INTERVAL_S) / k
    band = ((period >= 2 * SCAN_INTERVAL_S)
            & (period <= min(MAX_PERIOD_S, block * SCAN_INTERVAL_S / 4.0)))
    out.update({"blocks": int(n_blocks), "block_s": int(block * SCAN_INTERVAL_S),
                "resolution_s": None})
    if not band.any():
        return out

    idx = np.nonzero(band)[0]
    trials = int(idx.size)
    out["ordinates"] = trials

    # One critical value, solved once, instead of a p-value per ordinate:
    # `_chi2_sf` is a Python loop and there can be tens of thousands of
    # them. The Gamma tail is monotone, so a bisection is exact enough.
    target = PERIOD_ALPHA / max(trials, 1)
    lo, hi = 0.0, float(pooled[idx].max()) + 10.0 * n_blocks + 10.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if _chi2_sf(2.0 * mid, 2 * n_blocks) > target:
            lo = mid
        else:
            hi = mid
    significant = idx[pooled[idx] > hi]

    strongest = int(idx[np.argmax(pooled[idx])])
    out["strongest_s"] = float(period[strongest])
    if significant.size == 0:
        out["p"] = _bonferroni(
            _chi2_sf(2.0 * float(pooled[strongest]), 2 * n_blocks), trials)
        out["candidate_s"] = float(period[strongest])
        out["resolution_s"] = float(period[strongest] ** 2
                                    / (block * SCAN_INTERVAL_S))
        out["block_share"] = float(np.mean(stack[:, strongest] > 3.0))
        return out

    # The fundamental: the lowest significant frequency, believed only if
    # the rest of the significant set lands on its harmonics.
    k_sig = k[significant]
    k0 = int(k_sig.min())
    ratio = k_sig / float(k0)
    on_harmonic = np.abs(ratio - np.round(ratio)) * k0 <= 1.0
    harmonic_share = float(np.mean(on_harmonic))
    i0 = k0 - 1
    t0 = float(period[i0])
    stat = float(pooled[i0])
    p = _bonferroni(_chi2_sf(2.0 * stat, 2 * n_blocks), trials)
    # A real schedule shows up in most blocks; one lucky block is visibly
    # not that. Exp(1) puts only 5% of its mass above 3.
    share = float(np.mean(stack[:, i0] > 3.0)) if n_blocks else 0.0
    out.update({
        "power": stat,
        "p": p,
        "block_share": share,
        "candidate_s": t0,
        "harmonic_share": harmonic_share,
        "harmonics": int(significant.size),
        "resolution_s": float(t0 * t0 / (block * SCAN_INTERVAL_S)),
    })
    if (p < PERIOD_ALPHA and share >= PERIOD_MIN_BLOCK_SHARE
            and harmonic_share >= PERIOD_HARMONIC_SHARE):
        out["period_s"] = t0
    return out


# ── the whole report ───────────────────────────────────────────────────────

def characterize_stream(
    source: Source,
    *,
    rate_per_s: dict[int, float],
    t_from: int,
    t_to: int,
    filter_tree=None,
    filter_groups=None,
    max_hours: int = MAX_SAMPLE_HOURS_WEB,
    statement_timeout_ms: Optional[int] = None,
    dest_engine=None,
) -> dict:
    """Measure the character of the stream over `[t_from, t_to)`.

    `rate_per_s` is λ̂(h) from `core.query.gap_day_profile` — the very
    profile `detect_gaps` integrates, so the `model` half of the report
    judges the detector's own model rather than a second one built here.

    The query lives here; every number is computed by
    `characterize_counts`, which takes arrays and no connection, so the
    statistics can be pinned to fixtures without a database.
    """
    hours, contiguous = plan_scan(rate_per_s, t_from, t_to,
                                  max_hours=max_hours)
    report = _empty_report(t_from, t_to, hours, max_hours, contiguous)
    if not hours:
        report["notes"].append(
            "No full hour in the window carries traffic in the day profile.")
        return report

    slot_t, counts = bucket_counts(
        source, hour_starts=hours, filter_tree=filter_tree,
        filter_groups=filter_groups, interval_s=SCAN_INTERVAL_S,
        statement_timeout_ms=statement_timeout_ms, dest_engine=dest_engine)
    return characterize_counts(slot_t, counts, rate_per_s, report=report)


def _empty_report(t_from: int, t_to: int, hours: Sequence[int],
                  max_hours: int, contiguous: bool) -> dict:
    return {
        "window": {"from": int(t_from), "to": int(t_to)},
        "sampled": {"hours": len(hours), "rows": 0,
                    "hour_starts": list(hours), "max_hours": max_hours,
                    "contiguous": bool(contiguous), "rate_per_s": None},
        "resolution": {"scan_s": SCAN_INTERVAL_S, "mu_floor": None,
                       "at_floor": False},
        "mu_scales": [],
        "reading": None,
        "reading_reason": "",
        "batch_share": None,
        "batch_size": None,
        "cluster_size": None,
        "zero_bins": None,
        "ks": None,
        "period_s": None,
        "periodicity": None,
        "verdict": VERDICT_UNKNOWN,
        "verdict_reason": "",
        "recommended_threshold_events": None,
        "recommended_min_gap_s": None,
        "profile_level_error": None,
        "notes": [],
    }


def characterize_counts(
    slot_t: np.ndarray,
    counts: np.ndarray,
    rate_per_s: dict[int, float],
    *,
    report: Optional[dict] = None,
) -> dict:
    """Every statistic in the report, from arrays alone. No connection.

    `slot_t` is the epoch second each slot starts at and `counts` what fell
    into it, both dense and time-ordered — the shape `bucket_counts`
    returns.
    """
    slot_t = np.asarray(slot_t, dtype=np.int64)
    counts = np.asarray(counts, dtype=np.int64)
    if report is None:
        t0 = int(slot_t[0]) if slot_t.size else 0
        t1 = int(slot_t[-1]) + SCAN_INTERVAL_S if slot_t.size else 0
        report = _empty_report(t0, t1, [], 0, True)

    total = int(counts.sum())
    report["sampled"]["rows"] = total
    if slot_t.size == 0:
        report["notes"].append("Nothing was scanned.")
        return report

    lam_bar = total / float(slot_t.size)
    report["sampled"]["rate_per_s"] = lam_bar
    if total < MIN_EVENTS_TOTAL:
        report["notes"].append(
            f"The sample holds {total} rows, below the {MIN_EVENTS_TOTAL} "
            "this needs. Widen the window, or drop a filter.")
        return report

    if not any(v and v > 0 for v in (rate_per_s or {}).values()):
        report["profile_blind"] = True
        report["notes"].append(
            "The day profile is empty for this filter over the week before "
            "the watermark, so nothing is said about the detector's own "
            "model. The verdict stands — it never reads λ̂.")

    runs = contiguous_runs(slot_t)
    lams, lam_info = build_expectations(slot_t, counts, rate_per_s or {},
                                        runs=runs)
    report["resolution"]["detrend_window_s"] = lam_info.get("detrend_window_s")
    if not lams:
        report["notes"].append("No usable intensity could be built.")
        return report

    mu_floor = MU_FLOOR_FACTOR * lam_bar * SCAN_INTERVAL_S
    report["resolution"]["mu_floor"] = mu_floor
    report["mu_scales"] = mu_ladder(counts, lams, mu_floor=mu_floor,
                                    df_costs=lam_info.get("df"))
    if not report["mu_scales"]:
        report["notes"].append(
            f"No scale fits {MIN_BINS} bins into this sample — it is too "
            "short for the rate it carries.")
        return report

    reading, why = pick_reading(report["mu_scales"])
    report["reading_reason"] = why
    if reading is not None:
        report["reading"] = reading
        report["batch_share"] = reading["batch_share"]
        report["batch_size"] = reading["batch_size"]
        report["cluster_size"] = reading["cluster_size"]
        report["resolution"]["at_floor"] = (
            reading is report["mu_scales"][0])
        report["zero_bins"] = {
            "mu": reading["mu"],
            "wall_s": reading["mu"] / lam_bar if lam_bar > 0 else None,
            "observed": reading["p0_observed"],
            "expected": reading["p0_poisson"],
            "bins": reading["bins"],
        }
    else:
        report["notes"].append(why)

    report["ks"] = ks_uniform(counts, lams["shape"])
    report["periodicity"] = _periodicity(
        slot_t, counts, lams.get("detrended", lams["shape"]), runs)
    report["period_s"] = report["periodicity"].get("period_s")

    _verdict(report, lam_bar=lam_bar)
    return report


def _clump(entry: dict) -> Optional[float]:
    """The clumping measure: detrended where it exists, else the shape."""
    d = entry.get("d_detrended")
    return d if d is not None else entry.get("d_shape")


def _verdict(report: dict, *, lam_bar: float) -> None:
    """Fill in the verdict and the two recommendations, in place."""
    entries = report["mu_scales"]
    usable = [e for e in entries if e.get("usable")]
    reading = report.get("reading")
    share = report.get("batch_share")
    period = report.get("period_s")
    per = report.get("periodicity") or {}

    # --- the verdict ------------------------------------------------------
    if share is None:
        report["verdict"] = VERDICT_UNKNOWN
        report["verdict_reason"] = (report.get("reading_reason")
                                    or "nothing measurable in this sample")
    else:
        d_read = _clump(reading) if reading else None
        pct = f"{share:.0%}"
        size = report.get("batch_size") or 1.0
        cells = report.get("cluster_size")
        meaningful = size >= MIN_MEANINGFUL_BATCH
        report["batch_meaningful"] = meaningful
        if meaningful and share >= BATCH_SHARE_ONLINE:
            pass
        elif not meaningful and share >= BATCH_SHARE_ONLINE:
            # Clusters too small to call a batch. Say what was actually
            # measured — the mean cluster size — instead of a share whose
            # decomposition is noise at this size.
            report["verdict"] = VERDICT_ONLINE
            report["verdict_reason"] = (
                f"online traffic — rows arrive in clusters of "
                f"{cells:.2f} on average, i.e. the odd pair inside the "
                "scan's own resolution rather than batches")
            share = 0.0
        if share < BATCH_SHARE_ONLINE:
            if d_read is not None and d_read < CLOCKED_D_MAX:
                report["verdict"] = VERDICT_CLOCKED
                report["verdict_reason"] = (
                    f"under-dispersed (D = {d_read:.2f}) with no clustering — "
                    "arrivals are more regular than chance, which makes the "
                    "detector conservative rather than wrong")
            elif not report["verdict_reason"]:
                report["verdict"] = VERDICT_ONLINE
                report["verdict_reason"] = (
                    f"online traffic — {pct} of rows arrive as part of a "
                    "batch, which is within noise of none")
        elif period:
            report["verdict"] = VERDICT_BATCH_REGULAR
            report["verdict_reason"] = (
                f"{pct} of rows arrive in batches of ~{size:.0f}, on a beat of "
                f"~{_fmt_period(period)} — every inter-batch pause looks like "
                "an outage to the detector")
        else:
            report["verdict"] = VERDICT_BURSTY
            band = ("mostly online, with some batching"
                    if share < BATCH_SHARE_MOSTLY else
                    ("part online, part batch" if share < BATCH_SHARE_HEAVY
                     else "predominantly batch"))
            report["verdict_reason"] = (
                f"{band} — {pct} of rows arrive in bursts of ~{size:.0f} with "
                "no consistent schedule")

    # --- the recommendation ------------------------------------------------
    # Read at **the scale the detector actually works on**: it decides at
    # μ = GAP_THRESHOLD_EVENTS, and since the ladder is in μ that scale is
    # simply a row in it rather than the nearest of six fixed second-widths.
    if lam_bar > 0:
        report["detector_scale_s"] = GAP_THRESHOLD_EVENTS / lam_bar
    # **Not** restricted to the scales the cluster estimate could use. That
    # estimate needs empty bins and so runs out well before μ = 20.76 on a
    # busy stream; the factor needs only a dispersion, which every scale
    # has. Tying the two together would put the detector's own scale back
    # out of reach, which is the thing this whole axis exists to fix.
    pool = [e for e in entries if _clump(e) is not None]
    if pool:
        rec = min(pool, key=lambda e: abs(math.log(max(e["mu"], 1e-9)
                                                   / GAP_THRESHOLD_EVENTS)))
        factor = max(1.0, _clump(rec) or 1.0)
        report["dispersion_factor"] = factor
        report["dispersion_factor_mu"] = rec["mu"]
        report["recommended_threshold_events"] = GAP_THRESHOLD_EVENTS * factor
        if rec["mu"] < GAP_THRESHOLD_EVENTS / 4 or rec["mu"] > GAP_THRESHOLD_EVENTS * 4:
            report["notes"].append(
                f"The ladder does not reach μ = {GAP_THRESHOLD_EVENTS:.1f}, "
                f"where the detector decides; the factor below is read at "
                f"μ = {rec['mu']:.2g} instead. Treat it as indicative.")
    if period:
        # With a schedule, the length floor is the real answer and the
        # threshold factor is the crude one: a silence of one batch period
        # is not an outage by construction, whatever μ says about it.
        report["recommended_min_gap_s"] = 2.0 * period
        report["primary_recommendation"] = "min_gap"
    else:
        report["primary_recommendation"] = "threshold"

    # --- the notes ---------------------------------------------------------
    if report["resolution"].get("at_floor") and reading is not None:
        report["notes"].append(
            f"The reading sits at the resolution floor (μ = "
            f"{report['resolution']['mu_floor']:.2g}, about "
            f"{report['resolution']['mu_floor'] / lam_bar:.1f} s of wall "
            "clock at this rate). The 1-second scan cannot resolve a burst "
            "shorter than that, so the batch share is a lower bound.")

    drift = 0.0
    for e in (usable or entries):
        d_s, d_d = e.get("d_shape"), e.get("d_detrended")
        if d_s is not None and d_d is not None:
            drift = max(drift, d_s - d_d)
    if drift > 1.0:
        report["drift"] = drift
    # ...but it is only the *story* when nothing is clumping. On a pure
    # batch the drop is in the thousands and means only that the trend
    # absorbed part of the comb; saying "a ramp, not clumping" there would
    # be exactly backwards.
    if drift > 1.0 and share is not None and share < BATCH_SHARE_ONLINE:
        report["notes"].append(
            f"The rate moves within the hour: removing a smooth trend drops D "
            f"by {drift:.1f}. That is a ramp, not clumping — λ̂(h) approximates "
            "it with one rate per hour, which is what the coarse rows of the "
            "table show.")

    ks = report.get("ks") or {}
    if (ks and math.isfinite(ks.get("p", float("nan"))) and ks["p"] < 0.01
            and (share is not None and share < BATCH_SHARE_ONLINE)
            and drift <= 1.0):
        report["notes"].append(
            f"Arrivals drift across the window (KS D = {ks['d']:.3f}) while "
            "staying Poisson at the scales a gap lives on — an ordinary "
            "intraday ramp, which λ̂(h) approximates by one rate per hour.")

    zb = report.get("zero_bins") or {}
    # Below this the comparison is two numbers that both round to zero, and
    # "0.0% against 0.0%" is not a finding.
    if (zb.get("observed") is not None and zb.get("expected")
            and zb["expected"] >= 0.005):
        excess = zb["observed"] - zb["expected"]
        ratio = excess / zb["expected"] if zb["expected"] > 0 else None
        report["zero_bins"]["excess"] = excess
        report["zero_bins"]["excess_ratio"] = ratio
        if ratio is not None and ratio > 0.5:
            report["notes"].append(
                f"Empty bins run {zb['observed']:.1%} against "
                f"{zb['expected']:.1%} expected — the detector will see "
                "silence the model does not predict.")
        elif (ratio is not None and ratio < 0.10 and share is not None
                and share >= BATCH_SHARE_ONLINE):
            # The distinction that keeps the correction from being alarmist:
            # overdispersion can sit in the *level* rather than in the
            # silences, and only the second kind reaches the detector.
            report["notes"].append(
                f"Empty bins match the model ({zb['observed']:.1%} against "
                f"{zb['expected']:.1%}) even so — the clumping is in the "
                "level, not in the silences the detector reads, and the "
                "factor below is a safe bound rather than a fault.")

    if reading is not None:
        d_model, d_shape = reading.get("d_model"), reading.get("d_shape")
        if d_model is not None and d_shape:
            level = d_model / max(d_shape, 1e-9)
            report["profile_level_error"] = level
            if level > 2.0:
                report["notes"].append(
                    "The stored day profile fits worse than the sample's own "
                    f"shape (D_model/D_shape = {level:.1f}) — λ̂(h) has "
                    "drifted from what the stream currently carries.")

    if per.get("note"):
        report["notes"].append(per["note"])
