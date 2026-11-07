"""Empirical Kelly estimator; public runner: phase_3/evaluate.py --task kelly."""
import argparse
import numpy as np
from model_development.phase_3.ledger_io import config_load, sources, ledger_load, output_begin, write_csv, write_json, receipt
from model_development.phase_3.monte_carlo import scenarios, stressed_r


def empirical_kelly(r, weights):
    """Uncapped theoretical stop-risk fraction, finite empirical outcomes."""
    weights=np.atleast_2d(weights)
    if not np.any(r<0):
        # With no losses, there is no finite empirical interior maximum.
        return np.full(len(weights),np.nan)
    positive=weights@r>0
    lo=np.zeros(len(weights));hi=np.full(len(weights),.999/abs(r.min()))
    for _ in range(48):
        mid=(lo+hi)/2
        slope=np.sum(weights*r/(1+mid[:,None]*r),axis=1)
        lo=np.where(slope>0,mid,lo);hi=np.where(slope>0,hi,mid)
    return np.where(positive,(lo+hi)/2,0.)


def kelly_summary(data, r, *, resamples=2000, block_days=7, seed=20260927):
    """Existing empirical estimator and calendar-cluster bootstrap, reusable."""
    r=np.asarray(r,dtype=float)
    if any(type(v) is not int or v<1 for v in (resamples,block_days)) or type(seed) is not int or seed<0:
        raise ValueError('Positive integer resamples/block-days and nonnegative integer seed required')
    offsets=np.asarray(data['offset'])
    if (r.ndim!=1 or not np.isfinite(r).all() or offsets.shape!=r.shape
            or type(data['days']) is not int or data['days']<1
            or (len(offsets) and (offsets.dtype.kind not in 'iu' or np.any(offsets<0) or np.any(offsets>=data['days'])))):
        raise ValueError('Invalid finite outcomes or calendar indices')
    empty=dict(full_kelly=None,quarter_kelly_reference=None,p05=None,p50=None,p95=None,
               zero_fraction=None,without_largest_winner=None)
    if not len(r):return dict(status='insufficient_sample',**empty)
    if not np.any(r<0):return dict(status='no_observed_loss_no_finite_estimate',**empty)
    point=float(empirical_kelly(r,np.ones((1,len(r))))[0])
    blocks=offsets//block_days;nb=(data['days']+block_days-1)//block_days
    rng=np.random.default_rng(seed);est=[];omitted=0
    for begin in range(0,resamples,128):
        count=min(128,resamples-begin)
        counts=np.stack([np.bincount(x,minlength=nb) for x in rng.integers(0,nb,(count,nb))])
        weights=counts[:,blocks];valid=weights.sum(axis=1)>0;omitted+=int((~valid).sum())
        est.extend(empirical_kelly(r,weights[valid]).tolist())
    if not est:raise ValueError('All bootstrap samples empty')
    est=np.array(est);reduced=np.delete(r,np.argmax(r))
    without=float(empirical_kelly(reduced,np.ones((1,len(reduced))))[0]) if len(reduced) and np.any(reduced<0) else None
    return dict(status='empirical_reference_not_execution_constrained',full_kelly=point,
        quarter_kelly_reference=point/4,p05=float(np.quantile(est,.05)),p50=float(np.median(est)),p95=float(np.quantile(est,.95)),
        zero_fraction=float(np.mean(est==0)),without_largest_winner=without,empty_bootstrap_draws=omitted)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--output-dir',required=True)
    p.add_argument('--scenario',default='baseline',help='Named scenario from the same config')
    p.add_argument('--resamples',type=int,default=2000);p.add_argument('--block-days',type=int,default=7)
    p.add_argument('--seed',type=int,default=20260927)
    a=p.parse_args()
    if a.resamples<1 or a.block_days<1:p.error('Positive resamples/block-days required')
    cfg,root=config_load(a.config)
    scenario=next((s for s in scenarios(cfg) if s['name']==a.scenario),None)
    if scenario is None:p.error('Unknown scenario')
    out=output_begin(a.output_dir);results=[];provenance=[]
    for name,path,spec in sources(cfg,root):
        d=ledger_load(path,spec);r=stressed_r(d,scenario);n=len(r)
        provenance.append(dict(dataset=name,path=str(path),sha256=d['sha256'],trades=n))
        result=kelly_summary(d,r,resamples=a.resamples,block_days=a.block_days,seed=a.seed)
        empty=result.pop('empty_bootstrap_draws',None)
        results.append(dict(dataset=name,trades=n,scenario=a.scenario,**result))
        if empty is not None:
            provenance[-1]['empty_bootstrap_draws']=empty
            print(f"{name}: empirical Kelly {result['full_kelly']:.2%}",flush=True)
    write_csv(out/'kelly.csv',results)
    rec=receipt(cfg,provenance,'kelly',a)
    rec['definition']='max mean(log(1+f*net_R)); f is price-stop risk/equity. Uncapped theoretical reference, NOT margin/notional or leverage-constrained recommended sizing. Bootstrap P5-P95 is descriptive; not corrected for research selection.'
    write_json(out/'manifest.json',rec);write_json(out/'complete.json',dict(datasets=len(results)))


if __name__=='__main__':main()
