import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from backtest_engine.path_audit import audit_arrays, cached_path, summarize, validate_signal
from models.btc_pny.prepare_path_audit import choose_stop, evidence_json_value, publish_bundle
import json


def signal(direction='long'):
    return dict(sample_id='s1',session_day='2025-01-01',route='outside_va_clean_trend',
        direction=direction,entry=100.,entry_timestamp_ms=0,entry_fill_agg_trade_id=10,
        force_exit_timestamp_ms=5,feature_as_of_ms=0,
        stops={'baseline':90. if direction=='long' else 110.},
        context={'macro_direction':'BULL_TREND','volatility_state':'NORMAL','alignment':'aligned'})


def run(prices,s=None,targets=(1,2,3,4)):
    return audit_arrays(s or signal(),np.arange(len(prices)),prices,np.arange(10,10+len(prices)),targets,4)


class TestPathAudit(unittest.TestCase):
    def test_nested_missing_evidence(self):
        paths=[]
        data=evidence_json_value({'x':[float('nan'),np.float64('nan'),np.int64(2)]},paths)
        self.assertEqual(data,{'x':[None,None,2]})
        self.assertEqual(len(paths),2)
        json.dumps(data,allow_nan=False)

    def test_infinity_not_silently_missing(self):
        with self.assertRaises(ValueError):evidence_json_value({'x':float('inf')},[])

    def test_atomic_bundle_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out'
            publish_bundle(out,[{'entry':100.}],[{'metric':float('nan')}],{}, {})
            self.assertEqual(json.loads((out/'shelf_and_regime_evidence.jsonl').read_text()),{'metric':None})
            with self.assertRaises(FileExistsError):publish_bundle(out,[],[],{}, {})

    def test_bad_execution_value_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out'
            with self.assertRaises(ValueError):publish_bundle(out,[{'entry':float('nan')}],[],{}, {})
            self.assertFalse(out.exists())

    def test_stop_before_tail(self):
        rows=run([100,89,130,140,150,160,160])
        self.assertEqual(rows[0]['mfe_r'],0)
        self.assertFalse(any(r['hit_before_stop_or_cutoff'] for r in rows))
        self.assertEqual(rows[0]['terminal_fill_price'],130)

    def test_target_then_stop(self):
        rows=run([100,110,109,89,150,160,160])
        self.assertTrue(rows[0]['returned_to_initial_stop_after_hit'])
        self.assertEqual(rows[0]['exit_fill_price'],109)
        self.assertFalse(rows[1]['hit_before_stop_or_cutoff'])

    def test_no_milestone_at_cutoff(self):
        rows=run([100,101,102,103,104,150,151])
        self.assertFalse(rows[0]['hit_before_stop_or_cutoff'])
        self.assertEqual(rows[0]['mfe_r'],.4)
        self.assertEqual(rows[0]['exit_fill_price'],151)

    def test_short_symmetry(self):
        long=run([100,110,120,130,140,145,145])
        short=run([100,90,80,70,60,55,55],signal('short'))
        self.assertEqual([r['mfe_r'] for r in long],[r['mfe_r'] for r in short])
        self.assertTrue(all(r['hit_before_stop_or_cutoff'] for r in short))

    def test_each_stop_own_path(self):
        s=signal(); s['stops']['shelf']=92.5
        rows=run([100,92,110,120,130,140,140],s)
        self.assertTrue(rows[0]['hit_before_stop_or_cutoff'])
        self.assertFalse(rows[4]['hit_before_stop_or_cutoff'])

    def test_guard(self):
        self.assertEqual(choose_stop(100,90,'long',dict(available=True,stop=92.5))[0],92.5)
        self.assertEqual(choose_stop(100,90,'long',dict(available=True,stop=93))[0],90)
        self.assertEqual(choose_stop(100,110,'short',dict(available=True,stop=107.5))[0],107.5)
        self.assertEqual(choose_stop(100,90,'long',dict(available=False))[0],90)

    def test_future_features(self):
        s=signal();s['feature_as_of_ms']=1
        with self.assertRaises(ValueError):validate_signal(s)

    def test_wrong_stop(self):
        s=signal();s['stops']['baseline']=101
        with self.assertRaises(ValueError):validate_signal(s)

    def test_incomplete(self):
        with self.assertRaises(ValueError):run([100,101,102,103,104,105])

    def test_duplicate_ids(self):
        with self.assertRaises(ValueError):audit_arrays(signal(),np.arange(7),[100]*7,[10]*7,[1],4)

    def test_wrong_entry(self):
        with self.assertRaises(ValueError):run([101]*7)

    def test_pullback(self):
        rows=run([100,110,105,120,115,115,115])
        self.assertEqual(rows[0]['max_pullback_r_until_next_target_or_terminal'],.5)

    def test_summary(self):
        rows=run([100,110,109,89,150,160,160]); groups=summarize(rows)
        self.assertTrue(any(r['dimension']=='macro_direction' and r['state']=='BULL_TREND' for r in groups))

    def test_cache_reuse_and_entry_id(self):
        ticks=[dict(timestamp_ms=i,price=100.,agg_trade_id=10+i,qty=1.) for i in range(7)]
        module='models.orb.scripts.pre_ny_submodels.audit_pre_ny_early_immediate_tail_geometry_cp002._iter_ticks'
        with tempfile.TemporaryDirectory() as tmp:
            raw=Path(tmp)/'raw';raw.write_bytes(b'fixture')
            with patch(module,return_value=iter(ticks)),patch('backtest_engine.path_audit.pq.ParquetFile'):
                path,reused=cached_path(raw,signal(),Path(tmp)/'cache')
                self.assertFalse(reused)
            path2,reused=cached_path(raw,signal(),Path(tmp)/'cache')
            self.assertTrue(reused);self.assertEqual(path,path2)
            path.write_bytes(b'corrupt')
            with self.assertRaises(ValueError):cached_path(raw,signal(),Path(tmp)/'cache')


if __name__=='__main__':unittest.main()
