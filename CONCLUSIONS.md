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

### 1.3 Costs

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

### 1.4 Trend following on liquid survivors is the only thing that survived

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

### 1.5 The positive mean is a handful of assets

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

### 1.6 Diversifying the same rule across liquid survivors is where it works

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

### 1.7 The same signal, built two ways

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

* **Spread and slippage per symbol.** Every result uses a flat 0.1% fee and
  `--slippage 0`. Small pairs are worse in reality, and small pairs are exactly
  where the spectacular tail returns (+35000%) came from.
* **Funding, borrow rates, liquidation.** No perp funding or margin interest is
  charged anywhere; the archive is spot. The short-leg numbers are therefore
  *optimistic*, which is one more reason they still lose.
* **Intrabar stops.** Only close-based rules are expressible today; ATR trailing
  and break-even stops need an explicit fill model in `engine.py`.
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

1. **Universe** — five to ten of the most liquid majors, chosen by a rule (trailing
   turnover, at least three years of history, re-selected monthly or quarterly),
   never by hand. The hand-picked five are the largest remaining weakness in the
   best result in this file.
2. **Signal** — on a basket, the horizon *blend* (1/2/4/8 weeks, majority vote):
   the same return as a tuned lookback with 20 points less drawdown and nothing to
   fit. On a single asset, a 30-day TSMOM or a 150–200-day SMA — the two are a coin
   flip apart (§1.1), so take the one you will actually follow.
3. **Decision frequency** — weekly when you sample a slow signal on hourly bars
   (§1.1: +0.38 Sharpe, and it survives a zero fee). At daily frequency there is
   nothing to fix: SMA 200 every bar and weekly were 0.66 against 0.62 on BTC.
   Monthly is worse than weekly at both frequencies.
4. **Sizing** — equal weight across the names that pass; a volatility target (~40% a
   year) **on concentrated positions**, where it is the best risk-shape tool measured
   (SMA 200 + vol target on BTC: Sharpe 0.71, drawdown −26.3%, 19 trades in five
   years), not on an already-diversified basket, where it only de-risks; cash for the
   rest, no leverage.
5. **Costs** — budget at least 0.1% per side on majors and much worse elsewhere;
   every number here already pays it.
6. **Never** — leverage (83% of 3x runs were liquidated), shorting, pairs whose
   round trip is above ~1%, reading a slow signal every bar, or choosing a lookback
   on the same data you then judge it on.

### In one line

Best measured five-year outcome: **+203% (24.8% a year) with a −51% drawdown** (a tuned
lookback on five majors) or the same money at **+199% with a −31.7% drawdown** from the
blend, which needs no tuning. Best risk shape: **+185% (23.3% a year) with a −26.3%
drawdown and 19 trades in five years** (`voltarget-sma` on BTC daily). Nothing in this
repository supports ~5% a month; the four things that destroy an account here are
measured in §1.1, §1.3 and §5 — turnover, leverage, shorting and illiquid pairs.
Size so that a −50% year is survivable, keep the `--buy-hold` line on the chart, and
treat any single multiple as noise until a walk-forward agrees with it.

---

## 7. Next steps, in priority order

1. **Remove the hindsight from the asset list.** The best result here uses five
   hand-picked survivors. Replace it with a rule — e.g. every perp-listed pair
   with 5+ years of history, re-selected quarterly on data available at the time —
   and re-measure the basket. If the edge survives a rule-based universe it is
   real; if it does not, the +207% was selection. *Partly answered since this was
   written:* applying the history filter to the cross-sectional book moved the passive
   baseline nine points and the momentum ranking none (§1.2), so a rule-based universe
   will not rescue a ranking that is anti-informative — the open question is whether it
   rescues the *basket of hand-picked survivors*, which is a different test.
2. **Per-symbol spread and slippage** instead of a flat 0.1% taker. This is the
   last unmodelled part of the cost picture and it bites exactly the pairs that
   produce the tail.
3. **Walk-forward the basket and the portfolio**, not just a single series. Only
   one rule on one asset currently has an honest out-of-sample number.
4. **Intrabar stops — last.** Time-based exits already work and stops are paid for
   in commission; there is no measured evidence they would help.
5. **Walk-forward the trend lookback.** The two halves are now connected —
   `kcs-riskparity --trend 30d` sizes a book *and* leaves the market — and the gate does
   what it promises: on the 12 months that exposed the problem it turned −36.24% into
   −3.53% with the drawdown cut from −58.6% to −17.6%. But it did that by being at work
   only 25% of the time, and over eight years a fast gate costs more return than it
   saves (top 5: +1071% ungated, +177% at 30d, +868% at 200d). The lookback is now the
   module's most powerful and least justified knob: +0.41% / −3.53% / −28.70% on the same
   12 months at 7d / 30d / 90d, with a different best value at each horizon. It needs the
   treatment `kcs-walkforward` gives a strategy — re-choose it per window on past data
   only — before any of these numbers means anything.
6. **Per-symbol spread and slippage** instead of a flat 0.1% taker. This is the
7. **Do not touch shorting, leverage or small pairs.** The data is unambiguous on
   all three, and all three are in the same direction as the losses this project
   was built to understand.

---

## 8. Reproducing the headline numbers

```bash
uv run pytest                          # 376 tests, ~26 s

# one asset
uv run kcs-backtest --symbol BTC-USDT --strategy tsmom \
    --param lookback=720 --param rebalance=168

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
