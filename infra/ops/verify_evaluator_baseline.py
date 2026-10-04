"""Offline migration gate against a Phase 0 capture, on separate database copies.

No networking, paper order submission, or writes to the input artifact. This
checks evaluator reconstruction, not original event availability or fill parity.
"""
import argparse
import json
from pathlib import Path
import sqlite3

from live_engine.state_store import StateStore
from models.trend_following_preny_profile_15m.paper_worker import Worker

FIELDS = ('state','candidate','reason','selected','risk_fraction','signal','snapshot')


def verify(capture_dir, output_dir):
    output_dir.mkdir(parents=True, exist_ok=False)
    results = {}
    for venue in ('binance','lighter'):
        folder = capture_dir / venue
        manifest = json.loads((folder/'manifest.json').read_text())
        if manifest['status'] != 'complete' or manifest['venue'] != venue:
            raise ValueError('Incomplete/mismatched capture')
        day = manifest['fixture']['session_day']
        source = sqlite3.connect((folder/'snapshot.sqlite').resolve().as_uri()+'?immutable=1',uri=True)
        destination = output_dir / (venue+'_replay.sqlite')
        try:
            with sqlite3.connect(destination) as target:
                source.backup(target)
            expected = [(symbol,asof,json.loads(payload)) for symbol,asof,payload in source.execute(
                'SELECT symbol,asof,payload FROM evaluations ORDER BY asof,symbol')
                if json.loads(payload)['session_day'] == day]
            if not expected:
                raise ValueError('No baseline evaluations available')
            calibration = None
            if (folder/'calibration.json').is_file():
                calibration = json.loads((folder/'calibration.json').read_text())
            if calibration is None:
                for payload, in source.execute('SELECT payload FROM intents'):
                    saved = json.loads(payload)['snapshot'].get('c1_calibration')
                    if saved is not None:
                        calibration = saved
        finally:
            source.close()
        store = StateStore(destination, venue)
        try:
            with store.transaction():
                for symbol,asof,_ in expected:
                    store.db.execute('DELETE FROM evaluations WHERE symbol=? AND asof=?',(symbol,asof))
            worker = Worker(store, None, paper=False, calibration=calibration,
                            strict_coverage=venue=='lighter',
                            min_native_reference_sessions=10 if venue=='lighter' else 0)
            checks = []
            for symbol,asof,old in expected:
                worker.now_ms = lambda stamp=asof: stamp
                feed = json.loads(store.db.execute('SELECT state FROM feeds WHERE symbol=?',(symbol,)).fetchone()[0])
                with store.transaction():
                    worker.boundary(symbol,asof,feed)
                new = json.loads(store.db.execute('SELECT payload FROM evaluations WHERE symbol=? AND asof=?',
                                                  (symbol,asof)).fetchone()[0])
                differences = [field for field in FIELDS if old.get(field) != new.get(field)]
                checks.append(dict(symbol=symbol,asof=asof,state=old['state'],differences=differences))
            results[venue] = dict(session_day=day, evaluations=len(checks),
                                 matches=sum(not c['differences'] for c in checks), checks=checks)
        finally:
            store.close()
    (output_dir/'results.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    if any(data['matches'] != data['evaluations'] for data in results.values()):
        raise ValueError('Evaluator baseline changed; inspect results.json')
    return results


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-dir',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    result=verify(args.capture_dir,args.output_dir)
    print(json.dumps({venue:{k:v for k,v in row.items() if k!='checks'} for venue,row in result.items()},indent=2))
