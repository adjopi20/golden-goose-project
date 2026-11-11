"""Selected UNIUSDC Burst B policy and independent execution identity."""
from decimal import Decimal

from live_trading.live_model.burst_breakout.policy import BurstPackage


class Strategy(BurstPackage):
    SYMBOL = "UNIUSDC"
    CONTRACT = "BURST-UNIUSDC-12H-B-1M5M-RMA48-P10-LIVE-002"
    ALLOCATION_ID = "burst_uniusdc_12h_f_p10_initiative"
    MARGIN_ASSET = "USDC"
    DATABASE = "burst_uniusdc_12h_f_p10_initiative.sqlite"
    LOOKBACK_HOURS = 12
    FLOW = (1, 5)
    RISK_MANAGEMENT = "rm-c"
    PATH_FILTER = ('path_p10', 0.1)
    BASE_RISK = Decimal("0.01")
    POSITION_SIZING = "initiative_1p5"
