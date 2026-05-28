import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


TRAILING_ATR_MULTIPLE = 2.0


@dataclass
class SignalState:
    active: bool = False
    direction: str | None = None  # long | short
    signal_price: float | None = None
    signal_extreme: float | None = None  # dynamic high for long, low for short
    frozen_extreme: float | None = None
    in_pullback: bool = False
    pullback_low: float | None = None
    pullback_high: float | None = None


@dataclass
class PositionState:
    in_position: bool = False
    direction: str | None = None
    entry_idx: int | None = None
    entry_time: int | None = None
    entry_price: float | None = None
    atr_entry: float | None = None
    hard_stop_price: float | None = None
    structure_level: float | None = None
    state_regime: str | None = None
    state_pressure: str | None = None
    pullback_depth_at_entry: float | None = None
    delta_norm_at_entry: float | None = None
    mae: float = 0.0
    mfe: float = 0.0
    mfe_time: int | None = None
    flow_counter: int = 0
    time_since_last_extreme: int = 0
    max_price_since_entry: float | None = None
    min_price_since_entry: float | None = None


def build_mvp_trades(
    df: pd.DataFrame,
    fee_rate: float = 0.0004,
    atr_window: int = 300,
    delta_std_window: int = 300,
    pressure_quantile: float = 0.70,
    pullback_atr_min: float = 0.3,
    pullback_atr_max: float = 1.8,
    delta_norm_threshold: float = 2.0,
    price_norm_threshold: float = 0.5,
    max_hold_events: int = 20000,
    flow_invalidation_threshold: float = 1.0,
    flow_persistence_events: int = 3,
    trailing_atr_multiple: float = TRAILING_ATR_MULTIPLE,
    trailing_activation_mfe_atr: float = 0.5,
    decay_events: int = 200,
    no_follow_through_mfe_atr: float = 0.5,
    no_follow_through_events: int = 5,
) -> pd.DataFrame:
    use = df.copy().reset_index(drop=True)

    # ATR proxy from event-level prices.
    use["tr"] = use["price"].diff().abs()
    use["atr"] = use["tr"].rolling(atr_window, min_periods=atr_window).mean()

    # Signed and normalized event delta for unit-consistent re-acceleration checks.
    use["delta_std"] = use["delta"].rolling(delta_std_window, min_periods=delta_std_window).std()
    use["delta_norm"] = use["delta"] / use["delta_std"].replace(0, np.nan)

    # Causal pressure threshold: expanding quantile over prior trend pressures only.
    trend_abs_pressure = use["pressure_50"].abs().where(use["regime"] == "trend")
    if trend_abs_pressure.dropna().empty:
        raise ValueError("No trend rows found to derive pressure threshold.")
    pressure_threshold_series = trend_abs_pressure.shift(1).expanding(min_periods=50).quantile(pressure_quantile)

    signal = SignalState()
    pos = PositionState()
    trades: list[dict] = []

    for i, row in use.iterrows():
        price = float(row["price"])
        atr = row["atr"]
        delta_norm = row["delta_norm"]
        regime = str(row["regime"])
        pressure = float(row["pressure_50"])
        pressure_threshold = pressure_threshold_series.iloc[i]
        ts = int(row["timestamp"])

        if pd.isna(atr) or pd.isna(delta_norm):
            continue

        prev_price = float(use.loc[i - 1, "price"]) if i > 0 else price
        price_change = price - prev_price
        atr_shifted = use.loc[i - 1, "atr"] if i > 0 else np.nan
        if pd.isna(atr_shifted) or float(atr_shifted) <= 0:
            continue

        # ----- manage active position -----
        if pos.in_position:
            assert pos.entry_price is not None
            assert pos.direction is not None
            assert pos.atr_entry is not None
            assert pos.structure_level is not None
            assert pos.entry_idx is not None
            assert pos.max_price_since_entry is not None
            assert pos.min_price_since_entry is not None

            held = i - int(pos.entry_idx)

            if pos.direction == "long":
                favorable_now = price - pos.entry_price
                adverse_now = pos.entry_price - price
                structure_break = price < pos.structure_level
                flow_condition_now = delta_norm < -flow_invalidation_threshold
                trailing_armed = pos.mfe >= (trailing_activation_mfe_atr * pos.atr_entry)
                trail_stop = pos.max_price_since_entry - (trailing_atr_multiple * pos.atr_entry)
                trailing_break = trailing_armed and price < trail_stop
            else:
                favorable_now = pos.entry_price - price
                adverse_now = price - pos.entry_price
                structure_break = price > pos.structure_level
                flow_condition_now = delta_norm > flow_invalidation_threshold
                trailing_armed = pos.mfe >= (trailing_activation_mfe_atr * pos.atr_entry)
                trail_stop = pos.min_price_since_entry + (trailing_atr_multiple * pos.atr_entry)
                trailing_break = trailing_armed and price > trail_stop

            # Compute conditions using previous state only
            flow_invalid = (pos.flow_counter + (1 if flow_condition_now else 0)) >= flow_persistence_events
            decay_invalid = (pos.time_since_last_extreme + 1) >= decay_events
            safety_exit = held >= max_hold_events
            no_follow_through = (
                held > no_follow_through_events
                and pos.mfe < (no_follow_through_mfe_atr * pos.atr_entry)
            )

            # Strict exit priority: hard_stop -> structure -> flow -> trailing -> decay -> safety
            exit_reason = None
            if pos.direction == "long":
                if price <= pos.hard_stop_price:
                    exit_reason = "hard_stop"
            elif pos.direction == "short":
                if price >= pos.hard_stop_price:
                    exit_reason = "hard_stop"
                    
            if exit_reason is None and structure_break:
                exit_reason = "structure"
            elif flow_invalid:
                exit_reason = "flow"
            elif trailing_break:
                exit_reason = "trailing"
            elif no_follow_through:
                exit_reason = "no_follow_through"
            elif decay_invalid:
                exit_reason = "decay"
            elif safety_exit:
                exit_reason = "safety"

            if exit_reason is not None:
                final_mfe = max(pos.mfe, favorable_now)
                final_mae = min(pos.mae, -adverse_now)
                quantity = 1.0
                gross_pnl = (price - pos.entry_price) if pos.direction == "long" else (pos.entry_price - price)
                entry_notional = abs(pos.entry_price * quantity)
                exit_notional = abs(price * quantity)
                fee = (entry_notional + exit_notional) * fee_rate
                net_pnl = gross_pnl - fee
                trades.append(
                    {
                        "entry_idx": pos.entry_idx,
                        "exit_idx": i,
                        "entry_timestamp": pos.entry_time,
                        "exit_timestamp": ts,
                        "direction": pos.direction,
                        "hard_stop_price": pos.hard_stop_price,
                        "state_regime": pos.state_regime,
                        "state_pressure": pos.state_pressure,
                        "entry_price": pos.entry_price,
                        "exit_price": price,
                        "atr_entry": pos.atr_entry,
                        "structure_level": pos.structure_level,
                        "trailing_stop_at_exit": trail_stop,
                        "pnl": gross_pnl,
                        "gross_pnl": gross_pnl,
                        "entry_notional": entry_notional,
                        "exit_notional": exit_notional,
                        "fee": fee,
                        "net_pnl": net_pnl,
                        "pnl_r": gross_pnl / float(pos.atr_entry) if pos.atr_entry > 0 else np.nan,
                        "pullback_depth_at_entry": pos.pullback_depth_at_entry,
                        "delta_norm_at_entry": pos.delta_norm_at_entry,
                        "mae": final_mae,
                        "mfe": final_mfe,
                        "mfe_time": pos.mfe_time,
                        "max_price_since_entry": pos.max_price_since_entry,
                        "min_price_since_entry": pos.min_price_since_entry,
                        "time_since_last_extreme": pos.time_since_last_extreme,
                        "duration_events": held,
                        "exit_reason": exit_reason,
                    }
                )
                pos = PositionState()
                signal = SignalState()
                continue

            # Update state only if no exit triggered
            pos.mfe = max(pos.mfe, favorable_now)
            pos.mae = min(pos.mae, -adverse_now)
            
            # Flow and time counters updated after all exit checks
            pos.flow_counter = pos.flow_counter + 1 if flow_condition_now else 0

            if pos.direction == "long":
                if price > pos.max_price_since_entry:
                    pos.max_price_since_entry = price
                    pos.time_since_last_extreme = 0
                    pos.mfe_time = ts
                else:
                    pos.time_since_last_extreme += 1
                pos.min_price_since_entry = min(pos.min_price_since_entry, price)
            else:
                if price < pos.min_price_since_entry:
                    pos.min_price_since_entry = price
                    pos.time_since_last_extreme = 0
                    pos.mfe_time = ts
                else:
                    pos.time_since_last_extreme += 1
                pos.max_price_since_entry = max(pos.max_price_since_entry, price)

        # ----- trigger signal (trend-only, pressure-only) -----
        if not signal.active and not pos.in_position:
            if regime == "trend" and not pd.isna(pressure_threshold) and abs(pressure) >= float(pressure_threshold):
                direction = "long" if pressure > 0 else "short"
                signal = SignalState(
                    active=True,
                    direction=direction,
                    signal_price=price,
                    signal_extreme=price,
                    in_pullback=False,
                    pullback_low=price,
                    pullback_high=price,
                )
            continue

        if signal.active and not pos.in_position:
            assert signal.direction is not None
            assert signal.signal_extreme is not None

            # Update dynamic extreme until pullback begins; then freeze.
            if not signal.in_pullback:
                if signal.direction == "long":
                    signal.signal_extreme = max(float(signal.signal_extreme), price)
                    if price < float(signal.signal_extreme):
                        signal.in_pullback = True
                        signal.frozen_extreme = float(signal.signal_extreme)
                        signal.pullback_low = price
                        signal.pullback_high = float(signal.frozen_extreme)
                else:
                    signal.signal_extreme = min(float(signal.signal_extreme), price)
                    if price > float(signal.signal_extreme):
                        signal.in_pullback = True
                        signal.frozen_extreme = float(signal.signal_extreme)
                        signal.pullback_high = price
                        signal.pullback_low = float(signal.frozen_extreme)
                continue

            assert signal.frozen_extreme is not None

            # Track pullback structure after pullback phase starts.
            if signal.direction == "long":
                signal.pullback_low = min(float(signal.pullback_low), price)
                depth_atr = (float(signal.frozen_extreme) - price) / float(atr)
                pullback_mid = (float(signal.frozen_extreme) + float(signal.pullback_low)) / 2.0
                pressure_ok = pressure > float(pressure_threshold)

                depth_ok = pullback_atr_min <= depth_atr <= pullback_atr_max
                reaccel_ok = (
                    (delta_norm > delta_norm_threshold)
                    and (price_change > (price_norm_threshold * float(atr_shifted)))
                )
                direction_gate = price > pullback_mid
                if pressure_ok and depth_ok and reaccel_ok and direction_gate:
                    entry = price
                    hard_stop_price = entry - 2.5 * float(atr)
                    pos = PositionState(
                        in_position=True,
                        direction="long",
                        entry_idx=i,
                        entry_time=ts,
                        entry_price=entry,
                        atr_entry=float(atr),
                        hard_stop_price=hard_stop_price,
                        structure_level=float(signal.pullback_low),
                        state_regime=regime,
                        state_pressure="strong_up",
                        pullback_depth_at_entry=depth_atr,
                        delta_norm_at_entry=float(delta_norm),
                        mae=0.0,
                        mfe=0.0,
                        mfe_time=ts,
                        flow_counter=0,
                        time_since_last_extreme=0,
                        max_price_since_entry=entry,
                        min_price_since_entry=entry,
                    )
            else:
                signal.pullback_high = max(float(signal.pullback_high), price)
                depth_atr = (price - float(signal.frozen_extreme)) / float(atr)
                pullback_mid = (float(signal.frozen_extreme) + float(signal.pullback_high)) / 2.0
                pressure_ok = pressure < -float(pressure_threshold)

                depth_ok = pullback_atr_min <= depth_atr <= pullback_atr_max
                reaccel_ok = (
                    (delta_norm < -delta_norm_threshold)
                    and (price_change < -(price_norm_threshold * float(atr_shifted)))
                )
                direction_gate = price < pullback_mid
                if pressure_ok and depth_ok and reaccel_ok and direction_gate:
                    entry = price
                    hard_stop_price = entry + 2.5 * float(atr)
                    pos = PositionState(
                        in_position=True,
                        direction="short",
                        entry_idx=i,
                        entry_time=ts,
                        entry_price=entry,
                        atr_entry=float(atr),
                        hard_stop_price=hard_stop_price,
                        structure_level=float(signal.pullback_high),
                        state_regime=regime,
                        state_pressure="strong_down",
                        pullback_depth_at_entry=depth_atr,
                        delta_norm_at_entry=float(delta_norm),
                        mae=0.0,
                        mfe=0.0,
                        mfe_time=ts,
                        flow_counter=0,
                        time_since_last_extreme=0,
                        max_price_since_entry=entry,
                        min_price_since_entry=entry,
                    )

            # reset stale signals if trend context disappears
            if regime != "trend":
                signal = SignalState()

    if pos.in_position:
        assert pos.entry_price is not None
        assert pos.direction is not None
        assert pos.atr_entry is not None
        assert pos.structure_level is not None
        assert pos.entry_idx is not None
        assert pos.max_price_since_entry is not None
        assert pos.min_price_since_entry is not None

        exit_idx = len(use) - 1
        last_row = use.iloc[exit_idx]
        exit_price = float(last_row["price"])
        exit_ts = int(last_row["timestamp"])
        held = exit_idx - int(pos.entry_idx)

        if pos.direction == "long":
            favorable_now = exit_price - pos.entry_price
            adverse_now = pos.entry_price - exit_price
            trail_stop = pos.max_price_since_entry - (trailing_atr_multiple * pos.atr_entry)
        else:
            favorable_now = pos.entry_price - exit_price
            adverse_now = exit_price - pos.entry_price
            trail_stop = pos.min_price_since_entry + (trailing_atr_multiple * pos.atr_entry)

        final_mfe = max(pos.mfe, favorable_now)
        final_mae = min(pos.mae, -adverse_now)
        quantity = 1.0
        gross_pnl = (exit_price - pos.entry_price) if pos.direction == "long" else (pos.entry_price - exit_price)
        entry_notional = abs(pos.entry_price * quantity)
        exit_notional = abs(exit_price * quantity)
        fee = (entry_notional + exit_notional) * fee_rate
        net_pnl = gross_pnl - fee

        trades.append(
                    {
                        "entry_idx": pos.entry_idx,
                        "exit_idx": exit_idx,
                        "entry_timestamp": pos.entry_time,
                        "exit_timestamp": exit_ts,
                        "direction": pos.direction,
                        "hard_stop_price": pos.hard_stop_price,
                "state_regime": pos.state_regime,
                "state_pressure": pos.state_pressure,
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "atr_entry": pos.atr_entry,
                "structure_level": pos.structure_level,
                "trailing_stop_at_exit": trail_stop,
                "pnl": gross_pnl,
                "gross_pnl": gross_pnl,
                "entry_notional": entry_notional,
                "exit_notional": exit_notional,
                "fee": fee,
                "net_pnl": net_pnl,
                "pnl_r": gross_pnl / float(pos.atr_entry) if pos.atr_entry > 0 else np.nan,
                "pullback_depth_at_entry": pos.pullback_depth_at_entry,
                "delta_norm_at_entry": pos.delta_norm_at_entry,
                "mae": final_mae,
                "mfe": final_mfe,
                "mfe_time": pos.mfe_time,
                "max_price_since_entry": pos.max_price_since_entry,
                "min_price_since_entry": pos.min_price_since_entry,
                "time_since_last_extreme": pos.time_since_last_extreme,
                "duration_events": held,
                "exit_reason": "data_end",
            }
        )

    final_pressure_threshold = pressure_threshold_series.dropna()
    pressure_threshold_out = float(final_pressure_threshold.iloc[-1]) if not final_pressure_threshold.empty else np.nan

    return pd.DataFrame(trades), pressure_threshold_out


def summarize_trades(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(
            [
                {
                    "trades": 0,
                    "win_rate": np.nan,
                    "avg_pnl": np.nan,
                    "expectancy": np.nan,
                    "avg_mae": np.nan,
                    "avg_mfe": np.nan,
                    "avg_duration": np.nan,
                    "largest_winner": np.nan,
                }
            ]
        )

    wins = trades["pnl"] > 0
    summary = {
        "trades": int(len(trades)),
        "win_rate": float(wins.mean()),
        "avg_pnl": float(trades["pnl"].mean()),
        "expectancy": float(trades["pnl"].mean()),
        "avg_mae": float(trades["mae"].mean()),
        "avg_mfe": float(trades["mfe"].mean()),
        "avg_duration": float(trades["duration_events"].mean()),
        "largest_winner": float(trades["pnl"].max()),
    }
    return pd.DataFrame([summary])


def build_exit_reason_stats(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["exit_reason", "count", "pct"])
    counts = trades["exit_reason"].value_counts(dropna=False)
    out = counts.rename_axis("exit_reason").reset_index(name="count")
    out["pct"] = out["count"] / float(len(trades))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MVP execution backtest: trend+pressure -> pullback -> re-acceleration."
    )
    parser.add_argument(
        "--input",
        default="event_features_with_regime_direction_location.csv",
        help="Input features CSV",
    )
    parser.add_argument("--out-trades", default="mvp_trade_log.csv", help="Output trade log CSV")
    parser.add_argument("--out-summary", default="mvp_trade_summary.csv", help="Output summary CSV")
    parser.add_argument("--out-exit-stats", default="mvp_exit_reason_stats.csv", help="Exit reason stats CSV")
    parser.add_argument("--atr-window", type=int, default=300)
    parser.add_argument("--delta-std-window", type=int, default=300)
    # Discovery phase default: keep pressure filter tight (q70) unless explicitly overridden.
    parser.add_argument("--pressure-quantile", type=float, default=0.70)
    parser.add_argument("--pullback-min", type=float, default=0.3)
    parser.add_argument("--pullback-max", type=float, default=1.8)
    parser.add_argument("--delta-norm-threshold", type=float, default=2.0)
    parser.add_argument("--price-norm-threshold", type=float, default=0.5)
    parser.add_argument("--max-hold-events", type=int, default=20000)
    parser.add_argument("--flow-invalidation-threshold", type=float, default=1.0)
    parser.add_argument("--flow-persistence-events", type=int, default=3)
    parser.add_argument("--trailing-atr-multiple", type=float, default=TRAILING_ATR_MULTIPLE)
    parser.add_argument("--trailing-activation-mfe-atr", type=float, default=0.5)
    parser.add_argument("--decay-events", type=int, default=200)
    parser.add_argument("--fee-rate", type=float, default=0.0004)
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    df = pd.read_csv(input_path)
    required = {"timestamp", "price", "delta", "pressure_50", "regime"}
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    trades, pressure_threshold = build_mvp_trades(
        df,
        atr_window=args.atr_window,
        delta_std_window=args.delta_std_window,
        pressure_quantile=args.pressure_quantile,
        pullback_atr_min=args.pullback_min,
        pullback_atr_max=args.pullback_max,
        delta_norm_threshold=args.delta_norm_threshold,
        price_norm_threshold=args.price_norm_threshold,
        max_hold_events=args.max_hold_events,
        flow_invalidation_threshold=args.flow_invalidation_threshold,
        flow_persistence_events=args.flow_persistence_events,
        trailing_atr_multiple=args.trailing_atr_multiple,
        trailing_activation_mfe_atr=args.trailing_activation_mfe_atr,
        decay_events=args.decay_events,
        fee_rate=args.fee_rate,
    )

    summary = summarize_trades(trades)
    exit_stats = build_exit_reason_stats(trades)

    Path(args.out_trades).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_summary).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_exit_stats).parent.mkdir(parents=True, exist_ok=True)
    trades.to_csv(args.out_trades, index=False)
    summary.to_csv(args.out_summary, index=False)
    exit_stats.to_csv(args.out_exit_stats, index=False)

    print("=== MVP Execution Backtest Summary ===")
    print(f"input_rows                 : {len(df)}")
    print(f"pressure_threshold_abs_q{int(args.pressure_quantile * 100)} : {pressure_threshold:.8f}")
    print(f"trades                     : {len(trades)}")
    if len(trades) > 0:
        print(f"win_rate                   : {summary.loc[0, 'win_rate']:.4f}")
        print(f"avg_pnl                    : {summary.loc[0, 'avg_pnl']:.8f}")
        print(f"largest_winner             : {summary.loc[0, 'largest_winner']:.8f}")
        print(f"avg_mae                    : {summary.loc[0, 'avg_mae']:.8f}")
        print(f"avg_mfe                    : {summary.loc[0, 'avg_mfe']:.8f}")
        print(f"avg_duration_events        : {summary.loc[0, 'avg_duration']:.2f}")
        print("exit_reason_distribution   :")
        for _, r in exit_stats.iterrows():
            print(f"  - {r['exit_reason']}: {int(r['count'])} ({r['pct']:.2%})")

    print("\nSaved outputs:")
    print(f"- {args.out_trades}")
    print(f"- {args.out_summary}")
    print(f"- {args.out_exit_stats}")


if __name__ == "__main__":
    main()
