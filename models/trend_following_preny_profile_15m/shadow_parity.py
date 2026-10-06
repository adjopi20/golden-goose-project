"""Read-only migration audit of frozen inputs/decisions; no fills or new research."""
import argparse
from datetime import date
import json
from pathlib import Path
import sqlite3

from live_engine.strategy_store import StrategyStore
from trading_core.contracts import canonical_json
from trading_core.session import clock_ms
from .runtime.evaluator import ACCOUNT_IDS
from .runtime.observer import SessionObserver
from .runtime.preparation import paper_activation_signal

EXECUTION_KEYS = {'paper_result','paper_signal','decision_ready_ms','decision_delay_ms',
                  'provenance','mode','proposal_state','intention_id','input_delivery'}


def causal(row):
    return {k:v for k,v in row.items() if k not in EXECUTION_KEYS}


def replay(reference_path, output_dir, venue, day, calibration=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True,exist_ok=True)
    dbpath = output_dir/'replay.sqlite'
    if dbpath.exists(): raise ValueError('Use a new audit output directory')
    store = StrategyStore(dbpath,venue=venue,config_hash='audit',calibration_hash='audit',code_version='audit')
    checks=[]
    try:
        with sqlite3.connect(Path(reference_path).resolve().as_uri()+'?mode=ro',uri=True) as source:
            metadata=dict(source.execute('SELECT key,value FROM metadata'))
            if metadata.get('venue') != venue: raise ValueError('Reference venue mismatch')
            with store.transaction():
                for table,columns in [('minutes',4),('profiles',3),('profile_prices',4)]:
                    for row in source.execute(f'SELECT * FROM {table}'):
                        store.db.execute(f'INSERT INTO {table} VALUES ({",".join("?"*columns)})',row)
            expected=[(s,t,json.loads(p)) for s,t,p in source.execute(
                'SELECT * FROM evaluations WHERE asof>=? AND asof<=? ORDER BY asof,symbol',
                (clock_ms(day,9),clock_ms(day,12)))]
            if len(expected) != 39: raise ValueError('Need all 13 boundaries for each of three markets')
            for symbol,asof,reference in expected:
                raw=source.execute('SELECT state FROM feeds WHERE symbol=?',(symbol,)).fetchone()
                if raw is None: raise ValueError('Reference feed coverage missing')
                # Coverage is preserved at capture; no post-session reset allowed.
                state=json.loads(raw[0])
                if state['coverage_start_ms'] > clock_ms(day,1):
                    raise ValueError('Reference reset after session; historical coverage not reconstructible')
                ready=reference.get('decision_ready_ms',asof)
                observer=SessionObserver(store,calibration=calibration,now_ms=lambda:ready,
                    strict_coverage=venue=='lighter',min_native_reference_sessions=10 if venue=='lighter' else 0,
                    activate=lambda evaluated,ms: dict(paper_signal=paper_activation_signal(evaluated['signal'],ms)))
                with store.transaction(): observer.boundary(symbol,asof,state)
                actual=json.loads(store.db.execute('SELECT payload FROM evaluations WHERE symbol=? AND asof=?',
                                                    (symbol,asof)).fetchone()[0])
                a,b=causal(actual),causal(reference)
                differences=[k for k in sorted(set(a)|set(b)) if a.get(k)!=b.get(k)]
                checks.append(dict(symbol=symbol,asof=asof,state=actual['state'],differences=differences))
        result=dict(venue=venue,session_day=str(day),checks=checks,evaluations=len(checks),
                    matches=sum(not c['differences'] for c in checks),real_orders=False,
                    limitation='Identical captured inputs; does not prove live consumer delivery or fill parity')
        (output_dir/'results.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        return result
    finally:
        store.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference-database',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--venue',choices=('binance','lighter'),required=True)
    p.add_argument('--session-day',type=date.fromisoformat,required=True)
    p.add_argument('--c1-calibration',type=Path)
    args=p.parse_args()
    calibration=json.loads(args.c1_calibration.read_text()) if args.c1_calibration else None
    if calibration is not None and calibration.get('venue')!=args.venue: p.error('Calibration venue mismatch')
    r=replay(args.reference_database,args.output_dir,args.venue,args.session_day,calibration)
    print(canonical_json({k:v for k,v in r.items() if k!='checks'}))
    raise SystemExit(0 if r['matches']==r['evaluations'] else 1)


if __name__=='__main__': main()
