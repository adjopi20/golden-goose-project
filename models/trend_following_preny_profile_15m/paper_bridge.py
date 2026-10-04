"""Causal prepared-data bridge from the frozen model to the shared paper ledger.

The venue feed/preparation service must supply a finalized profile, completed
bars, prior-session references, and completed indicator snapshots. No market
connection, feature-fitting, raw tick archive, or real order submission here.
"""


from .runtime.evaluator import (POLICIES, ACCOUNT_IDS, NotReady, _finite, _risk, hype_candidate_features, _hype_gate, evaluate_completed_bar)


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
