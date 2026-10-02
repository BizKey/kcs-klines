"""What it costs to be *your* size, not a price-taker of zero size.

Everything else in this toolkit charges a flat fee plus (optionally) the pair's spread. Both
are independent of how much you are trading, which is fine for a hundred dollars and wrong for
a million: the part of the cost that scales with your order is market impact.

The model here is the standard square-root one, in the form that only needs what an OHLCV
archive has:

    impact per side = coefficient * sigma_bar * sqrt(order_usd / turnover_per_bar)

* `turnover_per_bar` is the median quote turnover of one bar — how much the pair actually
  trades while your order is working (daily bars therefore mean daily volume).
* `sigma_bar` is the per-bar volatility of the same window, so a wild pair costs more than a
  calm one at the same participation.
* `order_usd` is the notional of one trade: on a basket, the leg's weight times the capital.
* The coefficient is deliberately a knob, not a fitted number: the literature puts it between
  roughly 0.1 and 1 for the square-root law, so the honest reading is a band, not a point.
  Default 0.1 (mild); the tests and the report use it, and `--impact-coefficient` moves it.

The square root is what makes the answer non-obvious: doubling your size does **not** double
the cost, and the pair's turnover matters as much as your capital does.
"""

from __future__ import annotations

import math
import statistics

from .data import Bar

__all__ = ["leg_market_stats", "participation", "impact_fraction", "DEFAULT_COEFFICIENT"]

#: Mild square-root coefficient; the measured band is roughly 0.1-1.
DEFAULT_COEFFICIENT = 0.1


def participation(order_usd: float, turnover_per_bar: float) -> float | None:
    """Your order as a fraction of what the pair trades in one bar, or `None` if unknown."""
    if turnover_per_bar <= 0 or order_usd < 0:
        return None
    return order_usd / turnover_per_bar


def impact_fraction(
    order_usd: float,
    turnover_per_bar: float,
    sigma_bar: float,
    *,
    coefficient: float = DEFAULT_COEFFICIENT,
) -> float | None:
    """The proportional cost of one side of a trade of `order_usd`, or `None`."""
    share = participation(order_usd, turnover_per_bar)
    if share is None:
        return None
    if sigma_bar <= 0:
        return 0.0
    return coefficient * sigma_bar * math.sqrt(share)


def leg_market_stats(
    times: list[int], closes: list[float], turnover: list[float], *, before: int | None = None
) -> tuple[float, float] | None:
    """Median turnover per bar and per-bar volatility, measured strictly before `before`.

    The volatility is the standard deviation of log returns on the series' own bar spacing,
    so a daily series gives a daily figure and an hourly one an hourly figure: the
    participation and the volatility are then in the same units as the order.
    """
    sample = [
        (close, volume)
        for moment, close, volume in zip(times, closes, turnover)
        if before is None or moment < before
    ]
    sample = [(close, volume) for close, volume in sample if close > 0 and volume > 0]
    if len(sample) < 30:
        return None
    volumes = [volume for _, volume in sample]
    closes_ = [close for close, _ in sample]
    returns = [
        math.log(later / earlier)
        for earlier, later in zip(closes_, closes_[1:])
        if earlier > 0 and later > 0
    ]
    if len(returns) < 2:
        return None
    return statistics.median(volumes), statistics.stdev(returns)
