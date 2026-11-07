"""Combine saved previous-VA entry selections with saved ADX4h sizing scores."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd

import observe_edge_sizing as sizing
from observe_edge_context import classify

SYMBOL = 'XRPUSDT'
PROFILE = 'ny_0930_1300'
VA_FEATURE = 'value_previous_failed_attempts'
KEYS = ['symbol', 'profile', 'session_date']


def restrict(frame):
    return frame.loc[frame.symbol.eq(SYMBOL) & frame.profile.eq(PROFILE)].copy()


def gate(frame, va_artifact):
    known = frame.dropna(subset=[VA_FEATURE])
    labels = classify(known, VA_FEATURE, va_artifact['calibration'])
    return known.loc[labels.eq(va_artifact['selected_category'])].copy()


def allocate(train, test, va_artifact, adx_artifact):
    """All selections and control sizing depend exclusively on completed TRAIN."""
    tr = gate(train, va_artifact)
    te = gate(test, va_artifact).sort_values('session_date')
    if tr.empty:
        raise ValueError('Saved VA category has no matching training trades')
    tr_multiplier = 1+sizing.apply_component(tr.macro_4h_adx, adx_artifact)
    te_multiplier = 1+sizing.apply_component(te.macro_4h_adx, adx_artifact)
    fixed = sizing.match_fixed_risk(tr.distance, sizing.effective_risk(tr_multiplier, tr.distance).mean())
    te['multiplier'] = te_multiplier
    te['base_risk'] = sizing.effective_risk(np.ones(len(te)), te.distance)
    te['sized_risk'] = sizing.effective_risk(te_multiplier, te.distance)
    te['matched_risk'] = np.minimum(fixed, sizing.CAP*te.distance)
    te['matched_requested_multiplier'] = fixed/sizing.BASE_RISK
    te['cohort'] = np.select([te_multiplier <= .9, te_multiplier >= 1.1], ['lower','higher'], default='middle')
    return te, dict(va_train_entries=len(tr), train_mean_multiplier=float(tr_multiplier.mean()),
        train_effective_risk=float(sizing.effective_risk(tr_multiplier, tr.distance).mean()),
        fixed_matched_risk=float(fixed), test_entries=len(te),
        adx_missing_test_entries=int(te.macro_4h_adx.isna().sum()),
        neutral_test_entries=int(np.isclose(te_multiplier, 1, atol=1e-12, rtol=0).sum()),
        adx_artifact_status=adx_artifact['status'])


def build(root, out):
    runs = root/'model_development/models/mean_reversion_by_volume_profile/runs'
    paths = dict(
        va_predictions=runs/'edge_context_with_va_v09/test_predictions.csv',
        va_context=runs/'edge_context_with_va_v09/context_ledger.csv',
        va_calibration=runs/'edge_context_with_va_v09/calibration.json',
        folds=runs/'edge_context_with_va_v09/folds.csv',
        feature_ledger=runs/'edge_sizing_v12/feature_trade_ledger.csv',
        adx_calibration=runs/'edge_sizing_v12/calibration.json',
        adx_predictions=runs/'edge_sizing_v12/sizing_predictions.csv',
        old_validation=runs/'edge_sizing_v12/validation.json',
        original_trades=runs/'poc_rr_aligned_v03/trades.csv')
    sources = [dict(role=k,path=str(p),sha256=sizing.sha(p)) for k,p in paths.items()]
    assert json.loads(paths['old_validation'].read_text())['status'] == 'passed'
    base = restrict(pd.read_csv(paths['feature_ledger']))
    context = restrict(pd.read_csv(paths['va_context']))
    base = base.merge(context[KEYS+[VA_FEATURE]],on=KEYS,how='left',validate='one_to_one')
    assert len(base) == 1069
    original = restrict(pd.read_csv(paths['original_trades']))
    check = base.merge(original,on=KEYS,validate='one_to_one',suffixes=('','_old'))
    for c in ['entry','tp','sl','side','risk_price','gross_r']:
        np.testing.assert_allclose(check[c],check[c+'_old'])
    assert check.status.eq(check.status_old).all()
    for c in ['entry_timestamp_ny','exit_timestamp_ny']:
        assert check[c].eq(check[c+'_old']).all()
    pivot = pd.to_datetime(base.session_date+' 13:00').dt.tz_localize(sizing.NY).dt.tz_convert('UTC')
    stamps = pd.to_datetime(base.macro_4h_asof_utc,utc=True,format='ISO8601',errors='coerce')
    assert (base.macro_4h_adx.isna() | (stamps.notna() & stamps.le(pivot))).all()
    assert pd.to_datetime(base.entry_timestamp_ny,utc=True,format='ISO8601').ge(pivot).all()
    old = restrict(pd.read_csv(paths['va_predictions']))
    old = old.loc[old.feature.eq(VA_FEATURE)]
    assert len(old) == 891 and int(old.selected.sum()) == 387
    np.testing.assert_allclose(old.loc[old.selected,'gross_r'].sum(),21.260701109761516)
    va = {int(a['fold']):a for a in json.loads(paths['va_calibration'].read_text())
          if a['symbol']==SYMBOL and a['profile']==PROFILE and a['feature']==VA_FEATURE}
    adx = {(int(a['fold']),a['target_cost_bps']):a for a in json.loads(paths['adx_calibration'].read_text())
           if a['symbol']==SYMBOL and a['profile']==PROFILE and a['feature']=='adx_4h'}
    saved = restrict(pd.read_csv(paths['adx_predictions']))
    saved = saved.loc[saved.model.eq('adx_4h')]
    folds = restrict(pd.read_csv(paths['folds']))
    base['exit_utc'] = pd.to_datetime(base.exit_utc,utc=True,format='ISO8601')
    rows, controls, calibrations = [], [], []
    for meta in folds.to_dict('records'):
        number = int(meta['fold'])
        cutoff = pd.Timestamp(meta['test_start'],tz=sizing.NY).tz_convert('UTC')
        train_dates = base.session_date.ge(meta['train_start']) & base.session_date.lt(meta['test_start'])
        train = base.loc[train_dates & base.exit_utc.lt(cutoff)]
        test = base.loc[base.session_date.ge(meta['test_start']) & base.session_date.lt(meta['test_end_exclusive'])]
        vp = va[number]
        assert vp['train_start'] == meta['train_start'] and vp['test_start'] == meta['test_start']
        assert vp['test_end_exclusive'] == meta['test_end_exclusive']
        observed_train = train.dropna(subset=[VA_FEATURE])
        np.testing.assert_allclose(observed_train[VA_FEATURE].quantile([1/3,2/3]), vp['calibration']['cutpoints'])
        assert len(observed_train) == vp['training_feature_entries']
        selected_train = gate(train,vp)
        expected_train = vp['all_train_categories'][vp['selected_category']]
        assert len(selected_train) == expected_train['size']
        np.testing.assert_allclose(selected_train.gross_r.mean(),expected_train['mean'],atol=1e-12)
        for target in sizing.TARGET_COSTS:
            ap = adx[(number,target)]
            assert ap['train_start'] == meta['train_start'] and ap['test_start'] == meta['test_start']
            assert ap['test_end_exclusive'] == meta['test_end_exclusive']
            # Verify the saved score is reproducible from this fold's original TRAIN only.
            fitted = sizing.fit_component(train.macro_4h_adx,train.gross_r-target*train.cost_r_per_bps)
            assert fitted['status'] == ap['status']
            np.testing.assert_allclose(sizing.apply_component(train.macro_4h_adx,fitted),
                                       sizing.apply_component(train.macro_4h_adx,ap),atol=1e-12)
            q, control = allocate(train,test,vp,ap)
            expected = old.loc[old.fold.eq(number) & old.selected].sort_values('session_date')
            assert q.session_date.tolist() == expected.session_date.tolist()
            np.testing.assert_allclose(q.gross_r,expected.gross_r,atol=1e-12)
            previous = saved.loc[saved.fold.eq(number) & saved.target_cost_bps.eq(target),KEYS+['multiplier']]
            matched = q.merge(previous,on=KEYS,validate='one_to_one',suffixes=('','_saved'))
            assert len(matched) == len(q)
            np.testing.assert_allclose(matched.multiplier,matched.multiplier_saved,atol=1e-12)
            q['fold'],q['model'],q['target_cost_bps'] = number,'va_previous_plus_adx',target
            rows.append(q)
            controls.append(dict(**meta,target_cost_bps=target,
                va_category=vp['selected_category'],va_cutpoints=json.dumps(vp['calibration']['cutpoints']),
                purged_train=int((train_dates & ~base.exit_utc.lt(cutoff)).sum()),**control))
            calibrations.append(dict(fold=number,target_cost_bps=target,va_artifact=vp,
                adx_artifact=ap,subset_control=control))
    result = pd.concat(rows,ignore_index=True)
    assert not result.duplicated(KEYS+['target_cost_bps']).any()
    assert result.multiplier.between(.5-1e-12,1.5+1e-12).all() and result.sized_risk.gt(0).all()
    for target,g in result.groupby('target_cost_bps'):
        assert len(g) == 387
        np.testing.assert_allclose(g.gross_r.sum(),21.260701109761516)
    result.to_csv(out/'sizing_predictions.csv',index=False)
    pd.DataFrame(controls).to_csv(out/'coverage.csv',index=False)
    sizing.save_json(out/'calibration.json',calibrations)
    return result,folds,sources


def reconcile(pred,summary):
    groups = {key:g.sort_values('session_date') for key,g in pred.groupby('target_cost_bps')}
    for r in summary.itertuples():
        g = groups[r.target_cost_bps]
        risk = g[dict(fixed_base='base_risk',scored_sizing='sized_risk',fixed_train_matched='matched_risk')[r.policy]].to_numpy()
        net = g.gross_r.to_numpy()-r.cost_bps*g.cost_r_per_bps.to_numpy()
        np.testing.assert_allclose(np.sum(risk/.005*net),r.capital_net_base_r,atol=1e-10)
        np.testing.assert_allclose(100*(np.prod(1+risk*net)-1),r.compounded_return_pct,atol=1e-10)
        assert r.trades == 387
    for _,g in summary.groupby(['target_cost_bps','cost_bps']):
        assert g.original_gross_wr_pct.nunique() == g.original_net_wr_pct.nunique() == 1


def report(out,summary,stability,robust,cohorts):
    lines = ['# XRP: transisi VA kemarin + sizing ADX 4H — v13','',
        'Filter entry VA lama dipertahankan persis: 387 trade selama 46 bulan kalender test, sekitar 101 trade/tahun. Target POC pagi 09:30–13:00, entry reversal 13:00, TP/SL simetris, time exit sebelum 17:00. Original gross R = +21,260701. Tidak ada entry yang dibuang lagi oleh ADX.',
        'Bobot ADX 0,5–1,5× berasal dari kategori/expectancy TRAIN v12. Angka ADX lebih besar tidak otomatis berarti posisi lebih besar. Skor dilatih pada baseline train, kemudian dipakai pada trade yang lolos VA; tidak ada conditional refit pada kelompok kecil itu. ADX hilang/kurang riwayat mendapat bobot 1.',
        'Train 8 bulan → test 2 bulan, maju 2 bulan. Kategori VA dan bobot ADX dikunci sebelum test. Pembanding fixed-train-matched dicocokkan hanya pada trade TRAIN yang lolos kategori VA, setelah cap notional yang sama.',
        'Satu R modal = risiko acuan 0,5% equity sebelum trade; notional maksimum 5×. Angka R modal tercap tidak identik dengan +21,26R original yang tanpa batas notional. Return akun di bawah adalah total seluruh periode, bukan per tahun. DD hanya di penutupan trade.', '',
        '## Perbandingan pada kalibrasi gross','',
        'Target kalibrasi gross dikunci; semua skenario biaya berikut menilai bobot yang sama. Skenario 2 bps/sisi untuk melatih skor menjadi eksperimen terpisah di bagian berikutnya.', '',
        '| Biaya per sisi | Kebijakan | Trade | Total R modal | Return akun | Max DD akun | Rata-rata risiko/trade |','|---|---|---:|---:|---:|---:|---:|']
    names = dict(fixed_base='VA saja, fixed acuan',scored_sizing='VA + sizing ADX',fixed_train_matched='VA saja, fixed dicocokkan TRAIN')
    for cost in sizing.COSTS:
        g = summary.loc[summary.target_cost_bps.eq(0)&summary.cost_bps.eq(cost)]
        for policy in ['fixed_base','fixed_train_matched','scored_sizing']:
            r = g.loc[g.policy.eq(policy)].iloc[0]
            lines.append(f'| {cost:g} bps | {names[policy]} | {int(r.trades)} | {r.capital_net_base_r:+.3f} | {r.compounded_return_pct:+.2f}% | {r.max_drawdown_pct:.2f}% | {r.mean_effective_risk_pct:.3f}% |')
    lines += ['', '## Kalibrasi net 2 bps/sisi, dinilai pada biaya yang sama', '',
        '| Kebijakan | Total R modal net | Return akun | Max DD |','|---|---:|---:|---:|']
    for r in summary.loc[summary.target_cost_bps.eq(2)&summary.cost_bps.eq(2)].itertuples():
        lines.append(f'| {names[r.policy]} | {r.capital_net_base_r:+.3f} | {r.compounded_return_pct:+.2f}% | {r.max_drawdown_pct:.2f}% |')
    lines += ['', '## Stabilitas incremental sizing dibanding fixed yang dicocokkan TRAIN','',
        '| Kalibrasi / biaya evaluasi | Fold membaik | Delta R modal awal | Delta R modal akhir | Interval 95% delta per trade VA | Fraksi bobot acak sama/lebih baik |',
        '|---|---:|---:|---:|---:|---:|']
    for target,cost in [(0,0),(0,1),(0,2),(2,2)]:
        st = stability.loc[stability.target_cost_bps.eq(target)&stability.cost_bps.eq(cost)].iloc[0]
        u = robust.loc[robust.target_cost_bps.eq(target)&robust.cost_bps.eq(cost)].iloc[0]
        lines.append(f'| {target:g} / {cost:g} bps | {int(st.improved_folds)}/{int(st.folds)} | {st.early_delta:+.3f} | {st.late_delta:+.3f} | [{u.bootstrap_delta_per_opportunity_ci025:+.4f}, {u.bootstrap_delta_per_opportunity_ci975:+.4f}] | {u.random_weight_upper_tail_fraction:.3f} |')
    lines += ['', '## Kualitas kelompok bobot pada test, kalibrasi gross','',
        '| Kelompok | Trade | Mean R asli | WR gross asli | Mean bobot |','|---|---:|---:|---:|---:|']
    for name in ['lower','middle','higher']:
        q = cohorts.loc[cohorts.target_cost_bps.eq(0)&cohorts.cost_bps.eq(0)&cohorts.cohort.eq(name)]
        if len(q):
            r = q.iloc[0]
            lines.append(f'| {name} | {int(r.trades)} | {r.original_mean_gross_r:+.4f} | {r.original_gross_wr_pct:.2f}% | {r.mean_multiplier:.3f} |')
    lines += ['', '## Batas interpretasi dan artefak','',
        'Sizing tidak mengubah jumlah trade, outcome TP/SL atau WR asli. Ia mengubah alokasi modal pada hasil tersebut. Peningkatan raw multiplier-weighted R saja tidak cukup jika risikonya ikut naik; utamakan fixed_train_matched dan diagnostik equal-test-risk retrospektif.',
        'summary.csv memuat seluruh kalibrasi, biaya, statistik risiko, capital contribution, profit factor, break-even proportional cost dan uncapped weighted gross R. fold_metrics.csv, stability.csv, coverage.csv, sizing_cohorts.csv dan robustness.csv menyimpan hasil per periode, riwayat fallback dan ketidakpastian. calibration.json mempertahankan keputusan VA/ADX dari train.',
        'Bootstrap 7 hari kalender dan shuffle bobot dalam masing-masing fold memakai 2.000 draw. Tidak dikoreksi atas banyak percobaan sebelumnya. Riwayat sudah pernah dilihat; kombinasi ini bukan fresh OOS dan tidak otomatis layak live. Biaya masih skenario; funding, fill aktual, slippage nonlinear dan equity intratrade belum dimodelkan.', '']
    (out/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--repeats',type=int,default=2000)
    args = p.parse_args()
    if args.output.exists():
        raise ValueError('Preserve old results; use a new directory')
    if args.repeats < 100:
        p.error('At least 100 diagnostic draws required')
    args.output.mkdir(parents=True)
    model = args.root/'model_development/models/mean_reversion_by_volume_profile'
    sys.path.append(str(model))
    design = Path(__file__).with_name('VA_ADX_SIZING_DESIGN.md')
    shutil.copy2(design,args.output/design.name)
    snapshot = dict(status='running',recorded='2026-10-22',symbol=SYMBOL,profile=PROFILE,
        entry_feature=VA_FEATURE,adx_weights='saved v12 independent TRAIN score',
        base_risk=sizing.BASE_RISK,max_notional_x=sizing.CAP,multiplier_bounds=[.5,1.5],
        target_cost_bps=sizing.TARGET_COSTS,cost_bps=sizing.COSTS,repeats=args.repeats,
        train_months=8,test_months=2,step_months=2,prior_inspection=True,
        code_sha256=sizing.sha(__file__),design_sha256=sizing.sha(design),
        helpers=[dict(path=str(Path(sizing.__file__)),sha256=sizing.sha(sizing.__file__)),
                 dict(path=str(Path(classify.__code__.co_filename)),sha256=sizing.sha(classify.__code__.co_filename))])
    sizing.save_json(args.output/'config_snapshot.json',snapshot)
    pred,folds,sources = build(args.root,args.output)
    summary,stability,robust,cohorts = sizing.summarize(pred,folds,args.output,args.repeats)
    reconcile(pred,summary)
    report(args.output,summary,stability,robust,cohorts)
    sizing.save_json(args.output/'audit.json',dict(status='passed',sources=sources,
        checks=['original 387 VA entry/exit/outcome identities','saved TRAIN categories and ADX weights reused',
                'saved ADX fits reproduce from TRAIN only','VA subset TRAIN-only risk matching',
                'positive bounded weights and unchanged WR','capital and compounded-equity reconciliation']))
    snapshot['status'] = 'complete'
    sizing.save_json(args.output/'config_snapshot.json',snapshot)
    print('Complete: '+str(args.output),flush=True)


if __name__ == '__main__':
    main()
