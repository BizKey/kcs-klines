"""Small, dependency-free indicator and performance-statistics helpers.

Everything takes plain Python lists so the engine stays easy to audit: there is
no NumPy, no pandas, and no hidden state between calls.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass


def sma(values: list[float], window: int) -> list[float | None]:
    """Simple moving average; `None` until a full window is available.

    `sma(v, w)[i]` is the mean of `v[i-w+1 .. i]`, i.e. it includes the current
    value — the convention a bar-close strategy expects (no peek into the bar
    that follows).
    """
    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= window:
            running -= values[i - window]
        if i >= window - 1:
            out[i] = running / window
    return out


def max_drawdown(equity: list[float]) -> tuple[float, int, int]:
    """Worst peak-to-trough fall: `(drawdown, peak_index, trough_index)`."""
    if not equity:
        raise ValueError("empty equity curve")
    peak = equity[0]
    peak_i = 0
    worst = 0.0
    span = (0, 0)
    for i, value in enumerate(equity):
        if value > peak:
            peak, peak_i = value, i
        drawdown = value / peak - 1.0 if peak > 0 else 0.0
        if drawdown < worst:
            worst, span = drawdown, (peak_i, i)
    return worst, span[0], span[1]


def years_from_bars(bars: int, bars_per_year: float) -> float:
    return bars / bars_per_year if bars_per_year > 0 else 0.0


def cagr(final_equity: float, years: float) -> float:
    """Compound annual growth rate.

    Annualising a window shorter than a few weeks is arithmetic, not
    information, and the exponent overflows for short profitable runs: that case
    is reported as `inf` rather than crashing a report.
    """
    if years <= 0 or final_equity <= 0:
        return 0.0
    try:
        return math.expm1(math.log(final_equity) / years)
    except OverflowError:
        return math.inf


@dataclass(frozen=True, slots=True)
class Performance:
    """Realised performance of one equity curve."""

    final_equity: float
    total_return: float
    cagr: float
    ann_vol: float
    sharpe: float
    max_dd: float
    max_dd_start: int
    max_dd_end: int
    years: float

    def as_dict(self) -> dict:
        return asdict(self)


def performance(
    equity: list[float],
    bars_per_year: float,
    *,
    final_equity: float | None = None,
    timestamps: list[int] | None = None,
) -> Performance:
    """Summarise an equity curve marked once per bar.

    Volatility comes from per-bar simple returns, Sharpe from per-bar *log*
    returns (both annualised by `sqrt(bars_per_year)`), and the drawdown from the
    curve itself. `final_equity` overrides the last point, which is how an open
    position is marked to market. `timestamps` (unix seconds, one per bar) are
    echoed back as the drawdown span.
    """
    if len(equity) < 2:
        raise ValueError("need at least two equity points")

    series = list(equity)
    if final_equity is not None:
        series[-1] = final_equity

    returns = [series[i] / series[i - 1] - 1.0 for i in range(1, len(series)) if series[i - 1] > 0]
    if len(returns) < 2:
        vol = sharpe = 0.0
    else:
        annualiser = math.sqrt(bars_per_year)
        vol = statistics.pstdev(returns) * annualiser
        logs = [math.log1p(r) for r in returns if r > -1.0]
        sd_log = statistics.pstdev(logs) if len(logs) > 1 else 0.0
        sharpe = (statistics.fmean(logs) / sd_log) * annualiser if sd_log > 0 else 0.0

    dd, dd_start, dd_end = max_drawdown(series)
    years = years_from_bars(len(series), bars_per_year)
    final = series[-1]
    return Performance(
        final_equity=final,
        total_return=final - 1.0,
        cagr=cagr(final, years),
        ann_vol=vol,
        sharpe=sharpe,
        max_dd=dd,
        max_dd_start=timestamps[dd_start] if timestamps else dd_start,
        max_dd_end=timestamps[dd_end] if timestamps else dd_end,
        years=years,
    )


def pct(x: float) -> str:
    """`0.1234` -> `12.34%`."""
    return f"{x * 100:,.2f}%"
