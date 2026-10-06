import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('continuity', ROOT/'infra/ops/phase3_continuity.py')
continuity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(continuity)


def database(path, owner, rows, epoch='native-1'):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
        db.executemany('INSERT INTO metadata VALUES (?,?)', [('owner', owner), ('venue','binance'),
            ('environment','server-paper'), ('source_epoch',epoch)])
        db.execute('CREATE TABLE minutes(symbol TEXT,timestamp_ms INTEGER,payload TEXT,source TEXT)')
        db.executemany('INSERT INTO minutes VALUES (?,?,?,?)', rows)


def test_full_history_and_readonly(tmp_path):
    a, b = tmp_path/'strategy.sqlite', tmp_path/'collector.sqlite'
    rows = [('ETHUSDC', t, json.dumps({'timestamp_ms':t,'close':100}), 'aggregate_trades') for t in (60,120,180)]
    database(a,'preny_strategy_shadow',rows)
    database(b,'market_data',rows[1:])  # source retention is not a lost consumer event
    before = (a.read_bytes(), b.read_bytes())
    result = continuity.audit(a,b,180)
    assert result['status']=='passed'
    assert result['symbols']['ETHUSDC']['compared_rows']==2
    assert before == (a.read_bytes(), b.read_bytes())
    with sqlite3.connect(b) as db:
        db.execute('UPDATE minutes SET payload=? WHERE timestamp_ms=120', ('{"close":101}',))
    assert continuity.audit(a,b,180)['status']=='review_required'


def test_missing_and_epoch_rejected(tmp_path):
    a, b = tmp_path/'strategy.sqlite', tmp_path/'collector.sqlite'
    rows = [('ETHUSDC',t,'{"close":100}','native') for t in (60,120)]
    database(a,'preny_strategy_shadow',rows[:1])
    database(b,'market_data',rows,epoch='replacement')
    r = continuity.audit(a,b,120)
    assert not r['identity_ok'] and r['status']=='review_required'
    assert r['symbols']['ETHUSDC']['collector_only_rows']==1


def health_command():
    yaml = (ROOT/'models/trend_following_preny_profile_15m/deploy/phase3.shadow.yaml').read_text()
    line = next(line for line in yaml.splitlines() if line.strip().startswith('test:'))
    return json.loads(line.split('test:',1)[1].strip())[-1]


def test_lightweight_healthcheck(tmp_path):
    path = tmp_path/'health.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE metadata(key TEXT,value TEXT)')
        db.execute('INSERT INTO metadata VALUES (?,?)', ('last_poll_ms',str(int(time.time()*1000))))
    code = health_command().replace('/data/strategy.sqlite', path.as_posix())
    assert 'shadow_consumer' not in code and 'pandas' not in code
    assert subprocess.run([sys.executable,'-c',code]).returncode==0
    with sqlite3.connect(path) as db:
        db.execute("UPDATE metadata SET value='1'")
    assert subprocess.run([sys.executable,'-c',code]).returncode==1
    with sqlite3.connect(path) as db:
        db.execute('UPDATE metadata SET value=?', (str(int(time.time()*1000)+60000),))
    assert subprocess.run([sys.executable,'-c',code]).returncode==1
    assert subprocess.run([sys.executable,'-c',code.replace(path.as_posix(),(tmp_path/'missing.sqlite').as_posix())],
        capture_output=True).returncode!=0
    assert not (tmp_path/'missing.sqlite').exists()
