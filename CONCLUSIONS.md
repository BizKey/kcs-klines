# CONCLUSIONS.md — what the data has actually shown

Written for whoever picks this up next, human or agent. `AGENTS.md` says what
the place is and what has been learned the hard way; this file says what the
*measurements* support, what they killed, and what is still missing. Every number
here was produced by the toolkit in this repository on the archive in `data/`,
and the recipe to reproduce the big one is at the bottom.

Read it as a decision record, not as documentation. Where a claim is conditional,
the condition is stated.

---

## 1. The five facts that decide everything

Ordered by how much they change an outcome.

### 1.1 Costs, not signals

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

### 1.2 The universe is a graveyard, and the cross-section ranks it by pulse

996 pairs, of which the top 10 are 61.7% of all turnover and the top 200 are 94%.
The median pair has a maximum drawdown of **−97.5%**, is underwater 99% of the
time, and has a median daily return of −0.21%. Since 2023-10-06 the median pair
is at **0.31x** while BTC is 3.02x, SOL 4.91x, XRP 2.86x, ETH 1.63x; 81% of pairs
are in the red and the median maximum drawdown is −82.1%. Half a year after
listing, only 25% of pairs are above their day-one price and the median is
**−49.9%**.

**Consequence:** any statistic over "all assets" is dominated by dying listings,
and any rule that *selects* from the whole universe selects them on purpose.
Cross-sectional momentum over all pairs returned **−81.2%** against −47.4% for
equal weighting; restricted to the top 5 by momentum it returned +2.6% over nine
years while paying 46.8% of capital in commission and drawing down −97.4%.

### 1.3 Trend following on liquid survivors is the only thing that survived

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

### 1.4 The positive mean is a handful of assets

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

### 1.5 The grid, not the signal

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

---

## 3. Corrections — mistakes this project made and fixed

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
with a −47% drawdown** (five majors) or ~52% a year with −67% (BTC+ETH over 8.9
years). Single-asset TSMOM has produced Sharpe 0.7 with drawdowns of −65% and
worse, on 4–5 assets that happened to survive.

There is nothing in this data supporting a smooth monthly target. The four
factors that destroy accounts here are measured: **turnover** (Section 1.1),
**leverage** (83% liquidated at 3x), **shorting** (16 wipeouts in 244 series) and
**illiquid pairs** (round trips of 1.7–15%). Size so that a −50% year is
survivable, and expect flat or negative years — the rule spent 50% of the last
year in cash, which is why it returned +1.52% while BTC fell 24.95%.

---

## 6. Next steps, in priority order

1. **Remove the hindsight from the asset list.** The best result here uses five
   hand-picked survivors. Replace it with a rule — e.g. every perp-listed pair
   with 5+ years of history, re-selected quarterly on data available at the time —
   and re-measure the basket. If the edge survives a rule-based universe it is
   real; if it does not, the +207% was selection.
2. **Per-symbol spread and slippage** instead of a flat 0.1% taker. This is the
   last unmodelled part of the cost picture and it bites exactly the pairs that
   produce the tail.
3. **Walk-forward the basket and the portfolio**, not just a single series. Only
   one rule on one asset currently has an honest out-of-sample number.
4. **Intrabar stops — last.** Time-based exits already work and stops are paid for
   in commission; there is no measured evidence they would help.
5. **Do not touch shorting, leverage or small pairs.** The data is unambiguous on
   all three, and all three are in the same direction as the losses this project
   was built to understand.

---

## 7. Reproducing the headline numbers

```bash
uv run pytest                          # 280 tests, ~8 s

# one asset
uv run kcs-backtest --symbol BTC-USDT --strategy tsmom \
    --param lookback=720 --param rebalance=168

# a basket of named assets: combined curve, CSV, SVG chart and JSON
uv run kcs-basket --symbols BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,BNB-USDT --json

# what parameters would have been chosen on the past, and how they did after
uv run kcs-walkforward --strategy tsmom --grid lookback=336,720 \
    --train 4000 --test 1000
```

The archive-wide screen (Section 1.3) is one backtest per series with the rule
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
