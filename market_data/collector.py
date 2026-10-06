"""Atomic normalization output, existing aggregation, coverage and outbox."""
from datetime import datetime, timedelta
import json
import re

from trading_core.contracts import make_event
from trading_core.profiles import value_area
from trading_core.session import NY, clock_ms
from .aggregation import MarketData, MINUTE

FEATURE_VERSION = 'market-data-v1'


class Collector:
    def __init__(self, store, instruments):
        self.store = store
        self.venue = store.get_meta('venue')
        self.instruments = instruments
        if not instruments or any(not re.fullmatch(r'[A-Za-z0-9_-]+', str(x)) for x in instruments.values()):
            raise ValueError('Unsafe/empty native instrument registry')
        self.market = MarketData(store)  # no execution manager or evaluator
        self.received_ms = None

    @staticmethod
    def profile_key(stamp):
        local = datetime.fromtimestamp(stamp/1000,NY)
        return str(local.date()) if 1 <= local.hour < 9 else None

    def emit(self, symbol, kind, timestamp, payload, *, quality='complete', source_sequence=None):
        native = str(self.instruments[symbol])
        received = self.received_ms
        if timestamp > received:
            raise ValueError('Future source event: synchronize collector clock')
        event = make_event(venue=self.venue,product='futures',instrument_id=native,
            source='public_instrument_registry' if kind=='instrument' else 'native_aggregate_trades',
            event_type=kind,event_timestamp_ms=timestamp,
            available_at_ms=received,received_timestamp_ms=received,
            stream_sequence=self.store.next_sequence(),source_sequence=source_sequence,
            source_epoch=self.store.get_meta('source_epoch'),
            feature_version=FEATURE_VERSION,quality=quality,payload=payload)
        channel = 'exec' if kind=='trade' else 'md'
        subject = f'{channel}.v1.{self.venue}.futures.{native}.{kind}'
        self.store.queue(subject,event)
        return event

    def minute(self, symbol, bar, state):
        self.emit(symbol,'completed_bar',bar['timestamp_ms']+MINUTE,
            dict(**bar,open_timestamp_ms=bar['timestamp_ms'],close_timestamp_ms=bar['timestamp_ms']+MINUTE,
                 source='aggregate_trades',coverage_start_ms=state['coverage_start_ms']),
            source_sequence=f"minute:{bar['timestamp_ms']}")

    def boundary(self, symbol, end, state):
        local = datetime.fromtimestamp(end/1000,NY)
        if 9 <= local.hour < 12 or (local.hour == 12 and local.minute == 0):
            if local.hour == 9 and local.minute == 0:
                self.freeze_profile(symbol, end, state)
            # Sparse markets may have no trade in the last minute of a quarter.
            # Publish the same source-confirmed boundary as the legacy worker,
            # after all earlier bars/profile, never from wall-clock alone.
            self.emit(symbol, 'evaluation_boundary', end, dict(as_of_ms=end,
                coverage_start_ms=state['coverage_start_ms'],
                complete_from_ms=state['complete_from_ms'], next_event_ms=state['next_event_ms']),
                source_sequence=f'boundary:{end}')

    def freeze_profile(self, symbol, end, state):
        local = datetime.fromtimestamp(end/1000,NY)
        if local.hour != 9 or local.minute != 0:
            return
        day = local.date()
        if state['coverage_start_ms'] > clock_ms(day,1):
            self.emit(symbol,'coverage',end,dict(reason='incomplete_requested_profile',
                profile_window='pre_ny',coverage_start_ms=state['coverage_start_ms']),quality='partial')
            return
        prices = dict(self.store.db.execute('SELECT price,quantity FROM profile_prices WHERE symbol=? AND session=? ORDER BY price',
                                            (symbol,str(day))))
        if not prices:
            return
        profile = dict(session_day=str(day),profile_window='pre_ny',
            profile_start_timestamp_ms=clock_ms(day,1),profile_end_timestamp_ms=end,**value_area(prices))
        self.store.db.execute('INSERT OR IGNORE INTO profiles VALUES (?,?,?)',(symbol,str(day),json.dumps(profile)))
        self.emit(symbol,'profile',end,profile,source_sequence=f'profile:pre_ny:{day}')

    def consume(self, symbol, ticks, received_ms):
        if not ticks:
            return
        if symbol not in self.instruments:
            raise ValueError('Unregistered market')
        if len(ticks) > 500:
            raise ValueError('Batch exceeds bounded transport size')
        self.received_ms = received_ms
        state = self.market.state(symbol)
        cursor = state['cursor'] if state else None
        fresh = []
        last_id = cursor['agg_trade_id'] if cursor else -1
        for tick in ticks:
            if tick['agg_trade_id'] > last_id:
                fresh.append(tick)
                last_id = tick['agg_trade_id']
        with self.store.transaction():
            self.market.consume(symbol,ticks,profile_key=self.profile_key,on_boundary=self.boundary,
                on_minute=self.minute,consecutive_ids=self.venue=='binance')
            if fresh:
                self.emit(symbol,'trade',fresh[-1]['timestamp_ms'],dict(trades=fresh,
                    first_source_sequence=fresh[0]['agg_trade_id'],last_source_sequence=fresh[-1]['agg_trade_id']),
                    quality='partial' if state is None else 'complete',source_sequence=fresh[-1]['agg_trade_id'])
            self.store.set_meta('last_loop_ms',received_ms)

    def gap(self, symbol, received_ms, reason):
        self.received_ms = received_ms
        with self.store.transaction():
            prior = self.market.state(symbol)
            self.emit(symbol,'coverage',received_ms,dict(reason=reason,
                previous_cursor=prior['cursor'] if prior else None,action='reset_coverage'),quality='gap')
            self.market.reset_coverage(symbol)
            day = str(datetime.fromtimestamp(received_ms/1000,NY).date())
            self.store.db.execute('DELETE FROM profile_prices WHERE symbol=? AND session>=?',(symbol,day))

    def prune(self, now_ms):
        day = datetime.fromtimestamp(now_ms/1000,NY).date()
        with self.store.transaction():
            self.store.db.execute('DELETE FROM minutes WHERE timestamp_ms<?',(now_ms-45*86_400_000,))
            self.store.db.execute('DELETE FROM profile_prices WHERE session<?',(str(day-timedelta(days=2)),))
            self.store.db.execute('DELETE FROM profiles WHERE session<?',(str(day-timedelta(days=45)),))
