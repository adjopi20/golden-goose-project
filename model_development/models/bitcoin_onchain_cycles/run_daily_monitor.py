"""LEGACY frozen-accumulation monitor, retained for reproducible research.
Current selected deployment: live_mvrv/compose.yaml (0/7 + four-year P5/P95).
This entrypoint does NOT implement either selected binary model.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
import math
from pathlib import Path
import sys
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from model_development.models.bitcoin_onchain_cycles.allocation import (
    ACCUMULATION_TIERS,
    NO_BUY_ABOVE,
)
from model_development.models.bitcoin_onchain_cycles.data_sources import (
    download_coinmetrics,
    latest_cdd_date,
    prepared_window,
)
from model_development.models.bitcoin_onchain_cycles.frozen_accumulation import (
    frozen_accumulation_state,
    frozen_parameters,
    FROZEN_BASE_SHARE,
    FROZEN_TIERS,
    FROZEN_NO_BUY_ABOVE,
)
from model_development.models.bitcoin_onchain_cycles.strategy import PAPER_START, prepare_daily_data


MODEL_DIR = Path(__file__).resolve().parent


def account_proposal(daily: pd.DataFrame, account: dict[str, Any]) -> dict[str, Any]:
    """Propose a purchase from an operator-confirmed snapshot; never assume fills."""
    if account.get("strategy") != "frozen_accumulation":
        raise ValueError("account strategy must be explicitly 'frozen_accumulation'")
    as_of = pd.Timestamp(account["as_of"])
    latest_date = pd.Timestamp(daily["date"].iloc[-1])
    if pd.isna(as_of) or as_of < daily["date"].iloc[0] or as_of > latest_date:
        raise ValueError("account as_of must lie within available indicator history")
    cash, btc, inflow = (float(account[key]) for key in
                        ("cash_usd", "btc_units", "unallocated_inflow_usd"))
    if not all(math.isfinite(v) and v >= 0 for v in (cash, btc, inflow)) or inflow > cash:
        raise ValueError("account balances must be nonnegative and finite; inflow is part of cash")
    tiers = account["tiers_spent_this_cycle"]
    if not isinstance(tiers, list) or any(type(n) is not int or n not in (1, 2, 3) for n in tiers):
        raise ValueError("tiers_spent_this_cycle must list tier numbers 1, 2, 3")
    spent = set(tiers)
    subsequent = daily.loc[daily["date"].gt(as_of), "mvrv_zscore"]
    if subsequent.ge(FROZEN_NO_BUY_ABOVE).any():
        spent.clear()
    z = float(daily["mvrv_zscore"].iloc[-1])
    budget = 0.0
    proposed_tiers = []
    if z >= FROZEN_NO_BUY_ABOVE:
        spent.clear()
    else:
        budget = inflow * FROZEN_BASE_SHARE
        for n, (threshold, fraction) in enumerate(FROZEN_TIERS, 1):
            if z < threshold and n not in spent:
                purchase = fraction * (cash - budget)
                if purchase > 0:
                    budget += purchase
                    proposed_tiers.append(n)
    return {
        "scope": "proposal against supplied account; no transactions assumed or executed",
        "signal": "BUY" if budget > 0 else "WAIT",
        "signal_data_date": latest_date.date().isoformat(),
        "account_as_of": as_of.date().isoformat(),
        "cash_usd": cash, "btc_units": btc,
        "proposed_buy_usd": budget,
        "proposed_tiers": proposed_tiers,
        "tiers_spent_before_proposal": sorted(spent),
        "note": "Record actual fills, balances, consumed inflow and tier state before the next run. Repeated proposals are not new orders.",
    }


def classify_accumulation_signal(
    zscore: float,
    previous_zscore: float,
    *,
    tiers: tuple[tuple[float, float], ...] = ACCUMULATION_TIERS,
    no_buy_above: float = NO_BUY_ABOVE,
) -> str:
    """Return today's accumulation action from the MVRV Z-score alone.

    The monitor never sells. It reports whether the always-on base tranche
    should buy, whether a fresh downward crossing unlocks dry powder, or
    whether buying pauses at euphoria.
    """
    if zscore >= no_buy_above:
        return "PAUSE"
    for tier_number, (threshold, _) in enumerate(tiers, start=1):
        if zscore < threshold <= previous_zscore:
            return f"TIER_{tier_number}"
    if tiers and zscore < tiers[0][0]:
        return "ZONE"
    return "DCA"


def describe_zone(
    zscore: float,
    *,
    tiers: tuple[tuple[float, float], ...] = ACCUMULATION_TIERS,
    no_buy_above: float = NO_BUY_ABOVE,
) -> str:
    """Return the deepest tier the Z-score currently sits inside."""
    if zscore >= no_buy_above:
        return "euphoria"
    zone = "neutral"
    for tier_number, (threshold, _) in enumerate(tiers, start=1):
        if zscore < threshold:
            zone = f"tier_{tier_number}"
    return zone


def describe_tier_plan(state: dict[str, Any]) -> str:
    """Say when the reserve can next be deployed, from the replayed state."""
    pending = state["pending_tier_detail"]
    if not pending:
        return (
            "nothing while the cycle is locked: the reserve can deploy only after "
            f"Z >= {float(state['rearm_requires_mvrv_zscore']):.2f} re-arms the cycle"
        )
    return ", ".join(
        f"{entry['tier']} fires below Z {entry['buy_below_mvrv_zscore']:.2f} "
        f"({entry['reserve_fraction']:.0%} of the reserve)"
        for entry in pending
    )


def build_signal_report(
    daily: pd.DataFrame,
    *,
    position: str,
    coinmetrics_through: pd.Timestamp,
    expected_start: str | pd.Timestamp | None = PAPER_START,
    cdd_through: pd.Timestamp | None = None,
    cdd_rows: int | None = None,
    cdd_label: str | None = None,
    cdd_refresh: str = "not_requested",
    cdd_used_in_signal: bool = False,
) -> dict[str, object]:
    """Report the frozen rule's state today, replayed from the full history.

    The frozen contract is a state machine: a tier may fire at most once per
    cycle, and the cycle re-arms only at or above Z ``+5.00``. Reading the last
    two Z-scores cannot see that state, so ``state`` here comes from a replay of
    the reference simulator. The crossing read is kept, but only as
    ``informative_crossing_read``, because it can name a tier the frozen rule is
    not allowed to act on.
    """
    if len(daily) < 2:
        raise RuntimeError("prepared data needs at least two days for a signal")

    latest = daily.iloc[-1]
    previous = daily.iloc[-2]
    zscore = float(latest["mvrv_zscore"])
    previous_zscore = float(previous["mvrv_zscore"])
    state = frozen_accumulation_state(daily, expected_start=expected_start)
    crossing_signal = classify_accumulation_signal(zscore, previous_zscore)

    return {
        "report": "frozen accumulation rule, replayed from history",
        "actionable": False,
        "account_scope": "hypothetical reference account; holdings and tier spending are not the operator's",
        "rule": frozen_parameters(),
        "as_of": {
            "data_date": pd.Timestamp(latest["date"]).date().isoformat(),
            "btc_price": float(latest["close"]),
            "mvrv_zscore": zscore,
            "previous_mvrv_zscore": previous_zscore,
            "nupl": float(latest["nupl"]),
        },
        "position": position,
        "state": state,
        "next_close": {
            "base_tranche": (
                "paused, the inflow waits in cash while Z >= 5.00"
                if not state["base_tranche_enabled"]
                else "buys 25% of each inflow on the first day of the month"
            ),
            "tiers": describe_tier_plan(state),
            "note": (
                "an unspent tier fires on any close with Z below its threshold; "
                "it does not need a fresh crossing, but it cannot fire while the "
                "cycle is locked"
            ),
        },
        "informative_crossing_read": {
            "note": (
                "crossing-based reading only, NOT the frozen rule; it ignores the "
                "cycle state, so a tier named here may be unavailable"
            ),
            "signal": crossing_signal,
            "zone": describe_zone(zscore),
            "tier_unlocked_by_a_fresh_crossing": crossing_signal.startswith("TIER_"),
            "zone_bands_used": {
                f"tier_{number}": threshold
                for number, (threshold, _) in enumerate(ACCUMULATION_TIERS, start=1)
            },
        },
        "coinmetrics_through": coinmetrics_through.date().isoformat(),
        "cdd_through": cdd_through.date().isoformat() if cdd_through is not None else None,
        "cdd_rows": cdd_rows,
        "cdd_latest_label": cdd_label,
        "cdd_refresh": cdd_refresh,
        "cdd_used_in_signal": cdd_used_in_signal,
    }


def _assert_frozen_replay() -> None:
    """Offline smoke test: a locked cycle must not report a tier as available."""
    synthetic = pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=6, freq="MS"),
            "close": [100.0] * 6,
            "mvrv_zscore": [1.0, 0.4, -0.1, -0.3, -0.3, 5.5],
        }
    )
    locked = frozen_accumulation_state(synthetic.iloc[:5], expected_start=None)
    assert locked["cycle"] == "locked", locked["cycle"]
    assert locked["reserve_deployable"] is False
    assert locked["tiers_spent_this_cycle"] == [1, 2, 3]
    assert locked["pending_tier_detail"] == []

    armed = frozen_accumulation_state(synthetic, expected_start=None)
    assert armed["cycle"] == "armed", armed["cycle"]
    assert armed["base_tranche_enabled"] is False
    assert armed["last_rearm"]["date"] == "2020-06-01"
    assert armed["replay_totals"]["sells"] == 0


def main() -> None:
    import warnings
    warnings.warn(
        "Legacy accumulation monitor, NOT the selected 0/7 + P5/P95 dual engine. "
        "Use python -m live_mvrv.main run; see live_mvrv/CONTRACT.md.",
        FutureWarning,
        stacklevel=2,
    )
    parser = argparse.ArgumentParser(
        description=(
            "Refresh BTC on-chain data and print today's accumulation-only signal. "
            "Coin Metrics is the only required source."
        )
    )
    parser.add_argument(
        "--position",
        choices=("cash", "btc"),
        default="cash",
        help="Informational only: the operator's current holding.",
    )
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--account-state", type=Path,
                        help="Operator-confirmed frozen-accumulation account JSON; otherwise reference report only")
    parser.add_argument(
        "--with-cdd",
        action="store_true",
        help=(
            "Also scrape Blockchair Coin Days Destroyed. Optional and fragile: "
            "it needs a real browser and is not required by the live signal."
        ),
    )
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Rebuild the report from the already prepared CSV instead of calling "
            "Coin Metrics. The data age check still applies."
        ),
    )
    parser.add_argument("--max-data-age-days", type=int, default=3)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=MODEL_DIR / "data",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        assert classify_accumulation_signal(5.0, 4.0) == "PAUSE"
        assert classify_accumulation_signal(1.9, 2.1) == "TIER_1"
        assert classify_accumulation_signal(0.4, 0.6) == "TIER_2"
        assert classify_accumulation_signal(-0.3, 0.1) == "TIER_3"
        assert classify_accumulation_signal(1.4, 1.2) == "ZONE"
        assert classify_accumulation_signal(3.0, 3.2) == "DCA"
        assert describe_zone(-0.3) == "tier_3"
        assert describe_zone(0.3) == "tier_2"
        assert describe_zone(1.0) == "tier_1"
        assert describe_zone(3.0) == "neutral"
        assert describe_zone(6.0) == "euphoria"
        _assert_frozen_replay()
        print("self-test passed")
        return

    source_dir = args.data_dir / "source"
    btc_path = source_dir / "btc.csv"
    cdd_path = source_dir / "data.tsv"
    prepared_path = args.data_dir / "prepared" / "daily.csv"
    signal_path = args.data_dir / "latest_signal.json"

    source_dir.mkdir(parents=True, exist_ok=True)

    blockchair_refresh = "not_requested"
    cdd_rows: int | None = None
    cdd_label: str | None = None
    cdd_end: pd.Timestamp | None = None
    if args.with_cdd:
        from model_development.models.bitcoin_onchain_cycles.download_blockchair_cdd import (
            download_tsv,
            validate_tsv,
        )

        blockchair_refresh = "downloaded"
        try:
            cdd_text = download_tsv(args.timeout, args.headless)
            cdd_rows, cdd_label = validate_tsv(cdd_text)
            temporary_cdd = cdd_path.with_suffix(".tsv.tmp")
            temporary_cdd.write_text(cdd_text, encoding="utf-8", newline="")
            temporary_cdd.replace(cdd_path)
        except (RuntimeError, TimeoutError, ValueError) as error:
            if not cdd_path.exists():
                raise
            blockchair_refresh = "cached"
            cdd_text = cdd_path.read_text(encoding="utf-8")
            cdd_rows, cdd_label = validate_tsv(cdd_text)
            print(f"warning: Blockchair refresh failed; using cached TSV: {error}")

    requested_end = pd.Timestamp(args.end)
    if args.offline:
        if args.with_cdd:
            raise RuntimeError("--offline cannot be combined with --with-cdd")
        if not prepared_path.exists():
            raise RuntimeError(f"--offline needs an existing prepared CSV: {prepared_path}")
        daily = prepared_window(pd.read_csv(prepared_path), PAPER_START, args.end)
        btc_end = pd.Timestamp(daily["date"].iloc[-1])
        effective_end = btc_end
        blockchair_refresh = "offline"
    else:
        btc_end = download_coinmetrics(btc_path, args.end)
        effective_end = min(requested_end, btc_end)
    if cdd_path.exists() and args.with_cdd:
        cdd_end = latest_cdd_date(cdd_path)
        effective_end = min(effective_end, cdd_end)
    age_days = int((requested_end - effective_end).days)
    if age_days > args.max_data_age_days:
        raise RuntimeError(
            f"Latest common data is {age_days} days old; refusing to emit a signal"
        )

    if not args.offline:
        daily = prepare_daily_data(
            btc_path,
            cdd_path if args.with_cdd else None,
            start=PAPER_START,
            end=effective_end.strftime("%Y-%m-%d"),
        )
    if len(daily) < 2:
        raise RuntimeError("prepared data needs at least two days for a signal")
    if not args.offline:
        prepared_path.parent.mkdir(parents=True, exist_ok=True)
        daily.to_csv(prepared_path, index=False)

    result = build_signal_report(
        daily,
        position=args.position,
        coinmetrics_through=btc_end,
        cdd_through=cdd_end,
        cdd_rows=cdd_rows,
        cdd_label=cdd_label,
        cdd_refresh=blockchair_refresh,
        cdd_used_in_signal=False,
    )
    if args.account_state is not None:
        account = json.loads(args.account_state.read_text(encoding="utf-8"))
        result["account_proposal"] = account_proposal(daily, account)
        result["account_scope"] = "reference replay plus separately labelled operator-account proposal"
    temporary_signal = signal_path.with_suffix(".json.tmp")
    temporary_signal.write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_signal.replace(signal_path)
    print(json.dumps(result, indent=2))
    print(f"saved: {signal_path}")


if __name__ == "__main__":
    main()
