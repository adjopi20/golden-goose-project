from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from live_mvrv import signal_monitor as monitor
from live_mvrv.engine import configuration, initialize
from live_mvrv.storage import connect, get_meta


NOW = '2015-01-03T03:00:00+00:00'


def raw(end='2015-01-02'):
    dates = pd.date_range(monitor.data.ORIGIN, end)
    i = np.arange(len(dates))
    return pd.DataFrame(dict(time=dates.strftime('%Y-%m-%d'), PriceUSD=100 + i / 10,
                             CapMrktCurUSD=1e7 + i * 10000, CapMVRVCur=1.2 + .3 * np.sin(i / 50)))


@pytest.mark.parametrize('z,expected', [(-.1, 'BUY_ZONE'), (0, 'WAIT'), (7, 'SELL_ZONE')])
def test_strict_entry_and_inclusive_exit(z, expected):
    row = dict(price=100, z=z, fixed_0_7_entry=0, fixed_0_7_exit=7,
               percentile_4y_p5_p95_entry=-.5, percentile_4y_p5_p95_exit=3)
    assert monitor.zones(row, configuration())[0]['zone'] == expected


def test_daily_monitor_no_accounts_orders_or_duplicate_messages(tmp_path, monkeypatch):
    frame = raw()
    monkeypatch.setattr(monitor.data, 'fetch', lambda *a: frame)
    c = connect(tmp_path / 'signals.sqlite')
    monitor.checked_cycle(c, NOW)
    for table in ('accounts', 'actions', 'fills', 'marks'):
        assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert c.execute('SELECT COUNT(*) FROM decisions').fetchone()[0] == 2
    assert c.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 1
    c.close()
    c = connect(tmp_path / 'signals.sqlite')
    monkeypatch.setattr(monitor.data, 'fetch', lambda *a: pytest.fail('Already accepted day should not refetch'))
    monitor.checked_cycle(c, NOW)
    assert c.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 1
    assert monitor.report(c)['orders_enabled'] is False


def test_stale_data_is_not_published(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor.data, 'fetch', lambda *a: raw('2015-01-01'))
    c = connect(tmp_path / 'signals.sqlite')
    with pytest.raises(monitor.data.DataError): monitor.checked_cycle(c, NOW)
    assert c.execute('SELECT COUNT(*) FROM decisions').fetchone()[0] == 0
    assert get_meta(c, 'last_data_date') is None
    assert get_meta(c, 'error')


def test_revision_halts_and_preserves_daily_decisions(tmp_path, monkeypatch):
    frame = raw()
    monkeypatch.setattr(monitor.data, 'fetch', lambda *a: frame)
    c = connect(tmp_path / 'signals.sqlite')
    monitor.checked_cycle(c, NOW)
    changed = raw('2015-01-03')
    changed.loc[100, 'CapMVRVCur'] += .01
    monkeypatch.setattr(monitor.data, 'fetch', lambda *a: changed)
    with pytest.raises(monitor.data.DataError, match='revision'):
        monitor.checked_cycle(c, '2015-01-04T03:00:00+00:00')
    assert get_meta(c, 'last_data_date') == '2015-01-02'
    assert c.execute('SELECT COUNT(*) FROM decisions').fetchone()[0] == 2


def test_funded_database_refused(tmp_path):
    c = connect(tmp_path / 'funded.sqlite')
    with c: initialize(c, configuration(), '1000', 'alerts', NOW)
    with pytest.raises(ValueError, match='refuses'): monitor.bind(c)


def test_old_queued_status_superseded_on_next_day(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor.data, 'fetch', lambda *a: raw())
    c = connect(tmp_path / 'signals.sqlite')
    monitor.checked_cycle(c, NOW)
    monkeypatch.setattr(monitor.data, 'fetch', lambda *a: raw('2015-01-03'))
    monitor.checked_cycle(c, '2015-01-04T03:00:00+00:00')
    assert c.execute("SELECT delivered_at FROM outbox WHERE id='signal-status:2015-01-02'").fetchone()[0].startswith('superseded:')
    assert monitor.report(c)['unsent_notifications'] == 1


def test_compose_has_no_exchange_secrets_or_capital():
    text = (Path(__file__).resolve().parents[1] / 'compose.signal-monitor.yaml').read_text()
    assert 'BINANCE_' not in text and 'INITIAL_CAPITAL' not in text
    assert 'signal_data:/data' in text and 'signals.sqlite' in text


def test_notification_failure_keeps_outbox_and_recovers(tmp_path, monkeypatch):
    c = connect(tmp_path / 'signals.sqlite')
    monkeypatch.setattr(monitor, 'deliver', lambda *a: (_ for _ in ()).throw(RuntimeError('secret token')))
    monitor.notify(c, monitor.data.utc(NOW))
    assert 'secret token' not in get_meta(c, 'notification_error')
    monkeypatch.setattr(monitor, 'deliver', lambda *a: 1)
    monitor.notify(c, monitor.data.utc(NOW))
    assert get_meta(c, 'notification_error') == ''
