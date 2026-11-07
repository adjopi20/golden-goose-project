"""Release gate: runtime import closure excludes research and future outcomes."""
import ast
import json
from pathlib import Path
import subprocess
import sys
import shutil

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'model_development.models.trend_following_preny_profile_15m.legacy'
FORBIDDEN = [MODEL+'.'+name for name in (
    'strategy','audit_opportunity','evaluate_plugs','prepare_selected_entries','effort_result','replay_challenger')]


def test_workers_do_not_load_research_modules():
    code=f'import sys,json; import {MODEL}.paper_worker; import {MODEL}.lighter_worker; print(json.dumps(sorted(sys.modules)))'
    loaded=json.loads(subprocess.check_output([sys.executable,'-B','-c',code],cwd=ROOT,text=True))
    assert not set(FORBIDDEN).intersection(loaded)
    assert not any('.research.' in name or '.runs.' in name or name.startswith('notebook') for name in loaded)


def test_pure_runtime_and_core_have_no_io_dependencies():
    paths=[ROOT/'utils/engine/adapters/paper_execution.py',ROOT/'utils/closed_candle_context.py',
           ROOT/'utils/market_session.py',ROOT/'indicator/value_area.py',ROOT/'indicator/legacy_preny.py',
           ROOT/'utils/engine/contracts.py']
    paths+=list((ROOT/'model_development/models/trend_following_preny_profile_15m/legacy/runtime').glob('*.py'))
    for path in paths:
        tree=ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                imports=[node.module or '']
            elif isinstance(node,ast.Import):
                imports=[alias.name for alias in node.names]
            else: continue
            for name in imports:
                assert not any(name==bad or name.startswith(bad+'.') for bad in (
                    'utils.engine.state','utils.engine.execution','backtest_engine','requests','websockets','sqlite3','pyarrow','subprocess',*FORBIDDEN)), (path,name)


def test_research_reexports_same_functions_not_parallel_implementations():
    from model_development.models.trend_following_preny_profile_15m.legacy import strategy, audit_opportunity, effort_result, prepare_selected_entries
    from model_development.models.trend_following_preny_profile_15m.legacy.runtime import baseline, effort_result as runtime_effort, selectors
    from indicator import legacy_preny as indicators
    assert strategy.evaluate_session is baseline.evaluate_session
    assert audit_opportunity.trend_indicators_15m is indicators.trend_indicators_15m
    assert effort_result.annotate_session is runtime_effort.annotate_session
    assert prepare_selected_entries.keep is selectors.keep


def test_allowlisted_image_tree_imports_without_research(tmp_path):
    for directory in ('paper_trading','utils/engine','model_development/models/trend_following_preny_profile_15m/legacy/runtime'):
        shutil.copytree(ROOT/directory,tmp_path/directory,ignore=shutil.ignore_patterns('__pycache__'))
    for relative in ('utils/__init__.py','utils/market_session.py','indicator/__init__.py',
                     'indicator/legacy_preny.py','indicator/value_area.py',
                     'utils/engine/contracts.py'):
        dest=tmp_path/relative;dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/relative,dest)
    (tmp_path/'utils/engine/collectors/market_data').mkdir(parents=True,exist_ok=True)
    for filename in ('__init__.py','aggregation.py'):
        shutil.copy2(ROOT/'utils/engine/collectors/market_data'/filename,tmp_path/'utils/engine/collectors/market_data'/filename)
    for filename in ('paper_worker.py','lighter_worker.py','calibrate_c1_native.py'):
        shutil.copy2(ROOT/'model_development/models/trend_following_preny_profile_15m/legacy'/filename,tmp_path/'model_development/models/trend_following_preny_profile_15m/legacy'/filename)
    # Isolated cwd/sys.path: the full staged model tree must not supply missing imports.
    code=f'import sys; sys.path.insert(0,{str(tmp_path)!r}); import {MODEL}.paper_worker; import {MODEL}.lighter_worker; import {MODEL}.calibrate_c1_native'
    import os
    env=dict(os.environ)
    env.pop('PYTHONPATH',None)
    subprocess.check_call([sys.executable,'-I','-B','-c',code],cwd=tmp_path,env=env)
