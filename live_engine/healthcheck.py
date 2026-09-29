"""Read-only Docker health check: every configured market must have a fresh feed."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time


def check(database: Path, symbols: list[str], max_age_seconds: int) -> list[str]:
    if not database.is_file():
        return ["paper database is absent"]
    try:
        db = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2)
        with db:
            rows = dict(db.execute("SELECT symbol,state FROM feeds"))
    except (sqlite3.Error, OSError) as exc:
        return [f"cannot read feed checkpoints: {exc}"]
    now_ms = int(time.time() * 1000)
    failures = []
    for symbol in symbols:
        try:
            stamp = json.loads(rows[symbol])["cursor"]["timestamp_ms"]
            age = (now_ms - stamp) / 1000
            if age < -5 or age > max_age_seconds:
                failures.append(f"{symbol} feed age {age:.0f}s")
        except (KeyError, TypeError, ValueError):
            failures.append(f"{symbol} feed checkpoint absent/invalid")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--max-age-seconds", type=int, default=180)
    args = parser.parse_args()
    failures = check(args.database, args.symbols, args.max_age_seconds)
    if failures:
        print("; ".join(failures))
        return 1
    print("feeds fresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
