# CONCLUSIONS.md — what the data has actually shown

Written for whoever picks this up next, human or agent. `AGENTS.md` says what
the place is and what has been learned the hard way; this file says what the
*measurements* support, what they killed, and what is still missing. Every number
here was produced by the toolkit in this repository on the archive in `data/`,
and the recipe to reproduce the big one is at the bottom.

Read it as a decision record, not as documentation. Where a claim is conditional,
the condition is stated.

---

## 1. The four dials, in order of leverage

**Where this ends up, if you read nothing else.** The signal is the cheap part; the universe,
the decision grid, the exit and the size of the position are what decide the result. The best
configuration this repository has measured is:

> **take the handful of pairs that trade the most, hold each one only while its close is above
> its own ~50-bar mean, size the whole book to a 25-30% annual volatility target, and never
> rebalance the names against each other.**

Over the last five years (2021-10 … 2026-10, daily bars, 0.1%/side) the version chosen by a
rule — the ten busiest pairs as of the window's first day — returns **+91.4% at Sharpe 0.87
with a −17.5% drawdown**, against **−28.2%** for simply holding those ten. §1.4 and §1.5 carry
the measurements, §1.3 the filter that keeps dying names out, §1.6 why costs decide everything
else, and §2 what has been tried and rejected.

These are ordered by how much they move an outcome, and the order is itself a
result: the *frequency* of decisions beats the *cost* of trading, and the choice
of signal is not on the list at all because it is worth +0.006 Sharpe across 976
series — a coin flip. Read them as four dials, not four facts:

1. **how often you decide** (§1.1) — worth +0.38 Sharpe, and it is a filter, not a
   discount: the weekly grid wins at a 0.00% fee as well as at 0.20%.
2. **what you are allowed to trade** (§1.2) — the universe decides whether the
   median outcome is a dying listing or a surviving one.
3. **how much you trade** (§1.3) — once the structure is set, turnover decides the
   net result; at 0.25% per round trip, 250 trades a year costs 62.5% of capital.
4. **how much is in the market** (§1.5, §1.6) — exposure sets the drawdown, and it
   scales return and drawdown together rather than improving either.

What actually works is in §1.4, what the distribution looks like is in §1.5,
what happens when the same rule is applied to a whole universe is in §1.7, and
what was killed by measurement is in §2.

### 1.1 Frequency, not the signal

The rule everyone calls "trend following" is two decisions: *what* to compare the
price with, and *how often* to look. Measured separately, on 976 hourly series
with the same 720-bar horizon:

| what changes | median Sharpe gain | helps on |
|---|---|---|
| decision grid: every bar → weekly (**TSMOM**) | **+0.38** | **82%** of assets |
| decision grid: every bar → weekly (**SMA**) | **+0.39** | **81%** of assets |
| signal: SMA → TSMOM, weekly grid | **+0.006** | 51% (a coin flip) |
| signal: SMA → TSMOM, every bar | +0.034 | 54% (a coin flip) |

Read the two halves against each other: **the grid is worth about +0.39 Sharpe
and the choice of signal is worth nothing** on the median asset. Without the grid
both rules are equally bad (`every_bar` median Sharpe −0.57 for each, 18–19%
profitable); with it both become usable (weekly: −0.13 and −0.15, 38–39%
profitable). Monthly is *worse* than weekly for both (−0.31 / −0.32), so this is an
optimum rather than "slower is better".

Out-of-sample on BTC-USDT 1h (train 3000 / test 1000, fee 0.1%/side) the same
split shows up with money attached:

| rule | out-of-sample | Sharpe | max DD |
|---|---|---|---|
| SMA 720, every bar | **−2.18%** | −0.01 | −82.5% |
| SMA 720, weekly grid (lookback chosen per window) | **+322.72%** | 0.38 | −75.2% |
| TSMOM 720, weekly grid (lookback chosen per window) | **+978.53%** | 0.64 | −54.7% |
| holding BTC over the same spans | +579.09% | 0.35 | −77.6% |

So the honest answer to "does TSMOM win only because of its fixed rebalancing
windows" is: **mostly, yes** — the grid is what turns a losing rule into a working
one, and it does so for either signal. What is left over is asset-specific: on BTC
the signal still triples the result (+323% → +979%), while across the cross-section
it wins on 51% of assets for a median +0.006 Sharpe. TSMOM is not a better signal
than an SMA in general; it is a slow look at a slightly earlier reference, and on a
few assets (BTC among them) that reference is worth real money.

**It is not a cost effect.** If the weekly grid only saved commission, cheaper fees
would move the optimum towards more frequent decisions. They do not: sweeping the
grid against the fee on 751 hourly series, the best frequency is **one week at
0.00%, 0.05%, 0.10% and 0.20% per side alike** (median Sharpe −0.10, −0.11, −0.11,
−0.13, against −0.26 for reading the same signal every bar). Deciding every bar is
worse even when trading is *free*, so the grid is a **filter, not a discount**: a
slow 30-day signal read hourly whipsaws across zero — 164 trades against 12 — and
sampling it weekly removes that noise before it costs anything. The commission
effect is real but second-order: it accounts for about 0.02–0.03 of the difference,
which matches the trade arithmetic (a daily grid pays ~5%/year at 0.2%/side).

That also explains why a dead zone did nothing on top of it: the weekly grid is
already the de-noising, and a threshold repeats the work.

**The optimum is a broad plateau, not a knife edge.** The same signal on grids from
one hour to 30 days: median Sharpe −0.57 (1h), −0.37 (6h), −0.32 (1d), −0.21 (3d),
**−0.13 (1w)**, −0.19 (2w), −0.31 (30d). Three days to two weeks sit within 0.06 of
the peak, so "weekly" is a basin rather than a lucky value — which is what makes the
earlier finding trustworthy rather than a fit.

**The day of the week barely matters.** Shifting the weekly grid to each of the
seven days: Thursday (the epoch day, and the default the repository inherited) is
best at −0.13, Wednesday ties it, the weekend is worst at −0.23. Per asset Thursday
beats the median of the other six days on 58% of series for +0.04 Sharpe — real
enough to prefer a weekday, too small to be anything but a tie-breaker, and it fits
the liquidity picture (turnover peaks 13:00–17:00 UTC and the weekend is quiet).

**And the lookback cannot be judged independently of the grid.** The best lookback
in this repository (720 bars) was measured at a fixed weekly grid, which is exactly
four samples per horizon; a 336-bar lookback read weekly gets two samples per
horizon and looks worse than it is. Any statement of the form "this lookback is
best" is conditional on the frequency it was read at.

The obvious follow-up was to let the walk-forward choose the frequency too, since
every run above held it fixed at 168 bars. Doing that (`--grid lookback=336,720
--grid rebalance=24,168,720`) made it **slightly worse, not better**:

| BTC-USDT 1h, train 3000 / test 1000 | out-of-sample | Sharpe | max DD |
|---|---|---|---|
| frequency fixed at 168 (weekly) | **+978.53%** | **0.64** | −54.66% |
| frequency chosen per window from 24/168/720 | +900.74% | 0.62 | **−49.67%** |

and the choice it made was scattered across all six combinations (19, 15, 12, 10,
10 and 9 wins out of 75) — no stable answer to find. So the frequency has to be in
the right *ballpark* (weekly beats daily and monthly for both signals) and is not
worth optimising: each extra candidate in the grid is another chance to pick a
noise winner. Set it, do not tune it — the opposite of what one expects from a
parameter that buys the most.

**Per-pair tuning does not transfer — measured on 765 daily pairs.** The same rule
(hold while the close is above the close `lookback` bars ago, re-decide every `rebalance`
bars, 0.1% a side) was run over a 4x5 grid of lookbacks and rebalances for every pair, and
then the best configuration found in the **first half** of each pair's history was applied
to the **second half**:

| what | median Sharpe |
|---|---|
| best of 20 configurations, chosen on the first half | **+0.58** |
| the same choices, on the second half | **−0.36** |
| a fixed, untuned 30/7, on the second half | **−0.35** |

The best configuration in the first half is also the best in the second half in only
**43 of 765 cases (6%)** — a random pick would score 5%. So a "best period per pair"
exists on the data it was chosen on and is worth *exactly nothing* afterwards: tuned and
fixed end up within 0.01 Sharpe of each other. What *is* a population property is the grid
itself — median Sharpe across all pairs, by cell:

| lookback | rebalance 1 | 7 | 14 | 30 | 90 |
|---|---|---|---|---|---|
| 7 | −0.33 | **−0.11** | −0.19 | −0.26 | −0.29 |
| 14 | −0.32 | −0.19 | −0.30 | −0.44 | −0.43 |
| 30 | −0.28 | −0.19 | −0.24 | −0.29 | −0.28 |
| 90 | −0.32 | −0.31 | −0.31 | −0.31 | −0.41 |

Every cell is negative (the median asset loses under every setting — §1.2 again), reading
the signal every bar is the worst corner, and one week/four weeks is the best. That is
§1.1 restated on daily bars: **the grid is a population parameter worth setting; the
pair-level optimum is noise.**

### 1.2 The universe is a graveyard, and the cross-section ranks it by pulse

996 pairs, of which the top 10 are 61.7% of all turnover and the top 200 are 94%.
The median pair has a maximum drawdown of **−97.5%**, is underwater 99% of the
time, and has a median daily return of −0.21%. Since 2023-10-06 the median pair
is at **0.31x** while BTC is 3.02x, SOL 4.91x, XRP 2.86x, ETH 1.63x; 81% of pairs
are in the red and the median maximum drawdown is −82.1%. Half a year after
listing, only 25% of pairs are above their day-one price and the median is
**−49.9%**.

**Two facts about what "survival" means here, both measured on the daily archive.**

First, **there are no delisted pairs in it**: of 982 daily series, **not one** stopped
printing bars before the end (0 of 982 by 90 days, 0 by 365). The collector fetches the
exchange's current listing, so everything the archive contains is, by construction,
something that is still listed. Any survival rule tested here is therefore tested on a
survivor-biased universe, and the numbers are an upper bound.

Second, **still listed is not alive**. Measured from each series' own peak to today, the
median pair is **−93.6%**, 59% are more than 90% below their peak, and the median total
return is −86.5%.

| history | series | median from its peak | median total return | more than 90% below peak |
|---|---|---|---|---|
| 7+ years | 78 | −96.3% | −84.4% | 71% |
| 5–7 years | 160 | **−98.3%** | −93.9% | 80% |
| 3–5 years | 174 | −95.3% | −91.9% | 72% |
| 2–3 years | 122 | −97.1% | −93.9% | 78% |
| under 2 years | 448 | −86.1% | −75.9% | 40% |

So a long listing is **not** a health screen — the 5–7-year cohort is the most damaged of
all, and the youngest cohort looks best only because it has not had time to fall from its
listing pump. What `--min-history` actually buys is **continuity and liquidity**: a pair
that has traded for years can still be executed in and out of, and the fresh listings are
where the noise lives. That is why it cut the commission bill from 145% to 17% of capital
and the volatility from 111% to 33% in §6, and why 92% of 5+ year series beat holding
under a trend rule (§1.5) — not because they did not crash, but because they still trade.

**Consequence:** any statistic over "all assets" is dominated by dying listings,
and any rule that *selects* from the whole universe selects them on purpose.
Cross-sectional momentum over all pairs returned **−81.2%** against −47.4% for
equal weighting; restricted to the top 5 by momentum it returned +2.6% over nine
years while paying 46.8% of capital in commission and drawing down −97.4%.

**It is the ranking, not the universe.** Re-run with the history filter that the
survivor argument asks for (412 names with three years of bars instead of 886 with one),
cross-sectional momentum ranked by trailing 30-day return, rebalanced monthly:

| universe | top 20% by momentum | equal weight of the same universe |
|---|---|---|
| all pairs (886) | −81.39% (Sharpe −0.27, DD −91.2%) | −49.31% |
| **three years of history (412)** | **−82.49%** (−0.28, −91.4%) | −40.66% |
| three years, top **10 names** | −77.70% (−0.17, **DD −98.4%**) | −40.66% |
| three years, long/**short** | **−100%**, dead by 2017-12-20 after three rebalances | −40.66% |

The filter moved the *passive* baseline by nine points (−49% → −41%) and the momentum
book by none (−81% → −82%). So the graveyard explains why the *universe* loses, not why
the *ranking* loses: selecting the strongest trailing return picked names that then did
worse than the average pair, in both universes.

**Half of that conclusion was too strong — it is the *liquidity* filter, not the age
filter, that rescues a ranking.** Re-run on the universe the book itself uses (top 20 by
trailing turnover with three years of history, 2019-03 … 2026-09, monthly decisions):

| universe | buy everything that rose | buy the top 20% by rank | buy everything that fell | hold it all |
|---|---|---|---|---|
| every pair (≈390 names) | **6.53x** (Sharpe 0.43) | 2.76x (0.20) | 1.71x (0.13) | 3.14x (0.26) |
| **top 20 by turnover** | 2.96x (0.24) | **6.04x** (0.34) | 0.43x (−0.19) | 2.54x (0.21) |

Two things follow. A *history* filter over 412 names left the ranking at −82%, while a
*turnover* filter over 20 names puts it at +504%: liquidity, not age, is what makes a
cross-section usable. And on a broad universe the sign filter beats the ranking (6.53x
against 2.76x) while on a liquid handful the ranking wins — with 140 names you are buying
beta, with three you are buying concentration. That is the opposite of the cross-sectional
premise, and it is the cleanest statement of why this repository's working rule is
**time-series** momentum (compare an asset with its own past) and not the **cross-sectional**
kind (compare assets with each other).

### 1.3 Screening out the dying assets

The universe is a graveyard (§1.2), so the obvious question is whether a filter can refuse
the names that are going to die. `kcs-portfolio` now takes health gates, every reading taken
from bars at or before the rebalance date: the close against its own N-bar mean
(`--trend-gate`), the distance below the pair's own running peak (`--max-below-peak`), a
median quote-turnover floor and an annualised volatility floor (`--min-turnover`,
`--min-volatility`). Measured on the 7/7 sign book over 836 USDT pairs, against a placebo
that rotates the gate readings between symbols — same thresholds, same average breadth,
zero information:

| filter | total | Sharpe | max DD | breadth | losers still traded | placebo (median / best Sharpe) |
|---|---|---|---|---|---|---|
| none | +1,141% | 0.30 | −93% | 109 | 100% | — |
| trend 100 | **+4,372%** | **0.52** | −91% | 44 | — | — |
| trend 200 | +2,675% | 0.49 | −87% | 31 | 72% | −33% / 0.09 |
| peak within −30% | +177,786% | 0.90 | −83% | 13 | — | −54% / 0.45 |
| peak within −50% | +7,779% | 0.56 | −92% | 21 | 84% | — |
| turnover ≥ 1e4 | +408% | 0.21 | −90% | 96 | 100% | — |
| turnover ≥ 1e6 | **−87%** | −0.30 | −96% | 21 | 64% | −1% / 0.11 |
| volatility ≥ 10% | +701% | 0.26 | −89% | 107 | 99% | — |

**Use the trend gate.** A close above its own 100–200-bar mean is a plateau (100/200/300 bars
give Sharpe 0.52/0.49/0.48; 50 is too short), it lifts Sharpe from 0.30 to ~0.50, cuts the
drawdown from −93% to −83…−91%, refuses 72% of the pairs that later lost money, and survives
the placebo decisively. Out of the 2019–21 bull it still helps: over the last five years the
ungated book loses 65% and the gated one 39%.

**Distance below the running peak is stronger and more dangerous.** Tightening it improves
the result monotonically (−90% → +1,552%, −50% → +7,779%, −30% → +177,786% at Sharpe 0.90),
which is a slope rather than a plateau, and the tighter settings run a 13–21 name book whose
*placebo* still reaches Sharpe 0.45 — real signal mixed with small-book luck. It is also the
only family that made the recent five years positive (−50% → +19%, −30% → +1,067% at Sharpe
0.75), so it is worth keeping as a *quality* condition with the threshold chosen by
walk-forward, never as a constant fitted here. Top-3 pairs are 6–19% of the profit in every
gated variant, so none of these results rests on one name.

**Do not filter this rule by liquidity.** Every turnover floor makes it worse, up to −87% at
≥1e6/bar: the liquid majors are where it loses (`BTC-USDT` is in the loss column) and its
winners are illiquid early names. That is the opposite of what the cross-sectional *ranking*
needed (§1.2), so the two rules want different universes — a conclusion that only exists
because both were measured against the same engine.

### 1.4 The configuration that works: trend + a volatility target on liquid names

Everything above says the same thing three times: the universe decides, the exit matters, and
sizing changes the shape of the distribution. Put together on the one universe where this
repository's edge has never been fragile — a basket of five liquid majors (`kcs-basket`,
daily bars, 2021-08-04 … 2026-09-30, the 5.16 years every leg shares) — a slow SMA filter
with the position scaled to a volatility target is the best risk-adjusted result measured
anywhere in this project:

| configuration | total | holding the same five | Sharpe | max DD |
|---|---|---|---|---|
| `voltarget-sma` window 50, target 30% | **+126.87%** | +114.23% | **0.99** | −19.9% |
| `voltarget-sma` window 150, target 25% | +74.24% | +114.23% | 0.79 | **−14.2%** |
| `voltarget-sma` window 250, target 40% | +96.08% | +114.23% | 0.59 | −24.0% |
| `sma` window 200, no sizing | +127.84% | +114.23% | 0.42 | −39.9% |
| `tsmom` 30/7 | +151.00% | +114.23% | 0.48 | −41.6% |
| the five held equally | +114.23% | — | ~0.35 | **−78.6%** |

The grid is a **plateau, not a point** — Sharpe by (window × target) on the same window, and
every cell is at least 0.60:

| window | target 20% | 25% | 30% | 40% |
|---|---|---|---|---|
| 50 | 1.02 | 1.00 | 0.98 | 0.93 |
| 100 | 0.75 | 0.73 | 0.71 | 0.67 |
| 150 | 0.86 | 0.84 | 0.81 | 0.77 |
| 200 | 0.74 | 0.72 | 0.70 | 0.66 |
| 300 | 0.68 | 0.66 | 0.64 | 0.60 |

Read it honestly. **The trend rows do not beat the passive hold on return** in this window —
+114% for holding five majors against +74…+151% for the rules — because 2021-2026 was a bull
market for exactly these names. What they buy is **risk**: Sharpe 0.79-0.99 and a drawdown of
−14…−24% against ~0.35 and −78.6%, and the best row (window 50, target 30%) beats the hold on
both. The mechanism is visible in the same report: **only 18.8% of the capital is in the
assets on average** — the rest waits in cash, and the volatility target is what keeps it
there when the market is wild. Two caveats carry real weight: the five names were chosen
knowing they survived (§7 thread 1), and the fees are 3.11% of capital, so this is a
low-turnover configuration, not a cheap one to run on a thousand pairs. The rule-picked
analogue already exists in `kcs-riskparity` (top 5 by trailing turnover, three years of
history, 40% volatility budget, `--trend 200d`): +868% at Sharpe 0.68 with a −47.7% drawdown
over 8.93 years, against BTC's own 0.46 and −82.9%. **The missing piece is the same sizing on
the gated wide book** — and it is now joined: `kcs-portfolio --vol-target` sizes the book,
`kcs-riskparity`'s sizing and `kcs-portfolio`'s universe meet in §1.5, and the answer there is
that a narrow book of liquid names beats a wide one even when both are sized.

### 1.5a Walk-forward: the configuration survives five separate years

The configuration has one five-year window behind it in §1.4, which is exactly the shape of
result that turns out to be a single regime. So it was re-run as five consecutive one-year
windows, with the names chosen by turnover **strictly before each window started** — no
parameter is re-fitted, only the selection date moves:

| window | strategy | same names held | Sharpe | max DD |
|---|---|---|---|---|
| 2021-10 → 2022-10 (the crash) | **−7.85%** | −69.74% | −0.84 | −15.3% |
| 2022-10 → 2023-10 | **−0.30%** | −21.03% | −0.03 | −9.0% |
| 2023-10 → 2024-10 | +41.51% | +209.50% | 1.82 | −10.7% |
| 2024-10 → 2025-10 | +21.55% | +69.09% | 1.35 | −10.8% |
| 2025-10 → 2026-10 | **+11.30%** | −23.99% | 0.77 | −10.5% |

Compounded, the five windows return **+75.9%** with no losing year worse than **−7.85%** and a
worst drawdown of **−15.3%**; the passive hold of the same names compounded to **−52.4%** over
the same five stretches (−69.7%, −21.0%, +209.5%, +69.1%, −24.0%). The rule beats the passive in
three of five years — the two it loses are the strongest bull years, where being only ~18%
invested costs it (+41.5% against +209.5%) — and it wins the disasters by a mile: in the year
that destroyed the sign book (−65%, §1.2) it lost **7.85%**.

Two more things the walk-forward settles. Re-selecting the names every year **does not help**:
+75.9% compounded against +91.4% for the single selection made once at the start, so the
turnover ranking is stable enough that re-doing it is churn. And the same rule on the
hand-picked five majors over the same five windows returns +127.0% — better on return, with the
same shape (it also loses only 7.4% in the crash year) — which says the *rule* carries the
protection and the *names* carry the extra return.

### 1.5 The three approaches on one window, and the answer is not the wide book

The sizing half and the universe half are joined now: `kcs-portfolio` takes `--vol-target`,
`--vol-window`, `--vol-cap`/`--vol-floor`, and it sizes the book from the **unscaled** book's
own completed periods (measuring the account instead would divide by a volatility that
already contains the multiplier and turn the dial into a feedback loop). On the last five
years — the window with no 2021 mania in it — the three candidate approaches are:

| approach | how the names are chosen | total | benchmark | Sharpe | max DD |
|---|---|---|---|---|---|
| wide book: 836 USDT pairs, sign 7/7 + trend gate 200 | by rule, ~31 names | −45.55% | −49.03% | −0.18 | −85.8% |
| the same book + `--vol-target 25%` | by rule, ~31 names | **+37.06%** | −49.03% | **0.18** | −53.8% |
| `kcs-riskparity --top 5 --vol-budget 0.4 --trend 200d` | by rule, 5 names, monthly re-equalised | +50.11% | — | 0.19 | −49.6% |
| basket of 5 majors + `voltarget-sma` 50/30% | **hand-picked** | +126.87% | +114.23% | **0.99** | **−19.9%** |
| basket of 5 + `voltarget-sma` 50/30% | **top 5 by turnover before the window** | +123.62% | +62.64% | 0.87 | −21.0% |
| basket of 10 + `voltarget-sma` 50/30% | **top 10 by turnover before the window** | **+163.94%** | +98.82% | **1.17** | **−16.6%** |

Three conclusions, in order of importance.

**1. The result does not depend on hindsight** — the open thread this repository has carried
since §1.9. The five names a rule could have picked on 2021-08-04 (the top five by trailing
turnover) are `BTC, ETH, XRP, ADA, DOGE`, not the hand-picked `BTC, ETH, SOL, XRP, BNB` (SOL
was not even in the top twelve then). The same rule on that honest five returns +123.62% at
Sharpe 0.87, and on the honest ten **+163.94% at Sharpe 1.17 with a −16.6% drawdown** — better
than the hand-picked basket on every measure. So "a handful of the most traded pairs, a slow
trend filter per name, and a volatility target" is a rule, not a wish.

**2. Sizing is what rescues the wide book, and the narrow book still dominates it.** `--vol-target 25%`
turns the gated wide book from −45.55% into +37.06% and lifts Sharpe from −0.18 to 0.18 while
cutting the drawdown from −86% to −54% (fees fall from 1,559% of capital to 113%) — the single
largest improvement measured on that book. Across five separate yearly windows the sized wide
book compounds to **+46.9%** against **−36.8%** for holding the same universe (§1.5b), so it is
not uninvestable — but the narrow basket returns **+75.9%** with a worst year of −7.85% against
−20.1% and a worst drawdown of −15.3% against −46.1%. **Eight hundred pairs carry three times
the drawdown for two thirds of the return.**

**3. Re-equalising between names destroys the edge.** `kcs-riskparity` uses the same idea
(rule-picked names, a volatility budget, a trend gate) and lands at +50% with Sharpe 0.19,
because it re-equalises the book every month — averaging down into the weakest leg, exactly
what §1.9 measured on the two-name book. The basket holds **fixed weights** and never
rebalances, so a winner keeps its weight while it runs. The comparison is not "which tool is
better" but "rebalancing a five-name trend book is a cost, not a service".

### 1.5b Walk-forward of the wide book's own dials — and a correction

The same five yearly windows, now for the gated wide book (`kcs-portfolio`, 840 USDT pairs,
sign 7/7), with the date window taken by `--from/--to` so each year is measured on its own:

| window | no gates, no sizing | trend gate 200 | gate + `--vol-target 25%` | same universe held |
|---|---|---|---|---|
| 2021-10 → 2022-10 | −72.1% (−86.6% DD) | −66.1% (−82.3%) | **−6.9%** (−46.1%) | −19.3% |
| 2022-10 → 2023-10 | −21.0% | **+17.2%** | −20.1% | −9.0% |
| 2023-10 → 2024-10 | +127.8% | +100.9% | +95.4% (**Sharpe 1.95**, −28.8%) | +45.2% |
| 2024-10 → 2025-10 | +19.8% | +13.8% | **+22.8%** (Sharpe 0.58) | +10.8% |
| 2025-10 → 2026-10 | −37.0% | −36.5% | **−17.7%** | −46.5% |
| **compounded** | **−62.1%** | **−42.3%** | **+46.9%** | **−36.8%** |

**This corrects §1.5.** "A thousand pairs is the wrong shape for this rule" was measured
without the sizing dial and is too strong: sized to a 25% volatility target — in the market
about half the time — the wide book **beats its own passive benchmark by 84 points over five
separate years** (−37% for holding the same 840 names) and its worst year is −20.1% instead of
−72.1%. The trend gate helps in exactly one year (2022-23: +17.2% against −21.0%) and hurts in
the rest; **the volatility target is doing nearly all of the work**, which is consistent with
everything else in this file.

What survives from §1.5 is the *comparison*, not the dismissal: the narrow basket still
dominates on every measure over the same five windows — **+75.9% against +46.9%, a worst year
of −7.85% against −20.1%, and a worst drawdown of −15.3% against −46.1%** (§1.5a). So the wide
book is not uninvestable once it is sized; it is simply dominated by trading the ten busiest
pairs instead of eight hundred, and it carries three times the drawdown for the privilege.

### 1.5c The last dial, `--max-below-peak`, done honestly: re-chosen every window

§1.3 measured this gate on the whole history and found no plateau, which makes a *fixed*
threshold meaningless. The honest test is the one a trader would actually run: choose the
threshold on the **two years before each test year only** (the trend gate and the 25%
volatility target stay fixed), then apply it. Nothing about the test year is read:

| test year | chosen on the prior 2 y | its result | Sharpe | max DD | in market | same rule, no peak gate | hindsight-best in that year |
|---|---|---|---|---|---|---|---|
| 2021-10 → 2022-10 | −50% | **+48.7%** | 1.02 | −30.8% | 48% | −6.9% | −30% → +121.1% |
| 2022-10 → 2023-10 | −30% | **+23.9%** | 0.46 | −38.0% | 51% | −20.1% | −30% → +23.9% |
| 2023-10 → 2024-10 | −30% | **+86.6%** | 1.78 | −20.9% | 53% | +95.4% | off → +95.4% |
| 2024-10 → 2025-10 | −30% | **+72.5%** | 1.73 | −13.8% | 57% | +22.8% | −30% → +72.5% |
| 2025-10 → 2026-10 | −30% | **+20.1%** | 0.53 | −16.7% | 45% | −17.7% | −30% → +20.1% |
| **compounded** | | **+612.3%** | | worst −38.0% | ~51% | **+47.0%** | |

Three findings, and the third is the one that matters:

1. **The choice is stable.** −50% once and −30% four times in a row; hindsight inside each
   test year would have picked −30% in four of five, and in the fifth (2023-24) it would have
   turned the gate *off* for nine extra points. A dial that picks itself consistently out of
   sample is not a fitted parameter.
2. **All five years are positive** (+48.7%, +23.9%, +86.6%, +72.5%, +20.1%) with Sharpe
   0.46-1.78 and the book in the market about half the time.
3. **The value is in re-choosing, not in the gate.** The same configuration with no peak gate
   compounds to **+47.0%** over the same five years, and with a *fixed* −90% to **+23.2%** —
   worse than not having it. §1.3's "no plateau" was the correct observation, and it means a
   constant will not do; what was missing is that the honest rolling choice works anyway.

**The placebo, re-run under the sizing, says the gate is doing real work.** §1.3's rotation
test left a doubt: rotating the readings *between symbols* still scored Sharpe 0.45 against
0.90 for the real gate, so perhaps half of any gate effect is just "a smaller book churns
less". Repeating that rotation here — same thresholds, same book size, no information — kills
the doubt rather than confirming it:

| test year | names bought (real / placebo) | real gate | placebo, median of 3 rotations |
|---|---|---|---|
| 2021-10 → 2022-10 | 194 / 207 | **+48.7%** | +11.0% |
| 2022-10 → 2023-10 | 268 / 271 | **+23.9%** | −36.3% |
| 2023-10 → 2024-10 | 337 / 348 | **+86.6%** | +50.9% |
| 2024-10 → 2025-10 | 509 / 518 | **+72.5%** | −38.0% |
| 2025-10 → 2026-10 | 739 / 747 | **+20.1%** | −50.8% |
| **compounded** | | **+612.3%** | **−67.4%** |

The book size is within 2-8 names of the real one in every year, so this is not a comparison
between a small book and a large one. The gate beats its own placebo in all five years, by 36
to 111 points, and the placebo is a **losing** strategy. **§1.3's caution was too cautious for
the sized book**: there, the placebo kept half the Sharpe; here it loses money.

One caveat does survive, and it is §4's: this is the survivorship-biased archive. A
distance-from-peak gate on a universe where nothing ever delists is flattering, and an 840-name
book is more exposed to that than a ten-name one — the narrow basket does not need the gate at
all. So the honest ordering is: **the basket is the strategy to run, and the sized wide book
with a rolling drawdown gate is the higher-return, higher-drawdown alternative that a
delisting-inclusive archive would presumably punish.**

### 1.6 Costs

KuCoin spot VIP0 is not 0.1% for everyone: class A is 0.1/0.1% maker/taker, class
B is 0.2/0.2, class C is 0.3/0.3, and the archive splits **496 A / 236 B / 264 C**
— half of all pairs cost more than 0.1% per side before anything else.

Round trip for a $10,000 order, fees plus estimated impact, by the pair's daily
turnover:

| daily turnover | round trip |
|---|---|
| ≥ $1M | **0.25%** |
| $100k – $1M | 0.55% |
| $10k – $100k | 1.69% |
| < $10k | **15.02%** |

And the same tax expressed as annual drag at 0.25% per round trip: 12 trades a
year costs **3.0%**, 50 trades **12.5%**, 250 trades **62.5%** of capital per
year. This is why the archive's headline experiment looks the way it does — SMA
200 on BTC-USDT 1h makes 1,268 trades: gross +2,293.6%, net **+89.1%** against
+1,851.3% for holding; the same rule on daily bars makes 30 trades and returns
+1,114%.

**Consequence:** a rule has to trade rarely and only on liquid pairs. No entry
logic measured here comes close to outweighing this.

### 1.6a What the cost assumption is worth, measured two ways

Every number in this file pays a flat 0.1% per side because that is the only figure the engine
can know without an order book. Two measurements say how much that assumption matters, and
they disagree in an informative way.

**A per-pair spread, estimated from the bars.** `kcs-basket --spread-model corwin-schultz`
reads each pair's own high-low ranges (Corwin-Schultz 2012, on **hourly** bars — a daily range
is mostly volatility, not spread: the same estimator reads BTC at 30 bp on daily bars and
5.6 bp on hourly ones) and charges each leg half of it per side on top of the fee. On the
winning configuration over the last five years:

| pair | estimated spread | total per side | | pair | estimated spread | total per side |
|---|---|---|---|---|---|---|
| DOT-USDT | 28.2 bp | 24.1 bp | | BNB-USDT | 9.9 bp | 14.9 bp |
| ADA-USDT | 22.3 bp | 21.1 bp | | VET-USDT | 9.4 bp | 14.7 bp |
| XRP-USDT | 18.0 bp | 19.0 bp | | BTC-USDT | 5.6 bp | 12.8 bp |
| SOL-USDT | 17.5 bp | 18.8 bp | | MOVR-USDT | 0.0 bp | 10.0 bp (fallback) |
| DOGE-USDT | 14.4 bp | 17.2 bp | | ETH-USDT | 12.3 bp | 16.2 bp |

The result moves from **+91.43% at Sharpe 0.87** to **+86.05% at 0.83** (drawdown −17.5% →
−17.9%, commission 6.2% → 9.9% of capital). The estimator is biased *upward* on volatile pairs,
so this is a conservative bound: the honest reading is "realistic spreads cost a few points,
not the edge".

**A flat fee sweep, five times the modelled commission.** If the cost assumption is simply
wrong by a factor, this is what it costs:

| fee per side | 0.05% | 0.10% | 0.20% | 0.30% | 0.50% |
|---|---|---|---|---|---|
| total | +96.07% | +91.43% | +82.50% | +74.01% | **+58.27%** |
| Sharpe | 0.90 | 0.87 | 0.80 | 0.74 | **0.61** |
| max DD | −17.2% | −17.5% | −18.1% | −19.1% | −22.1% |

**This configuration is not fee-fragile.** At five times the modelled commission it still
returns +58% at Sharpe 0.61, where the wide 7/7 book lost half its result for every extra
0.1% per side (§1.5). The reason is structural and worth stating plainly: a book that holds ten
liquid pairs ~18% of the time pays 10-13% of capital in total costs over five years, while a
book that re-shuffles 146 pairs every week pays 860% over nine. **Costs decide everything — on
the strategy that trades a lot, and almost nothing on the one that does not.**

### 1.6b What it costs to be your size, not zero size

Every other number in this file is charged as if the order were infinitesimal: a flat fee plus
(where measured) the pair's spread. The part of the cost that depends on **how much you are
trading** is market impact, and it is now modelled as the standard square root:

    impact per side = coefficient * per-bar volatility * sqrt(order_usd / turnover_per_bar)

with the coefficient left as a knob (the literature band is roughly 0.1-1) so the answer is a
band rather than a point. `kcs-basket --capital 60000` charges it per leg, and warns when a
single order is 10% or more of one bar's turnover — which is days of that pair's volume, not
one trade. The winning configuration, §1.4-1.5a, over the last five years:

| cost assumption | total | Sharpe | max DD | commission, % of capital |
|---|---|---|---|---|
| fee + estimated spreads, no impact | +86.05% | 0.83 | −17.89% | 9.9% |
| **$60k of capital** (coefficient 0.1) | **+83.90%** | **0.81** | −18.02% | 11.3% |
| $1M | +77.64% | 0.77 | −18.63% | 15.2% |
| $10M | +62.28% | 0.64 | −21.25% | 24.7% |
| $1M with coefficient 0.5 (harsh) | +51.61% | 0.55 | −23.31% | 31.1% |

Three things worth knowing:

1. **At sixty thousand dollars the impact is noise** — 2.2 points of return and 0.02 of Sharpe,
   because the orders are a few basis points of the volume even in the thin legs. The
   configuration is size-agnostic where a retail account actually lives.
2. **Capacity is set by the thinnest leg, not by the largest.** At $60k the per-leg charge is
   0.5 bp on BTC and 29 bp on MOVR; the engine holds each leg at `1/N` of the capital, so the
   smallest name in the selection decides how big the book can get. A ten-name basket is as
   large as its least liquid name.
3. **The damage is graceful.** Ten times the capital (+$10M) costs 24 points of return and 0.19
   of Sharpe, and even the deliberately harsh calibration ($1M at coefficient 0.5) leaves
   +51.6% at Sharpe 0.55. There is no cliff between $60k and $1M: the difference is 8 points.

One caveat keeps this from being the last word: the turnover is measured **before** the
reported window (the same causal rule as the spread model), so liquidity *decaying inside* the
window is not captured — and it does decay. VET-USDT was selected as one of the ten busiest
pairs in 2021 trading $15M a day and trades $0.7M a day now. Re-measuring liquidity per window
— and dropping or shrinking a leg when it thins — is the remaining piece of this thread, and
`--from/--to` is what it would be measured with.

### 1.6c A leg that goes quiet — and what the survivorship bias is worth

§1.6b left one hole: liquidity is measured once, before the window, so a leg that *thins while
it is held* keeps its 1/N weight. `--min-turnover-now` closes it — a leg is held only while the
rolling median turnover of its last `--turnover-window` bars (30 by default) is at least the
threshold, decided bar by bar from bars up to that one, so a pair that goes quiet is dropped
after the window and picked up again if it comes back:

| rule | total | Sharpe |
|---|---|---|
| keep everything | **+86.05%** | **0.83** |
| drop a leg below $100k a day | +78.20% | 0.80 |
| drop a leg below $300k a day | +68.95% | 0.73 |
| drop a leg below $1M a day | +63.81% | 0.71 |
| $60k of capital, keep everything | +83.90% | 0.81 |
| $60k of capital, drop below $300k | +67.83% | 0.72 |
| $60k of capital, drop below $1M | +62.95% | 0.70 |

**Every liquidity rule costs return, monotonically, and by far more than the impact it avoids**
— 17 points to save the 2.2 points that §1.6b measured at $60k. That inversion is the
signature of this archive's survivorship bias, not an argument against the rule: with no
delistings in the data, the thin names that are present are precisely the ones that survived
and multiplied, while the thin names that went quiet and then *died* are absent by
construction. So the measured cost of the rule is a **lower bound on what the bias is worth**
— at least 17 points over five years on this configuration, against the 2.2 points that size
costs at retail.

The practical reading for a real account is uncomfortable and worth stating plainly. **At $60k
the arithmetic says keep the thin legs and pay their impact**, and that is what the tool now
lets you measure — but the same archive cannot tell you whether the pairs that stayed thin
would have kept trading. The risk is asymmetric: a leg that dies costs all of it, and there is
no such leg anywhere in this file. Treat the +86% as the answer to "what happened to the pairs
that survived", and treat the 17-point gap as the price of the question.

### 1.7 Trend following on liquid survivors is the only thing that survived

The rule: long while the price is above where it was 30 days ago, decided once a
week, flat otherwise, spot, no leverage, no stops. Across the whole archive, with
the rule expressed in calendar time on every timeframe:

| timeframe | bars | series | median window | median return | profitable | median Sharpe | beats holding | median DD |
|---|---|---|---|---|---|---|---|---|
| 1h | 720/168 | 976 | 2.4 y | −17.5% | 38% | −0.14 | **88%** | −75% |
| 4h | 180/42 | 974 | 2.4 y | −18.9% | 38% | −0.15 | **89%** | −75% |
| 1d | 30/7 | 966 | 2.5 y | −24.5% | 35% | −0.22 | 88% | −75% |
| 1w | 4/1 | 916 | 2.6 y | −29.7% | 33% | −0.25 | **89%** | −73% |
| 1mon | 1/1 | 602 | 4.4 y | −53.5% | 24% | −0.42 | 85% | −76% |

The median asset loses money (−26.3% whole-archive) — but holding that same
median asset loses three times more (median buy & hold **−88.4%**, drawdown
−97%). TSMOM beats holding on return for **88%** of series and on Sharpe for 79%,
halving both drawdown and volatility (73% against 129%).

**It is a risk-reduction rule, not a money printer.** 35% of series are
profitable, the median Sharpe is −0.22, and only 9% clear Sharpe 0.5.

### 1.8 The positive mean is a handful of assets

| trimming | mean return | median |
|---|---|---|
| everything | +136.4% | −26.3% |
| without top 0.1% (4 series) | +117.0% | −26.4% |
| without top 1% (44) | +65.4% | −26.9% |
| without top 5% (221) | +7.7% | −31.0% |
| without top 10% (443) | **−15.1%** | −35.2% |

The top 1% of series account for **52%** of the summed profit. This is the same
shape seen inside a single series (the best 5% of trades produced 87% of the
profit in the candle-and-volume study). Never quote the mean without the trade
count and the window next to it.

History length separates the two populations cleanly (1h): 2–3 years → median
−46.4%, 3–5 years → −7.4%, 5–7 years → **+31.9%**, 7+ years → **+43.8%**. Short
histories are recent listings that mostly die; long ones have already survived.
Taking only series with 5+ years: 1,186 series, 50% profitable, **92% beat
holding**, median Sharpe 0.00, median 29 trades.

### 1.9 Diversifying the same rule across liquid survivors is where it works

Fixed 20% weights, 30-day lookback, weekly decision, 1h:

| basket | window | TSMOM | holding the same names | Sharpe | max DD |
|---|---|---|---|---|---|
| BTC, ETH, SOL, XRP, BNB | 2021-08 … 2026-09 (5.14 y) | **+207.3%** | +124.7% | 0.53 vs 0.23 | −47.0% vs −83.9% |
| BTC, ETH | 2017-10 … 2026-09 (8.92 y) | **42.40x** | 15.40x | 0.74 vs 0.38 | −67.3% vs −87.1% |

This is the only configuration measured here that beats the passive alternative
on return **and** risk at the same time. Commission was 10.2% of capital over the
5.14 years.

One variation is worth naming here because it was measured and registered:
`BlendTsmom` (horizons of 1/2/4/8 weeks, decided weekly) removes the lookback bet
— it beat the average single lookback on 68% of 963 hourly series and had the
family's best median drawdown, and the spread between its own two
parameterizations is 0.14 against 0.60 between four single lookbacks. On BTC
judged out-of-sample it returned +156.7% against +978.5% for the single-lookback
rule, so it buys robustness with return: use it where you cannot tune, not as an
upgrade over a walk-forwarded single horizon.

Two caveats that matter more than the numbers:

* the five names were **chosen by hand**, knowing which assets survived. That is
  hindsight in the asset list, not in the rule, and it is the largest remaining
  weakness in the best-looking result here;
* the window starts where every leg has data and each leg is rebased to 1.0
  there, so these figures are window-relative, not since-listing.

---

### 1.10 The same signal, built two ways

`--select sign` and `--strategy tsmom` read the **same** signal: the close above its level
`lookback` bars ago, on the same absolute grid, held between decisions. It is literally the
same parameter — `Tsmom.threshold` is the portfolio's `--threshold`. Run over the same nine
years with the same 7-bar lookback and 7-bar grid, they agree on nothing else:

| | TSMOM, one asset (BTC) | `--select sign`, 836 USDT pairs |
|---|---|---|
| assets | 1 | 0 … 604, median 51, mean 109 |
| weight per name | 0 or 100% | 0.2–2% |
| **out of the market** | **46% of bars** (mean exposure 0.54) | **4% of rebalances** |
| decisions in nine years | 114 trades | 466 rebalances × 1.30 books |
| commission | ~20% of capital | **860.6% of capital** |
| nine years | **10.87x**, Sharpe 0.55, −75.0% | **12.4x**, Sharpe 0.30, **−92.6%** |
| last five years | **+86.7%**, 0.36, −38.1% | **−65.4%**, −0.30, −88.6% |
| its benchmark | holding BTC 16.24x, 0.46, −82.9% | the same universe −37.0% |

(Measured after the timing fix in §3 — the loop used to run the whole strategy one
rebalance late, which flattered this book: the same comparison read 30.5x at Sharpe 0.42
before. `--quote any` gives 12.4x→**33.4x** at Sharpe 0.57 and −81.4%, so the crosses are
*not* the drag they looked like under the lag. The fee totals are in starting-capital
units and grow with the curve; the turnover is what compares.)

The mechanism changed, not the signal. TSMOM's entire measured value is *being absent* — it
is flat 46% of the time on BTC, which is why it beats holding on 88% of 966 series and halves
the drawdown. A book of ~146 names neutralises that half of the rule: something is always
rising, so the filter never takes the account out of the market (0% of 466 rebalances), and
each name is too small (0.2%) for its own signal to matter. What is left is the average of
the universe — an alt index with a weekly re-shuffle.

That re-shuffle is the expensive part, and its fragility is measurable. The same 7/7
configuration at different commissions:

| fee per side | total | CAGR | Sharpe |
|---|---|---|---|
| 0.00% | +3,197% | 47.7% | 0.56 |
| 0.05% | +2,303% | 42.6% | 0.50 |
| **0.10%** | **+1,651%** | 37.7% | 0.45 |
| 0.20% | +829% | 28.3% | 0.35 |
| 0.30% | +392% | 19.5% | 0.25 |

Every extra 0.1% per side roughly halves the result. A control run says the same thing from
the other side: holding **every** pair with the same weekly re-equalisation and no sign
filter returns **+350%** at 30% of capital in commission and 0.11 turnover per rebalance,
against the filter's 1.36 — so the signal is worth about 4.7x and multiplies the trading
twelvefold.

**The rule that follows:** apply this signal **per asset** — one position, or a handful of
liquid names (`kcs-basket`: five majors, weekly grid, +202.9% against +51.1% for holding them)
— and never to the whole universe at equal weight. Across ~1000 pairs the same signal stops
being a trend rule and becomes an index with a bill attached. It is the cleanest illustration
of §1.1: the signal is the cheapest part of the system; the universe, the ability to leave
the market, and the number of trades decide the outcome.

### 1.11 Take-profit and stop-loss: when they help, and why not here

**Read §1.11b first if you only read one of them.** This part fills the exits at the **next
open** after a close beyond the level — the softest possible trigger, and the one that made a
stop look useful on a breakout. §1.11b fills them *inside the bar* at the level, stop first,
and **reverses that finding**. The law at the end of §1.11b is the one to keep.

The natural next idea after a trend rule is to add the exits every trading course teaches. They
are now measurable: `stops-sma`, `stops-breakout` and `voltarget-stops-sma` wrap any signal with
a take-profit, a stop-loss and a trailing exit, all read **on closes** with a cooldown that
stops the inner signal from buying straight back what it was just stopped out of. Reading them
on closes is a deliberate limitation: with OHLCV bars nobody knows whether the high or the low
came first, so "my stop would have filled at −10%" is an assumption, not a measurement — and
this implementation is pessimistic instead, because the engine fills the exit at the **next
open**, which on a gap is worse than the level.

**Inside the configuration this file recommends** (top ten by turnover, `voltarget-sma` 50 bars
/ 30% target, estimated spreads):

| exits added | total | Sharpe | max DD | commission |
|---|---|---|---|---|
| none (the recommended one) | **+86.05%** | **0.83** | −17.89% | 9.94% |
| stop-loss 15%, cooldown 5 | +85.75% | 0.83 | −17.89% | 9.97% |
| stop-loss 30% | +86.05% | 0.83 | −17.89% | 9.94% |
| trailing 20%, cooldown 5 | +82.13% | 0.82 | −17.93% | 10.20% |
| take-profit 50% + stop-loss 15% | **+65.50%** | 0.72 | −17.20% | 10.44% |

**Nothing here helps.** A 15% stop moves the result by three tenths of a point, a 30% stop
changes literally nothing, a 20% trail costs four points, and a 50% take-profit costs **twenty
points of return and 0.11 of Sharpe** in exchange for 0.7 points of drawdown. The take-profit is
the clearest failure and the reason is §1.8 seen from the other side: if a handful of large runs
pays for the whole strategy, then exiting at +50% removes precisely the trades being measured.

**Why the stops do nothing at all** is a structural fact, not a threshold that happened to be
wrong. Measured directly on BTC daily over five years, a stop-loss of 3%, 5%, 10% or 20% leaves
the targets **identical on every one of the 1,826 bars**: the 50- or 200-bar mean exits before a
close ever gets that far below the entry, so the stop is dead code. The same measurement with
the plain SMA gives the same answer:

| exits | total | Sharpe | max DD | trades |
|---|---|---|---|---|
| none | +221.11% | 0.66 | −36.19% | 19 |
| stop-loss 3 / 5 / 10 / 20% | +221.11% | 0.66 | −36.19% | 19 |
| trailing 25% | +234.31% | 0.69 | −33.57% | 20 |
| trailing 15% | +115.77% | 0.46 | −39.63% | 28 |
| take-profit 30% + stop-loss 10% | +114.82% | 0.46 | −43.47% | 32 |

**Where a stop does have a job: when the strategy's own exit is far away.** A Donchian breakout
leaves on a shorter low channel, which on daily bars can sit well below the entry — and there the
stop earns its keep, improving return, Sharpe and drawdown together:

| exits | total | Sharpe | max DD | trades |
|---|---|---|---|---|
| none | +41.95% | 0.22 | −54.03% | 28 |
| stop-loss 10%, cooldown 5 | +54.12% | 0.28 | −48.29% | 30 |
| stop-loss 20% / trailing 20% | +41.95% | 0.22 | −54.03% | 28 |
| take-profit 30% + stop-loss 10% | +58.24% | 0.30 | −45.95% | 35 |

**And on hourly bars they disappear again**: the same breakout on `1h` returns −62.39% with 626
trades, and a 10% stop or a 20% trail changes **not one** of them, because at hourly scale a
ten-bar channel is always closer than ten percent.

**The law, which is what to take away from this section: an exit only matters if it is tighter
than the exit the strategy already has — and even then it helps only when the existing exit is
not the thing the edge depends on.** A trend follower's mean line is tighter than any sensible
stop *and* it is the protection the edge is built on, so the stop is dead code and the
take-profit cuts the winners. A breakout has no such protection, so the same stop pays.

### 1.11b The same exits, filled inside the bar: the answer reverses

§1.11 read its levels on closes, which is the softest trigger available. The conventional
intrabar rule is stricter and needs a choice, because OHLC bars do not say whether the high or
the low came first. This repository now states the choice and sticks to it: **if a bar touched
both the stop and the take-profit, the stop is assumed to have filled**, the fill is the level
itself, a bar that *opened* beyond the stop fills at the open (worse), and a gap through the
take-profit still fills at the level (a windfall is not booked). The engine gained
`exit_prices[t]` for this — a price at which a position still open during bar `t` was closed
inside it — and the strategy reports it through `intrabar_exits`; `prod(1 + net_return)` still
equals the final equity, which the tests pin.

**BTC, daily, SMA 200, five years:**

| exits | total | Sharpe | max DD | trades |
|---|---|---|---|---|
| none | **+221.11%** | **0.66** | **−36.19%** | 19 |
| stop-loss 10%, cooldown 5 | +194.62% | 0.60 | −41.45% | 20 |
| stop-loss 20% / 30% | +221.11% | 0.66 | −36.19% | 19 (never touched) |
| take-profit 30% + stop-loss 10% | +71.90% | 0.33 | −42.31% | 33 |
| trailing 15% | +55.99% | 0.26 | −52.15% | 32 |

**The daily Donchian breakout — the case §1.11 held up as the stop's justification:**

| exits | §1.11, close-based | §1.11b, filled in the bar |
|---|---|---|
| none | +41.95%, Sharpe 0.22, DD −54.03% | +41.95%, Sharpe 0.22, DD −54.03% |
| stop-loss 10% | **+54.12%, 0.28, −48.29%** | **+33.04%, 0.18, −56.49%** |
| take-profit 30% + stop-loss 10% | +58.24%, 0.30, −45.95% | +18.33%, 0.11, −54.52% |

**The finding reverses.** The soft trigger was doing the work, not the stop: a close beyond the
level is a rare event that carries information about the trend, while a wick through it is
noise that the strategy's own signal knows nothing about. Exit on the wick and you realize the
dip, pay the round trip, and re-enter higher — which is what the equity curve shows.

**The configuration this file recommends, with intrabar exits:**

| exits added | total | Sharpe | max DD | commission |
|---|---|---|---|---|
| none | **+86.08%** | 0.83 | −17.88% | 9.92% |
| stop-loss 15%, cooldown 5 | +85.72% | 0.83 | −17.56% | 9.93% |
| stop-loss 30% | +86.37% | 0.83 | −17.88% | 9.92% |
| trailing 20% | +83.41% | **0.85** | −17.76% | 10.35% |
| take-profit 50% + stop-loss 15% | +65.98% | 0.75 | **−16.35%** | 10.81% |

Here the exits are nearly free but still not positive: a 15% stop moves three tenths of a point,
a 30% stop does nothing, a 20% trail buys 0.02 of Sharpe for 2.7 points of return, and the
take-profit costs **20 points of return** for 1.5 points of drawdown. The one honest use of
these dials is as a *risk preference*: if you want a smaller drawdown and will pay for it in
return, `--param take_profit=0.50 --param stop_loss=0.15` is the measured price.

**The law, corrected. An exit that is never touched is dead code; an exit that is touched costs
money, because a wick carries no information about the signal that put the position on.** The
stop's justification in §1.11 — "it helps where the strategy's own exit is far away" — held
only for the soft trigger. Add exits to control *your* risk tolerance, not to improve the
strategy, and expect to pay for them in return.

### 1.12 TSMOM's lookback on daily bars: the defaults mislead, and shorter is better

`tsmom` counts `lookback` and `rebalance` in **bars**, so its defaults (720 and 168) are an
hourly calibration: on daily data that is a two-year lookback reviewed every five and a half
months. The 2025-26 runs showed the consequence — the strategy was a spectator (0.00% on SEI
and WLD because it never decided, −67% on ADA against −71% for holding, because its one
decision kept it in). Shortening the lookback is the obvious fix, and it works, but the
in-sample "best" lookback differs per asset, which is the trap:

| best in-sample lookback, 2025-10 → 2026-10 | BTC | SEI | WLD | ADA |
|---|---|---|---|---|
| the winner | 180/7: +3.1% | 360/30: +0.9% | **30/7: +40.6%** | **30/7: +12.3%** |
| the hourly default (720/168) | −25.6% | −72.9% | 0.0% | −67.3% |

Four assets, four different answers, and the default is the worst of them everywhere. So the
question has to be asked out of sample, with parameters re-chosen on the past only
(`kcs-walkforward --train 300 --test 60`):

| walk-forward over the whole history | ADA (39 splits, 6.41 y) | BTC (49 splits, 8.06 y) |
|---|---|---|
| holding the asset | +387.98%, Sharpe 0.26, DD −95.4% | +999.03%, Sharpe 0.48, DD −76.9% |
| `tsmom`, lookback 30-360 × rebalance 1/7 | **+2,484.55%, Sharpe 0.73**, DD −76.8% | +479.22%, Sharpe 0.46, DD −65.1% |
| `tsmom-blend`, base 7-60 × rebalance 1/7 | +1,711.95%, Sharpe 0.65, DD −82.7% | **+3,096.60%, Sharpe 1.13**, DD −38.8% |
| `sma`, window 50-200 (reference) | +579.55%, Sharpe 0.43, DD −85.1% | +2,758.78%, Sharpe 0.97, DD −56.5% |
| `voltarget-sma` (reference) | +101.41%, Sharpe 0.63, **DD −36.6%** | +493.23%, **Sharpe 1.19**, **DD −23.1%** |

**A shorter lookback is a real improvement, not a fit.** On ADA the short-lookback TSMOM
compounds to +2,485% against +388% for holding, with a Sharpe of 0.73 against 0.26; on BTC the
*honestly chosen* `tsmom-blend` with a short base is the best return of anything measured in
this repository (+3,096% at Sharpe 1.13) and its drawdown is half of holding's.

**And the correction: it does not transfer to every asset.** On SUI-USDT (15 splits, 2.47
years, holding it returned **−60.34%** at Sharpe −0.37 and a −87.5% drawdown) the short-lookback
TSMOM *lost* — the plain one −21.66% at Sharpe −0.16, the blend +77.30% — while the boring
references did the work: `sma` **+86.33%** (Sharpe 0.40) and `voltarget-sma` **+20.78% with the
best Sharpe of all, 0.54, and a −17.0% drawdown**. The same pattern shows in SUI's year:
`tsmom 30/7` −29.21% and `tsmom-blend base14/7` −47.21% against `sma` +9.45% and
`voltarget-sma` +8.13% (Sharpe 0.41, DD −13.3%) while holding lost 66.65%. So the transferable
finding is the narrow one — **never run TSMOM with its hourly defaults on daily bars, because
that is dead code everywhere** — and not "a short lookback wins". On four assets the short
lookback was the best of the TSMOM family on two (ADA, BTC), the blend was the best on one
(BTC), and neither was the best strategy on the fourth (SUI).

Three caveats, in order of importance. First, the **candidate set** (lookback 30-360, base
7-60) was my choice, and a different set would move the result — the honest claim is "much
shorter than 720 works out of sample", not "30 is the answer". Second, these are wild rides:
drawdowns of −39% (BTC blend) to −83% (ADA blend) while the curves compound, so the useful
comparison is against each strategy's own risk, and `voltarget-sma` still owns the best
risk-adjusted result on BTC (Sharpe 1.19, DD −23%). Third, all of it is one archive with no
delistings (§4). The practical resolution is the one §1.1 already gave: **the blend exists so
that a lookback does not have to be chosen at all**, and on this evidence using it with a
*daily* base rather than the hourly default is the better default.

### 1.13 The same rule on nine assets, out of sample

Every coin the session looked at, run through the same harness (`kcs-walkforward --train 300
--test 60`, parameters re-chosen on the past only, spot-only, 0.1%/side):

| asset | splits / years | holding it | `sma` 50-200 | **`voltarget-sma`** | `tsmom` short lookback | `tsmom-blend` short base | `rsi-rev` |
|---|---|---|---|---|---|---|---|
| BTC | 49 / 8.1 | +999% (0.48) | +2,759% (0.97) | +493% (**1.19**, DD −23%) | +479% (0.46) | **+3,097% (1.13)** | — |
| ADA | 39 / 6.4 | +388% (0.26) | +580% (0.43) | +101% (0.63, DD −37%) | **+2,485% (0.73)** | +1,712% (0.65) | +156% (0.29) |
| ICP | 27 / 4.4 | −88% | −59% | **+12%** (0.15, DD −34%) | −35% | +8% | −58% |
| APT | 19 / 3.1 | −88% | −22% | **+8%** (0.15, DD −31%) | −76% | +11% | −39% |
| FET | 17 / 2.8 | −66% | +79% (0.27) | **+36%** (0.63, DD −21%) | +130% (0.46) | −18% | −5% |
| FLR | 17 / 2.8 | −33% | +194% (0.60) | **+50%** (0.77, DD −13%) | +95% | +69% | +165% (0.83) |
| SUI | 15 / 2.5 | −60% | +86% (0.40) | **+21%** (0.54, DD −17%) | −22% | +77% (0.38) | −16% |
| WLD | 14 / 2.3 | −92% | +19% (0.09) | **+21%** (0.48, DD −16%) | — | — | +27% (0.18) |
| SEI | 14 / 2.3 | −85% | −24% | **+4%** (0.10, DD −25%) | −38% | −1% | −35% |
**Re-checked 2026-10-02** with `analysis/experiments/verify_conclusions.py`: the BTC
`tsmom-blend` cell no longer reproduces. The same harness now returns **+1,234% at Sharpe
0.93** with `base=7`, and widening the grid lowers it (`base=7,30` → +511%, `base=7,30,60,120`
→ +143%); the sign of the result is unchanged, the multiple is not. The number above predates
the timing fixes that forced every portfolio figure to be re-measured, and the rest of this
table has not been re-run since. Treat the multiples here as unreproduced until
`verify_conclusions.py` covers them.

**`voltarget-sma` is positive on all nine assets** — the only rule in the table that is:
with drawdowns of −13…−37% while the assets themselves fell 33-92% over the same stretches
(its weakest result, +4% on SEI, is still 89 points better than holding that asset).
Nothing else comes close to that consistency: `sma` ranged from +194% to −59%, the short-lookback
`tsmom` from +2,485% to −76%, `rsi-rev` from +165% to −58%. The best *individual* numbers belong
to those erratic rules (BTC's blend at +3,097%, ADA's TSMOM at +2,485%, FLR's `rsi-rev` at
+165%), and that is precisely the trap: a rule that is sometimes the best and sometimes the
worst is a rule you cannot size.

A practical reading: the recommended single-asset configuration is `voltarget-sma` with a
50-200 bar trend window and a 25-40% volatility target — the boring one — and the more
aggressive `tsmom`/`blend` variants are worth running only as a *second* book, sized for the
−55…−85% drawdowns they show whenever they are wrong. All nine assets are also the same kind of
asset (crypto that fell hard), on one survivorship-biased archive (§4), and the walk-forward
still chooses parameters on the past, which is honest but is not a guarantee about the future.

### 1.14 Every mechanism that can only take exposure away

"Limit the damage" is a different job from "find a signal", and the toolkit now has five ways to
do it. The distinction that matters turned out to be **what each one reads**: the exit that reads
*price* can add value, and the ones that read only *risk* trade return for risk.

| lever | what it reads | measured verdict |
|---|---|---|
| **trend exit** (`sma`, `tsmom`) | price | the only thing that ever improved risk-adjusted return — it is the core, not an overlay (§1.7-1.9, §1.13) |
| **volatility target** (`voltarget-*`) | the asset's own volatility | positive on all nine assets walked forward, best Sharpe on most, **threshold-insensitive** (target 25% vs 40%, window 20 vs 168 — a few points either way, §1.6b-test) |
| **volatility ceiling** (`volfilter-*`, new) | the same volatility, binary | **threshold-sensitive**: at 50% three of five coins never trade at all (0.00%); at 80% the best Sharpes in the table (WLD 1.02, SUI 0.30) or −19% on ADA; the winning threshold differs per asset (SUI 50%, WLD/FET 80%, SEI 120%) — a parameter to fit, not a rule |
| **both at once** (`voltarget-volfilter-sma`) | volatility | the best Sharpe on four of five coins (WLD 1.07, SUI 0.60, FET 0.57, ADA −0.55) with the smallest drawdowns (−1.7…−21.4%) — and the lowest return on four of five: it is the most conservative setting, not a better one |
| **drawdown overlay** (`--dd-scale`) | the account's own equity | return and drawdown fall together and Sharpe is unchanged; over five years it costs more Sharpe than it saves (0.55 → 0.48) — a preference, not an edge |
| **stops** (`stops-*`) | a price level | dead code when never touched, costly when touched; the intrabar convention reversed the breakout "win" (§1.11b) |
| **portfolio gates** (`--trend-gate`, `--max-below-peak`, `--min-volatility`) | the cross-section | the trend gate lifts the wide book from 0.30 to ~0.50 Sharpe; the peak gate needs a rolling re-choice; a *turnover* floor hurts at every level (§1.3, §1.5c) |
| **a lower cap or a bigger cash weight** | nothing | not a separate lever at all: it is `target_vol` turned down |

The year 2025-10 → 2026-10 on five alts, at the same window and `vol_window=30`, shows the
shape of it (`voltarget` at a 30% target against the binary ceiling at 80%):

| coin | `voltarget-sma` | `volfilter-sma` 80% | both |
|---|---|---|---|
| ADA | −9.88% (−0.64) | −19.18% (−0.65) | −7.95% (**−0.55**, DD −21.4%) |
| SEI | +11.31% (0.60) | +9.85% (0.26) | +6.53% (0.40, DD −12.4%) |
| FET | +10.56% (0.51) | +11.78% (0.60) | +4.51% (0.57, DD **−3.5%**) |
| SUI | +12.54% (0.50) | +13.06% (0.30) | +13.40% (**0.60**, DD −14.1%) |
| WLD | +17.08% (0.76) | +17.61% (1.02) | +7.69% (**1.07**, DD **−1.7%**) |

**The two practical conclusions.** First, the *robust* damage limiter is the volatility target: it
is positive on nine of nine assets, its parameters barely matter, and it never sits out
completely. Second, the *binary* ceiling is fragile in the same way a lookback is: on one asset
and one year it can look like a big improvement (SUI Sharpe 0.50 → 1.09 at a 50% ceiling), and on
the next asset the same ceiling means "never trade" — so it has to be treated as a parameter to
choose per asset, with everything §1.12 says about that. If you want the most conservative
setting that still trades, stack both (`voltarget-volfilter-sma`) and accept the lowest return for
the smallest drawdown.

## 2. Rejected by measurement

Each of these was tested on this archive, with costs, and lost. Do not re-open
them without new data.

| idea | what happened |
|---|---|
| **Shorting** | Median 0.333x against 0.899x for long-only, better than long-only in 23.4% of 244 series, and **16 accounts wiped out completely** (needing a single bar that opens >2x higher). Only 45.8% of the archive even has a perpetual; on that shortable subset it still loses, 0.440x against 0.956x. Funding adds ±2%/yr, and reacting to funding is worse than ignoring it. |
| **Leverage** | 3x liquidated 83% of positions (median 486 days), 5x → 88%, 10x → 94%. |
| **Cross-sectional momentum over all pairs** | −81.2% against −47.4% for equal weight; the long/short variant was wiped out in Jan 2018. |
| **Counter-trend / mean reversion** | SMA inversion on 1h −95% (negative gross too, so it is not a fee story); RSI(2) reversion on daily bars −59.6%. |
| **Buying low volume** | Worst cell of the volume study: −2.36% per trade. |
| **Grids (1% step)** | EV −1.39% per entry: median adverse excursion −5.38% in 24h and −16% in 7 days, and 3% of entries never recover within 90 days (median −71.6%). |
| **TP1% / SL3%** | 67.1% wins against the 75–80% needed to break even at these costs. |
| **Funding as a strategy** | Long-run ±2%/yr (BTC +1.7%, ETH +2.0%, SOL −0.7%); the worst month was −1.06%, the longest adverse streak 7.7 days. |
| **DCA as a timing rule** | Over the last five years weekly DCA into BTC earned a money-weighted **28.0%** a year against **14.9%** for a lump sum of the same 26,100 — but that is what the *schedule* did in a window that opened with a bear market, not an edge. Filtering the contributions by a 200-day trend cut the IRR to **12.3%** (46% of the money never got invested), and adding a trend exit finished **flat over five years** (0.99x paid in, −0.3% a year) while the trend rules on the same window made 2.5–3.2x. |
| **Value averaging** | A linear target demanded **83,858 paid in to end at 26,100**; a +1%/week target demanded 657k to end at 347k. Its 40.7% IRR is an artefact of withdrawing into strength, and the plan needs unbounded capital exactly when the market falls. |
| **Grid trading, at portfolio level** | Best of five configurations **1.51x against 2.00x** for holding the same window, with a −61% drawdown at half the average exposure — worse risk per unit of return than simply holding half in BTC and half in cash. Re-centring the grid monthly turned it into **0.64x** and weekly into **0.52x**, because re-centring *realises* losses; fees (5% of the budget at 2% spacing) were the smaller problem. It is structurally "buy more as it falls", so exposure peaks at the bottom. |
| **Martingale / averaging down** | On a 10,000 budget every sizing tested **ran out of cash in the first big decline** — base 500 on 2021-12-09 at 47,549, base 1,000 on 2021-11-26 at 53,723 — and then held a bag for years: −67.5% and −70.7% drawdowns for 1.24x and 1.28x over the five years. The base-100 variant shows a −15% drawdown only because 99% of the capital never left the account. |
| **RSI** | Buying oversold (RSI(14) < 30) 0.52x with a −67.8% drawdown, and the registry's 2-period reversion −29.2% over the same window; RSI(14) > 50 as a momentum filter 1.35x at −50.3%; only "buy strength" (RSI > 70) was respectable at **1.93x with −20.7%**, still behind a plain 200-day SMA (2.97x) on the same data. |

### Retirement-plan rules, measured: "buy what rose last month"

The idea of deciding on the first of the month — buy the pairs whose month closed up, sell
the ones that closed down — is monthly *time-series* momentum applied to a whole universe:
a sign filter, not a ranking. Both forms were measured on the same engine (decision on the
month's last close, applied on the next close, 0.1% a side, equal weight, cash when nothing
qualifies), over 2019-03 … 2026-09, with the last five years reported separately:

| rule | 7.6 years | CAGR | Sharpe | max DD | names held | book turned over per month | fees | last 5 years |
|---|---|---|---|---|---|---|---|---|
| all pairs: buy what rose | 6.53x | 28.1% | 0.43 | −77.1% | 142 | 1.39 | **82% of capital** | **0.61x** |
| all pairs: buy the top 20% | 2.76x | 14.3% | 0.20 | −79.1% | 77 | 1.52 | 40% | 0.50x |
| all pairs: buy what fell (control) | 1.71x | 7.4% | 0.13 | −83.5% | 244 | 1.15 | 31% | 0.37x |
| top 20 liquid: buy what rose | 2.96x | 15.4% | 0.24 | −89.6% | 7.4 | 1.05 | 31% | 0.36x |
| top 20 liquid: buy the top 20% | 6.04x | 26.8% | 0.34 | −91.4% | 3.1 | 1.25 | 60% | 0.54x |
| **one asset (BTC): buy if the month closed up** | **17.65x** | 46.1% | **0.86** | **−55.7%** | 0.6 | 0.48 | 32% | 2.06x |
| holding BTC | 20.17x | 48.7% | 0.64 | −76.6% | 1 | 0 | 0% | 1.74x |
| TSMOM on BTC, **weekly** grid, same period | **32.87x** | — | **1.06** | **−44.2%** | 1 | — | — | — |

What the table says, in order of size:

* **The direction of the idea is right and the sign carries information**: buying what rose
  beat buying what fell in both universes (2.96x against 0.43x on the liquid one), and it
  beat holding the same broad universe (6.53x against 3.14x).
* **Breadth is what breaks it.** 142 names, re-equalised monthly, turn over 1.39 books a
  month and pay **82% of the starting capital** in commission over 7.6 years (~10.9% a
  year) — the single largest number in the row.
* **The edge is not stable**: over the last five years the broad version returns 0.61x
  (−39%) while holding BTC returns 1.74x (+74%). The 6.53x is 2019–2021.
* **The best version of the same idea is on one asset.** The monthly sign rule on BTC made
  17.65x at Sharpe 0.86 and a −55.7% drawdown, with 44% of months spent in cash — a better
  risk shape than holding BTC (0.64, −76.6%) at a similar return.
* **And the monthly grid is the expensive part of it.** On BTC, over exactly the same
  period, the same 30-day signal decided **weekly** returns 32.87x at Sharpe 1.06 and
  −44.2%, against 13.71x at 0.74 and −65.1% decided monthly. That is §1.1 restated with
  money: the grid is worth more than the signal, and a month is a slow grid.

### The indicator zoo, screened

Thirty classic indicator rules were put through one harness — signal on a close, held
over the next close-to-close move, 0.1% a side, long or cash — and then twenty of them
were screened over the **whole daily archive** (530 series with 500+ bars), which is the
only place a ranking means anything. Two answers came out, and the second matters more.

**On BTC, which is a survivor, all of them work and the ranking is soft.** Over the 8.8
years: Ichimoku (price above the cloud) 24.67x at Sharpe 0.97, ADX(14) > 25 with +DI > −DI
21.66x at **1.14**, RSI(14) > 50 20.39x at 0.87, Donchian 20/10 13.79x at 0.78, plain
SMA 200 12.15x at 0.70, Keltner 8.26x at 0.82 — against 13.00x at 0.50 for holding. One
caveat on the best-looking row: **ADX + DMI could not be validated archive-wide** — on the
median series the implementation latches (100% exposure, 0.20x, −95.2% drawdown, the
profile of buy & hold), because `+DI > −DI` with a strong ADX stays true forever on a
dying series. Treat it as a candidate to re-implement, not as the winner its BTC number
suggests. The Spearman correlation between the Sharpe ranking on the last five years and on the whole
history is only **+0.70**, with three of five names shared at the top, so "the best
indicator" is half noise. What actually separates the rows is **how much of the time they
are out**: ADX 29%, Ichimoku 45%, RSI 50%, SMA 200 54%, and each row beats a constant
exposure at its own average by 1–20x, which is the trend edge of §1.4 in thirty costumes.

**Across the archive, none of them has a positive median.** Median Sharpe is **−0.10 at
best** (Bollinger breakout, 22% exposure) and −0.53 at worst, and the median asset loses
money under every rule — 0.91x at best against 0.20x for holding it. Ranked by median
Sharpe:

| rule | median x | profitable | median Sharpe | beats holding | median DD | exposed |
|---|---|---|---|---|---|---|
| Bollinger breakout (> upper, out < middle) | 0.85x | 43% | −0.10 | 86% | −65.1% | 22% |
| Keltner (> EMA20+2ATR, out < EMA20) | 0.91x | 44% | −0.10 | 86% | −57.3% | 13% |
| TRIX(15) rising | 0.81x | 42% | −0.12 | 88% | −73.0% | 34% |
| Ichimoku (above the cloud) | 0.85x | 40% | −0.14 | 87% | −69.5% | 22% |
| Bollinger reversion | 0.71x | 33% | −0.20 | 79% | −67.8% | 32% |
| EMA 200 | 0.75x | 26% | −0.23 | 82% | −70.2% | 19% |
| ROC(20) > 0 | 0.66x | 34% | −0.23 | 81% | −79.4% | 39% |
| RSI(14) > 50 | 0.65x | 37% | −0.24 | 83% | −77.6% | 35% |
| Donchian 20/10 | 0.73x | 36% | −0.24 | 85% | −72.1% | 26% |
| Stochastic %K > 50 | 0.61x | 32% | −0.33 | 79% | −78.2% | 34% |
| SMA 200 | 0.63x | 24% | −0.33 | 78% | −75.0% | 25% |
| MFI(14) > 50 | 0.49x | 28% | −0.37 | 73% | −84.7% | 52% |
| MACD line > signal | 0.38x | 22% | −0.42 | 72% | −87.7% | 54% |
| OBV > its 20-bar average | 0.42x | 24% | −0.44 | 71% | −86.0% | 47% |
| Heikin-Ashi close > open | 0.41x | 25% | −0.45 | 70% | −86.2% | 44% |

**The family really is one trade.** Across twelve trend rules on BTC daily the positions
agree on a mean of **71%** of days (range: 45% for SMA 200 against MACD, 95% for SMA 200
against EMA 200; 36 of the 66 pairs agree more than 70% of the time), and a **majority vote
of all twelve** returned 15.84x at Sharpe 0.80 and a −45.2% drawdown at 52% exposure —
better than the *median* single rule (12.41x, 0.75) and worse than the *best* one (Ichimoku
24.67x), and comfortably better than holding (13.00x, 0.50). So voting gives you the average
outcome without the hindsight of having picked the winner, which is the same trade the
horizon blend makes in §1.6: robustness instead of a peak.

Read the table against §1.2: the universe, not the indicator, is what decides the median
outcome. Every rule here beats holding the same dying asset (70–88% of series) and every
rule still loses money on the median asset, exactly as TSMOM did in §1.4. So the answer
to "which indicator" is: **the simplest one you will actually follow**, because the family
is one trade in thirty costumes and the choice inside it is worth less than the choice of
universe, grid and exit. Two families are worth naming separately: **mean reversion is
dead in every form measured** (Bollinger reversion 0.71x median and 0.66x on BTC; RSI(2)
0.65x on BTC with 314 trades), and **the exotic trend proxies are not better than a
moving average** — Supertrend 7.49x, Parabolic SAR 4.31x, Heikin-Ashi 5.75x and linear
regression 7.66x on BTC, all below plain SMA 200 (12.15x) and usually with worse exits.
Volume-based indicators (OBV, A&nbsp;/D, VWAP) are trend proxies with a noisier input and
land at the bottom of both tables; MFI is the one exception and it is RSI with volume in
it.

Three of those families share one defect and it is worth naming: **DCA, value averaging
and a grid are schedules, not signals.** They decide *when money goes in*, never *when it
comes out*, so their result is whatever the asset did between the first contribution and
the last, reshaped by the schedule. The measurements above only show which shape is worse:
a grid and a martingale buy most aggressively at the bottom of a decline (exposure peaks
where the trend rules are in cash), and value averaging does the same but with a capital
demand that grows as the price falls. The one family here with a real, measured edge is an
**exit** — the trend rule in §1.4 — and the second is **how big the position is** (§1.5,
and the volatility target in §6).

---

## 3. Corrections — mistakes this project made and fixed

* **A hand-rolled backtest needs calibrating against `engine.py` before it means
  anything.** The indicator screen was first written with the position applied one bar
  late (two bars from the decision), which made every rule look about 30% worse and
  reordered the table: SMA 200 came out at 2.44x where the engine reports 3.1918x for the
  same rule and window. Feeding the engine's own position column back through the harness
  is what settled it — after the fix the harness reproduced 3.19x. Any new exploratory
  backtest should be checked against the engine on one rule before its numbers are quoted.


Kept here so nobody re-introduces them. **None of these was caught by the test
suite**; every one was caught by comparing two independent paths to the same
number.

* **The benchmark entered at the wrong bar.** `buy_and_hold` bought at `open[0]`
  of the series. `targets[0]` never trades, so `open[1]` is the earliest price any
  strategy can be filled at, and a benchmark entered at `open[0]` is credited with
  the first bar's move. On a listing bar that move *is* the result: PYTH-USDT's
  first hourly bar ran 0.06 → 0.319 (5.3x), which turned a −78.8% series into a
  "+13.5% buy & hold"; SUI 10.15x → 0.79x; BTC and ETH were unaffected because
  their first bar opens exactly where their second does. The basket module
  repeated the same mistake with a 12.8x listing bar and was fixed the same way.
* **The portfolio normalised its drifted book twice.** `run_portfolio` marks held
  weights to the next rebalance as `w·ratio / growth` and divided by `growth` a
  second time, so a risen book looked under-weighted and every rebalance paid
  commission for a position that was merely held — 61% phantom turnover per
  rebalance on a single position. The cross-section moved from −81.27% to −81.18%
  and fees from 7.35% to 7.23%, small only because a monthly rebalance genuinely
  replaces most of the book.
* **Artifacts overwrote each other.** `basket_<strategy>_<timeframe>` did not
  include the symbols, so a one-leg run silently replaced a five-leg chart. The
  legs are in the filename now.
* **The chart crashed on a wiped-out account**, because `log10(0)` is not a
  number; and labels were injected into the SVG unescaped, so an `&` in a name
  corrupted the file.

Method note that generalises: **tests pin what you already believe; cross-checks
find the rest.** The basket now has a test asserting that a one-leg basket must
reproduce `kcs-backtest` exactly — return, benchmark, trade count and fees — and
its window-fee helper is checked against the engine's own total.

Engine health, checked over the whole archive: 4,434 backtests, **zero wiped
accounts**, maximum `bookkeeping_error` 8.9e-15, no exceptions.

---

**The portfolio ran one rebalance late (fixed).** `run_portfolio` decided the book at
rebalance `k` and then marked the *previous* book over the period `k → k+1`, swapping the new
one in afterwards — so the strategy took effect one rebalance period after its signal: seven
bars on a weekly grid, thirty on a monthly one. The engine's convention (and what the report
claims) is that a decision taken on a close is exposed to the next bar. Found while
attributing P&L per pair: a replication of the loop matched the module to the digit under the
lagged convention, and the two conventions differ by a factor on the same book — 22.76x
(decided at k, earns k → k+1) against 56.70x (as it ran). Every portfolio number in this
repository, including the ones quoted in §1.7 and the earlier "the quote filter pays"
reading, was re-measured afterwards; `kcs-riskparity` never had the flaw (its loop states
"yesterday's decision takes effect at this close, never at its own"). The second half of the
same fix: the entry commission used to be written into `equity[-1]`, which on the first
iteration *is* `equity[0]` — the base the curve is normalised by — so it cancelled itself out
and was invisible in every published portfolio result. Both are pinned by tests
(`test_the_book_is_exposed_to_the_very_period_it_is_decided_in`,
`test_the_entry_commission_reaches_the_curve`).

**Numbers measured before that fix.** The exploratory figures that came from `/tmp` scripts
driving the old module — the "cross-sectional momentum over 965 symbols −81.39%" line below,
the monthly "buy what rose" 6.53x, the fee-sensitivity ladder and the per-pair grid
comparisons — were produced under the lagged convention. The *rankings* they establish (a
ranking loses, the sign filter is the least bad, the grid matters more than the signal) were
re-checked after the fix and still hold, but the levels moved, so quote the re-measured
tables in §1.7 and `AGENTS.md` rather than those.

## 4. Not modelled — read every number above with this in mind

* **Size, not the spread, is now the missing cost.** The spread *is* modelled where it matters:
  `kcs-basket --spread-model corwin-schultz` charges each pair half its own estimated spread
  per side, measured on hourly bars, and it costs the winning configuration 0.04 Sharpe
  (§1.6a). What no tool here knows is **how much you are trading**: there is no order size in
  the model, so market impact, partial fills and the queue are absent — and they are worst
  exactly on the small pairs where the spectacular tail returns came from. A flat fee is still
  the default everywhere else, and the small-pair tail is where it is most wrong.
* **Funding, borrow rates, liquidation.** No perp funding or margin interest is
  charged anywhere; the archive is spot. The short-leg numbers are therefore
  *optimistic*, which is one more reason they still lose.
* **The intrabar path itself.** Stops and take-profits are now expressible *inside* a bar
  (`engine.run_backtest(..., exit_prices=...)`, fed by `Strategy.intrabar_exits`), but the
  **convention** is an assumption and it is stated rather than measured: a bar that touches
  both levels is taken to have filled the stop first, the fill is the level, a gap through it
  fills at the open and a gap in your favour does not. OHLCV cannot tell you the true path, so
  every intrabar number in §1.11b inherits that choice — and it is the pessimistic one.
* **One regime.** Everything here is 2017–2026 crypto, which for alts was mostly a
  bear market. The edge is conditional on that.
* **Selection.** Every screen over the archive is in-sample. The honest
  out-of-sample results are the walk-forwards on BTC-USDT 1h, and each one is
  quoted with the split it used: TSMOM long-only, train 4,000 / test 1,000 bars,
  returned +1,086% against +875% for holding (Sharpe 0.66 vs 0.43); SMA 200 on a
  3,000 / 1,000 split returned **−2.18%** against +579%, which is the same rule
  that looks fine on a parameter sweep. Re-run either with `kcs-walkforward`
  before trusting the number.
* **Thin samples.** The median series has 12 closed trades and 42% have fewer than
  ten. The top 20 by Sharpe is noise: 85% of them have less than a year of history
  and a median of **zero** closed trades.

---

## 5. What to expect from it

The best survivable configuration measured here returns roughly **24% a year
with a −47% drawdown** (five majors over the 5.14-year window all legs share) or
~52% a year with −67% (BTC+ETH over 8.9 years). §6 puts the candidates on one
common five-year window; there the same basket returns 24.8% a year with a −51.4%
drawdown, and single-asset TSMOM on BTC returns 20.5% a year with −37.5%. Single-asset TSMOM has produced Sharpe 0.7 with drawdowns of −65% and
worse, on 4–5 assets that happened to survive.

There is nothing in this data supporting a smooth monthly target. The four
factors that destroy accounts here are measured: **turnover** (Section 1.1),
**leverage** (83% liquidated at 3x), **shorting** (16 wipeouts in 244 series) and
**illiquid pairs** (round trips of 1.7–15%). Size so that a −50% year is
survivable, and expect flat or negative years — the rule spent 50% of the last
year in cash, which is why it returned +1.52% while BTC fell 24.95%.

---

## 6. What to use, and in what order

Everything above is measurement; this is the decision it supports. One table, one
window, so the rows are comparable to each other instead of being dragged out of
different eras: the last five years of the daily archive, 2021-09-28 … 2026-09-27,
0.1% per side, spot, no leverage.

| what you would have run | five years | CAGR | vol | Sharpe | max DD |
|---|---|---|---|---|---|
| **TSMOM on five majors** — 30-day lookback, weekly decision, 1h, equal weight | **+202.9%** (3.03x) | 24.8% | 42.8% | 0.52 | −51.4% |
| TSMOM on BTC alone — 30-day, weekly, 1d | **+154.1%** (2.54x) | 20.5% | 34.5% | **0.55** | **−37.5%** |
| buy & hold BTC | +105.7% (2.06x) | 15.5% | 51.5% | 0.28 | −76.6% |
| rule-picked book, inverse-vol weights, 40% budget (`kcs-riskparity`) | +101.8% (2.02x) | 15.1% | 51.1% | 0.27 | −62.8% |
| the same book with a 200-day trend gate | +91.8% (1.92x) | 13.9% | 40.6% | 0.32 | −47.7% |
| the same book with a 30-day trend gate | −6.6% (0.93x) | −1.4% | 28.1% | −0.05 | −50.7% |
| holding those five majors passively | +51.1% (1.51x) | 8.6% | 59.6% | 0.14 | −78.5% |
| holding the rule-picked selection passively | +49.3% (1.49x) | 8.3% | 62.3% | 0.13 | −71.1% |
| **`tsmom-blend` on five majors** — horizons of 1/2/4/8 weeks, weekly, no lookback to choose | **+199.2%** (2.99x) | 24.5% | 35.7% | **0.61** | **−31.7%** |
| `voltarget-tsmom` on five majors (40% vol target) | +82.4% (1.82x) | 12.8% | 23.2% | 0.52 | −32.6% |
| `tsmom-blend` on **ten** majors (adds ADA, DOGE, AVAX, LINK, DOT) | +124.6% (2.25x) | 17.6% | 36.0% | 0.45 | −37.8% |
| holding those ten majors passively | −10.6% (0.89x) | −2.2% | 65.1% | −0.03 | −81.2% |

The basket row runs on hourly bars over its own five-year span (2021-09-29 …
2026-09-28, the window all five legs share); every other row is daily. A day of
overlap does not matter here: each row pays 0.1% a side, none uses leverage, and
each carries its own passive comparison from the same run.

Read it by rows, not by headlines:

* **Trend following on liquid names is first, and it wins on both sides of the
  comparison**: the basket made +202.9% against +51.1% for holding the same five,
  and single-asset TSMOM made +154.1% against +105.7% for holding BTC. Same rule,
  same window, same costs — the only thing that beat a passive hold on return *and*
  risk at once.
* **Diversifying the rule beat improving it.** The basket and the single asset are
  the same signal; spreading it over five liquid names added 49 points of return.
  The single-asset version has the better drawdown (−37.5% against −51.4%), which is
  the honest reason to prefer it if you size by pain rather than by return.
* **Risk parity did not beat holding BTC over these five years** (+101.8% against
  +105.7%, same Sharpe), although it beats BTC over the 8.9-year history at a much
  better drawdown (−62.8% against −82.9%). Its value is the rule-picked universe and
  the risk shape, not extra return.
* **A trend gate on the book improves the shape, not the return**: 200 days turned
  +101.8% into +91.8% while cutting the drawdown from −62.8% to −47.7% and lifting
  Sharpe from 0.27 to 0.32, at 54% of capital at work. The 30-day gate destroyed the
  result (−6.6%): on a book that is re-selected monthly, a fast gate whipsaws. Gate
  slowly or not at all.
* **Everything passive lost to everything active**, with one exception worth staring
  at: plain BTC beat both risk-parity rows. Any rule that cannot beat the
  `--buy-hold` line over a full cycle is decoration.
* **Blending horizons is the best signal for a basket.** `tsmom-blend` on the same
  five majors returned the same money as a tuned 30-day lookback (+199.2% against
  +202.9%) with the drawdown cut from −51.4% to −31.7% and Sharpe lifted from 0.52 to
  0.61 — and it has **no lookback to choose**, which is the point: it cannot be fitted
  to the window it is judged on. On BTC alone the same blend was much worse (+62.4%
  against +154.1%), so this is a diversification effect, not a better rule.
* **More names is not more diversification.** Adding five more majors (ADA, DOGE,
  AVAX, LINK, DOT) took the basket from +199.2% to +124.6% while their passive hold
  returned −10.6%: the extra names were weaker alts, and the rule still beat holding
  them by 135 points. Five liquid majors beat ten.

### Where each knob belongs

The same measurements say which of these is a *sizing* tool and which is a
*selection* tool, and getting that the wrong way round costs money:

| knob | on one concentrated asset | on a diversified basket |
|---|---|---|
| volatility target (40%) | **helps**: TSMOM on BTC 0.55 → 0.58 Sharpe, and SMA 200 0.66 → **0.71** with the drawdown −36.2% → **−26.3%** | **only de-risks**: `voltarget-tsmom` on five majors keeps Sharpe at 0.52 but halves the return (+82.4% against +202.9%) |
| blending horizons | hurts on BTC (+62.4% against +154.1%) | **helps**: −20 points of drawdown, same return, nothing to tune |
| a trend gate on a rebalanced book | — | improves shape, not return (200d: +101.8% → +91.8%, drawdown −62.8% → −47.7%) |
| a drawdown overlay (`--dd-scale 10,40,25`) | **neither**: return and drawdown fall together, Sharpe unchanged (TSMOM 24.94x/0.80/−65.6% → 12.25x/0.78/−44.1%; SMA 200 12.15x/0.67/−64.1% → 6.51x/0.68/−43.3%; `voltarget-sma` 8.12x/0.79/−45.4% → 5.26x/0.77/−35.8%) | same, and on the last five years it costs more Sharpe than it saves (0.55 → 0.48) |

So: **volatility sizing on concentrated positions, blending on diversified ones**, and
neither as a substitute for the other.

### Also measured, and dominated

Run over the same five years on BTC daily, so they can be compared to the rows above:
`SMA 200` every bar +219.2% at Sharpe 0.66 and −36.2%; `SMA 200` decided weekly
+197.4%, 0.62, −34.2%; `voltarget-sma` (200-day SMA plus a 40% volatility target)
**+185.4%, Sharpe 0.71, drawdown −26.3%, 19 trades in five years** — the best
risk-adjusted row in this file; `MACD` +84.9%, 0.37; `breakout` (Donchian 20/10)
+41.2%, 0.22; `rsi-rev` −29.2%, −0.25 (rejected again, on a fifth window).

The volatility-targeted SMA is the one to promote: its parameter is a **plateau**, not
a knife edge — with the same 40% target, SMA windows of 50 / 100 / 150 / 200 days give
Sharpe **0.78 / 0.70 / 0.77 / 0.71** and final equities 3.01x / 2.67x / 3.01x / 2.85x,
and it only degrades past 250 (0.47). Anything in that range is the same rule.

**One correction to §6's advice.** "Never read the signal every bar" was measured
archive-wide on *hourly* bars (§1.1), where a slow signal sampled hourly whipsaws
across zero. On **daily** bars it is not true: SMA 200 read every bar (+219.2%, 0.66)
slightly beat the same rule decided weekly (+197.4%, 0.62) on BTC. The grid matters when
you sample a slow signal fast; at daily frequency there is nothing to fix.

### The stack I would actually run

**One constraint comes before every number in this file: this is a spot account.** A spot
balance cannot hold a negative position, so every recommended configuration is long or flat,
and the tools now refuse to run anything else unless told otherwise (`kcs-backtest`,
`kcs-basket` and `kcs-walkforward` all take `--allow-short`, and the long/short variants are
named with an `-ls` suffix that no recommendation uses). The short-side work in §2 was done to
*test* whether the mirror image of a long rule pays — it does not, which is one more reason the
constraint costs nothing here.

1. **Universe** — five to ten of the most liquid majors, chosen by a rule (trailing
   turnover strictly before the window you are judging, §1.3–1.5), never by hand. This
   *was* the largest weakness in this file and it is now measured: the same rule on the
   top five by turnover as of 2021-08-04 returns +123.6% at Sharpe 0.87 and on the top ten
   **+163.9% at 1.17**, both with a −17…−21% drawdown, so the rule does not need the
   hindsight. `kcs-basket --select-turnover N --last …` does it in one command.
2. **Signal** — on a basket, the horizon *blend* (1/2/4/8 weeks, majority vote):
   the same return as a tuned lookback with 20 points less drawdown and nothing to
   fit. On a single asset, a 30-day TSMOM or a 150–200-day SMA — the two are a coin
   flip apart (§1.1), so take the one you will actually follow.
3. **Decision frequency** — weekly when you sample a slow signal on hourly bars
   (§1.1: +0.38 Sharpe, and it survives a zero fee). At daily frequency there is
   nothing to fix: SMA 200 every bar and weekly were 0.66 against 0.62 on BTC.
   Monthly is worse than weekly at both frequencies.
4. **Sizing** — this is the knob that turned out to matter most, and it belongs on the whole
   book, not only on single positions. A 25–30% annual target on a five-to-ten-name trend
   basket is the best risk-adjusted configuration measured anywhere in this file (§1.4):
   Sharpe 0.87–1.17 with a −17…−21% drawdown against ~0.35 and −79% for holding the same
   names. It also repairs the wide book (§1.5): the gated 836-pair rule goes from +2,675% at
   Sharpe 0.49 and −86.8% to +655% at **0.73** and **−53.8%**, with the commission bill
   falling from 1,559% of capital to 113%. Lower targets buy Sharpe (25% beat 40% on every
   window measured); the cap is 1.0 — no leverage; equal weight inside the book, and **never**
   rebalance the names against each other monthly, which averages down into the weakest leg
   (Sharpe 0.19 against 0.87 for the same idea).
5. **Costs** — budget at least 0.1% per side on majors and much worse elsewhere;
   every number here already pays it.
6. **Never** — leverage (83% of 3x runs were liquidated), shorting, pairs whose
   round trip is above ~1%, reading a slow signal every bar, or choosing a lookback
   on the same data you then judge it on.

### In one line

Best measured five-year outcome: **the ten busiest pairs, a 50-bar trend filter per name and
a 30% volatility target — +91% at Sharpe 0.87 with a −17.5% drawdown**, chosen by rule as of
the window's first day and reproducibly exactly that by one command
(`kcs-basket --select-turnover 10 --timeframe 1d --strategy voltarget-sma --param window=50
--param target_vol=0.30 --last 5y`); the equal-weight hold of the same ten is −28%. The tuned
hourly alternatives still win on raw return (+199% from the blend, +203% from a tuned
lookback on five majors) but carry two to three times the drawdown, and on BTC alone every
one of these rules **lost** money over the same five years (−49…−61%) while holding gained
+75% — which is the whole argument for a basket. Nothing in this repository supports ~5% a
month; the four things that destroy an account here are measured in §1.1, §1.2 and §5 —
turnover, leverage, shorting and illiquid pairs.
Size so that a −50% year is survivable, keep the `--buy-hold` line on the chart, and
treat any single multiple as noise until a walk-forward agrees with it.

---

## 7. Next steps, in priority order

1. **The measurement programme is complete for what this archive can answer** — the basket in
   §1.5a (compounded +75.9%, worst year −7.85%), the wide book's gates and sizing in §1.5b
   (+46.9% against −36.8% for holding the same 840 pairs), the rolling `--max-below-peak` in
   §1.5c (+612.3% and stable), and that gate's own placebo under sizing (−67.4% at the same book
   size). Two threads are left and neither is a dial: **an archive with delistings** (this one
   has none by construction, which flatters every drawdown gate) and **order size** (`--vol-window`
   = 12 rebalances is also still assumed rather than measured, and it is the last fitted number).
2. **The universe question is now answered for this rule.** "Remove the hindsight from the
   hand-picked asset list" — the thread this file opened at §1.9 — is closed: names chosen by
   turnover as of the window's first day do at least as well as the hand-picked five
   (`kcs-basket --select-turnover N`). What remains open is whether the *selection rule*
   itself holds up when the window moves, which is item 1.
3. **Per-symbol market impact** instead of a flat 0.1% taker. This is the last unmodelled
   part of the cost picture and it bites exactly the pairs that produce the tail — and the
   winning configuration trades the busiest names, where it should bite least.
4. **Walk-forward the health-gate thresholds** (`--max-below-peak` has no plateau; `--trend-gate`
   does) and the sizing dial (`--vol-target`/`--vol-window`), which is the knob with the largest
   measured effect on the wide book.
5. **Beyond the stated intrabar convention.** The fill model exists (`exit_prices`), the
   convention is written down (stop first, level fills, gaps against you), and §1.11b shows the
   exits cost money — so what is left is not machinery but a *better path model* (tick data, or
   a sub-bar reconstruction) for anyone who wants to argue that a stop filled at a better price
   than this convention assumes. Until then the honest position is §1.11b's.

## 8. Reproducing the headline numbers

```bash
uv run pytest                          # 510 tests, ~45 s

# one asset
uv run kcs-backtest --symbol BTC-USDT --strategy tsmom \
    --param lookback=720 --param rebalance=168

# the whole archive as one book, with the health gates on the dying names
uv run kcs-portfolio --timeframe 1d --lookback 7 --rebalance 7 --select sign \
    --trend-gate 200 --pairs-csv analysis/out/gated.csv

# the same book without them, which is what the tables in §1.3 compare against
uv run kcs-portfolio --timeframe 1d --lookback 7 --rebalance 7 --select sign

# a basket of named assets: combined curve, CSV, SVG chart and JSON
uv run kcs-basket --symbols BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,BNB-USDT --json

# the rule-picked book, bought and held unevenly, with a passive reference line
uv run kcs-riskparity --top 5 --min-history 3y --weight invvol --vol-budget 0.4 --last 5y
uv run kcs-riskparity --top 5 --min-history 3y --weight invvol --trend 200d --last 5y

# what parameters would have been chosen on the past, and how they did after
uv run kcs-walkforward --strategy tsmom --grid lookback=336,720 \
    --train 4000 --test 1000
```

The archive-wide screen (Section 1.4) is one backtest per series with the rule
expressed in calendar time. There is no CLI for it yet; this is the whole thing:

```python
from analysis import Costs, data, engine
from analysis.strategies import get_strategy

for symbol, timeframe in data.available_series("data/kucoin/spot"):
    bars = data.load_series("data/kucoin/spot", symbol, timeframe)
    days = 86400
    step = 30 * days if timeframe == "1mon" else data.interval_seconds(timeframe)
    lookback, rebalance = max(1, round(30 * days / step)), max(1, round(7 * days / step))
    if len(bars) < lookback + 20:
        continue
    strategy = get_strategy("tsmom", lookback=lookback, rebalance=rebalance)
    result = engine.run_backtest(
        bars, strategy.targets(bars), timeframe, Costs(fee_per_side=0.001)
    )
    print(symbol, timeframe, result.performance.sharpe, result.benchmark.performance.sharpe)
```

Four shells in parallel finish the 4,434 series in about two and a half minutes
(`/dev/shm` is not writable in some sandboxes, so `multiprocessing` is not an
option there; shard the list instead).

**The configuration that works, in one command** (chosen by rule as of the window's first
day, no hand-picked names) — and it is **recorded in `journal/runs.jsonl`**, so this result
can be re-run and compared at any time rather than believed:
`uv run kcs-journal verify --id 20261002T081142Z`:

```bash
uv run kcs-basket --select-turnover 10 --timeframe 1d --strategy voltarget-sma \
    --param window=50 --param target_vol=0.30 --last 5y
```

**The three-approach comparison.** The rule-picked rows come from
`uv run kcs-portfolio --timeframe 1d --lookback 7 --rebalance 7 --select sign --trend-gate 200
--last 267` (with and without `--vol-target 25%`), the narrow ones from
`uv run kcs-basket --timeframe 1d --strategy voltarget-sma --param window=50 --param
target_vol=0.30 --symbols ...` over the same 5.16 years, and the no-hindsight basket was
chosen by median quote turnover over the 90 days before the window opened (a throwaway
script: rank every USDT pair by that median, take the top five or ten). `kcs-riskparity`
rows use `--min-history 3y --last 1870d`.

**The gate experiment and its placebo.** The tables in §1.3 come from a throwaway script
that built the panels once and then, for each configuration, ran the same book through
`run_portfolio(gates=...)`; the placebo repeated each run with the `gates` readings rotated
between symbols (`random.Random(seed).shuffle` over the panels' gate lists), which keeps the
thresholds and the average breadth identical while removing all information. Three details
worth copying if you redo it: rebuild the panels for every gate *window* you sweep (a
`GateSpec` only selects which pre-computed series to read, so a window sweep over one panel
set returns identical columns), carry the sliced date list into `build_panel` as well as into
`run_portfolio` when you test a sub-period, and report the *share of the baseline's losing
pairs that are still traded* — that is the number that says whether a filter screens death or
merely shrinks the book.
