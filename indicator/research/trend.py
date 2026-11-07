"""Optional research composition; indicators remain independently reusable."""
import pandas as pd
from indicator.research._common import TECHNICAL_CONVENTION
from indicator.research.moving_average import moving_average
from indicator.research.atr import average_true_range, candle_true_range
from indicator.research.adx import directional_movement_index

def technical_indicators(bars: pd.DataFrame, *, ma_window: int=20, ma_method: str='ema', atr_window: int=14, adx_window: int=14) -> pd.DataFrame:
    """CandleContext IndicatorSpec callback; close MA plus Wilder ATR and DMI.

    Formulas are pure; CandleContext supplies the chosen completed timeframe,
    rejects missing candle coverage and blocks model evaluation during warmup.
    """
    if not bars.columns.is_unique or any((col not in bars for col in ('high', 'low', 'close'))):
        raise ValueError('Technical indicators require unique high/low/close columns')
    if 'symbol' in bars and (bars.symbol.isna().any() or bars.symbol.nunique() > 1):
        raise ValueError('Technical indicators require one instrument')
    output = directional_movement_index(bars.high, bars.low, bars.close, adx_window)
    output['ma'] = moving_average(bars.close, ma_window, method=ma_method)
    output['atr'] = average_true_range(bars.high, bars.low, bars.close, atr_window)
    output['convention'] = TECHNICAL_CONVENTION
    return output
