"""Selected HYPEUSDT Burst B policy and independent execution identity."""
from decimal import Decimal

from live_trading.live_model.burst_breakout.policy import BurstPackage, SelectedBurstAdditionExecutor


class Strategy(BurstPackage):
    SYMBOL = "HYPEUSDT"
    CONTRACT = "BURST-HYPEUSDT-16H-B-3M12M-RME48-P1-LIVE-002"
    ALLOCATION_ID = "burst_hypeusdt_16h_f_p1_initiative_add"
    MARGIN_ASSET = "USDT"
    DATABASE = "burst_hypeusdt_16h_f_p1_initiative_add.sqlite"
    LOOKBACK_HOURS = 16
    FLOW = (3, 12)
    RISK_MANAGEMENT = "rm-e"
    PATH_FILTER = ('path_p1', 0.01)
    BASE_RISK = Decimal("0.01")
    POSITION_SIZING = "initiative_1p5_add_at_2r"
    EXECUTOR = SelectedBurstAdditionExecutor
