"""Freeze HYPE C1 medians from earlier venue-native paper observations."""
import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from statistics import median

from trading_core.session import NY


KEYS = ("directional_delta_imbalance", "directional_result_atr")


def calibrate(database, venue, valid_from, *, lookback_days=180, min_candidates=25):
    first = valid_from-timedelta(days=lookback_days)
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        stored_venue = db.execute("SELECT value FROM metadata WHERE key='venue'").fetchone()
        if not stored_venue or stored_venue[0] != venue:
            raise ValueError("Database venue does not match calibration venue")
        records = db.execute("SELECT payload FROM evaluations WHERE symbol='HYPEUSDT'").fetchall()
    selected = []
    for (payload,) in records:
        row = json.loads(payload)
        day = date.fromisoformat(row["session_day"])
        if not first <= day < valid_from or not row.get("candidate"):
            continue
        features = row.get("c1_candidate_features")
        if features is None or any(type(features.get(key)) not in (int, float) for key in KEYS):
            continue
        from math import isfinite
        if not all(isfinite(features[key]) for key in KEYS):
            continue
        # Session and feature timestamp must refer to the same NY day.
        if datetime.fromtimestamp(row["as_of_ms"]/1000, NY).date() != day:
            raise ValueError("C1 candidate session/timestamp mismatch")
        selected.append((str(day), row["as_of_ms"], features))
    selected.sort(key=lambda row:(row[0], row[1]))
    if len(selected) < min_candidates:
        raise ValueError(f"Need {min_candidates} prior native HYPE candidates; found {len(selected)}")
    if len({row[0] for row in selected}) != len(selected):
        raise ValueError("Duplicate HYPE candidates in a session")
    digest = hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return dict(venue=venue, symbol="HYPEUSDT", policy="c1_delta",
                calibration_rule=f"Median of prior {lookback_days} days' pre-entry C1 candidates; no outcomes",
                source_sha256=digest, candidate_count=len(selected),
                trained_through=selected[-1][0], valid_from=str(valid_from),
                valid_to_exclusive=str(valid_from+timedelta(days=30)),
                thresholds={key:median(row[2][key] for row in selected) for key in KEYS})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--venue", choices=("binance", "lighter"), required=True)
    parser.add_argument("--valid-from", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact = calibrate(args.database, args.venue, args.valid_from)
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.write_text(json.dumps(artifact, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps({"output":str(args.output), "candidate_count":artifact["candidate_count"],
                      "valid_to_exclusive":artifact["valid_to_exclusive"]}))


if __name__ == "__main__":
    main()
