import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from model_development.phase_3.monte_carlo import select_sizing
from model_development.phase_3 import monte_carlo as stress


class SizingStressTests(unittest.TestCase):
    def fixture(self):
        rows=[dict(sample_id=str(i),session_day=day,entry_timestamp_ms=100*i+10,
                   exit_timestamp_ms=100*i+20) for i,day in enumerate(['2025-01-01','2025-04-01','2025-04-02','2025-04-03'])]
        d=dict(rows=rows,r=np.array([1.,2.,-1.,.5]),fee_r=np.full(4,.1),distance=np.full(4,.01),
               stop_remaining=np.array([0.,0.,1.,0.]),offset=np.array([0,90,91,92]),
               start='2025-01-01',end='2025-04-30',days=120,sha256='abc')
        m=dict(tool='indicator_sizing',sources=[dict(dataset='asset',ledger_sha256='abc')],
               ledger_config=dict(risk_cap=.005,max_leverage=5,initial_equity=1000),
               config=dict(indicator_sizing=dict(low_multiplier=.5)))
        study=dict(manifest=m,trades=[],summary=[],folds=[dict(dataset='asset',family='adx',threshold='30',
                   test_month='2025-04',fixed_matched_risk=.004)],path='synthetic',hashes={})
        for policy,risks in [('fixed_base',[.005]*3),('indicator_sizing',[.005,.0025,.0025]),('fixed_train_matched',[.004]*3)]:
            eq=np.r_[1000.,1000*np.cumprod(1+np.array(risks)*d['r'][1:])]
            study['summary'].append(dict(dataset='asset',family='adx',threshold='30',policy=policy,
                evaluation_start='2025-04-01',evaluation_end='2025-04-30',final_equity=eq[-1],
                closed_trade_max_dd=(1-eq/np.maximum.accumulate(eq)).max()))
            for i,risk in enumerate(risks,1):
                study['trades'].append(dict(**rows[i],dataset='asset',family='adx',threshold='30',policy=policy,
                    feature_as_of_ms=rows[i]['entry_timestamp_ms']-1,net_r=d['r'][i],planned_risk_fraction=risk,
                    indicator_value='35' if i==1 else '20',feature_fallback='False',
                    requested_multiplier='1' if i==1 else '.5',fold='2025-04'))
        spec=dict(sizing_selection=dict(family='adx',threshold=30,policies=['fixed_base','indicator_sizing','fixed_train_matched']))
        return d,spec,study

    def test_same_sample_and_recorded_size(self):
        d,s,study=self.fixture()
        out,policies=select_sizing(d,'asset',s,study,5)
        self.assertEqual(out['start'],'2025-04-01')
        np.testing.assert_array_equal(out['offset'],[0,1,2])
        self.assertEqual(len(out['rows']),3)
        np.testing.assert_allclose(dict(policies)['indicator_sizing'],[1,.5,.5])

    def test_hype_fixed_has_no_indicator_rule(self):
        d,s,study=self.fixture()
        s['sizing_selection']['policies']=['fixed_base']
        _,p=select_sizing(d,'asset',s,study,5)
        np.testing.assert_array_equal(p[0][1],[1,1,1])

    def test_hash_mismatch_fails(self):
        d,s,study=self.fixture();d['sha256']='changed'
        with self.assertRaises(ValueError):select_sizing(d,'asset',s,study,5)

    def test_future_snapshot_fails(self):
        d,s,study=self.fixture();study['trades'][0]['feature_as_of_ms']=99999
        with self.assertRaises(ValueError):select_sizing(d,'asset',s,study,5)

    def test_size_tampering_fails(self):
        d,s,study=self.fixture();study['trades'][4]['planned_risk_fraction']=.005
        with self.assertRaises(ValueError):select_sizing(d,'asset',s,study,5)

    def test_missing_trade_fails(self):
        d,s,study=self.fixture();study['trades'].pop()
        with self.assertRaises(ValueError):select_sizing(d,'asset',s,study,5)

    def test_outcome_tampering_fails(self):
        d,s,study=self.fixture();study['trades'][0]['net_r']=100
        with self.assertRaises(ValueError):select_sizing(d,'asset',s,study,5)

    def test_stability_bounds(self):
        periods=stress.stability_periods(dict(start='2025-04-01',end='2025-04-30'))
        self.assertEqual(periods,[('early_half','2025-04-01','2025-04-15'),
                                 ('late_half','2025-04-16','2025-04-30'),('2025-04','2025-04-01','2025-04-30')])

    def test_end_to_end_with_and_without_overlay(self):
        d,s,study=self.fixture()
        for overlay in (False,True):
            with self.subTest(overlay=overlay), tempfile.TemporaryDirectory() as temp:
                output=Path(temp)/'out'
                cfg=dict(risk_fractions=[.005],block_days=[7,14],initial_equity=1000,max_leverage=5,
                         scenarios=[dict(name='baseline'),dict(name='fees',fee_multiplier=1.5)])
                if overlay:cfg['sizing_study']='existing'
                with patch.object(stress,'config_load',return_value=(cfg,Path(temp))), \
                     patch.object(stress,'sources',return_value=[('asset',Path('ledger'),s)]), \
                     patch.object(stress,'ledger_load',return_value=copy.deepcopy(d)), \
                     patch('model_development.phase_3.monte_carlo.load_study',return_value=study), \
                     patch('sys.argv',['stress','--config','unused','--output-dir',str(output),'--simulations','16','--chunk-size','5']), \
                     contextlib.redirect_stdout(io.StringIO()):
                    stress.main()
                done=json.loads((output/'complete.json').read_text())
                self.assertEqual(done['combinations'],12 if overlay else 4)
                self.assertTrue((output/'period_stability.csv').is_file())
                self.assertEqual((output/'paired_sizing_comparison.csv').is_file(),overlay)


if __name__=='__main__':unittest.main()
