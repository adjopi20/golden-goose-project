"""Distribution comparison of the existing v09 ADX4h selections; no refitting."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

KEYS = ["symbol", "profile", "session_date"]
FEATURE = "macro_4h_adx"
PROFILE = "ny_0930_1300"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe(a, b):
    return float(a / b) if b else np.nan


def distribution(values):
    x = np.asarray(values, dtype=float)
    if not len(x):
        return {"n": 0}
    unique, count = np.unique(x, return_counts=True)
    top = count.max()
    mode = unique[count == top].tolist() if top > 1 else []
    trimmed = np.sort(x)
    trim = int(len(x)*.05)
    trimmed = trimmed[trim:len(x)-trim] if trim else trimmed
    pos, neg = x[x > 1e-12], x[x < -1e-12]
    curve = np.r_[0., np.cumsum(x)]
    return dict(n=len(x), mean=x.mean(), median=np.median(x), mode=json.dumps(mode),
        mode_count=int(top), std=np.std(x, ddof=1) if len(x)>1 else np.nan,
        skew=float(pd.Series(x).skew()), excess_kurtosis=float(pd.Series(x).kurt()),
        trimmed_mean_5pct=trimmed.mean(), minimum=x.min(), maximum=x.max(),
        **{f"q{q:02d}": np.quantile(x, q/100) for q in [5,10,25,50,75,90,95]},
        worst_decile_mean=x[x <= np.quantile(x,.1)].mean(),
        wins=len(pos), losses=len(neg), breakevens=len(x)-len(pos)-len(neg),
        win_rate_pct=100*len(pos)/len(x), avg_win=pos.mean() if len(pos) else np.nan,
        avg_loss=neg.mean() if len(neg) else np.nan, total=x.sum(),
        profit_factor=safe(pos.sum(),-neg.sum()), payoff_ratio=safe(pos.mean(),-neg.mean()) if len(pos) and len(neg) else np.nan,
        max_drawdown=float(np.max(np.maximum.accumulate(curve)-curve)))


def streak(mask):
    best = current = 0
    for x in mask:
        current = current+1 if x else 0
        best = max(best, current)
    return best


def equity(values):
    v = np.asarray(values, dtype=float)
    assert np.all(v > -1)
    path = np.r_[1., np.cumprod(1+v)]
    dd = 1 - path / np.maximum.accumulate(path)
    return dict(compounded_return_pct=100*(path[-1]-1), max_drawdown_pct=100*dd.max())


def metrics(frame):
    r, p = distribution(frame.gross_r), distribution(frame.position_return_pct)
    result = {"trades": len(frame)}
    if not len(frame):
        return result
    for prefix, vals in [("r",r), ("pct",p)]:
        result.update({f"{prefix}_{k}":v for k,v in vals.items()})
    result.update({f"account_1pct_risk_{k}":v for k,v in equity(frame.gross_r*.01).items()})
    result.update({f"notional_1x_{k}":v for k,v in equity(frame.position_return_pct/100).items()})
    tp, sl, timed = [int(frame.status.eq(s).sum()) for s in ["tp_first","sl_first","time_exit"]]
    result.update(tp_first=tp,sl_first=sl,time_exit=timed,tp_pct=100*tp/len(frame),sl_pct=100*sl/len(frame),
        time_exit_pct=100*timed/len(frame),resolved_tp_pct=100*safe(tp,tp+sl),
        max_consecutive_wins=streak(frame.gross_r.gt(1e-12)), max_consecutive_losses=streak(frame.gross_r.lt(-1e-12)),
        risk_distance_mean_pct=frame.risk_bps.mean()/100, risk_distance_median_pct=frame.risk_bps.median()/100,
        hold_mean_minutes=frame.hold_minutes.mean(), hold_median_minutes=frame.hold_minutes.median(),
        long_entries=int(frame.side.eq(1).sum()),short_entries=int(frame.side.eq(-1).sum()))
    return result


def uncertainty(base, schedule, rng, repeats):
    # Same number retained in each fold, preserving the observed temporal allocation.
    r, p = base.gross_r.to_numpy(), base.position_return_pct.to_numpy()
    kept = base.selected.to_numpy(dtype=bool)
    null_mask = np.zeros((repeats,len(base)),dtype=bool)
    sampled = np.zeros((repeats,8), dtype=float)
    for fold, g in base.groupby("fold"):
        positions = base.index.get_indexer(g.index)
        n_keep = int(g.selected.sum())
        if n_keep:
            order = rng.random((repeats,len(g))).argsort(axis=1)[:,:n_keep]
            null_mask[np.arange(repeats)[:,None],positions[order]] = True
        meta = schedule.loc[schedule.fold.eq(fold)].iloc[0]
        dates = pd.date_range(meta.test_start,pd.Timestamp(meta.test_end_exclusive)-pd.Timedelta(days=1),freq="D")
        k = g.selected.astype(float)
        components = pd.DataFrame(dict(br=g.gross_r,kr=g.gross_r*k,bn=1.,kn=k,
            bw=g.gross_r.gt(1e-12).astype(float),kw=g.gross_r.gt(1e-12).astype(float)*k,
            bp=g.position_return_pct,kp=g.position_return_pct*k),index=g.index)
        components.index=pd.to_datetime(g.session_date)
        daily=components.reindex(dates,fill_value=0).to_numpy()
        days=len(daily)
        starts=rng.integers(days,size=(repeats,int(np.ceil(days/7)),1))
        indices=((starts+np.arange(7))%days).reshape(repeats,-1)[:,:days]
        sampled += daily[indices].sum(axis=1)
    n = int(kept.sum())
    null_r = (null_mask*r).sum(axis=1)/n
    null_p = (null_mask*p).sum(axis=1)/n
    null_wr = 100*(null_mask*(r>1e-12)).sum(axis=1)/n
    curve=np.concatenate([np.zeros((repeats,1)),np.cumsum(null_mask*r,axis=1)],axis=1)
    null_dd=(np.maximum.accumulate(curve,axis=1)-curve).max(axis=1)
    observed=dict(mean_r=r[kept].mean(), mean_pct=p[kept].mean(), wr_pct=100*(r[kept]>1e-12).mean(),max_dd_r=distribution(r[kept])["max_drawdown"])
    result=dict(repeats=repeats,block_calendar_days=7)
    for name, draws in [("mean_r",null_r),("mean_pct",null_p),("wr_pct",null_wr),("max_dd_r",null_dd)]:
        result.update({f"random_{name}_p025":np.quantile(draws,.025),f"random_{name}_p975":np.quantile(draws,.975),
                       f"random_{name}_tail_fraction":(1+(draws<=observed[name] if name=="max_dd_r" else draws>=observed[name]).sum())/(repeats+1)})
    br,kr,bn,kn,bw,kw,bp,kp=sampled.T
    good=(kn>0)&(bn>kn)
    br,kr,bn,kn,bw,kw,bp,kp=[v[good] for v in [br,kr,bn,kn,bw,kw,bp,kp]]
    contrasts=dict(mean_r_kept_minus_base=kr/kn-br/bn,mean_r_kept_minus_discarded=kr/kn-(br-kr)/(bn-kn),
        mean_pct_kept_minus_base=kp/kn-bp/bn,wr_pp_kept_minus_base=100*(kw/kn-bw/bn),
        total_r_delta_per_base_opportunity=(kr-br)/bn)
    for name, draws in contrasts.items():
        result.update({f"bootstrap_{name}_ci025":np.quantile(draws,.025),f"bootstrap_{name}_ci975":np.quantile(draws,.975)})
    result["bootstrap_valid_replicates"]=int(good.sum())
    return result


def fmt(x, digits=3):
    return f"{float(x):.{digits}f}" if pd.notna(x) else "—"


def write_report(out, summary, robust, folds):
    lines=["# ADX 4h: base versus retained trade quality", "",
        "Reanalysis of the existing v09 predictions, today's 09:30–pre13:00 NY POC only. No new rule, training or selection. "
        "ADX category was selected in each original 8-month train and locked for its next 2-month test. Categories change by fold.",
        "Base = exactly the old matched test entries. Kept is a subset of base; discarded is its complement. Previously inspected historical evidence, before fees/slippage/funding. "
        "The original model scores TP/SL at their barriers; these are modeled returns, not actual execution fills.", "",
        "Position return % = gross R × risk_price / entry × 100. This is signed linear-notional price P&L at 1x, not account return. "
        "The 1%-risk illustration compounds R × 1% on current equity; no sizing, leverage or execution constraints are imposed. Assets are separate, not a pooled portfolio.", ""]
    selected_metrics=[("trades","Trades",0),("r_win_rate_pct","WR incl. time exits %",2),("r_mean","Mean R",4),
        ("r_median","Median R",4),("r_mode","Mode R",None),("r_trimmed_mean_5pct","5%-trimmed mean R",4),
        ("r_std","Std R",4),("r_q25","25th percentile R",4),("r_q75","75th percentile R",4),
        ("r_avg_win","Average winning R",4),("r_avg_loss","Average losing R",4),("r_profit_factor","Profit factor in R",3),
        ("r_payoff_ratio","Payoff in R",3),("r_total","Total R",3),("r_max_drawdown","Max drawdown R",3),
        ("max_consecutive_losses","Max consecutive losing trades",0),("tp_first","TP first",0),("sl_first","SL first",0),
        ("time_exit","Time exits",0),("resolved_tp_pct","TP / resolved TP+SL %",2),
        ("pct_mean","Mean position return %",4),("pct_median","Median position return %",4),("pct_std","Std position return pp",4),
        ("pct_avg_win","Average winning position return %",4),("pct_avg_loss","Average losing position return %",4),
        ("pct_profit_factor","Profit factor in position %",3),("risk_distance_mean_pct","Mean stop/target distance %",4),
        ("hold_median_minutes","Median holding minutes",2),("account_1pct_risk_compounded_return_pct","Illustration: compounded account return, risk 1%",3),
        ("account_1pct_risk_max_drawdown_pct","Illustration: account max drawdown, risk 1%",3),
        ("notional_1x_compounded_return_pct","Illustration: compounded return, 100% notional at 1x",3),
        ("notional_1x_max_drawdown_pct","Illustration: max drawdown, 100% notional at 1x",3)]
    for symbol,g in summary.groupby("symbol",sort=True):
        d=g.set_index("cohort")
        lines += [f"## {symbol}","","| Metric | Base | Kept | Discarded |","|---|---:|---:|---:|"]
        for key,label,digits in selected_metrics:
            values=[str(d.loc[c,key]) if digits is None else fmt(d.loc[c,key],digits) for c in ["base","kept","discarded"]]
            lines.append("| "+" | ".join([label]+values)+" |")
        b,k=d.loc["base"],d.loc["kept"]
        lines += ["",f"Retained {100*k.trades/b.trades:.2f}% of entries; {100-100*k.trades/b.trades:.2f}% discarded. "
            f"Kept total R / base total R: {100*k.r_total/b.r_total:.1f}% (unstable when base total R is near zero).", ""]
        u=robust.loc[robust.symbol.eq(symbol)].iloc[0]
        lines += ["| Quality difference: kept minus base | Estimate | 95% seven-day block interval |", "|---|---:|---:|"]
        for name,estimate in [("mean_r",k.r_mean-b.r_mean),("mean_pct",k.pct_mean-b.pct_mean),("wr_pp",k.r_win_rate_pct-b.r_win_rate_pct)]:
            lo=u[f"bootstrap_{name}_kept_minus_base_ci025"];hi=u[f"bootstrap_{name}_kept_minus_base_ci975"]
            lines.append(f"| {name} | {estimate:+.4f} | [{lo:+.4f}, {hi:+.4f}] |")
        lines += ["",f"Same-frequency random control preserves the exact retained count in each fold: "
            f"mean-R upper-tail fraction {u.random_mean_r_tail_fraction:.4f}; "
            f"position-% upper-tail fraction {u.random_mean_pct_tail_fraction:.4f}. These are descriptive, uncorrected for the earlier hypothesis search.", ""]
    lines += ["## How to read this", "",
        "Mean R is quality per unit of planned risk. Position percentage also depends on stop/target distance, so an R improvement need not imply larger raw price returns.",
        "Median/mode alone are weak here: TP and SL are fixed at +1R/-1R. Percent returns are usually continuous and often have no repeated exact mode; [] in the CSV means no repeated mode.",
        "Smaller drawdown and fewer losses in a row can follow mechanically from taking fewer trades. The equal-frequency random drawdown control is in robustness.csv; all confidence intervals preserve the base/subset dependence.",
        "Block bootstrap resamples seven-calendar-day blocks separately inside each original scored fold. Empty days count as zero observations; ratios use trade counts. "
        "Random control samples without replacement within each fold, keeping its original number of selected trades. These checks do not create an untouched holdout or correct for trying many indicators.",
        "Complete numerical details: metrics.csv, distribution.csv, cohort_trades.csv, fold_metrics.csv, month_metrics.csv, side_metrics.csv, robustness.csv, and coverage.csv. "
        "MAE/MFE and mark-to-market intratrade equity are not available from these endpoint ledgers; drawdowns are measured at closed trades. "
        "No annualized return or Sharpe is asserted for a continuous live strategy from these incomplete matched test windows.", ""]
    (out/"REPORT.md").write_text("\n".join(lines),encoding="utf-8")


def plot(out, data):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(3,2,figsize=(13,10),constrained_layout=True)
    for row,(symbol,g) in enumerate(data.groupby("symbol",sort=True)):
        for cohort,color in [("base","#708090"),("kept","#137f72"),("discarded","#c17a32")]:
            q=g if cohort=="base" else g.loc[g.selected.eq(cohort=="kept")]
            axes[row,0].plot(pd.to_datetime(q.session_date),q.gross_r.cumsum(),label=f"{cohort} (n={len(q)})",color=color)
            x=np.sort(q.position_return_pct.to_numpy())
            axes[row,1].step(x,np.arange(1,len(x)+1)/len(x),where="post",label=cohort,color=color)
        axes[row,0].set(title=f"{symbol}: cumulative gross R",ylabel="R")
        axes[row,1].set(title=f"{symbol}: position-return distribution",xlabel="Modeled position return (%)",ylabel="Cumulative fraction")
        for ax in axes[row]:
            ax.axhline(0,color="black",alpha=.2);ax.grid(alpha=.15);ax.legend(fontsize=8)
        axes[row,0].tick_params(axis="x",rotation=25)
    fig.suptitle("Existing ADX4h selections: matched base / retained / discarded; gross retrospective results")
    fig.savefig(out/"quality_comparison.png",dpi=160)
    plt.close(fig)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--repeats",type=int,default=10000)
    args=p.parse_args()
    if args.output.exists():
        raise ValueError("Preserve old reports: use a new output directory")
    args.output.mkdir(parents=True)
    names=["test_predictions.csv","context_ledger.csv","selected_folds.csv","aggregate.csv","audit.json"]
    audit=json.loads((args.source/"audit.json").read_text())
    assert audit["status"]=="passed"
    pred=pd.read_csv(args.source/names[0])
    pred=pred.loc[pred.feature.eq(FEATURE)&pred.profile.eq(PROFILE)].copy()
    ledger=pd.read_csv(args.source/names[1])
    cols=KEYS+["entry","side","risk_price","risk_bps","entry_timestamp_ny","exit_timestamp_ny","gross_r","status"]
    data=pred.merge(ledger[cols],on=KEYS,validate="one_to_one",suffixes=("","_ledger"))
    assert len(data)==len(pred) and not data.duplicated(KEYS).any()
    assert data.status.eq(data.status_ledger).all()
    np.testing.assert_allclose(data.gross_r,data.gross_r_ledger)
    np.testing.assert_allclose(data.risk_bps,10000*data.risk_price/data.entry)
    assert data.entry.gt(0).all() and data.risk_price.gt(0).all()
    data["position_return_pct"]=data.gross_r*data.risk_bps/100
    data["hold_minutes"]=(pd.to_datetime(data.exit_timestamp_ny,utc=True,format="ISO8601")-pd.to_datetime(data.entry_timestamp_ny,utc=True,format="ISO8601")).dt.total_seconds()/60
    assert data.hold_minutes.ge(0).all() and data.hold_minutes.le(240).all()
    data=data.sort_values(["symbol","session_date"]).reset_index(drop=True)
    data.to_csv(args.output/"cohort_trades.csv",index=False)
    folds=pd.read_csv(args.source/names[2])
    folds=folds.loc[folds.feature.eq(FEATURE)&folds.profile.eq(PROFILE)]
    aggregate=pd.read_csv(args.source/names[3])
    aggregate=aggregate.loc[aggregate.feature.eq(FEATURE)&aggregate.profile.eq(PROFILE)].set_index("symbol")
    summaries,distributions,foldrows,monthly_rows,siderows,robust,coverage=[],[],[],[],[],[],[]
    rng=np.random.default_rng(20261013)
    for symbol,g in data.groupby("symbol",sort=True):
        a=aggregate.loc[symbol]
        assert len(g)==a.matched_baseline_entries and g.selected.sum()==a.selected_entries
        np.testing.assert_allclose(g.gross_r.mean(),a.baseline_mean_r)
        np.testing.assert_allclose(g.loc[g.selected,"gross_r"].mean(),a.mean_r)
        meta=folds.loc[folds.symbol.eq(symbol)&folds.selected_category.notna()]
        for cohort,part in [("base",g),("kept",g.loc[g.selected]),("discarded",g.loc[~g.selected])]:
            summaries.append(dict(symbol=symbol,cohort=cohort,**metrics(part)))
            for unit,col in [("R","gross_r"),("position_pct","position_return_pct")]:
                distributions.append(dict(symbol=symbol,cohort=cohort,unit=unit,**distribution(part[col])))
            for fold,b in g.groupby("fold"):
                q=part.loc[part.fold.eq(fold)]
                foldrows.append(dict(symbol=symbol,cohort=cohort,fold=fold,base_entries=len(b),**metrics(q)))
            for month,b in part.groupby(part.session_date.str[:7]):
                monthly_rows.append(dict(symbol=symbol,cohort=cohort,month=month,**metrics(b)))
            for side,b in part.groupby("side"):
                siderows.append(dict(symbol=symbol,cohort=cohort,side=side,**metrics(b)))
        robust.append(dict(symbol=symbol,**uncertainty(g,meta,rng,args.repeats)))
        coverage.append(dict(symbol=symbol,first_entry=g.session_date.min(),last_entry=g.session_date.max(),scored_folds=g.fold.nunique(),
            valid_sessions_scored_folds=meta.valid_test_sessions.sum(),all_baseline_entries_scored_folds=meta.all_baseline_test_entries.sum(),
            matched_base_entries=len(g),selected_entries=int(g.selected.sum()),limited_selected_folds=int(meta.evidence.eq("limited").sum())))
    summary=pd.DataFrame(summaries);u=pd.DataFrame(robust)
    for name,frame in [("metrics",summary),("distribution",pd.DataFrame(distributions)),("fold_metrics",pd.DataFrame(foldrows)),
        ("month_metrics",pd.DataFrame(monthly_rows)),("side_metrics",pd.DataFrame(siderows)),("robustness",u),("coverage",pd.DataFrame(coverage))]:
        frame.to_csv(args.output/f"{name}.csv",index=False)
    # Independent reconciliation of the subset/complement accounting.
    for symbol,g in summary.groupby("symbol"):
        s=g.set_index("cohort")
        assert s.loc["base","trades"]==s.loc["kept","trades"]+s.loc["discarded","trades"]
        for key in ["r_total","pct_total","r_wins","r_losses","tp_first","sl_first","time_exit"]:
            np.testing.assert_allclose(s.loc["base",key],s.loc["kept",key]+s.loc["discarded",key],atol=1e-10)
    write_report(args.output,summary,u,folds)
    plot(args.output,data)
    receipt=dict(status="passed",recorded="2026-10-13",feature=FEATURE,profile=PROFILE,prior_inspection=True,
        seed=20261013,repeats=args.repeats,source=str(args.source.resolve()),script_sha256=sha(__file__),
        inputs={n:sha(args.source/n) for n in names},
        checks=["unchanged source predictions/outcomes","original aggregate counts/means","risk-price percentage identity",
                "subset plus discarded equals base","exit times within four hours","paired calendar-block and fold-stratified random controls"])
    receipt["outputs"]={p.name:sha(p) for p in args.output.iterdir() if p.is_file()}
    (args.output/"receipt.json").write_text(json.dumps(receipt,indent=2),encoding="utf-8")
    print(summary[["symbol","cohort","trades","r_win_rate_pct","r_mean","pct_mean","r_total","r_max_drawdown"]].to_string(index=False))
    print(u.to_string(index=False))


if __name__=="__main__":
    main()
