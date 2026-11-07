import ast,hashlib,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from datetime import date,timedelta
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
OLD=ROOT/'model_development/models/trend_following_preny_profile_15m/legacy/strategy.py'
NEW=ROOT/'model_development/models/trend_following_preny_profile_15m/strategy.py'

class PreNYMigration(unittest.TestCase):
    def test_frozen_origins_and_shared_math_parity(self):
        meta=json.loads(NEW.with_name('migration.json').read_text())
        for path,h in meta['origins'].items():
            file=Path(path) if Path(path).is_absolute() else ROOT/path
            self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(),h,path)
        for name in ('_common','moving_average','atr','adx','order_flow'):
            old=next(Path(p) for p in meta['origins'] if p.endswith('/indicator/'+name+'.py') or p.endswith('\\indicator\\'+name+'.py'))
            expected=old.read_text().replace('from indicator.cvd import','from indicator.research.cvd import')
            self.assertEqual(ast.dump(ast.parse(expected)),ast.dump(ast.parse((ROOT/'indicator/research'/f'{name}.py').read_text())))
        release=json.loads((ROOT/'model_development/models/legacy_frozen_candidates/release.json').read_text(encoding='utf-8'))
        self.assertTrue(all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in release['legacy_source_sha256'].items()))

    def test_embedded_calibration_and_runtime_decision_parity(self):
        from model_development.models.trend_following_preny_profile_15m.legacy import strategy as old
        from model_development.models.trend_following_preny_profile_15m import strategy as new
        first=date(2025,1,1)
        rows=[dict(sample_id=str(i),session_day=str(first+timedelta(days=i)),features=dict(directional_delta_imbalance=.1+(i%7)*.1,directional_result_atr=.5+(i%9)*.1),signal=dict(sample_id=str(i),session_day=str(first+timedelta(days=i)))) for i in range(250)]
        self.assertEqual(old.c1_calibrated_signals(rows),new.c1_calibrated_signals(rows))
        self.assertEqual(new.evaluate_session.__module__,new.__name__)
        self.assertEqual(new._gate.__module__,new.__name__)
        self.assertEqual(new._thresholds.__module__,new.__name__)
        self.assertFalse(hasattr(new,'phase2_main'))

    def test_global_cli_master_parity_with_205_day_calibration(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)
            stamps=pd.date_range('2025-01-01',periods=205*1440,freq='min',tz='America/New_York')
            hour=stamps.hour.to_numpy();minute=stamps.minute.to_numpy()
            price=np.where(hour<9,99.+minute%2,101.+(hour-9)*.01+minute*.001)
            raw=base/'HYPEUSDT-aggTrades-synthetic.parquet'
            pq.write_table(pa.table(dict(timestamp=stamps.tz_convert('UTC').as_unit('ms').asi8,agg_trade_id=np.arange(len(stamps)),price=price,qty=np.ones(len(stamps)),is_buyer_maker=np.zeros(len(stamps),dtype=bool))),raw)
            ledgers=[];metrics=[]
            for label,path in [('legacy',OLD),('canonical',NEW)]:
                output=base/label
                args=[sys.executable,'-B','-W','error',str(ROOT/'model_development/phase_2/backtest.py'),'--model',str(path),'--input',str(raw),'--output',str(output),'--cache-dir',str(base/'cache'),'--venue','synthetic','--product','linear_quote_margined','--quantity-unit','HYPE','--pnl-unit','USDT','--iteration','ITER-001','--inspection-status','development']
                result=subprocess.run(args,cwd=ROOT,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                master=pq.read_table(output/'trades.parquet').to_pylist()
                self.assertGreater(sum(r['record_type']=='closed_trade' for r in master),0)
                ledgers.append([{k:v for k,v in row.items() if k not in ('run_id','model_version')} for row in master])
                metadata=json.loads((output/'run.json').read_text());metrics.append(metadata['metrics'])
                self.assertEqual(metadata['config']['tp1_fraction'],.1)
                self.assertEqual(metadata['config']['risk_fraction'],.005)
                self.assertTrue(all(r['decision_timestamp_ms']>=r['feature_available_at_ms'] for r in master))
            self.assertEqual(ledgers[0],ledgers[1]);self.assertEqual(metrics[0],metrics[1])

if __name__=='__main__':unittest.main()
