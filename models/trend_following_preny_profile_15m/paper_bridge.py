"""Causal prepared-data bridge from the frozen model to the shared paper ledger.

The venue feed/preparation service must supply a finalized profile, completed
bars, prior-session references, and completed indicator snapshots. No market
connection, feature-fitting, raw tick archive, or real order submission here.
"""
from datetime import date
import math

from .effort_result import annotate_session
from .evaluate_plugs import _gate
from .prepare_selected_entries import keep
from .strategy import BAR_MS, clock_ms, evaluate_session, validate_bar


POLICIES = {
    "ETHUSDC": "c2_union_i_a_p",
    "BNBUSDC": "c2_initiative",
    "HYPEUSDT": "c1_delta",
}
ACCOUNT_IDS = {"ETHUSDC": "tf-preny-eth-v1",
               "BNBUSDC": "tf-preny-bnb-v1",
               "HYPEUSDT": "tf-preny-hype-v1"}


class NotReady(ValueError):
    """Missing/stale preparation: wait for repair; do not invent a signal."""


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _risk(symbol, trend, direction, asof):
    if symbol == "HYPEUSDT":
        return .005
    if trend is None or trend.get("status") != "ready" or trend.get("end_ms") != asof:
        raise NotReady("Completed 15m trend snapshot is unavailable")
    if symbol == "ETHUSDC":
        gap = trend.get("price_vs_ema200_pct")
        if not _finite(gap):
            raise NotReady("EMA200 directional gap missing")
        return .005 if (1 if direction == "long" else -1) * gap >= .5 else .0025
    adx = trend.get("adx14")
    if not _finite(adx) or not 0 <= adx <= 100:
        raise NotReady("Completed ADX14 missing/invalid")
    return .005 if adx >= 30 else .0025


def hype_candidate_features(bars, direction, atr_before, signal_atr=None):
    recent = bars[-4:]
    volume = sum(float(b["buy_volume"]) + float(b["sell_volume"]) for b in recent)
    # The research C1 feature uses the last completed 1h ATR at signal time.
    atr = (signal_atr["atr14"] if signal_atr is not None else
           atr_before.get(int(recent[-1]["open_timestamp_ms"])))
    if not volume > 0 or not _finite(atr) or atr <= 0:
        raise NotReady("HYPE C1 needs completed-hour ATR and positive volume")
    sign = 1 if direction == "long" else -1
    return {
        "directional_delta_imbalance": sign * sum(float(b["buy_volume"]) - float(b["sell_volume"]) for b in recent) / volume,
        "directional_result_atr": sign * (float(recent[-1]["close"]) - float(recent[0]["open"])) / atr,
    }


def _hype_gate(bars, direction, atr_before, calibration, day, signal_atr=None):
    if calibration is None:
        raise NotReady("HYPE C1 calibration missing")
    try:
        first, last = date.fromisoformat(calibration["valid_from"]), date.fromisoformat(calibration["valid_to_exclusive"])
        trained_through = date.fromisoformat(calibration["trained_through"])
        thresholds = calibration["thresholds"]
    except (KeyError, ValueError, TypeError) as exc:
        raise NotReady("Invalid C1 calibration artifact") from exc
    if not trained_through < day or not first <= day < last:
        raise NotReady("HYPE C1 calibration is not valid for this session")
    keys = ("directional_delta_imbalance", "directional_result_atr")
    if any(not _finite(thresholds.get(key)) for key in keys):
        raise NotReady("HYPE C1 thresholds incomplete")
    features = hype_candidate_features(bars, direction, atr_before, signal_atr)
    # Existing research gate; do not create a new threshold or selector.
    return _gate(features, "delta_without_result", thresholds), features


def evaluate_completed_bar(*, symbol, profile, bars, as_of_ms,
                           references=None, atr_before=None, trend=None,
                           c1_calibration=None, signal_atr=None):
    """Evaluate one just-completed 15m bar. Returns None or immutable evidence.

    Bars after as_of_ms are rejected, never silently sliced away. The caller
    must call this on every completed 15m close in 09:00-12:00 NY time.
    A session with no candidate is still WAIT until the entry cutoff.
    """
    if symbol not in POLICIES:
        raise ValueError("No paper contract for symbol")
    day = date.fromisoformat(profile["session_day"])
    start, cutoff = clock_ms(day, 9), clock_ms(day, 12)
    if type(as_of_ms) is not int or not start + BAR_MS <= as_of_ms <= cutoff or (as_of_ms - start) % BAR_MS:
        raise ValueError("Expected a completed NY 15m evaluation close")
    checked = sorted((validate_bar(b) for b in bars), key=lambda b:b["open_timestamp_ms"])
    if any(b["close_timestamp_ms"] > as_of_ms for b in checked):
        raise ValueError("Future bar supplied to paper evaluator")
    if not checked or checked[-1]["close_timestamp_ms"] != as_of_ms:
        raise NotReady("Current completed 15m bar missing")
    signals, decisions = evaluate_session(profile, checked, symbol=symbol)
    # evaluate_session reports a synthetic cutoff NO_TRADE on partial sessions;
    # that row must never be taken as a current decision before 12:00.
    current = next((d for d in decisions if d["timestamp_ms"] == as_of_ms and d["state"] != "NO_TRADE"), None)
    if current is None:
        raise NotReady("Current decision missing")
    if not signals:
        return dict(state="NO_TRADE" if as_of_ms == cutoff else "WAIT", decision=current, signal=None)
    signal = signals[0]
    if signal["feature_as_of_ms"] != as_of_ms:
        raise ValueError("Earlier first candidate was not processed at its close")
    if signal["session_day"] != profile["session_day"] or signal["symbol"] != symbol:
        raise ValueError("Signal/profile mismatch")
    atr_before = atr_before or {}
    if any(int(key) > checked[-1]["open_timestamp_ms"] for key in atr_before):
        raise ValueError("Future ATR snapshot supplied")
    # HYPE's frozen selector is C1, not C2. Do not impose unrelated C2 warmup.
    if symbol == "HYPEUSDT":
        if signal_atr is not None:
            end = signal_atr.get("end_ms")
            if (type(end) is not int or end > as_of_ms or as_of_ms-end >= 3_600_000
                    or not _finite(signal_atr.get("atr14")) or signal_atr["atr14"] <= 0):
                raise NotReady("Completed signal-time HYPE ATR is unavailable")
        expected = list(range(start, as_of_ms, BAR_MS))
        if [b["open_timestamp_ms"] for b in checked] != expected:
            raise NotReady("Incomplete 09:00-to-signal order-flow coverage")
        gate, features = _hype_gate(checked, signal["direction"], atr_before,
                                    c1_calibration, day, signal_atr)
        row = dict(trend_15m={}, effort_result_pre_entry={"setup_direction":{"labels":[], "state":"NOT_USED"}},
                   research_gates={"delta_without_result":{"gate":gate}})
        selected = bool(keep(row, POLICIES[symbol]))
        snapshot = dict(feature_as_of_ms=as_of_ms, profile=profile, decision=current,
                        c2=None, trend_15m=None, c1_features=features,
                        c1_calibration=c1_calibration, selector=POLICIES[symbol],
                        selected=selected, risk_fraction=.005)
        return dict(state="ENTRY_READY" if selected else "SELECTOR_REJECTED",
                    decision=current, signal=signal, snapshot=snapshot,
                    selected=selected, risk_fraction=.005)
    if references is None or references.get("through_session_day") is None:
        raise NotReady("Prior-session C2 references missing")
    if not date.fromisoformat(references["through_session_day"]) < day:
        raise ValueError("C2 references include current/future session")
    slot = (as_of_ms - start) // BAR_MS - 1
    slot_refs = references.get("slots", {})
    ref = slot_refs.get(slot, slot_refs.get(str(slot)))
    if not isinstance(ref, dict) or ref.get("prior_sessions", 0) < 10:
        raise NotReady("Insufficient same-clock C2 history")
    refs = {int(k): v for k, v in slot_refs.items()}
    timeline = annotate_session(str(day), checked, profile, refs, atr_before)
    evidence = next((r for r in timeline if r["feature_as_of_ms"] == as_of_ms), None)
    if evidence is None or evidence["status"] != "observed" or not evidence["session_coverage_complete"]:
        raise NotReady("Incomplete 09:00-to-signal order-flow coverage")
    direction_state = evidence["directions"][signal["direction"]]
    if direction_state["state"] in ("UNKNOWN_DATA", "UNKNOWN_REFERENCE"):
        raise NotReady("C2 references not ready")
    gate = None
    c1_features = None
    if symbol == "HYPEUSDT":
        gate, c1_features = _hype_gate(checked, signal["direction"], atr_before, c1_calibration, day)
    row = dict(direction=signal["direction"], trend_15m=trend or {},
               effort_result_pre_entry={"setup_direction": direction_state},
               research_gates={"delta_without_result": {"gate": gate}})
    selected = bool(keep(row, POLICIES[symbol]))
    risk = _risk(symbol, trend, signal["direction"], as_of_ms)
    snapshot = dict(feature_as_of_ms=as_of_ms, profile=profile, decision=current,
                    c2=evidence, trend_15m=trend, c1_features=c1_features,
                    c1_calibration=c1_calibration, selector=POLICIES[symbol],
                    selected=selected, risk_fraction=risk)
    return dict(state="ENTRY_READY" if selected else "SELECTOR_REJECTED",
                decision=current, signal=signal, snapshot=snapshot,
                selected=selected, risk_fraction=risk)


def submit_completed_bar(manager, *, symbol, **inputs):
    """Submit the first signal to the shared paper order manager exactly once."""
    if symbol not in ACCOUNT_IDS:
        raise ValueError("No paper contract for symbol")
    prior = manager.store.db.execute(
        "SELECT payload FROM intents WHERE account=? AND session=?",
        (ACCOUNT_IDS[symbol], inputs["profile"]["session_day"])).fetchone()
    if prior:
        import json
        first_asof = json.loads(prior[0])["signal"]["feature_as_of_ms"]
        if inputs["as_of_ms"] > first_asof:
            return dict(state="SESSION_LOCKED", first_candidate_as_of_ms=first_asof,
                        signal=None, paper_result="session_locked")
    result = evaluate_completed_bar(symbol=symbol, **inputs)
    if result["signal"] is None:
        return result
    outcome = manager.submit(ACCOUNT_IDS[symbol], result["signal"],
                             snapshot=result["snapshot"],
                             risk_fraction=result["risk_fraction"],
                             selected=result["selected"])
    return {**result, "paper_result": outcome}
