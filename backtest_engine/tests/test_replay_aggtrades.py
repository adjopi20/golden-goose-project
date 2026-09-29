import json
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest_engine import replay_aggtrades as engine
from backtest_engine.replay_aggtrades import Config, Portfolio, on_tick, parameter_grid


def _signal(direction, stop):
    return {
        "sample_id": direction,
        "session_day": "2026-08-01",
        "direction": direction,
        "stop": stop,
        "entry_reference": 100.0,
        "entry_eligible_timestamp_ms": 1000,
        "entry_deadline_timestamp_ms": 3000,
        "force_exit_timestamp_ms": 5000,
    }


def _tick(timestamp, trade_id, price):
    return {"timestamp_ms": timestamp, "agg_trade_id": trade_id, "price": price}


def test_next_print_stop_and_separate_time_exit():
    config = Config(initial_equity=1000, risk_fraction=0.005, fee_bps=4)
    long = Portfolio("profile_a", [_signal("long", 95.0)], equity=1000)
    short = Portfolio("profile_b", [_signal("short", 105.0)], equity=1000)
    for portfolio in (long, short):
        on_tick(portfolio, _tick(1000, 1, 100.0), config)
    assert len(long.orders) == len(short.orders) == 1
    on_tick(long, _tick(2000, 2, 94.0), config)
    assert not long.trades
    on_tick(long, _tick(2001, 3, 93.0), config)
    assert long.trades[0]["exit_reason"] == "initial_stop_next_print"
    assert long.trades[0]["exit_agg_trade_id"] == 3
    on_tick(short, _tick(5000, 4, 90.0), config)
    assert short.trades[0]["exit_reason"] == "time_exit_next_print"
    assert short.trades[0]["pnl"] > 0
    assert long.trades[0]["pnl"] < 0


@pytest.mark.parametrize("direction,stop,entry,target,fill", [
    ("long", 95, 101, 110, 109.5),
    ("short", 105, 99, 90, 90.5),
])
def test_target_actual_fill_next_print_and_fees(direction, stop, entry, target, fill):
    config = Config()
    portfolio = Portfolio("test", [_signal(direction, stop)], 1000, target_r=1.5)
    on_tick(portfolio, _tick(1000, 1, entry), config)
    assert portfolio.position["target_price"] == target
    on_tick(portfolio, _tick(2000, 2, target), config)
    assert not portfolio.trades
    on_tick(portfolio, _tick(2000, 3, fill), config)
    trade = portfolio.trades[0]
    assert trade["exit_reason"] == "fixed_target_next_print"
    assert trade["exit_agg_trade_id"] == 3
    assert trade["target_trigger_timestamp_ms"] == 2000
    assert trade["r"] < 1.5
    assert trade["fees"] == pytest.approx((entry + fill) * trade["quantity"] * .0004)


def test_stop_wins_before_future_target_and_target_cannot_override_cutoff():
    config = Config()
    portfolio = Portfolio("test", [_signal("long", 95)], 1000, target_r=1)
    on_tick(portfolio, _tick(1000, 1, 100), config)
    on_tick(portfolio, _tick(2000, 2, 95), config)
    on_tick(portfolio, _tick(3000, 3, 110), config)
    assert portfolio.trades[0]["exit_reason"] == "initial_stop_next_print"
    portfolio = Portfolio("test", [_signal("long", 95)], 1000, target_r=1)
    on_tick(portfolio, _tick(1000, 1, 100), config)
    on_tick(portfolio, _tick(5000, 2, 110), config)
    assert portfolio.trades[0]["exit_reason"] == "time_exit_next_print"


def test_grid_cartesian_and_validation():
    variants = parameter_grid(Config(), {"target_r": [1, 1.5, 2], "risk_fraction": [.005, .01]})
    assert len(variants) == 6
    assert len({row[0] for row in variants}) == 6
    assert {row[1].risk_fraction for row in variants} == {.005, .01}
    for invalid in ({"unknown": [1]}, {"target_r": []}, {"target_r": [0]},
                    {"target_r": [float("nan")]}, {"target_r": [1, 1.0]},
                    {"fee_bps": [-1]}, {"max_leverage": [float("inf")]}):
        with pytest.raises(ValueError):
            parameter_grid(Config(), invalid)
    assert parameter_grid(Config(), {"target_r": [None]})[0][2] is None


def test_sweep_one_scan_independent_equity_and_time_control(tmp_path, monkeypatch):
    raw = tmp_path / "ticks.parquet"
    pq.write_table(pa.table({"timestamp": [0, 1000, 2000, 2001, 3000, 3001, 5000],
                             "agg_trade_id": list(range(7)),
                             "price": [100., 100., 105., 105., 110., 110., 101.]}), raw)
    signals = tmp_path / "signals.jsonl"
    signals.write_text(json.dumps(_signal("long", 95)) + "\n", encoding="utf-8")
    scans = []
    original = engine.raw_ticks
    def counted(*args):
        scans.append(1)
        yield from original(*args)
    monkeypatch.setattr(engine, "raw_ticks", counted)
    cfg = Config(fee_bps=0)
    variants = parameter_grid(cfg, {"target_r": [1, 1.5, 2, None]})
    results = engine.run(raw, {"baseline": signals, "challenger": signals}, tmp_path / "sweep",
                         cfg, date(2026, 8, 1), date(2026, 8, 1), variants=variants)
    assert len(scans) == 1
    assert len(results) == 8
    assert results["baseline__fixed_1r_full"]["metrics"]["final_equity"] == pytest.approx(1005)
    assert results["baseline__fixed_2r_full"]["metrics"]["final_equity"] == pytest.approx(1010)
    assert results["challenger__fixed_2r_full"]["metrics"]["final_equity"] == pytest.approx(1010)
    control = engine.run(raw, {"baseline": signals}, tmp_path / "old", cfg,
                         date(2026, 8, 1), date(2026, 8, 1))
    assert control["baseline"]["metrics"] == results["baseline__time_exit"]["metrics"]
    assert (tmp_path / "sweep" / "comparison.csv").is_file()
    assert (tmp_path / "sweep" / "complete.json").is_file()


def test_unfinished_next_print_fill_fails():
    portfolio = Portfolio("test", [_signal("long", 95)], 1000)
    with pytest.raises(ValueError, match="open position"):
        engine.replay([portfolio], iter([_tick(1000, 1, 100), _tick(2000, 2, 95)]), Config())
