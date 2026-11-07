from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from model_development.phase_3.outcomes import parser,run,monte_carlo
from model_development.phase_3.run_input import load_run,digest
from model_development.phase_3 import evaluate
from model_development.phase_3.kelly import kelly_summary
from model_development.phase_3.monte_carlo import resample_paths,path_stats
from model_development.phase_3.monte_carlo import block_indices

DAY=86400000
ORIGIN=int(datetime(2026,1,1,tzinfo=timezone.utc).timestamp()*1000)


class Phase3Run(unittest.TestCase):
    def fixture(self,base,*,phase=2,accounts=1,same_day=False,zero=False):
        source=base/'source';source.mkdir()
        rows=[];declared=[]
        for a in range(accounts):
            equity=1000.;identity=dict(model_id='fixture',instrument='AAAUSDT',configuration_id='cfg',
                submodel_id=str(a),portfolio_id=f'p{a}' if phase==2 else None)
            for i,r in enumerate([1.]*6+[-1.]*4):
                entry=ORIGIN+(0 if same_day else i*DAY)+1000+i*10;exit=entry+1
                gross=r*10;fees=1.;net=gross-fees
                rows.append(dict(identity,record_type='closed_trade',trade_id=f'p{a}:{i}',setup_id=f'setup:{i}',
                    direction='long',entry_timestamp_ms=entry,exit_timestamp_ms=exit,
                    feature_as_of_ms=entry-1,feature_available_at_ms=entry-1,
                    decision_timestamp_ms=entry,entry_eligible_timestamp_ms=entry,
                    entry_agg_trade_id=2*i+1,exit_agg_trade_id=2*i+2,entry_price=100.,exit_price=100.+gross,
                    stop=90.,quantity=1.,initial_risk=10.,gross_pnl=gross,fees=fees,net_pnl=net,gross_R=r,net_R=net/10,
                    equity_before_entry=equity if phase==2 else None,equity_after_exit=equity+net if phase==2 else None))
                equity+=net
            declared.append(dict(identity,status='complete',closed_trades=0 if zero else 10,
                metrics=dict(final_equity=1000. if zero else equity)))
        table=pa.Table.from_pylist(rows)
        if zero:table=table.slice(0,0)
        pq.write_table(table,source/'trades.parquet')
        end=ORIGIN+(1 if same_day else 10)*DAY
        meta=dict(status='complete',model_id='fixture',instrument='AAAUSDT',configuration_id='cfg',
            prior_inspection='development',iteration_id='ITER-TEST',checkpoint_id=None,pnl_unit='USDT')
        if phase==2:
            meta.update(phase='backtest',jobs=declared,closed_trades=table.num_rows,
                config=dict(initial_equity=1000.),cache=dict(source_start_ms=ORIGIN,source_end_ms=end-1))
        else:
            meta.update(protocol='global_observe_v3',fixed_risk=10.,start_ms=ORIGIN,end_ms=end,
                counts=dict(closed_trades=table.num_rows))
        (source/'run.json').write_text(json.dumps(meta))
        return source

    def args(self,base,tasks=('kelly','monte-carlo'),output='evaluation',extra=()):
        return parser().parse_args(['--task',*tasks,'--run',str(base/'source'),'--output',str(base/output),
            '--resamples','33','--simulations','31','--block-days','2','--seed','7',*extra])

    def test_explicit_selected_run_gross_net_source_immutable_and_one_output(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base)
            hashes={p.name:digest(p) for p in source.iterdir()}
            with patch('model_development.phase_2.backtest.main',side_effect=AssertionError('No automatic Phase 2')):
                result=run(self.args(base))
            self.assertEqual(result['status'],'complete')
            self.assertAlmostEqual(result['results']['kelly']['gross']['full_kelly'],.2,places=8)
            self.assertAlmostEqual(result['results']['kelly']['net']['full_kelly'],.1/.99,places=8)
            self.assertEqual(result['source_accounting']['gross_pnl'],20.)
            self.assertEqual(result['source_accounting']['fees'],10.)
            self.assertEqual(result['source_accounting']['net_pnl'],10.)
            self.assertEqual({p.name for p in (base/'evaluation').iterdir()},{'run.json'})
            self.assertEqual(hashes,{p.name:digest(p) for p in source.iterdir()})
            with self.assertRaises(FileExistsError):run(self.args(base))

    def test_account_selection_required_and_failed_source_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base,accounts=2)
            with self.assertRaisesRegex(ValueError,'exactly one'):run(self.args(base))
            self.assertFalse((base/'evaluation').exists())
            result=run(self.args(base,extra=['--portfolio','p1']))
            self.assertEqual(result['source_accounting']['closed_trades'],10)
            self.assertEqual(result['source_accounting']['excluded_other_account_trades'],10)
            metadata=json.loads((source/'run.json').read_text());metadata['status']='failed'
            (source/'run.json').write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError,'complete'):run(self.args(base,output='bad'))

    def test_multiple_trades_same_day_compound_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);self.fixture(base,same_day=True)
            result=run(self.args(base))
            self.assertEqual(result['calendar_days'],1)
            mc=result['results']['monte-carlo']['estimates']
            self.assertAlmostEqual(mc['equity_p05'],1010.)
            self.assertAlmostEqual(mc['equity_p95'],1010.)

    def test_trade_shuffle_500_paths_same_day_source_immutable_and_no_bootstrap(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base,same_day=True)
            hashes={p.name:digest(p) for p in source.iterdir()}
            args=self.args(base,tasks=('monte-carlo',),extra=['--mc-method','trade-shuffle','--simulations','500'])
            with patch('model_development.phase_3.outcomes.resample_paths',side_effect=AssertionError('No bootstrap in shuffle')), \
                 patch('model_development.phase_2.backtest.main',side_effect=AssertionError('No market replay')):
                result=run(args)
            mc=result['results']['monte-carlo'];paths=np.asarray(mc['equity_paths'])
            self.assertEqual(paths.shape,(500,11))
            self.assertEqual(mc['configuration']['horizon_trades'],10)
            self.assertIsNone(mc['configuration']['block_days'])
            self.assertIsNone(mc['configuration']['horizon_days'])
            self.assertEqual(len(mc['source_trade_ids']),len(set(mc['source_trade_ids'])))
            self.assertEqual(len(mc['source_trade_ids']),10)
            returns=np.asarray(mc['trade_net_returns'])
            np.testing.assert_allclose(np.sort(paths[:,1:]/paths[:,:-1]-1,axis=1),
                                       np.tile(np.sort(returns),(500,1)),atol=1e-14)
            np.testing.assert_allclose(paths[:,-1],1010.,atol=1e-9)
            np.testing.assert_allclose(mc['equity_bands']['mean'],paths.mean(axis=0))
            np.testing.assert_allclose(mc['equity_bands']['p05'],np.quantile(paths,.05,axis=0))
            self.assertEqual(mc['estimates']['frequency_insolvent'],0.)
            self.assertGreater(mc['estimates']['dd_p95'],0.)
            self.assertGreater(mc['estimates']['frequency_ever_below_initial'],0.)
            self.assertEqual(len(mc['path_statistics']['max_drawdown']),500)
            self.assertEqual(result['source_accounting']['fees'],10.)
            self.assertEqual({p.name for p in (base/'evaluation').iterdir()},{'run.json'})
            self.assertEqual(hashes,{p.name:digest(p) for p in source.iterdir()})
            saved=json.loads((base/'evaluation/run.json').read_text())
            self.assertEqual(saved['results']['monte-carlo']['equity_paths'],mc['equity_paths'])

    def test_trade_shuffle_public_dispatch_and_zero_sample(self):
        for zero in (False,True):
            with self.subTest(zero=zero),tempfile.TemporaryDirectory() as temp:
                base=Path(temp);self.fixture(base,zero=zero)
                with patch('builtins.print'):
                    code=evaluate.main(['--task','monte-carlo','--run',str(base/'source'),
                        '--output',str(base/'evaluation'),'--mc-method','trade-shuffle',
                        '--simulations','7','--seed','19'])
                self.assertEqual(code,0)
                result=json.loads((base/'evaluation/run.json').read_text())
                mc=result['results']['monte-carlo']
                self.assertEqual(mc['configuration']['method'],'trade-shuffle')
                if zero:self.assertIsNone(mc['estimates'])
                else:self.assertEqual(len(mc['equity_paths']),7)

    def test_selected_scenario_reuses_stress_math_for_shuffle_and_net_kelly(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base)
            config=base/'scenarios.json';config.write_text(json.dumps({'scenarios':[{'name':'baseline'},{'name':'fees_1p5x','fee_multiplier':1.5}]}))
            args=self.args(base,extra=['--mc-method','trade-shuffle','--scenario-config',str(config),'--scenario','fees_1p5x'])
            before={p.name:digest(p) for p in source.iterdir()};result=run(args)
            self.assertEqual(result['status'],'complete')
            self.assertAlmostEqual(result['results']['kelly']['net']['full_kelly'],.05/(.85*1.15),places=8)
            self.assertAlmostEqual(result['results']['kelly']['gross']['full_kelly'],.2,places=8)
            self.assertEqual(result['scenario_config_hash'],digest(config))
            data=load_run(source,{});returns=np.array([(t['net_pnl']-.5*t['fees'])/t['equity_before_entry'] for t in data['trades']])
            mc=result['results']['monte-carlo'];paths=np.asarray(mc['equity_paths'])
            np.testing.assert_allclose(paths[:,-1],1000*np.prod(1+returns))
            np.testing.assert_allclose(np.sort(paths[:,1:]/paths[:,:-1]-1,axis=1),np.tile(np.sort(returns),(31,1)),atol=1e-14)
            self.assertEqual(mc['configuration']['scenario']['fee_multiplier'],1.5)
            self.assertEqual(before,{p.name:digest(p) for p in source.iterdir()})
            with self.assertRaisesRegex(ValueError,'Unknown selected scenario'):
                run(self.args(base,output='unknown',extra=['--scenario','missing']))

    def test_selected_stop_gap_uses_remaining_partial_quantity_and_requires_evidence(self):
        from model_development.phase_3.outcomes import scenario_arrays
        from model_development.phase_3.monte_carlo import stressed_r
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base)
            data=load_run(source,{});data['scenario']={'name':'stop_gap','stop_extra_r':.25}
            with self.assertRaisesRegex(ValueError,'requires recorded exit reason'):
                scenario_arrays(data)
            for trade in data['trades']:trade.update(exit_reason='time_exit_next_print',tp1_filled=False)
            trade=data['trades'][0];trade.update(exit_reason='risk_lock_stop_next_print',tp1_filled=True,remaining_exit_quantity=.9)
            arrays=scenario_arrays(data);shocked=stressed_r(arrays,data['scenario'])
            self.assertAlmostEqual(shocked[0],trade['net_R']-.25*.9)
            np.testing.assert_array_equal(shocked[1:],np.array([t['net_R'] for t in data['trades'][1:]]))
            del trade['remaining_exit_quantity']
            with self.assertRaisesRegex(ValueError,'requires remaining_exit_quantity'):
                scenario_arrays(data)

    def test_explicit_replay_window_excludes_cache_warmup_from_bootstrap(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base)
            path=source/'run.json';meta=json.loads(path.read_text())
            meta['cache'].update(source_start_ms=ORIGIN-124*DAY,source_end_ms=ORIGIN+20*DAY-1)
            meta['replay_window_ms']=[ORIGIN,ORIGIN+10*DAY];path.write_text(json.dumps(meta))
            data=load_run(source,{})
            self.assertEqual(data['days'],10)
            self.assertEqual(data['source_bounds_ms'],[ORIGIN,ORIGIN+10*DAY])
            np.testing.assert_array_equal(data['offset'],np.arange(10))
            meta['replay_window_ms']=[ORIGIN+DAY,ORIGIN+10*DAY];path.write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError,'cover all selected outcomes'):
                load_run(source,{})

    def test_phase1_requires_explicit_counterfactual_capital(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);self.fixture(base,phase=1)
            with self.assertRaisesRegex(ValueError,'Phase 1 has no geometric capital'):run(self.args(base))
            self.assertEqual(json.loads((base/'evaluation/run.json').read_text())['status'],'failed')
            result=run(self.args(base,output='explicit',extra=['--risk-fraction','.005','--initial-equity','1000','--max-leverage','5']))
            self.assertIn('counterfactual_not_backtest',result['results']['monte-carlo']['configuration']['basis'])
            self.assertEqual(result['source_accounting']['closed_trades'],10)

    def test_invalid_accounting_and_source_mutation_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base)
            def mutate(data,args):
                result=monte_carlo(data,args)
                with (source/'run.json').open('a') as f:f.write(' ')
                return result
            with patch('model_development.phase_3.outcomes.monte_carlo',side_effect=mutate):
                with self.assertRaisesRegex(ValueError,'changed'):run(self.args(base))
            self.assertEqual(json.loads((base/'evaluation/run.json').read_text())['status'],'failed')
            rows=pq.read_table(source/'trades.parquet').to_pylist();rows[0]['net_R']+=1
            pq.write_table(pa.Table.from_pylist(rows),source/'trades.parquet')
            with self.assertRaisesRegex(ValueError,'reconciliation'):run(self.args(base,output='invalid'))

    def test_zero_and_no_observed_loss_are_not_finite_optimal_estimates(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);self.fixture(base,zero=True)
            result=run(self.args(base))
            self.assertEqual(result['results']['kelly']['net']['status'],'insufficient_sample')
            self.assertIsNone(result['results']['monte-carlo']['estimates'])
        summary=kelly_summary(dict(days=1,offset=np.array([0,0])),np.array([1.,2.]),resamples=3)
        self.assertEqual(summary['status'],'no_observed_loss_no_finite_estimate')
        self.assertIsNone(summary['full_kelly'])

    def test_resampling_wrapper_matches_existing_numerical_kernels(self):
        daily=np.array([.01,0.,-.02,.01,0.,0.,-.01])
        ix=block_indices(len(daily),2,31,np.random.default_rng(7))
        expected=path_stats(daily[ix],1000.)
        actual=resample_paths(daily,1000.,block_days=2,simulations=31,seed=7)
        for a,b in zip(actual,expected):np.testing.assert_array_equal(a,b)

    def test_invalid_numeric_helpers_fail_instead_of_estimates(self):
        with self.assertRaises(ValueError):kelly_summary(dict(days=1,offset=np.array([0])),np.array([np.nan]))
        with self.assertRaises(ValueError):kelly_summary(dict(days=1,offset=np.array([.5])),np.array([-1.]))
        with self.assertRaises(ValueError):resample_paths(np.array([.01]),1000.,simulations=True)

    def test_batch_source_nested_accounts_and_same_timestamp_print_order(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base,accounts=2)
            old=json.loads((source/'run.json').read_text())
            rows=pq.read_table(source/'trades.parquet').to_pylist()
            jobs=[]
            for index,account in enumerate(old['jobs']):
                account['configuration_id']=f'cfg{index}'
                child={**old,'configuration_id':f'cfg{index}','closed_trades':10,'accounts':[account]}
                child.pop('jobs')
                jobs.append(dict(status='complete',metadata=child))
                for row in rows:
                    if row['portfolio_id']==f'p{index}':row['configuration_id']=f'cfg{index}'
            meta={**old,'jobs':jobs};meta.pop('config');meta.pop('cache')
            (source/'run.json').write_text(json.dumps(meta));pq.write_table(pa.Table.from_pylist(rows),source/'trades.parquet')
            result=run(self.args(base,extra=['--configuration','cfg1']))
            self.assertEqual(result['selected_account']['portfolio_id'],'p1')
            self.assertEqual(result['source_accounting']['closed_trades'],10)
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base)
            rows=pq.read_table(source/'trades.parquet').to_pylist()
            for row in rows:
                for key in ('feature_as_of_ms','feature_available_at_ms','decision_timestamp_ms',
                            'entry_eligible_timestamp_ms','entry_timestamp_ms','exit_timestamp_ms'):
                    row[key]=ORIGIN+1000
            pq.write_table(pa.Table.from_pylist(rows),source/'trades.parquet')
            self.assertEqual(len(load_run(source,{})['trades']),10)
            rows[1]['entry_agg_trade_id']=1
            pq.write_table(pa.Table.from_pylist(rows),source/'trades.parquet')
            with self.assertRaisesRegex(ValueError,'Overlapping'):load_run(source,{})

    def test_legacy_render_run_flag_stays_forwarded(self):
        with patch.object(evaluate,'launch',return_value=7) as launch:
            self.assertEqual(evaluate.main(['--task','statistical-jump','--','render','--run','saved','--output','new']),7)
            launch.assert_called_once_with('model_development/phase_3/statistical_jump.py',
                ['render','--run','saved','--output','new'],module=False)

    def test_public_cli_from_neutral_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);self.fixture(base)
            command=[sys.executable,'-B',str(ROOT/'model_development/phase_3/evaluate.py'),
                '--task','kelly','monte-carlo','--run',str(base/'source'),'--output',str(base/'cli'),
                '--resamples','17','--simulations','19','--block-days','2','--seed','7']
            result=subprocess.run(command,cwd=temp,capture_output=True,text=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads((base/'cli/run.json').read_text())['status'],'complete')


if __name__=='__main__':unittest.main()
