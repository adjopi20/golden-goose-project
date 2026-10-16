"""Offline parity and funded 50:50 portfolio replay. Never updates a live DB."""
import argparse
import json
from pathlib import Path
import sqlite3

import pandas as pd

from .data import normalize, prepare
from .engine import apply_day, configuration, initialize
from .report import report
from .storage import connect


def replay(raw, start, end, capital="10000", database=":memory:"):
    config = configuration()
    daily = prepare(normalize(raw), config)
    daily = daily.loc[daily.date.between(start, end)]
    if daily.empty or daily.date.iloc[0] != start or daily.date.iloc[-1] != end:
        raise ValueError("Requested replay dates must be fully available")
    if daily["percentile_4y_p5_p95_entry"].isna().any():
        raise ValueError("Replay starts before full warmup")
    c = connect(database)
    if c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
        c.close()
        raise ValueError("Replay requires an empty database")
    try:
        with c:
            initialize(c, config, capital, "paper", (pd.Timestamp(start, tz="UTC") + pd.Timedelta(days=1)).isoformat())
            for row in daily.to_dict("records"):
                when = (pd.Timestamp(row["date"], tz="UTC") + pd.Timedelta(days=1)).isoformat()
                apply_day(c, row, config, when, "paper", historical=True)
        result = report(c)
        result["horizon"] = dict(start=start, end=end, initial_capital=float(capital))
        result["timing"] = "Historical next-close lag proxy; not archived first-publication vintages or forward execution"
        curves = pd.read_sql_query("SELECT * FROM marks ORDER BY date,account", c)
        fills = [dict(r) for r in c.execute("SELECT fills.*, actions.account, actions.signal_date FROM fills JOIN actions ON actions.id=fills.action_id ORDER BY price_time,fills.id")]
        return result, curves, fills
    finally:
        c.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--tail", type=Path)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--capital", default="10000")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise FileExistsError("Use a fresh output directory")
    raw = pd.read_csv(args.input)
    if args.tail:
        raw = pd.concat([raw, pd.read_csv(args.tail)], ignore_index=True)
    result, curves, fills = replay(raw, args.start, args.end, args.capital)
    args.output.mkdir(parents=True)
    (args.output / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (args.output / "fills.json").write_text(json.dumps(fills, indent=2) + "\n", encoding="utf-8")
    curves.to_parquet(args.output / "equity.parquet", index=False)
    print(json.dumps(result["portfolio"], indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
