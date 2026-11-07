"""Offline parity and release isolation; no real endpoints or operational DBs."""
import ast
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date,timedelta
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from live_trading import live_model


def load_file(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module


class FrozenModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest=live_model.verify_release()
        cls.models={name:live_model.load_model(name) for name in cls.manifest['models']}

    def preny_inputs(self,symbol='ETHUSDC'):
        m=self.models['pre_ny_eth'];day=date(2026,8,19);start=m.clock_ms(day,9);bar=m.BAR_MS
        profile=dict(session_day=str(day),profile_window='pre_ny',profile_start_timestamp_ms=m.clock_ms(day,1),
            profile_end_timestamp_ms=start,val=90.,poc=100.,vah=110.)
        bars=[dict(open_timestamp_ms=start+i*bar,close_timestamp_ms=start+(i+1)*bar,
            open=110.+i,low=109.+i,high=112.+i,close=111.+i,buy_volume=10.,sell_volume=2.) for i in range(2)]
        refs=dict(through_session_day=str(day-timedelta(days=1)),slots={str(i):dict(prior_sessions=20,
            volume=20.,buy_volume=5.,sell_volume=5.,body=.5) for i in range(12)})
        return dict(symbol=symbol,profile=profile,bars=bars,as_of_ms=start+2*bar,references=refs,
            atr_before={start+bar:2.},trend=dict(status='ready',end_ms=start+2*bar,price_vs_ema200_pct=.6,adx14=31.),
            c1_calibration=dict(valid_from='2026-08-01',valid_to_exclusive='2026-09-01',trained_through='2026-07-28',
                thresholds=dict(directional_delta_imbalance=.5,directional_result_atr=.5)))

    def test_six_models_and_shared_numerical_source_parity(self):
        self.assertEqual(len(self.models),6)
        for source,target in self.manifest['shared_infrastructure_origins'].items():
            if source=='live_engine/order_manager.py':continue
            relocated = {'trading_core/paper_execution.py': 'utils/engine/adapters/paper_execution.py', 'trading_core/contracts.py': 'utils/engine/contracts.py', 'live_engine/state_store.py': 'utils/engine/state/base.py', 'models/bitcoin_onchain_cycles/live_mvrv/storage.py': 'model_development/models/bitcoin_onchain_cycles/live_mvrv/storage.py', 'live_engine/adapters/binance.py': 'utils/engine/adapters/binance_public.py'}.get(source, source)
            # Historical origin names stay immutable; moved bytes must match the original hash.
            self.assertEqual(hashlib.sha256((ROOT/relocated).read_bytes()).hexdigest(),
                             self.manifest['legacy_source_sha256'][source])
            self.assertEqual(ast.dump(ast.parse((ROOT/relocated).read_text(encoding='utf-8-sig'))),
                ast.dump(ast.parse((ROOT/target).read_text(encoding='utf-8-sig'))))
        old=ast.parse((ROOT/'model_development/models/bitcoin_onchain_cycles/live_mvrv/engine.py').read_text())
        new=ast.parse((ROOT/'live_trading/execution/frozen_v1/mvrv.py').read_text())
        for name in ('amount','fill'):
            get=lambda tree:ast.dump(next(n for n in tree.body if getattr(n,'name',None)==name))
            self.assertEqual(get(old),get(new))

    def test_preny_entry_sizing_rejection_and_bad_input_parity(self):
        from model_development.models.trend_following_preny_profile_15m.legacy.runtime import evaluator as old
        for name,symbol in (('pre_ny_eth','ETHUSDC'),('pre_ny_bnb','BNBUSDC'),('pre_ny_hype','HYPEUSDT')):
            model=self.models[name]
            cases=[self.preny_inputs(symbol)]
            low=deepcopy(cases[0]);low['trend']['price_vs_ema200_pct']=.4;low['trend']['adx14']=29.;cases.append(low)
            wait=deepcopy(cases[0]);wait['bars']=wait['bars'][:1];wait['as_of_ms']-=model.BAR_MS;cases.append(wait)
            reject=deepcopy(cases[0]);reject['c1_calibration']['thresholds']['directional_result_atr']=2.;cases.append(reject)
            for inputs in cases:
                with self.subTest(model=name,asof=inputs['as_of_ms']):
                    self.assertEqual(old.evaluate_completed_bar(**deepcopy(inputs)),model.evaluate_completed_bar(**deepcopy(inputs)))
            bad=deepcopy(cases[0]);bad['bars'].append({**bad['bars'][-1],
                'open_timestamp_ms':bad['as_of_ms'],'close_timestamp_ms':bad['as_of_ms']+model.BAR_MS})
            for evaluate in (old.evaluate_completed_bar,model.evaluate_completed_bar):
                with self.assertRaisesRegex(ValueError,'Future bar'):evaluate(**deepcopy(bad))
            other='ETHUSDC' if symbol!='ETHUSDC' else 'BNBUSDC'
            with self.assertRaises(ValueError):model.evaluate_completed_bar(**self.preny_inputs(other))

    def test_preny_profiles_preparation_indicators_and_deadlines_match(self):
        from model_development.models.trend_following_preny_profile_15m.legacy.runtime import preparation as old
        from indicator import legacy_preny as indicators
        day=date(2026,8,19);model=self.models['pre_ny_eth'];asof=model.clock_ms(day,9,30)
        stamps=np.arange(asof-26*86400000,asof,900000,dtype='int64');t=np.arange(len(stamps));close=105+np.sin(t/9)
        minutes=pd.DataFrame(dict(timestamp_ms=stamps,open=close-.1,high=close+.5,low=close-.5,close=close,
            volume=20.+np.cos(t),delta=3.*np.sin(t)))
        prepared=old.prepare_context(minutes,day,asof)
        fixtures=json.loads((ROOT/'tests/fixtures/preny_profile_geometry_v1.json').read_text())
        for name in ('pre_ny_eth','pre_ny_bnb','pre_ny_hype'):
            m=self.models[name]
            self.assertEqual(m.prepare_context(minutes,day,asof),prepared)
            pd.testing.assert_frame_equal(m.hourly_indicators(minutes),indicators.hourly_indicators(minutes))
            for case in fixtures:
                self.assertEqual(m.make_profile(date.fromisoformat(case['expected']['session_day']),dict(case['prices'])),case['expected'])
            signal=dict(feature_as_of_ms=asof,entry_eligible_timestamp_ms=asof,entry_deadline_timestamp_ms=asof+900000)
            self.assertEqual(m.paper_activation_signal(signal,asof+20000),old.paper_activation_signal(signal,asof+20000))
            with self.assertRaisesRegex(m.NotReady,'deadline'):m.paper_activation_signal(signal,asof+300001)

    def test_preny_observer_state_lock_and_restart_parity(self):
        from model_development.models.trend_following_preny_profile_15m.legacy.runtime.observer import SessionObserver
        inputs=self.preny_inputs();m=self.models['pre_ny_eth'];start=m.clock_ms(date(2026,8,19),9)
        for observer in (SessionObserver,m.SessionObserver):
            db=sqlite3.connect(':memory:')
            try:
                db.executescript('CREATE TABLE profiles(symbol TEXT,session TEXT,payload TEXT);CREATE TABLE evaluations(symbol TEXT,asof INTEGER,payload TEXT);')
                db.execute('INSERT INTO profiles VALUES(?,?,?)',('ETHUSDC',inputs['profile']['session_day'],json.dumps(inputs['profile'])))
                state=dict(coverage_start_ms=m.clock_ms(date(2026,8,19),1))
                with redirect_stdout(io.StringIO()):
                    result=observer(SimpleNamespace(db=db),now_ms=lambda:start).boundary('ETHUSDC',start,state)
                    second=observer(SimpleNamespace(db=db),now_ms=lambda:start).boundary('ETHUSDC',start,state)
                self.assertEqual(result['state'],'PROFILE_READY');self.assertIsNone(second)
                rows=db.execute('SELECT * FROM evaluations').fetchall()
                if observer is SessionObserver:expected=rows
                else:self.assertEqual(rows,expected)
            finally:db.close()

    def test_preny_pinned_account_config_and_partial_exit_match_source(self):
        from utils.engine.execution.paper_manager import PaperOrderManager as OriginalManager
        from utils.engine.state.base import StateStore as OriginalStore
        from utils.engine.adapters.paper_execution import Config as OriginalConfig
        from live_trading.execution.frozen_v1.order_manager import PaperOrderManager
        from live_trading.state.frozen_v1.state_store import StateStore
        from live_trading.execution.frozen_v1.paper_execution import Config
        from model_development.models.trend_following_preny_profile_15m.legacy.runtime.evaluator import evaluate_completed_bar
        for name in ('pre_ny_eth','pre_ny_bnb','pre_ny_hype'):
            m=self.models[name]
            for venue in ('binance','lighter'):
                source=json.loads((ROOT/f'model_development/models/trend_following_preny_profile_15m/legacy/deploy/{venue}.paper.yaml').read_text())
                expected=next(a for a in source['accounts'] if a['symbol']==m.SYMBOL)
                self.assertEqual(m.account_configuration(venue),expected)
            with self.subTest(model=name),tempfile.TemporaryDirectory() as temp:
                base=Path(temp);source_store=OriginalStore(base/'original.sqlite','binance');store=StateStore(base/'candidate.sqlite','binance')
                try:
                    inputs=self.preny_inputs(m.SYMBOL);result=m.evaluate_completed_bar(**inputs)
                    cfg=m.account_configuration('binance');account=cfg['id']
                    pairs=[(OriginalManager(source_store),OriginalConfig),(PaperOrderManager(store),Config)]
                    final=[]
                    for manager,config_class in pairs:
                        manager.register(account,m.SYMBOL,config_class(**cfg['execution']),allowed_risks=cfg['allowed_risks'],contract=cfg['contract'])
                        evaluated=evaluate_completed_bar(**deepcopy(inputs)) if config_class is OriginalConfig else deepcopy(result)
                        ready=inputs['as_of_ms']+20000;signal=m.paper_activation_signal(evaluated['signal'],ready)
                        manager.submit(account,signal,snapshot=evaluated['snapshot'],risk_fraction=evaluated['risk_fraction'],selected=evaluated['selected'])
                        entry=signal['entry_reference'];risk=entry-signal['stop'];target=entry+cfg['execution']['tp1_r']*risk
                        for index,price in enumerate((entry,target,target+.1,signal['stop'],signal['stop']-.1),1):
                            manager.ticks(account,[dict(symbol=m.SYMBOL,agg_trade_id=index,timestamp_ms=ready+index,
                                price=float(price),quantity=1.,buy=True)])
                        p=manager._load(account)[1]
                        journal=[(r['kind'],r['payload']) for r in manager.store.db.execute("SELECT kind,payload FROM journal WHERE kind IN ('fill','trade') ORDER BY id")]
                        final.append((p.equity,p.position,journal))
                    self.assertEqual(final[0],final[1])
                    fills=[json.loads(payload) for kind,payload in final[1][2] if kind=='fill']
                    self.assertGreaterEqual(len(fills),3)
                finally:source_store.close();store.close()

    def test_mvrv_indicator_threshold_causality_and_zone_parity(self):
        from model_development.models.bitcoin_onchain_cycles.live_mvrv import data,engine,signal_monitor
        n=1700;t=np.arange(n);cap=1000+np.cumsum(1+.3*np.sin(t/17));ratio=1.4+.6*np.sin(t/41)
        raw=pd.DataFrame(dict(time=pd.date_range('2010-07-18',periods=n).strftime('%Y-%m-%d'),
            PriceUSD=100+t/20,CapMrktCurUSD=cap,CapMVRVCur=ratio))
        expected=data.prepare(raw,engine.configuration())
        for name in ('btc_mvrv_fixed','btc_mvrv_p5_p95'):
            m=self.models[name];actual=m.prepare(raw);columns=['date','price','z',m.MODEL_ID+'_entry',m.MODEL_ID+'_exit']
            pd.testing.assert_frame_equal(actual,expected[columns])
            altered=raw.copy();altered.loc[n-2:,'CapMrktCurUSD']*=2
            pd.testing.assert_frame_equal(m.prepare(altered).iloc[:-2],actual.iloc[:-2])
            wanted=next(r for r in signal_monitor.zones(expected.iloc[-1],engine.configuration()) if r['model']==m.MODEL_ID)
            self.assertEqual(m.zone(actual.iloc[-1]),wanted)
            for evaluated in ('2026-01-02T01:00:00Z','2026-01-04T01:00:00Z'):
                self.assertEqual(m.next_close('2026-01-01',evaluated),data.next_close('2026-01-01',evaluated))
            row=actual.iloc[-1].to_dict();row[m.MODEL_ID+'_entry']=np.nan
            self.assertEqual(m.decide(row,cash='500',btc='0'),'DATA_NOT_READY')

    def test_mvrv_paper_ledger_two_independent_candidates_match_source_accounts(self):
        from model_development.models.bitcoin_onchain_cycles.live_mvrv import engine,storage
        from live_trading.state.frozen_v1 import mvrv as frozen_storage
        for mode in ('paper','alerts'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as temp:
                base=Path(temp);legacy=storage.connect(base/'original.sqlite')
                candidates={name:frozen_storage.connect(base/(name+'.sqlite')) for name in ('btc_mvrv_fixed','btc_mvrv_p5_p95')}
                try:
                    config=engine.configuration();when='2026-01-01T00:00:00Z'
                    with legacy:engine.initialize(legacy,config,'1000',mode,when)
                    for name,c in candidates.items():
                        with c:self.models[name].initialize(c,'1000',mode,when)
                    for i,z in enumerate((-1.,-1.,8.,8.,-1.,-1.,8.,8.)):
                        day=str(date(2026,1,1)+timedelta(days=i));when=(pd.Timestamp(day,tz='UTC')+pd.Timedelta(days=1,hours=1)).isoformat()
                        row=dict(date=day,price=100.+i,z=z,fixed_0_7_entry=0.,fixed_0_7_exit=7.,
                            percentile_4y_p5_p95_entry=-.5,percentile_4y_p5_p95_exit=1.5)
                        with legacy:engine.apply_day(legacy,row,config,when,mode,historical=True)
                        for name,c in candidates.items():
                            with c:self.models[name].apply_day(c,row,when,mode,historical=True)
                    for name,c in candidates.items():
                        model=self.models[name];key=model.MODEL_ID
                        self.assertEqual(c.execute('SELECT count(*) FROM accounts').fetchone()[0],1)
                        for table,column in (('accounts','id'),('decisions','account'),('actions','account'),('marks','account')):
                            query=f'SELECT * FROM {table} WHERE {column}=? ORDER BY rowid'
                            self.assertEqual([tuple(r) for r in c.execute(query,(key,))],[tuple(r) for r in legacy.execute(query,(key,))])
                        query='SELECT f.* FROM fills f JOIN actions a ON a.id=f.action_id WHERE a.account=? ORDER BY f.rowid'
                        self.assertEqual([tuple(r) for r in c.execute(query,(key,))],[tuple(r) for r in legacy.execute(query,(key,))])
                        before=c.execute('SELECT count(*) FROM decisions').fetchone()[0]
                        with c:self.assertFalse(model.apply_day(c,row,when,mode,historical=True))
                        self.assertEqual(c.execute('SELECT count(*) FROM decisions').fetchone()[0],before)
                finally:
                    legacy.close()
                    for c in candidates.values():c.close()

    def test_ltc_existing_behavior_suite_on_frozen_model(self):
        tests=load_file('_frozen_ltc_existing_tests',ROOT/'model_development/models/mean_reversion_by_volume_profile/paper_v33/test_worker.py')
        tests.w=self.models['volume_profile_reversion']
        names=[name for name in unittest.defaultTestLoader.getTestCaseNames(tests.PaperTests) if 'history' not in name]
        suite=unittest.TestSuite(tests.PaperTests(name) for name in names)
        result=unittest.TestResult();suite.run(result)
        self.assertEqual(result.testsRun,14)
        self.assertTrue(result.wasSuccessful(),repr(result.errors+result.failures))

    def test_mvrv_delay_expiry_and_legacy_database_are_not_silently_migrated(self):
        from model_development.models.bitcoin_onchain_cycles.live_mvrv import engine,storage
        from live_trading.state.frozen_v1 import mvrv as frozen_storage
        for name in ('btc_mvrv_fixed','btc_mvrv_p5_p95'):
            with self.subTest(model=name),tempfile.TemporaryDirectory() as temp:
                base=Path(temp);m=self.models[name]
                original=storage.connect(base/'original.sqlite');candidate=frozen_storage.connect(base/'candidate.sqlite')
                try:
                    config=engine.configuration();when='2026-01-01T00:00:00Z'
                    with original:engine.initialize(original,config,'1000','paper',when)
                    with candidate:m.initialize(candidate,'1000','paper',when)
                    with self.assertRaisesRegex(ValueError,'configuration changed'):
                        m.initialize(original,'1000','paper',when)
                    self.assertEqual(original.execute('SELECT count(*) FROM accounts').fetchone()[0],2)
                    for day,z in (('2026-01-01',-1.),('2026-01-04',1.)):
                        row=dict(date=day,price=100.,z=z,fixed_0_7_entry=0.,fixed_0_7_exit=7.,
                            percentile_4y_p5_p95_entry=-.5,percentile_4y_p5_p95_exit=1.5)
                        when=(pd.Timestamp(day,tz='UTC')+pd.Timedelta(days=1,hours=1)).isoformat()
                        with original:engine.apply_day(original,row,config,when,'paper')
                        with candidate:m.apply_day(candidate,row,when,'paper')
                    self.assertEqual(candidate.execute('SELECT count(*) FROM fills').fetchone()[0],0)
                    self.assertEqual(candidate.execute('SELECT status FROM actions').fetchone()[0],'expired')
                    query='SELECT * FROM actions WHERE account=? ORDER BY rowid'
                    self.assertEqual([tuple(r) for r in candidate.execute(query,(m.MODEL_ID,))],
                        [tuple(r) for r in original.execute(query,(m.MODEL_ID,))])
                    ctime='2026-01-04T12:00:00Z'
                    with self.assertRaisesRegex(ValueError,'not closed yet'):m.apply_day(candidate,row,ctime,'paper')
                finally:original.close();candidate.close()

    def test_mutable_research_calculation_changes_do_not_reach_models(self):
        from indicator import legacy_preny as indicators
        from indicator import volume_profile
        from model_development.models.bitcoin_onchain_cycles.live_mvrv import data
        from model_development.models.trend_following_preny_profile_15m.legacy.runtime import evaluator
        with (patch.object(evaluator,'evaluate_completed_bar',side_effect=AssertionError('mutable evaluator')),
             patch.object(indicators,'hourly_indicators',side_effect=AssertionError('mutable indicator')),
             patch.object(volume_profile,'build_basic_volume_profile',side_effect=AssertionError('mutable profile')),
             patch.object(data,'prepare',side_effect=AssertionError('mutable MVRV'))):
            self.assertTrue(self.models['pre_ny_eth'].evaluate_completed_bar(**self.preny_inputs())['selected'])
            profile=self.models['volume_profile_reversion'].build_basic_volume_profile(pd.DataFrame(dict(price=[1.,2.,3.],qty=[1.,3.,1.])),n_bins=3)
            self.assertGreater(profile['total_volume'],0)
            raw=pd.DataFrame(dict(time=['2010-07-18','2010-07-19'],PriceUSD=[100.,101.],
                CapMrktCurUSD=[1000.,1100.],CapMVRVCur=[1.,1.1]))
            self.assertEqual(len(self.models['btc_mvrv_fixed'].prepare(raw)),1)
            live_model.verify_release()

    def test_release_guard_fails_for_changed_source_or_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=root/'live_trading/live_model';folder.mkdir(parents=True)
            file=root/'shared.py';file.write_text('VERSION=1\n')
            manifest=deepcopy(self.manifest);manifest['files']={'shared.py':hashlib.sha256(file.read_bytes()).hexdigest()}
            (folder/'release.json').write_text(json.dumps(manifest))
            with patch.object(live_model,'ROOT',root),patch.object(live_model,'__file__',str(folder/'__init__.py')):
                live_model.verify_release();file.write_text('VERSION=2\n')
                with self.assertRaisesRegex(RuntimeError,'source changed'):live_model.verify_release()
                file.write_text('VERSION=1\n')
                with patch.object(live_model.metadata,'version',return_value='different'):
                    with self.assertRaisesRegex(RuntimeError,'dependency version'):live_model.verify_release()

    def test_import_is_offline_and_excludes_mutable_model_indicator_packages(self):
        code=f'''import sys,json,socket,sqlite3
sys.path.insert(0,{str(ROOT)!r})
from unittest.mock import patch
with patch.object(socket,'create_connection',side_effect=AssertionError('network')),patch.object(sqlite3,'connect',side_effect=AssertionError('database')):
 from live_trading.live_model import load_model,verify_release
 for name in verify_release()['models']:load_model(name)
assert not any(n=='indicator' or n.startswith(('indicator.','model_development.models.','research.','backtest_engine.','utils.engine.','trading_core.')) for n in sys.modules)
print('offline imports isolated')
'''
        result=subprocess.run([sys.executable,'-I','-B','-W','error','-c',code],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
