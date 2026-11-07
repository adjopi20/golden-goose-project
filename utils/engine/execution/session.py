"""Select a venue adapter; both modes construct the exact same order lifecycle."""
from pathlib import Path

from utils.engine.execution.preny_futures import FuturesExecutor
from utils.engine.state.futures import FuturesStore


def create_execution(*, mode, config, database, adapter_options):
    """No activation on import. Current supported contract is Pre-NY ETH Futures.

    Paper requires a separate durable venue DB, explicit production instrument
    metadata and fee configuration. Live retains its existing acknowledgement and
    credential guards. Model decisions are not selected or modified here.
    """
    if mode not in ('paper','live'):raise ValueError('Unknown execution mode')
    if config.get('symbol')!='ETHUSDC':
        raise ValueError('Only the declared TF-PRENY-ETH-001 Futures contract is supported')
    if any(type(config.get(k)) is not bool for k in ('execution_enabled','accept_new_entries')):
        raise ValueError('Execution switches must be explicit booleans')
    cfg=dict(config,execution_mode=mode)
    if mode=='paper':
        from utils.engine.adapters.paper_futures import PaperFutures
        venue_path=Path(adapter_options['database']).resolve()
        if venue_path==Path(database).resolve():raise ValueError('Paper venue/account DBs must be separate')
        options={k:v for k,v in adapter_options.items() if k!='database'}
        venue=PaperFutures(venue_path,symbol=cfg['symbol'],initial_equity=cfg['initial_equity'],
                           enabled=cfg['execution_enabled'],**options)
    else:
        from utils.engine.adapters.binance_execution import FuturesOrders
        venue=FuturesOrders(execution_enabled=cfg['execution_enabled'],**adapter_options)
    store=None
    try:
        store=FuturesStore(database,cfg)
        store.acquire_writer()
        return FuturesExecutor(store,venue,cfg)
    except BaseException:
        if store is not None:store.close()
        if hasattr(venue,'close'):venue.close()
        raise
