"""Stream characterisation: does the gap detector's Poisson model hold?

`core/query.py:detect_gaps` marks a stretch of silence as an outage when
μ = ∫λ̂ dt exceeds −ln(α) ≈ 20.76 — the one-sided 6σ tail of
P(no event) = e^{−μ}. That arithmetic is only calibrated if the arrivals
inside an hour are **scattered at random over time**. A source fed by a
batch job — a burst every fifteen minutes, silence in between — breaks it:
the silence between two bursts is normal, and the detector calls every one
of them a 6σ outage. The same error hits `count(*) < 1` threshold alarms
with a short `window_s`.

Nothing else in the tree tests that assumption. This module does, from a
single 1-second scan, with four views of the same counts:

1. **Dispersion over scales** — the index of dispersion D(Δ) = X²/(K−1)
   for Δ ∈ {1, 5, 15, 60, 300, 900} s. D ≈ 1 is Poisson, D ≫ 1 is bursty,
   D < 1 is more regular than chance. The *curve* is the finding, not the
   number: a batch of period T peaks at Δ ≈ T, because below T one sees
   "burst or nothing" and above T the beat averages out again.
2. **Empty-bin share** against e^{−μ}. This is the test that matters most
   to the detector, because emptiness is exactly what it reacts to: an
   excess of empty bins *is* the false-positive rate, with no model in
   between.
3. **Periodicity** — Fisher's g-test on the periodogram of the Pearson
   residuals plus their autocorrelation. Dispersion says "bursty"; only
   this says "bursty with a period of fifteen minutes", which is the
   number an operator can act on.
4. **Kolmogorov–Smirnov on the arrival distribution** inside each hour.
   Conditioned on n events in an hour, a Poisson process puts them
   i.i.d. uniform over that hour — so the empirical CDF of the arrivals,
   read straight off the second counts, must be a straight line. KS
   catches slow drift (a ramp) where dispersion catches clumping.

**Two expectations, deliberately, and the difference between them is the
point.** μ can be had two ways, and each answers a different question:

- `shape` — μᵢ normalised to the hour's **own observed total**. This is
  the conditional test: given n arrivals in this hour, were they spread
  evenly? It is immune to a level error in the profile (a quiet Sunday is
  not a batch) and is what decides the verdict.
- `model` — μᵢ from λ̂(h), the seven-day profile `detect_gaps` actually
  integrates. This answers "does the detector's own model hold", level
  included.

Reporting only the second would call every ordinary day-to-day swing a
batch; reporting only the first would never notice that the profile the
detector rests on has drifted. The gap between them is a level error, and
naming it is cheaper than explaining it later.

**Why Δ stops at 900 s.** λ̂(h) is piecewise constant per hour, so the
ramp *inside* an hour is not modelled by anything here; at bin widths
approaching an hour that ramp is the whole signal and would be read as
overdispersion. Every test therefore runs **within one hour** and pools
afterwards — Σ X², Σ df for dispersion, Fisher's method for the KS
p-values — which also means an hour that never carries traffic
(λ̂(h) = 0) contributes nothing, the same way it is never marked.

No scipy: it is not in `requirements.txt` and does not belong there for
four formulas. `numpy` arrives with pandas and is already used in
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

from .query import _gap_where, _profile_integral
from .source import Source

log = logging.getLogger(__name__)

# The scan granularity. Fixed at 1 s like `GAP_SCAN_INTERVAL_S`, and for the
# same reason: it is the finest resolution the fact table can answer, and
# every coarser scale is a sum over these counts rather than another query.
SCAN_INTERVAL_S = 1

# Bin widths D(Δ) is reported at. The cap is not cosmetic — see the module
# docstring: above ~900 s the within-hour ramp dominates a bin.
DEFAULT_SCALES = (1, 5, 15, 60, 300, 900)
MAX_SCALE_S = 900

# A scale whose mean expectation falls below this is not reported: the χ²
# approximation to the dispersion statistic degrades, and a source running
# 1 000/h has μ = 0.28 at Δ = 1 s, which is an ordinary case rather than an
# exotic one. Skipped scales are named in the report instead of silently
# dropped, or the table would look like the test simply had nothing to say.
MIN_EXPECTED_PER_BIN = 0.5

# The measuring window. A day is the floor because D(Δ) has to be spread
# across hours-of-day: a batch beat that is plain at three in the morning
# disappears inside midday traffic, and λ̂(h) being hourly means pooling
# needs several hours to mean anything at all.
MIN_WINDOW_S = 86400
MAX_WINDOW_S = 7 * 86400

# Sampled hours, not scanned seconds, is the cost knob. The web path is
# bounded well below uwsgi's `harakiri = 120` (which kills the whole worker,
# taking its other in-flight requests with it); the CLI hangs off no worker.
MAX_SAMPLE_HOURS_WEB = 24
MAX_SAMPLE_HOURS_CLI = 168

# The verdict rests on scales at or below this. Above it the **ramp inside
# the hour** is the whole signal — λ̂(h) is piecewise constant per hour and
# models none of it — so D climbs for a stream that is perfectly Poisson at
# every scale a gap lives on. Measured on a synthetic 1/s → 5/s ramp: D runs
# 1.43 at 1 s and 480 at 900 s, and reading the latter would call ordinary
# intraday traffic a batch job. The coarser scales stay in the table, where
# a rising tail is informative, but they do not vote.
VERDICT_SCALE_MAX_S = 60

# The detrending window. Dispersion alone cannot tell a stream that *clumps*
# from one that merely *drifts*: a rate ramping 1/s → 5/s across an hour is
# Poisson at every instant, yet reads D = 27 at Δ = 60 s because μ is held
# flat across the hour. Removing a smooth trend of this width first turns the
# test into a high-pass one — variation slower than the window is the ramp and
# is not the question; variation faster than it is clumping and is. A batch
# beat well inside the window survives detrending untouched (a 300 s comb
# smoothed over 900 s leaves the spikes exactly where they were), which is
# what makes the two separable at all.
DETREND_WINDOW_S = 900
# Below this many bins per window the trend is not a trend, it is the data.
DETREND_MIN_BINS = 15

# Periodicity search. The ACF is the primary estimator and Fisher's g the
# significance test — not the other way round, which is what the first
# implementation got wrong: a Dirac comb spreads its power over every
# harmonic (a 300 s beat in a 3600 s hour lights up 150 ordinates), so
# g = max/Σ collapses to ~1/150 and the sharpest possible period lands at
# p ≈ 0.01. The ACF has no such problem: one unmistakable spike at lag 300.
ACF_MIN_LAG = 2
FISHER_ALPHA = 0.01
# A peak must clear this many standard errors of white noise (σ ≈ 1/√n)...
ACF_SIGMA = 4.0
# ...and it must be *substantial*, which is a different question. Bursts at
# random intervals averaging 300 s are genuinely autocorrelated: a broad,
# shallow hump that clears 4σ easily and puts its maximum somewhere new each
# hour. Measured, that hump peaks at ~0.09 where a real 300 s schedule peaks
# at ~0.92. Significance alone called the first one a timetable; height is
# what separates "correlated" from "on a schedule".
ACF_MIN_PEAK = 0.25
# And the hours must agree on the period, not merely each find one. An
# irregular stream produces a different maximum every hour; a schedule
# produces the same lag every time, so the spread is near zero rather than
# merely smallish.
PERIOD_SPREAD_MAX = 0.10
# A "period" at the scale of the measuring window is the window, not a beat:
# every test here runs inside one hour, so anything approaching an hour is an
# artefact of that — a linear ramp across the hour is the textbook case.
MAX_PERIOD_S = 1800

# An hour with fewer arrivals than this says nothing about shape: the
# residuals are all −1 and 0, and every test degenerates.
MIN_EVENTS_PER_HOUR = 60

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

    Exact for an even `df` up to 200 — the closed form is a finite series
    and this is where Fisher's method lands (df = 2k for k pooled hours),
    precisely the range where Wilson–Hilferty is at its worst. Everything
    else uses Wilson–Hilferty, which is excellent for the large df the
    dispersion statistic produces (K − 1 with K = 3600 at Δ = 1 s).
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


def _fisher_g_pvalue(g: float, m: int) -> float:
    """P(g > x) for Fisher's g on `m` periodogram ordinates.

    The exact form is an alternating sum of binomials, which overflows
    spectacularly at the m ≈ 1800 a one-hour, 1-second series produces.
    It is also unnecessary: (1 − kg)^{m−1} collapses so fast that the
    first few terms carry the whole value, so the sum is taken in log
    space and stopped once a term cannot move the result.
    """
    if not (0.0 < g < 1.0) or m < 2:
        return float("nan")
    kmax = min(int(1.0 / g), 12)
    total = 0.0
    for k in range(1, kmax + 1):
        rest = 1.0 - k * g
        if rest <= 0:
            break
        # log C(m,k) + (m-1) log(1-kg)
        log_term = (math.lgamma(m + 1) - math.lgamma(k + 1)
                    - math.lgamma(m - k + 1) + (m - 1) * math.log(rest))
        if log_term < -700:
            break
        term = math.exp(log_term)
        total += -term if k % 2 == 0 else term
        if term < 1e-15 * max(abs(total), 1e-300):
            break
    return min(1.0, max(0.0, total))


def _fisher_combine(pvals: Sequence[float]) -> tuple[float, float, int]:
    """Fisher's method: combine independent p-values into one.

    Returns (statistic, p, df). Each hour is its own sample of the same
    stream, so the per-hour p-values are independent by construction —
    which is what makes this the right combiner rather than a minimum with
    a Bonferroni correction.
    """
    usable = [p for p in pvals if p is not None and math.isfinite(p)]
    if not usable:
        return float("nan"), float("nan"), 0
    # A p of exactly 0 would make the statistic infinite; clamp at the
    # smallest value the tails above can meaningfully produce.
    stat = -2.0 * sum(math.log(max(p, 1e-300)) for p in usable)
    df = 2 * len(usable)
    return stat, _chi2_sf(stat, df), df


# ── the scan ───────────────────────────────────────────────────────────────

def sample_hours(
    rate_per_s: dict[int, float],
    t_from: int,
    t_to: int,
    *,
    max_hours: int,
) -> list[int]:
    """Pick whole hours to measure, spread over the hours of the day.

    Candidates are the full hour blocks inside `[t_from, t_to)` whose
    hour-of-day carries traffic (λ̂(h) > 0); an hour the stream never uses
    is skipped for the same reason the detector never marks one. The pick
    is round-robin **across hours-of-day**, newest first within each, so a
    budget of six hours buys six different times of day rather than six
    consecutive ones — a batch beat that is obvious at 03:00 is invisible
    inside midday traffic.

    Deterministic on purpose: the CLI and the chart must be able to report
    the same number for the same window, and a random sample could not.
    """
    if t_to <= t_from or max_hours <= 0:
        return []
    first = ((t_from + 3599) // 3600) * 3600
    last = (t_to // 3600) * 3600            # exclusive end of the last full hour
    by_hod: dict[int, list[int]] = {}
    for start in range(first, last, 3600):
        hod = (start % 86400) // 3600
        if rate_per_s.get(hod, 0.0) <= 0:
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
    return picked


def stream_window(
    source: Source,
    t_from: Optional[int],
    t_to: Optional[int],
    hwm: Optional[int],
) -> tuple[int, int, list[str]]:
    """The measuring window, clamped and widened. Returns (from, to, notes).

    **This is the authority, not the client.** The chart proposes a window
    from its viewport, but a request may ask for anything, and a window
    below a day cannot be measured honestly (see `MIN_WINDOW_S`): D(Δ) has
    to be spread over hours-of-day or a beat that is plain at three in the
    morning stays invisible inside midday traffic.

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

    if t_to - t_from > MAX_WINDOW_S:
        t_from = t_to - MAX_WINDOW_S
        notes.append(f"limited to {MAX_WINDOW_S // 86400} days")
    elif t_to - t_from < MIN_WINDOW_S:
        mid = (t_from + t_to) // 2
        t_from = mid - MIN_WINDOW_S // 2
        t_to = mid + MIN_WINDOW_S // 2
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
        notes = [n for n in notes if not n.startswith("limited to")]
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
) -> dict[int, np.ndarray]:
    """Second-resolution counts for each hour in `hour_starts`.

    One query for all of them — `include_time_range=False` on `_gap_where`
    plus an OR of the hour ranges, the same shape `gap_day_profile` uses to
    bring its own window. Sharing `_gap_where` is what keeps this test
    behind exactly the filters the chart, the scan and the profile use; a
    second filter builder here would eventually describe a different stream
    than the one the detector runs on.

    Returns `{hour_start: array of `3600 // interval_s` counts}`, **dense**:
    the empty bins are half the finding, and a `GROUP BY` alone would drop
    every one of them.

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
    if not hour_starts:
        return {}
    pc = source.partition_column
    slots = 3600 // interval_s

    flt_parts, params = _gap_where(
        source, filter_tree=filter_tree, filter_groups=filter_groups,
        include_time_range=False)

    ranges = []
    for i, start in enumerate(hour_starts):
        ranges.append(f"(f.{pc} >= :h{i}a AND f.{pc} < :h{i}b)")
        params[f"h{i}a"] = datetime.fromtimestamp(start, tz=timezone.utc)
        params[f"h{i}b"] = datetime.fromtimestamp(start + 3600, tz=timezone.utc)
    where = [f"({' OR '.join(ranges)})"] + flt_parts

    sql = f"""
        SELECT floor(extract(epoch from f.{pc}) / :ival)::bigint AS slot,
               count(*) AS n
          FROM {source.schema_name}.fact f
         WHERE {" AND ".join(where)}
         GROUP BY 1
    """
    params["ival"] = interval_s

    out = {h: np.zeros(slots, dtype=np.int64) for h in hour_starts}
    index = {h: i for i, h in enumerate(hour_starts)}
    with (dest_engine or source.dest_engine()).connect() as conn:
        if statement_timeout_ms:
            conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"),
                         {"ms": str(int(statement_timeout_ms))})
        rows = conn.execute(text(sql), params).mappings().all()
    for row in rows:
        sec = int(row["slot"]) * interval_s
        hour = (sec // 3600) * 3600
        i = index.get(hour)
        if i is None:                        # a row outside every asked range
            continue
        out[hour][(sec - hour) // interval_s] = int(row["n"])
    return out


# ── the four tests, per hour ───────────────────────────────────────────────

def _coarsen(counts: np.ndarray, factor: int) -> np.ndarray:
    """Sum `factor` adjacent bins. Coarsening is free; re-querying is not."""
    if factor <= 1:
        return counts
    usable = (len(counts) // factor) * factor
    if usable == 0:
        return np.empty(0, dtype=counts.dtype)
    return counts[:usable].reshape(-1, factor).sum(axis=1)


def _smooth(x: np.ndarray, win: int) -> np.ndarray:
    """Moving average of `x` over `win` bins, edge-padded, length preserved."""
    if win % 2 == 0:
        win += 1
    if win <= 1 or win >= len(x):
        return np.full(len(x), float(x.mean()))
    pad = win // 2
    padded = np.pad(x.astype(float), pad, mode="edge")
    kernel = np.ones(win) / win
    return np.convolve(padded, kernel, mode="valid")[:len(x)]


def _detrended_mu(counts: np.ndarray, bin_s: int) -> Optional[np.ndarray]:
    """μ from a smooth trend through the counts, or None if too coarse.

    The trend absorbs whatever moves slower than `DETREND_WINDOW_S` — the
    ramp across the hour — so what is left to test is clumping. Rescaled to
    the observed total so the comparison stays conditional, exactly like the
    flat `shape` expectation it replaces.
    """
    win = DETREND_WINDOW_S // bin_s
    if win < DETREND_MIN_BINS or win >= len(counts):
        return None
    trend = _smooth(counts, win)
    total = float(counts.sum())
    ssum = float(trend.sum())
    if ssum <= 0 or total <= 0:
        return None
    mu = trend * (total / ssum)
    return np.where(mu > 0, mu, np.nan)


def _dispersion_one(counts: np.ndarray, mu: np.ndarray,
                    *, estimated: bool, df_cost: int = 0) -> tuple[float, int]:
    """X² = Σ (n−μ)²/μ and its degrees of freedom for one hour.

    `estimated=True` means μ was normalised to this hour's own total, which
    costs one degree of freedom — the conditional test. With μ from the
    stored profile nothing was estimated here and df = K. `df_cost` is the
    extra price of a fitted trend (roughly one parameter per smoothing
    window). Bins whose μ is not positive — a NaN from the detrender — drop
    out on the `mu > 0` test, which NaN fails.
    """
    with np.errstate(invalid="ignore"):
        ok = mu > 0
    if not ok.any():
        return float("nan"), 0
    resid = counts[ok] - mu[ok]
    chi2 = float(np.sum(resid * resid / mu[ok]))
    df = int(ok.sum()) - (1 if estimated else 0) - df_cost
    return chi2, max(df, 0)


def _ks_uniform_one(counts: np.ndarray) -> Optional[float]:
    """KS statistic of the arrivals against a uniform spread over the hour.

    Conditioned on n arrivals in an hour, a Poisson process scatters them
    i.i.d. uniform across it, so the empirical CDF read off the cumulative
    second counts must be a straight line. Reconstructing the CDF from the
    counts rather than from raw timestamps costs nothing in resolution at
    3600 points and avoids both a second scan and the tie problem that
    millisecond stamps bring at high rates.

    Returns the KS statistic, or None when the hour is too thin to judge.
    """
    n = int(counts.sum())
    if n < MIN_EVENTS_PER_HOUR:
        return None
    k = len(counts)
    cum = np.cumsum(counts) / n
    grid = np.arange(1, k + 1) / k
    # Both sides of the step: the CDF is a step function and the supremum
    # can sit just before a jump as easily as just after it.
    d_plus = float(np.max(cum - (grid - 1.0 / k)))
    d_minus = float(np.max(grid - cum))
    return max(d_plus, d_minus)


def _periodogram(resid: np.ndarray) -> np.ndarray:
    """Periodogram ordinates of the residuals, DC term dropped."""
    centred = resid - resid.mean()
    spec = np.abs(np.fft.rfft(centred)) ** 2
    return spec[1:]


def _acf(resid: np.ndarray) -> Optional[np.ndarray]:
    """Autocorrelation for lags 0…n/2, via FFT.

    Zero-padded to twice the length so this is the linear autocorrelation,
    not the circular one — a circular ACF wraps the end of the hour onto its
    start and invents a correlation at the lags that matter most.
    """
    n = len(resid)
    if n < 8:
        return None
    centred = resid - resid.mean()
    nfft = 1 << (2 * n - 1).bit_length()
    spec = np.fft.rfft(centred, nfft)
    acf = np.fft.irfft(spec * np.conj(spec), nfft)[:n // 2]
    if acf.size < 4 or acf[0] <= 0:
        return None
    return acf / acf[0]


def _acf_peak(acf: np.ndarray, n: int) -> Optional[tuple[int, float]]:
    """The lag of a genuine periodic peak in `acf`, or None.

    Three conditions, and the middle one is what keeps a ramp out. A ramp's
    ACF is monotonically falling: its largest value above the minimum lag is
    at that minimum lag, and it is *not* a local maximum. A beat's is — a
    sharp spike standing above both neighbours. Without that test a linear
    1/s → 5/s ramp reports a period of one hour, which is the measuring
    window talking about itself.
    """
    if acf.size <= ACF_MIN_LAG + 1:
        return None
    search = acf[ACF_MIN_LAG:-1]
    if search.size < 2:
        return None
    lag = int(search.argmax()) + ACF_MIN_LAG
    peak = float(acf[lag])
    if peak < max(ACF_SIGMA / math.sqrt(n), ACF_MIN_PEAK):
        return None
    if not (acf[lag] > acf[lag - 1] and acf[lag] > acf[lag + 1]):
        return None
    return lag, peak


# ── the whole report ───────────────────────────────────────────────────────

def characterize_stream(
    source: Source,
    *,
    rate_per_s: dict[int, float],
    t_from: int,
    t_to: int,
    filter_tree=None,
    filter_groups=None,
    scales: Sequence[int] = DEFAULT_SCALES,
    max_hours: int = MAX_SAMPLE_HOURS_WEB,
    statement_timeout_ms: Optional[int] = None,
    dest_engine=None,
) -> dict:
    """Measure the character of the stream over `[t_from, t_to)`.

    `rate_per_s` is λ̂(h) from `core.query.gap_day_profile` — the very
    profile `detect_gaps` integrates, so the `model` half of the report
    judges the detector's own model rather than a second one built here.

    Returns the report dict; see the module docstring for what each test
    says and `docs/stream-character.md` for why.
    """
    scales = tuple(s for s in sorted(set(int(s) for s in scales))
                   if 1 <= s <= MAX_SCALE_S)
    hours = sample_hours(rate_per_s, t_from, t_to, max_hours=max_hours)
    report: dict = {
        "window": {"from": int(t_from), "to": int(t_to)},
        "sampled": {"hours": len(hours), "rows": 0,
                    "hour_starts": hours, "max_hours": max_hours},
        "scales": [],
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
    if not hours:
        report["notes"].append(
            "No full hour in the window carries traffic in the day profile.")
        return report

    counts_by_hour = bucket_counts(
        source, hour_starts=hours, filter_tree=filter_tree,
        filter_groups=filter_groups, interval_s=SCAN_INTERVAL_S,
        statement_timeout_ms=statement_timeout_ms, dest_engine=dest_engine)

    usable = {h: c for h, c in counts_by_hour.items()
              if int(c.sum()) >= MIN_EVENTS_PER_HOUR}
    total_rows = int(sum(int(c.sum()) for c in counts_by_hour.values()))
    report["sampled"]["rows"] = total_rows
    report["sampled"]["hours_usable"] = len(usable)
    if not usable:
        report["notes"].append(
            f"Every sampled hour held fewer than {MIN_EVENTS_PER_HOUR} rows — "
            "too thin to judge the shape of the stream.")
        return report
    if len(usable) < len(hours):
        report["notes"].append(
            f"{len(hours) - len(usable)} of {len(hours)} sampled hours were "
            f"below {MIN_EVENTS_PER_HOUR} rows and were left out.")

    # --- dispersion, per scale, pooled over hours -------------------------
    for delta in scales:
        if 3600 % delta:
            continue
        factor = delta // SCAN_INTERVAL_S
        chi2_shape = chi2_model = chi2_det = 0.0
        df_shape = df_model = df_det = 0
        mu_sum = 0.0
        mu_n = 0
        for hour, counts in usable.items():
            coarse = _coarsen(counts, factor)
            if len(coarse) < 4:
                continue
            k = len(coarse)
            # shape: μ from this hour's own total — the conditional test
            mu_shape = np.full(k, float(coarse.sum()) / k)
            c2, d = _dispersion_one(coarse, mu_shape, estimated=True)
            if d:
                chi2_shape += c2
                df_shape += d
            # model: μ from the stored profile, through the detector's own
            # integral so the two cannot drift apart
            mu_model = np.array(
                [_profile_integral(rate_per_s, hour + i * delta,
                                   hour + (i + 1) * delta) for i in range(k)],
                dtype=float)
            c2, d = _dispersion_one(coarse, mu_model, estimated=False)
            if d:
                chi2_model += c2
                df_model += d
            # detrended: the ramp across the hour absorbed into a smooth
            # trend, so what is measured is clumping and nothing else
            mu_det = _detrended_mu(coarse, delta)
            if mu_det is not None:
                cost = max(1, k * delta // DETREND_WINDOW_S)
                c2, d = _dispersion_one(coarse, mu_det, estimated=True,
                                        df_cost=cost)
                if d:
                    chi2_det += c2
                    df_det += d
            mu_sum += float(mu_shape.sum())
            mu_n += k
        if not df_shape:
            continue
        mean_mu = mu_sum / mu_n if mu_n else 0.0
        entry = {
            "bin_s": delta,
            "mean_expected": mean_mu,
            "d_shape": chi2_shape / df_shape,
            "p_shape": _chi2_sf(chi2_shape, df_shape),
            "chi2_shape": chi2_shape,
            "df_shape": df_shape,
            "d_model": (chi2_model / df_model) if df_model else None,
            "p_model": _chi2_sf(chi2_model, df_model) if df_model else None,
            "d_detrended": (chi2_det / df_det) if df_det else None,
            "p_detrended": _chi2_sf(chi2_det, df_det) if df_det else None,
            "weak": mean_mu < MIN_EXPECTED_PER_BIN,
        }
        report["scales"].append(entry)
    if not report["scales"]:
        report["notes"].append("No scale had enough bins to test.")
        return report

    strong = [s for s in report["scales"] if not s["weak"]]
    if not strong:
        report["notes"].append(
            "Every scale's expected count is below "
            f"{MIN_EXPECTED_PER_BIN} — the rate is too low for the χ² "
            "approximation; read the table as indicative only.")
    basis = strong or report["scales"]

    # --- empty bins, at the finest scale that is not degenerate ----------
    fine = basis[0]
    factor = fine["bin_s"] // SCAN_INTERVAL_S
    obs_zero = exp_zero = var_zero = 0.0
    n_bins = 0
    for hour, counts in usable.items():
        coarse = _coarsen(counts, factor)
        if len(coarse) < 4:
            continue
        mu = float(coarse.sum()) / len(coarse)
        p0 = math.exp(-mu)
        obs_zero += float((coarse == 0).sum())
        exp_zero += p0 * len(coarse)
        var_zero += p0 * (1.0 - p0) * len(coarse)
        n_bins += len(coarse)
    if n_bins:
        sd = math.sqrt(var_zero) if var_zero > 0 else 0.0
        z = ((obs_zero - exp_zero) / sd) if sd > 0 else float("nan")
        report["zero_bins"] = {
            "bin_s": fine["bin_s"],
            "observed": obs_zero / n_bins,
            "expected": exp_zero / n_bins,
            "bins": n_bins,
            "z": z,
            "p": _norm_sf(z) if math.isfinite(z) else float("nan"),
            "excess": (obs_zero - exp_zero) / n_bins,
        }

    # --- KS on the arrival distribution, pooled by Fisher ----------------
    ks_stats: list[float] = []
    ks_pvals: list[float] = []
    for counts in usable.values():
        d = _ks_uniform_one(counts)
        if d is None:
            continue
        n = int(counts.sum())
        ks_stats.append(d)
        ks_pvals.append(_kolmogorov_q(math.sqrt(n) * d))
    if ks_stats:
        stat, p, df = _fisher_combine(ks_pvals)
        report["ks"] = {
            "d_max": max(ks_stats),
            "d_median": float(np.median(ks_stats)),
            "hours": len(ks_stats),
            "fisher_stat": stat,
            "fisher_df": df,
            "p": p,
        }

    # --- periodicity ------------------------------------------------------
    report["periodicity"] = _periodicity(usable, scales=basis)
    if report["periodicity"] and report["periodicity"].get("period_s"):
        report["period_s"] = report["periodicity"]["period_s"]

    _verdict(report)
    return report


def _periodicity(usable: dict[int, np.ndarray], *, scales: list[dict]) -> dict:
    """Find a batch beat: the ACF for the period, Fisher's g for support.

    The estimator is the **autocorrelation**, not the periodogram. A batch
    of period T is a near-Dirac comb, and a comb spreads its power across
    every harmonic of 1/T — a 300 s beat inside a 3600 s hour lights up 150
    ordinates, which drags Fisher's g = max/Σ down to ~1/150 and leaves the
    sharpest imaginable period sitting at p ≈ 0.01. The same signal puts a
    spike of ~1.0 at lag 300 in the ACF, where there is nothing to miss.

    What carries the finding is **consistency across hours**, not a single
    p-value: a real schedule repeats at the same lag in nearly every hour,
    which sidesteps the multiplicity problem a per-hour test would raise,
    and one lucky hour is visibly not that.
    """
    if not scales:
        return {}
    bin_s = scales[0]["bin_s"]
    factor = bin_s // SCAN_INTERVAL_S
    periods: list[float] = []
    peaks: list[float] = []
    lags: list[int] = []
    g_pvals: list[float] = []
    hits = 0
    tested = 0
    for counts in usable.values():
        coarse = _coarsen(counts, factor).astype(float)
        if len(coarse) < 32:
            continue
        mu = coarse.mean()
        if mu <= 0:
            continue
        tested += 1
        resid = (coarse - mu) / math.sqrt(mu)
        acf = _acf(resid)
        spec = _periodogram(resid)
        if spec.size >= 4 and spec.sum() > 0:
            g = float(spec.max() / spec.sum())
            g_pvals.append(_fisher_g_pvalue(g, int(spec.size)))
        if acf is None:
            continue
        found = _acf_peak(acf, len(coarse))
        if found is None:
            continue
        lag, peak = found
        lags.append(lag)
        period_s = lag * bin_s
        if not (2 * bin_s <= period_s <= MAX_PERIOD_S):
            continue
        hits += 1
        periods.append(float(period_s))
        peaks.append(peak)
    if not tested:
        return {}
    out: dict = {
        "bin_s": bin_s,
        "hours_tested": tested,
        "hours_significant": hits,
        "share_significant": hits / tested,
        "period_s": None,
    }
    if g_pvals:
        stat, p, df = _fisher_combine(g_pvals)
        out["fisher_g_p"] = p
    if peaks:
        out["acf_peak"] = float(np.median(peaks))
        out["acf_peak_lag"] = int(np.median(lags))
    # A beat is only called one when it shows up in most hours *and* the
    # periods agree — a spread of wildly different peaks is noise finding a
    # different maximum every hour, not a schedule.
    if periods and hits / tested >= 0.5:
        med = float(np.median(periods))
        spread = float(np.median([abs(x - med) for x in periods]))
        out["period_spread_s"] = spread
        if med > 0 and spread / med < PERIOD_SPREAD_MAX:
            out["period_s"] = med
    return out


def _verdict(report: dict) -> None:
    """Fill in the verdict and the two recommendations, in place."""
    strong = [x for x in report["scales"] if not x["weak"]]
    basis = strong or report["scales"]
    if not basis:
        return
    # Only the fine scales vote — above VERDICT_SCALE_MAX_S the within-hour
    # ramp is the signal, not the stream's character. Fall back to the
    # finest available if the whole table sits above the cap (a very thin
    # stream), and say so.
    voting = [x for x in basis if x["bin_s"] <= VERDICT_SCALE_MAX_S] or basis[:1]
    if voting is not basis and len(voting) < len(basis):
        report["verdict_scales"] = [x["bin_s"] for x in voting]
    # The clumping measure is the detrended one wherever it exists: it is
    # the half of the signal the gap detector is exposed to. `d_shape` is
    # kept for the table and for the drift note below, where the difference
    # between the two *is* the drift.
    def _clump(entry):
        d = entry.get("d_detrended")
        return d if d is not None else entry["d_shape"]

    d_fine = _clump(voting[0])
    d_max_entry = max(voting, key=_clump)
    d_max = _clump(d_max_entry)
    drift = max((x["d_shape"] - _clump(x)) for x in voting)
    period = report.get("period_s")
    zero = report.get("zero_bins") or {}
    per = report.get("periodicity") or {}

    if period and d_max > 1.5:
        report["verdict"] = VERDICT_BATCH_REGULAR
        report["verdict_reason"] = (
            f"overdispersed (D = {d_max:.1f} at {d_max_entry['bin_s']} s) with a "
            f"beat of ~{_fmt_period(period)} in "
            f"{per.get('hours_significant')} of {per.get('hours_tested')} hours")
    elif d_max > 1.5:
        report["verdict"] = VERDICT_BURSTY
        report["verdict_reason"] = (
            f"overdispersed (D = {d_max:.1f} at {d_max_entry['bin_s']} s) with no "
            "consistent period — bursts without a schedule")
    elif d_fine < 0.7:
        report["verdict"] = VERDICT_CLOCKED
        report["verdict_reason"] = (
            f"underdispersed (D = {d_fine:.2f} at {voting[0]['bin_s']} s) — "
            "arrivals are more regular than chance")
    else:
        report["verdict"] = VERDICT_ONLINE
        report["verdict_reason"] = (
            f"D stays near 1 up to {voting[-1]['bin_s']} s (max {d_max:.2f}) — "
            "the Poisson model the gap detector rests on holds")

    # --- the recommendation ------------------------------------------------
    # Quasi-Poisson: an overdispersed count needs a proportionally longer
    # silence for the same significance. The factor is read at **the scale
    # the detector actually works on** — a gap that just reaches the
    # threshold is −ln(α)/λ̂ seconds long — rather than at the worst scale in
    # the table. Reading D at 1 s would size the correction by clumping the
    # detector never looks at, and at 900 s by the ramp it does not model.
    from .query import GAP_THRESHOLD_EVENTS
    lam = (voting[0]["mean_expected"] / voting[0]["bin_s"]) if voting[0]["bin_s"] else 0.0
    rec_entry = voting[0]
    if lam > 0:
        target_s = GAP_THRESHOLD_EVENTS / lam
        report["detector_scale_s"] = target_s
        rec_entry = min(basis,
                        key=lambda x: abs(math.log(x["bin_s"] / target_s)))
        if rec_entry["bin_s"] > VERDICT_SCALE_MAX_S:
            report["notes"].append(
                f"At {lam:.3g} rows/s a gap only reaches the 6σ threshold after "
                f"{_fmt_period(target_s)}, so the correction below is read at "
                f"{rec_entry['bin_s']} s — a scale where the within-hour ramp "
                "also contributes. Treat it as an upper bound.")
    factor = max(1.0, rec_entry["d_shape"])
    report["recommended_threshold_events"] = GAP_THRESHOLD_EVENTS * factor
    report["dispersion_factor"] = factor
    report["dispersion_factor_scale_s"] = rec_entry["bin_s"]
    if period:
        # With a schedule, the length floor is the real answer and the
        # threshold factor is the crude one: a silence of one batch period
        # is not an outage by construction, whatever μ says about it.
        report["recommended_min_gap_s"] = 2.0 * period
        report["primary_recommendation"] = "min_gap"
    else:
        report["primary_recommendation"] = "threshold"

    # The notes hang on **effect size**, not on z. With 86 400 bins z is
    # brutally sensitive: 31.14% empty against 30.64% expected — half a
    # percentage point, nothing an operator would act on — scores z = 3.9.
    # Significance is not the question here; how much extra silence the
    # detector will trip over is. z stays in the payload, informationally.
    excess_ratio = None
    if zero and zero.get("expected", 0) > 0:
        excess_ratio = (zero["observed"] - zero["expected"]) / zero["expected"]
        report["zero_bins"]["excess_ratio"] = excess_ratio
    if excess_ratio is not None and excess_ratio > 0.5:
        report["notes"].append(
            f"Empty {zero['bin_s']} s bins run {zero['observed']:.1%} against "
            f"{zero['expected']:.1%} expected — the detector will see silence "
            "the model does not predict.")
    elif (excess_ratio is not None and excess_ratio < 0.10
          and d_max > 1.5):
        # The distinction that keeps the correction from being alarmist.
        # Overdispersion can sit in the *level* — the rate wandering between
        # minutes — rather than in the silences, and only the second kind
        # reaches the detector. Measured on real payment traffic: D = 4.4 at
        # 60 s while empty seconds ran 33.74% against 33.31% expected. The
        # factor is then a bound, not a diagnosis.
        report["notes"].append(
            f"Empty {zero['bin_s']} s bins match the model ({zero['observed']:.1%} "
            f"against {zero['expected']:.1%}) even though D is {d_max:.1f} — the "
            "overdispersion is in the level, not in the silences the detector "
            "reads. The factor below is a safe bound rather than a fault.")

    # KS with a healthy fine-scale D is drift, not clumping: the arrivals are
    # Poisson at every scale a gap lives on, but their rate moves across the
    # hour. That is ordinary intraday traffic, and saying so is the point —
    # otherwise the KS line reads as a failure on every real source.
    ks = report.get("ks") or {}
    if drift > 1.0 and d_max <= 1.5:
        report["drift"] = drift
        report["notes"].append(
            f"The rate moves across the hour (D falls from "
            f"{d_max_entry['d_shape']:.1f} to {d_max:.2f} once a smooth trend "
            "is removed) — a ramp, not clumping. λ̂(h) approximates it with one "
            "rate per hour, which is what the coarse scales in the table show.")
    elif (ks and math.isfinite(ks.get("p", float("nan"))) and ks["p"] < 0.01
            and d_max <= 1.5):
        report["notes"].append(
            f"Arrivals drift within the hour (KS D = {ks['d_median']:.3f}) while "
            "staying Poisson at fine scales — an ordinary intraday ramp, which "
            "λ̂(h) approximates by one rate per hour.")

    model_entry = next((x for x in voting if x.get("d_model") is not None), None)
    if model_entry and model_entry["d_model"] is not None:
        level = model_entry["d_model"] / max(model_entry["d_shape"], 1e-9)
        report["profile_level_error"] = level
        if level > 2.0:
            report["notes"].append(
                "The stored day profile fits worse than the hours' own "
                f"totals (D_model/D_shape = {level:.1f}) — λ̂(h) has drifted "
                "from what the stream currently carries.")


def _fmt_period(seconds: float) -> str:
    """A period as an operator would say it."""
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"
