from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from bisect import bisect_left

import pandas as pd

from indicator.ohlcv import aggregate_trades_to_ohlcv
from indicator.volume_profile import build_volume_profile

from .config import load_config
from .paper_broker import PaperBroker
from .risk_gate import RiskGate
from .strategies.orb_trend_following import decide_algorithm
from .trigger_observer import observe_triggers


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", maxsplit=1)
    return time(int(hour), int(minute))


def _ms(day: date, t: time, tz: ZoneInfo) -> int:
    dt = datetime.combine(day, t, tzinfo=tz)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _local_day_time(timestamp_ms: int, tz: ZoneInfo) -> tuple[date, time]:
    dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).astimezone(tz)
    return dt.date(), dt.time()


def _slice(df: pd.DataFrame, ts: list[int], start_ms: int, end_ms: int) -> pd.DataFrame:
    return df.iloc[bisect_left(ts, start_ms):bisect_left(ts, end_ms)]


def _profile_for(df: pd.DataFrame, bins: int, profile_type: str, start_ms: int, end_ms: int, tz: ZoneInfo) -> dict[str, Any] | None:
    if len(df) < 2:
        return None
    profile = build_volume_profile(df, n_bins=bins)
    total_volume = sum(float(row["total_volume"]) for row in profile.get("volume_profile", []))
    if total_volume > 0:
        profile["total_volume"] = total_volume
        profile["poc_volume_pct"] = float(profile["poc_volume"]) / total_volume
    if profile.get("val") is not None and profile.get("vah") is not None:
        profile["value_area_width"] = float(profile["vah"]) - float(profile["val"])
    profile["profile_type"] = profile_type
    profile["window_start"] = datetime.fromtimestamp(start_ms / 1000.0, timezone.utc).astimezone(tz).isoformat()
    profile["window_end"] = datetime.fromtimestamp(end_ms / 1000.0, timezone.utc).astimezone(tz).isoformat()
    profile["timezone"] = str(tz)
    return profile


def _extreme(df: pd.DataFrame, session: str, day: date, tz: ZoneInfo) -> dict[str, Any] | None:
    if df.empty:
        return None
    return {
        "session": session,
        "session_day": day.isoformat(),
        "timezone": str(tz),
        "high": float(df["price"].max()),
        "low": float(df["price"].min()),
    }


def _session_contexts(df: pd.DataFrame, ts: list[int], start_day: date, end_day: date, config: Any, tz: ZoneInfo) -> dict[date, dict[str, Any]]:
    ny_open = _parse_time(config.ny_open_time)
    pre_ny_start = _parse_time(config.pre_ny_start_time)
    overnight_start = _parse_time(config.overnight_start_time)
    setup_cutoff = _parse_time(config.setup_cutoff_time)
    out: dict[date, dict[str, Any]] = {}
    for offset in range((end_day - start_day).days + 1):
        day = start_day + timedelta(days=offset)
        ny_open_ms = _ms(day, ny_open, tz)
        orb_end_ms = ny_open_ms + 15 * 60_000
        prior_start_ms = ny_open_ms - 24 * 60 * 60_000

        prior24 = _slice(df, ts, prior_start_ms, ny_open_ms)
        orb = _slice(df, ts, ny_open_ms, orb_end_ms)
        previous_ny = _slice(df, ts, _ms(day - timedelta(days=1), ny_open, tz), _ms(day - timedelta(days=1), setup_cutoff, tz))
        overnight = _slice(df, ts, _ms(day - timedelta(days=1), overnight_start, tz), _ms(day, pre_ny_start, tz))
        pre_ny = _slice(df, ts, _ms(day, pre_ny_start, tz), ny_open_ms)

        previous_profile = _profile_for(prior24, config.volume_profile_bins, "previous_24h_profile_for_session", prior_start_ms, ny_open_ms, tz)
        if previous_profile:
            previous_profile["frozen_at_session_open"] = True
        orb_profile = _profile_for(orb, config.volume_profile_bins, "ny_first_15m_profile", ny_open_ms, orb_end_ms, tz)
        if orb_profile:
            orb_profile["frozen_at_window_end"] = True

        out[day] = {
            "session_extremes": {
                "pre_ny": _extreme(pre_ny, "pre_ny", day, tz),
                "overnight": _extreme(overnight, "overnight", day, tz),
                "previous_ny": _extreme(previous_ny, "ny", day - timedelta(days=1), tz),
            },
            "previous_24h_profile_for_session": previous_profile,
            "ny_first_15m_profile": orb_profile,
            "p95_qty": float(prior24["qty"].quantile(config.bubble_percentile)) if not prior24.empty else None,
        }
    return out


def _bubbles(df: pd.DataFrame, p95_qty: float | None) -> list[dict[str, Any]]:
    if p95_qty is None or df.empty:
        return []
    rows = df.loc[df["qty"] >= p95_qty]
    return [{"price": float(r.price), "qty": float(r.qty), "is_buyer_maker": bool(r.is_buyer_maker)} for r in rows.itertuples()]


def _row_to_trade(row: pd.Series) -> dict[str, Any]:
    return {"timestamp": int(row["timestamp"]), "price": float(row["price"])}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n")


def _stamp(events: list[dict[str, Any]], timestamp_ms: int, tz: ZoneInfo) -> list[dict[str, Any]]:
    for event in events:
        event["timestamp_ms"] = int(timestamp_ms)
        event["time"] = datetime.fromtimestamp(timestamp_ms / 1000.0, timezone.utc).astimezone(tz).isoformat()
    return events


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    tz = ZoneInfo(config.session_timezone)
    start_day = _parse_date(args.start_date)
    end_day = _parse_date(args.end_date)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    preload_ms = _ms(start_day, _parse_time(config.ny_open_time), tz) - 25 * 60 * 60_000
    replay_end_ms = _ms(end_day + timedelta(days=1), _parse_time(config.pre_ny_start_time), tz) + 60 * 60_000
    df = pd.read_parquet(args.input, filters=[("timestamp", ">=", preload_ms), ("timestamp", "<", replay_end_ms)])
    df = df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    ts = [int(v) for v in df["timestamp"].to_list()]
    candles = aggregate_trades_to_ohlcv(df, config.symbol, "1m")
    contexts = _session_contexts(df, ts, start_day, end_day, config, tz)

    gate = RiskGate(config.paper_min_stop_risk_pct, config.paper_max_stop_risk_pct)
    broker = PaperBroker(config)
    orders: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []

    for candle in candles:
        candle_ms = int(candle["timestamp_ms"])
        if broker.position and broker.position.max_hold_exit_ms is not None and candle_ms >= broker.position.max_hold_exit_ms:
            idx = bisect_left(ts, int(broker.position.max_hold_exit_ms))
            if int(idx) < len(df):
                force_ts = int(df.iloc[int(idx)]["timestamp"])
                orders.extend(_stamp(broker.on_trade(_row_to_trade(df.iloc[int(idx)])), force_ts, tz))
        orders.extend(_stamp(broker.on_candle(candle), candle_ms, tz))
        recent.append(candle)

        day, local_t = _local_day_time(candle_ms, tz)
        if day < start_day or day > end_day or not (time(9, 45) <= local_t < time(10, 15)):
            continue
        context = contexts.get(day)
        if not context:
            continue
        snapshot = {
            "symbol": config.symbol,
            "snapshot_timestamp_ms": candle_ms,
            "target_session_day": day.isoformat(),
            "session_timezone": config.session_timezone,
            "setup_observation_active": True,
            "last_candle": candle,
            "recent_candles": recent[-30:],
            **context,
        }
        minute_trades = _slice(df, ts, candle_ms, candle_ms + 60_000)
        trigger = observe_triggers(snapshot, _bubbles(minute_trades, context.get("p95_qty")), context)
        decision = decide_algorithm(snapshot, trigger)
        gate_result = gate.validate(decision, broker.has_open_position())
        open_event = broker.on_decision(decision, gate_result, snapshot)
        decisions.append({"decision": decision, "gate": gate_result, "trigger": trigger})
        if open_event:
            open_event["timestamp_ms"] = candle_ms
            open_event["time"] = datetime.fromtimestamp(candle_ms / 1000.0, timezone.utc).astimezone(tz).isoformat()
            orders.append(open_event)

    trades = _trades_from_orders(orders)
    summary = {
        "event": "fast_orb_backtest_finished",
        "rows_loaded": int(len(df)),
        "candles": int(len(candles)),
        "trades_taken": len(trades),
        "wins": sum(1 for t in trades if t["pnl"] > 0),
        "losses": sum(1 for t in trades if t["pnl"] <= 0),
        "net_pnl": sum(float(t["pnl"]) for t in trades),
        "final_equity": broker.equity,
        "output_dir": str(output_dir),
    }
    _write_jsonl(output_dir / "paper_orders.jsonl", orders)
    _write_jsonl(output_dir / "decisions.jsonl", decisions)
    _write_jsonl(output_dir / "trades.jsonl", trades)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def _trades_from_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for order in orders:
        if order.get("event") == "paper_open":
            pos = order["position"]
            current = {
                "direction": pos["direction"],
                "entry_time": order.get("time"),
                "entry_timestamp_ms": order.get("timestamp_ms"),
                "entry": order["entry_fill"],
                "stop_loss": pos["stop_loss"],
                "entry_fee": order["entry_fee"],
                "pnl": -float(order["entry_fee"]),
                "close_reason": None,
            }
        elif current and order.get("event") in {"paper_tp1", "paper_close"}:
            current["pnl"] += float(order.get("pnl", 0.0))
            if order.get("event") == "paper_close":
                current["exit_time"] = order.get("time")
                current["exit_timestamp_ms"] = order.get("timestamp_ms")
                current["exit"] = order.get("exit_fill")
                current["close_reason"] = order.get("reason")
                current["close_equity"] = order.get("equity")
                trades.append(current)
                current = None
    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description="Fast no-lookahead ORB strategy backtest.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", required=True)
    print(json.dumps(run(parser.parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
