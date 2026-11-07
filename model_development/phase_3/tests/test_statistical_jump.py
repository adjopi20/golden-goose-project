import importlib.util
import itertools
import json
from pathlib import Path
import sys
from contextlib import contextmanager
import shutil
import unittest
from unittest.mock import patch
import uuid
from types import SimpleNamespace

import numpy as np
import pandas as pd

MODULE = Path(__file__).resolve().parents[1]/"statistical_jump.py"
spec = importlib.util.spec_from_file_location("postmod_jump_under_test", MODULE)
sj = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sj
spec.loader.exec_module(sj)
DEP, _ = sj.load_dependencies()


@contextmanager
def test_directory():
    root = Path.cwd().resolve()/"tmp"
    folder = root/("statistical_jump_test_"+uuid.uuid4().hex)
    folder.mkdir(parents=True)
    try:
        yield folder
    finally:
        if not folder.resolve().is_relative_to(root.resolve()):
            raise ValueError("Refuse cleanup outside test workspace")
        shutil.rmtree(folder)


def market(n=450):
    rng = np.random.default_rng(23)
    t = np.arange(n)
    r = np.where((t//65)%2 == 0, .005, -.007) + rng.normal(0, .008, n)
    close = 100*np.cumprod(1+r)
    return pd.DataFrame(dict(timestamp_ms=pd.date_range("2020-01-01", periods=n, tz="UTC").as_unit("ms").asi8,
        open=close/(1+r), high=np.maximum(close,close/(1+r))*1.001,
        low=np.minimum(close,close/(1+r))*.999, close=close, volume=np.ones(n)))


def master(regimes):
    boundaries = regimes.loc[regimes.state.notna(), "end_ms"].to_numpy()[::9]
    return pd.DataFrame([dict(record_type="closed_trade", trade_id=str(i), instrument="TEST",
        run_id="synthetic", model_id="synthetic", configuration_id="fixed", submodel_id="main",
        decision_timestamp_ms=int(t+1000), entry_timestamp_ms=int(t+1001),
        exit_timestamp_ms=int(t+4000), exit_agg_trade_id=i,
        gross_R=float(np.cos(i*.7)+.1), net_R=float(np.cos(i*.7)+.06)) for i,t in enumerate(boundaries)])


def raw_prints(daily):
    times, prices, quantities = [], [], []
    for row in daily.itertuples(index=False):
        times.extend(row.timestamp_ms + np.array([0, 600000, 3600000, sj.DAY-60000]))
        prices.extend([row.open, row.high, row.low, row.close])
        quantities.extend([.1, .2, .3, .4])
    times = [int(daily.timestamp_ms.iloc[0]-1), *times, int(daily.timestamp_ms.iloc[-1]+sj.DAY)]
    prices = [daily.open.iloc[0], *prices, daily.close.iloc[-1]]
    quantities = [.1, *quantities, .1]
    return pd.DataFrame(dict(timestamp=np.array(times,dtype="int64"), price=prices, qty=quantities,
        agg_trade_id=np.arange(len(times),dtype="int64"), is_buyer_maker=np.arange(len(times))%2==0))


def raw_args(folder, path):
    return SimpleNamespace(market=None,input=[path],cache_dir=folder/"cache",candle_cache=None,
        project_root=sj.DEFAULT_ROOT,output=folder/"result",instrument="TEST",base_timeframe="1d")


class JumpTests(unittest.TestCase):
    def setUp(self):
        self.raw = market()
        self.end = int(self.raw.timestamp_ms.iloc[-1]+sj.DAY)
        self.daily = sj.prepare_daily(self.raw,self.end,DEP["clock"], instrument="TEST")
        self.cfg = sj.Config(jump_penalty=5, train_days=100, inference_days=100,
                             warmup_days=60, restarts=2, refit_months=2)
        self.start = int(self.daily.end_ms.iloc[0])

    def test_solver_bruteforce_and_terminal_paths(self):
        solver = DEP["solver"]
        losses = np.random.default_rng(6).uniform(size=(8,2))
        for penalty in (0., .3, 10.):
            path = solver._viterbi(losses, penalty)
            def objective(s):
                return sum(losses[t,k] for t,k in enumerate(s)) + penalty*np.count_nonzero(np.diff(s))
            self.assertAlmostEqual(objective(path), min(objective(p) for p in itertools.product(range(2),repeat=8)))
            for t in range(1,9):
                self.assertEqual(solver._last_state(losses[:t],penalty), solver._viterbi(losses[:t],penalty)[-1])
        np.testing.assert_array_equal(solver._viterbi(np.zeros((4,2)),1.), np.zeros(4))

    def test_ewm_formula_and_no_fabricated_first_return(self):
        f=sj.features(self.daily,60)
        self.assertTrue(np.isnan(f.excess_return.iloc[0]))
        self.assertTrue(f[sj.FEATURES].iloc[:60].isna().all().all())
        e=f.excess_return.iloc[1:81].to_numpy()
        weights=2.**(-np.arange(79,-1,-1)/10)
        self.assertAlmostEqual(f.downside_deviation_10.iloc[80],np.sqrt(np.average(np.minimum(e,0)**2,weights=weights)))
        positive=self.daily.copy()
        positive["close"]=100*np.exp(np.arange(len(positive))*.01)
        self.assertTrue(sj.features(positive,60).sortino_60.isna().all())

    def test_causal_future_invariance_including_refits(self):
        before,refits=sj.classify(self.daily,self.cfg,self.start,self.end,DEP)
        changed=self.raw.copy()
        cut=310
        changed.loc[cut:, ["open","high","low","close"]] *= 3
        altered=sj.prepare_daily(changed,self.end,DEP["clock"])
        after,_=sj.classify(altered,self.cfg,self.start,self.end,DEP)
        pd.testing.assert_frame_equal(before.iloc[:cut],after.iloc[:cut])
        for refit in refits:
            self.assertLess(refit["fit_end_ms"],refit["effective_from_ms"])
        prefix,_=sj.classify(self.daily.iloc[:cut],self.cfg,self.start,int(self.daily.end_ms.iloc[cut-1]),DEP)
        pd.testing.assert_frame_equal(before.iloc[:len(prefix)].reset_index(drop=True), prefix)

    def test_fit_parity_objective_and_convergence(self):
        f=sj.features(self.daily,60).iloc[60:160]
        model,_=sj.fit_checked(f,self.cfg,DEP["solver"])
        ref=DEP["solver"].StatisticalJumpModel(5,restarts=2).fit(f[sj.FEATURES].to_numpy(),f.excess_return.to_numpy())
        np.testing.assert_array_equal(model.states_,ref.states_)
        solver=DEP["solver"]
        z=model._transform(f[sj.FEATURES].to_numpy())
        base=solver._objective(z,model.centers_,model.states_,5)
        improved=solver._viterbi(solver._losses(z,model.centers_),5)
        self.assertLessEqual(solver._objective(z,model.centers_,improved,5),base+1e-10)
        flat=f.copy()
        flat[sj.FEATURES]=0.
        with self.assertRaisesRegex(ValueError,"constant"):
            sj.fit_checked(flat,self.cfg,solver)

    def test_missing_coverage_availability_and_bad_inputs(self):
        with self.assertRaises(ValueError):
            sj.prepare_daily(self.raw.drop(index=10),self.end,DEP["clock"])
        with self.assertRaises(ValueError):
            sj.prepare_daily(pd.concat([self.raw,self.raw.iloc[:1]]),self.end,DEP["clock"])
        with self.assertRaises(ValueError):
            sj.Config(jump_penalty=float("nan")).validate()
        with self.assertRaises(ValueError):
            sj.Config(inference_days=0).validate()
        delayed=self.raw.copy()
        delayed["available_at_ms"]=delayed.timestamp_ms+sj.DAY
        delayed.loc[200,"available_at_ms"] += 1000
        daily=sj.prepare_daily(delayed,self.end,DEP["clock"])
        regimes,_=sj.classify(daily,self.cfg,self.start,self.end,DEP)
        self.assertEqual(regimes.label_available_at_ms.iloc[200],int(delayed.available_at_ms.iloc[200]))
        unclosed=self.raw.copy()
        unclosed["is_closed"]=True
        unclosed.loc[200,"is_closed"]=False
        with self.assertRaises(ValueError):
            sj.prepare_daily(unclosed,self.end,DEP["clock"])

    def test_entry_attribution_blocks_zero_and_experiment_isolation(self):
        r,_=sj.classify(self.daily,self.cfg,self.start,self.end,DEP)
        trades=master(r)
        stamp=int(r.loc[r.state.notna(),"end_ms"].iloc[1])
        trades.loc[0,["decision_timestamp_ms","entry_timestamp_ms","exit_timestamp_ms"]]=[stamp-1,stamp+100,stamp+5000]
        blocks=trades.iloc[:1].copy()
        blocks["record_type"]="position_block"
        result=sj.attribute_trades(pd.concat([trades,blocks]),r,"TEST",self.start,self.end)
        self.assertEqual(len(result),len(trades))
        self.assertTrue((result.regime_available_at_ms <= result.decision_timestamp_ms).all())
        mixed=trades.copy()
        mixed.loc[0,"configuration_id"]="other"
        with self.assertRaisesRegex(ValueError,"configuration_id"):
            sj.attribute_trades(mixed,r,"TEST",self.start,self.end)
        empty=sj.attribute_trades(trades.iloc[:0],r,"TEST",self.start,self.end)
        self.assertTrue(all(x["net_expectancy_R"] is None for x in sj.summaries(empty)))

    def test_retrospective_explicit_hindsight(self):
        cfg=sj.Config(**{**self.cfg.__dict__,"mode":"retrospective"})
        regimes,_=sj.classify(self.daily,cfg,self.start,self.end,DEP)
        known=regimes.loc[regimes.state.notna()]
        self.assertTrue((known.label_available_at_ms==self.end).all())
        self.assertTrue((known.status=="retrospective_hindsight").all())

    def test_cli_artifacts_and_failed_run_no_overwrite(self):
        with test_directory() as folder:
            market_path=folder/"market.parquet"
            self.raw.to_parquet(market_path,index=False)
            regimes,_=sj.classify(self.daily,self.cfg,self.start,self.end,DEP)
            master(regimes).to_parquet(folder/"trades.parquet",index=False)
            (folder/"run.json").write_text(json.dumps(dict(status="complete",instrument="TEST",entry_fee_bps=4,exit_fee_bps=4)))
            args=["--market",str(market_path),"--instrument","TEST","--venue","synthetic","--product","linear",
                "--start",str(pd.to_datetime(self.start,unit="ms",utc=True)),"--end",str(pd.to_datetime(self.end,unit="ms",utc=True)),
                "--inspection-status","development","--train-days","100","--inference-days","100","--warmup-days","60",
                "--restarts","2","--jump-penalty","5","--refit-months","2","--trades",str(folder/"trades.parquet"),
                "--output",str(folder/"result")]
            sj.main(args)
            out=folder/"result"
            self.assertEqual(set(p.name for p in out.iterdir()),{"regimes.parquet","attributed_trades.parquet","run.json","equity_regimes.png"})
            meta=json.loads((out/"run.json").read_text())
            self.assertEqual(meta["status"],"complete")
            t=pd.read_parquet(out/"attributed_trades.parquet")
            self.assertAlmostEqual(sum(x["net_total_R"] or 0 for x in meta["by_entry_regime"]),t.net_R.sum())
            self.assertEqual(meta["chart"]["template"],sj.CHART)
            original={name:sj.digest(out/name) for name in ['run.json','regimes.parquet','attributed_trades.parquet','equity_regimes.png']}
            with patch.object(sj,'classify',side_effect=AssertionError('Rendering must not refit')), patch.object(sj,'load_dependencies',side_effect=AssertionError('Rendering must not load live cache/solver dependencies')):
                sj.main(['render','--run',str(out),'--output',str(folder/'calendar_export')])
            rendered=json.loads((folder/'calendar_export/run.json').read_text())
            self.assertEqual(rendered['status'],'complete')
            self.assertFalse(rendered['model_refit'])
            self.assertAlmostEqual(rendered['net_total_R'],t.net_R.sum())
            self.assertEqual(original,{name:sj.digest(out/name) for name in original})
            with self.assertRaises(FileExistsError):
                sj.render_saved_run(out,folder/'calendar_export')
            with self.assertRaises(FileExistsError):
                sj.main(args)
            (folder/"run.json").write_text('{"status":"failed"}')
            args[-1]=str(folder/"failed")
            with self.assertRaisesRegex(ValueError,"incomplete/failed"):
                sj.main(args)
            self.assertEqual(json.loads((folder/"failed/run.json").read_text())["status"],"failed")

    def test_calendar_layout_uses_actual_exits_and_label_availability(self):
        import matplotlib.dates as dates
        from matplotlib.collections import PathCollection
        start=int(self.raw.timestamp_ms.iloc[0]);end=start+5*sj.DAY
        switch=start+2*sj.DAY+2000
        regimes=pd.DataFrame(dict(end_ms=[start,start+2*sj.DAY],label_available_at_ms=[start,switch],regime=['unknown','bull']))
        trades=pd.DataFrame(dict(record_type=['closed_trade']*2,trade_id=['late','early'],
            decision_timestamp_ms=[start+sj.DAY*3,start+sj.DAY*2+3000],
            exit_timestamp_ms=[start+sj.DAY*4,start+sj.DAY*3+1000],
            gross_R=[.8,-.6],net_R=[.7,-.7],entry_regime=['bull','bull']))
        with test_directory() as folder:
            fig=sj.plot_equity(trades,folder/'calendar.png','causal','TEST',daily=self.daily,regimes=regimes,start_ms=start,end_ms=end)
            self.assertEqual(len(fig.axes),4)
            self.assertEqual(len({tuple(ax.get_xlim()) for ax in fig.axes}),1)
            equity=fig.axes[1]
            np.testing.assert_allclose(equity.lines[1].get_ydata(),[0,-.7,0,0],atol=1e-12)
            plotted=pd.to_datetime(equity.lines[1].get_xdata(),utc=True).as_unit('ms').asi8
            np.testing.assert_array_equal(plotted,[start,start+sj.DAY*3+1000,start+sj.DAY*4,end])
            self.assertEqual(equity.lines[1].get_drawstyle(),'steps-post')
            fig.canvas.draw()
            x=dates.date2num(pd.to_datetime(switch,unit='ms',utc=True))
            expected=fig.axes[2].transData.transform((x,0))[0]
            self.assertAlmostEqual(fig.axes[2].patches[1].get_extents().xmin,expected,places=5)
            positions=np.concatenate([c.get_offsets()[:,0] for c in fig.axes[3].collections if isinstance(c,PathCollection)])
            np.testing.assert_allclose(np.sort(positions),np.sort(dates.date2num(pd.to_datetime(trades.decision_timestamp_ms,unit='ms',utc=True))))
            self.assertEqual(fig.axes[3].get_xlabel(),'Calendar date (UTC)')
            boxes=[label.get_window_extent(fig.canvas.get_renderer()) for label in fig.axes[3].get_xticklabels()]
            self.assertTrue(all(boxes[i].x1<boxes[i+1].x0 for i in range(len(boxes)-1)))
            bad=self.daily.copy();bad.loc[bad.index[0],'close']=np.nan
            with self.assertRaisesRegex(ValueError,'Invalid/duplicate measured daily prices'):
                sj.plot_equity(trades,folder/'bad.png','causal','TEST',daily=bad,regimes=regimes,start_ms=start,end_ms=end)

    def test_calendar_empty_trades_and_hindsight_are_explicit(self):
        start=int(self.raw.timestamp_ms.iloc[0]);end=start+5*sj.DAY
        regimes=pd.DataFrame(dict(end_ms=[start,start+2*sj.DAY],label_available_at_ms=[end,end],regime=['unknown','bull']))
        trades=pd.DataFrame(dict(record_type=pd.Series(dtype=str),trade_id=pd.Series(dtype=str),
            decision_timestamp_ms=pd.Series(dtype='int64'),exit_timestamp_ms=pd.Series(dtype='int64'),
            gross_R=pd.Series(dtype=float),net_R=pd.Series(dtype=float),entry_regime=pd.Series(dtype=str)))
        with test_directory() as folder:
            fig=sj.plot_equity(trades,folder/'empty.png','retrospective','TEST',daily=self.daily,regimes=regimes,start_ms=start,end_ms=end)
            np.testing.assert_array_equal(fig.axes[1].lines[1].get_ydata(),[0,0])
            self.assertTrue(any('No closed trades' in text.get_text() for text in fig.axes[1].texts))
            self.assertTrue(any('HINDSIGHT' in text.get_text() for text in fig.texts))


    def test_sparse_cache_raw_daily_regime_parity_and_reuse(self):
        with test_directory() as folder:
            raw_path=folder/"TEST-aggTrades-synthetic.parquet"
            raw_prints(self.raw).to_parquet(raw_path,index=False)
            args=raw_args(folder,raw_path)
            daily,info,_=sj.load_market(args,self.end,DEP)
            self.assertFalse(info["reused"])
            self.assertEqual(info["first_complete_day_ms"],int(self.raw.timestamp_ms.iloc[0]))
            self.assertIsNotNone(info["excluded_initial_partial_ms"])
            np.testing.assert_allclose(daily[["open","high","low","close","volume"]],self.daily[["open","high","low","close","volume"]])
            before,_=sj.classify(self.daily,self.cfg,self.start,self.end,DEP)
            after,_=sj.classify(daily,self.cfg,self.start,self.end,DEP)
            pd.testing.assert_frame_equal(before,after,check_exact=False,rtol=1e-12,atol=1e-12)
            reused,reuse,_=sj.load_market(args,self.end,DEP)
            self.assertTrue(reuse["reused"])
            pd.testing.assert_frame_equal(daily,reused)
            self.assertEqual(reuse["path"],info["path"])
            # Direct cache mode has the same validation and measured daily result.
            args.input=None;args.candle_cache=Path(info["path"])
            cached,_,_=sj.load_market(args,self.end,DEP)
            pd.testing.assert_frame_equal(cached,daily)
            with self.assertRaisesRegex(ValueError,"conflict"):
                sj.daily_from_cache(Path(info["path"]),self.end,"OTHER",DEP)

    def test_cache_corruption_source_changes_and_coverage(self):
        with test_directory() as folder:
            raw_path=folder/"TEST-aggTrades-synthetic.parquet"
            prints=raw_prints(self.raw)
            prints.to_parquet(raw_path,index=False)
            _,info,_=sj.load_market(raw_args(folder,raw_path),self.end,DEP)
            cache=Path(info["path"])
            with self.assertRaisesRegex(ValueError,"complete UTC days"):
                sj.daily_from_cache(cache,self.end+sj.DAY,"TEST",DEP)
            file=cache/"candles_1m.parquet"
            original=file.read_bytes()
            file.write_bytes(original+b"changed")
            with self.assertRaisesRegex(ValueError,"checksum"):
                sj.daily_from_cache(cache,self.end,"TEST",DEP)
            file.write_bytes(original)
            meta=cache/"cache.json"
            original_meta=meta.read_bytes()
            changed=json.loads(original_meta);changed["identity"]["builder"]="unrecognized"
            meta.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError,"different builder"):
                sj.daily_from_cache(cache,self.end,"TEST",DEP)
            meta.write_bytes(original_meta)
            prints.loc[2,"price"]*=2
            prints.to_parquet(raw_path,index=False)
            with self.assertRaisesRegex(ValueError,"sources changed"):
                sj.daily_from_cache(cache,self.end,"TEST",DEP)

    def test_cache_empty_day_raw_gap_bounds_and_future_features(self):
        with test_directory() as folder:
            path=folder/"TEST-aggTrades-synthetic.parquet"
            prints=raw_prints(self.raw)
            prints.to_parquet(path,index=False)
            args=raw_args(folder,path)
            cut=int(self.raw.timestamp_ms.iloc[300])
            daily,_,_=sj.load_market(args,cut,DEP)
            # Mutating later raw prices rebuilds the cache but cannot change earlier features.
            prints.loc[prints.timestamp>=cut,"price"]*=3
            prints.to_parquet(path,index=False)
            after,_,_=sj.load_market(args,cut,DEP)
            pd.testing.assert_frame_equal(sj.features(daily,60),sj.features(after,60))
            args.output=args.cache_dir/"bad_run"
            with self.assertRaisesRegex(ValueError,"separate directories"):
                sj.load_market(args,cut,DEP)
            gap=folder/"TEST-aggTrades-gap.parquet"
            prints.drop(index=5).to_parquet(gap,index=False)
            with self.assertRaisesRegex(ValueError,"missing raw trades|Raw ID discontinuity"):
                sj.load_market(raw_args(folder,gap),cut,DEP)
            empty_day=int(self.raw.timestamp_ms.iloc[50])
            prints=prints.loc[(prints.timestamp<empty_day)|(prints.timestamp>=empty_day+sj.DAY)].copy()
            prints["agg_trade_id"]=np.arange(len(prints),dtype="int64")
            prints.to_parquet(path,index=False)
            with self.assertRaisesRegex(ValueError,"No observed prices"):
                sj.load_market(raw_args(folder,path),cut,DEP)

    def test_cache_current_and_allowlisted_legacy_compatibility(self):
        with test_directory() as folder:
            path=folder/'TEST-aggTrades-compat.parquet'
            raw_prints(self.raw).to_parquet(path,index=False)
            daily,info,_=sj.load_market(raw_args(folder,path),self.end,DEP)
            self.assertEqual(info['producer_version'],2)
            cache=Path(info['path']);meta=cache/'cache.json'
            original=json.loads(meta.read_text())
            legacy=json.loads(json.dumps(original))
            legacy['identity']['version']=1
            legacy['identity']['builder']=next(iter(DEP['cache'].LEGACY_BUILDERS))
            legacy['identity'].pop('id_policy',None)
            legacy['validation']='strict ordered contiguous aggregate IDs, positive finite price/qty'
            legacy.pop('source_continuity',None);legacy.pop('discontinuities',None)
            meta.write_text(json.dumps(legacy))
            restored,proof,_=sj.daily_from_cache(cache,self.end,'TEST',DEP)
            self.assertEqual(proof['builder_compatibility'],'allowlisted_legacy_v1')
            pd.testing.assert_frame_equal(daily,restored)
            legacy['identity']['builder']='unknown_legacy_producer'
            meta.write_text(json.dumps(legacy))
            with self.assertRaisesRegex(ValueError,'different builder'):
                sj.daily_from_cache(cache,self.end,'TEST',DEP)
            if getattr(DEP['cache'],'LEGACY_V2_BUILDERS',set()):
                prior_v2=json.loads(json.dumps(original))
                for builder in DEP['cache'].LEGACY_V2_BUILDERS:
                    prior_v2['identity']['builder']=builder
                    for validation in (original['validation'], 'ordered unique aggregate IDs; positive finite price/qty; explicit forward-ID-gap policy'):
                        prior_v2['validation']=validation
                        meta.write_text(json.dumps(prior_v2))
                        older,proof,_=sj.daily_from_cache(cache,self.end,'TEST',DEP)
                        self.assertEqual(proof['builder_compatibility'],'allowlisted_legacy_v2')
                        pd.testing.assert_frame_equal(daily,older)
            bad=json.loads(json.dumps(original));bad['validation']='unknown validation contract'
            meta.write_text(json.dumps(bad))
            with self.assertRaisesRegex(ValueError,'continuity is unverified'):
                sj.daily_from_cache(cache,self.end,'TEST',DEP)
            # A record-policy cache is usable only when its observed stream is contiguous.
            original['identity']['id_policy']='record'
            meta.write_text(json.dumps(original))
            sj.daily_from_cache(cache,self.end,'TEST',DEP)
            for field,value in [('source_continuity','unverified_discontinuities'),('discontinuities',[{'aggregate_id_step':2}])]:
                bad=json.loads(json.dumps(original));bad[field]=value
                meta.write_text(json.dumps(bad))
                with self.assertRaisesRegex(ValueError,'continuity is unverified'):
                    sj.daily_from_cache(cache,self.end,'TEST',DEP)
            bad=json.loads(json.dumps(original));bad.pop('source_continuity')
            meta.write_text(json.dumps(bad))
            with self.assertRaisesRegex(ValueError,'continuity is unverified'):
                sj.daily_from_cache(cache,self.end,'TEST',DEP)

    def test_raw_cli_and_cached_cli_match_direct_daily(self):
        with test_directory() as folder:
            path=folder/"TEST-aggTrades-synthetic.parquet"
            raw_prints(self.raw).to_parquet(path,index=False)
            common=["--instrument","TEST","--venue","synthetic","--product","linear",
                "--start",str(pd.to_datetime(self.start,unit="ms",utc=True)),"--end",str(pd.to_datetime(self.end,unit="ms",utc=True)),
                "--inspection-status","development","--train-days","100","--warmup-days","60","--restarts","2",
                "--jump-penalty","5","--refit-months","2"]
            sj.main(["--input",str(path),"--cache-dir",str(folder/"cache"),*common,"--output",str(folder/"raw_result")])
            saved=json.loads((folder/"raw_result/run.json").read_text())
            self.assertEqual(saved["status"],"complete")
            self.assertFalse(saved["market_cache"]["reused"])
            self.assertEqual(saved["base_timeframe"],"1m")
            self.assertEqual(saved["prepared_timeframe"],"1d")
            sj.main(["--candle-cache",saved["market_cache"]["path"],*common,"--output",str(folder/"cached_result")])
            cached=json.loads((folder/"cached_result/run.json").read_text())
            self.assertTrue(cached["market_cache"]["reused"])
            pd.testing.assert_frame_equal(pd.read_parquet(folder/"raw_result/regimes.parquet"),pd.read_parquet(folder/"cached_result/regimes.parquet"))


if __name__=="__main__":
    unittest.main()
