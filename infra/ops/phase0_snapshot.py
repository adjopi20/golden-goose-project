"""Capture current paper state without importing or changing the trading engine.

Run via Python stdin inside the existing container, or with --inventory-only
on the research machine. Standard library only; no secrets/environment export.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sqlite3
import sys


CODE_ROOTS = ("trading_core", "market_data", "live_engine", "backtest_engine", "models/trend_following_preny_profile_15m")
SKIP_PARTS = {"runs", "research", "tests", "__pycache__", "images", ".git"}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8")


def inventory(root):
    files = {}
    for relative in CODE_ROOTS:
        directory = root / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if (path.is_file() and path.suffix in {".py", ".md", ".yaml", ".json", ".txt"}
                    and not SKIP_PARTS.intersection(path.relative_to(root).parts)):
                files[path.relative_to(root).as_posix()] = sha256(path)
    if not files:
        raise ValueError("No runtime source found at the requested root")
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"files": files, "fingerprint": fingerprint,
            "scope": list(CODE_ROOTS), "excluded_parts": sorted(SKIP_PARTS)}


def process_config(root, output, proc_root=Path("/proc")):
    """Capture known safe path arguments, never arbitrary argv or environment."""
    captured = {}
    # Compose init=true can put docker-init at PID 1; inspect the worker itself.
    workers = []
    for process_file in proc_root.glob("[0-9]*/cmdline"):
        try:
            arguments = process_file.read_bytes().decode().split("\0")
        except (OSError, UnicodeError):
            continue
        if (any(module in arguments for module in (
                "models.trend_following_preny_profile_15m.paper_worker",
                "models.trend_following_preny_profile_15m.lighter_worker"))
                and "--action" in arguments
                and arguments[arguments.index("--action") + 1:][:1] == ["run"]
                and Path(arguments[0]).name.startswith("python")):
            workers.append(arguments)
    if not workers:
        return {"status": "worker_process_not_found"}
    if len(workers) != 1:
        raise ValueError("Expected exactly one paper worker in the container")
    arguments = workers[0]
    if not arguments:
        return captured
    for option in ("--config", "--database", "--action", "--c1-calibration"):
        if option in arguments:
            index = arguments.index(option)
            if index + 1 < len(arguments):
                captured[option] = arguments[index + 1]
    captured["--paper"] = "--paper" in arguments
    captured["status"] = "captured"
    calibration = captured.get("--c1-calibration")
    if calibration:
        path = Path(calibration)
        if not path.is_absolute():
            path = root / path
        artifact = json.loads(path.read_text(encoding="utf-8-sig"))
        write_json(output / "calibration.json", artifact)
        captured["calibration_sha256"] = sha256(path)
    return captured


def backup_database(database, destination):
    if not database.is_file():
        raise FileNotFoundError(database)
    if destination.exists() or database.resolve() == destination.resolve():
        raise ValueError("Backup must be a new file separate from the source")
    source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    target = sqlite3.connect(destination)
    try:
        source.backup(target, pages=256, sleep=0.05)
        result = target.execute("PRAGMA integrity_check").fetchall()
        if result != [("ok",)]:
            raise ValueError("Snapshot SQLite integrity check failed")
    finally:
        target.close()
        source.close()


def rows(connection, query, params=()):
    return [dict(row) for row in connection.execute(query, params)]


def export_fixture(connection, output, session_day):
    evaluations = rows(connection, "SELECT symbol,asof,payload FROM evaluations ORDER BY symbol,asof")
    if not session_day:
        days = [json.loads(row["payload"]).get("session_day") for row in evaluations]
        session_day = max((day for day in days if day), default=None)
    selected = [row for row in evaluations if json.loads(row["payload"]).get("session_day") == session_day]
    symbols = sorted({row["symbol"] for row in selected})
    fixture = {"session_day": session_day, "symbols": {},
               "input_semantics": "database reconstruction at capture time, not an exact historical tick archive",
               "warning": "No raw tick replay; do not infer stop-path/fill parity from this fixture."}
    for symbol in symbols:
        symbol_evaluations = [row for row in selected if row["symbol"] == symbol]
        asof = max(row["asof"] for row in symbol_evaluations)
        filename = f"minutes_{symbol}.jsonl.gz"
        count = 0
        # Keep ALL stored causal warmup: truncating it could change recursive EMA.
        with gzip.open(output / filename, "wt", encoding="utf-8", newline="\n") as handle:
            for row in connection.execute(
                    "SELECT timestamp_ms,payload,source FROM minutes WHERE symbol=? AND timestamp_ms+60000<=? ORDER BY timestamp_ms",
                    (symbol, asof)):
                handle.write(json.dumps(dict(row), sort_keys=True, allow_nan=False) + "\n")
                count += 1
        profile = rows(connection, "SELECT payload FROM profiles WHERE symbol=? AND session=?", (symbol, session_day))
        fixture["symbols"][symbol] = {
            "asof_max": asof, "minutes_file": filename, "minute_count": count,
            "profile": json.loads(profile[0]["payload"]) if profile else None,
            "evaluations": [json.loads(row["payload"]) for row in symbol_evaluations],
        }
    write_json(output / "fixture_index.json", fixture)
    return {"session_day": session_day, "symbols": symbols,
            "status": "captured" if symbols else "no_evaluation_available"}


def capture(root, output, *, database=None, venue=None, session_day=None):
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "status": "incomplete",
                "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                "venue": venue, "python": sys.version,
                "limitations": ["No secret/env export", "No code or active database mutation",
                                "Snapshot does not establish profitability or fill/recovery correctness"]}
    try:
        write_json(output / "source_inventory.json", inventory(root))
        write_json(output / "python_packages.json", dict(sorted(
            (dist.metadata["Name"], dist.version) for dist in importlib.metadata.distributions()
            if dist.metadata.get("Name"))))
        if database is not None:
            manifest["process_config"] = process_config(root, output)
            snapshot = output / "snapshot.sqlite"
            backup_database(database, snapshot)
            connection = sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                metadata = dict(connection.execute("SELECT key,value FROM metadata"))
                if metadata.get("venue") != venue or metadata.get("mode") != "paper":
                    raise ValueError("Expected matching venue and paper-only database")
                write_json(output / "paper_state.json", {
                    "metadata": metadata,
                    "accounts": rows(connection, "SELECT id,symbol,config,state,cursor FROM accounts ORDER BY id"),
                    "feeds": rows(connection, "SELECT symbol,state FROM feeds ORDER BY symbol"),
                    "journal_counts": rows(connection, "SELECT account,kind,COUNT(*) AS count FROM journal GROUP BY account,kind"),
                    "intents": rows(connection, "SELECT account,session,payload FROM intents ORDER BY account,session"),
                })
                manifest["fixture"] = export_fixture(connection, output, session_day)
            finally:
                connection.close()
        manifest["status"] = "complete"
        manifest["files"] = {path.name: {"bytes": path.stat().st_size, "sha256": sha256(path)}
                             for path in sorted(output.iterdir()) if path.is_file()}
        write_json(output / "manifest.json", manifest)
        return manifest
    except BaseException as exc:
        manifest.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        write_json(output / "manifest.json", manifest)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--venue", choices=("binance", "lighter"))
    parser.add_argument("--session-day", help="Optional NY session date YYYY-MM-DD")
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    if not args.inventory_only and (args.database is None or args.venue is None):
        parser.error("capture requires --database and --venue; use --inventory-only on the laptop")
    if args.inventory_only and args.database is not None:
        parser.error("--inventory-only cannot be combined with --database")
    result = capture(args.root.resolve(), args.output_dir,
                     database=args.database, venue=args.venue, session_day=args.session_day)
    print(json.dumps({"status": result["status"], "venue": args.venue,
                      "output_dir": str(args.output_dir), "fixture": result.get("fixture")}, indent=2))


if __name__ == "__main__":
    main()
