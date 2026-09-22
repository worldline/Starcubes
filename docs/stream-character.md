# Stream character

Research note: how much of a datasource's transaction stream is online
traffic and how much arrives as batches, how you would tell at any rate,
and what follows for the gap detector.

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

`manage.py stream_check` and the chart's **Stream character** button test
it. Both write nothing: the report recommends, it does not correct. A bad
measurement must never be able to silence the detector.

## 2. What is actually being tested

H₀: the arrivals are a Poisson process with intensity λ̂(h).

### 2.1 The axis is Λ, not seconds

The first version of this module measured at six fixed bin widths, 1 s to
900 s, and ran every test inside one hour. That works on a source carrying
more than a row a second and refuses to answer on anything else — and
"anything else" is the normal case, because the check runs behind the
chart's filters and a stream is usually *one* interface, customer or
transaction type. Unfiltered payment traffic runs 1.7 rows/s; one
transaction type of it runs 450 rows/day, and every hour of that fell
below the old minimum of 60 rows, so all of them were discarded before a
single test ran.

Time-rescaling removes the whole problem. With

    u = Λ(t) = ∫ λ̂ dt

an inhomogeneous Poisson process of intensity λ̂ becomes a **homogeneous
one of rate 1** in u. A scale is then a μ — an expected row count, a pure
number — and the same ladder measures a stream at 1 000/s exactly as it
measures one at 300/day. The ladder is geometric in μ, √2 per step, from
the resolution floor upward.

It also puts the **detector's own scale** inside reach, which is the point
of the exercise. `detect_gaps` decides at μ = 20.76; that is 12 s on
unfiltered payment traffic and 4 320 s on one transaction type of it. No
list of second-widths can show both, and the one it cannot reach is always
the one that matters.

**Every window follows the axis, not only the bin width.** That is the
part that costs a rewrite if it is missed: a detrending window of 900 s
holds four expected events on a stream of 0.0048/s, at which point the
"trend" is the data and the dispersion collapses to 0.12 — a number about
the smoother and nothing else. Sample minimum, detrending window and
period search all count expected rows.

The hour as a unit of analysis disappears with it. It was never needed:
after rescaling, a Poisson process restricted to a disjoint union of
intervals is still a rate-1 process on [0, Λ_tot], so a sample made of
scattered whole hours is as legitimate as a contiguous one.

### 2.2 Three expectations, and why all three are reported

μ can be had three ways, and the differences between them are where the
information is.

| λ from | what a departure means |
|---|---|
| `shape` — the sample's own hour-of-day profile | the arrivals are not spread the way a Poisson process spreads them |
| `model` — λ̂(h), the profile the detector integrates | the detector's own model does not fit, level included |
| `detrended` — `shape` times a slow level fit | the arrivals **clump**, beyond any drift in the rate |

`model` alone would be useless as a verdict: an ordinary quiet Sunday is a
level error against a seven-day profile, and calling that a batch job would
flag every source. `shape` alone cannot see that the profile has drifted.
And `shape` alone confuses a **ramp** with **clumping**, which is the
distinction the whole exercise turns on — see 2.3.

Note what `detrended` is: the day profile **multiplied** by a slow level
fit, never a plain moving average of the counts. A Λ-sized window is many
hours wide on a thin stream, so a plain average irons the hour-of-day shape
flat and then reports the missing shape as clumping — measured, that alone
put D = 2.0 at the scale the detector decides on, on a stream that is
Poisson there.

All three are evaluated on **one binning per row of the table**, the edges
taken from `detrended`. That is what lets the three D values in a row be
subtracted from each other, which is exactly what the drift note does.

### 2.3 The trap: inhomogeneity, at two levels

Computed naively as Var/Mean over a day, D measures the **day profile** and
declares every source a batch job. That one is obvious and is handled by
the expectation coming from the profile, not from a global mean.

The second level is not obvious. Even inside one hour the rate is not
constant — traffic ramps. λ̂(h) models none of that, so the ramp lands in
the residuals and D climbs with the bin width. Measured on a synthetic
stream ramping 1/s → 5/s across the hour, Poisson at every instant:

| Δ | 1 s | 5 s | 15 s | 60 s | 300 s | 900 s |
|---|---|---|---|---|---|---|
| D shape | 1.43 | 3.16 | 7.49 | 27.3 | 140 | 480 |
| D detrended | 1.08 | 1.12 | 1.16 | 1.23 | – | – |

Read `D shape` at 900 s and you call ordinary intraday traffic a batch job.
The defence is the detrended expectation, and on the μ axis it is also why
the **reading is taken at the fine end**: drift only grows with μ, so the
coarsest row of the table is the most contaminated one, not the safest.
The coarse rows stay in the table, where a rising tail is informative, but
they decide nothing.

### 2.4 Dispersion over μ

    X²(μ) = Σᵢ (nᵢ − μᵢ)² / μᵢ          D(μ) = X²(μ) / df

D is the index of dispersion: **1 is Poisson, above is clumped, below is
more regular than chance.** The degrees of freedom subtract what the
expectation cost — 24 for the hour-of-day profile, nothing for `model`
which was measured elsewhere, and for the trend **one per smoothing
window**. Not one per `DETREND_EVENTS` rows: those two are the same number
only by coincidence, and the first reading of it charged 715 against 646
bins, drove the dof to 1 and reported D = 2945 for a stream sitting at 9.

p-values come from Wilson–Hilferty, exact for the small even df that the
periodicity sum produces. There is no scipy in `requirements.txt` and five
formulas do not justify adding it; numpy arrives with pandas and is already
used in `core/audit.py`.

### 2.5 Emptiness — the test that matters to the detector

Under H₀, P(nᵢ = 0) = e^{−μᵢ}; the observed share of empty bins against
that is the most directly relevant test of the set, because **emptiness is
exactly what the detector reacts to.** An excess of empty bins *is* the
false-positive rate, measured without a model in between.

It also earns its place by dissenting. On real payment traffic the
dispersion said D = 4.4 at 60 s, while empty seconds ran 33.74% against
33.31% expected — essentially a match. The overdispersion was in the
*level*, the rate wandering between minutes, not in the silences. The
recommended factor is then a safe bound, not a fault, and the report says
so instead of leaving the operator to act on the scarier number.

On the μ axis it earns its place twice, because it is half of what
produces the headline.

### 2.6 From two moments to a batch share

Read the stream as a **cluster process**: clusters arrive Poisson, each
carries C rows. Two statistics already in hand pin down C's first two
moments, and neither needs a new query:

- the empty-bin share. The *clusters* in a bin are Poisson with mean
  μ/E[C], so P(bin empty) = e^{−μ/E[C]} and **E[C] = μ / −ln p̂₀**.
- the dispersion. For a compound Poisson count,
  **D = Var/Mean = E[C²]/E[C]**.

With a two-point cluster size — a share of clusters carries B rows, the
rest carry one — those two moments identify the model exactly:

    m₁ = E[C],   m₂ = D·m₁
    B = (m₂−1)/(m₁−1) − 1,   p = (m₁−1)/(B−1),   f_batch = p·B/m₁

`f_batch` is the number an operator asked for: **the share of rows that
arrive as part of a batch.** B is how big the batches are. Section 8 has
the check against synthetic streams with a known answer.

Three things have to be right around it, and each was wrong once:

1. **Which D.** The detrended one, or a drifting rate is counted as
   clustering.
2. **Which scale.** The bin has to be wider than the burst before a cluster
   counts as one object; below that the burst is split across bins and the
   share reads low. So the ladder is walked upward until the estimate stops
   moving (`PLATEAU_TOL`) — on a 35%-in-batches-of-105 stream it settles
   immediately at 0.352 / 0.364 / 0.372, on a thin one it is still climbing
   three steps in and has to go further. Where it never settles, the
   **finest** usable scale is read, per 2.3.
3. **When not to say it at all.** Near B = 1 the inversion divides by
   (E[C] − 1) and swings on noise: on unfiltered payment traffic the share
   bounced 0.47 / 0.35 / 0.26 / 0.38 across four neighbouring scales while
   E[C] sat calmly at 1.14…1.25. What that stream has is *pairs* — two rows
   inside the two seconds the scan can just about resolve, a coincidence
   rather than a batch job. Below `MIN_MEANINGFUL_BATCH` the verdict says
   so and reports E[C] instead of a decomposition that is noise.

The usability test on p̂₀ is a **precision** test, not a band. A heavily
batched stream legitimately leaves 99.3% of its bins empty, and with a
hundred thousand bins that reads E[C] to within 4%; rejecting it as "too
close to 1" threw away the only scales where the estimate was right. What
is required is that the standard error of −ln p̂₀ — the quantity E[C]
divides by — stays under `LOG_P0_MAX_RSE`.

### 2.7 Periodicity — regular versus irregular

Dispersion says "bursty"; it cannot say "bursty every fifteen minutes",
which is the number an operator can act on.

The statistic is the periodogram of the residual r = n − μ, read against
the **absolute** Poisson noise floor: Var(r_s) = μ_s under H₀, so the
ordinates |FFT(r)_k|² have expectation Λ_block and, divided by it, are
Exp(1). Summed over blocks that is Gamma(n_blocks), which is `_chi2_sf` at
df = 2·n_blocks.

The comb bites twice here, and both bites are worth recording.

**Never normalise by the total power.** Fisher's g = max/Σ is the textbook
choice and it fails on exactly the signal it is wanted for: a batch is a
near-Dirac comb, a comb spreads its power over every harmonic of 1/T — a
300 s beat inside a 3600 s block lights up 150 ordinates — so g collapses
to ~1/150 and the sharpest imaginable schedule lands at p ≈ 0.01, just
short of significance. Against a known floor the fundamental still stands
out by orders of magnitude, because nothing divides it by the power its own
harmonics took.

**Then take the fundamental, not the strongest ordinate.** With an absolute
floor every harmonic is wildly significant, so the maximum is an arbitrary
one of them: measured, a clean 300 s schedule reported 50 s (the 6th
harmonic) and an hourly one reported 8.4 s. The fundamental is the
**lowest** significant frequency, believed only when the rest of the
significant set sits on its multiples (`PERIOD_HARMONIC_SHARE`) — that
check is what separates a comb from a few unrelated peaks.

Blocks are runs of **contiguous** scan, and that is why a thin stream is
scanned whole rather than sampled: a 3600 s block resolves a 300 s period
only to ±25 s, an 86 400 s block to ±1 s. The blocks must also agree —
a schedule shows up in most of them, one lucky block visibly does not.

### 2.8 Uniformity (KS)

Conditioned on N arrivals, an inhomogeneous Poisson process places them so
that Λ(t)/Λ_tot is uniform — so the empirical CDF read off the cumulative
counts must lie on the cumulative intensity. One test over every row in the
sample, rather than one per hour with the thin hours discarded: that
per-hour split is precisely what used to refuse a filtered stream, and the
pooling bought nothing the single test does not have.

KS catches slow **drift** where dispersion catches **clumping**, so on a
real source it is usually significant — intraday traffic ramps. That is a
fact about λ̂(h) being one rate per hour, not a fault, and the report says
which of the two it is seeing rather than printing a failing p-value on
every source.

## 3. The verdict

| batch share | period | verdict | what it means for the detector |
|---|---|---|---|
| < 5%, or clusters below `MIN_MEANINGFUL_BATCH` | – | **online** | calibrated; nothing to change |
| ≥ 5% | consistent, T | **batch_regular** | every inter-batch pause is a false outage |
| ≥ 5% | none | **bursty** | threshold is optimistic by roughly D |
| < 5% and D < 0.7 | – | **clocked** | conservative; the detector will under-report |

The codes are unchanged from the first version — the CLI and the chart both
key on them — but they are read off a share now rather than off a
dispersion threshold, and `bursty` carries a band in words: mostly online
with some batching, part online part batch, predominantly batch.

## 4. What follows

**Quasi-Poisson.** An overdispersed count needs a proportionally longer
silence for the same significance, so the threshold becomes D·(−ln α). D is
read at **the scale the detector actually works on**, and on the μ axis
that is no longer an approximation: μ = 20.76 is simply a row in the
ladder. Note that the factor is read across *all* scales, not only the ones
the cluster estimate could use — that estimate needs empty bins and runs
out well before μ = 20.76 on a busy stream, and tying the two together put
the detector's own scale back out of reach.

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

- below a day, widened to one;
- **a thin stream widened all the way to seven days**, because the sample
  minimum is a count of rows and one day of 450 rows/day does not reach it.
  This costs nothing at those rates and is the difference between an answer
  and a refusal;
- past the watermark, **moved back rather than cut short** — cutting would
  hand back under a day exactly when the operator is looking at the newest
  traffic, which is the most common way to arrive here;
- clamped to `retention_floor`, capped at seven days.

Because the widening needs the rate, the **profile is loaded before the
window is decided** — in the CLI and in `viz/views.py` alike. Nothing is
circular about it: the profile is watermark-anchored and does not depend on
the sample window at all.

Whatever it did comes back in `window.notes` and is printed, because a
verdict only means something together with the stretch it was measured
over — and without it the reader cannot tell why zooming does not change
the answer.

## 6. Cost

Sampled hours are the knob for a **busy** stream: up to
`MAX_SAMPLE_HOURS_WEB` = 24 from the chart and 168 from the CLI, picked
round-robin across hours-of-day (newest first within each) so a budget of
six buys six different times of day rather than six consecutive ones. The
pick is deterministic, so the CLI and the chart report the same numbers for
the same window.

Below `CONTIGUOUS_MAX_RATE_PER_S` the cap does **not** apply and the window
is scanned whole. At those rates seven days are a few thousand rows and one
`GROUP BY` — cheaper than the sampled form it replaces, and it buys the
contiguity the detrender and the periodicity blocks both want. A contiguous
run is also one `BETWEEN` rather than an OR chain of 168 terms.

Measured against the live warehouse through the chart endpoint: 0.46 s
unfiltered, 1.2 s for a filtered thin stream scanning seven days whole.
The web path carries `STARCUBES_STREAM_TIMEOUT_MS` (default 20 s, clamped
to 30 s in the view) on top. uwsgi's `harakiri = 120` kills the **whole
worker** and the other requests it is holding, which is why `rollup_advise`
and friends never run in a request at all; this endpoint stays far below it
by construction.

## 7. What this cannot do

- **Resolve a burst shorter than the scan.** This is the one rate
  dependence the Λ axis does not remove, and it is a real one: the scan is
  1-second, so the finest honest scale is μ_floor = λ·1 s. On unfiltered
  payment traffic that is μ ≈ 3.4, and clusters below about three rows
  cannot be seen; on one transaction type of it there is effectively no
  floor at all. Below the floor the binning degenerates outright — measured,
  D = 30.9 with 15% empty bins against 78% expected. When the reading sits
  at the floor the report says so and calls the share a lower bound.
- **Mixed traffic.** A source carrying online traffic *and* a nightly batch
  is judged as one stream unless the filters separate them first. The
  filters are the chart's — and `--filter` on the CLI — so that is a
  question you can ask, by filtering before measuring.
- **A verdict about a span it did not sample.** The character of a stream
  changes; the same source read over two different weeks can legitimately
  come back `online` and `bursty`, which is why the window is printed with
  the answer.
- **Anything about measures.** The test is defined on "did a row fall into
  this second", so both entry points are `count(*)`-only, exactly like the
  overlay.
- **Separate a timetable from random cluster arrivals in the share.** The
  empty-bin reading assumes clusters arrive at random; a strict timetable
  biases it at coarse μ. The period is what tells you there is a timetable,
  and the share is read at the fine end where the bias is smallest.

## 8. Validation

`manage.py check_stream_stats` runs these and fails if any of them drifts.
Synthetic streams with a known answer, seven days at 1 s, through
`characterize_counts` — arrays only, no database, which is what makes them
usable as fixtures at all:

| stream | truth | measured |
|---|---|---|
| 900 rows every 300 s | f = 1.00, B = 900, T = 300 s | f = 1.000, B = 896, T = 300 s |
| Poisson 1/s | f = 0.00 | f = 0.000, no period |
| Poisson 0.65/s + 105 every 300 s | f = 0.35, B = 105, T = 300 s | f = 0.356, B = 103, T = 300 s |
| Poisson 0.0033/s + 12 every 3600 s | f = 0.50, B = 12, T = 3600 s | f = 0.501, B = 11.7, T = 3600 s |
| 40 rows every 900 s | f = 1.00, B = 40, T = 900 s | f = 1.000, B = 40.0, T = 900 s |
| Poisson 0.005/s | f = 0.00 | f = 0.000, no period |

And against the live warehouse, the case that prompted the rewrite:

| stream | rows | verdict |
|---|---|---|
| `payments`, unfiltered (1.7/s) | 142 928 over 1 day | online; clusters of 1.13 — pairs, not batches; factor ×1.81 at μ = 19.5 |
| `payments`, `txn_type = 4428` (450/day) | 2 915 over 7 days | online; f = 0.00; factor ×1.28 at μ = 19.7, where a gap is 72 min |
| `payments`, `issuer = 41400` (800/day) | 5 554 over 7 days | online; f = 0.00; factor ×1.29 |

The second of those is the one the old module answered with "every sampled
hour held fewer than 60 rows — too thin to judge the shape of the stream".

## 9. Files

| File | Role |
|------|------|
| `core/stream_stats.py` | the scan, the Λ axis, the four tests, the verdict; Django-free |
| `sources/management/commands/stream_check.py` | the CLI, including `--filter` |
| `sources/management/commands/check_stream_stats.py` | the fixtures of §8, as a check that fails |
| `viz/views.py:stream_check_json` | the chart endpoint, over `_load_gap_profile` |
| `viz/templates/viz/chart.html` | `#stream-toggle`, `#stream-panel`, `renderStreamPanel` |
