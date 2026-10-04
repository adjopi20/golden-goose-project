import gzip
import json
from pathlib import Path
import sqlite3

import pytest

from infra.ops.phase0_snapshot import backup_database, capture, process_config


def source_root(tmp_path):
    root = tmp_path / "source"
    (root / "live_engine").mkdir(parents=True)
    (root / "live_engine" / "worker.py").write_text("VERSION = 1\n", encoding="utf-8")
    return root


def paper_db(tmp_path):
    path = tmp_path / "active.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE accounts(id TEXT,symbol TEXT,config TEXT,state TEXT,cursor TEXT);
        CREATE TABLE journal(account TEXT,kind TEXT,payload TEXT);
        CREATE TABLE feeds(symbol TEXT,state TEXT);
        CREATE TABLE intents(account TEXT,session TEXT,payload TEXT);
        CREATE TABLE minutes(symbol TEXT,timestamp_ms INTEGER,payload TEXT,source TEXT);
        CREATE TABLE profiles(symbol TEXT,session TEXT,payload TEXT);
        CREATE TABLE evaluations(symbol TEXT,asof INTEGER,payload TEXT);
        INSERT INTO metadata VALUES ('venue','binance'),('mode','paper');
        INSERT INTO accounts VALUES ('eth','ETHUSDC','{}','{}',NULL);
        INSERT INTO journal VALUES ('eth','fill','{}');
        INSERT INTO feeds VALUES ('ETHUSDC','{}');
    """)
    for stamp in (0, 60_000, 120_000, 180_000):
        db.execute("INSERT INTO minutes VALUES (?,?,?,?)", (
            "ETHUSDC", stamp, json.dumps({"timestamp_ms": stamp, "close": 100}), "aggregate_trades"))
    profile = {"session_day": "2026-10-02", "poc": 100}
    db.execute("INSERT INTO profiles VALUES (?,?,?)", ("ETHUSDC", "2026-10-02", json.dumps(profile)))
    for asof, day in ((60_000, "2026-10-01"), (180_000, "2026-10-02")):
        db.execute("INSERT INTO evaluations VALUES (?,?,?)", (
            "ETHUSDC", asof, json.dumps({"session_day": day, "state": "SESSION_LOCKED", "as_of_ms": asof})))
    db.commit()
    return path, db


def test_consistent_backup_preserves_live_db_and_causal_fixture(tmp_path):
    active, connection = paper_db(tmp_path)
    before = connection.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    output = tmp_path / "capture"
    result = capture(source_root(tmp_path), output, database=active, venue="binance")
    assert result["status"] == "complete"
    assert connection.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == before
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    fixture = json.loads((output / "fixture_index.json").read_text())
    assert fixture["session_day"] == "2026-10-02"
    symbol = fixture["symbols"]["ETHUSDC"]
    assert symbol["evaluations"][0]["state"] == "SESSION_LOCKED"
    with gzip.open(output / symbol["minutes_file"], "rt") as handle:
        stamps = [json.loads(line)["timestamp_ms"] for line in handle]
    assert stamps == [0, 60_000, 120_000]
    backup = sqlite3.connect(output / "snapshot.sqlite")
    assert backup.execute("SELECT COUNT(*) FROM minutes").fetchone()[0] == 4
    backup.close()


def test_venue_mismatch_is_failed_not_complete(tmp_path):
    active, connection = paper_db(tmp_path)
    output = tmp_path / "capture"
    with pytest.raises(ValueError, match="venue"):
        capture(source_root(tmp_path), output, database=active, venue="lighter")
    assert json.loads((output / "manifest.json").read_text())["status"] == "failed"
    connection.close()


def test_refuses_to_overwrite_capture_or_active_database(tmp_path):
    active, connection = paper_db(tmp_path)
    with pytest.raises(ValueError, match="new file"):
        backup_database(active, active)
    output = tmp_path / "capture"
    output.mkdir()
    with pytest.raises(FileExistsError):
        capture(source_root(tmp_path), output, database=active, venue="binance")
    connection.close()


def test_inventory_only_does_not_require_market_data(tmp_path):
    output = tmp_path / "inventory"
    result = capture(source_root(tmp_path), output)
    assert result["status"] == "complete"
    assert not (output / "snapshot.sqlite").exists()
    inventory = json.loads((output / "source_inventory.json").read_text())
    assert "live_engine/worker.py" in inventory["files"]


def test_process_config_finds_worker_behind_docker_init_without_exporting_secrets(tmp_path):
    proc = tmp_path / "proc"
    for pid, argv in ((1, ["/sbin/docker-init", "--", "python"]),
                      (7, ["python", "-m", "models.trend_following_preny_profile_15m.paper_worker",
                           "--action", "run", "--database", "/data/binance.sqlite", "--paper",
                           "--unrelated-secret", "DO_NOT_EXPORT"])):
        directory = proc / str(pid)
        directory.mkdir(parents=True)
        (directory / "cmdline").write_bytes("\0".join(argv).encode())
    result = process_config(tmp_path, tmp_path, proc)
    assert result["status"] == "captured"
    assert result["--paper"] is True
    assert result["--database"] == "/data/binance.sqlite"
    assert "DO_NOT_EXPORT" not in json.dumps(result)
