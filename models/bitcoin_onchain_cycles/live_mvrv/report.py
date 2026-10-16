"""USD reference marks; combined DD is calculated from the combined curve."""
import json

import numpy as np
import pandas as pd

from .storage import get_meta


def metrics(equity, dates, initial):
    equity = np.asarray(equity, dtype=float)
    if not len(equity):
        return {}
    returns = equity / np.r_[initial, equity[:-1]] - 1
    dd = equity / np.maximum.accumulate(np.r_[initial, equity])[1:] - 1
    days = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days
    cagr = (equity[-1] / initial) ** (365.25 / days) - 1 if days else None
    vol = returns.std()
    downside = np.sqrt(np.mean(np.minimum(returns, 0) ** 2))
    streak = longest = 0
    for below in dd < -1e-12:
        streak = streak + 1 if below else 0
        longest = max(streak, longest)
    return dict(ending_value=float(equity[-1]), return_pct=float(equity[-1] / initial - 1),
                max_dd=float(dd.min()), cagr=cagr,
                calmar=cagr / abs(dd.min()) if cagr is not None and dd.min() else None,
                sharpe=float(returns.mean() / vol * np.sqrt(365)) if vol else None,
                sortino=float(returns.mean() / downside * np.sqrt(365)) if downside else None,
                longest_underwater_days=longest, underwater_days=int((dd < -1e-12).sum()))


def report(c):
    accounts = [dict(r) for r in c.execute("SELECT * FROM accounts ORDER BY id")]
    marks = pd.read_sql_query("SELECT * FROM marks ORDER BY date,account", c)
    portfolio = {}
    by_account = {}
    if not marks.empty:
        pivot = marks.pivot(index="date", columns="account", values="equity")
        if pivot.isna().any().any() or len(pivot.columns) != len(accounts):
            raise ValueError("Incomplete portfolio marks")
        portfolio = metrics(pivot.sum(axis=1).to_numpy(), list(pivot.index), float(get_meta(c, "initial_capital")))
        for account in accounts:
            branch = pivot[account["id"]]
            by_account[account["id"]] = metrics(branch.to_numpy(), list(branch.index), float(get_meta(c, "initial_capital")) / 2)
    last_price = float(get_meta(c, "last_mark_price", 0))
    for account in accounts:
        account["marked_btc_value"] = float(account["btc"]) * last_price
        account["current_reference_equity"] = float(account["cash"]) + account["marked_btc_value"]
    return dict(contract="btc-mvrv-dual-v1", mode=get_meta(c, "mode"),
                last_data_date=get_meta(c, "last_data_date"), last_decision_date=get_meta(c, "last_decision_date"),
                error=get_meta(c, "error"), accounts=accounts, portfolio=portfolio, by_account=by_account,
                pending=[dict(r) for r in c.execute("SELECT * FROM actions WHERE status='pending'")],
                latest_decisions=[dict(r) for r in c.execute("SELECT * FROM decisions WHERE date=(SELECT MAX(date) FROM decisions)")],
                fills=c.execute("SELECT COUNT(*) FROM fills").fetchone()[0],
                unsent_notifications=c.execute("SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL").fetchone()[0],
                valuation="Coin Metrics daily USD reference, not current executable price",
                portfolio_metrics_scope="Recorded daily marks; manual intraday fills affect the next accepted mark. Current balances are separately valued at the last reference price.",
                timing="Paper planned future-close proxy; publication/recording timestamps retained. Alerts holdings only change on manual fills.",
                extra_costs="spread/slippage, tax, cash interest not modeled")
