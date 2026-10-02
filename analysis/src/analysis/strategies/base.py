"""What a strategy is, from the engine's point of view.

A strategy only has to answer one question: *given the bars seen so far, what
exposure do I want after this bar's close?* It never sees the future and never
touches money — sizing, commission and execution belong to `analysis.engine`.
That split is what makes a look-ahead bug a one-line violation instead of a
silent one: `targets[t]` may depend on `bars[0..t]` and nothing later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..data import Bar


class Strategy(ABC):
    """Base class for every strategy in `analysis.strategies`."""

    #: Short stable identifier, e.g. `"sma"`.
    name: str = "strategy"

    #: Parameter the CLI sweeps when `--sweep` is given without a name, e.g.
    #: `"window"` for a moving-average strategy. `None` means "no default sweep".
    sweep_param: str | None = None

    @property
    @abstractmethod
    def slug(self) -> str:
        """Identifier including parameters, e.g. `"sma200"` — used for filenames."""

    @property
    def warmup(self) -> int:
        """Bars needed before the first non-zero target can appear."""
        return 0

    @abstractmethod
    def targets(self, bars: list[Bar]) -> list[float]:
        """Desired exposure after each bar's close, one value per bar in `[-1, 1]`."""

    @abstractmethod
    def describe(self) -> str:
        """One human-readable line, printed in the report header."""

    @property
    def params(self) -> dict:
        """Parameters that define this instance, for logs and JSON."""
        return {}

    def intrabar_exits(self, bars: list[Bar]) -> list[float | None] | None:
        """Prices at which a position was closed *inside* a bar, one entry per bar.

        Only strategies that model levels — a stop-loss, a take-profit — return anything
        here, and the convention has to be stated by the strategy because OHLC bars do not
        say whether the high or the low came first. `None` (the default) means the engine
        fills every exposure change at the next open, which is what everything else does.
        """
        return None

    def __str__(self) -> str:
        return self.slug
