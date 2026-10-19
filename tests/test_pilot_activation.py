from pathlib import Path

from live_engine.futures_execution import FuturesExecutor
from live_engine.futures_store import FuturesStore
from models.trend_following_preny_profile_15m.pilot_worker import load_policy
from test_futures_execution import FakeVenue,NOW,DAY,intent

ROOT=Path(__file__).resolve().parents[1]
DEPLOY=ROOT/'models/trend_following_preny_profile_15m/deploy'


def test_live_and_pause_only_change_execution_switches():
    base=load_policy(DEPLOY/'pilot.ethusdc.execution.json')
    live=load_policy(DEPLOY/'pilot.ethusdc.execution.live.json')
    pause=load_policy(DEPLOY/'pilot.ethusdc.execution.paused.json')
    assert base['execution_enabled'] is False and base['accept_new_entries'] is False
    assert {k for k in base if base[k]!=live[k]}=={'execution_enabled','accept_new_entries'}
    assert {k for k in base if base[k]!=pause[k]}=={'execution_enabled'}
    assert pause['execution_enabled'] is True and pause['accept_new_entries'] is False


def test_activation_preserves_ledger_and_source_anchor(tmp_path,monkeypatch):
    monkeypatch.setattr('time.time',lambda:NOW/1000)
    base=load_policy(DEPLOY/'pilot.ethusdc.execution.json')
    path=tmp_path/'execution-200.sqlite'
    with_store=FuturesStore(path,base)
    with with_store.transaction():
        with_store.set_meta('source_cursor','22')
        with_store.set_meta('source_identity','original-producer')
    with_store.close()
    live=load_policy(DEPLOY/'pilot.ethusdc.execution.live.json')
    store=FuturesStore(path,live)
    try:
        assert store.meta('source_cursor')=='22' and store.meta('source_identity')=='original-producer'
        assert store.meta('initial_wallet') is None
        assert store.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==0
        venue=FakeVenue();executor=FuturesExecutor(store,venue,live)
        assert executor.offer(intent(),NOW)=='PENDING'
        assert not venue.posts  # Configuration/offer alone must not submit an order.
    finally:store.close()


def test_pause_blocks_new_entries_but_manages_existing_position(tmp_path,monkeypatch):
    clock=[NOW];monkeypatch.setattr('time.time',lambda:clock[0]/1000)
    live=load_policy(DEPLOY/'pilot.ethusdc.execution.live.json')
    store=FuturesStore(tmp_path/'execution-200.sqlite',live);venue=FakeVenue()
    executor=FuturesExecutor(store,venue,live)
    try:
        executor.offer(intent(),NOW);executor.cycle(NOW)
        assert venue.position>0
        executor.c=load_policy(DEPLOY/'pilot.ethusdc.execution.paused.json')
        clock[0]=intent()['force_exit_timestamp_ms'];venue.clock=clock[0]
        executor.cycle(clock[0]);executor.cycle(clock[0]+1)
        assert venue.position==0 and store.active() is None
        from datetime import timedelta
        from test_futures_execution import millis
        day=DAY+timedelta(days=1);t=millis(day,9,30)
        p=intent(session_id=str(day),feature_as_of_ms=t,signal_timestamp_ms=t,eligible_timestamp_ms=t,
                 expires_timestamp_ms=t+300000,force_exit_timestamp_ms=millis(day+timedelta(days=1),8,59))
        assert executor.offer(p,t)=='SKIPPED'
        assert [role for role,_ in venue.posts].count('ENTRY')==1
    finally:store.close()


def test_overlay_does_not_weaken_base_or_include_diagnostic_entry():
    overlay=(DEPLOY/'pilot.executor.live.yaml').read_text()
    base=(DEPLOY/'pilot.executor.yaml').read_text()
    assert '${GG_ETH_PILOT_ACTIVATION:?' in overlay
    assert '--acknowledge-real-money' in overlay and '--acknowledge-real-money' not in base
    assert '/data/execution-200.sqlite' in overlay and '/strategy/strategy.sqlite' in overlay
    assert 'read_only: true' in overlay and 'create_host_path: false' in overlay
    assert 'pilot_live_smoke' not in overlay and 'BINANCE_API_KEY' not in overlay
    assert 'execution.live.json' not in base
