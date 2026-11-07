"""Independent output/identity checks; no indicator calibration or selection changes."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--script", type=Path, required=True)
    args = p.parse_args()
    cfg = json.loads((args.run / "config_snapshot.json").read_text())
    assert cfg["status"] == "complete"
    assert cfg["code_sha256"] == hashlib.sha256(args.script.read_bytes()).hexdigest()
    features = pd.read_csv(args.run / "daily_features.csv")
    ledger = pd.read_csv(args.run / "context_ledger.csv")
    predictions = pd.read_csv(args.run / "test_predictions.csv")
    selected_folds = pd.read_csv(args.run / "selected_folds.csv")
    aggregate = pd.read_csv(args.run / "aggregate.csv")
    key = ["symbol", "profile", "session_date"]
    assert not ledger.duplicated(key).any()
    assert not features.duplicated(["symbol", "session_date"]).any()
    assert not predictions.duplicated(key + ["feature"]).any()
    original = pd.read_csv(args.baseline / "trades.csv")
    checked = ledger.merge(original[key + ["gross_r", "entry", "status"]], on=key, suffixes=("", "_source"), validate="one_to_one")
    np.testing.assert_allclose(checked.gross_r, checked.gross_r_source, equal_nan=True)
    np.testing.assert_allclose(checked.entry, checked.entry_source, equal_nan=True)
    assert checked.status.eq(checked.status_source).all()
    decision = pd.to_datetime(features.feature_asof_utc, utc=True)
    assert decision.dt.tz_convert("America/New_York").dt.hour.eq(13).all()
    for tf, hours in [("4h", 4), ("1d", 24)]:
        ready = features[f"macro_{tf}_available"].fillna(False)
        stamp = pd.to_datetime(features.loc[ready, f"macro_{tf}_asof_utc"], utc=True)
        assert (stamp <= decision[ready]).all()
        assert (decision[ready] - stamp < pd.Timedelta(hours=hours)).all()
        assert stamp.dt.minute.eq(0).all()
        assert stamp.dt.hour.mod(hours).eq(0).all()
    for r in aggregate.itertuples():
        g = predictions.loc[predictions.symbol.eq(r.symbol) & predictions.profile.eq(r.profile) & predictions.feature.eq(r.feature)]
        kept = g.loc[g.selected]
        assert len(g) == r.matched_baseline_entries
        assert len(kept) == r.selected_entries
        np.testing.assert_allclose(kept.gross_r.sum(), r.total_r, atol=1e-10)
        if len(kept):
            np.testing.assert_allclose(kept.gross_r.mean(), r.mean_r, atol=1e-10)
            np.testing.assert_allclose(100 * kept.gross_r.gt(1e-12).mean(), r.win_rate_pct, atol=1e-10)
        np.testing.assert_allclose((kept.gross_r.sum() - g.gross_r.sum()) / len(g), r.delta_r_per_baseline_opportunity, atol=1e-10)
    result = dict(status="passed", feature_dates=len(features), baseline_profile_dates=len(ledger),
                  prediction_rows=len(predictions), verified_aggregate_rows=len(aggregate),
                  selected_fold_rows=len(selected_folds), code_sha256=cfg["code_sha256"],
                  checks=["baseline entry/outcome identity", "unique profile/date keys", "nonoverlapping OOS predictions",
                          "macro availability before 13:00", "UTC macro bar boundaries", "WR/R/retention arithmetic"])
    (args.run / "audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
