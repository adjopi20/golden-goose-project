"""INJUSDT Burst B with a 24H profile, 1m/5m confirmation and rm-d."""

from live_trading.burst_runtime import NativeBurst


class Strategy(NativeBurst):
    SYMBOL = "INJUSDT"
    CONTRACT = "BURST-INJ-24H-B-1M5M-RMD48-LIVE-001"
    ALLOCATION_ID = CONTRACT
    MARGIN_ASSET = "USDT"
    DATABASE = "injusdt.sqlite"
    LOOKBACK_HOURS = 24
    FLOW = (1, 5)
    RISK_MANAGEMENT = "rm-d"
    HOLD_HOURS = 48
