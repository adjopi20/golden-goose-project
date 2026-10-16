"""Synthetic restart probe. Only a separate acceptance volume may be mounted."""
from decimal import Decimal
import json
import os
from pathlib import Path
import sys

from live_mvrv.engine import apply_day, configuration, initialize
from live_mvrv.storage import backup, connect


def snapshot(c):
    return {table: [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("accounts", "actions", "decisions", "fills", "marks", "outbox", "meta")}


if not Path("/.dockerenv").exists() or os.getuid() != 1000:
    raise RuntimeError("Run this probe inside the non-root MVRV acceptance container only")
phase = sys.argv[1]
path = Path("/data/synthetic.sqlite")
config = configuration()
c = connect(path)
try:
    if phase == "seed":
        assert not c.execute("SELECT 1 FROM accounts").fetchone(), "Use a NEW test volume"
        with c:
            initialize(c, config, "1000", "paper", "2020-01-02T01:00:00+00:00")
            row = dict(date="2020-01-01", price=100, z=-1, fixed_0_7_entry=0, fixed_0_7_exit=7,
                       percentile_4y_p5_p95_entry=-.5, percentile_4y_p5_p95_exit=3)
            apply_day(c, row, config, "2020-01-02T01:00:00+00:00", "paper")
        assert c.execute("SELECT COUNT(*) FROM actions WHERE status='pending'").fetchone()[0] == 2
        Path("/data/expected.json").write_text(json.dumps(snapshot(c), sort_keys=True))
    elif phase == "resume":
        expected = json.loads(Path("/data/expected.json").read_text())
        assert snapshot(c) == expected, "Persisted state changed across containers"
        with c:
            initialize(c, config, "1000", "paper", "2020-01-03T01:00:00+00:00")
            row = dict(date="2020-01-02", price=110, z=1, fixed_0_7_entry=0, fixed_0_7_exit=7,
                       percentile_4y_p5_p95_entry=-.5, percentile_4y_p5_p95_exit=3)
            assert apply_day(c, row, config, "2020-01-03T01:00:00+00:00", "paper")
            assert not apply_day(c, row, config, "2020-01-03T02:00:00+00:00", "paper")
        assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM actions WHERE status='filled'").fetchone()[0] == 2
        for r in c.execute("SELECT * FROM accounts"):
            assert Decimal(r["cash"]) >= 0 and Decimal(r["btc"]) > 0
        backup(c, "/data/synthetic-backup.sqlite")
        Path("/data/expected-after.json").write_text(json.dumps(snapshot(c), sort_keys=True))
    elif phase == "verify":
        assert snapshot(c) == json.loads(Path("/data/expected-after.json").read_text())
        restored = connect("/data/synthetic-backup.sqlite")
        try:
            assert snapshot(restored) == snapshot(c), "Backup differs from committed ledger"
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            restored.close()
    else:
        raise ValueError("Unknown phase")
    print(json.dumps({"phase": phase, "status": "passed", "database": str(path),
                      "fills": c.execute("SELECT COUNT(*) FROM fills").fetchone()[0]}))
finally:
    c.close()
