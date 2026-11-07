"""Audit helper: compare replayed reports against the recorded artifacts.

Usage, from the repository root:

    python model_development/models/bitcoin_onchain_cycles/run_accumulation_experiment.py --output model_development/models/bitcoin_onchain_cycles/research/audit_2026-09-26/_replay/accumulation
    python model_development/models/bitcoin_onchain_cycles/run_cost_basis_experiment.py --output model_development/models/bitcoin_onchain_cycles/research/audit_2026-09-26/_replay/cost_basis
    python model_development/models/bitcoin_onchain_cycles/run_ladder_experiment.py --output model_development/models/bitcoin_onchain_cycles/research/audit_2026-09-26/_replay/ladder
    python model_development/models/bitcoin_onchain_cycles/research/audit_2026-09-26/reproducibility_check.py

The replay directory is scratch space, not a research artifact; delete it
afterwards. Frozen artifacts are only read, never written.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

BASE = Path("model_development/models/bitcoin_onchain_cycles/research")
REPLAY = BASE / "audit_2026-09-26/_replay"

PAIRS = {
    "accumulation": (
        BASE / "portfolio_allocation/accumulation_results/performance_report.csv",
        REPLAY / "accumulation/performance_report.csv",
    ),
    "cost_basis": (
        BASE / "portfolio_allocation/cost_basis_results/cost_basis_report.csv",
        REPLAY / "cost_basis/cost_basis_report.csv",
    ),
    "ladder": (
        BASE / "portfolio_allocation/ladder_results/performance_report.csv",
        REPLAY / "ladder/performance_report.csv",
    ),
}


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype(float)


# Reporting changes made on 2026-09-26. Values must still match; only the name
# or the day-count convention moved. Anything else counts as drift.
COLUMN_ALIASES = {"time_underwater_days": "longest_drawdown_streak_days"}
# Renamed *and* sign-flipped on purpose: the old column was "variant minus DCA",
# where negative meant better, while the accumulation round reports positive as
# an improvement (audit finding G10).
SIGN_FLIPPED_ALIASES = {
    "drawdown_vs_dca": "drawdown_improvement_vs_dca",
    "drawdown_vs_frozen": "drawdown_improvement_vs_frozen",
}
EXPECTED_CHANGES = {
    "money_weighted_return_xirr": (
        "XIRR day count changed from 365 to 365.25 to match CAGR (audit finding B3)"
    ),
    "tier_purchases": (
        "counted signal labels before 2026-09-26; now counts every tier that fired "
        "(audit finding G2)"
    ),
    "base_purchases": (
        "counted signal labels before 2026-09-26; now counts inflow days on which "
        "the base tranche could buy (audit finding G2)"
    ),
    "equity_drawdown_rank": (
        "ranked raw values before 2026-09-26, which split a one-ULP tie into ranks "
        "7 and 8; now rounded first (audit finding G3)"
    ),
    "rank_change": "follows the corrected equity_drawdown_rank (audit finding G3)",
    "pareto_optimal": (
        "the frontier now covers never-sell variants only, so the selling reference "
        "row is excluded (audit finding G9)"
    ),
    "beats_dca_wealth": (
        "the benchmark can no longer beat itself, and a tie is no longer a win "
        "(audit finding G11)"
    ),
    "beats_dca_drawdown": "same self-reference fix as beats_dca_wealth (finding G11)",
    "dominates_dca": "follows the corrected beats_dca flags (audit finding G11)",
}


def main() -> int:
    failed = False
    notes: list[str] = []
    for name, (recorded_path, replay_path) in PAIRS.items():
        if not replay_path.exists():
            print(f"--- {name}: replay missing at {replay_path}")
            failed = True
            continue
        recorded = pd.read_csv(recorded_path).sort_values("strategy").reset_index(drop=True)
        replay = pd.read_csv(replay_path).sort_values("strategy").reset_index(drop=True)
        rows_match = list(recorded["strategy"]) == list(replay["strategy"])
        if not rows_match:
            failed = True
        extra = [c for c in replay.columns if c not in recorded.columns]
        diffs = []
        for column in recorded.columns:
            target = COLUMN_ALIASES.get(
                column, SIGN_FLIPPED_ALIASES.get(column, column)
            )
            if target not in replay.columns:
                diffs.append((column, "missing in replay"))
                continue
            left, right = numeric(recorded[column]), numeric(replay[target])
            if left.isna().all():
                continue
            if column in SIGN_FLIPPED_ALIASES:
                # Renamed and sign-flipped: the magnitudes must still agree.
                if (left + right).abs().max() > 1e-12:
                    diffs.append((column, "renamed and sign-flipped but magnitudes differ"))
                continue
            delta = (left - right).abs().max()
            if pd.isna(delta) or delta == 0:
                continue
            if column in COLUMN_ALIASES:
                diffs.append((column, "renamed but values changed"))
            elif column in EXPECTED_CHANGES:
                notes.append(f"{name}.{column}: max|delta|={delta:.6g} — {EXPECTED_CHANGES[column]}")
            else:
                diffs.append((column, f"max|delta|={delta:.6g}"))
        print(f"--- {name}: rows {len(recorded)}/{len(replay)} strategies_match={rows_match}")
        print(f"    new columns in replay: {extra or 'none'}")
        if diffs:
            failed = True
            for column, note in diffs:
                print(f"    DIFF {column}: {note}")
        else:
            print("    numeric columns: IDENTICAL (after documented renames)")
    print()
    if notes:
        print("Documented reporting changes:")
        for note in notes:
            print(f"  - {note}")
        print()
    print(
        "RESULT:",
        "REPRODUCIBILITY GAPS FOUND" if failed else "ALL RECORDED NUMBERS REPRODUCE",
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
