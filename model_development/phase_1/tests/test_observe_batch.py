from pathlib import Path
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

CORE=Path(__file__).resolve().parents[3]
MODEL=CORE/'model_development/models/mean_reversion_by_vwap/monthly_sd_reversion.py'
sys.path.insert(0,str(CORE))
from model_development.phase_1 import observe
from model_development.phase_1.observe_batch import Lane,run_batch
from model_development.observation_cache import candle_cache,aggregate_candles,RawPrints


def raw(path,days=1):
    start=int(pd.Timestamp('2025-01-06',tz='UTC').timestamp()*1000)
    ts=start+np.arange(days*1440*2+1,dtype='int64')*30000
    price=100+8*np.sin(np.arange(len(ts))/31)+np.arange(len(ts))*.00001
    table=pa.table(dict(timestamp=ts,price=price,qty=np.ones(len(ts)),
        agg_trade_id=np.arange(len(ts),dtype='int64'),is_buyer_maker=np.arange(len(ts))%2==0))
    pq.write_table(table,path,row_group_size=311)
    return table,start,start+days*86400000


class BatchTests(unittest.TestCase):
    def test_non_monthly_fold_boundaries_do_not_overlap(self):
        start=int(pd.Timestamp('2024-08-31',tz='UTC').timestamp()*1000)
        end=int(pd.Timestamp('2026-08-31',tz='UTC').timestamp()*1000)
        folds=observe.fold_schedule(start,end)
        self.assertTrue(all(a['test_end_ms']==b['test_start_ms'] for a,b in zip(folds,folds[1:])))

    def test_cache_reuse_corruption_and_exact_raw_index(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'tmp') as directory:
            base=Path(directory);path=base/'TEST-aggTrades-a.parquet'
            table,start,end=raw(path)
            frame,first=candle_cache([path],base/'cache')
            again,second=candle_cache([path],base/'cache')
            self.assertFalse(first['reused']);self.assertTrue(second['reused'])
            pd.testing.assert_frame_equal(frame,again)
            self.assertAlmostEqual(frame.volume.sum(),table.num_rows)
            self.assertTrue(np.allclose(frame.delta,frame.buy_volume-frame.sell_volume))
            reader=RawPrints([path])
            ref=reader.first(start+310*30000)
            self.assertEqual(reader.tick(ref)['agg_trade_id'],310)
            self.assertEqual(reader.tick(reader.next(ref))['agg_trade_id'],311)
            reader.close()
            (Path(first['path'])/'candles_1m.parquet').write_bytes(b'broken')
            with self.assertRaisesRegex(ValueError,'checksum'):
                candle_cache([path],base/'cache')

    def test_prepared_cache_matches_raw_replay(self):
        for reset_ids in (False,True):
            with tempfile.TemporaryDirectory(dir=Path.cwd()/'tmp') as directory:
                base=Path(directory);path=base/'TEST-aggTrades-a.parquet'
                table,start,end=raw(path)
                if reset_ids:
                    ids=table['agg_trade_id'].to_numpy().copy()+5000
                    ids[len(ids)//2:]-=1000
                    table=table.set_column(table.schema.get_field_index('agg_trade_id'),'agg_trade_id',pa.array(ids))
                    pq.write_table(table,path,row_group_size=311)
                config=dict(anchor_period='week',timeframe='15m',sd_levels=[1,1.5,2,3])
                regular=observe.load_model(MODEL,config)
                rows=table.to_pydict()
                stream=[dict(timestamp_ms=int(t),price=float(p),qty=float(q),agg_trade_id=int(i))
                    for t,p,q,i in zip(rows['timestamp'],rows['price'],rows['qty'],rows['agg_trade_id']) if t<end]
                expected=[]
                baseline=observe.execute(regular,stream,start=start,end=end,risk=10.,entry_bps=4.,exit_bps=4.,
                    identity={'model_id':'test'},folds=[],emit=expected.append)
                minutes,_=candle_cache([path],base/'cache',id_policy='record' if reset_ids else 'error')
                prepared=observe.load_model(MODEL,config)
                bars=aggregate_candles(minutes,'15m',start,end)
                schedule=prepared.prepare_observation(bars,as_of_ms=end)
                actual=[]
                lane=Lane(prepared,RawPrints([path]),dict(model_id='test',instrument='TEST',configuration_id='x'),
                    SimpleNamespace(risk=10.,entry_fee_bps=4.,exit_fee_bps=4.),[],actual.append)
                for opening,closing in schedule:
                    if closing<end:
                        lane.decision(opening,closing)
                lane.advance(lane.reader.first(end))
                self.assertEqual(lane.closed,baseline['closed_trades'])
                self.assertEqual(lane.blocks,baseline['position_blocks'])
                self.assertEqual({str(k):int(p.position is not None) for k,p in lane.portfolios.items()},baseline['omitted_open_positions'])
                def tidy(rows):
                    frame=pd.DataFrame(rows)
                    fields=['record_type','submodel_id','setup_id','direction','feature_as_of_ms','decision_timestamp_ms',
                        'entry_timestamp_ms','exit_timestamp_ms','entry_agg_trade_id','exit_agg_trade_id',
                        'entry_price','exit_price','stop','target','quantity','fees','gross_R','net_R']
                    return frame.reindex(columns=fields).sort_values(['record_type','submodel_id','setup_id']).reset_index(drop=True)
                self.assertGreater(lane.closed,0)
                lane.reader.close()
                pd.testing.assert_frame_equal(tidy(expected),tidy(actual),check_dtype=False,rtol=1e-10,atol=1e-10)

    def test_weekly_reset_prepared_causality_and_decimal_levels(self):
        model=observe.load_model(MODEL,dict(anchor_period='week',timeframe='1d',sd_levels=[1,1.5,2,3]))
        bars=pd.DataFrame(dict(open=[100,120,80,100,130,90,100,200],high=[100,120,80,100,130,90,100,200],
            low=[100,120,80,100,130,90,100,200],close=[100,120,80,100,130,90,100,200],volume=[1.]*8),
            index=pd.date_range('2025-01-06',periods=8,freq='1D',tz='UTC'))
        end=int(pd.Timestamp('2025-01-14',tz='UTC').timestamp()*1000)
        model.prepare_observation(bars,as_of_ms=end)
        self.assertEqual(model._prepared_features.vwap.iloc[-1],200)
        self.assertEqual(model._prepared_features['std'].iloc[-1],0)
        self.assertIn('upper_1.5',model._prepared_features)
        other=observe.load_model(MODEL,dict(anchor_period='week',timeframe='1d',sd_levels=[1,1.5,2,3]))
        changed=bars.copy();changed.iloc[-1]=[999,999,999,999,1]
        other.prepare_observation(changed,as_of_ms=end)
        pd.testing.assert_frame_equal(model._prepared_features.iloc[:-1],other._prepared_features.iloc[:-1])
        with self.assertRaises(Exception):
            model.evaluate_prepared(int(bars.index[0].timestamp()*1000),decision_timestamp_ms=int(bars.index[0].timestamp()*1000)+1)

    def test_multi_asset_grid_one_output_and_cache_reuse(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'tmp') as directory:
            base=Path(directory)
            a,b=base/'AAA-aggTrades-a.parquet',base/'BBB-aggTrades-b.parquet'
            raw(a,days=7);raw(b,days=7)
            grid=base/'grid.json'
            grid.write_text(json.dumps(dict(anchor_period=['week'],timeframe=['1h','1d'],sd_levels=[[1,1.5,2,3]])))
            def args(name):
                return observe.parser().parse_args(['--batch','--model',str(MODEL),'--shared-core',str(CORE),
                    '--input',str(a),str(b),'--grid-file',str(grid),'--cache-dir',str(base/'cache'),
                    '--risk','10','--venue','synthetic','--iteration','ITER-005','--inspection-status','development',
                    '--output',str(base/name)])
            metadata=run_batch(args('first'))
            self.assertEqual(metadata['status'],'complete')
            self.assertEqual(len(metadata['jobs']),4)
            self.assertEqual(sum(len(j['configuration']['sd_levels']) for j in metadata['jobs']),16)
            self.assertEqual(sorted(p.name for p in (base/'first').iterdir()),['run.json','trades.parquet'])
            result=pq.read_table(base/'first/trades.parquet').to_pandas()
            self.assertEqual(set(result.instrument),{'AAA','BBB'})
            reused=run_batch(args('second'))
            self.assertTrue(all(s['reused'] for s in reused['sources'].values()))
            pd.testing.assert_frame_equal(result.drop(columns='run_id'),pq.read_table(base/'second/trades.parquet').to_pandas().drop(columns='run_id'))


if __name__=='__main__':
    unittest.main()
