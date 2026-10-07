"""Pure next-public-print paper execution; shared by runtime and backtest.

Extracted without changing fill, fee, partial or closed-equity sizing rules.
Not an exchange API; no BBO, impact, liquidation or funding model.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
import math
from typing import Any


@dataclass(frozen=True)
class Config:
    initial_equity: float = 1000.0
    risk_fraction: float = 0.005
    fee_bps: float = 4.0
    max_leverage: float = 5.0
    max_entry_delay_ms: int = 300_000
    tp1_r: float | None = None
    tp1_fraction: float | None = None


@dataclass
class Portfolio:
    label: str
    signals: list[dict[str, Any]]
    equity: float
    next_signal: int = 0
    position: dict[str, Any] | None = None
    stop_trigger: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    target_r: float | None = None
    target_trigger: dict[str, Any] | None = None
    config: Config | None = None
    source_label: str | None = None
    partial_trigger: dict[str, Any] | None = None

    def done(self) -> bool:
        return self.next_signal == len(self.signals) and self.position is None



def close_position(portfolio: Portfolio, tick: dict, reason: str, config: Config) -> None:
    position = portfolio.position
    assert position is not None
    side = 1 if position["direction"] == "long" else -1
    exit_price = tick["price"]
    remaining = position.get("remaining_quantity", position["quantity"])
    final_fee = exit_price * remaining * config.fee_bps / 10_000
    gross = (side * (exit_price - position["entry_price"]) * remaining
             + position.get("partial_gross", 0.0))
    exit_fee = final_fee + position.get("partial_fee", 0.0)
    fees = position["entry_fee"] + exit_fee
    net = gross - fees
    equity_before_exit = portfolio.equity
    portfolio.equity += net
    trade = dict(sample_id=position["sample_id"], session_day=position["session_day"],
                 signal_group=portfolio.label, strategy=position["strategy"],
                 route=position["route"],
                 direction=position["direction"], entry_timestamp_ms=position["entry_timestamp_ms"],
                 entry_agg_trade_id=position["entry_agg_trade_id"], entry_price=position["entry_price"],
                 entry_reference=position["entry_reference"],
                 entry_reference_deviation_bps=position["entry_reference_deviation_bps"],
                 exit_timestamp_ms=tick["timestamp_ms"], exit_agg_trade_id=tick["agg_trade_id"],
                 exit_price=exit_price, exit_reason=reason, stop=position["stop"],
                 stop_trigger_timestamp_ms=(portfolio.stop_trigger or {}).get("timestamp_ms"),
                 stop_trigger_price=(portfolio.stop_trigger or {}).get("price"),
                 target_r=portfolio.target_r, target_price=position.get("target_price"),
                 target_trigger_timestamp_ms=(portfolio.target_trigger or {}).get("timestamp_ms"),
                 target_trigger_price=(portfolio.target_trigger or {}).get("price"),
                 quantity=position["quantity"], leverage_at_entry=position["leverage_at_entry"],
                 stop_risk_pct=position["stop_risk_pct"], planned_risk_fraction=config.risk_fraction,
                 actual_initial_risk=position["actual_initial_risk"],
                 equity_before_entry=position["equity_before_entry"],
                 equity_before_exit=equity_before_exit, equity_after_exit=portfolio.equity,
                 gross_pnl_before_costs=gross, entry_fee=position["entry_fee"], exit_fee=exit_fee,
                 fees=fees, pnl=net, r=net / position["actual_initial_risk"])
    if config.tp1_r is not None:
        trade.update(tp1_r=config.tp1_r, tp1_fraction=config.tp1_fraction,
                     tp1_filled=position.get("tp1_filled", False),
                     tp1_price=position.get("tp1_fill_price"),
                     tp1_timestamp_ms=position.get("tp1_timestamp_ms"),
                     remaining_exit_quantity=remaining,
                     partial_gross=position.get("partial_gross", 0.0),
                     partial_exit_fee=position.get("partial_fee", 0.0),
                     final_exit_fee=final_fee)
    portfolio.trades.append(trade)
    portfolio.orders.append(dict(sample_id=position["sample_id"], action="EXIT", reason=reason,
                                 timestamp_ms=tick["timestamp_ms"], agg_trade_id=tick["agg_trade_id"],
                                 price=exit_price, quantity=remaining, fee=final_fee))
    portfolio.equity_curve.append(dict(timestamp_ms=tick["timestamp_ms"],
                                       session_day=position["session_day"], equity=portfolio.equity,
                                       drawdown_from_peak=None))
    portfolio.position = None
    portfolio.stop_trigger = None
    portfolio.target_trigger = None
    portfolio.partial_trigger = None


def take_partial(portfolio: Portfolio, tick: dict, config: Config) -> None:
    """One TP1 fill; original stop remains active on the remaining quantity."""
    p = portfolio.position
    assert p is not None and config.tp1_fraction is not None
    qty = p["quantity"] * config.tp1_fraction
    side = 1 if p["direction"] == "long" else -1
    p.update(remaining_quantity=p["quantity"]-qty, tp1_filled=True,
             tp1_fill_price=tick["price"], tp1_timestamp_ms=tick["timestamp_ms"],
             partial_gross=side*(tick["price"]-p["entry_price"])*qty,
             partial_fee=tick["price"]*qty*config.fee_bps/10_000)
    portfolio.orders.append(dict(sample_id=p["sample_id"], action="PARTIAL_EXIT",
        reason="tp1_next_print", timestamp_ms=tick["timestamp_ms"],
        agg_trade_id=tick["agg_trade_id"], price=tick["price"], quantity=qty,
        fee=p["partial_fee"], trigger_timestamp_ms=portfolio.partial_trigger["timestamp_ms"]))
    portfolio.partial_trigger = None


def on_tick(portfolio: Portfolio, tick: dict, config: Config) -> None:
    ts, price = tick["timestamp_ms"], tick["price"]
    position = portfolio.position
    if position is not None:
        if portfolio.stop_trigger is not None:
            close_position(portfolio, tick, "initial_stop_next_print", config)
            return
        if portfolio.target_trigger is not None:
            close_position(portfolio, tick, "fixed_target_next_print", config)
            return
        if portfolio.partial_trigger is not None:
            take_partial(portfolio, tick, config)
            # This print can also cross the hard stop or reach the time exit.
        if ts >= position["force_exit_timestamp_ms"]:
            close_position(portfolio, tick, "time_exit_next_print", config)
            return
        side = 1 if position["direction"] == "long" else -1
        if side * (price - position["stop"]) <= 0:
            portfolio.stop_trigger = dict(timestamp_ms=ts, agg_trade_id=tick["agg_trade_id"],
                                          price=price)
            return
        target = position.get("target_price")
        if target is not None and side * (price - target) >= 0:
            portfolio.target_trigger = dict(timestamp_ms=ts, agg_trade_id=tick["agg_trade_id"],
                                             price=price)
        partial_target = position.get("tp1_target_price")
        if (partial_target is not None and not position.get("tp1_filled", False)
                and side * (price - partial_target) >= 0):
            portfolio.partial_trigger = dict(timestamp_ms=ts, price=price)
        return
    while portfolio.next_signal < len(portfolio.signals):
        signal = portfolio.signals[portfolio.next_signal]
        eligible = int(signal["entry_eligible_timestamp_ms"])
        if ts < eligible:
            return
        portfolio.next_signal += 1
        if ts > int(signal["entry_deadline_timestamp_ms"]):
            portfolio.decisions.append(dict(sample_id=signal["sample_id"], decision="SKIP",
                                            reason="entry_deadline_passed"))
            continue
        if ts - eligible > config.max_entry_delay_ms:
            portfolio.decisions.append(dict(sample_id=signal["sample_id"], decision="SKIP",
                                            reason="first_trade_arrived_after_max_delay",
                                            delay_ms=ts-eligible))
            continue
        stop = float(signal["stop"])
        price = float(tick["price"])
        side = 1 if signal["direction"] == "long" else -1
        if side * (price - stop) <= 0:
            portfolio.decisions.append(dict(sample_id=signal["sample_id"], decision="SKIP",
                                            reason="stop_invalid_at_actual_fill"))
            continue
        risk_per_unit = abs(price-stop)
        target_price = (price + side * portfolio.target_r * risk_per_unit
                        if portfolio.target_r is not None else None)
        # An unreachable (nonpositive) short target remains unreachable; do not
        # silently discard the entry. The initial stop/time exit still applies.
        quantity = min(portfolio.equity*config.risk_fraction/risk_per_unit,
                       portfolio.equity*config.max_leverage/price)
        if quantity <= 0 or not math.isfinite(quantity):
            raise ValueError("Invalid position quantity")
        fee = quantity*price*config.fee_bps/10_000
        portfolio.position = dict(
            sample_id=signal["sample_id"], session_day=signal["session_day"],
            strategy=signal.get("strategy"), route=signal.get("route"),
            direction=signal["direction"], stop=stop,
            target_price=target_price,
            tp1_target_price=(price + side * config.tp1_r * risk_per_unit
                              if config.tp1_r is not None else None),
            entry_timestamp_ms=ts, entry_agg_trade_id=tick["agg_trade_id"],
            entry_price=price, entry_reference=float(signal["entry_reference"]),
            entry_reference_deviation_bps=side*(price-float(signal["entry_reference"]))
                                          /float(signal["entry_reference"])*10_000,
            quantity=quantity, entry_fee=fee, equity_before_entry=portfolio.equity,
            leverage_at_entry=quantity*price/portfolio.equity,
            stop_risk_pct=risk_per_unit/price, actual_initial_risk=quantity*risk_per_unit,
            force_exit_timestamp_ms=int(signal["force_exit_timestamp_ms"]))
        portfolio.orders.append(dict(sample_id=signal["sample_id"], action="ENTRY",
                                     timestamp_ms=ts, agg_trade_id=tick["agg_trade_id"],
                                     price=price, quantity=quantity, fee=fee))
        portfolio.decisions.append(dict(sample_id=signal["sample_id"], decision="TAKE",
                                        entry_timestamp_ms=ts, delay_ms=ts-eligible))
        return



def validate_config(config: Config) -> None:
    if (not all(math.isfinite(value) for value in asdict(config).values() if value is not None) or
            config.initial_equity <= 0 or not 0 < config.risk_fraction < 1 or
            not 0 <= config.fee_bps < 10000 or config.max_leverage <= 0 or
            config.max_entry_delay_ms < 0):
        raise ValueError("Invalid backtest configuration")
    if (config.tp1_r is None) != (config.tp1_fraction is None):
        raise ValueError("tp1_r and tp1_fraction must be supplied together")
    if config.tp1_r is not None and (config.tp1_r <= 0 or not 0 < config.tp1_fraction < 1):
        raise ValueError("TP1 needs positive R and fraction strictly between 0 and 1")
