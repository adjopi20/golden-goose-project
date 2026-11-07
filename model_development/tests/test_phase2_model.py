from pathlib import Path
from datetime import date
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_development.phase_2.model import parser, run_model, load_model
from model_development.phase_2 import backtest as engine

CORE = Path('C:/Users/adjop/OneDrive/Documents/ChatGPT/GOLDEN GOOSE PROJECT/work/binance_futures_executor')


def signal(sid='a', eligible=1000, force=5000):
    return dict(sample_id=sid, session_day='2026-08-01', direction='long', stop=95.,
        entry_reference=100., feature_as_of_ms=eligible, feature_available_at_ms=eligible,
        decision_timestamp_ms=eligible, entry_eligible_timestamp_ms=eligible,
        entry_deadline_timestamp_ms=eligible+1000, force_exit_timestamp_ms=force)


class ModelBacktest(unittest.TestCase):
    def args(self, base, output='run'):
        return parser().parse_args(['--model',str(base/'model.py'),'--input',str(base/'UNITUSDT-aggTrades-test.parquet'),
            '--output',str(base/output),'--shared-core',str(CORE),'--cache-dir',str(base/'cache'),
            '--venue','synthetic','--product','linear_quote_margined','--quantity-unit','UNIT',
            '--pnl-unit','USDT','--iteration','ITER-TEST','--inspection-status','development'])

    def fixture(self, base, signals=None):
        signals = [signal(),signal('b',6000,10000)] if signals is None else signals
        pq.write_table(pa.table(dict(timestamp=[0,1000,2000,2001,5000,6000,7000,7001,10000],
            agg_trade_id=list(range(9)), price=[100.,100.,105.,105.,110.,100.,105.,105.,110.],
            qty=[1.]*9)),base/'UNITUSDT-aggTrades-test.parquet')
        (base/'model.py').write_text(
            'from datetime import date\nfrom model_development.phase_2.backtest import Config\n'
            'def timing(signal):\n    pass\n'
            'def prepare_backtest(a):\n'
            f'    return dict(signals={signals!r}, config=Config(a.initial_equity,a.risk_fraction,a.fee_bps,a.max_leverage,tp1_r=1.,tp1_fraction=.1),\n'
            '        start_day=date(2026,8,1),end_day=date(2026,8,1),metadata=dict(model_id="fixture",model_version="v1",submodel_id="one"),timing_validator=timing)\n')

    def test_generic_external_model_matches_existing_geometric_engine(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp); self.fixture(base)
            args = self.args(base)
            result = run_model(args)
            original = engine.run_phase2(args.input,[signal(),signal('b',6000,10000)],base/'legacy',
                engine.Config(tp1_r=1.,tp1_fraction=.1),date(2026,8,1),date(2026,8,1),{},timing_validator=lambda s:None)
            actual = pq.read_table(base/'run/trades.parquet').to_pylist()
            expected = pq.read_table(base/'legacy/trades.parquet').to_pylist()
            for a,b in zip(actual,expected):
                for name in ('entry_timestamp_ms','exit_timestamp_ms','entry_agg_trade_id','exit_agg_trade_id',
                             'quantity','gross_pnl','fees','net_pnl','initial_risk','equity_before_entry',
                             'equity_after_exit','partial_gross','partial_exit_fee','tp1_timestamp_ms'):
                    self.assertEqual(a[name],b[name],name)
            self.assertEqual(len(actual),len(expected))
            self.assertEqual(result['metrics'],original['metrics'])
            self.assertGreater(actual[1]['quantity'],actual[0]['quantity'])
            self.assertEqual({p.name for p in args.output_dir.iterdir()},{'run.json','trades.parquet','equity_curve.png'})
            with self.assertRaises(FileExistsError): run_model(args)

    def test_missing_timing_fails_and_json_only_attempt_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp); bad=signal(); del bad['decision_timestamp_ms']; self.fixture(base,[bad])
            args=self.args(base)
            with self.assertRaisesRegex(ValueError,'timing'): run_model(args)
            self.assertEqual(json.loads((args.output_dir/'run.json').read_text())['status'],'failed')
            self.assertEqual([p.name for p in args.output_dir.iterdir()],['run.json'])
            with self.assertRaises(FileExistsError): run_model(args)

    def test_development_forward_gap_keeps_session_and_records_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self.fixture(base)
            args = self.args(base, 'strict')
            args.id_policy = 'error'
            table = pq.read_table(args.input)
            index = table.schema.get_field_index('agg_trade_id')
            pq.write_table(table.set_column(index, 'agg_trade_id', pa.array([0,1,4,5,6,7,8,9,10])), args.input)
            with self.assertRaisesRegex(ValueError, 'Raw ID discontinuity'):
                run_model(args)
            args = self.args(base, 'record')
            args.id_policy = 'record'
            result = run_model(args)
            self.assertEqual(result['closed_trades'], 2)
            self.assertEqual(result['id_policy'], 'record')
            self.assertEqual(result['cache']['source_continuity'], 'unverified_discontinuities')
            self.assertEqual([g['kind'] for g in result['cache']['discontinuities']], ['forward_id_gap'])
            self.assertTrue((args.output_dir/'equity_curve.png').is_file())

    def test_recorded_id_transitions_preserve_fills_and_reuse_cache(self):
        for ids in ([0,1,2,1,2,3,4,5,6], [0,1,2,3,4,1,2,3,4]):
            with self.subTest(ids=ids), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                self.fixture(base)
                baseline = run_model(self.args(base, 'baseline'))
                args = self.args(base, 'strict_reset')
                args.id_policy = 'error'
                table = pq.read_table(args.input)
                index = table.schema.get_field_index('agg_trade_id')
                pq.write_table(table.set_column(index, 'agg_trade_id', pa.array(ids)), args.input)
                with self.assertRaisesRegex(ValueError, 'Raw ID discontinuity'):
                    run_model(args)
                args = self.args(base, 'record_reset')
                args.id_policy = 'record'
                result = run_model(args)
                self.assertEqual(result['closed_trades'], 2)
                self.assertEqual(result['metrics'], baseline['metrics'])
                self.assertEqual(result['cache']['source_continuity'], 'unverified_discontinuities')
                event, = result['cache']['discontinuities']
                self.assertEqual(event['kind'], 'backward_id_transition')
                self.assertEqual((event['source_segment_before'], event['source_segment_after']), (0, 1))
                self.assertEqual(result['raw_id_discontinuity_counts'],
                                 dict(forward_id_gap=0, backward_id_transition=1))
                rows = pq.read_table(args.output_dir/'trades.parquet').to_pylist()
                closed = [r for r in rows if r['record_type'] == 'closed_trade']
                self.assertEqual([r['entry_agg_trade_id'] for r in closed], [ids[1], ids[5]])
                self.assertEqual([r['exit_agg_trade_id'] for r in closed], [ids[4], ids[8]])
                with patch('model_development.observation_cache.validate_batch',
                           side_effect=AssertionError('Validated cache must be reused')):
                    reused = run_model(self.args(base, 'cached_reset'))
                self.assertTrue(reused['cache']['reused'])
                self.assertEqual(reused['metrics'], result['metrics'])
                self.assertEqual(reused['cache']['discontinuities'], result['cache']['discontinuities'])

    def test_named_prepared_portfolios_write_one_geometric_master(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); self.fixture(base)
            long=signal()
            short=dict(signal('b',6000,10000), direction='short', stop=115.)
            lanes=dict(baseline_long_only=[long], baseline_both=[long,short],
                       c1_long_only=[], c1_both=[short])
            fake=SimpleNamespace(prepare_backtest=lambda request:dict(signals=lanes,config=engine.Config(),
                start_day=date(2026,8,1),end_day=date(2026,8,1),
                metadata=dict(report_directional_expectancy=True,directional_block_months=1),
                timing_validator=lambda s:None,replay_window=(1000,10001)))
            with patch('model_development.phase_2.model.load_model',return_value=fake):
                result=run_model(self.args(base))
            jobs={j['portfolio_id']:j for j in result['jobs']}
            self.assertEqual(set(jobs),set(lanes))
            self.assertEqual([jobs[k]['closed_trades'] for k in lanes],[1,2,0,1])
            self.assertIsNone(jobs['baseline_long_only']['directional_expectancy']['overall']['short']['expectancy_net_R'])
            self.assertEqual(jobs['c1_both']['directional_expectancy']['monthly_blocks'][0]['short']['trades'],1)
            self.assertEqual(result['replay_window_ms'],[1000,10001])
            rows=pq.read_table(base/'run/trades.parquet').to_pylist()
            self.assertEqual(len({r['trade_id'] for r in rows}),4)
            self.assertEqual({p.name for p in (base/'run').iterdir()},{'run.json','trades.parquet','equity_curve.png'})

    def test_hype_oos_freezes_saved_threshold_beyond_original_fold_end(self):
        from model_development.models.trend_following_preny_profile_15m.strategy import frozen_direction_oos
        with tempfile.TemporaryDirectory() as temp:
            receipt = Path(temp)/'run.json'
            config=dict(comparison='direction',oos_start='2026-09-01',oos_end='2026-10-31',calibration_run=str(receipt))
            thresholds=dict(directional_delta_imbalance=.1,directional_result_atr=.8)
            fold=dict(train_end_exclusive='2026-07-25',test_start='2026-07-26',test_end_exclusive='2026-09-24',thresholds=thresholds)
            saved=dict(run_id='fixture',status='complete',phase='backtest',instrument='HYPEUSDT',model_version='phase2_hype_direction_v1',
                       requested_model_config={'comparison':'direction'},end_date='2026-08-31',config={},calibration_folds=[fold])
            receipt.write_text(json.dumps(saved))
            start,end,pinned=frozen_direction_oos(config,{})
            self.assertEqual((start,end),(date(2026,9,1),date(2026,10,31)))
            self.assertEqual(pinned['thresholds'],thresholds)
            saved['end_date']='2026-09-01'
            receipt.write_text(json.dumps(saved))
            with self.assertRaisesRegex(ValueError,'pre-OOS'):
                frozen_direction_oos(config,{})

    def test_observer_only_model_and_unknown_flags_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); self.fixture(base)
            (base/'model.py').write_text('def create_observer(config):\n    pass\n')
            args=self.args(base)
            with self.assertRaisesRegex(ValueError,'Unsupported lifecycle model'): run_model(args)
            self.assertEqual(json.loads((args.output_dir/'run.json').read_text())['status'],'failed')
            with self.assertRaises(SystemExit): parser().parse_args(['--grid-file','unsupported.json'])

    def test_zero_open_and_block_censoring(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); self.fixture(base,[])
            zero=run_model(self.args(base))
            self.assertEqual(zero['closed_trades'],0)
            self.assertIsNone(zero['metrics']['win_rate'])
            self.fixture(base,[signal(force=90000),signal('blocked',3000,100000)])
            # No time exit in source; TP1 is a partial, not a complete trade.
            cfg=engine.Config(tp1_r=100.,tp1_fraction=.1)
            fake=SimpleNamespace(prepare_backtest=lambda request:dict(signals=[signal(force=90000),signal('blocked',3000,100000)],
                config=cfg,start_day=date(2026,8,1),end_day=date(2026,8,1),metadata={},timing_validator=lambda s:None))
            with patch('model_development.phase_2.model.load_model',return_value=fake):
                opened=run_model(self.args(base,'open'))
            self.assertEqual(opened['closed_trades'],0)
            self.assertEqual(opened['open_positions_omitted'],1)
            rows=pq.read_table(base/'open/trades.parquet').to_pylist()
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['record_type'],'position_block')
            self.assertIsNone(rows[0]['net_pnl'])


if __name__=='__main__': unittest.main()
