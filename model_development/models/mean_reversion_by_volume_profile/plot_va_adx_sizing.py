"""Review figures for existing v13 results; never recalibrate a policy."""
import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import observe_edge_sizing as sizing


def main(out):
    p = pd.read_csv(out/'sizing_predictions.csv')
    p = p.loc[p.target_cost_bps.eq(0)].sort_values('session_date')
    cohorts = pd.read_csv(out/'sizing_cohorts.csv')
    cohorts = cohorts.loc[cohorts.target_cost_bps.eq(0)&cohorts.cost_bps.eq(0)].set_index('cohort')
    fig,axes = plt.subplots(2,2,figsize=(13,8.5),constrained_layout=True)
    dates = pd.to_datetime(p.session_date)
    for ax,cost in zip(axes[0],[0,2]):
        for field,label,color in [('base_risk','VA only: fixed baseline','#8e99a3'),
                                 ('matched_risk','VA only: TRAIN-matched fixed','#425466'),
                                 ('sized_risk','VA + ADX sizing','#087f8c')]:
            contribution = p[field]/.005*(p.gross_r-cost*p.cost_r_per_bps)
            ax.plot(dates,contribution.cumsum(),label=label,color=color,lw=1.7)
        ax.axhline(0,color='black',lw=.6)
        ax.set(title='Gross' if cost==0 else '2 bps/side cost stress; same gross-trained weights',ylabel='Cumulative capital base-R')
        ax.grid(alpha=.15); ax.legend(fontsize=8); ax.tick_params(axis='x',rotation=20)
    delta = ((p.sized_risk-p.matched_risk)/.005*p.gross_r).groupby(p.fold).sum()
    axes[1,0].bar(delta.index,delta,color=np.where(delta>=0,'#087f8c','#b34e33'))
    axes[1,0].axhline(0,color='black',lw=.7)
    axes[1,0].set(title='Gross incremental allocation by test fold',xlabel='Original two-month fold',ylabel='VA + ADX minus TRAIN-matched fixed (base-R)')
    axes[1,0].grid(axis='y',alpha=.15)
    names=['lower','middle','higher']
    vals=cohorts.reindex(names)
    axes[1,1].bar(names,vals.original_mean_gross_r,color=['#8296a7','#bec9d2','#087f8c'])
    for i,r in enumerate(vals.itertuples()):
        axes[1,1].text(i,r.original_mean_gross_r+(0.009 if r.original_mean_gross_r>=0 else -.012),
            f'n={int(r.trades)}; WR {r.original_gross_wr_pct:.1f}%',ha='center',va='bottom' if r.original_mean_gross_r>=0 else 'top',fontsize=9)
    axes[1,1].set(title='Original trade quality by requested weight',ylabel='Mean original gross R')
    axes[1,1].set_ylim(-.1,.24); axes[1,1].axhline(0,color='black',lw=.7); axes[1,1].grid(axis='y',alpha=.15)
    fig.suptitle('XRP: previous-VA entry filter + ADX4h position weights\n387 identical entries; rolling 8+2-month historical development study',fontsize=14)
    fig.supxlabel('One capital base-R = 0.5% pre-trade equity; all policies capped at 5x entry notional. Closed-trade curves; costs hypothetical.',fontsize=9)
    fig.savefig(out/'comparison.png',dpi=160)
    plt.close(fig)
    yearly=[]
    for year,g in p.groupby(p.session_date.str[:4]):
        for policy,col in [('VA fixed baseline','base_risk'),('VA TRAIN-matched fixed','matched_risk'),('VA + ADX sizing','sized_risk')]:
            for cost in [0,1,2]:
                m = (g.multiplier if col=='sized_risk' else g.matched_requested_multiplier if col=='matched_risk' else np.ones(len(g)))
                yearly.append(dict(year=year,policy=policy,cost_bps=cost,**sizing.metrics(g,g[col],cost,m)))
    years=pd.DataFrame(yearly)
    years.to_csv(out/'yearly_metrics.csv',index=False)
    lines=['# Calendar-year diagnostic, gross-trained ADX weights','',
        '2022 covers September–December only; 2026 covers January–June only. No annualization. This is a post-run descriptive breakdown, not a new calibration or selection rule. One capital base-R = 0.5% pre-trade equity, with the same 5x notional cap. All comparisons keep the same VA entries.', '',
        '| Year | Entries | Fixed TRAIN-matched gross capital R | VA + ADX gross capital R |','|---|---:|---:|---:|']
    for year,g in years.loc[years.cost_bps.eq(0)].groupby('year'):
        b=g.loc[g.policy.eq('VA TRAIN-matched fixed')].iloc[0]
        s=g.loc[g.policy.eq('VA + ADX sizing')].iloc[0]
        lines.append(f'| {year} | {int(s.trades)} | {b.capital_net_base_r:+.3f} | {s.capital_net_base_r:+.3f} |')
    lines+=['','The allocation improves pooled gross returns, but the calendar-year results are uneven: 2025 is gross negative and the partial 2026 period is also gross negative. Positive incremental sizing contribution is not the same as an absolutely profitable model in every regime. Pooled gain should not be described as stable profitability.','']
    (out/'YEARLY.md').write_text('\n'.join(lines),encoding='utf-8')
    report=out/'REPORT.md'
    note='\nLihat [diagnostik tahun kalender](YEARLY.md) dan [grafik perbandingan](comparison.png). Tahun 2025 dan periode Januari–Juni 2026 tetap negatif secara gross pada kombinasi; perbaikan relatif sizing tidak berarti profit stabil.\n'
    if '[diagnostik tahun kalender]' not in report.read_text(encoding='utf-8'):
        report.write_text(report.read_text(encoding='utf-8')+note,encoding='utf-8')
    (out/'figure_receipt.json').write_text(json.dumps(dict(code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        rows=len(p),targets='gross-trained weights only',figure='comparison.png'),indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    main(parser.parse_args().run)
