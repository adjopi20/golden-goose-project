"""UNIUSDC Quiet Breakout with the single +2R position addition."""

from live_trading.live_model.quiet_breakout.hypeusdt.strategy import QuietBreakout, run_worker


class Strategy(QuietBreakout):
    PACKAGE_SOURCE = __file__
    VARIANT_FILE = "model_config.json"
    SYMBOL = "UNIUSDC"
    MARGIN_ASSET = "USDC"
    VARIANT = "stable"
    CONTRACT = "QUIET-UNI-STABLE-P85-P5-INIT15-RME48-ADD-LIVE-001"
    ALLOCATION_ID = "quiet_breakout_uniusdc_stable_position_addition"
    DATABASE = "quiet_breakout_uniusdc_stable_position_addition.sqlite"
    LOOKBACK_HOURS = 16
    FLOW = (1, 5)
    RISK_MANAGEMENT = "rm-e"
    RISK_P85 = True
    POSITION_ADDITION = True


def main(argv=None):
    return run_worker({Strategy.VARIANT: Strategy}, Strategy.MARGIN_ASSET, argv)


if __name__ == "__main__":
    raise SystemExit(main())
