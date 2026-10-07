"""Reusable paper allocation/position owner. Intention in, native prints in."""
import json
import math
from trading_core.contracts import canonical_json, validate_event, validate_intention
from trading_core.paper_execution import Config
from live_engine.order_manager import PaperOrderManager, save_state, integer, positive


class AccountOwner:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.manager = PaperOrderManager(store)
        self.allocations = {a['id']:a for a in config['allocations']}
        if len(self.allocations) != len(config['allocations']) or not self.allocations:
            raise ValueError('Duplicate/empty allocations')
        self.instruments = {}
        for a in self.allocations.values():
            self.instruments.setdefault(str(a['instrument_id']),[]).append(a['id'])
            self.manager.register(a['id'],a['symbol'],Config(**a['execution']),
                allowed_risks=a['allowed_risks'],contract=a['contract'])
            with store.transaction():
                if not store.get_meta('currency:'+a['id']):
                    store.set_meta('currency:'+a['id'],a['currency'])
                    store.post('capital:'+a['id'],a['id'],a['currency'],'cash','owner_capital',a['execution']['initial_equity'])

    def intention(self, raw, now_ms):
        with self.store.transaction():
            return self._intention(raw, now_ms)

    def _intention(self, raw, now_ms):
        p = validate_intention(raw)
        if (p['environment'],p['venue'],p['product']) != ('server-paper',self.config['venue'],'futures'):
            raise ValueError('Wrong intention environment/venue/product')
        allocation = self.allocations.get(p['allocation_id'])
        if (not allocation or p['account_id'] != p['allocation_id']
                or str(p['instrument_id']) != str(allocation['instrument_id'])
                or p['contract_version'] != allocation['contract']
                or p['model_id'] != allocation['model_id'] or p['model_version'] != allocation['model_version']
                or p['config_hash'] != self.config['strategy_config_hash']
                or p['risk_fraction'] not in allocation['allowed_risks']
                or p['exit_policy'] != 'initial_stop_or_time'):
            raise ValueError('Intention differs from registered allocation contract')
        old = self.store.db.execute('SELECT payload,status FROM requests WHERE intention_id=?',(p['intention_id'],)).fetchone()
        if old:
            if old['payload'] != canonical_json(p): raise ValueError('Conflicting duplicate intention')
            return 'duplicate'
        _, portfolio, execution = self.manager._load(allocation['id'])
        reason = None
        if p['signal_timestamp_ms'] > now_ms: reason='future_signal_at_account_receipt'
        elif now_ms > p['expires_timestamp_ms']: reason='expired_at_account_receipt'
        elif portfolio.equity <= 0: reason='allocation_equity_nonpositive'
        elif self.store.get_meta('blocked:'+str(p['instrument_id'])): reason='execution_gap_requires_review'
        elif self.store.db.execute('SELECT 1 FROM reservations WHERE instrument_id=?',(str(p['instrument_id']),)).fetchone():
            reason='instrument_has_active_owner'
        elif self.store.db.execute('SELECT 1 FROM intents WHERE account=? AND session=?',
                                   (allocation['id'],p['session_id'])).fetchone(): reason='session_already_consumed'
        # Account receipt is a real causal latency stage; never fill before it.
        eligible = max(p['eligible_timestamp_ms'],now_ms)
        if not reason:
            cursor = json.loads(self.store.db.execute('SELECT cursor FROM accounts WHERE id=?',
                                                     (allocation['id'],)).fetchone()[0] or 'null')
            if cursor: eligible=max(eligible,cursor['timestamp_ms']+1)
            if eligible > p['expires_timestamp_ms']: reason='no_causal_entry_time_remaining'
        self.store.db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?)',(p['intention_id'],allocation['id'],
            canonical_json(p),'rejected' if reason else 'pending',reason,now_ms))
        if reason:
            self.store.record(allocation['id'],'intention_rejected',dict(intention_id=p['intention_id'],reason=reason))
            return 'rejected'
        signal=dict(sample_id=p.get('source_sample_id',p['intention_id']),session_day=p['session_id'],
            symbol=allocation['symbol'],direction=p['direction'],strategy=p['model_version'],route=p['model_id'],
            stop=p['initial_stop'],entry_reference=p['entry_reference'],feature_as_of_ms=p['feature_as_of_ms'],
            entry_eligible_timestamp_ms=eligible,entry_deadline_timestamp_ms=p['expires_timestamp_ms'],
            force_exit_timestamp_ms=p['force_exit_timestamp_ms'])
        qty=min(portfolio.equity*p['risk_fraction']/abs(p['entry_reference']-p['initial_stop']),
                portfolio.equity*execution.max_leverage/p['entry_reference'])
        risk=qty*abs(p['entry_reference']-p['initial_stop'])
        margin=qty*p['entry_reference']/execution.max_leverage
        if not math.isfinite(qty) or qty <= 0 or margin > portfolio.equity+1e-9:
            raise ValueError('Invalid capital reservation')
        self.store.db.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
            (allocation['id'],p['intention_id'],str(p['instrument_id']),risk,margin))
        self.manager.submit(allocation['id'],signal,snapshot=dict(feature_as_of_ms=p['feature_as_of_ms'],
            decision_snapshot_ref=p['decision_snapshot_ref']),risk_fraction=p['risk_fraction'])
        self.store.record(allocation['id'],'capital_reserved',dict(intention_id=p['intention_id'],
            risk_amount=risk,margin_amount=margin,eligible_timestamp_ms=eligible))
        return 'pending'

    def event(self, raw, now_ms):
        with self.store.transaction():
            return self._event(raw, now_ms)

    def _event(self, raw, now_ms):
        event=validate_event(raw)
        if (event['venue'],event['product']) != (self.config['venue'],'futures'):
            raise ValueError('Wrong execution venue/product')
        native=str(event['instrument_id'])
        if native not in self.instruments: raise ValueError('Unknown native execution instrument')
        epoch=event.get('source_epoch')
        if not epoch: raise ValueError('Missing execution source epoch')
        old=self.store.get_meta('source_epoch')
        if old and old != epoch: raise ValueError('Execution collector epoch changed; review required')
        self.store.set_meta('source_epoch',epoch)
        if event['event_type']=='coverage' and event['quality'] in ('gap','invalid'):
            self.block(native,event,'collector_coverage_gap')
            return
        if event['event_type']!='trade': return
        if event['revision'] != 0 or event['quality'] not in ('complete','partial'):
            raise ValueError('Unusable execution trade event')
        ticks=event['payload']['trades']
        if not ticks or len(ticks)>500: raise ValueError('Invalid execution batch size')
        if (event['payload']['first_source_sequence'] != ticks[0]['agg_trade_id']
                or event['payload']['last_source_sequence'] != ticks[-1]['agg_trade_id']
                or event['source_sequence'] != ticks[-1]['agg_trade_id']
                or event['event_timestamp_ms'] != ticks[-1]['timestamp_ms']):
            raise ValueError('Execution batch envelope mismatch')
        previous=None
        for t in ticks:
            if (not integer(t.get('agg_trade_id')) or not integer(t.get('timestamp_ms'))
                    or not positive(t.get('price')) or t['timestamp_ms']>now_ms
                    or any(t.get('symbol') != self.allocations[a]['symbol'] for a in self.instruments[native])
                    or (previous and (t['agg_trade_id']<=previous['agg_trade_id']
                                     or t['timestamp_ms']<previous['timestamp_ms']))):
                raise ValueError('Malformed native execution batch')
            previous=t
        self.clock(now_ms)
        for account in self.instruments[native]:
            row=self.store.db.execute('SELECT cursor FROM accounts WHERE id=?',(account,)).fetchone()
            previous=json.loads(row[0]) if row[0] else None
            for t in ticks:
                if previous and t['agg_trade_id']>previous['agg_trade_id']:
                    if self.config['venue']=='binance' and t['agg_trade_id'] != previous['agg_trade_id']+1:
                        self.block(native,event,'native_execution_sequence_gap')
                if not previous or t['agg_trade_id']>previous['agg_trade_id']: previous=t
            # A reset/gap blocks entries, not observation of protective exits.
            self.manager.ticks(account,ticks)
            self.sync_reservation(account)

    def block(self, native, event, reason):
        self.store.set_meta('blocked:'+native,event['event_timestamp_ms'])
        prior=event['payload'].get('previous_cursor') or {}
        uncertain_from=prior.get('timestamp_ms',event['event_timestamp_ms'])
        for account in self.instruments[native]:
            self.store.record(account,'execution_uncertain',dict(event_id=event['event_id'],
                since_ms=event['event_timestamp_ms'],reason=reason))
            # Coverage and prints travel on separate streams. A gap notice may
            # arrive after a close; do not leave that interval labelled verified.
            self.store.db.execute("UPDATE requests SET status='closed_uncertain' WHERE allocation=? AND status='closed' "
                "AND COALESCE(json_extract(payload,'$.source_sample_id'),intention_id) IN "
                "(SELECT json_extract(payload,'$.sample_id') FROM journal WHERE account=? AND kind='trade' "
                "AND json_extract(payload,'$.exit_timestamp_ms')>=? AND json_extract(payload,'$.entry_timestamp_ms')<=?)",
                (account,account,uncertain_from,event['event_timestamp_ms']))
            _,p,cfg=self.manager._load(account)
            if p.signals and not p.position:
                self.store.record(account,'decision',dict(sample_id=p.signals[0]['sample_id'],
                    decision='SKIP',reason='execution_gap_before_entry'))
                p.signals=[]; p.next_signal=0
                self.store.db.execute('UPDATE accounts SET state=? WHERE id=?',(save_state(p,cfg),account))
                self.sync_reservation(account)

    def sync_reservation(self, account):
        r=self.store.db.execute('SELECT * FROM reservations WHERE allocation=?',(account,)).fetchone()
        if not r: return
        _, p, cfg=self.manager._load(account)
        if p.position:
            pos=p.position; qty=pos.get('remaining_quantity',pos['quantity'])
            self.store.db.execute('UPDATE reservations SET risk_amount=?,margin_amount=? WHERE allocation=?',
                (qty*abs(pos['entry_price']-pos['stop']),qty*pos['entry_price']/cfg.max_leverage,account))
            self.store.db.execute("UPDATE requests SET status='open' WHERE intention_id=?",(r['intention_id'],))
        elif not p.signals:
            self.store.db.execute('DELETE FROM reservations WHERE allocation=?',(account,))
            filled=self.store.db.execute("SELECT 1 FROM journal WHERE account=? AND kind='trade' "
                "AND json_extract(payload,'$.sample_id')=(SELECT json_extract(payload,'$.signal.sample_id') FROM intents "
                "WHERE account=? ORDER BY rowid DESC LIMIT 1)",(account,account)).fetchone()
            state = ('closed_uncertain' if self.store.get_meta('blocked:'+r['instrument_id']) else 'closed') if filled else 'entry_skipped'
            self.store.db.execute('UPDATE requests SET status=? WHERE intention_id=?',(state,r['intention_id']))
            self.store.db.execute("UPDATE deadlines SET status='filled' WHERE allocation=? AND intention_id=?",
                                  (account,r['intention_id']))
            due=self.store.db.execute('SELECT deadline_ms,detected_ms FROM deadlines WHERE allocation=? AND intention_id=?',
                                      (account,r['intention_id'])).fetchone()
            if due:
                trade=self.store.db.execute("SELECT payload FROM journal WHERE account=? AND kind='trade' ORDER BY id DESC LIMIT 1",
                                            (account,)).fetchone()
                if trade:
                    t=json.loads(trade[0])
                    self.store.record(account,'exit_deadline_execution',dict(intention_id=r['intention_id'],
                        deadline_ms=due['deadline_ms'],detected_ms=due['detected_ms'],
                        fill_ms=t['exit_timestamp_ms'],delay_ms=max(0,t['exit_timestamp_ms']-due['deadline_ms']),
                        exit_reason=t['exit_reason']))

    def clock(self, now_ms):
        """Wall-clock scheduling persists exit duty, never fabricates a price."""
        with self.store.transaction():
            for account in self.allocations:
                _,p,cfg=self.manager._load(account)
                r=self.store.db.execute('SELECT intention_id FROM reservations WHERE allocation=?',(account,)).fetchone()
                if p.position and r and now_ms>=p.position['force_exit_timestamp_ms']:
                    self.store.db.execute('INSERT OR IGNORE INTO deadlines VALUES (?,?,?,?,?)',
                        (account,r[0],p.position['force_exit_timestamp_ms'],now_ms,'awaiting_next_native_print'))
                elif p.signals and now_ms>p.signals[0]['entry_deadline_timestamp_ms']:
                    self.store.record(account,'decision',dict(sample_id=p.signals[0]['sample_id'],
                        decision='SKIP',reason='entry_deadline_passed_wall_clock'))
                    p.signals=[]; p.next_signal=0
                    self.store.db.execute('UPDATE accounts SET state=? WHERE id=?',(save_state(p,cfg),account))
                    self.sync_reservation(account)
            self.store.set_meta('last_timer_ms',now_ms)

    def status(self):
        accounts=self.manager.status()
        for a in accounts:
            a['cash_balance']=str(self.store.cash(a['account']))
            a['currency']=self.allocations[a['account']]['currency']
            r=self.store.db.execute('SELECT * FROM reservations WHERE allocation=?',(a['account'],)).fetchone()
            a['reservation']=dict(r) if r else None
        return dict(mode='independent_paper_shadow',real_orders=False,venue=self.config['venue'],
            simulation_version='next-native-print-v1',accounts=accounts,
            checkpoints=[dict(r) for r in self.store.db.execute('SELECT * FROM checkpoints')],
            due_exits=[dict(r) for r in self.store.db.execute("SELECT * FROM deadlines WHERE status!='filled'")])
