"""Durable one-allocation USD-M lifecycle; real fills, never public-print fills."""
from datetime import date, datetime, time as clock, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import time
from zoneinfo import ZoneInfo

from trading_core.contracts import canonical_json, validate_intention
from live_engine.adapters.binance_futures_execution import VenueError
from live_engine.adapters.binance_futures_readonly import PreflightError
from live_engine.futures_preflight import number, one, positive, quantity_rules

TERMINAL = {'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED', 'FINISHED'}
STATES = TERMINAL | {'NEW', 'PARTIALLY_FILLED', 'TRIGGERING', 'TRIGGERED'}
NY = ZoneInfo('America/New_York')


def millis(day, hour, minute=0):
    return int(datetime.combine(day, clock(hour, minute), NY).timestamp() * 1000)


def rounded(value, step, *, up=False):
    return (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step


def order_id(session_id, role):
    return 'ggp-' + hashlib.sha256(session_id.encode()).hexdigest()[:20] + '-' + role.lower()


def plan_order(p, spec, quote, *, equity, available, leverage, max_notional):
    """Round down entry to an exact 10% split; never enlarge the risk budget."""
    rules = quantity_rules(spec)
    sign = 1 if p['direction'] == 'long' else -1
    entry = positive(quote['askPrice'] if sign == 1 else quote['bidPrice'])
    stop = rounded(positive(p['initial_stop']), rules['tick_size'], up=sign == -1)
    if stop <= 0 or sign * (entry - stop) <= 0:
        raise ValueError('POC is not behind executable entry')
    equity, available, leverage = positive(equity), number(available), positive(leverage)
    budget = equity * positive(p['risk_fraction'])
    cap = min(equity * 5, positive(max_notional))
    qty = rounded(min(budget / abs(entry - stop), cap / entry, rules['max_qty'],
                      available * leverage / entry), 10 * rules['step'])
    partial = qty / 10
    if qty <= 0 or min(partial, qty - partial) < rules['min_qty'] or qty * entry < rules['min_notional']:
        raise ValueError('Venue quantity/TP split cannot fit the existing risk and margin budget')
    if qty * abs(entry - stop) > budget or qty * entry > cap:
        raise ValueError('Rounded plan exceeds risk/notional budget')
    return dict(entry_reference=str(entry), stop=str(stop), quantity=str(qty), tp1_quantity=str(partial),
                tick_size=str(rules['tick_size']), quantity_step=str(rules['step']),
                min_qty=str(rules['min_qty']), equity=str(equity), risk_budget=str(budget),
                planned_price_risk=str(qty * abs(entry - stop)), notional=str(qty * entry),
                force_exit_ms=p['force_exit_timestamp_ms'], direction=p['direction'])


class FuturesExecutor:
    def __init__(self, store, venue, config):
        self.s, self.v, self.c = store, venue, config
        self.symbol = config['symbol']

    def block(self, reason):
        with self.s.transaction():
            self.s.set_meta('entries_blocked', reason)
            self.s.audit('attention_required', dict(reason=reason))

    def offer(self, raw, now_ms):
        p = validate_intention(raw)
        if (p['environment'], p['venue'], p['product'], p['instrument_id'], p['contract_version'],
            p['model_id'], p['model_version'], p['account_id'], p['allocation_id']) != (
                'server-live', 'binance', 'futures', self.symbol, 'TF-PRENY-ETH-001',
                'trend_following_preny_profile_15m', 'trend_following_preny_profile_15m_v1',
                self.c['allocation_id'], self.c['allocation_id']):
            raise ValueError('Wrong live intention identity')
        day = date.fromisoformat(p['session_id'])
        if (not millis(day, 9, 30) <= p['feature_as_of_ms'] <= p['expires_timestamp_ms'] <= millis(day, 12)
                or p['force_exit_timestamp_ms'] != millis(day + timedelta(days=1), 8, 59)
                or p['exit_policy'] != 'initial_stop_or_time' or p['risk_fraction'] not in (.0025, .005)
                or p['feature_as_of_ms'] % 900000):
            raise ValueError('Intention timing/risk/exit differs from frozen contract')
        old = self.s.db.execute('SELECT * FROM sessions WHERE session=?', (p['session_id'],)).fetchone()
        if old:
            if old['intention'] != canonical_json(p): raise ValueError('Conflicting session intention')
            return 'duplicate'
        reason = None
        state = 'PENDING'
        if not self.v.armed:
            state, reason = 'DISARMED', 'real_execution_off'
        elif not self.c['accept_new_entries']:
            state, reason = 'SKIPPED', 'new_entries_paused'
        elif p['signal_timestamp_ms'] > now_ms or now_ms > p['expires_timestamp_ms']:
            state, reason = 'SKIPPED', 'future_or_expired_intention'
        elif self.s.active() or self.s.meta('entries_blocked'):
            state, reason = 'SKIPPED', 'active_position_or_review_block'
        with self.s.transaction():
            self.s.db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)',
                              (p['session_id'], p['intention_id'], canonical_json(p), None, state, reason))
            self.s.audit('intention_received', dict(id=p['intention_id'], state=state, reason=reason,
                         feature_as_of_ms=p['feature_as_of_ms'], signal_timestamp_ms=p['signal_timestamp_ms'],
                         eligible_timestamp_ms=p['eligible_timestamp_ms'], poll_started_at_ms=now_ms))
        return state

    def position(self):
        rows = self.v.read('/fapi/v3/positionRisk')
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError('Invalid positions response')
        found = [r for r in rows if r['symbol'] == self.symbol and number(r['positionAmt'], signed=True)]
        if len(found) > 1 or any(r.get('positionSide') != 'BOTH' for r in found):
            raise ValueError('Unexpected hedge/multiple ETH position')
        other = any(r['symbol'] != self.symbol and number(r['positionAmt'], signed=True) for r in rows)
        return (number(found[0]['positionAmt'], signed=True) if found else Decimal(0)), other

    def wallet(self):
        account = self.v.read('/fapi/v3/account')
        asset = one(account.get('assets'), 'asset', 'USDC')
        return number(asset['walletBalance'], signed=True), number(asset['availableBalance'])

    def entry_guard(self, now_ms):
        cfg = self.v.read('/fapi/v1/accountConfig')
        if cfg.get('canTrade') is not True or cfg.get('dualSidePosition') is not False or cfg.get('multiAssetsMargin') is not False:
            raise ValueError('Account must permit trading in one-way/single-asset mode')
        amount, other = self.position()
        if amount or other or self.v.read('/fapi/v1/openOrders') or self.v.read('/fapi/v1/openAlgoOrders'):
            raise ValueError('Dedicated Futures account is not flat/order-free')
        self.v.load_credentials()
        key_hash = hashlib.sha256(self.v._key.encode()).hexdigest()
        if self.s.meta('key_hash', key_hash) != key_hash:
            raise ValueError('API identity changed; reviewed key rotation required')
        wallet, available = self.wallet()
        if self.s.meta('initial_wallet') is None:
            if available < positive(self.c['initial_equity']):
                raise ValueError('Insufficient USDC for initial allocation')
            with self.s.transaction():
                self.s.set_meta('key_hash', key_hash)
                self.s.set_meta('initial_wallet', wallet)
                self.s.set_meta('funding_start_ms', now_ms)
        self.accounting(now_ms)
        settings = one(self.v.read('/fapi/v1/symbolConfig', symbol=self.symbol), 'symbol', self.symbol)
        if settings.get('marginType') not in ('CROSSED', 'ISOLATED') or type(settings.get('leverage')) is not int:
            raise ValueError('Unknown margin/leverage configuration')
        spec = one(self.v.get('/fapi/v1/exchangeInfo').get('symbols'), 'symbol', self.symbol)
        if (spec.get('status'), spec.get('contractType'), spec.get('marginAsset')) != ('TRADING', 'PERPETUAL', 'USDC'):
            raise ValueError('Unavailable or changed contract')
        if any(r.get('filterType') == 'POSITION_RISK_CONTROL' and r.get('positionControlSide') != 'NONE' for r in spec['filters']):
            raise ValueError('Exchange position restriction active; review before entry')
        return spec, available, settings

    def quote(self, now_ms):
        q = self.v.get('/fapi/v1/ticker/bookTicker', symbol=self.symbol)
        observed = int(time.time() * 1000) + (self.v.offset or 0)
        if q.get('symbol') != self.symbol or type(q.get('time')) is not int or not -1000 <= observed - q['time'] <= 10000:
            raise ValueError('Stale/wrong/future quote')
        if positive(q['bidPrice']) > positive(q['askPrice']): raise ValueError('Crossed quote')
        return q

    def _prepare(self, session, role, params):
        cid = params.get('clientAlgoId', params.get('newClientOrderId'))
        old = self.s.db.execute('SELECT * FROM orders WHERE client_id=?', (cid,)).fetchone()
        if old and (old['params'] != canonical_json(params) or old['role'] != role):
            raise ValueError('Conflicting durable order template')
        if not old:
            with self.s.transaction():
                self.s.db.execute('INSERT INTO orders(client_id,session_id,role,params,state) VALUES (?,?,?,?,?)',
                                  (cid, session, role, canonical_json(params), 'PREPARED'))
                self.s.audit('order_prepared', dict(client_id=cid, session=session, role=role))
        return cid

    def _validate_response(self, r, row):
        p = json.loads(row['params'])
        stop = row['role'] == 'STOP'
        if not isinstance(r, dict) or (r.get('symbol'), r.get('side'), r.get('positionSide')) != (
                self.symbol, p['side'], 'BOTH'):
            raise ValueError('Exchange order identity/side mismatch')
        if r.get('clientAlgoId' if stop else 'clientOrderId') != row['client_id']:
            raise ValueError('Exchange client ID mismatch')
        state = r.get('algoStatus' if stop else 'status')
        if state not in STATES: raise ValueError('Unknown native order status')
        native_id = str(r.get('algoId' if stop else 'orderId', ''))
        if not native_id.isdigit() or int(native_id) <= 0: raise ValueError('Missing native order ID')
        if stop:
            if (r.get('orderType'), r.get('workingType'), r.get('closePosition')) != ('STOP_MARKET', 'CONTRACT_PRICE', True):
                raise ValueError('Native stop protection differs from request')
            if r.get('priceProtect') is not False: raise ValueError('Unreviewed native price-protection delay')
            if number(r['triggerPrice']) != number(p['triggerPrice']): raise ValueError('Native stop price mismatch')
        else:
            if r.get('type') != p['type'] or number(r['origQty']) != number(p['quantity']):
                raise ValueError('Native order type/quantity mismatch')
            if row['role'] != 'ENTRY' and r.get('reduceOnly') is not True:
                raise ValueError('Native exit is not reduce-only')
            if row['role'] == 'TP1' and (number(r['price']) != number(p['price']) or r.get('timeInForce') != 'GTC'):
                raise ValueError('Native TP price/TIF mismatch')
            if number(r['executedQty']) > number(p['quantity']): raise ValueError('Overfilled order')
        return state

    def _accept(self, row, response, *, source='query', request_duration_ms=None):
        state = self._validate_response(response, row)
        with self.s.transaction():
            prior = json.loads(row['response']) if row['response'] else None
            if prior and row['role'] != 'STOP' and number(response['executedQty']) < number(prior['executedQty']):
                raise ValueError('Native cumulative execution moved backward')
            self.s.db.execute('UPDATE orders SET state=?,response=?,error=NULL WHERE client_id=?',
                              (state, canonical_json(response), row['client_id']))
            changed = not prior or state != row['state'] or any(
                response.get(k) != prior.get(k) for k in ('executedQty', 'actualOrderId'))
            if source == 'submit' or changed:
                self.s.audit('order_observed', dict(client_id=row['client_id'], role=row['role'], state=state,
                             observation_source=source, request_duration_ms=request_duration_ms,
                             server_offset_ms=self.v.offset,
                             native_times={k: response[k] for k in ('time', 'updateTime', 'createTime', 'triggerTime', 'workingTime') if k in response},
                             executed_quantity=response.get('executedQty')))

    def drive(self, cid):
        row = self.s.db.execute('SELECT * FROM orders WHERE client_id=?', (cid,)).fetchone()
        if row['state'] == 'PREPARED':
            if row['role'] == 'ENTRY':
                source = self.s.db.execute('SELECT intention FROM sessions WHERE session=?', (row['session_id'],)).fetchone()
                deadline = json.loads(source[0])['expires_timestamp_ms']
                if int(time.time()*1000) > deadline:
                    with self.s.transaction():
                        self.s.db.execute("UPDATE orders SET state='REJECTED',error='entry_expired_before_send' WHERE client_id=?", (cid,))
                    return self.s.db.execute('SELECT * FROM orders WHERE client_id=?', (cid,)).fetchone()
            # A crash after this commit is UNKNOWN, even if the HTTP call never began.
            with self.s.transaction():
                self.s.db.execute("UPDATE orders SET state='SENDING' WHERE client_id=?", (cid,))
                self.s.audit('order_send_started', dict(client_id=cid, role=row['role'], server_offset_ms=self.v.offset))
            started = time.perf_counter_ns()
            try:
                response = self.v.submit(row['role'], json.loads(row['params']))
                duration = (time.perf_counter_ns() - started) / 1000000
                self._accept(row, response, source='submit', request_duration_ms=duration)
            except VenueError as e:
                with self.s.transaction():
                    self.s.db.execute('UPDATE orders SET state=?,error=? WHERE client_id=?',
                                      ('UNKNOWN' if e.uncertain else 'REJECTED', str(e), cid))
                    self.s.audit('order_submit_error', dict(client_id=cid, role=row['role'],
                                 request_duration_ms=(time.perf_counter_ns()-started)/1000000,
                                 http_status=e.status, venue_code=e.code, uncertain=e.uncertain,
                                 server_offset_ms=self.v.offset))
            except (ValueError, TypeError, KeyError):
                with self.s.transaction():
                    self.s.db.execute("UPDATE orders SET state='UNKNOWN',error='invalid_submit_response' WHERE client_id=?", (cid,))
                    self.s.audit('order_submit_error', dict(client_id=cid, role=row['role'],
                                 request_duration_ms=(time.perf_counter_ns()-started)/1000000,
                                 uncertain=True, error='invalid_submit_response', server_offset_ms=self.v.offset))
        else:
            # Query, never re-POST SENDING/UNKNOWN/ACKed orders.
            try:
                started = time.perf_counter_ns()
                r = self.v.query(cid, stop=row['role'] == 'STOP', symbol=self.symbol)
                if r is not None:
                    self._accept(row, r, request_duration_ms=(time.perf_counter_ns()-started)/1000000)
                elif row['state'] not in TERMINAL:
                    with self.s.transaction():
                        self.s.db.execute("UPDATE orders SET state='UNKNOWN',error='native_order_not_found' WHERE client_id=?", (cid,))
            except (VenueError, PreflightError) as e:
                with self.s.transaction(): self.s.db.execute('UPDATE orders SET error=? WHERE client_id=?', (str(e), cid))
            except (ValueError, TypeError, KeyError):
                with self.s.transaction():
                    self.s.db.execute("UPDATE orders SET state='UNKNOWN',error='invalid_query_response' WHERE client_id=?", (cid,))
        return self.s.db.execute('SELECT * FROM orders WHERE client_id=?', (cid,)).fetchone()

    def cancel(self, row):
        if row['state'] == 'PREPARED':
            # PREPARED proves no HTTP submission began. Never drive it while canceling.
            with self.s.transaction():
                self.s.db.execute("UPDATE orders SET state='CANCELED',cancel_pending=0 WHERE client_id=?", (row['client_id'],))
            return True
        if row['state'] in TERMINAL:
            if row['cancel_pending']:
                with self.s.transaction(): self.s.db.execute('UPDATE orders SET cancel_pending=0 WHERE client_id=?', (row['client_id'],))
            return True
        with self.s.transaction(): self.s.db.execute('UPDATE orders SET cancel_pending=1 WHERE client_id=?', (row['client_id'],))
        try:
            self.v.cancel(row['client_id'], stop=row['role'] == 'STOP', symbol=self.symbol)
        except VenueError:
            pass  # Only a later query can confirm cancellation or a competing fill.
        current = self.drive(row['client_id'])
        if current['state'] in TERMINAL:
            with self.s.transaction(): self.s.db.execute('UPDATE orders SET cancel_pending=0 WHERE client_id=?', (row['client_id'],))
            return True
        return False

    def sync_fills(self, row):
        if not row['response']: return
        native = json.loads(row['response'])
        oid = native.get('actualOrderId') if row['role'] == 'STOP' else native.get('orderId')
        if not oid: return
        rows = self.v.read('/fapi/v1/userTrades', symbol=self.symbol, orderId=oid, limit=1000)
        if not isinstance(rows, list) or len(rows) >= 1000:
            raise ValueError('Fill pagination limit/schema requires review; never silently truncate')
        side = json.loads(row['params'])['side']
        for t in rows:
            if (t.get('symbol'), str(t.get('orderId')), t.get('side')) != (self.symbol, str(oid), side) or type(t.get('maker')) is not bool:
                raise ValueError('Fill attribution mismatch')
            qty, price, pnl, fee = positive(t['qty']), positive(t['price']), number(t['realizedPnl'], signed=True), number(t['commission'], signed=True)
            identity = self.symbol + ':' + str(t['id'])
            encoded = canonical_json(t)
            with self.s.transaction():
                old = self.s.db.execute('SELECT payload FROM fills WHERE id=?', (identity,)).fetchone()
                if old and old[0] != encoded: raise ValueError('Conflicting fill redelivery')
                inserted = self.s.db.execute('INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)',
                                  (identity, str(oid), row['session_id'], str(qty), str(price), str(pnl), str(fee),
                                   t['commissionAsset'], int(t['maker']), encoded))
                if inserted.rowcount:
                    self.s.audit('fill_first_observed', dict(fill_id=identity, client_id=row['client_id'],
                                 role=row['role'], native_fill_time_ms=t.get('time'), server_offset_ms=self.v.offset))

    def accounting(self, now_ms):
        if self.s.meta('initial_wallet') is None: return
        # Overlap a day so delayed funding records are not lost behind a cursor.
        start = max(int(self.s.meta('funding_start_ms')), int(self.s.meta('funding_scan_ms', now_ms)) - 86400000)
        while start <= now_ms:
            end = min(start + 7 * 86400000 - 1, now_ms)
            rows = self.v.read('/fapi/v1/income', symbol=self.symbol, incomeType='FUNDING_FEE', startTime=start, endTime=end, limit=1000)
            if not isinstance(rows, list) or len(rows) >= 1000: raise ValueError('Funding pagination/schema requires review')
            with self.s.transaction():
                for r in rows:
                    if (r.get('symbol'), r.get('asset'), r.get('incomeType')) != (self.symbol, 'USDC', 'FUNDING_FEE'):
                        raise ValueError('Unexpected funding asset/type')
                    identity = 'funding:' + str(r['tranId'])
                    payload = canonical_json(r)
                    old = self.s.db.execute('SELECT payload FROM funding WHERE id=?', (identity,)).fetchone()
                    if old and old[0] != payload: raise ValueError('Conflicting funding record')
                    self.s.db.execute('INSERT OR IGNORE INTO funding VALUES (?,?,?)', (identity, str(number(r['income'], signed=True)), payload))
            start = end + 1
        wallet, _ = self.wallet()
        expected = number(self.s.meta('initial_wallet')) + self.s.equity(self.c['initial_equity']) - positive(self.c['initial_equity'])
        if abs(wallet - expected) > Decimal('.00000001'):
            raise ValueError('Wallet/fill/funding mismatch; wait/reconcile, no automatic capital adjustment')
        with self.s.transaction(): self.s.set_meta('funding_scan_ms', now_ms)

    def _market(self, session, role, quantity, *, close=False, suffix=''):
        p = json.loads(session['intention'])
        side = 'BUY' if (p['direction'] == 'long') != close else 'SELL'
        params = dict(symbol=self.symbol, side=side, positionSide='BOTH', type='MARKET', quantity=str(quantity),
                      newClientOrderId=order_id(session['id'], role + suffix), newOrderRespType='RESULT')
        if close: params['reduceOnly'] = 'true'
        return self._prepare(session['session'], role, params)

    def _protect(self, session, plan):
        params = dict(algoType='CONDITIONAL', symbol=self.symbol, side='SELL' if plan['direction'] == 'long' else 'BUY',
                      positionSide='BOTH', type='STOP_MARKET', triggerPrice=plan['stop'], workingType='CONTRACT_PRICE',
                      closePosition='true', priceProtect='false', clientAlgoId=order_id(session['id'], 'stop'))
        return self.drive(self._prepare(session['session'], 'STOP', params))

    def _exit(self, session, quantity, reason):
        with self.s.transaction(): self.s.session_state(session['session'], 'EXIT_PENDING', reason)
        # Even if cancellation races a fill, reduceOnly prevents an exit reversing the position.
        for row in self.s.db.execute("SELECT * FROM orders WHERE session_id=? AND role IN ('ENTRY','TP1')", (session['session'],)).fetchall():
            self.cancel(row)
        quantity, _ = self.position()
        if not quantity: return
        previous = self.s.db.execute("SELECT * FROM orders WHERE session_id=? AND role='CLOSE' ORDER BY rowid", (session['session'],)).fetchall()
        if previous and previous[-1]['state'] not in TERMINAL:
            latest = self.drive(previous[-1]['client_id'])
            if latest['state'] not in TERMINAL: return
        if len(previous) >= 3:
            self.block('close_attempts_exhausted; native stop retained; operator required')
            return
        self.drive(self._market(session, 'CLOSE', abs(quantity), close=True, suffix=str(len(previous))))

    def _cleanup(self, session, now_ms):
        clear = True
        for row in self.s.db.execute('SELECT * FROM orders WHERE session_id=?', (session['session'],)).fetchall():
            if row['state'] == 'PREPARED':
                # A prepared exit was never sent and there is no position left.
                with self.s.transaction(): self.s.db.execute("UPDATE orders SET state='CANCELED' WHERE client_id=?", (row['client_id'],))
            else:
                if not self.cancel(row): clear = False
                self.sync_fills(self.s.db.execute('SELECT * FROM orders WHERE client_id=?', (row['client_id'],)).fetchone())
        if not clear: return
        for row in self.s.db.execute('SELECT * FROM orders WHERE session_id=?', (session['session'],)):
            if not row['response']: continue
            native = json.loads(row['response'])
            oid = native.get('actualOrderId') if row['role'] == 'STOP' else native.get('orderId')
            if not oid: continue
            if row['role'] == 'STOP':
                native = self.v.read('/fapi/v1/order', symbol=self.symbol, orderId=oid)
                if native.get('symbol') != self.symbol or native.get('side') != json.loads(row['params'])['side']:
                    raise ValueError('Conditional child-order attribution mismatch')
            expected = number(native['executedQty'])
            found = sum((number(r[0]) for r in self.s.db.execute('SELECT quantity FROM fills WHERE order_id=?', (str(oid),))), Decimal(0))
            if found != expected: raise ValueError('Native fills not complete yet; no closed-equity finalization')
        signed = Decimal(0)
        for r in self.s.db.execute('SELECT payload FROM fills WHERE session_id=?', (session['session'],)):
            fill = json.loads(r[0]); signed += number(fill['qty']) * (1 if fill['side'] == 'BUY' else -1)
        if signed: raise ValueError('Flat venue but unbalanced attributed fills; keep reconciliation pending')
        self.accounting(now_ms)
        entered = self.s.db.execute("SELECT response FROM orders WHERE session_id=? AND role='ENTRY'", (session['session'],)).fetchone()
        filled = number(json.loads(entered[0])['executedQty']) if entered and entered[0] else Decimal(0)
        with self.s.transaction():
            self.s.session_state(session['session'], 'CLOSED' if filled else 'SKIPPED', session['reason'] or ('entry_not_filled' if not filled else None))

    def cycle(self, now_ms, *, new_entries_ok=True):
        if self.v.armed and self.s.meta('key_hash'):
            self.v.load_credentials()
            if hashlib.sha256(self.v._key.encode()).hexdigest() != self.s.meta('key_hash'):
                raise ValueError('API key changed during lifecycle; reviewed recovery required')
        try:
            self._cycle(now_ms, new_entries_ok=new_entries_ok)
        finally:
            # Do not let fee/history reporting delay native protection or time exit.
            if self.v.armed:
                active = self.s.active()
                if active:
                    for r in self.s.db.execute('SELECT * FROM orders WHERE session_id=?', (active['session'],)).fetchall():
                        self.sync_fills(r)

    def _cycle(self, now_ms, *, new_entries_ok=True):
        session = self.s.active()
        if session is None:
            with self.s.transaction(): self.s.set_meta('last_cycle_ms', now_ms)
            return
        if not self.v.armed:
            raise ValueError('Live lifecycle present while adapter disarmed; reconcile before changing execution mode')
        p = json.loads(session['intention'])
        if session['state'] == 'PENDING':
            if now_ms < p['eligible_timestamp_ms']: return
            if now_ms > p['expires_timestamp_ms'] or self.s.meta('entries_blocked') or not new_entries_ok:
                with self.s.transaction(): self.s.session_state(session['session'], 'SKIPPED', 'expired_or_review_blocked')
                return
            spec, available, settings = self.entry_guard(now_ms)
            quote = self.quote(now_ms)
            plan = plan_order(p, spec, quote, equity=self.s.equity(self.c['initial_equity']), available=available,
                              leverage=settings['leverage'], max_notional=settings['maxNotionalValue'])
            plan['entry_quote'] = dict(bid=quote['bidPrice'], ask=quote['askPrice'], native_time_ms=quote['time'],
                                      local_observed_at_ms=int(time.time()*1000), server_offset_ms=self.v.offset)
            with self.s.transaction():
                self.s.db.execute("UPDATE sessions SET plan=?,state='ENTRY_PENDING' WHERE session=?", (canonical_json(plan), session['session']))
                cid = self._market(session, 'ENTRY', number(plan['quantity']))
            session = self.s.active()
            self.drive(cid)
        else:
            plan = json.loads(session['plan'])
        for row in self.s.db.execute('SELECT * FROM orders WHERE session_id=?', (session['session'],)).fetchall():
            # Refresh exits/recovery first; PREPARED orders are submitted only by their owning lifecycle stage.
            if row['state'] == 'PREPARED' and row['role'] == 'ENTRY' and not new_entries_ok:
                with self.s.transaction():
                    self.s.db.execute("UPDATE orders SET state='REJECTED',error='source_stale_before_send' WHERE client_id=?", (row['client_id'],))
            elif row['state'] != 'PREPARED' or row['role'] in ('ENTRY', 'CLOSE'):
                self.drive(row['client_id'])
        entry = self.s.db.execute("SELECT * FROM orders WHERE session_id=? AND role='ENTRY'", (session['session'],)).fetchone()
        amount, other = self.position()
        sign = 1 if plan['direction'] == 'long' else -1
        if other: self.block('unexpected_other_instrument_position')
        if amount and (amount * sign < 0 or abs(amount) > number(plan['quantity'])):
            self.block('unexpected_position_direction_or_size; operator required')
            return
        if not amount:
            if entry and entry['state'] not in TERMINAL and now_ms > p['expires_timestamp_ms']:
                self.cancel(entry)
                entry = self.s.db.execute("SELECT * FROM orders WHERE session_id=? AND role='ENTRY'", (session['session'],)).fetchone()
            if entry and entry['state'] in TERMINAL:
                self._cleanup(self.s.active(), now_ms)
            return
        stop = self._protect(session, plan)
        if stop['state'] not in ('NEW', 'TRIGGERING', 'TRIGGERED'):
            self._exit(session, amount, 'protective_stop_not_confirmed')
            return
        if session['state'] == 'EXIT_PENDING' or now_ms >= plan['force_exit_ms']:
            self._exit(session, amount, session['reason'] or 'time_exit')
            return
        if entry['state'] in ('SENDING', 'UNKNOWN', 'REJECTED'):
            self._exit(session, amount, 'entry_status_uncertain')
            return
        if entry['state'] not in TERMINAL:
            if now_ms > p['expires_timestamp_ms']: self.cancel(entry)
            return  # Stop closePosition already protects any partial entry.
        response = json.loads(entry['response']) if entry['response'] else {}
        filled, price = number(response.get('executedQty', '0')), positive(response.get('avgPrice', '0'))
        split_grid = 10 * number(plan['quantity_step'])
        if not filled or filled % split_grid or filled / 10 < number(plan['min_qty']) or sign * (price - number(plan['stop'])) <= 0:
            self._exit(session, amount, 'partial_fill_or_price_cannot_meet_contract')
            return
        plan.update(actual_entry_price=str(price), actual_entry_quantity=str(filled),
                    actual_initial_price_risk=str(filled * abs(price-number(plan['stop']))),
                    entry_slippage_bps=str(sign * (price/number(plan['entry_reference']) - 1) * 10000))
        with self.s.transaction():
            self.s.db.execute('UPDATE sessions SET plan=? WHERE session=?', (canonical_json(plan), session['session']))
        target = rounded(price + sign * abs(price - number(plan['stop'])), number(plan['tick_size']), up=sign == 1)
        params = dict(symbol=self.symbol, side='SELL' if sign == 1 else 'BUY', positionSide='BOTH', type='LIMIT',
                      quantity=str(filled / 10), price=str(target), timeInForce='GTC', reduceOnly='true',
                      newClientOrderId=order_id(session['id'], 'tp1'))
        tp = self.drive(self._prepare(session['session'], 'TP1', params))
        if tp['state'] == 'REJECTED': self.block('TP1_rejected; original stop/time exit retained')
        with self.s.transaction():
            self.s.session_state(session['session'], 'OPEN')
            self.s.set_meta('last_cycle_ms', now_ms)
