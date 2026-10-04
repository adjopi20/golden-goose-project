"""Release gate: runtime import closure excludes research and future outcomes."""
import ast
import json
from pathlib import Path
import subprocess
import sys
import shutil

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'models.trend_following_preny_profile_15m'
FORBIDDEN = [MODEL+'.'+name for name in (
    'strategy','audit_opportunity','evaluate_plugs','prepare_selected_entries','effort_result','replay_challenger')]


def test_workers_do_not_load_research_modules():
    code=f'import sys,json; import {MODEL}.paper_worker; import {MODEL}.lighter_worker; print(json.dumps(sorted(sys.modules)))'
    loaded=json.loads(subprocess.check_output([sys.executable,'-B','-c',code],cwd=ROOT,text=True))
    assert not set(FORBIDDEN).intersection(loaded)
    assert not any('.research.' in name or '.runs.' in name or name.startswith('notebook') for name in loaded)


def test_pure_runtime_and_core_have_no_io_dependencies():
    roots=[ROOT/'trading_core',ROOT/'models/trend_following_preny_profile_15m/runtime']
    for directory in roots:
        for path in directory.glob('*.py'):
            tree=ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom):
                    imports=[node.module or '']
                elif isinstance(node,ast.Import):
                    imports=[alias.name for alias in node.names]
                else: continue
                for name in imports:
                    assert not any(name==bad or name.startswith(bad+'.') for bad in (
                        'live_engine','backtest_engine','requests','websockets','sqlite3','pyarrow','subprocess',*FORBIDDEN)), (path,name)


def test_research_reexports_same_functions_not_parallel_implementations():
    from models.trend_following_preny_profile_15m import strategy, audit_opportunity, effort_result, prepare_selected_entries
    from models.trend_following_preny_profile_15m.runtime import baseline, effort_result as runtime_effort, selectors
    from trading_core import indicators
    assert strategy.evaluate_session is baseline.evaluate_session
    assert audit_opportunity.trend_indicators_15m is indicators.trend_indicators_15m
    assert effort_result.annotate_session is runtime_effort.annotate_session
    assert prepare_selected_entries.keep is selectors.keep


def test_allowlisted_image_tree_imports_without_research(tmp_path):
    for directory in ('trading_core','live_engine','models/trend_following_preny_profile_15m/runtime'):
        shutil.copytree(ROOT/directory,tmp_path/directory,ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'market_data').mkdir()
    for filename in ('__init__.py','aggregation.py'):
        shutil.copy2(ROOT/'market_data'/filename,tmp_path/'market_data'/filename)
    for filename in ('__init__.py','replay_aggtrades.py','results.py','exit_policy.py'):
        (tmp_path/'backtest_engine').mkdir(exist_ok=True)
        shutil.copy2(ROOT/'backtest_engine'/filename,tmp_path/'backtest_engine'/filename)
    for filename in ('paper_worker.py','lighter_worker.py','calibrate_c1_native.py'):
        shutil.copy2(ROOT/'models/trend_following_preny_profile_15m'/filename,tmp_path/'models/trend_following_preny_profile_15m'/filename)
    # Isolated cwd/sys.path: the full staged model tree must not supply missing imports.
    code=f'import sys; sys.path.insert(0,{str(tmp_path)!r}); import {MODEL}.paper_worker; import {MODEL}.lighter_worker; import {MODEL}.calibrate_c1_native'
    import os
    env=dict(os.environ)
    env.pop('PYTHONPATH',None)
    subprocess.check_call([sys.executable,'-I','-B','-c',code],cwd=tmp_path,env=env)
