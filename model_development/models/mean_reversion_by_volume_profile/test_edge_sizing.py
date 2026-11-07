import numpy as np
import pandas as pd

from observe_edge_sizing import (BASE_RISK, CAP, FEATURES, apply_component,
    effective_risk, fit_component, match_fixed_risk, predictions, uncertainty)


def test_scores_centered_bounded_and_missing_neutral():
    x = np.r_[np.arange(300, dtype=float), [np.nan]*20]
    y = np.r_[np.repeat([-1., 0., 1.], 100), np.ones(20)]
    fit = fit_component(x, y)
    scores = apply_component(x, fit)
    assert fit["status"] == "calibrated"
    np.testing.assert_allclose(scores.mean(), 0, atol=1e-12)
    assert np.max(np.abs(scores)) <= .5
    np.testing.assert_array_equal(scores[-20:], 0)
    assert scores[0] < scores[150] < scores[299]
    # Unknown test values use the locked tail bins, never extrapolated leverage.
    assert np.max(np.abs(apply_component([-1e9, 1e9, np.nan], fit))) <= .5


def test_small_or_constant_history_is_neutral():
    for x in [np.arange(89), np.ones(120)]:
        fit = fit_component(x, np.ones(len(x)))
        assert fit["status"] != "calibrated"
        np.testing.assert_array_equal(apply_component([0, 10, np.nan], fit), 0)


def test_training_risk_match_includes_notional_caps():
    distance = np.array([1e-5, .0003, .0008, .001, .004, .01])
    multipliers = np.array([1.5, 1.2, .8, .5, 1., 1.])
    risk = effective_risk(multipliers, distance)
    fixed = match_fixed_risk(distance, risk.mean())
    np.testing.assert_allclose(np.minimum(fixed, CAP*distance).mean(), risk.mean(), atol=1e-12)
    assert np.all(risk/distance <= CAP + 1e-12)
    assert fixed != BASE_RISK


def synthetic_population():
    dates = pd.date_range("2024-01-01", "2024-09-30")
    n = len(dates)
    g = pd.DataFrame(dict(symbol="BTCUSDC", profile="ny_0930_1300",
        session_date=dates.strftime("%Y-%m-%d"), gross_r=np.where(np.arange(n)%3, 1., -1.),
        status="time_exit", entry=100., risk_price=1., distance=.01, side=1., cost_r_per_bps=.02,
        exit_utc=pd.to_datetime(dates.strftime("%Y-%m-%d")+" 20:00", utc=True)))
    for i, col in enumerate(FEATURES.values()):
        g[col] = (np.arange(n)*(i+1)) % 41
    folds = pd.DataFrame([dict(symbol="BTCUSDC", profile="ny_0930_1300", fold=1,
        train_start="2024-01-01", test_start="2024-09-01", test_end_exclusive="2024-10-01",
        train_end_exclusive="2024-09-01", all_baseline_test_entries=30)])
    return g, folds


def test_test_outcomes_cannot_change_frozen_sizes(tmp_path):
    g, folds = synthetic_population()
    a = tmp_path/"a"
    b = tmp_path/"b"
    a.mkdir(); b.mkdir()
    first = predictions(g, folds, a)
    changed = g.copy()
    changed.loc[changed.session_date.ge("2024-09-01"), "gross_r"] *= -1
    second = predictions(changed, folds, b)
    np.testing.assert_array_equal(first.multiplier, second.multiplier)
    np.testing.assert_array_equal(first.matched_risk, second.matched_risk)
    assert len(first) == 30*9*2
    assert first.multiplier.gt(0).all()


def test_purge_boundary_outcome_and_missing_test_feature(tmp_path):
    g, folds = synthetic_population()
    # A train entry whose exit lies in test must not influence the fit.
    g.loc[0, "exit_utc"] = pd.Timestamp("2024-09-01 12:00", tz="UTC")
    g.loc[g.session_date.ge("2024-09-01"), FEATURES["adx_4h"]] = np.nan
    first = tmp_path/"first"
    second = tmp_path/"second"
    first.mkdir(); second.mkdir()
    a = predictions(g, folds, first)
    g.loc[0, "gross_r"] = 1e6
    b = predictions(g, folds, second)
    np.testing.assert_array_equal(a.multiplier, b.multiplier)
    np.testing.assert_array_equal(a.loc[a.model.eq("adx_4h"), "multiplier"], 1.)


def test_neutral_sizing_bootstrap_is_exactly_zero():
    g, folds = synthetic_population()
    q = g.loc[g.session_date.ge("2024-09-01")].copy()
    q["fold"] = 1
    q["multiplier"] = 1.
    q["sized_risk"] = q["matched_risk"] = BASE_RISK
    rows = uncertainty(q, folds, np.random.default_rng(42), 100)
    for row in rows:
        assert row["bootstrap_delta_per_opportunity_ci025"] == 0
        assert row["bootstrap_delta_per_opportunity_ci975"] == 0
        assert row["retrospective_equal_risk_delta_per_opportunity"] == 0
        assert row["random_weight_upper_tail_fraction"] == 1.
