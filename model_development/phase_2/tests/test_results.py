from __future__ import annotations

from datetime import date

import pytest

from model_development.phase_2.backtest import (
    calculate_shared_backtest_metrics,
    write_shared_backtest_result,
)


def _trades() -> list[dict[str, object]]:
    return [
        {
            "entry_time": "2026-01-02T14:30:00+00:00",
            "exit_time": "2026-01-02T15:30:00+00:00",
            "pnl": 20.0,
            "gross_pnl_before_costs": 22.0,
            "fees": 1.0,
            "slippage": 1.0,
            "r": 2.0,
        },
        {
            "entry_time": "2026-01-03T14:30:00+00:00",
            "exit_time": "2026-01-03T15:00:00+00:00",
            "pnl": -10.0,
            "gross_pnl_before_costs": -8.0,
            "fees": 1.0,
            "slippage": 1.0,
            "r": -1.0,
        },
    ]


def test_calculate_shared_backtest_metrics() -> None:
    metrics = calculate_shared_backtest_metrics(
        _trades(),
        initial_equity=1_000.0,
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 3),
    )

    assert metrics["trades"] == 2
    assert metrics["win_rate"] == pytest.approx(0.5)
    assert metrics["expectancy_r"] == pytest.approx(0.5)
    assert metrics["profit_factor_r"] == pytest.approx(2.0)
    assert metrics["final_equity"] == pytest.approx(1_010.0)
    assert metrics["total_fees"] == pytest.approx(2.0)
    assert metrics["total_slippage"] == pytest.approx(2.0)


def test_write_shared_backtest_result(tmp_path) -> None:
    result = write_shared_backtest_result(
        tmp_path,
        _trades(),
        initial_equity=1_000.0,
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 3),
        summary={"strategy": "test"},
        orders=[{"event": "fill"}],
        decisions=[{"decision": "TAKE"}],
    )

    assert result["strategy"] == "test"
    assert result["metrics"]["final_equity"] == pytest.approx(1_010.0)
    assert (tmp_path / "trades.jsonl").exists()
    assert (tmp_path / "paper_orders.jsonl").exists()
    assert (tmp_path / "decisions.jsonl").exists()
    assert (tmp_path / "summary.json").exists()


def test_prepared_direction_portfolios_and_empty_periods():
    from model_development.lifecycle import PreparedModel
    from model_development.phase_2.lifecycle_output import directional_expectancy

    signal = dict(sample_id='same', entry_eligible_timestamp_ms=10, decision_timestamp_ms=10)
    model = PreparedModel({'baseline_both': [signal], 'c1_both': [signal], 'empty': []})
    model.evaluate_at(10, 10)
    assert set(model.pending) == {'baseline_both', 'c1_both'}
    assert model.pending['baseline_both'] is not model.pending['c1_both']
    assert set(model.submodels) == {'baseline_both', 'c1_both', 'empty'}
    with pytest.raises(ValueError, match='Duplicate'):
        PreparedModel({'baseline_both': [signal, signal]})

    result = directional_expectancy([
        dict(session_day='2026-01-31', direction='long', r=1.5),
        dict(session_day='2026-04-01', direction='short', r=-1.0),
    ], date(2026, 1, 15), date(2026, 7, 2))
    assert result['overall']['short'] == dict(trades=1, expectancy_net_R=-1.0)
    first, second, last = result['three_month_blocks']
    assert first['start'] == '2026-01-15' and first['end'] == '2026-03-31'
    assert first['short'] == dict(trades=0, expectancy_net_R=None)
    assert second['short']['trades'] == 1
    assert last['end'] == '2026-07-02' and last['long']['trades'] == 0


def test_loss_reward_summary_keeps_fee_flipped_losses_and_tail_payoff():
    from model_development.phase_2.lifecycle_output import loss_reward_summary
    rows=[dict(pnl=59.,r=5.9,fees=1.,actual_initial_risk=10.,equity_before_entry=1000.,entry_price=100.,stop=90.),
          dict(pnl=-1.,r=-.1,fees=2.,actual_initial_risk=10.,equity_before_entry=1059.,entry_price=100.,stop=90.),
          dict(pnl=-11.,r=-1.1,fees=1.,actual_initial_risk=10.,equity_before_entry=1058.,entry_price=100.,stop=90.)]
    result=loss_reward_summary(rows)
    assert result['net_winning_trades']==1 and result['net_losing_trades']==2
    assert result['total_net_wins']==59 and result['total_net_loss_magnitude']==12
    assert result['average_net_win_R']==pytest.approx(5.9)
    assert result['average_net_loss_magnitude_R']==pytest.approx(.6)
    assert result['average_fee_R']==pytest.approx(.4/3)
    assert result['tail_trades']==1 and result['tail_net_pnl']==59
    empty=loss_reward_summary([])
    assert empty['average_net_loss_magnitude_R'] is None and empty['average_net_win_R'] is None
    assert empty['tail_trades']==0 and empty['total_net_loss_magnitude']==0
