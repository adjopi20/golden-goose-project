"""Selected INJUSDT Burst B policy and independent execution identity."""
from decimal import Decimal

from live_trading.live_model.burst_breakout.policy import BurstPackage


class Strategy(BurstPackage):
    SYMBOL = "INJUSDT"
    CONTRACT = "BURST-INJUSDT-24H-B-5M10M-RME48-P15-LIVE-002"
    ALLOCATION_ID = "burst_injusdt_24h_f_p15_initiative"
    MARGIN_ASSET = "USDT"
    DATABASE = "burst_injusdt_24h_f_p15_initiative.sqlite"
    LOOKBACK_HOURS = 24
    FLOW = (5, 10)
    RISK_MANAGEMENT = "rm-e"
    PATH_FILTER = ('path_p15', 0.15)
    BASE_RISK = Decimal("0.009")
    POSITION_SIZING = "initiative_1p5"
