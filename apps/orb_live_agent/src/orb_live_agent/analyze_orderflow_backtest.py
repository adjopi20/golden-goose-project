from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_dirs(root: Path) -> list[Path]:
    if (root / "trades.jsonl").exists():
        return [root]
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "trades.jsonl").exists())


def _bucket(value: float | None, cuts: tuple[float, ...]) -> str:
    if value is None:
        return "missing"
    lo = "-inf"
    for cut in cuts:
        if value < cut:
            return f"{lo}..{cut:g}"
        lo = f"{cut:g}"
    return f"{lo}..inf"


def _side_qty_ratio(row: dict[str, Any]) -> float | None:
    direction = row["direction"]
    support = float(row["sell_bubble_qty"] if direction == "short" else row["buy_bubble_qty"])
    oppose = float(row["buy_bubble_qty"] if direction == "short" else row["sell_bubble_qty"])
    if support == 0 and oppose == 0:
        return None
    if oppose == 0:
        return float("inf")
    return support / oppose


def _signed(row: dict[str, Any], field: str) -> float | None:
    value = row.get(field)
    if not isinstance(value, (int, float)):
        return None
    return float(value) if row["direction"] == "long" else -float(value)


def _rows(run_dir: Path) -> list[dict[str, Any]]:
    trades = _read_jsonl(run_dir / "trades.jsonl")
    decisions = _read_jsonl(run_dir / "decisions.jsonl")
    orders = _read_jsonl(run_dir / "paper_orders.jsonl")
    decisions_by_ts = {int(r["decision"]["snapshot_timestamp_ms"]): r for r in decisions if r.get("decision", {}).get("decision") == "TAKE"}
    opens_by_ts = {int(r["timestamp_ms"]): r for r in orders if r.get("event") == "paper_open"}
    out: list[dict[str, Any]] = []
    for trade in trades:
        ts = int(trade["entry_timestamp_ms"])
        decision = decisions_by_ts.get(ts, {}).get("decision", {})
        features = decision.get("orderflow_features") or {}
        position = (opens_by_ts.get(ts) or {}).get("position") or {}
        risk_dollars = float(position.get("initial_risk", 0.0)) * float(position.get("qty_total", 0.0))
        row = {
            "run": run_dir.name,
            "entry_time": trade.get("entry_time"),
            "exit_time": trade.get("exit_time"),
            "direction": trade.get("direction"),
            "close_reason": trade.get("close_reason"),
            "pnl": float(trade.get("pnl", 0.0)),
            "r": float(trade.get("pnl", 0.0)) / risk_dollars if risk_dollars else None,
            "decision_reason": decision.get("reason"),
            **features,
        }
        row["directional_delta_ratio"] = _signed(row, "candle_delta_ratio")
        row["directional_cvd_recent_30"] = _signed(row, "cvd_recent_30")
        row["supportive_bubble_qty_ratio"] = _side_qty_ratio(row)
        row["two_sided_bubbles"] = float(row.get("buy_bubble_qty", 0.0)) > 0 and float(row.get("sell_bubble_qty", 0.0)) > 0
        out.append(row)
    return out


def _group(rows: list[dict[str, Any]], key: str) -> list[tuple[str, dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key))].append(row)
    out = []
    for value, subset in groups.items():
        rs = [float(r["r"]) for r in subset if isinstance(r.get("r"), (int, float))]
        out.append((value, {
            "trades": len(subset),
            "wins": sum(1 for r in subset if float(r["pnl"]) > 0),
            "win_rate": sum(1 for r in subset if float(r["pnl"]) > 0) / len(subset),
            "avg_r": mean(rs) if rs else 0.0,
            "total_r": sum(rs),
        }))
    return sorted(out, key=lambda x: (x[1]["total_r"], x[1]["trades"]))


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    body = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        body.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(body)


def _fmt(value: Any) -> str:
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rs = [float(r["r"]) for r in rows if isinstance(r.get("r"), (int, float))]
    return {
        "trades": len(rows),
        "wins": sum(1 for r in rows if float(r["pnl"]) > 0),
        "win_rate": sum(1 for r in rows if float(r["pnl"]) > 0) / len(rows) if rows else 0.0,
        "avg_r": mean(rs) if rs else 0.0,
        "total_r": sum(rs),
    }


def _entry_date(row: dict[str, Any]) -> date:
    return date.fromisoformat(str(row["entry_time"])[:10])


def _threshold_report(rows: list[dict[str, Any]], title: str, field: str, thresholds: tuple[float, ...]) -> list[str]:
    splits = [
        ("WF1", date(2024, 12, 31), date(2025, 1, 1), date(2025, 6, 30)),
        ("WF2", date(2025, 6, 30), date(2025, 7, 1), date(2025, 12, 31)),
        ("WF3", date(2025, 12, 31), date(2026, 1, 1), date(2026, 6, 30)),
    ]
    selected_rows: list[list[Any]] = []
    stability: dict[float, list[dict[str, Any]]] = {threshold: [] for threshold in thresholds}
    for name, train_end, test_start, test_end in splits:
        train = [r for r in rows if _entry_date(r) <= train_end]
        test = [r for r in rows if test_start <= _entry_date(r) <= test_end]
        if not train or not test:
            continue
        baseline = _stats(test)
        scored = []
        for threshold in thresholds:
            train_keep = [r for r in train if isinstance(r.get(field), (int, float)) and float(r[field]) >= threshold]
            test_keep = [r for r in test if isinstance(r.get(field), (int, float)) and float(r[field]) >= threshold]
            train_stats = _stats(train_keep)
            test_stats = _stats(test_keep)
            stability[threshold].append(test_stats)
            scored.append((threshold, train_stats, test_stats))
        threshold, train_stats, test_stats = max(scored, key=lambda item: (item[1]["total_r"], item[1]["trades"]))
        selected_rows.append([
            name,
            f"<= {train_end}",
            f"{test_start}..{test_end}",
            f">= {threshold:g}",
            train_stats["trades"],
            f"{train_stats['total_r']:.2f}",
            baseline["trades"],
            f"{baseline['total_r']:.2f}",
            test_stats["trades"],
            f"{test_stats['total_r']:.2f}",
        ])
    stability_rows = []
    for threshold, chunks in stability.items():
        trades = sum(int(c["trades"]) for c in chunks)
        wins = sum(int(c["wins"]) for c in chunks)
        total_r = sum(float(c["total_r"]) for c in chunks)
        stability_rows.append([f">= {threshold:g}", trades, wins, f"{wins / trades:.1%}" if trades else "0.0%", f"{total_r:.2f}", f"{total_r / trades:.2f}" if trades else "0.00"])
    return [
        f"## Walk-Forward: {title}",
        "",
        _table(
            ["split", "train", "test", "picked_on_train", "train_trades", "train_R", "test_base_trades", "test_base_R", "test_keep_trades", "test_keep_R"],
            selected_rows,
        ),
        "",
        f"## OOS Stability: {title}",
        "",
        _table(["rule", "test_trades", "test_wins", "test_win_rate", "test_total_R", "test_avg_R"], stability_rows),
        "",
    ]


def report(root: Path) -> str:
    if not root.exists():
        raise SystemExit(f"Backtest path not found: {root}")
    rows = [row for run_dir in _run_dirs(root) for row in _rows(run_dir)]
    if not rows:
        return f"# Orderflow Backtest Analysis\n\nNo trades found under `{root}`.\n"

    for row in rows:
        row["volume_expansion_bucket"] = _bucket(row.get("volume_expansion_ratio"), (0.75, 1.5, 3.0, 8.0))
        row["directional_delta_bucket"] = _bucket(row.get("directional_delta_ratio"), (-0.25, 0.0, 0.25, 0.6))
        row["supportive_bubble_qty_ratio_bucket"] = _bucket(row.get("supportive_bubble_qty_ratio"), (0.5, 1.0, 2.0, 5.0))

    rs = [float(r["r"]) for r in rows if isinstance(r.get("r"), (int, float))]
    lines = [
        "# Orderflow Backtest Analysis",
        "",
        f"Source: `{root}`",
        "",
        f"- Trades: `{len(rows)}`",
        f"- Wins: `{sum(1 for r in rows if float(r['pnl']) > 0)}`",
        f"- Losses: `{sum(1 for r in rows if float(r['pnl']) <= 0)}`",
        f"- Total R: `{sum(rs):.2f}`",
        f"- Avg R: `{mean(rs):.2f}`",
        "",
    ]
    for key in ("direction", "close_reason", "volume_expansion_bucket", "directional_delta_bucket", "supportive_bubble_qty_ratio_bucket", "two_sided_bubbles", "max_bubble_side"):
        lines += [
            f"## By {key}",
            "",
            _table(
                [key, "trades", "wins", "win_rate", "avg_r", "total_r"],
                [[value, stats["trades"], stats["wins"], f"{stats['win_rate']:.1%}", f"{stats['avg_r']:.2f}", f"{stats['total_r']:.2f}"] for value, stats in _group(rows, key)],
            ),
            "",
        ]

    worst = sorted(rows, key=lambda r: float(r["r"] if r["r"] is not None else 0.0))[:20]
    lines += [
        "## Worst Trades",
        "",
        _table(
            ["entry", "exit", "dir", "reason", "R", "vol_exp", "dir_delta", "support_bubble_ratio", "two_sided"],
            [[r["entry_time"], r["exit_time"], r["direction"], r["close_reason"], _fmt(r["r"]), _fmt(r.get("volume_expansion_ratio")), _fmt(r.get("directional_delta_ratio")), _fmt(r.get("supportive_bubble_qty_ratio")), r["two_sided_bubbles"]] for r in worst],
        ),
        "",
    ]
    lines += _threshold_report(rows, "Volume Expansion Only", "volume_expansion_ratio", (0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 8.0))
    lines += _threshold_report(rows, "Directional Delta Only", "directional_delta_ratio", (0.0, 0.25, 0.4, 0.5, 0.6, 0.75, 0.9))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze ORB backtest orderflow features from JSONL artifacts.")
    parser.add_argument("run_dir", help="Backtest folder, or parent folder containing chunk folders.")
    parser.add_argument("--output", help="Optional markdown output path.")
    args = parser.parse_args()
    text = report(Path(args.run_dir))
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
