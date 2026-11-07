import json
from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_mvrv.binance_probe import frozen, guard, init_probe, phase, restore_surplus, verify
from live_mvrv.binance_execution import reserve, inventory
from live_mvrv.binance_spot import UnknownExecution
from live_mvrv.data import utc
from live_mvrv.storage import connect, get_meta, set_meta
from test_binance import Fake, INFO, NOW


class ProbeFake(Fake):
    def rules(self, symbol): return INFO
    def market(self, *args):
        order = super().market(*args)
        trade = self.fills[order['orderId']][0]
        trade['time'] = int(utc(NOW).timestamp() * 1000)
        trade['commission'] = '.001'
        self.cash -= Decimal('.001')
        return order


def prepared(tmp_path):
    strategy = connect(tmp_path / 'strategy.sqlite')
    spot = ProbeFake()
    reserve(strategy, spot, '1000', NOW)
    c = connect(tmp_path / 'probe.sqlite')
    init_probe(c, strategy, spot, '50', NOW, tmp_path / 'before.sqlite')
    return c, strategy, spot


def test_roundtrip_restart_native_fees_and_surplus_only(tmp_path):
    c, strategy, spot = prepared(tmp_path)
    snapshot = frozen(strategy)
    phase(c, strategy, spot, 'buy', NOW)
    assert len(spot.posts) == 2
    state = [tuple(r) for r in c.execute('SELECT * FROM accounts ORDER BY id')]
    fills = c.execute('SELECT COUNT(*) FROM fills').fetchone()[0]
    c.close()
    c = connect(tmp_path / 'probe.sqlite')
    verify(c, strategy, spot, NOW)
    phase(c, strategy, spot, 'buy', NOW)  # Replay after restart cannot repost.
    assert [tuple(r) for r in c.execute('SELECT * FROM accounts ORDER BY id')] == state
    assert c.execute('SELECT COUNT(*) FROM fills').fetchone()[0] == fills
    assert len(spot.posts) == 2
    phase(c, strategy, spot, 'sell', NOW)
    assert len(spot.posts) == 4
    result = verify(c, strategy, spot, NOW)
    assert result['strategy_performance'] is False
    assert result['native_fees'] == [{'fee_asset': 'USDT', 'quantity': .004}]
    restored = restore_surplus(c, strategy, spot, NOW)
    assert Decimal(restored['delta']['USDT']) == Decimal('-.104')
    assert Decimal(restored['delta']['BTC']) == Decimal('.001')
    assert frozen(strategy) == snapshot
    assert not get_meta(strategy, 'active_testnet_probe')
    inventory(strategy, spot)
    assert restore_surplus(c, strategy, spot, NOW) == restored
    assert strategy.execute('SELECT COUNT(*) FROM fills').fetchone()[0] == 0
    assert all(Decimal(r[0]) == 500 for r in strategy.execute('SELECT cash FROM accounts'))
    with pytest.raises(ValueError, match='Completed'): phase(c, strategy, spot, 'buy', NOW)


@pytest.mark.parametrize('capital', ['0', '-1', '51', 'NaN'])
def test_budget_bound_never_arms_or_orders(tmp_path, capital):
    c, strategy, spot = prepared(tmp_path)
    other = connect(tmp_path / 'another.sqlite')
    with pytest.raises(ValueError): init_probe(other, strategy, spot, capital, NOW, tmp_path / 'no.sqlite')
    assert not spot.posts


def test_probe_cannot_attach_to_strategy_db(tmp_path):
    c, strategy, spot = prepared(tmp_path)
    with pytest.raises(ValueError, match='dedicated'): guard(strategy, strategy, spot)
    assert not spot.posts


def test_changed_strategy_halts_before_buy(tmp_path):
    c, strategy, spot = prepared(tmp_path)
    with strategy: strategy.execute("UPDATE accounts SET cash='499' WHERE id='fixed_0_7'")
    with pytest.raises(ValueError, match='changed'): phase(c, strategy, spot, 'buy', NOW)
    assert not spot.posts


def test_missing_marker_halts_before_buy(tmp_path):
    c, strategy, spot = prepared(tmp_path)
    with strategy: set_meta(strategy, 'active_testnet_probe', '')
    with pytest.raises(ValueError, match='active'): phase(c, strategy, spot, 'buy', NOW)
    assert not spot.posts


def test_no_sell_before_buy_and_no_early_restore(tmp_path):
    c, strategy, spot = prepared(tmp_path)
    with pytest.raises(ValueError, match='buys'): phase(c, strategy, spot, 'sell', NOW)
    with pytest.raises(ValueError, match='exactly'): restore_surplus(c, strategy, spot, NOW)
    assert not spot.posts


def test_unknown_probe_post_not_retried(tmp_path):
    c, strategy, spot = prepared(tmp_path)
    spot.unknown = True
    with pytest.raises(ValueError, match='unknown'): phase(c, strategy, spot, 'buy', NOW)
    c.close()
    c = connect(tmp_path / 'probe.sqlite')
    with pytest.raises(ValueError, match='unknown'): phase(c, strategy, spot, 'buy', NOW)
    assert len(spot.posts) == 1
    assert get_meta(strategy, 'active_testnet_probe')


def test_accepted_lost_response_recovers_then_only_buys_other_branch(tmp_path):
    c, strategy, spot = prepared(tmp_path)
    actual_market = spot.market
    def lost(*args):
        actual_market(*args)
        raise UnknownExecution()
    spot.market = lost
    with pytest.raises(ValueError, match='unknown'): phase(c, strategy, spot, 'buy', NOW)
    assert len(spot.posts) == 1
    c.close()
    c = connect(tmp_path / 'probe.sqlite')
    spot.market = actual_market
    verify(c, strategy, spot, NOW)
    assert c.execute('SELECT COUNT(*) FROM fills').fetchone()[0] == 1
    phase(c, strategy, spot, 'buy', NOW)
    assert len(spot.posts) == 2
    assert len({post[0] for post in spot.posts}) == 2


def test_probe_can_be_loaded_from_online_backup(tmp_path):
    from live_mvrv.storage import backup
    c, strategy, spot = prepared(tmp_path)
    phase(c, strategy, spot, 'buy', NOW)
    phase(c, strategy, spot, 'sell', NOW)
    expected = frozen(c)
    backup(c, tmp_path / 'restore.sqlite')
    restored = connect(tmp_path / 'restore.sqlite')
    assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert frozen(restored) == expected
    verify(restored, strategy, spot, NOW)
    assert len(spot.posts) == 4


def test_unexplained_venue_delta_cannot_adjust_surplus(tmp_path):
    c, strategy, spot = prepared(tmp_path)
    phase(c, strategy, spot, 'buy', NOW)
    phase(c, strategy, spot, 'sell', NOW)
    old = get_meta(strategy, 'venue_unallocated')
    spot.cash += 1
    with pytest.raises(ValueError, match='mismatch'): restore_surplus(c, strategy, spot, NOW)
    assert get_meta(strategy, 'venue_unallocated') == old
    assert get_meta(strategy, 'active_testnet_probe')


def test_worker_cannot_run_during_probe(tmp_path):
    from live_mvrv.binance_main import _tick
    c, strategy, spot = prepared(tmp_path)
    with pytest.raises(ValueError, match='probe active'): _tick(strategy, spot, '1000')
    assert not spot.posts
