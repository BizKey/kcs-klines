"""The strategy registry: names, factories and their parameters.

Kept in its own module on purpose. A strategy module must be able to do

    from .registry import register

at import time, and importing `analysis.strategies` must import the registry
*before* it imports any strategy module — otherwise the two imports deadlock
(`ImportError: cannot import name 'register' from partially initialized module`).
Strategy modules therefore import from here, never from the package root.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable

from .base import Strategy

__all__ = [
    "REGISTRY",
    "register",
    "get_strategy",
    "available",
    "parameters",
    "sweep_parameter",
    "describe_registry",
]

#: name -> factory taking keyword arguments and returning a `Strategy`
REGISTRY: dict[str, Callable[..., Strategy]] = {}


def register(name: str) -> Callable[[Callable[..., Strategy]], Callable[..., Strategy]]:
    """Add a strategy factory to the registry under `name`."""

    def decorator(factory: Callable[..., Strategy]) -> Callable[..., Strategy]:
        if name in REGISTRY:
            raise ValueError(f"strategy {name!r} is already registered")
        REGISTRY[name] = factory
        return factory

    return decorator


def get_strategy(name: str, **params: object) -> Strategy:
    """Build a registered strategy by name; unknown names list what exists."""
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown strategy {name!r}; available: {', '.join(available())}") from None
    return factory(**params)


def available() -> list[str]:
    return sorted(REGISTRY)


def parameters(name: str) -> dict[str, object]:
    """The named parameters a strategy accepts, with their defaults.

    Introspected from the factory, so a new strategy needs no CLI plumbing. A
    `**kwargs` catch-all is ignored rather than reported as a parameter.
    """
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy {name!r}; available: {', '.join(available())}")
    signature = inspect.signature(REGISTRY[name])
    kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    return {
        parameter.name: parameter.default
        for parameter in signature.parameters.values()
        if parameter.kind in kinds and parameter.default is not inspect.Parameter.empty
    }


def sweep_parameter(name: str) -> str | None:
    """Parameter `--sweep` uses when it is given bare values, e.g. `--sweep 50,100`."""
    try:
        instance = get_strategy(name)
    except TypeError:  # parameters without defaults: nothing to build an instance from
        return None
    return instance.sweep_param


def describe_registry() -> list[str]:
    """Two lines per strategy: its name and parameters, then what it does."""
    lines: list[str] = []
    for name in available():
        params = ", ".join(f"{key}={value!r}" for key, value in parameters(name).items())
        summary = (REGISTRY[name].__doc__ or "").strip().splitlines()
        sweep = sweep_parameter(name)
        sweep_text = f"   [--sweep {sweep}]" if sweep else ""
        lines.append(f"  {name}({params}){sweep_text}")
        if summary:
            lines.append(f"      {summary[0]}")
    return lines
