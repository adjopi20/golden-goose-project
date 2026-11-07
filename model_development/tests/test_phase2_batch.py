from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from model_development.phase_2.model import parser,run_model
from model_development.phase_2.batch import plan,run_batch

CORE=Path('C:/Users/adjop/OneDrive/Documents/ChatGPT/GOLDEN GOOSE PROJECT/work/binance_futures_executor')
ORIGIN=int(datetime(2026,8,1,tzinfo=timezone.utc).timestamp()*1000)


class Phase2Batch(unittest.TestCase):
    def fixture(self,base):
        for instrument,scale in [('AAAUSDT',1.),('BBBUSDC',2.)]:
            pq.write_table(pa.table(dict(timestamp=[ORIGIN+x for x in (0,1000,2000,2001,5000,6000,7000,7001,10000)],
                agg_trade_id=list(range(9)),price=[p*scale for p in (100.,100.,105.,105.,110.,100.,105.,105.,110.)],qty=[1.]*9)),
                base/f'{instrument}-aggTrades-test.parquet')
        (base/'model.py').write_text('''from datetime import date
import pyarrow.parquet as pq
from model_development.phase_2.backtest import Config
def prepare_backtest(a):
    values=pq.read_table(a.input).to_pydict(); origin=values['timestamp'][0]; price=values['price'][1]
    if a.model_config.get('fail'):raise ValueError('declared test failure')
    signals=[]
    if not a.model_config.get('zero'):
        for ident,eligible,force in [('same-a',1000,5000),('same-b',6000,10000)]:
            signals.append(dict(sample_id=ident,session_day='2026-08-01',direction='long',stop=price*.95,
                entry_reference=price,feature_as_of_ms=origin+eligible,feature_available_at_ms=origin+eligible,
                decision_timestamp_ms=origin+eligible,entry_eligible_timestamp_ms=origin+eligible,
                entry_deadline_timestamp_ms=origin+eligible+1000,force_exit_timestamp_ms=origin+force))
    return dict(signals=signals,config=Config(a.initial_equity,a.risk_fraction,a.fee_bps,a.max_leverage,tp1_r=1.,tp1_fraction=.1),
        start_day=date(2026,8,1),end_day=date(2026,8,1),metadata=dict(model_id='generic-fixture',model_version='v1',submodel_id='generic'),
        timing_validator=lambda signal:None)
''')
        grid=dict(schema_version=1,model=dict(zero=[False,True]),execution=dict(risk_fraction=[.005,.01]),
            assets={'AAAUSDT':dict(venue='synthetic',product='linear_quote_margined',quantity_unit='AAA',pnl_unit='USDT'),
                    'BBBUSDC':dict(venue='synthetic',product='linear_quote_margined',quantity_unit='BBB',pnl_unit='USDC')})
        (base/'grid.json').write_text(json.dumps(grid))
        return grid

    def args(self,base,output='batch'):
        return parser().parse_args(['--batch','--model',str(base/'model.py'),'--input',
            str(base/'AAAUSDT-aggTrades-test.parquet'),str(base/'BBBUSDC-aggTrades-test.parquet'),
            '--grid-file',str(base/'grid.json'),'--shared-core',str(CORE),'--cache-dir',str(base/'cache'),
            '--iteration','ITER-TEST','--inspection-status','development','--output',str(base/output)])

    def test_batch_single_parity_isolation_zero_schema_and_cache_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);self.fixture(base);args=self.args(base)
            result=run_batch(args)
            self.assertEqual(result['status'],'complete');self.assertEqual(len(result['jobs']),8)
            self.assertNotIn('metrics',result)  # No pooled account return.
            self.assertEqual({p.name for p in args.output_dir.iterdir()},{'trades.parquet','run.json','equity_curve.png'})
            rows=pq.read_table(args.output_dir/'trades.parquet').to_pylist()
            closed=[r for r in rows if r['record_type']=='closed_trade']
            self.assertEqual(len(closed),8)
            self.assertEqual(len({r['trade_id'] for r in closed}),8)
            self.assertEqual(len({r['portfolio_id'] for r in closed}),4)
            target=next(j for j in result['jobs'] if j['instrument']=='AAAUSDT' and j['execution']['risk_fraction']==.005 and not j['model_config']['zero'])
            single=deepcopy(args);single.input=Path(target['input']);single.output_dir=base/'single'
            single.model_config=json.dumps(target['model_config']);single.cache_dir=base/'cache/AAAUSDT'
            for k in ('venue','product','quantity_unit','pnl_unit'):setattr(single,k,target[k])
            expected=run_model(single)
            actual=target['metadata']['accounts'][0]
            self.assertEqual(actual['metrics'],expected['metrics'])
            one=pq.read_table(base/'single/trades.parquet').to_pylist()
            many=[r for r in rows if r['batch_job_id']==target['job_id']]
            fields=('setup_id','quantity','gross_pnl','fees','net_pnl','entry_timestamp_ms','exit_timestamp_ms','equity_before_entry','equity_after_exit')
            self.assertEqual([{k:r[k] for k in fields} for r in many],[{k:r[k] for k in fields} for r in one])
            self.assertTrue(target['metadata']['cache']['reused']==False)
            self.assertTrue(any(j['metadata']['cache']['reused'] for j in result['jobs']))
            for job in result['jobs']:
                account=job['metadata']['accounts'][0]
                if job['model_config']['zero']:
                    self.assertEqual(account['closed_trades'],0);self.assertIsNone(account['metrics']['win_rate'])
                self.assertEqual(job['metadata']['requested_execution']['risk_fraction'],job['execution']['risk_fraction'])
            before=(args.output_dir/'run.json').read_bytes()
            with self.assertRaises(FileExistsError):run_batch(args)
            self.assertEqual(before,(args.output_dir/'run.json').read_bytes())

    def test_model_cases_and_folder_layout_preserve_master_child_results(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);grid=self.fixture(base)
            source=base/'model.py';text=source.read_text()
            text=text.replace("    values=pq.read_table(a.input).to_pydict();", "    pq.write_table(pq.read_table(a.input).slice(0,1),a.output_dir/'orderflow.parquet')\n    values=pq.read_table(a.input).to_pydict();")
            source.write_text(text)
            grid['model']={'zero':[False]};grid['execution']={}
            grid['model_cases']=[{'risk_management':'rm-d','risk_exit_hours':24},{'risk_management':'rm-d','risk_exit_hours':48}]
            (base/'grid.json').write_text(json.dumps(grid));args=self.args(base);args.batch_layout='folders'
            result=run_batch(args);self.assertEqual(len(result['jobs']),4)
            master=pq.read_table(args.output_dir/'trades.parquet').to_pylist()
            self.assertFalse((args.output_dir/'orderflow.parquet').exists())
            for job in result['jobs']:
                output=Path(job['output_dir']);self.assertEqual(output.parent,args.output_dir.resolve())
                saved=json.loads((output/'run.json').read_text());self.assertEqual(saved['status'],'complete')
                self.assertEqual(saved['requested_model_config']['risk_exit_hours'],job['model_config']['risk_exit_hours'])
                self.assertTrue((output/'orderflow.parquet').is_file());self.assertTrue((output/'equity_curve.png').is_file())
                rows=pq.read_table(output/'trades.parquet').to_pylist()
                self.assertEqual(rows,[r for r in master if r['batch_job_id']==job['job_id']])
                self.assertIn('loss_reward',saved);self.assertEqual(saved['loss_reward'],saved['jobs'][0]['loss_reward'])
                self.assertEqual(saved['metrics'],job['metadata']['metrics'])

    def test_folder_layout_failure_keeps_failed_child_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);grid=self.fixture(base);grid['model']={};grid['execution']={}
            grid['model_cases']=[{'fail':False},{'fail':True}]
            (base/'grid.json').write_text(json.dumps(grid));args=self.args(base);args.batch_layout='folders'
            with self.assertRaisesRegex(ValueError,'declared test failure'):run_batch(args)
            saved=json.loads((args.output_dir/'run.json').read_text())
            self.assertEqual(saved['status'],'failed')
            self.assertEqual([j['status'] for j in saved['jobs']],['complete','failed','pending','pending'])
            failed=saved['jobs'][1];receipt=json.loads((Path(failed['output_dir'])/'run.json').read_text())
            self.assertEqual(receipt['status'],'failed');self.assertIn('declared test failure',receipt['error'])
            self.assertTrue((Path(saved['jobs'][0]['output_dir'])/'trades.parquet').is_file())

    def test_invalid_grid_units_and_same_instrument_sources_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);grid=self.fixture(base)
            bads=[dict(grid,typo={}),dict(grid,execution={'slippage':[1]}),
                  dict(grid,execution={'risk_fraction':[True]}),dict(grid,execution={'risk_fraction':[.005,.005]}),
                  dict(grid,assets={}),dict(grid,schema_version=True)]
            for bad in bads:
                (base/'grid.json').write_text(json.dumps(bad))
                with self.subTest(bad=bad),self.assertRaises(ValueError):plan(self.args(base))
            (base/'grid.json').write_text(json.dumps(grid))
            args=self.args(base);args.input=[args.input[0],args.input[0]]
            with self.assertRaisesRegex(ValueError,'one file'):plan(args)
            self.assertFalse(args.output_dir.exists())

    def test_failed_job_keeps_completed_results_and_parent_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);grid=self.fixture(base)
            grid['model']={'fail':[False,True]};grid['execution']={}
            (base/'grid.json').write_text(json.dumps(grid));args=self.args(base)
            with self.assertRaisesRegex(ValueError,'declared test failure'):run_batch(args)
            saved=json.loads((args.output_dir/'run.json').read_text())
            self.assertEqual(saved['status'],'failed')
            self.assertEqual([j['status'] for j in saved['jobs']],['complete','failed','pending','pending'])
            self.assertEqual(pq.read_table(args.output_dir/'trades.parquet').num_rows,2)
            self.assertFalse((args.output_dir/'equity_curve.png').exists())

    def test_chart_failure_never_marks_batch_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);grid=self.fixture(base);grid['model']={};grid['execution']={}
            (base/'grid.json').write_text(json.dumps(grid));args=self.args(base)
            with patch('model_development.phase_2.batch.plot_accounts',side_effect=RuntimeError('chart failed')):
                with self.assertRaisesRegex(RuntimeError,'chart failed'):run_batch(args)
            saved=json.loads((args.output_dir/'run.json').read_text())
            self.assertEqual(saved['status'],'failed');self.assertTrue(all(j['status']=='complete' for j in saved['jobs']))
            self.assertEqual(pq.read_table(args.output_dir/'trades.parquet').num_rows,4)

    def test_asset_names_are_not_hardcoded_and_ids_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);self.fixture(base);args=self.args(base)
            grid,jobs,sources=plan(args);again=plan(args)[1]
            self.assertEqual([j['job_id'] for j in jobs],[j['job_id'] for j in again])
            self.assertEqual(set(sources),{'AAAUSDT','BBBUSDC'})

    def test_all_zero_batch_retains_typed_master_and_null_rates(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);grid=self.fixture(base);grid['model']={'zero':[True]};grid['execution']={}
            (base/'grid.json').write_text(json.dumps(grid));args=self.args(base)
            result=run_batch(args);table=pq.read_table(args.output_dir/'trades.parquet')
            self.assertEqual(result['status'],'complete');self.assertEqual(table.num_rows,0)
            self.assertEqual(table.schema.field('batch_job_id').type,pa.string())
            for job in result['jobs']:
                self.assertIsNone(job['metadata']['accounts'][0]['metrics']['win_rate'])

    def test_source_mutation_between_jobs_fails_without_mixing(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);self.fixture(base);args=self.args(base);count=0
            def mutate(child,*,owner):
                nonlocal count
                result=run_model(child,owner=owner);count+=1
                if count==1:
                    stats=child.input.stat()
                    os.utime(child.input,ns=(stats.st_atime_ns,stats.st_mtime_ns+1_000_000_000))
                return result
            with patch('model_development.phase_2.model.run_model',side_effect=mutate):
                with self.assertRaisesRegex(ValueError,'Source identity changed'):run_batch(args)
            saved=json.loads((args.output_dir/'run.json').read_text())
            self.assertEqual(saved['status'],'failed')
            self.assertEqual(saved['jobs'][0]['status'],'complete')
            self.assertEqual(saved['jobs'][1]['status'],'failed')

    def test_public_cli_real_vwap_multiple_assets_and_submodels(self):
        import pandas as pd
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);grid=self.fixture(base)
            dates=pd.date_range('2025-01-01','2025-03-01',freq='1D',tz='UTC')
            for instrument,scale in [('AAAUSDT',1.),('BBBUSDC',2.)]:
                pq.write_table(pa.table(dict(timestamp=dates.as_unit('ms').asi8,agg_trade_id=list(range(len(dates))),
                    price=[scale*(100.+(i%7)*5) for i in range(len(dates))],qty=[1.]*len(dates))),
                    base/f'{instrument}-aggTrades-test.parquet')
            grid['model']={'sd_levels':[[1],[1,2]]};grid['execution']={}
            (base/'grid.json').write_text(json.dumps(grid))
            command=[sys.executable,'-B',str(ROOT/'model_development/phase_2/backtest.py'),
                '--batch','--model',str(ROOT/'model_development/models/mean_reversion_by_vwap/legacy/monthly_sd_reversion.py'),
                '--input',str(base/'AAAUSDT-aggTrades-test.parquet'),str(base/'BBBUSDC-aggTrades-test.parquet'),
                '--grid-file',str(base/'grid.json'),'--shared-core',str(CORE),'--cache-dir',str(base/'cache'),
                '--iteration','ITER-TEST','--inspection-status','development','--output',str(base/'cli')]
            result=subprocess.run(command,cwd=temp,capture_output=True,text=True,timeout=90)
            self.assertEqual(result.returncode,0,result.stderr)
            meta=json.loads((base/'cli/run.json').read_text())
            self.assertEqual(len(meta['jobs']),4)
            self.assertEqual(sum(len(j['metadata']['accounts']) for j in meta['jobs']),6)
            self.assertGreater(meta['closed_trades'],0);self.assertGreater(meta['position_blocks'],0)
            self.assertEqual({p.name for p in (base/'cli').iterdir()},{'trades.parquet','run.json','equity_curve.png'})
            self.assertNotIn('final_equity',meta)


if __name__=='__main__':unittest.main()
