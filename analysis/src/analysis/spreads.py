"""What the round trip actually costs, estimated from the bars themselves.

The engine charges a flat fee because that is the only number it can know. A pair's real
cost is wider than the fee: you cross the spread on entry and again on exit. With only
OHLCV there is no bid/ask to read, but there is a published estimator for exactly this
situation — Corwin & Schultz (2012), "A Simple Way to Estimate Bid-Ask Spreads from Daily
High and Low Prices" — which infers the proportional spread from two consecutive bars.

The intuition: the two-day high-low range covers both days' true ranges plus the gap the
price had to cross between them, so the part of the range that the single days cannot
explain is the spread. It is a *proxy*: a strongly trending two-day stretch makes it
over-estimate, so negative estimates are set to zero (the paper's own recommendation) and
the window's **median** is reported rather than its mean.

Everything here reads bars at or before the date it is asked about, so a spread estimate can
never peek into the window it is about to be used in.
"""

from __future__ import annotations

import math
import statistics

from .data import Bar

__all__ = ["corwin_schultz", "spread_by_symbol", "SPREAD_MODELS"]

#: Which estimators `kcs-basket --spread-model` understands.
SPREAD_MODELS = ("none", "corwin-schultz")

_ROOT_TWO_MINUS_ONE = math.sqrt(2.0) - 1.0
_K = 3.0 - 2.0 * math.sqrt(2.0)


def _two_day_spread(first: Bar, second: Bar) -> float | None:
    """One Corwin-Schultz estimate from two consecutive bars, or `None` when unusable."""
    if min(first.high, first.low, second.high, second.low) <= 0:
        return None
    if first.high < first.low or second.high < second.low:
        return None
    beta = (math.log(first.high / first.low) ** 2
            + math.log(second.high / second.low) ** 2)
    gamma = math.log(
        max(first.high, second.high) / min(first.low, second.low)
    ) ** 2
    alpha = (math.sqrt(2.0 * beta) - math.sqrt(beta)) / _K - math.sqrt(gamma / _K)
    return 2.0 * (math.exp(alpha) - 1.0) / (1.0 + math.exp(alpha))


def corwin_schultz(
    bars: list[Bar], *, window: int | None = None, before: int | None = None
) -> float | None:
    """The median estimated proportional spread of a series, or `None`.

    `window` keeps only the last that many bars; `before` (an epoch second) drops every bar
    at or after it — the causal switch a walk-forward needs. Negative daily estimates are
    floored at zero before the median, as the paper recommends.
    """
    usable = [bar for bar in bars if before is None or bar.time < before]
    if window:
        usable = usable[-window:]
    if len(usable) < 3:
        return None
    estimates = []
    for first, second in zip(usable, usable[1:]):
        value = _two_day_spread(first, second)
        if value is not None:
            estimates.append(max(value, 0.0))
    if not estimates:
        return None
    return statistics.median(estimates)


def spread_by_symbol(
    series: dict[str, list[Bar]], *, window: int | None = None, before: int | None = None
) -> dict[str, float]:
    """`{symbol: estimated proportional spread}` for whichever symbols can be measured."""
    measured = {}
    for symbol, bars in series.items():
        value = corwin_schultz(bars, window=window, before=before)
        if value is not None:
            measured[symbol] = value
    return measured
