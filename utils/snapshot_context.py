from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class SnapshotContext:
    """Snapshot Contract for trade snapshot rendering.

    One snapshot always contains:
    - Reference Session (N-1)
      - Candles
      - Volume Profile (POC, VAH, VAL, HVN, LVN when supplied)
    - Trading Session (N)
      - Candles
      - Order Bubbles
    - Execution lifecycle
      - Entry
      - Stop loss
      - TP1 / management markers when supplied
      - Exit

    Output:
    - A single HTML snapshot chart.

    Notes:
    - The renderer must consume this context only.
    - The renderer must not perform interpretation, simulation, or execution decisions.
    """

    # Metadata
    symbol: str
    timeframe: str
    session_date: str | None

    # Reference Session (N-1)
    previous_session_start: pd.Timestamp | None
    previous_session_end: pd.Timestamp | None
    previous_session_candles: pd.DataFrame
    previous_session_profile: dict[str, Any] | None

    # Trading Session (N)
    current_session_start: pd.Timestamp | None
    current_session_end: pd.Timestamp | None
    current_session_candles: pd.DataFrame
    current_session_bubbles: pd.DataFrame

    # Execution
    executed_trades: list[Any]

    # Optional future extensions
    interpreter_states: pd.DataFrame | None = None

    # Rendering controls
    profile_overlay_start: pd.Timestamp | None = None
    profile_overlay_end: pd.Timestamp | None = None
    profile_clamp_low: float | None = None
    profile_clamp_high: float | None = None
