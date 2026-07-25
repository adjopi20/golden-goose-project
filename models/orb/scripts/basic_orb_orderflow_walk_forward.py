from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


RULE = (
    "first follow candle body is adverse/flat AND closes inside the ORB "
    "AND directional delta is adverse/flat"
)


def _load(observation_dir: Path) -> pd.DataFrame:
    samples_path = observation_dir / "samples.jsonl"
    path_path = observation_dir / "orderflow_path.parquet"
    if not samples_path.exists() or not path_path.exists():
        raise FileNotFoundError("Observation directory must contain samples.jsonl and orderflow_path.parquet")

    samples = pd.DataFrame(
        json.loads(line)
        for line in samples_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    samples = samples[samples.get("sample", False).eq(True) & samples["result"].isin(["win", "loss"])].copy()
    samples = samples[
        ["session_day", "breakout_time", "outcome_time", "breakout_level", "direction", "result"]
    ]

    path = pd.read_parquet(path_path)
    breakout_volume = path[path["is_breakout_candle"]][
        ["session_day", "breakout_time", "volume_expansion_ratio"]
    ].rename(columns={"volume_expansion_ratio": "breakout_volume_expansion"})
    follow = path[path["minutes_from_breakout_candle"].eq(1)][
        [
            "session_day",
            "breakout_time",
            "orderflow_candle_complete_time",
            "open",
            "close",
            "directional_delta_ratio",
            "directional_bubble_qty_imbalance",
            "volume_expansion_ratio",
        ]
    ].rename(columns={"volume_expansion_ratio": "follow_volume_expansion"})

    rows = samples.merge(follow, on=["session_day", "breakout_time"], how="inner", validate="one_to_one")
    rows = rows.merge(breakout_volume, on=["session_day", "breakout_time"], how="left", validate="one_to_one")
    for column in ("breakout_time", "outcome_time", "orderflow_candle_complete_time"):
        rows[column] = pd.to_datetime(rows[column], format="mixed", utc=True)
    rows["session_day"] = pd.to_datetime(rows["session_day"])
    rows = rows[
        rows["orderflow_candle_complete_time"].le(rows["outcome_time"])
        & rows[["open", "close", "breakout_level", "directional_delta_ratio"]].notna().all(axis=1)
    ].copy()

    sign = np.where(rows["direction"].eq("long"), 1.0, -1.0)
    rows["adverse_or_flat_body"] = sign * (rows["close"] - rows["open"]) <= 0
    rows["closes_inside_orb"] = sign * (rows["close"] - rows["breakout_level"]) <= 0
    rows["adverse_or_flat_delta"] = rows["directional_delta_ratio"] <= 0
    rows["failure_score"] = rows[
        ["adverse_or_flat_body", "closes_inside_orb", "adverse_or_flat_delta"]
    ].sum(axis=1)
    rows["reject_candidate"] = rows["failure_score"].eq(3)
    rows["follow_to_breakout_volume"] = rows["follow_volume_expansion"].div(
        rows["breakout_volume_expansion"].where(rows["breakout_volume_expansion"] > 0)
    )
    return rows.sort_values(["session_day", "breakout_time"], kind="mergesort").reset_index(drop=True)


def _metrics(rows: pd.DataFrame) -> dict[str, Any]:
    rejected = rows[rows["reject_candidate"]]
    kept = rows[~rows["reject_candidate"]]
    wins = int(rows["result"].eq("win").sum())
    losses = int(rows["result"].eq("loss").sum())
    kept_wins = int(kept["result"].eq("win").sum())
    kept_losses = int(kept["result"].eq("loss").sum())
    return {
        "samples": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(rows) if len(rows) else None,
        "rejected": len(rejected),
        "rejected_wins": int(rejected["result"].eq("win").sum()),
        "rejected_losses": int(rejected["result"].eq("loss").sum()),
        "rejected_loss_rate": rejected["result"].eq("loss").mean() if len(rejected) else None,
        "kept": len(kept),
        "kept_wins": kept_wins,
        "kept_losses": kept_losses,
        "kept_win_rate": kept_wins / len(kept) if len(kept) else None,
        "win_rate_lift": (kept_wins / len(kept) - wins / len(rows)) if len(kept) and len(rows) else None,
        "trade_retention": len(kept) / len(rows) if len(rows) else None,
        "loss_removal_rate": int(rejected["result"].eq("loss").sum()) / losses if losses else None,
        "winner_removal_rate": int(rejected["result"].eq("win").sum()) / wins if wins else None,
    }


def _prefix(prefix: str, values: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}_{key}": value for key, value in values.items()}


def _walk_forward(rows: pd.DataFrame, train_months: int, test_months: int) -> pd.DataFrame:
    cursor = rows["session_day"].min().to_period("M").start_time
    last_day = rows["session_day"].max()
    folds: list[dict[str, Any]] = []
    fold = 1
    while True:
        train_start = cursor
        test_start = train_start + pd.DateOffset(months=train_months)
        test_end = test_start + pd.DateOffset(months=test_months) - pd.Timedelta(days=1)
        if test_start > last_day:
            break
        train = rows[rows["session_day"].between(train_start, test_start - pd.Timedelta(days=1))]
        test = rows[rows["session_day"].between(test_start, test_end)]
        if len(train) and len(test):
            folds.append(
                {
                    "fold": fold,
                    "train_start": train_start.date().isoformat(),
                    "train_end": (test_start - pd.Timedelta(days=1)).date().isoformat(),
                    "test_start": test_start.date().isoformat(),
                    "test_end": test_end.date().isoformat(),
                    **_prefix("train", _metrics(train)),
                    **_prefix("test", _metrics(test)),
                }
            )
            fold += 1
        cursor += pd.DateOffset(months=test_months)
    return pd.DataFrame(folds)


def _fmt(value: Any) -> str:
    return "" if value is None or pd.isna(value) else f"{float(value):.4f}"


def _markdown(summary: dict[str, Any], folds: pd.DataFrame, observation_dir: Path) -> str:
    oos = summary["oos"]
    lines = [
        "# ORB Orderflow Filter Walk-Forward",
        "",
        f"Source: `{observation_dir}`",
        "",
        f"Frozen observer rule: {RULE}.",
        "",
        "This does not change entry execution. Outcomes remain anchored to the original breakout observation.",
        "",
        f"- Folds: `{summary['folds']}`",
        f"- OOS eligible samples: `{oos['samples']}`",
        f"- OOS baseline WR: `{_fmt(oos['win_rate'])}`",
        f"- OOS filtered-label WR: `{_fmt(oos['kept_win_rate'])}`",
        f"- OOS WR lift: `{_fmt(oos['win_rate_lift'])}`",
        f"- OOS rejected losses / winners: `{oos['rejected_losses']}` / `{oos['rejected_wins']}`",
        f"- OOS trade retention: `{_fmt(oos['trade_retention'])}`",
        f"- Positive test-fold WR lift: `{summary['positive_test_folds']}` / `{summary['folds']}`",
        "",
        "Volume expansion, follow/breakout volume, and directional bubble imbalance remain exported as context fields; they are not rejection requirements.",
        "",
        "| fold | train | test | test samples | baseline WR | filtered-label WR | lift | rejected L/W | retention |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in folds.to_dict("records"):
        lines.append(
            f"| {row['fold']} | {row['train_start']}..{row['train_end']} | {row['test_start']}..{row['test_end']} | "
            f"{row['test_samples']} | {_fmt(row['test_win_rate'])} | {_fmt(row['test_kept_win_rate'])} | "
            f"{_fmt(row['test_win_rate_lift'])} | {row['test_rejected_losses']}/{row['test_rejected_wins']} | "
            f"{_fmt(row['test_trade_retention'])} |"
        )
    return "\n".join(lines) + "\n"


def run(observation_dir: Path, output_dir: Path, train_months: int = 3, test_months: int = 3) -> dict[str, Any]:
    if train_months <= 0 or test_months <= 0:
        raise ValueError("train_months and test_months must be positive")
    rows = _load(observation_dir)
    if rows.empty:
        raise ValueError("No resolved trades survived through the first completed follow candle")
    folds = _walk_forward(rows, train_months, test_months)
    if folds.empty:
        raise ValueError("Not enough dated observations for one train/test fold")

    test_ranges = [
        rows[rows["session_day"].between(pd.Timestamp(row.test_start), pd.Timestamp(row.test_end))]
        for row in folds.itertuples()
    ]
    oos_rows = pd.concat(test_ranges, ignore_index=True)
    summary = {
        "event": "basic_orb_orderflow_walk_forward_finished",
        "rule": RULE,
        "train_months": train_months,
        "test_months": test_months,
        "folds": len(folds),
        "positive_test_folds": int(folds["test_win_rate_lift"].gt(0).sum()),
        "all_eligible": _metrics(rows),
        "oos": _metrics(oos_rows),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    rows.to_parquet(output_dir / "filter_observations.parquet", index=False)
    folds.to_csv(output_dir / "walk_forward_summary.csv", index=False)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "findings.md").write_text(_markdown(summary, folds, observation_dir), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the frozen ORB follow-candle reject candidate walk-forward.")
    parser.add_argument("--observation-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--train-months", type=int, default=3)
    parser.add_argument("--test-months", type=int, default=3)
    args = parser.parse_args()
    summary = run(Path(args.observation_dir), Path(args.output_dir), args.train_months, args.test_months)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
