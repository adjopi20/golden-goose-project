"""Research model/execution lifecycle shared across strategy families.

Models own setup state and geometry. Portfolio is authoritative for executed
quantity/equity; model positions mirror accepted setup identity, not cash.
No transport, deployment, strategy formula or alternative fill engine here.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import math
import sys

from utils.engine.adapters.paper_execution import Config, Portfolio, on_tick, take_partial, validate_config

VERSION = 'research_lifecycle_v1'


def validate_model(model):
    names = ('submodels', 'pending', 'positions', 'position_blocks', 'records', 'accept_fill', 'record_exit')
    if any(not hasattr(model, name) for name in names):
        raise ValueError('Unsupported lifecycle model: missing state/fill/exit interface')
    keys = tuple(model.submodels)
    if not keys or len(set(keys)) != len(keys) or model.positions or model.pending:
        raise ValueError('Require unique submodels and fresh empty model state; resume is unsupported')
    return keys


def timing(signal, decision_ms):
    fields = ('feature_as_of_ms', 'feature_available_at_ms', 'decision_timestamp_ms', 'entry_eligible_timestamp_ms')
    if any(type(signal.get(k)) is not int or signal[k] < 0 for k in fields):
        raise ValueError('Missing integer feature/availability/decision/eligibility timing')
    a, b, c, d = (signal[k] for k in fields)
    if not a <= b <= c <= d or c > decision_ms:
        raise ValueError('Signal availability/decision timing is not causal')


class Session:
    def __init__(self, model, config, *, timed_scale_out=None, one_entry_per_utc_day=False, risk_manager=None):
        validate_config(config)
        self.model, self.config = model, config
        self.risk_manager = risk_manager
        if risk_manager is not None and not callable(risk_manager):
            raise ValueError("Risk manager requires a callable")
        self.timed_scale_out = timed_scale_out
        self.one_entry_per_utc_day = one_entry_per_utc_day
        self.last_entry_day = {}
        if timed_scale_out is not None:
            partial_ms, final_ms, fraction = timed_scale_out
            if (type(partial_ms) is not int or type(final_ms) is not int
                    or not 0 < partial_ms < final_ms or not 0 < fraction < 1
                    or config.tp1_r is not None):
                raise ValueError("Invalid timed scale-out")
            self.partial_config = replace(config, tp1_fraction=fraction)
        keys = validate_model(model)
        self.portfolios = {k: Portfolio(str(k), [], config.initial_equity, config=config) for k in keys}
        for p in self.portfolios.values():
            p.equity_curve.append(dict(timestamp_ms=None, equity=config.initial_equity))
        self.entries, self.blocks = {}, []
        self.last_order = None
        self.event_counts = {}
        self.insolvent = set()

    def notify(self, kind, key, payload):
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1
        callback = getattr(self.model, 'on_execution', None)
        if callback is not None:
            callback(deepcopy(dict(type=kind, submodel_key=key, **payload)))

    def decide(self, callback, decision_ms):
        if type(decision_ms) is not int or (self.last_order and decision_ms < self.last_order[0]):
            raise ValueError('Decision precedes consumed execution data')
        self.check_state()
        for key,signal in tuple(self.model.pending.items()):
            if ((signal.get('expires_at_ms') is not None and decision_ms >= signal['expires_at_ms'])
                    or (signal.get('entry_deadline_timestamp_ms') is not None
                        and decision_ms > signal['entry_deadline_timestamp_ms'])):
                self.cancel_pending(key,decision_ms,'expired_before_next_decision')
        previous=deepcopy(self.model.pending)
        callback()
        for key,signal in previous.items():
            current=self.model.pending.get(key)
            if current is None or current['setup_id'] != signal['setup_id']:
                self.notify('entry_rejected',key,dict(timestamp_ms=decision_ms,reason='model_cancelled_or_replaced',setup_id=signal['setup_id']))
        for block in self.model.position_blocks:
            key = block['submodel_key']
            timing(dict(block, entry_eligible_timestamp_ms=block.get('entry_eligible_timestamp_ms', decision_ms)), decision_ms)
            p = self.portfolios[key]
            if p.position is None or block['blocking_setup_id'] != p.position['sample_id']:
                raise ValueError('Position-block evidence does not match executed position')
            self.blocks.append((key, deepcopy(block)))
        self.model.position_blocks.clear()
        for key, signal in self.model.pending.items():
            if key not in self.portfolios:
                raise ValueError('Unknown pending submodel')
            timing(signal, decision_ms)

    def check_state(self):
        active = {k for k,p in self.portfolios.items() if p.position is not None}
        if active != set(self.model.positions):
            raise ValueError('Model/executor position state diverged')
        for key in active:
            if self.model.positions[key]['setup_id'] != self.portfolios[key].position['sample_id']:
                raise ValueError('Model/executor setup identity diverged')

    def cancel_pending(self, key, timestamp_ms, reason='cancelled'):
        signal = self.model.pending.get(key)
        if signal is None:
            return
        if type(timestamp_ms) is not int or timestamp_ms < signal['decision_timestamp_ms']:
            raise ValueError('Cancellation precedes decision')
        del self.model.pending[key]
        self.notify('entry_rejected', key, dict(timestamp_ms=timestamp_ms, reason=reason, setup_id=signal['setup_id']))

    def manage_risk(self, key, portfolio, tick):
        position = portfolio.position
        update = self.risk_manager(deepcopy(position), dict(tick))
        if update is None:
            return
        stop = update["stop"]
        side = 1 if position["direction"] == "long" else -1
        if (not math.isfinite(stop) or stop <= 0
                or side * (stop - position["stop"]) < 0
                or side * (tick["price"] - stop) <= 0
                or not math.isfinite(update["risk_trigger_price"])
                or side * (update["risk_trigger_price"] - tick["price"]) <= 0):
            raise ValueError("Invalid stop update or next reward milestone")
        changed = stop != position["stop"]
        position.update(update)
        if changed:
            position["risk_stop_updates"] += 1
            portfolio.orders.append(dict(
                sample_id=position["sample_id"], action="STOP_UPDATE",
                timestamp_ms=tick["timestamp_ms"], agg_trade_id=tick["agg_trade_id"],
                stop=stop, locked_r=position["risk_lock_r"],
            ))
            self.notify("stop_update", key, portfolio.orders[-1])

    def tick(self, tick):
        order = (tick['timestamp_ms'], tick['agg_trade_id'])
        if (any(type(x) is not int or x < 0 for x in order)
                or not math.isfinite(tick['price']) or tick['price'] <= 0
                or (self.last_order is not None and order <= self.last_order)):
            raise ValueError('Invalid or non-increasing execution print')
        self.check_state()
        for key,p in self.portfolios.items():
            if p.position is not None:
                count = len(p.orders)
                position = p.position
                if self.timed_scale_out is not None:
                    partial_ms, _, _ = self.timed_scale_out
                    ts = tick['timestamp_ms']
                    side = 1 if position['direction'] == 'long' else -1
                    if (not position.get('tp1_filled') and p.stop_trigger is None
                            and position['entry_timestamp_ms'] + partial_ms <= ts < position['force_exit_timestamp_ms']
                            and side * (tick['price'] - position['stop']) > 0):
                        p.partial_trigger = dict(timestamp_ms=position['entry_timestamp_ms'] + partial_ms)
                        take_partial(p, tick, self.partial_config)
                        p.orders[-1]['reason'] = 'timed_partial_next_print'
                on_tick(p, tick, self.config)
                if (self.risk_manager is not None and p.position is not None
                        and p.stop_trigger is None and p.target_trigger is None):
                    self.manage_risk(key, p, tick)
                for event in p.orders[count:]:
                    if event['action'] == 'PARTIAL_EXIT':
                        self.notify('partial_fill', key, event)
                    elif event['action'] == 'EXIT':
                        trade = p.trades[-1]
                        if self.risk_manager is not None:
                            trade.update(
                                initial_stop=position["initial_stop"], final_stop=position["stop"],
                                risk_lock_r=position["risk_lock_r"],
                                risk_stop_updates=position["risk_stop_updates"],
                                stop=position["initial_stop"],
                            )
                            if trade["exit_reason"] == "initial_stop_next_print" and position["risk_lock_r"] >= 0:
                                trade["exit_reason"] = "risk_lock_stop_next_print"
                                event["reason"] = trade["exit_reason"]
                        if self.timed_scale_out is not None:
                            trade.update(
                                tp1_fraction=self.timed_scale_out[2],
                                tp1_filled=position.get('tp1_filled', False),
                                tp1_price=position.get('tp1_fill_price'),
                                tp1_timestamp_ms=position.get('tp1_timestamp_ms'),
                                remaining_exit_quantity=position.get('remaining_quantity', position['quantity']),
                                partial_gross=position.get('partial_gross', 0.0),
                                partial_exit_fee=position.get('partial_fee', 0.0),
                                final_exit_fee=event['fee'],
                            )
                        if p.equity <= 0:
                            self.insolvent.add(key)
                        self.model.record_exit(key, price=tick['price'], timestamp_ms=tick['timestamp_ms'], reason=trade['exit_reason'])
                        self.model.records[key].clear()
                        self.notify('exit_fill', key, dict(event, trade=trade))
                # Never close and re-enter using the same print.
                continue
            pending = self.model.pending.get(key)
            if pending is None:
                continue
            timing(pending, tick['timestamp_ms'])
            ts = tick['timestamp_ms']
            eligible = pending['entry_eligible_timestamp_ms']
            if ts < eligible:
                continue
            expires = pending.get('expires_at_ms')
            deadline = pending.get('entry_deadline_timestamp_ms')
            if ((expires is not None and ts >= expires) or (deadline is not None and ts > deadline)
                    or ts - pending.get('intended_entry_eligible_timestamp_ms', eligible) > self.config.max_entry_delay_ms):
                self.cancel_pending(key, ts, 'entry_expired_or_late')
                continue
            if p.equity <= 0:
                self.insolvent.add(key)
                self.cancel_pending(key, ts, 'insufficient_equity')
                continue
            entry_day = ts // 86_400_000
            if self.one_entry_per_utc_day and self.last_entry_day.get(key) == entry_day:
                self.cancel_pending(key, ts, 'daily_entry_limit')
                continue
            proposal = self.model.accept_fill(key, price=tick['price'], timestamp_ms=ts)
            if key in self.model.pending:
                raise ValueError('accept_fill must consume the pending intention')
            if proposal is None:
                if key in self.model.positions:
                    raise ValueError('Rejected geometry left a model position')
                self.notify('entry_rejected', key, dict(timestamp_ms=ts, reason='model_geometry', setup_id=pending['setup_id']))
                continue
            side = 1 if proposal['direction'] == 'long' else -1 if proposal['direction'] == 'short' else 0
            distance = side * (tick['price']-proposal['stop'])
            target = proposal.get('target')
            if (not side or not math.isfinite(distance) or distance <= 0 or proposal['stop'] <= 0
                    or (target is not None and (not math.isfinite(target) or side*(target-tick['price']) <= 0))):
                raise ValueError('Invalid accepted entry geometry')
            force = proposal.get('force_exit_timestamp_ms', sys.maxsize)
            if type(force) is not int or force <= ts:
                raise ValueError('Accepted time exit must follow entry')
            if self.timed_scale_out is not None:
                force = ts + self.timed_scale_out[1]
            elif 'exit_after_fill_ms' in proposal:
                delay = proposal['exit_after_fill_ms']
                if type(delay) is not int or delay <= 0:
                    raise ValueError('Invalid fill-relative exit duration')
                force = ts + delay
            signal = dict(proposal, sample_id=proposal['setup_id'],
                session_day=proposal.get('session_day', datetime.fromtimestamp(ts/1000, timezone.utc).date().isoformat()),
                entry_reference=proposal.get('entry_reference', tick['price']),
                entry_eligible_timestamp_ms=eligible, entry_deadline_timestamp_ms=deadline if deadline is not None else ts,
                force_exit_timestamp_ms=force)
            self.entries[key, signal['sample_id']] = deepcopy(signal)
            p.signals.append(signal)
            p.target_r = side*(target-tick['price'])/distance if target is not None else None
            on_tick(p, tick, self.config)
            if p.position is None:
                raise ValueError('Executor rejected geometry already accepted by model')
            # Retain the exact absolute model target, avoiding ratio reconstruction drift.
            p.position['target_price'] = target
            if self.risk_manager is not None:
                p.position.update(initial_stop=p.position["stop"], risk_stop_updates=0)
                self.manage_risk(key, p, tick)
            if self.one_entry_per_utc_day:
                self.last_entry_day[key] = entry_day
            self.notify('entry_fill', key, dict(p.orders[-1], position=p.position))
        self.last_order = order
        self.check_state()


class PreparedModel:
    """Compatibility normalization for complete prepared intentions, no strategy rules."""
    submodels = ('prepared',)

    def __init__(self, signals):
        lanes = signals if isinstance(signals, dict) else {'prepared': signals}
        if not lanes or any(not isinstance(key, str) or not key for key in lanes):
            raise ValueError('Require named prepared portfolios')
        self.submodels = tuple(lanes)
        for rows in lanes.values():
            if len({s['sample_id'] for s in rows}) != len(rows):
                raise ValueError('Duplicate prepared setup identity within portfolio')
        self.signals = sorted(((key, deepcopy(s)) for key, rows in lanes.items() for s in rows),
                              key=lambda item: (item[1]['entry_eligible_timestamp_ms'], item[0], item[1]['sample_id']))
        self.pending, self.positions, self.position_blocks = {}, {}, []
        self.records = {key: [] for key in lanes}
        self.index = 0

    def evaluate_at(self, boundary, decision_ms):
        while self.index < len(self.signals) and self.signals[self.index][1]['entry_eligible_timestamp_ms'] == boundary:
            key, original = self.signals[self.index]; self.index += 1
            signal = dict(original, setup_id=original['sample_id'],
                          intended_entry_eligible_timestamp_ms=original['entry_eligible_timestamp_ms'],
                          decision_timestamp_ms=max(decision_ms, original['decision_timestamp_ms']))
            signal['entry_eligible_timestamp_ms'] = max(signal['entry_eligible_timestamp_ms'], signal['decision_timestamp_ms'])
            if key in self.positions:
                self.position_blocks.append(dict(signal, submodel_key=key,
                    blocking_setup_id=self.positions[key]['setup_id'],
                    blocking_entry_timestamp_ms=self.positions[key]['entry_timestamp_ms']))
            else:
                if key in self.pending:
                    raise ValueError('Prepared model requires at most one unfilled intention per lane')
                self.pending[key] = signal

    def accept_fill(self, key, *, price, timestamp_ms):
        signal = self.pending.pop(key)
        side = 1 if signal['direction']=='long' else -1
        if side*(price-signal['stop']) <= 0:
            return None
        position = dict(signal, entry_timestamp_ms=timestamp_ms)
        self.positions[key] = position
        return deepcopy(position)

    def record_exit(self, key, **kwargs):
        del self.positions[key]
