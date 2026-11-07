from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from live_mvrv import data
from live_mvrv.engine import apply_day, configuration, fill, initialize
from live_mvrv.main import checked_cycle, health
from live_mvrv.notify import deliver
from live_mvrv.report import metrics, report
from live_mvrv.storage import connect, get_meta, backup


def opened(tmp_path, mode="paper", capital="1000"):
    c = connect(tmp_path / "state.sqlite")
    with c:
        initialize(c, configuration(), capital, mode, "2020-01-02T00:00:00+00:00")
    return c


def row(day, price=100, z=-1, p5=-.5, p95=3):
    return dict(date=day, price=price, z=z, fixed_0_7_entry=0, fixed_0_7_exit=7,
                percentile_4y_p5_p95_entry=p5, percentile_4y_p5_p95_exit=p95)


def day(c, r, mode="paper", now=None):
    when = now or (pd.Timestamp(r["date"], tz="UTC") + pd.Timedelta(days=1)).isoformat()
    with c:
        return apply_day(c, r, configuration(), when, mode)


def balances(c):
    return {r["id"]: (Decimal(r["cash"]), Decimal(r["btc"])) for r in c.execute("SELECT * FROM accounts")}


def test_init_5050_restart_and_config_lock(tmp_path):
    c = opened(tmp_path)
    assert set(balances(c).values()) == {(Decimal("500"), Decimal(0))}
    day(c, row("2020-01-01"))
    c.close()
    c = connect(tmp_path / "state.sqlite")
    with c:
        initialize(c, configuration(), "1000.0", "paper", "2020-01-03T00:00:00+00:00")
    assert c.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 2
    with pytest.raises(ValueError), c:
        initialize(c, configuration(), "2000", "paper", "2020-01-03T00:00:00+00:00")
    assert set(balances(c).values()) == {(Decimal("500"), Decimal(0))}


def test_duplicates_and_future_close_only(tmp_path):
    c = opened(tmp_path)
    day(c, row("2020-01-01"), now="2020-01-02T01:00:00+00:00")
    assert not day(c, row("2020-01-01"), now="2020-01-02T02:00:00+00:00")
    assert all(btc == 0 for cash, btc in balances(c).values())
    day(c, row("2020-01-02", 110), now="2020-01-03T01:00:00+00:00")
    assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
    assert all(float(btc) == pytest.approx(500 / 1.001 / 110) for cash, btc in balances(c).values())
    with pytest.raises(ValueError):
        day(c, row("2020-01-03"), now="2020-01-03T10:00:00+00:00")


def test_no_cross_account_transfer_and_reinvest_profits(tmp_path):
    c = opened(tmp_path)
    day(c, row("2020-01-01", z=-.2))  # Only fixed buys.
    day(c, row("2020-01-02", price=100, z=2))
    day(c, row("2020-01-03", price=200, z=8))
    day(c, row("2020-01-04", price=200, z=1))
    a, b = balances(c).values()
    assert float(a[0]) == pytest.approx(500 / 1.001 * 2 * .999)
    assert b == (Decimal("500.0"), Decimal(0))
    day(c, row("2020-01-05", price=100, z=-.2))
    day(c, row("2020-01-06", price=100, z=1))
    assert float(balances(c)["fixed_0_7"][1]) > 9
    assert balances(c)["percentile_4y_p5_p95"] == b


def test_downtime_does_not_backfill_missed_price(tmp_path):
    c = opened(tmp_path)
    day(c, row("2020-01-01"), now="2020-01-02T01:00:00+00:00")
    day(c, row("2020-01-04", z=1), now="2020-01-05T01:00:00+00:00")
    assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM actions WHERE status='expired'").fetchone()[0] == 2


def test_alerts_require_manual_fill_partial_and_idempotency(tmp_path):
    c = opened(tmp_path, "alerts")
    day(c, row("2020-01-01"), "alerts")
    day(c, row("2020-01-02", z=1), "alerts")
    assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    key = c.execute("SELECT id FROM actions WHERE account='fixed_0_7'").fetchone()[0]
    with c:
        fill(c, key, "operator-1", "1", "100", ".1", "2020-01-03T01:00:00+00:00", complete=False)
        assert not fill(c, key, "operator-1", "1", "100", ".1", "2020-01-03T01:00:00+00:00", complete=False)
    assert balances(c)["fixed_0_7"] == (Decimal("399.9"), Decimal(1))
    with pytest.raises(ValueError), c:
        fill(c, key, "bad", "5", "100", ".5", "2020-01-03T01:00:00+00:00")
    assert balances(c)["fixed_0_7"][0] == Decimal("399.9")
    with pytest.raises(ValueError), c:
        fill(c, key, "operator-1", "2", "100", ".1", "2020-01-03T01:00:00+00:00")
    with c:
        fill(c, key, "operator-2", "1", "100", ".1", "2020-01-03T02:00:00+00:00")
    assert c.execute("SELECT status FROM actions WHERE id=?", (key,)).fetchone()[0] == "filled"


def test_backdated_operator_fill_rejected(tmp_path):
    c = opened(tmp_path, "alerts")
    day(c, row("2020-01-01"), "alerts", now="2020-01-02T01:00:00+00:00")
    key = c.execute("SELECT id FROM actions LIMIT 1").fetchone()[0]
    with pytest.raises(ValueError), c:
        fill(c, key, "bad", 1, 100, 0, "2020-01-02T02:00:00+00:00", "2020-01-02T00:00:00+00:00")


def raw_frame(n=1500):
    return pd.DataFrame(dict(time=pd.date_range(data.ORIGIN, periods=n).strftime("%Y-%m-%d"),
                             PriceUSD=np.arange(n) + 100., CapMrktCurUSD=(np.arange(n) + 100.) * 1e6,
                             CapMVRVCur=1. + np.sin(np.arange(n) / 10) / 10))


def test_formula_full_warmup_and_causality():
    raw = raw_frame()
    d = data.prepare(raw, configuration())
    col = "percentile_4y_p5_p95_entry"
    assert d[col].iloc[:1460].isna().all()
    assert d[col].iloc[1460] == pytest.approx(d.z.iloc[:1460].quantile(.05))
    changed = raw.copy()
    changed.loc[1480:, "CapMVRVCur"] = 10
    pd.testing.assert_frame_equal(d.iloc[:1479], data.prepare(changed, configuration()).iloc[:1479])
    with pytest.raises(data.DataError):
        data.prepare(raw.iloc[1:], configuration())


@pytest.mark.parametrize("problem", ["gap", "duplicate", "nan", "negative"])
def test_bad_history_fails_closed(problem):
    raw = raw_frame()
    if problem == "gap": raw = raw.drop(index=50)
    if problem == "duplicate": raw = pd.concat([raw, raw.iloc[[2]]])
    if problem == "nan": raw.loc[3, "CapMVRVCur"] = np.nan
    if problem == "negative": raw.loc[3, "PriceUSD"] = -1
    with pytest.raises(data.DataError):
        data.prepare(raw, configuration())


def test_revision_archived_and_account_unchanged(tmp_path):
    c = opened(tmp_path)
    raw = raw_frame(1600)
    path = tmp_path / "raw.csv"
    raw.to_csv(path, index=False)
    now = (pd.Timestamp(raw.time.iloc[-1], tz="UTC") + pd.Timedelta(days=1, hours=1)).isoformat()
    checked_cycle(c, configuration(), "1000", "paper", now=now, input_path=path)
    before = balances(c)
    raw.loc[50, "CapMVRVCur"] += .1
    raw.to_csv(path, index=False)
    with pytest.raises(data.DataError):
        checked_cycle(c, configuration(), "1000", "paper", now=now, input_path=path)
    assert balances(c) == before
    assert c.execute("SELECT COUNT(*) FROM vintages").fetchone()[0] == 2
    assert "revision" in get_meta(c, "error")


def test_stale_data_and_health(tmp_path):
    c = opened(tmp_path)
    raw = raw_frame(1600)
    path = tmp_path / "raw.csv"
    raw.to_csv(path, index=False)
    now = (pd.Timestamp(raw.time.iloc[-1], tz="UTC") + pd.Timedelta(days=2, hours=1)).isoformat()
    with pytest.raises(data.DataError):
        checked_cycle(c, configuration(), "1000", "paper", now=now, input_path=path)
    assert not health(c, now)
    assert c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_outbox_retry_redacts_secret_and_suppresses_old_proposal(tmp_path, monkeypatch):
    import requests
    c = opened(tmp_path)
    day(c, row("2020-01-01"))
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-example")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    class Bad:
        def post(self, *a, **kw): raise requests.ConnectionError("secret-example")
    with pytest.raises(RuntimeError) as error:
        deliver(c, "2020-01-02T01:00:00+00:00", Bad())
    assert "secret-example" not in str(error.value)
    assert c.execute("SELECT COUNT(*) FROM outbox WHERE delivered_at IS NOT NULL").fetchone()[0] == 0
    day(c, row("2020-01-03", z=1))  # Expire both proposals.
    class Good:
        def post(self, *a, **kw):
            assert "Proposal" not in kw["json"]["text"]
            class R:
                status_code = 200
                def json(self): return {"ok": True}
            return R()
    deliver(c, "2020-01-04T01:00:00+00:00", Good())
    assert c.execute("SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL").fetchone()[0] == 0


def test_portfolio_dd_is_not_mean_branch_dd():
    a, b = np.array([500, 1000, 500]), np.array([500, 500, 1000])
    dates = ["2020-01-01", "2020-01-02", "2020-01-03"]
    combined = metrics(a + b, dates, 1000)
    assert combined["max_dd"] == 0
    assert (metrics(a, dates, 500)["max_dd"] + metrics(b, dates, 500)["max_dd"]) / 2 == -.25


def test_backup_persists_pending_state(tmp_path):
    c = opened(tmp_path)
    day(c, row("2020-01-01"))
    path = tmp_path / "backup.sqlite"
    backup(c, path)
    restored = connect(path)
    assert balances(restored) == balances(c)
    assert restored.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 2
    with pytest.raises(FileExistsError): backup(c, path)


def test_both_accounts_roll_back_on_second_branch_failure(tmp_path):
    c = opened(tmp_path)
    broken = row("2020-01-01")
    del broken["percentile_4y_p5_p95_entry"]
    with pytest.raises(KeyError):
        day(c, broken)
    assert c.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert get_meta(c, "last_decision_date") is None


def test_late_evaluation_never_plans_past_close():
    assert data.next_close("2020-01-01", "2020-01-04T10:00:00+00:00") == "2020-01-05T00:00:00+00:00"


def test_pagination_repeated_token_fails_closed():
    class Repeated:
        def get(self, *a, **kw):
            class R:
                def raise_for_status(self): pass
                def json(self): return {"data": [], "next_page_token": "same-token"}
            return R()
    with pytest.raises(data.DataError, match="Repeated"):
        data.fetch(data.ORIGIN, "2020-01-01", Repeated())


def test_malformed_api_payload_fails_closed():
    class Malformed:
        def get(self, *a, **kw):
            class R:
                def raise_for_status(self): pass
                def json(self): return {"data": "not-an-array"}
            return R()
    with pytest.raises(data.DataError, match="Malformed"):
        data.fetch(data.ORIGIN, "2020-01-01", Malformed())
