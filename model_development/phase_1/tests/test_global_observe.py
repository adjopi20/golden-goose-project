import importlib.util
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import tempfile
import unittest

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


from model_development.phase_1.observe import execute, fold_schedule, parser, run, ticks, DAY, infer_instrument, load_model


CORE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(CORE))


class SimpleModel:
    """Deterministic fixture; no market-specific rule in the shared observer."""
    submodels=(1,)
    class config:
        base_timeframe=calculation_timeframe=evaluation_timeframe='1d'
    def __init__(self):
        self.pending={}
        self.positions={}
        self.position_blocks=[]
        self.records={1:[]}
    def evaluate(self, bars, *, decision_timestamp_ms):
        if not self.positions:
            self.pending[1]=dict(setup_id=str(decision_timestamp_ms), direction='long',
                feature_as_of_ms=int(bars.timestamp_ms.iloc[-1])+DAY,
                feature_available_at_ms=int(bars.timestamp_ms.iloc[-1])+DAY,
                decision_timestamp_ms=decision_timestamp_ms,
                entry_eligible_timestamp_ms=decision_timestamp_ms)
    def accept_fill(self,key,*,price,timestamp_ms):
        p=dict(self.pending.pop(key),entry=price,stop=price-10,target=price+10)
        self.positions[key]=p
        return p
    def record_exit(self,key,**kwargs):
        del self.positions[key]


class ObserverTests(unittest.TestCase):
    def test_default_fees_and_filename_instrument(self):
        args=parser().parse_args(['--model','chosen.py','--shared-core',str(CORE),
            '--input','ETHUSDC-aggTrades-2024-01-05_to_2026-08-31.parquet',
            '--start','2024-02-01','--end','2026-08-01','--risk','10',
            '--venue','unverified','--iteration','ITER-003','--inspection-status','unknown',
            '--output','new_run'])
        self.assertEqual((args.entry_fee_bps,args.exit_fee_bps),(4.,4.))
        self.assertEqual(infer_instrument(args.input,args.instrument),'ETHUSDC')
        with self.assertRaisesRegex(ValueError,'same instrument'):
            infer_instrument([Path('ETHUSDC-aggTrades-a.parquet'),Path('BTCUSDC-aggTrades-b.parquet')])
        with self.assertRaisesRegex(ValueError,'conflicts'):
            infer_instrument(args.input,'BTCUSDC')

    def test_script_selection_is_dynamic(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'tmp') as directory:
            for name in ('strategy_a','strategy_b'):
                script=Path(directory)/(name+'.py')
                script.write_text('from types import SimpleNamespace\n'
                    'def create_observer(config):\n'
                    '    return SimpleNamespace(submodels=(), pending={}, positions={}, position_blocks=[], '
                    'records={}, evaluate=None, accept_fill=None, record_exit=None, config=config, '
                    f'identity={name!r})\n',encoding='utf-8')
                model=load_model(script,{'parameter':7})
                self.assertEqual(model.identity,name)
                self.assertEqual(model.config,{'parameter':7})


    def test_fixed_risk_and_next_print_exit_no_terminal_close(self):
        model=SimpleModel()
        series=[(0,100),(DAY,100),(DAY+1,110),(DAY+2,112),
                (2*DAY,100),(2*DAY+1,90),(2*DAY+2,88),(3*DAY,100)]
        stream=[dict(timestamp_ms=t,price=p,qty=1.,agg_trade_id=i) for i,(t,p) in enumerate(series)]
        rows=[]
        counts=execute(model,stream,start=0,end=4*DAY,risk=10.,entry_bps=1.,exit_bps=2.,
            identity={'model_id':'fixture'},folds=[],emit=rows.append)
        self.assertEqual(len(rows),2)
        self.assertEqual([r['initial_risk'] for r in rows],[10,10])
        self.assertEqual([r['quantity'] for r in rows],[1,1])
        self.assertEqual([r['gross_R'] for r in rows],[1.2,-1.2])
        self.assertAlmostEqual(rows[0]['net_R'],(12-.01-.0224)/10)
        self.assertEqual(rows[0]['entry_timestamp_ms'],DAY)
        self.assertEqual(rows[0]['exit_timestamp_ms'],DAY+2)
        self.assertEqual(counts['omitted_open_positions'],{'1':1})

    def test_base_and_evaluation_timeframes_are_independent(self):
        model=SimpleModel()
        class HourlyConfig:
            base_timeframe='1h'
            calculation_timeframe=evaluation_timeframe='1d'
        model.config=HourlyConfig()
        stream=[dict(timestamp_ms=t,price=100.,qty=1.,agg_trade_id=i)
                for i,t in enumerate(range(0,DAY+1,3600000))]
        rows=[]
        counts=execute(model,stream,start=0,end=2*DAY,risk=10,entry_bps=0,exit_bps=0,
                       identity={'model_id':'fixture'},folds=[],emit=rows.append)
        self.assertEqual(model.positions[1]['decision_timestamp_ms'],DAY)
        self.assertEqual(counts['omitted_open_positions'],{'1':1})

    def test_missing_candle_stops(self):
        stream=[dict(timestamp_ms=t,price=100.,qty=1.,agg_trade_id=i) for i,t in enumerate((0,2*DAY))]
        with self.assertRaisesRegex(ValueError,'Missing candle'):
            execute(SimpleModel(),stream,start=0,end=3*DAY,risk=10,entry_bps=0,exit_bps=0,
                    identity={'model_id':'fixture'},folds=[],emit=lambda row:None)

    def test_walk_forward_calendar_and_incomplete_tail(self):
        start=int(pd.Timestamp('2025-01-01',tz='UTC').timestamp()*1000)
        end=int(pd.Timestamp('2026-02-01',tz='UTC').timestamp()*1000)
        folds=fold_schedule(start,end)
        self.assertEqual(len(folds),3)
        self.assertEqual([f['complete'] for f in folds],[True,True,False])
        self.assertEqual(folds[0]['test_end_ms'],folds[1]['test_start_ms'])
        self.assertEqual(pd.to_datetime(folds[1]['train_start_ms'],unit='ms',utc=True).month,3)

    def test_raw_cross_file_order_and_coverage(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'tmp') as directory:
            path=Path(directory)/'ticks.parquet'
            def write(ids):
                pq.write_table(pa.table(dict(timestamp=[0,1,10],price=[100.,101.,102.],qty=[1.,1.,1.],agg_trade_id=ids)),path)
            write([1,2,3])
            self.assertEqual(len(list(ticks([path],0,10,[]))),2)
            write([1,2,4])
            with self.assertRaisesRegex(ValueError,'aggregate IDs'):
                list(ticks([path],0,10,[]))
            write([1,2,3])
            with self.assertRaisesRegex(ValueError,'boundaries'):
                list(ticks([path],0,11,[]))

    def test_end_between_row_groups_and_files(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'tmp') as directory:
            root=Path(directory)
            table=pa.table(dict(timestamp=[0,1,11,12],price=[100.,101.,102.,103.],
                                qty=[1.,1.,1.,1.],agg_trade_id=[1,2,3,4]))
            path=root/'groups.parquet'
            pq.write_table(table,path,row_group_size=2)
            manifest=[]
            rows=list(ticks([path],0,10,manifest))
            self.assertEqual([r['timestamp_ms'] for r in rows],[0,1])
            self.assertEqual(manifest[0]['observed_last_ms'],11)
            first,second=root/'first.parquet',root/'second.parquet'
            pq.write_table(table.slice(0,2),first)
            pq.write_table(table.slice(2),second)
            self.assertEqual([r['timestamp_ms'] for r in ticks([first,second],0,10,[])],[0,1])

    def test_vwap_full_run_two_files_and_failure_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'tmp') as directory:
            base=Path(directory)
            model_path=CORE/'model_development/models/mean_reversion_by_vwap/monthly_sd_reversion.py'
            source=base/'input.parquet'
            dates=pd.date_range('2025-01-01','2025-11-01',freq='1D',tz='UTC')
            n=len(dates)
            pq.write_table(pa.table(dict(timestamp=dates.as_unit('ms').asi8,
                price=[100.+(i%7)*5 for i in range(n)],qty=[1.]*n,agg_trade_id=list(range(n)))),source)
            def arguments(output):
                return parser().parse_args(['--model',str(model_path),'--shared-core',str(CORE),
                    '--input',str(source),'--start','2025-01-01','--end','2025-11-01',
                    '--risk','10','--entry-fee-bps','1','--exit-fee-bps','1',
                    '--venue','synthetic','--instrument','TEST','--iteration','ITER-003',
                    '--inspection-status','development','--output',str(output)])
            output=base/'run'
            metadata=run(arguments(output))
            self.assertEqual(metadata['status'],'complete')
            self.assertEqual(sorted(p.name for p in output.iterdir()),['run.json','trades.parquet'])
            table=pq.read_table(output/'trades.parquet').to_pandas()
            self.assertIn('position_block',set(table.record_type))
            self.assertIn('closed_trade',set(table.record_type))
            self.assertTrue(table.loc[table.record_type=='position_block','gross_R'].isna().all())
            self.assertTrue((table.loc[table.record_type=='closed_trade','initial_risk']==10).all())
            with self.assertRaises(FileExistsError):
                run(arguments(output))
            zero=base/'zero'
            dates_zero=pd.date_range('2025-01-01','2025-02-01',freq='1D',tz='UTC')
            source_zero=base/'zero_input.parquet'
            pq.write_table(pa.table(dict(timestamp=dates_zero.as_unit('ms').asi8,
                price=[100.]*len(dates_zero),qty=[1.]*len(dates_zero),agg_trade_id=list(range(len(dates_zero))))),source_zero)
            zero_args=arguments(zero)
            zero_args.input=[source_zero]
            zero_args.end='2025-02-01'
            run(zero_args)
            zero_table=pq.read_table(zero/'trades.parquet')
            self.assertEqual(zero_table.num_rows,0)
            self.assertEqual(zero_table.schema.field('gross_R').type,pa.float64())
            broken=base/'failed'
            args=arguments(broken)
            args.end='2026-01-01'
            with self.assertRaises(ValueError):
                run(args)
            self.assertFalse(broken.exists())
            runtime=base/'runtime_failed'
            args=arguments(runtime)
            # Metadata covers the interval, but the first two IDs are duplicated.
            damaged=pq.read_table(source).set_column(3,'agg_trade_id',pa.array([0,0]+list(range(2,n))))
            damaged_path=base/'damaged.parquet'
            pq.write_table(damaged,damaged_path)
            args.input=[damaged_path]
            with self.assertRaises(ValueError):
                run(args)
            self.assertEqual(json.loads((runtime/'run.json').read_text())['status'],'failed')


if __name__=='__main__':
    unittest.main()
