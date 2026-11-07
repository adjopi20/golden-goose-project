"""Canonical VWAP relocation parity; historical model/core remain immutable."""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT=Path(__file__).resolve().parents[2]
OLD=ROOT/'model_development/models/mean_reversion_by_vwap/legacy/monthly_sd_reversion.py'
NEW=ROOT/'model_development/models/mean_reversion_by_vwap/monthly_sd_reversion.py'
CORE=Path('C:/Users/adjop/OneDrive/Documents/ChatGPT/GOLDEN GOOSE PROJECT/work/binance_futures_executor')


class VWAPMigration(unittest.TestCase):
    def test_calculation_and_decision_asts_are_preserved(self):
        for source,target in ((CORE/'indicator/vwap.py',ROOT/'indicator/research/vwap.py'),
                              (CORE/'trading_core/candles.py',ROOT/'utils/closed_candle_context.py')):
            self.assertEqual(ast.dump(ast.parse(source.read_text(encoding='utf-8').replace('from indicator.cvd import','from indicator.research.cvd import'))),ast.dump(ast.parse(target.read_text(encoding='utf-8'))))
        normalized=OLD.read_text(encoding='utf-8').replace('from indicator.vwap import','from indicator.research.vwap import').replace('import indicator.vwap','import indicator.research.vwap')
        before=ast.parse(normalized);after=ast.parse(NEW.read_text(encoding='utf-8'))
        for node in before.body:
            if isinstance(node,(ast.FunctionDef,ast.ClassDef)) and node.name!='observe_main':
                migrated=next(n for n in after.body if getattr(n,'name',None)==node.name)
                self.assertEqual(ast.dump(node),ast.dump(migrated),node.name)

    def test_legacy_sources_and_live_release_are_unchanged(self):
        migration=json.loads(NEW.with_name('migration.json').read_text())
        for path,expected in {**migration['shared_sources'],**migration['preserved_legacy_sources']}.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(),expected,path)
        release=json.loads((ROOT/'model_development/models/legacy_frozen_candidates/release.json').read_text(encoding='utf-8'))
        for path,expected in release['legacy_source_sha256'].items():
            self.assertEqual(hashlib.sha256((ROOT/path).read_bytes()).hexdigest(),expected,path)

    def cli(self,args,*,success=True):
        result=subprocess.run([sys.executable,'-B','-W','error',*map(str,args)],cwd=ROOT,capture_output=True,text=True)
        if success:self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        else:self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        return result

    def test_monthly_weekly_observe_backtest_and_legacy_core_parity(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);stamps=pd.date_range('2025-01-01','2025-03-03',freq='1D',tz='UTC')
            prices=[60. if d.day==21 else 65. if d.day==22 else 150. if d.day==25 else 145. if d.day==26 else 100. for d in stamps]
            raw=base/'UNITUSDT-aggTrades-synthetic.parquet'
            pq.write_table(pa.table(dict(timestamp=stamps.as_unit('ms').asi8,price=prices,qty=[1.]*len(stamps),
                agg_trade_id=list(range(len(stamps))))),raw)
            observe_grid=base/'observe_grid.json';observe_grid.write_text(json.dumps(dict(anchor_period=['week'],timeframe=['1d'],sd_levels=[[.5,1.]])))
            backtest_grid=base/'backtest_grid.json';backtest_grid.write_text(json.dumps(dict(schema_version=1,
                model=dict(anchor_period=['week'],timeframe=['1d'],sd_levels=[[.5,1.]]))))
            for phase,batch in ((1,False),(1,True),(2,False),(2,True)):
                with self.subTest(phase=phase,batch=batch):
                    prefix=f'p{phase}_'+('week' if batch else 'month')
                    outputs=[]
                    for label,model,core in [('legacy',OLD,CORE),('canonical',NEW,None)]:
                        out=base/(prefix+'_'+label)
                        runner=ROOT/f'model_development/phase_{phase}'/('observe.py' if phase==1 else 'backtest.py')
                        args=[runner,'--model',model,'--input',raw,'--iteration','ITER-005','--inspection-status','development',
                            '--venue','synthetic','--output',out]
                        if core:args+=['--shared-core',core]
                        if phase==1:args+=['--start','2025-01-01','--end','2025-03-01','--risk','10']
                        else:args+=['--product','linear_quote_margined','--quantity-unit','UNIT','--pnl-unit','USDT','--cache-dir',base/'cache2']
                        if batch:args+=['--batch','--grid-file',observe_grid if phase==1 else backtest_grid,'--cache-dir',base/f'cache{phase}']
                        self.cli(args)
                        rows=pq.read_table(out/'trades.parquet').to_pylist()
                        normalized=[{k:v for k,v in row.items() if k not in ('run_id','model_version')} for row in rows]
                        normalized.sort(key=lambda r:(r['record_type'],r.get('trade_id') or r.get('setup_id'),r.get('submodel_id') or ''))
                        metadata=json.loads((out/'run.json').read_text())
                        if phase==2 and batch:
                            # Batch IDs intentionally include the selected source SHA;
                            # relocation changes provenance, never its account mapping.
                            for index,job in enumerate(metadata['jobs']):
                                original=job['job_id'];replacement=f"{job['instrument']}:job{index}"
                                for row in normalized:
                                    for key,value in row.items():
                                        if isinstance(value,str) and value.startswith(original):
                                            row[key]=replacement+value[len(original):]
                        self.assertEqual(metadata['status'],'complete')
                        self.assertGreater(sum(r['record_type']=='closed_trade' for r in rows),0)
                        self.assertEqual(set(p.name for p in out.iterdir()),{'trades.parquet','run.json'}|({'equity_curve.png'} if phase==2 else set()))
                        if label=='canonical':
                            dependencies=metadata['dependencies'] if phase==1 else metadata.get('code',{})
                            if phase==2 and batch:dependencies=metadata['jobs'][0]['metadata']['code']
                            self.assertIn(str((ROOT/'indicator/research/vwap.py').resolve()),dependencies)
                            self.assertIn(str((ROOT/'utils/closed_candle_context.py').resolve()),dependencies)
                        outputs.append(normalized)
                    self.assertEqual(outputs[0],outputs[1])
            # Migrated model can also use the explicitly selected historical clock,
            # without replacing indicator.vwap or silently mixing its formula.
            out=base/'canonical_legacy_core'
            self.cli([ROOT/'model_development/phase_1/observe.py','--model',NEW,'--shared-core',CORE,'--input',raw,
                '--start','2025-01-01','--end','2025-03-01','--risk','10','--venue','synthetic',
                '--iteration','ITER-005','--inspection-status','development','--output',out])
            actual=pq.read_table(out/'trades.parquet').to_pylist();expected=pq.read_table(base/'p1_month_canonical/trades.parquet').to_pylist()
            normalize=lambda rows:[{k:v for k,v in r.items() if k not in ('run_id','model_version')} for r in rows]
            self.assertEqual(normalize(actual),normalize(expected))
            # Simulate a verified pre-migration orchestrator fingerprint; model/core
            # and actual data stay untouched. The allowlist must not exempt formulas.
            parent=base/'p1_week_legacy';meta=json.loads((parent/'run.json').read_text())
            migration=json.loads(NEW.with_name('migration.json').read_text())
            for name,sha in migration['previous_orchestrator_sha256'].items():meta['dependencies'][str((ROOT/'research'/name).resolve())]=sha
            (parent/'run.json').write_text(json.dumps(meta))
            def resume(model,output):
                return [ROOT/'model_development/phase_1/observe.py','--batch','--model',model,'--shared-core',CORE,
                    '--input',raw,'--grid-file',observe_grid,'--cache-dir',base/'cache1','--start','2025-01-01','--end','2025-03-01',
                    '--risk','10','--venue','synthetic','--iteration','ITER-005','--inspection-status','development',
                    '--resume-from',parent,'--output',output]
            self.cli(resume(OLD,base/'resumed'))
            saved=json.loads((base/'resumed/run.json').read_text())
            self.assertEqual(saved['resume']['carried_jobs'],1)
            rejected=self.cli(resume(NEW,base/'wrong_model_resume'),success=False)
            self.assertIn('model/shared-core path mismatch',rejected.stderr)
            dependency=base/'frozen_formula.py';dependency.write_text('FORMULA=1\n')
            meta['dependencies'][str(dependency)]=hashlib.sha256(dependency.read_bytes()).hexdigest()
            (parent/'run.json').write_text(json.dumps(meta));dependency.write_text('FORMULA=2\n')
            rejected=self.cli(resume(OLD,base/'changed_formula_resume'),success=False)
            self.assertIn('Changed model/execution dependency',rejected.stderr)


if __name__=='__main__':unittest.main()
