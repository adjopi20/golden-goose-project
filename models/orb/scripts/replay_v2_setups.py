from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
RAW_DEFAULT = ROOT / "storage/avaxusdc/parquet/AVAXUSDC-aggTrades-2024-06_to_2026-05.parquet"
BARS_DEFAULT = ROOT / "models/orb/runs/20260626_013_backward_profile_behavior/outputs/rr_check/one_minute_bars_cache.parquet"
WIB = "Asia/Jakarta"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay frozen ORB V2 candidate setups on raw aggTrades.")
    p.add_argument("--setups", required=True, help="CSV with date,name,type,direction,signal_time,entry_time,sl,flow_start,flow_end,basis; optional tp1_price,tp2_price,protect_after_tp1,trail_after_tp1_fraction,protect_at_r,protect_bubble_qty_threshold,protect_bubble_notional_threshold,entry_model,flow_mode")
    p.add_argument("--out", required=True, help="Output folder")
    p.add_argument("--raw", default=str(RAW_DEFAULT))
    p.add_argument("--bars", default=str(BARS_DEFAULT))
    p.add_argument("--tp-r", type=float, default=4.0)
    p.add_argument("--trail-r", type=float, default=1.0)
    p.add_argument("--replay-hours", type=float, default=8.0)
    p.add_argument("--fee-rate", type=float, default=0.0)
    p.add_argument("--slippage-rate", type=float, default=0.0)
    p.add_argument("--allow-overlap", action="store_true")
    return p.parse_args()


def wib(value) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize(WIB) if t.tzinfo is None else t.tz_convert(WIB)


def ms(value: pd.Timestamp) -> int:
    return int(value.tz_convert("UTC").timestamp() * 1000)


def fmt(value) -> str:
    if pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def load_bars(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index).tz_convert(WIB)
    return df.sort_index()


def load_ticks(path: Path, setups: pd.DataFrame, replay_hours: float) -> pd.DataFrame:
    starts = pd.concat([setups["flow_start"].map(wib), setups["entry_time"].map(wib)])
    ends = setups["entry_time"].map(wib) + pd.to_timedelta(replay_hours, unit="h")
    df = pd.read_parquet(
        path,
        columns=["price", "qty", "timestamp", "is_buyer_maker"],
        filters=[("timestamp", ">=", ms(starts.min() - pd.Timedelta(minutes=5))), ("timestamp", "<=", ms(ends.max()))],
    )
    df["dt_wib"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_convert(WIB)
    df["notional"] = df["price"] * df["qty"]
    df["side"] = df["is_buyer_maker"].map(lambda x: "sell" if bool(x) else "buy")
    return df.sort_values("timestamp").reset_index(drop=True)


def flow_stats(bars: pd.DataFrame, setup: dict) -> dict:
    start = wib(setup["flow_start"])
    end = wib(setup["flow_end"])
    entry = wib(setup["entry_time"])
    assert end + pd.Timedelta(minutes=1) <= entry, f"flow candle not closed before entry: {setup['name']}"
    window = bars.loc[start:end]
    assert not window.empty, f"missing flow bars: {setup['name']}"
    delta = float(window["delta"].sum())
    volume = float(window["volume"].sum())
    buy = float(window["buy_volume"].sum())
    sell = float(window["sell_volume"].sum())
    mode = str(setup.get("flow_mode", "same_side")).strip().lower()
    if mode == "counter_absorption":
        flow_ok = delta < 0 if setup["direction"] == "long" else delta > 0
    else:
        flow_ok = delta > 0 if setup["direction"] == "long" else delta < 0
    return {
        "flow_delta": delta,
        "flow_mode": mode,
        "flow_volume": volume,
        "flow_buy": buy,
        "flow_sell": sell,
        "flow_buy_pct": buy / volume if volume else 0.0,
        "flow_ok": flow_ok,
        "closed_flow_bars": len(window),
    }


def first_tick(ticks: pd.DataFrame, value) -> pd.Series:
    rows = ticks.loc[ticks.dt_wib >= wib(value)]
    assert not rows.empty, f"no tick at/after {value}"
    return rows.iloc[0]


def enabled(value) -> bool:
    return pd.notna(value) and str(value).strip().lower() in {"1", "true", "yes", "y"}


def optional_float(value):
    if pd.isna(value) or str(value).strip() == "":
        return None
    return float(value)


def make_result(setup: dict, entry_time, entry, risk, tp1, tp1_time, tp1_exit, exit_time, exit_price, reason, tp1_r: float, protect_time=pd.NaT, protect_trigger="", protect_level=pd.NA) -> dict:
    if pd.isna(tp1_exit):
        if setup["direction"] == "long":
            weighted_r = (float(exit_price) - entry) / risk
        else:
            weighted_r = (entry - float(exit_price)) / risk
    elif setup["direction"] == "long":
        weighted_r = 0.5 * tp1_r + 0.5 * ((float(exit_price) - entry) / risk)
    else:
        weighted_r = 0.5 * tp1_r + 0.5 * ((entry - float(exit_price)) / risk)
    return {
        "date": setup["date"],
        "trade": setup["name"],
        "entry_model": setup.get("entry_model", "trend_following"),
        "setup_type": setup["type"],
        "direction": setup["direction"],
        "signal_time_wib": setup["signal_time"],
        "entry_time_wib": entry_time,
        "entry": entry,
        "sl": float(setup["sl"]),
        "risk": risk,
        "tp1": tp1,
        "tp1_r": tp1_r,
        "tp1_time_wib": tp1_time,
        "tp1_exit": tp1_exit,
        "runner_exit_time_wib": exit_time,
        "runner_exit": exit_price,
        "runner_reason": reason,
        "pre_tp_protect_time_wib": protect_time,
        "pre_tp_protect_trigger": protect_trigger,
        "pre_tp_protect_level": protect_level,
        "weighted_r": weighted_r,
        "decision_basis": setup.get("basis", ""),
    }


def replay(ticks: pd.DataFrame, setup: dict, tp_r: float, trail_r: float) -> dict:
    entry_tick = first_tick(ticks, setup["entry_time"])
    assert wib(setup["signal_time"]) < entry_tick.dt_wib, f"entry before signal close: {setup['name']}"
    direction = setup["direction"]
    entry = float(entry_tick.price)
    sl = float(setup["sl"])
    risk = entry - sl if direction == "long" else sl - entry
    assert risk > 0, f"invalid risk: {setup['name']}"
    fixed_tp1 = setup.get("tp1_price")
    if pd.notna(fixed_tp1):
        tp1 = float(fixed_tp1)
        assert (tp1 > entry if direction == "long" else tp1 < entry), f"invalid fixed TP1: {setup['name']}"
    else:
        tp1 = entry + tp_r * risk if direction == "long" else entry - tp_r * risk
    tp1_r = abs(tp1 - entry) / risk
    fixed_tp2 = setup.get("tp2_price")
    tp2 = float(fixed_tp2) if pd.notna(fixed_tp2) else None
    if tp2 is not None:
        assert (tp2 > tp1 if direction == "long" else tp2 < tp1), f"invalid fixed TP2: {setup['name']}"
    protect_after_tp1 = enabled(setup.get("protect_after_tp1"))
    trail_fraction = setup.get("trail_after_tp1_fraction")
    fixed_trail_distance = abs(tp1 - entry) * float(trail_fraction) if pd.notna(trail_fraction) else None
    protect_at_r = optional_float(setup.get("protect_at_r"))
    bubble_qty_threshold = optional_float(setup.get("protect_bubble_qty_threshold"))
    bubble_notional_threshold = optional_float(setup.get("protect_bubble_notional_threshold"))
    supportive_side = "buy" if direction == "long" else "sell"
    known_bubble_levels = []
    if bubble_qty_threshold is not None or bubble_notional_threshold is not None:
        prior = ticks.loc[(ticks.dt_wib >= wib(setup["flow_start"])) & (ticks.dt_wib < entry_tick.dt_wib)]
        for row in prior.itertuples(index=False):
            if row.side != supportive_side:
                continue
            if (bubble_qty_threshold is not None and float(row.qty) >= bubble_qty_threshold) or (bubble_notional_threshold is not None and float(row.notional) >= bubble_notional_threshold):
                level = float(row.price)
                if (direction == "long" and level > entry) or (direction == "short" and level < entry):
                    known_bubble_levels.append(level)
    tp1_hit = False
    tp1_time = pd.NaT
    best = entry
    runner_stop = sl
    pre_tp_stop = sl
    protect_time = pd.NaT
    protect_trigger = ""
    protect_level = pd.NA

    for row in ticks.loc[ticks.dt_wib >= entry_tick.dt_wib].itertuples(index=False):
        price = float(row.price)
        if not tp1_hit:
            if pd.isna(protect_time):
                r_level = entry + protect_at_r * risk if direction == "long" and protect_at_r is not None else entry - protect_at_r * risk if protect_at_r is not None else None
                if r_level is not None and (price >= r_level if direction == "long" else price <= r_level):
                    pre_tp_stop = entry
                    protect_time = row.dt_wib
                    protect_trigger = f"{protect_at_r:g}R"
                    protect_level = r_level
                if row.side == supportive_side and ((bubble_qty_threshold is not None and float(row.qty) >= bubble_qty_threshold) or (bubble_notional_threshold is not None and float(row.notional) >= bubble_notional_threshold)):
                    level = price
                    if (direction == "long" and level > entry) or (direction == "short" and level < entry):
                        known_bubble_levels.append(level)
                if pd.isna(protect_time) and known_bubble_levels:
                    passed = [x for x in known_bubble_levels if price >= x] if direction == "long" else [x for x in known_bubble_levels if price <= x]
                    if passed:
                        pre_tp_stop = entry
                        protect_time = row.dt_wib
                        protect_trigger = "supportive_bubble"
                        protect_level = max(passed) if direction == "long" else min(passed)
            stopped = price <= pre_tp_stop if direction == "long" else price >= pre_tp_stop
            hit_tp = price >= tp1 if direction == "long" else price <= tp1
            if stopped:
                reason = "protected_stop_pre_tp1" if pre_tp_stop == entry else "initial_stop"
                return make_result(setup, entry_tick.dt_wib, entry, risk, tp1, pd.NaT, pd.NA, row.dt_wib, pre_tp_stop, reason, tp1_r, protect_time, protect_trigger, protect_level)
            if hit_tp:
                tp1_hit = True
                tp1_time = row.dt_wib
                best = max(best, price) if direction == "long" else min(best, price)
                if fixed_trail_distance is not None:
                    runner_stop = best - fixed_trail_distance if direction == "long" else best + fixed_trail_distance
                else:
                    runner_stop = entry if protect_after_tp1 else entry
                continue
            continue

        if tp2 is not None:
            hit_tp2 = price >= tp2 if direction == "long" else price <= tp2
            protected = price <= runner_stop if direction == "long" else price >= runner_stop
            if hit_tp2:
                return make_result(setup, entry_tick.dt_wib, entry, risk, tp1, tp1_time, tp1, row.dt_wib, tp2, "tp2", tp1_r, protect_time, protect_trigger, protect_level)
            if protected:
                return make_result(setup, entry_tick.dt_wib, entry, risk, tp1, tp1_time, tp1, row.dt_wib, runner_stop, "protected_stop", tp1_r, protect_time, protect_trigger, protect_level)
            continue

        if direction == "long":
            best = max(best, price)
            runner_stop = max(runner_stop, best - (fixed_trail_distance if fixed_trail_distance is not None else trail_r * risk))
            crossed = price <= runner_stop
        else:
            best = min(best, price)
            runner_stop = min(runner_stop, best + (fixed_trail_distance if fixed_trail_distance is not None else trail_r * risk))
            crossed = price >= runner_stop
        if crossed:
            return make_result(setup, entry_tick.dt_wib, entry, risk, tp1, tp1_time, tp1, row.dt_wib, runner_stop, "runner_trailing_stop", tp1_r, protect_time, protect_trigger, protect_level)

    last = ticks.loc[ticks.dt_wib >= entry_tick.dt_wib].iloc[-1]
    return make_result(setup, entry_tick.dt_wib, entry, risk, tp1, tp1_time, tp1 if tp1_hit else pd.NA, last.dt_wib, float(last.price), "replay_end", tp1_r, protect_time, protect_trigger, protect_level)


def add_costs(trades: pd.DataFrame, fee_rate: float, slippage_rate: float) -> pd.DataFrame:
    if trades.empty:
        return trades
    rate = fee_rate + slippage_rate
    trades["fee_rate"] = fee_rate
    trades["slippage_rate"] = slippage_rate
    trades["total_cost_rate_per_fill"] = rate
    if rate == 0:
        trades["fill_count"] = 3
        trades.loc[trades["tp1_exit"].isna(), "fill_count"] = 2
        trades["cost_r"] = 0.0
        trades["net_r"] = trades["weighted_r"]
        return trades
    cost_r = []
    fill_count = []
    for row in trades.to_dict("records"):
        if pd.isna(row["tp1_exit"]):
            fill_count.append(2)
            cost = (float(row["entry"]) + float(row["runner_exit"])) * rate / float(row["risk"])
        else:
            fill_count.append(3)
            cost = (float(row["entry"]) + 0.5 * float(row["tp1_exit"]) + 0.5 * float(row["runner_exit"])) * rate / float(row["risk"])
        cost_r.append(cost)
    trades["fill_count"] = fill_count
    trades["cost_r"] = cost_r
    trades["net_r"] = trades["weighted_r"] - trades["cost_r"]
    return trades


def write_findings(out: Path, trades: pd.DataFrame, attempts: pd.DataFrame) -> None:
    lines = [
        "# ORB V2 Setup Replay",
        "",
        "Raw aggTrade execution; closed 1m candle order-flow gate; no fees; no slippage.",
        "",
        "## Taken Trades",
        "",
        "| Date | Trade | Model | Type | Dir | Entry WIB | Entry | SL | Risk | TP1 | TP1R | TP1 WIB | Runner Exit WIB | Runner Exit | Flow Delta | Buy% | Reason | R |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for t in trades.to_dict("records"):
        lines.append(
            f"| {t['date']} | {t['trade']} | {t['entry_model']} | {t['setup_type']} | {t['direction']} | {t['entry_time_wib']} | "
            f"{float(t['entry']):.3f} | {float(t['sl']):.3f} | {float(t['risk']):.3f} | {float(t['tp1']):.3f} | {float(t['tp1_r']):.2f} | {t['tp1_time_wib']} | "
            f"{t['runner_exit_time_wib']} | {float(t['runner_exit']):.3f} | {float(t['flow_delta']):.2f} | {float(t['flow_buy_pct']):.2%} | {t['runner_reason']} | {float(t['weighted_r']):.2f} |"
        )
    lines += ["", "## Attempted Setups", "", "| Date | Setup | Dir | Signal | Flow Window | Flow Delta | Buy% | Status |", "|---|---|---|---|---|---:|---:|---|"]
    for t in attempts.to_dict("records"):
        lines.append(f"| {t['date']} | {t['name']} | {t['direction']} | {t['signal_time']} | {t['flow_start']} to {t['flow_end']} | {float(t['flow_delta']):.2f} | {float(t['flow_buy_pct']):.2%} | {t['status']} |")
    lines += [
        "",
        "## Summary",
        "",
        f"- Trades: `{len(trades)}`",
        f"- Wins: `{int((trades['weighted_r'] > 0).sum()) if not trades.empty else 0}`",
        f"- Total R: `{float(trades['weighted_r'].sum()) if not trades.empty else 0.0:.2f}`",
        f"- Average R: `{float(trades['weighted_r'].mean()) if not trades.empty else 0.0:.2f}`",
    ]
    (out / "findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    setups = pd.read_csv(args.setups)
    bars = load_bars(Path(args.bars))
    ticks = load_ticks(Path(args.raw), setups, args.replay_hours)

    attempts = []
    results = []
    last_exit_by_date = {}
    earliest = pd.Timestamp.min.tz_localize("UTC").tz_convert(WIB)
    for setup in setups.to_dict("records"):
        setup["direction"] = str(setup["direction"]).lower()
        flow = flow_stats(bars, setup)
        row = {**setup, **flow, "status": "taken" if flow["flow_ok"] else "skipped_flow_filter"}
        attempts.append(row)
        if not flow["flow_ok"]:
            continue
        if not args.allow_overlap and first_tick(ticks, setup["entry_time"]).dt_wib <= last_exit_by_date.get(setup["date"], earliest):
            attempts[-1]["status"] = "skipped_overlap"
            continue
        trade = replay(ticks, {**setup, **flow}, args.tp_r, args.trail_r)
        trade.update(flow)
        results.append(trade)
        last_exit_by_date[setup["date"]] = trade["runner_exit_time_wib"]

    trades = pd.DataFrame(results)
    attempts_df = pd.DataFrame(attempts)
    raw_trades = trades.copy()
    for frame in (trades, attempts_df):
        for col in ["entry_time_wib", "tp1_time_wib", "runner_exit_time_wib", "pre_tp_protect_time_wib"]:
            if col in frame:
                frame[col] = frame[col].map(fmt)
    trades = add_costs(trades, args.fee_rate, args.slippage_rate)
    trades.to_csv(out / "trades.csv", index=False)
    attempts_df.to_csv(out / "attempted_setups.csv", index=False)
    pd.DataFrame([{
        "trades": len(raw_trades),
        "wins": int((raw_trades["weighted_r"] > 0).sum()) if not raw_trades.empty else 0,
        "total_r": float(raw_trades["weighted_r"].sum()) if not raw_trades.empty else 0.0,
        "avg_r": float(raw_trades["weighted_r"].mean()) if not raw_trades.empty else 0.0,
    }]).to_csv(out / "summary.csv", index=False)
    closed_flow_ok = all(wib(row["flow_end"]) + pd.Timedelta(minutes=1) <= wib(row["entry_time"]) for row in attempts)
    (out / "no_lookahead_audit.json").write_text(json.dumps({
        "setups": str(Path(args.setups).resolve()),
        "raw": str(Path(args.raw).resolve()),
        "bars": str(Path(args.bars).resolve()),
        "flow_source": "already-closed 1m bars",
        "execution_source": "raw aggTrade prices",
        "all_flow_windows_closed_before_entry": closed_flow_ok,
        "fixed_tp1_price_setups": int(setups["tp1_price"].notna().sum()) if "tp1_price" in setups else 0,
        "fixed_tp2_price_setups": int(setups["tp2_price"].notna().sum()) if "tp2_price" in setups else 0,
        "fractional_tp1_trail_setups": int(setups["trail_after_tp1_fraction"].notna().sum()) if "trail_after_tp1_fraction" in setups else 0,
        "tp1_r": args.tp_r,
        "runner_trail_r": args.trail_r,
        "fee_rate": args.fee_rate,
        "slippage_rate": args.slippage_rate,
        "protect_at_r_setups": int(setups["protect_at_r"].notna().sum()) if "protect_at_r" in setups else 0,
        "bubble_protect_setups": int(setups["protect_bubble_qty_threshold"].notna().sum()) if "protect_bubble_qty_threshold" in setups else 0,
        "replay_hours": args.replay_hours,
        "allow_overlap": args.allow_overlap,
    }, indent=2), encoding="utf-8")
    write_findings(out, trades, attempts_df)
    print(out / "findings.md")


if __name__ == "__main__":
    main()
