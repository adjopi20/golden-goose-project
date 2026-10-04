"""Compare independent collector history to a consistent old-worker DB export.

Compare only shared completed aggregate-trade minutes and shared frozen profiles.
No price tolerance: any float or source difference is reported, not hidden.
Missing overlap/readiness is not a parity pass. Never opens a writer connection.
"""
import argparse
import json
from pathlib import Path
import sqlite3


def compare(legacy, collector):
    def connect(path):
        return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    old,new=connect(legacy),connect(collector)
    try:
        def rows(db,table):
            if table=='minutes':
                query="SELECT symbol,timestamp_ms,payload FROM minutes WHERE source='aggregate_trades' ORDER BY symbol,timestamp_ms"
            else:
                query='SELECT symbol,session,payload FROM profiles ORDER BY symbol,session'
            return {(s,k):json.loads(payload) for s,k,payload in db.execute(query)}
        result={}
        for table in ('minutes','profiles'):
            left,right=rows(old,table),rows(new,table)
            overlap=sorted(left.keys() & right.keys())
            changes=[dict(symbol=s,key=k,legacy=left[(s,k)],collector=right[(s,k)]) for s,k in overlap if left[(s,k)]!=right[(s,k)]]
            result[table]=dict(shared=len(overlap),matches=len(overlap)-len(changes),differences=changes[:20],
                old_only=len(left.keys()-right.keys()),new_only=len(right.keys()-left.keys()))
        result['status']='pass_on_overlap' if all(v['shared']>0 and not v['differences'] for v in result.values()) else 'not_passed'
        result['limits']='Independent live feeds may start at different instants. Missing history, receipt timing, execution and recovery require separate checks.'
        return result
    finally:
        old.close(); new.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--legacy',type=Path,required=True)
    p.add_argument('--collector',type=Path,required=True)
    args=p.parse_args()
    result=compare(args.legacy,args.collector)
    print(json.dumps(result,indent=2,allow_nan=False))
    raise SystemExit(0 if result['status']=='pass_on_overlap' else 1)
