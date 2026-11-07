"""Independent reconciliation, raw spot checks and reused statistical controls for v20."""
import argparse
import hashlib
import inspect
import json
from pathlib import Path

import numpy as np
import pandas as pd

from validate_half_pullback import metrics, compare, stamps
import observe as raw
from observe_va_orderflow import controls as existing_controls

ACTIVE = ["tp_first","sl_first","time_exit"]
MODEL = ["symbol","timeframe","setup"]
EVENT = ["symbol","session_date","timeframe","signal_bar_start_ny","touch_edge"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def policies(p):
    return dict(base_all=p,
        absorption_matched=p.loc[p.absorption_observable.eq(True) & p.absorption_supported.eq(True)],
        absorption=p.loc[p.absorption_selected.eq(True)],
        exhaustion_matched=p.loc[p.exhaustion_observable.eq(True)],
        exhaustion=p.loc[p.exhaustion_selected.eq(True)])


def raw_spotchecks(ledger, root, cfg, params):
    choices = []
    for _, p in ledger.loc[ledger.absorption_calibratable.eq(True)].groupby(MODEL):
        p = p.sort_values(["session_date","signal_bar_start_ny"])
        choices += [p.iloc[0].to_dict(),p.iloc[-1].to_dict()]
    chosen = pd.DataFrame(choices).drop_duplicates(EVENT)
    results = []
    for symbol, group in chosen.groupby("symbol"):
        f, mins, maxs = raw.metadata(root/cfg["assets"][symbol])
        for r in group.to_dict("records"):
            start = pd.Timestamp(r["signal_bar_start_ny"]).value//1000000
            end = pd.Timestamp(r["signal_bar_end_ny"]).value//1000000
            width = end-start
            groups = np.flatnonzero((maxs >= start-2*width) & (mins < end)).tolist()
            table = f.read_row_groups(groups,columns=["timestamp","price","qty","agg_trade_id","is_buyer_maker"])
            ts,p,q,ids,maker = [table[c].combine_chunks().to_numpy(zero_copy_only=False) for c in table.column_names]
            left,right = np.searchsorted(ts,[start,end])
            hit = np.flatnonzero(ids == int(r["boundary_contact_id"]))
            assert len(hit) == 1
            hit = int(hit[0])
            assert left <= hit < right and ts[hit] >= pd.Timestamp(r["profile_end_ny"]).value//1000000
            level = r[r["touch_edge"]]
            distance = abs(level-r["poc"])
            direction = 1 if r["touch_edge"] == "vah" else -1
            ix = np.arange(hit,right)
            side = ~maker[ix] if direction == 1 else maker[ix]
            ix = ix[side & (np.abs(p[ix]-level) <= params["zone_fraction"]*distance)]
            elapsed = ts[right-1]-ts[hit]
            volume = float(q[ix].sum())
            assert np.isclose(volume,r["outward_zone_volume"],atol=1e-10)
            assert np.isclose(volume*60000/elapsed,r["abs_pressure_rate"],atol=1e-10)
            usable = ix[(ts[right-1]-ts[ix] >= params["response_fraction"]*width)
                        & (right-ix-1 >= params["minimum_later_prints"])]
            assert len(usable)
            event = int(usable[np.argmax(p[usable]*q[usable])])
            assert ids[event] == int(r["abs_large_id"])
            assert np.isclose(p[event]*q[event],r["abs_large_notional"],atol=1e-10)
            progress = max(0.,float(np.max(direction*(p[event+1:right]-p[event]))))/distance
            assert np.isclose(progress,r["abs_progress"],atol=1e-12)
            volumes, extremes = [],[]
            for begin in (start-2*width,start-width,start):
                a,b = np.searchsorted(ts,[begin,begin+width])
                assert a < b
                mask = ~maker[a:b] if direction == 1 else maker[a:b]
                volumes.append(float(q[a:b][mask].sum()))
                extremes.append(float(p[a:b].max() if direction == 1 else p[a:b].min()))
            flag = all(direction*(extremes[i+1]-extremes[i]) > 0 for i in (0,1)) and volumes[0] > volumes[1] > volumes[2]
            assert bool(r["exhaustion_signal"]) == flag
            for i in range(3):
                assert np.isclose(r["exhaustion_volume_"+str(i)],volumes[i],atol=1e-10)
            results.append(dict(symbol=symbol,timeframe=r["timeframe"],setup=r["setup"],session_date=r["session_date"],
                signal_bar_start_ny=r["signal_bar_start_ny"],abs_large_id=int(ids[event]),status="passed"))
        f.close()
    return pd.DataFrame(results)


def validate(run, first_source, second_source, root):
    audit = json.loads((run/"audit.json").read_text())
    assert audit["status"] == "passed" and audit["profiles_built"] == 0
    for name,digest in audit["outputs"].items():
        assert sha(run/name) == digest,name
    snap = json.loads((run/"config_snapshot.json").read_text())
    cfg,params = snap["config"],snap["parameters"]
    assert sha(first_source/"audit.json") == snap["first_audit_sha256"]
    assert sha(second_source/"audit.json") == snap["second_audit_sha256"]
    ledger = pd.read_parquet(run/"feature_ledger.parquet")
    predictions = pd.read_parquet(run/"test_predictions.parquet")
    cuts = pd.read_csv(run/"train_cutpoints.csv")
    folds = pd.read_csv(run/"fold_metrics.csv")
    summary = pd.read_csv(run/"summary.csv")
    original = pd.read_csv(first_source/"sessions.csv",float_precision="round_trip")
    original = original.loc[original.entry_mode.eq("body_close") & original.timeframe.isin(["15m","30m","1h"])]
    joined = ledger.merge(original,on=["symbol","session_date","timeframe"],suffixes=("","_v17"),validate="many_to_one")
    assert len(joined) == len(ledger) == 3*len(original)
    assert not ledger.duplicated(MODEL+["session_date"]).any()
    assert joined.valid_session.equals(joined.valid_session_v17) and joined.opening_eligible.equals(joined.opening_eligible_v17)
    for name in ("poc","val","vah","opening_price"):
        np.testing.assert_allclose(joined[name],joined[name+"_v17"],equal_nan=True,atol=1e-12)
    active = ledger.loc[ledger.status.isin(ACTIVE)]
    assert (active.feature_available_ms.to_numpy() <= stamps(active.entry_timestamp_ny)).all()
    sign = np.where(active.touch_edge.eq("val"),1,-1)
    np.testing.assert_allclose(active.tp,active.poc,atol=1e-12)
    np.testing.assert_allclose(active.sl,2*active.entry-active.poc,atol=1e-12)
    np.testing.assert_allclose(active.risk_price,np.abs(active.entry-active.poc),atol=1e-12)
    gross = sign*(active.exit_price-active.entry)/active.risk_price
    fee = cfg["fee_bps_per_side"]*.0001*(active.entry+active.exit_price)/active.risk_price
    np.testing.assert_allclose(active.gross_r,gross,atol=1e-10)
    np.testing.assert_allclose(active.net_r,gross-fee,atol=1e-10)
    assert (stamps(active.entry_timestamp_ny) >= stamps(active.signal_bar_end_ny)).all()
    assert (stamps(active.exit_timestamp_ny) < stamps(active.period_end_ny)).all()
    observed = ledger.loc[ledger.absorption_calibratable.eq(True)]
    assert (stamps(observed.abs_large_timestamp_ny) < observed.feature_available_ms).all()
    assert observed.abs_response_prints.ge(params["minimum_later_prints"]).all()
    widths = observed.timeframe.map({"15m":900000,"30m":1800000,"1h":3600000})
    assert (observed.abs_response_observed_ms >= params["response_fraction"]*widths).all()
    for c in cuts.to_dict("records"):
        boundary = pd.Timestamp(c["test_start"],tz="America/New_York").value//1000000
        a = ledger.loc[ledger.symbol.eq(c["symbol"]) & ledger.timeframe.eq(c["timeframe"])
            & ledger.session_date.ge(c["train_start"]) & ledger.session_date.lt(c["test_start"])
            & ledger.feature_available_ms.lt(boundary) & ledger.absorption_calibratable.eq(True)].drop_duplicates(EVENT)
        assert len(a) == c["train_distinct_events"]
        assert bool(c["supported"]) == (len(a) >= params["minimum_train_events"])
        if c["supported"]:
            expected = [np.median(a.abs_pressure_rate),np.quantile(a.abs_large_notional,.75),
                        min(np.median(a.abs_progress),params["progress_cap"])]
            np.testing.assert_allclose(expected,[c["pressure_cut"],c["size_cut"],c["progress_cut"]],atol=1e-10)
    p = predictions.merge(cuts[["symbol","timeframe","fold","supported","pressure_cut","size_cut","progress_cut"]],
        on=["symbol","timeframe","fold"],suffixes=("","_check"),validate="many_to_one")
    selection = p.supported & p.absorption_calibratable.eq(True) & p.abs_pressure_rate.ge(p.pressure_cut_check)
    selection &= p.abs_large_notional.ge(p.size_cut_check) & p.abs_progress.le(p.progress_cut_check)
    assert selection.equals(p.absorption_selected)
    assert p.exhaustion_signal.equals(p.exhaustion_selected)
    checked = 0
    for key,g in predictions.groupby(MODEL):
        q = summary
        for name,value in zip(MODEL,key):
            q = q.loc[q[name].eq(value)]
        for policy,part in policies(g).items():
            compare(q.loc[q.policy.eq(policy)].iloc[0],metrics(part,cfg))
            checked += 1
    for meta in folds.to_dict("records"):
        g = ledger
        for name in MODEL:
            g = g.loc[g[name].eq(meta[name])]
        start = meta["train_start"] if meta["phase"] == "train" else meta["test_start"]
        finish = meta["test_start"] if meta["phase"] == "train" else meta["test_end_exclusive"]
        population = g.loc[g.valid_session & g.session_date.ge(start) & g.session_date.lt(finish)]
        assert len(population) == meta["valid_sessions"]
        part = population.loc[population.status.isin(ACTIVE)].copy()
        if meta["phase"] == "train" and len(part):
            boundary = pd.Timestamp(meta["test_start"],tz="America/New_York").value//1000000
            part = part.loc[stamps(part.exit_timestamp_ny) < boundary]
        c = cuts.loc[cuts.symbol.eq(meta["symbol"]) & cuts.timeframe.eq(meta["timeframe"]) & cuts.fold.eq(meta["fold"])].iloc[0]
        part["absorption_supported"] = bool(c.supported)
        part["absorption_selected"] = (part.absorption_calibratable.eq(True) & c.supported
            & part.abs_pressure_rate.ge(c.pressure_cut) & part.abs_large_notional.ge(c.size_cut) & part.abs_progress.le(c.progress_cut))
        part["exhaustion_selected"] = part.exhaustion_signal.eq(True)
        compare(meta,metrics(policies(part)[meta["policy"]],cfg))
        checked += 1
    prior = pd.read_csv(second_source/"summary.csv")
    old_tests = prior.loc[prior["sample"].eq("walk_forward_test")]
    second_checked = 0
    for r in old_tests.to_dict("records"):
        a = summary.loc[summary.symbol.eq(r["symbol"]) & summary.timeframe.eq(r["timeframe"])
            & summary.setup.eq("second_"+r["pullback_mode"]) & summary.policy.eq("base_all")].iloc[0]
        for name in ("trades","gross_mean_r","net_mean_r","gross_total_r","net_total_r","net_account_return_pct"):
            assert np.isclose(a[name],r[name],atol=1e-9),name
        second_checked += 1
    probes = raw_spotchecks(ledger,root,cfg,params)
    probes.to_csv(run/"raw_feature_spotchecks.csv",index=False)
    result = dict(status="passed",rows=len(ledger),baseline_trade_geometry_checked=len(active),
        calibrations_checked=len(cuts),test_classifications_checked=len(predictions),performance_rows_reconciled=checked,
        v19_second_baselines_reconciled=second_checked,raw_feature_windows_checked=len(probes),
        first_audit_sha256=sha(first_source/"audit.json"),second_audit_sha256=sha(second_source/"audit.json"),run_audit_sha256=sha(run/"audit.json"))
    (run/"VALIDATION.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)
    return predictions,folds


def statistical_controls(predictions,folds,repeats=2000):
    results = []
    for number,(key,g) in enumerate(predictions.groupby(MODEL,sort=True)):
        for offset,policy in enumerate(("absorption","exhaustion")):
            matched = policies(g)[policy+"_matched"].copy()
            parent = dict(zip(MODEL,key),policy=policy)
            if matched.empty:
                results.append(dict(**parent,status="no_matched_trades"))
                continue
            matched["feature_status"] = "valid"
            matched["selected"] = matched[policy+"_selected"]
            seed = 20261025+2*number+offset
            f = folds.loc[folds.symbol.eq(key[0]) & folds.timeframe.eq(key[1]) & folds.setup.eq(key[2])
                & folds.phase.eq("test") & folds.policy.eq(policy+"_matched")]
            result = existing_controls(matched,f,repeats=repeats,seed=seed).iloc[0].to_dict()
            result.pop("symbol",None)
            results.append(dict(**parent,**result,seed=seed,matched_trades=len(matched),selected_trades=int(matched.selected.sum())))
    return pd.DataFrame(results)


def diagnostics(run,predictions,folds):
    control = statistical_controls(predictions,folds)
    control.to_csv(run/"controls.csv",index=False)
    stability = []
    for key,p in folds.groupby(MODEL+["policy"]):
        tr = p.loc[p.phase.eq("train")].set_index("fold")
        te = p.loc[p.phase.eq("test")].set_index("fold")
        joint = te[["trades","net_mean_r"]].join(tr[["trades","net_mean_r"]],rsuffix="_train")
        available = joint.loc[joint.trades.gt(0) & joint.trades_train.gt(0)]
        stability.append(dict(zip(MODEL+["policy"],key),total_folds=len(te),
            nonempty_test_folds=int(te.trades.gt(0).sum()),positive_net_test_folds=int(te.net_mean_r.gt(0).sum()),
            comparable_folds=len(available),train_test_both_positive=int((available.net_mean_r.gt(0)&available.net_mean_r_train.gt(0)).sum())))
    pd.DataFrame(stability).to_csv(run/"stability.csv",index=False)
    names = ["VALIDATION.json","raw_feature_spotchecks.csv","controls.csv","stability.csv"]
    result = dict(status="passed",control_rows=len(control),repeats=2000,block_days=7,multiple_comparison_adjusted=False,
        statistics_reused_from="observe_va_orderflow.controls (statistics only)",
        statistics_helper_sha256=sha(Path(inspect.getfile(existing_controls))),validator_sha256=sha(Path(__file__)),
        outputs={name:sha(run/name) for name in names})
    (run/"diagnostics_audit.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",type=Path,required=True)
    parser.add_argument("--first-source",type=Path,required=True)
    parser.add_argument("--second-source",type=Path,required=True)
    parser.add_argument("--root",type=Path,required=True)
    args = parser.parse_args()
    diagnostics(args.run,*validate(args.run,args.first_source,args.second_source,args.root))
