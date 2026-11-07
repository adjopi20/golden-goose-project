"""Clock source routing and live-model isolation are migration contracts."""
import ast
from pathlib import Path
import subprocess
import sys
import numpy as np
import pandas as pd
import pytest
from model_development.research_dependencies import clock_source
from utils.closed_candle_context import CandleContext,CandleNotReady

ROOT=Path(__file__).resolve().parents[2]
LEGACY=Path('C:/Users/adjop/OneDrive/Documents/ChatGPT/GOLDEN GOOSE PROJECT/work/binance_futures_executor')

def test_default_clock_source_and_availability_gate():
    assert clock_source(ROOT)==ROOT/'utils/closed_candle_context.py'
    bars=pd.DataFrame(dict(timestamp_ms=[0],open=[1.],high=[1.],low=[1.],close=[1.],volume=[1.],available_at_ms=[60010]))
    with pytest.raises(CandleNotReady):CandleContext(bars,as_of_ms=60000).candles('1m')
    assert len(CandleContext(bars,as_of_ms=60010).candles('1m'))==1

def test_explicit_legacy_clock_and_shadow_rejection():
    if not (LEGACY/'trading_core/candles.py').is_file():pytest.skip('Historical checkout not present')
    code=f"""from pathlib import Path
import pandas as pd
from model_development.research_dependencies import bind_clock
root=Path({str(ROOT)!r});legacy=Path({str(LEGACY)!r})
clock=bind_clock(legacy)
from utils.closed_candle_context import CandleContext
assert CandleContext is clock.CandleContext
bars=pd.DataFrame(dict(timestamp_ms=[0],open=[1.],high=[1.],low=[1.],close=[1.],volume=[1.],buy_volume=[1.],sell_volume=[0.]))
assert clock.CandleContext(bars,as_of_ms=60000).candles('1m').iloc[0]['delta']==1
try:bind_clock(root)
except ImportError:pass
else:raise AssertionError('A conflicting clock must fail')
"""
    subprocess.run([sys.executable,'-B','-c',code],cwd=ROOT,check=True)

def test_six_frozen_models_do_not_import_mutable_model_calculations():
    import json
    manifest=json.loads((ROOT/'model_development/models/legacy_frozen_candidates/release.json').read_text())
    for name in manifest['models']:
        tree=ast.parse((ROOT/'model_development/models/legacy_frozen_candidates'/f'{name}.py').read_text())
        for node in ast.walk(tree):
            modules=[]
            if isinstance(node,ast.ImportFrom):modules=[node.module or '']
            elif isinstance(node,ast.Import):modules=[a.name for a in node.names]
            assert not any(m==bad or m.startswith(bad+'.') for m in modules for bad in ('indicator','utils','trading_core','paper_trading','research','model_development'))
