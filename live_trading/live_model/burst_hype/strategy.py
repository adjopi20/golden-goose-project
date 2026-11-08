"""HYPEUSDT Burst B with a 24H profile, 3m/12m confirmation and rm-c."""

from live_trading.burst_runtime import NativeBurst


class Strategy(NativeBurst):
    SYMBOL = "HYPEUSDT"
    CONTRACT = "BURST-HYPE-24H-B-3M12M-RMC48-LIVE-001"
    ALLOCATION_ID = "hype_burst_rmc48"
    MARGIN_ASSET = "USDT"
    DATABASE = "hype.sqlite"
    LOOKBACK_HOURS = 24
    FLOW = (3, 12)
    RISK_MANAGEMENT = "rm-c"
    HOLD_HOURS = 48
