from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from model_development.tests import test_phase3_run as fixtures
from model_development.phase_3 import evaluate
from model_development.phase_3.run_input import digest
from model_development.phase_3 import statistical_jump as sj

ORIGIN,DAY=fixtures.ORIGIN,fixtures.DAY

class JumpRun(unittest.TestCase):
    def fixture(self,base,**kwargs):
        source=fixtures.Phase3Run().fixture(base,**kwargs)
        stamps=pd.date_range(end='2026-01-10',periods=170,tz='UTC').as_unit('ms').asi8
        t=np.arange(len(stamps));rng=np.random.default_rng(23)
        returns=np.where((t//65)%2==0,.005,-.007)+rng.normal(0,.008,len(stamps))
        close=100*np.cumprod(1+returns)
        opening=close/(1+returns)
        pd.DataFrame(dict(timestamp_ms=stamps,open=opening,high=np.maximum(close,opening)+1,low=np.minimum(close,opening)-1,close=close,
            volume=np.ones(len(stamps)))).to_parquet(base/'market.parquet',index=False)
        return source

    def args(self,base,output='diagnosis',extra=()):
        return ['--run',str(base/'source'),'--market',str(base/'market.parquet'),
            '--instrument','AAAUSDT','--venue','synthetic','--product','linear',
            '--start','2026-01-01','--end','2026-01-11','--inspection-status','development',
            '--train-days','100','--inference-days','100','--warmup-days','60','--restarts','2','--jump-penalty','5',
            '--output',str(base/output),*extra]

    def test_selected_portfolio_preserves_full_population_columns_and_monetary_basis(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base,accounts=2)
            rows=pq.read_table(source/'trades.parquet').to_pylist()
            for r in rows:r['submodel_id']='same';r['custom_evidence']='retain'
            pq.write_table(pa.Table.from_pylist(rows),source/'trades.parquet')
            meta=json.loads((source/'run.json').read_text())
            for a in meta['jobs']:a['submodel_id']='same'
            (source/'run.json').write_text(json.dumps(meta))
            hashes={p.name:digest(p) for p in source.iterdir()}
            with self.assertRaisesRegex(ValueError,'exactly one'):sj.main(self.args(base))
            self.assertFalse((base/'diagnosis').exists())
            with patch('model_development.phase_2.backtest.main',side_effect=AssertionError('No Phase 2 invocation')):
                sj.main(self.args(base,extra=['--portfolio','p1']))
            saved=json.loads((base/'diagnosis/run.json').read_text())
            trades=pd.read_parquet(base/'diagnosis/attributed_trades.parquet')
            self.assertEqual(set(trades.portfolio_id),{'p1'})
            self.assertEqual(len(trades),10)
            self.assertEqual(set(trades.custom_evidence),{'retain'})
            self.assertEqual(saved['source_accounting']['excluded_other_account_trades'],10)
            self.assertEqual(saved['source_accounting']['net_pnl'],10.)
            self.assertEqual(saved['counts']['unknown_trades'],1)
            self.assertEqual(saved['chart']['equity'],'observed_realized_monetary_equity_at_exit')
            self.assertEqual(saved['chart']['account_equity'],dict(initial_equity=1000.,pnl_unit='USDT'))
            self.assertEqual({p.name for p in (base/'diagnosis').iterdir()},
                {'run.json','regimes.parquet','attributed_trades.parquet','equity_regimes.png'})
            self.assertEqual(hashes,{p.name:digest(p) for p in source.iterdir()})
            regimes=pd.read_parquet(base/'diagnosis/regimes.parquet')
            fig=sj.plot_equity(trades,base/'check.png','causal','AAAUSDT',daily=regimes,regimes=regimes,
                start_ms=ORIGIN,end_ms=ORIGIN+10*DAY,account_equity=saved['chart']['account_equity'])
            self.assertEqual(fig.axes[1].lines[0].get_ydata()[-1],1020.)
            self.assertEqual(fig.axes[1].lines[1].get_ydata()[-1],1010.)
            with patch.object(sj,'classify',side_effect=AssertionError('No refit')):
                sj.render_saved_run(base/'diagnosis',base/'render')
            rendered=json.loads((base/'render/run.json').read_text())
            self.assertFalse(rendered['model_refit'])
            self.assertEqual(rendered['source_chart'],saved['chart'])
            with self.assertRaises(FileExistsError):sj.main(self.args(base,extra=['--portfolio','p1']))

    def test_cross_boundary_outcomes_and_conflicting_provenance_fail_before_fit(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base)
            args=self.args(base);args[args.index('--end')+1]='2026-01-10'
            with patch.object(sj,'classify',side_effect=AssertionError('Must preflight')):
                with self.assertRaisesRegex(ValueError,'every selected closed outcome'):sj.main(args)
            self.assertFalse((base/'diagnosis').exists())
            meta=json.loads((source/'run.json').read_text());meta['venue']='different'
            (source/'run.json').write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError,'venue conflicts'):sj.main(self.args(base))

    def test_phase1_stays_R_and_zero_trade_phase2_stays_typed(self):
        for phase,zero in ((1,False),(2,True)):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as temp:
                base=Path(temp);self.fixture(base,phase=phase,zero=zero)
                sj.main(self.args(base))
                saved=json.loads((base/'diagnosis/run.json').read_text())
                self.assertEqual(saved['counts']['closed_trades'],0 if zero else 10)
                expected='realized_cumulative_R_at_exit' if phase==1 else 'observed_realized_monetary_equity_at_exit'
                self.assertEqual(saved['chart']['equity'],expected)
                self.assertIn('entry_regime',pq.read_schema(base/'diagnosis/attributed_trades.parquet').names)

    def test_source_mutation_is_failed_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);source=self.fixture(base)
            original=sj.classify
            def mutate(*args):
                result=original(*args)
                with (source/'run.json').open('a') as f:f.write(' ')
                return result
            with patch.object(sj,'classify',side_effect=mutate):
                with self.assertRaisesRegex(ValueError,'changed'):sj.main(self.args(base))
            self.assertEqual(json.loads((base/'diagnosis/run.json').read_text())['status'],'failed')
            with self.assertRaises(FileExistsError):sj.main(self.args(base))

    def test_selected_run_raw_and_validated_cache_match_measured_daily(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);self.fixture(base)
            sj.main(self.args(base))
            daily=pd.read_parquet(base/'market.parquet')
            stamps=[];prices=[];qty=[]
            for r in daily.itertuples(index=False):
                stamps.extend(r.timestamp_ms+np.array([0,600000,3600000,DAY-60000]))
                prices.extend([r.open,r.high,r.low,r.close]);qty.extend([.1,.2,.3,.4])
            stamps=[int(daily.timestamp_ms.iloc[0]-1),*stamps,int(daily.timestamp_ms.iloc[-1]+DAY)]
            prices=[daily.open.iloc[0],*prices,daily.close.iloc[-1]];qty=[.1,*qty,.1]
            raw=base/'AAAUSDT-aggTrades-synthetic.parquet'
            pd.DataFrame(dict(timestamp=np.array(stamps,dtype='int64'),price=prices,qty=qty,
                agg_trade_id=np.arange(len(stamps)),is_buyer_maker=np.arange(len(stamps))%2==0)).to_parquet(raw,index=False)
            args=self.args(base,output='raw')
            index=args.index('--market');del args[index:index+2]
            sj.main([*args,'--input',str(raw),'--cache-dir',str(base/'cache')])
            saved=json.loads((base/'raw/run.json').read_text())
            args=self.args(base,output='cached');index=args.index('--market');del args[index:index+2]
            sj.main([*args,'--candle-cache',saved['market_cache']['path']])
            expected=pd.read_parquet(base/'diagnosis/regimes.parquet')
            for name in ('raw','cached'):
                actual=pd.read_parquet(base/name/'regimes.parquet')
                pd.testing.assert_frame_equal(expected,actual,check_exact=False,rtol=1e-12,atol=1e-12)
                selected=pd.read_parquet(base/name/'attributed_trades.parquet')
                self.assertEqual(len(selected),10)
                self.assertEqual(selected.net_pnl.sum(),10.)

    def test_public_dispatch_and_neutral_directory_cli(self):
        with patch.object(evaluate,'launch',return_value=9) as launch:
            self.assertEqual(evaluate.main(['--task=statistical-jump','--run=saved','--output','new']),9)
            launch.assert_called_once_with('model_development/phase_3/statistical_jump.py',
                ['--run=saved','--output','new'],module=False)
        with self.assertRaises(SystemExit):evaluate.main(['--task','statistical-jump','kelly','--run','saved'])
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);self.fixture(base)
            command=[sys.executable,'-B','-W','error',str(ROOT/'model_development/phase_3/evaluate.py'),
                '--task','statistical-jump',*self.args(base)]
            result=subprocess.run(command,cwd=base,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(json.loads((base/'diagnosis/run.json').read_text())['status'],'complete')


if __name__=='__main__':unittest.main()
