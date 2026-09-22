# Stream character

Research note: is a datasource's transaction stream online traffic or a
batch load, how would you tell, and what follows for the gap detector.

## 1. The assumption nobody was testing

The gap overlay ([`chart-app.md`](chart-app.md), "The gap overlay") calls a
stretch of silence an outage when

    μ = ∫ λ̂ dt  >  −ln(α) ≈ 20.76

— the expected transaction count over the gap, integrated hour segment by
hour segment against the day profile λ̂(h), tested against the one-sided 6σ
tail of P(no event) = e^{−μ}.

That arithmetic is exact. It is also **conditional on a model that had
never been checked**: that the arrivals are an inhomogeneous Poisson
process, i.e. scattered at random over time at the rate λ̂(h).

A source fed by a batch job violates it outright. If 900 rows land every
five minutes and nothing arrives in between, then at 3 rows/s the ordinary
quiet between two bursts carries μ = 900 — forty-three times the
threshold. Every single inter-batch pause is reported as a 6σ outage, the
list fills with them, and the one real outage is indistinguishable from the
schedule. The same failure hits `count(*) < 1` threshold alarms with a
short `window_s`.

Nothing in the tree measured this. `core/alarm.py` has no notion of an
expected value at all (it compares an aggregate to a number an operator
typed); the only distributional assumption in the codebase is the one
above, and it sat unexamined.

`manage.py stream_check` and the chart's **Stream character** button test
it. Both write nothing: the report recommends, it does not correct. A bad
measurement must never be able to silence the detector.

## 2. What is actually being tested

H₀: the arrivals are a Poisson process with intensity λ̂(h).

Every test below runs on **one dataset** — the counts nᵢ in bins of width
Δ, together with their expectations μᵢ. One scan at 1-second resolution
gives the finest view; every coarser scale is a sum over those counts
rather than another query, the same coarsening-is-cheap asymmetry
[`rollup.md`](rollup.md) rests on.

Everything runs **inside one hour** and is pooled afterwards. That is not
a convenience: λ̂(h) is piecewise constant per hour, so an hour is the
largest span over which the model claims anything at all.

### 2.1 Three expectations, and why all three are reported

μ can be had three ways, and the differences between them are where the
information is.

| μ from | what a departure means |
|---|---|
| `shape` — the hour's own observed total, spread flat | the arrivals are not uniform within the hour |
| `model` — λ̂(h), the profile the detector integrates | the detector's own model does not fit, level included |
| `detrended` — a smooth trend through the counts | the arrivals **clump**, beyond any drift in the rate |

`model` alone would be useless as a verdict: an ordinary quiet Sunday is a
level error against a seven-day profile, and calling that a batch job would
flag every source. `shape` alone cannot see that the profile has drifted.
And `shape` alone confuses a **ramp** with **clumping**, which is the
distinction the whole exercise turns on — see 2.3.

So the verdict rests on `detrended`, `model` is reported because the
detector runs on it, and the gap between them is named rather than left
for the reader to infer.

### 2.2 Dispersion over scales

    X²(Δ) = Σᵢ (nᵢ − μᵢ)² / μᵢ          D(Δ) = X²(Δ) / (K − 1)

Under H₀, X² ~ χ²_{K−1} (normalising μ to the hour's total costs one
degree of freedom; a fitted trend costs roughly one per smoothing window).
D is the index of dispersion: **1 is Poisson, above is clumped, below is
more regular than chance.**

The **curve** is the finding, not the number. Read across the scales:

- flat at ≈ 1 — Poisson at every scale a gap lives on;
- large at fine Δ, collapsing at some Δ — a schedule, see 2.4;
- rising monotonically with no collapse — bursts, or long-range
  correlation (2.6);
- below 1 everywhere — a clock, not a stream.

p-values come from Wilson–Hilferty, exact for the small even df that
Fisher's method produces. There is no scipy in `requirements.txt` and four
formulas do not justify adding it; numpy arrives with pandas and is already
used in `core/audit.py`.

### 2.3 The trap: inhomogeneity, at two levels

Computed naively as Var/Mean over a day, D measures the **day profile** and
declares every source a batch job. That one is obvious and is handled by
μᵢ coming from the hour, not from a global mean.

The second level is not obvious and cost this module two rewrites. Even
inside one hour the rate is not constant — traffic ramps. λ̂(h) models none
of that, so the ramp lands in the residuals and D climbs with Δ. Measured
on a synthetic stream ramping 1/s → 5/s across the hour, Poisson at every
instant:

| Δ | 1 s | 5 s | 15 s | 60 s | 300 s | 900 s |
|---|---|---|---|---|---|---|
| D shape | 1.43 | 3.16 | 7.49 | 27.3 | 140 | 480 |
| D detrended | 1.08 | 1.12 | 1.16 | 1.23 | – | – |

Read `D shape` at 900 s and you call ordinary intraday traffic a batch job.
Two defences, both needed:

1. **A detrended expectation** (`DETREND_WINDOW_S` = 900 s). Removing a
   smooth trend of that width turns the test into a high-pass one:
   variation slower than the window is the ramp and is not the question;
   variation faster is clumping and is. A batch beat well inside the window
   survives untouched — a 300 s comb smoothed over 900 s leaves its spikes
   exactly where they were — which is what makes the two separable at all.
2. **A cap on the voting scales** (`VERDICT_SCALE_MAX_S` = 60 s). The
   coarser rows stay in the table, where a rising tail is informative, but
   they do not decide anything.

### 2.4 Emptiness — the test that matters to the detector

Under H₀, P(nᵢ = 0) = e^{−μᵢ}; the expected share of empty bins is
(1/K)·Σ e^{−μᵢ} with Poisson-binomial variance, and the observed share
against it is a z-test.

This is the most directly relevant test of the four, because **emptiness is
exactly what the detector reacts to.** An excess of empty bins *is* the
false-positive rate, measured without a model in between.

It also earns its place by dissenting. On real payment traffic the
dispersion said D = 4.4 at 60 s, while empty seconds ran 33.74% against
33.31% expected — z = 2.5, essentially a match. The overdispersion was in
the *level*, the rate wandering between minutes, not in the silences. The
recommended factor is then a safe bound, not a fault, and the report says
so instead of leaving the operator to act on the scarier number.

### 2.5 Periodicity — regular versus irregular

Dispersion says "bursty"; it cannot say "bursty every fifteen minutes",
which is the number an operator can act on.

The estimator is the **autocorrelation**, and getting there took one wrong
turn worth recording. Fisher's g-test on the periodogram is the textbook
choice, and it fails on exactly the signal it is wanted for: a batch is a
near-Dirac comb, a comb spreads its power over every harmonic of 1/T — a
300 s beat inside a 3600 s hour lights up 150 ordinates — so g = max/Σ
collapses to ~1/150 and the sharpest imaginable schedule lands at p ≈ 0.01,
just short of significance. The same signal puts a spike of 0.92 at lag 300
in the ACF, where there is nothing to miss.

Three conditions separate a schedule from mere correlation:

- the peak is a **local maximum** — a ramp's ACF falls monotonically and
  has none, which is what stops a ramp reporting a period of one hour;
- it is **substantial** (`ACF_MIN_PEAK` = 0.25), not merely significant.
  Bursts at random intervals averaging 300 s are genuinely autocorrelated:
  a broad hump peaking at ~0.09 that clears 4σ easily and puts its maximum
  somewhere new each hour;
- the hours **agree** on it (spread under 10% of the median). A schedule
  gives the same lag every hour; noise gives a different one each time.

A period approaching the measuring window is rejected outright
(`MAX_PERIOD_S` = 1800 s): every test here runs inside one hour, so a
"period" at that scale is the window describing itself.

### 2.6 Uniformity (KS)

Conditioned on n arrivals in an hour, a Poisson process scatters them
i.i.d. uniform across it — so the empirical CDF of the arrivals must be a
straight line. Reconstructing that CDF from the second counts costs nothing
in resolution at 3600 points and avoids both a second scan and the tie
problem raw millisecond stamps bring at high rates (many arrivals share a
millisecond, which biases KS conservatively). Per-hour p-values are
combined by Fisher's method.

KS catches slow **drift** where dispersion catches **clumping**, so on a
real source it is usually significant — intraday traffic ramps. That is a
fact about λ̂(h) being one rate per hour, not a fault, and the report says
which of the two it is seeing rather than printing a failing p-value on
every source.

## 3. The verdict

| D detrended (≤ 60 s) | period | verdict | what it means for the detector |
|---|---|---|---|
| ≈ 1 | – | **online** | calibrated; nothing to change |
| > 1.5 | consistent, T | **batch_regular** | every inter-batch pause is a false outage |
| > 1.5 | none | **bursty** | threshold is optimistic by roughly D |
| < 0.7 | – | **clocked** | conservative; the detector will under-report |

## 4. What follows

**Quasi-Poisson.** An overdispersed count needs a proportionally longer
silence for the same significance, so the threshold becomes D·(−ln α). D is
read at **the scale the detector actually works on** — a gap that just
reaches the threshold is −ln(α)/λ̂ seconds long — not at the worst scale in
the table. Reading it at 1 s would size the correction by clumping the
detector never looks at; at 900 s, by the ramp it does not model.

The factor is never scaled *down*. A stream more regular than Poisson makes
the detector conservative, which is the safe direction.

**A length floor.** With a schedule of period T, `min_gap ≈ 2T` is the real
answer and the factor is the crude one: a silence of one batch period is
not an outage by construction, whatever μ says about it.

Neither is applied automatically. `detect_gaps` is untouched — a
measurement that went wrong must not be able to blind the detector, and the
operator is the one who knows whether last Tuesday's load was
representative.

## 5. Window: the profile and the sample have different clocks

λ̂(h) is anchored at the **watermark** and does not follow the viewport.
That is settled in [`chart-app.md`](chart-app.md): keying the profile on the
request window made the same gap score μ = 25.1, 40.9 or 63.3 depending on
which slice the client asked for. The check tests the model the detector
runs on, so it must take that model as it stands.

The **sample** does follow the viewport, because which stretch of stream to
characterise is a legitimate question — "what did this week look like" is
not the same question as "what does the profile say".

`core.stream_stats.stream_window` is the authority, not the client:

- below a day, widened to one — D(Δ) has to be spread across hours-of-day,
  or a beat that is plain at three in the morning stays invisible inside
  midday traffic, and hourly λ̂ needs several hours before pooling means
  anything;
- past the watermark, **moved back rather than cut short** — cutting would
  hand back under a day exactly when the operator is looking at the newest
  traffic, which is the most common way to arrive here;
- clamped to `retention_floor`, capped at seven days.

Whatever it did comes back in `window.notes` and is printed, because a
verdict only means something together with the stretch it was measured
over — and without it the reader cannot tell why zooming does not change
the answer.

## 6. Cost

Sampled hours, not scanned seconds, is the knob: up to
`MAX_SAMPLE_HOURS_WEB` = 24 from the chart and 168 from the CLI, picked
round-robin across hours-of-day (newest first within each) so a budget of
six buys six different times of day rather than six consecutive ones. The
pick is deterministic, so the CLI and the chart report the same numbers for
the same window — verified: both produce D = 1.03 / 1.12 / 1.25 / 1.63
across 1–60 s on the same six hours of `payments`.

The web path carries `STARCUBES_STREAM_TIMEOUT_MS` (default 20 s, clamped
to 30 s in the view) on top of the hour cap. uwsgi's `harakiri = 120` kills
the **whole worker** and the other requests it is holding, which is why
`rollup_advise` and friends never run in a request at all; this endpoint
stays far below it by construction.

## 7. What this cannot do

- **A period below the finest bin.** The scan is 1-second; a burst pattern
  faster than that is a single busy second.
- **Mixed traffic.** A source carrying online traffic *and* a nightly batch
  is judged as one stream unless the filters separate them first. The
  filters are the chart's, so that is a question you can ask — by filtering
  before measuring.
- **A verdict about a span it did not sample.** The character of a stream
  changes; the same source read over two different days can legitimately
  come back `online` and `bursty`, which is why the window is printed with
  the answer.
- **Anything about measures.** The test is defined on "did a row fall into
  this second", so both entry points are `count(*)`-only, exactly like the
  overlay.

## 8. Files

| File | Role |
|------|------|
| `core/stream_stats.py` | the scan, the four tests, the verdict; Django-free |
| `sources/management/commands/stream_check.py` | the CLI |
| `viz/views.py:stream_check_json` | the chart endpoint, over `_load_gap_profile` |
| `viz/templates/viz/chart.html` | `#stream-toggle`, `#stream-panel` |
