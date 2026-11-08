"""UNIUSDC Burst B with a 12H profile, 1m/5m confirmation and rm-c."""

from live_trading.burst_runtime import NativeBurst


class Strategy(NativeBurst):
    SYMBOL = "UNIUSDC"
    CONTRACT = "BURST-UNI-12H-B-1M5M-RMC48-LIVE-001"
    ALLOCATION_ID = CONTRACT
    MARGIN_ASSET = "USDC"
    DATABASE = "uniusdc.sqlite"
    LOOKBACK_HOURS = 12
    FLOW = (1, 5)
    RISK_MANAGEMENT = "rm-c"
    HOLD_HOURS = 48
