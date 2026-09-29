"""Strategy-agnostic backtest and exit-sweep primitives."""

from .exit_policy import ExitPolicy, build_exit_policy_grid
from .results import calculate_shared_backtest_metrics, write_shared_backtest_result

__all__ = [
    "ExitPolicy",
    "build_exit_policy_grid",
    "calculate_shared_backtest_metrics",
    "write_shared_backtest_result",
]
