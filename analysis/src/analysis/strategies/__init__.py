"""Strategy registry — the machinery lives in `analysis.strategies.registry`.

This module is the package's front door. It imports the registry *first* (so any
strategy module can do `from .registry import register` at import time without a
circular import) and then the strategies themselves, which register on import.

Adding your own strategy means one import line here plus the module itself;
nothing else in the toolkit needs to know it exists. The CLI, the report, the
artifacts and the contract tests all read the registry:

    from analysis.strategies import get_strategy

    strategy = get_strategy("sma", window=100)

The factory's named parameters (defaults included) are exactly what `--param`
and `--sweep` accept on the command line; `run_backtest --list` prints them.
"""

from __future__ import annotations

from .base import Strategy
from .registry import (
    REGISTRY,
    available,
    describe_registry,
    get_strategy,
    parameters,
    register,
    sweep_parameter,
)
from .sma_reversion import SmaReversion
from .sma_trend import SmaTrend, SmaTrendLongShort

# Import strategy modules below this line; each registers itself with
# `@register("name")`. This import is what makes it visible to the CLI.
from .breakout import DonchianBreakout  # noqa: E402
from .macd import MacdTrend  # noqa: E402
from .rsi_reversion import RsiReversion  # noqa: E402
from .scaled import ScaledStrategy  # noqa: E402
from .tsmom import Tsmom  # noqa: E402

__all__ = [
    "Strategy",
    "SmaTrend",
    "SmaTrendLongShort",
    "SmaReversion",
    "DonchianBreakout",
    "MacdTrend",
    "RsiReversion",
    "ScaledStrategy",
    "Tsmom",
    "REGISTRY",
    "register",
    "get_strategy",
    "available",
    "parameters",
    "sweep_parameter",
    "describe_registry",
]
