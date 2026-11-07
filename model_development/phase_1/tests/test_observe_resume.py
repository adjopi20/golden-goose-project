from pathlib import Path
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import tempfile
from unittest import mock
import unittest

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

CORE=Path(__file__).resolve().parents[3]
MODEL=CORE/'model_development/models/mean_reversion_by_vwap/monthly_sd_reversion.py'
sys.path.insert(0,str(CORE))
from model_development.phase_1 import observe
from model_development.phase_1 import observe_batch
from model_development.observation_cache import candle_cache, validate_batch, LEGACY_BUILDERS


class ResumeTests(unittest.TestCase):
    def arrays(self):
        return dict(timestamp=np.array([1000,2683,3000]),price=np.array([100.,101.,102.]),
            qty=np.ones(3),agg_trade_id=np.array([10,40,41]),
            first_trade_id=np.array([20,15,16]),last_trade_id=np.array([20,15,16]))

    def test_discontinuity_recorded_without_dropping_or_renumbering(self):
        a=self.arrays()
        with self.assertRaisesRegex(ValueError,'Raw ID discontinuity'):
            validate_batch(a,policy='error')
        events,last=validate_batch(a,policy='record',source='test',row_offset=100)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['row'],101)
        self.assertEqual(events[0]['aggregate_id_step'],30)
        self.assertEqual(events[0]['before']['last_trade_id'],20)
        self.assertEqual(events[0]['after']['first_trade_id'],15)
        self.assertEqual(a['agg_trade_id'].tolist(),[10,40,41])
        self.assertEqual(last['agg_trade_id'],41)
        first={k:v[:1] for k,v in a.items()};rest={k:v[1:] for k,v in a.items()}
        _,previous=validate_batch(first,policy='record')
        split,_=validate_batch(rest,previous,policy='record',row_offset=1)
        self.assertEqual(split[0]['aggregate_id_step'],30)

    def test_chronological_backward_id_transition_is_recorded_in_its_own_segment(self):
        a=self.arrays()
        a['agg_trade_id']=np.array([100,40,41])
        events,last=validate_batch(a,policy='record')
        self.assertEqual(events[0]['kind'],'backward_id_transition')
        self.assertEqual(events[0]['source_segment_before'],0)
        self.assertEqual(events[0]['source_segment_after'],1)
        self.assertEqual(last['source_segment'],1)
        self.assertEqual(a['agg_trade_id'].tolist(),[100,40,41])
        first={k:v[:1] for k,v in a.items()};rest={k:v[1:] for k,v in a.items()}
        _,previous=validate_batch(first,policy='record')
        split,last=validate_batch(rest,previous,policy='record',row_offset=1)
        self.assertEqual(split[0]['kind'],'backward_id_transition')
        self.assertEqual(last['source_segment'],1)
        with self.assertRaisesRegex(ValueError,'discontinuity'):
            validate_batch(a,policy='error')
        a['timestamp']=np.array([1000,1000,3000])
        with self.assertRaisesRegex(ValueError,'same-timestamp'):
            validate_batch(a,policy='record')

    def test_record_policy_still_rejects_bad_values_duplicates_and_reversals(self):
        for field,values in [('qty',[1.,0.,1.]),('price',[100.,np.nan,102.]),
                             ('agg_trade_id',[10,10,11]),('timestamp',[1000,999,3000])]:
            a=self.arrays();a[field]=np.array(values)
            with self.subTest(field=field),self.assertRaises(ValueError):
                validate_batch(a,policy='record')

    def test_known_valid_legacy_cache_reused_and_record_cache_not_promoted(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'tmp') as directory:
            root=Path(directory);source=root/'TEST-aggTrades-a.parquet'
            pq.write_table(pa.table(dict(timestamp=[0,60000,120000],price=[100.,101.,102.],qty=[1.,1.,1.],agg_trade_id=[1,2,3])),source)
            frame,saved=candle_cache([source],root/'cache')
            meta=Path(saved['path'])/'cache.json'
            legacy=json.loads(meta.read_text());legacy['identity'].update(version=1,builder=next(iter(LEGACY_BUILDERS)))
            legacy['identity'].pop('id_policy');meta.write_text(json.dumps(legacy))
            reused,details=candle_cache([source],root/'cache',id_policy='record')
            pd.testing.assert_frame_equal(frame,reused)
            self.assertTrue(details['reused'])
            _,clean_record=candle_cache([source],root/'record_only',id_policy='record')
            _,clean_strict=candle_cache([source],root/'record_only',id_policy='error')
            self.assertTrue(clean_strict['reused'])
            bad=root/'GAP-aggTrades-a.parquet'
            pq.write_table(pa.table(dict(timestamp=[0,60000,120000],price=[100.,101.,102.],qty=[1.,1.,1.],agg_trade_id=[1,5,6])),bad)
            _,record=candle_cache([bad],root/'gap_cache',id_policy='record')
            self.assertEqual(record['source_continuity'],'unverified_discontinuities')
            self.assertEqual(len(record['discontinuities']),1)
            with self.assertRaisesRegex(ValueError,'discontinuity'):
                candle_cache([bad],root/'gap_cache',id_policy='error')

    def test_resume_carries_completed_asset_and_restarts_failed_asset(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()/'tmp') as directory:
            root=Path(directory)
            start=int(pd.Timestamp('2025-01-06',tz='UTC').timestamp()*1000)
            ts=start+np.arange(7*24+1,dtype='int64')*3600000
            n=len(ts)
            a,b=root/'AAA-aggTrades-a.parquet',root/'BBB-aggTrades-b.parquet'
            def write(path,gap):
                ids=np.arange(n,dtype='int64')+100
                if gap:
                    ids[n//3:]+=1000
                    ids[2*n//3:]-=500
                pq.write_table(pa.table(dict(timestamp=ts,price=100+np.sin(np.arange(n))*5,qty=np.ones(n),agg_trade_id=ids)),path)
            write(a,False);write(b,True)
            grid=root/'grid.json';grid.write_text(json.dumps(dict(anchor_period=['week'],timeframe=['1d'],sd_levels=[[1,2,3]])))
            def arguments(output,policy='error',parent=None):
                command=['--batch','--model',str(MODEL),'--shared-core',str(CORE),'--input',str(a),str(b),
                    '--grid-file',str(grid),'--cache-dir',str(root/'cache'),'--risk','10','--venue','synthetic',
                    '--iteration','ITER-005','--inspection-status','development','--id-policy',policy,'--output',str(output)]
                if parent:command+=['--resume-from',str(parent)]
                return command,observe.parser().parse_args(command)
            parent=root/'parent';command,args=arguments(parent)
            with mock.patch.object(sys,'argv',['observe.py']+command):
                with self.assertRaisesRegex(ValueError,'discontinuity'):
                    observe_batch.run_batch(args)
            before=(parent/'trades.parquet').read_bytes()
            previous=json.loads((parent/'run.json').read_text())
            self.assertEqual(previous['status'],'failed')
            self.assertEqual([j['instrument'] for j in previous['jobs'] if j['status']=='complete'],['AAA'])
            child=root/'child';command,args=arguments(child,'record',parent)
            actual_cache=observe_batch.candle_cache
            def guard(paths,*rest,**kwargs):
                self.assertNotIn(a,paths,'completed AAA should not even rebuild its cache')
                return actual_cache(paths,*rest,**kwargs)
            with mock.patch.object(sys,'argv',['observe.py']+command),mock.patch.object(observe_batch,'candle_cache',side_effect=guard):
                resumed=observe_batch.run_batch(args)
            self.assertEqual(resumed['status'],'complete')
            self.assertEqual(resumed['resume']['carried_jobs'],1)
            self.assertEqual([j['instrument'] for j in resumed['jobs']],['AAA','BBB'])
            self.assertEqual(resumed['sources']['BBB']['source_continuity'],'unverified_discontinuities')
            self.assertEqual([e['kind'] for e in resumed['sources']['BBB']['discontinuities']],['forward_id_gap','backward_id_transition'])
            self.assertEqual(before,(parent/'trades.parquet').read_bytes())
            self.assertEqual(sorted(p.name for p in child.iterdir()),['run.json','trades.parquet'])
            master=pq.read_table(child/'trades.parquet').to_pandas()
            self.assertTrue((master.run_id=='child').all())
            self.assertEqual(master[master.instrument=='AAA'].shape[0],pq.read_table(parent/'trades.parquet').num_rows)
            original=pq.read_table(parent/'trades.parquet').to_pandas().drop(columns='run_id').reset_index(drop=True)
            copied=master[master.instrument=='AAA'].drop(columns='run_id').reset_index(drop=True)
            pd.testing.assert_frame_equal(original,copied)
            command,bad_args=arguments(root/'bad_child','record',parent)
            bad_args.risk=11
            with self.assertRaisesRegex(ValueError,'fixed_risk'):
                observe_batch.run_batch(bad_args)
            self.assertFalse((root/'bad_child').exists())


if __name__=='__main__':
    unittest.main()
