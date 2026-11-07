"""LTCUSDC paper_v33 volume-profile reversion. Local frozen candidate; import does not start a feed or place an order."""
from __future__ import annotations
from live_trading.live_model import verify_release as _verify_frozen_release
_verify_frozen_release()
# Frozen from indicator/volume_profile.py
import json
import numpy as np
import pandas as pd
DEFAULT_VOLUME_PROFILE_BINS = 30
DEFAULT_VALUE_AREA_PCT = 0.7
HVN_THRESHOLD = 0.65
LVN_THRESHOLD = 0.2
SMOOTHING_WINDOW = 3

def _compute_value_area(volume_profile: list[dict], poc_bin_index: int, value_area_pct: float) -> dict:
    """
    Compute Value Area using outward expansion from POC.

    Algorithm:
    1) Start from POC bin
    2) Expand outward
    3) At each step, compare neighboring bin volumes and include the larger first
    4) If both adjacent bins have equal volume, include both bins in the same iteration
    5) Stop when cumulative included volume >= target percentage of total session volume

    VAL = lower edge of lowest included bin
    VAH = upper edge of highest included bin
    """
    if not volume_profile:
        raise ValueError('volume_profile must not be empty')
    total_volume = float(sum((bin_row['total_volume'] for bin_row in volume_profile)))
    target_volume = float(value_area_pct) * total_volume
    included_bins = {int(poc_bin_index)}
    cumulative_volume = float(volume_profile[poc_bin_index]['total_volume'])
    left = poc_bin_index - 1
    right = poc_bin_index + 1
    while cumulative_volume < target_volume and (left >= 0 or right < len(volume_profile)):
        has_left = left >= 0
        has_right = right < len(volume_profile)
        if has_left and has_right:
            left_vol = float(volume_profile[left]['total_volume'])
            right_vol = float(volume_profile[right]['total_volume'])
            if left_vol == right_vol:
                included_bins.add(int(left))
                included_bins.add(int(right))
                cumulative_volume += left_vol + right_vol
                left -= 1
                right += 1
            elif left_vol > right_vol:
                included_bins.add(int(left))
                cumulative_volume += left_vol
                left -= 1
            else:
                included_bins.add(int(right))
                cumulative_volume += right_vol
                right += 1
        elif has_left:
            included_bins.add(int(left))
            cumulative_volume += float(volume_profile[left]['total_volume'])
            left -= 1
        else:
            included_bins.add(int(right))
            cumulative_volume += float(volume_profile[right]['total_volume'])
            right += 1
    low_idx = min(included_bins)
    high_idx = max(included_bins)
    return {'val': float(volume_profile[low_idx]['bin_low']), 'vah': float(volume_profile[high_idx]['bin_high']), 'value_area_volume': float(cumulative_volume)}

def _prepare_basic_profile_inputs(trades_df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    required_cols = {'price', 'qty'}
    missing = sorted(required_cols - set(trades_df.columns))
    if missing:
        raise ValueError(f'Missing required columns: {missing}')
    if trades_df.empty:
        raise ValueError('trades_df must not be empty')
    local_df = trades_df.loc[:, ['price', 'qty']].copy()
    local_df['price'] = pd.to_numeric(local_df['price'], errors='coerce')
    local_df['qty'] = pd.to_numeric(local_df['qty'], errors='coerce')
    local_df = local_df.dropna(subset=['price', 'qty'])
    if local_df.empty:
        raise ValueError('No valid numeric price/qty rows remain after local cleaning')
    if (local_df['qty'] < 0).any():
        raise ValueError('Negative quantity detected in trades_df; basic volume profile requires non-negative qty values')
    prices = local_df['price'].to_numpy(dtype=np.float64)
    quantities = local_df['qty'].to_numpy(dtype=np.float64)
    total_volume = float(np.sum(quantities))
    if total_volume <= 0.0:
        raise ValueError('Total valid quantity must be positive')
    return (prices, quantities)

def build_basic_volume_profile(trades_df: pd.DataFrame, n_bins: int=DEFAULT_VOLUME_PROFILE_BINS, value_area_pct: float=DEFAULT_VALUE_AREA_PCT) -> dict:
    """
    Build a lightweight session-relative, quantity-weighted volume profile.

    This function is intentionally independent from HVN/LVN/node processing.
    It requires only `price` and `qty` columns.
    """
    if n_bins <= 0:
        raise ValueError('n_bins must be > 0')
    if not 0 < value_area_pct <= 1:
        raise ValueError('value_area_pct must satisfy 0 < value_area_pct <= 1')
    prices, quantities = _prepare_basic_profile_inputs(trades_df)
    session_low = float(np.min(prices))
    session_high = float(np.max(prices))
    total_volume = float(np.sum(quantities))
    if session_low == session_high:
        poc_price = float(session_low)
        return {'session_low': float(session_low), 'session_high': float(session_high), 'bin_width': 0.0, 'bins': int(n_bins), 'total_volume': float(total_volume), 'poc_bin_index': 0, 'poc_price': float(poc_price), 'poc_volume': float(total_volume), 'poc_volume_pct': 1.0, 'val': float(session_low), 'vah': float(session_high), 'value_area_width': 0.0, 'value_area_width_pct': np.nan if session_low == 0 else 0.0, 'value_area_volume': float(total_volume), 'value_area_volume_pct': 1.0}
    histogram, edges = np.histogram(prices, bins=n_bins, range=(session_low, session_high), weights=quantities)
    histogram = histogram.astype(np.float64, copy=False)
    bin_width = float((session_high - session_low) / n_bins)
    poc_bin_index = int(np.argmax(histogram))
    poc_volume = float(histogram[poc_bin_index])
    poc_price = float((edges[poc_bin_index] + edges[poc_bin_index + 1]) / 2.0)
    basic_bins = [{'bin_index': int(i), 'bin_low': float(edges[i]), 'bin_high': float(edges[i + 1]), 'total_volume': float(histogram[i])} for i in range(len(histogram))]
    value_area_data = _compute_value_area(volume_profile=basic_bins, poc_bin_index=poc_bin_index, value_area_pct=value_area_pct)
    val = float(value_area_data['val'])
    vah = float(value_area_data['vah'])
    value_area_width = float(vah - val)
    value_area_width_pct = np.nan if session_low == 0 else float(value_area_width / session_low)
    return {'session_low': float(session_low), 'session_high': float(session_high), 'bin_width': float(bin_width), 'bins': int(n_bins), 'total_volume': float(total_volume), 'poc_bin_index': int(poc_bin_index), 'poc_price': float(poc_price), 'poc_volume': float(poc_volume), 'poc_volume_pct': float(poc_volume / total_volume), 'val': float(val), 'vah': float(vah), 'value_area_width': float(value_area_width), 'value_area_width_pct': value_area_width_pct, 'value_area_volume': float(value_area_data['value_area_volume']), 'value_area_volume_pct': float(value_area_data['value_area_volume'] / total_volume)}

# Frozen from models/mean_reversion_by_volume_profile/paper_v33/worker.py
import argparse
from datetime import datetime, time as day_time, timedelta
import hashlib
import inspect
import json
import math
from pathlib import Path
import signal
import sqlite3
import time
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from live_trading.collectors.frozen_v1.binance import BinancePublic, normalize, UnrecoverableGap
from live_trading.execution.frozen_v1.order_manager import PaperOrderManager, save_state
from live_trading.state.frozen_v1.state_store import StateStore, encode
from live_trading.execution.frozen_v1.paper_execution import Config
ACCOUNT = 'mr-ltcusdc-intersection-r025-v1'
SYMBOL = 'LTCUSDC'
NY = ZoneInfo('America/New_York')
CONTRACT = dict(version=ACCOUNT, mode='paper', venue='binance_usdm', symbol=SYMBOL, initial_equity=10000, risk_fraction=0.025, fee_bps_per_side=4, max_notional_equity=5, bins=50, value_area_pct=0.7, ny_open='09:30', location='strictly_outside_previous_value_area', distance_pct_min=1, target='frozen_previous_poc', reward_risk=1, max_candidates_per_session=1, execution='next_public_print', fresh_entry_max_age_ms=5000, max_entry_delay_ms=300000)

def session_bounds(ts):
    now = datetime.fromtimestamp(ts / 1000, NY)
    day = now.date() - timedelta(days=now.time() < day_time(9, 30))
    times = [int(datetime.combine(day + timedelta(days=i), day_time(9, 30), NY).timestamp() * 1000) for i in (-1, 0, 1)]
    return (str(day), *times)

class PocPaperManager(PaperOrderManager):
    """Resolve a contingent symmetric bracket, then use the unchanged shared engine."""

    def tick(self, account, tick, *, allow_entry=True):
        with self.store.transaction():
            row, p, cfg = self._load(account)
            cursor = json.loads(row['cursor']) if row['cursor'] else None
            new = cursor is None or tick['agg_trade_id'] > cursor['agg_trade_id']
            if new and p.signals and (p.position is None):
                candidate = p.signals[p.next_signal]
                reason = None
                eligible = tick['timestamp_ms'] >= candidate['entry_eligible_timestamp_ms']
                if not allow_entry:
                    reason = 'pending_entry_cancelled_during_recovery_or_stale_feed'
                elif eligible and tick['timestamp_ms'] <= candidate['entry_deadline_timestamp_ms']:
                    price, poc = (tick['price'], candidate['frozen_poc'])
                    side = 1 if candidate['direction'] == 'long' else -1
                    risk = side * (poc - price)
                    if risk <= 0 or 2 * price - poc <= 0 or 100 * risk / price < 1:
                        reason = 'distance_or_geometry_invalid_at_actual_fill'
                    else:
                        candidate['stop'] = 2 * price - poc
                        p.target_r = 1.0
                        self.store.record(account, 'execution_bracket_resolved', dict(sample_id=candidate['sample_id'], timestamp_ms=tick['timestamp_ms'], agg_trade_id=tick['agg_trade_id'], entry=price, stop=candidate['stop'], frozen_poc=poc))
                if reason:
                    p.next_signal += 1
                    self.store.record(account, 'decision', dict(sample_id=candidate['sample_id'], decision='SKIP', reason=reason))
                self.store.db.execute('UPDATE accounts SET state=? WHERE id=?', (save_state(p, cfg), account))
            return super().ticks(account, [tick])[0]

class Model:

    def __init__(self, store):
        self.store = store
        if any((row[0] != ACCOUNT for row in store.db.execute('SELECT id FROM accounts'))):
            raise ValueError('Use a dedicated database for this paper model')
        self.manager = PocPaperManager(store)
        identity = hashlib.sha256(encode(CONTRACT).encode()).hexdigest()
        self.manager.register(ACCOUNT, SYMBOL, Config(initial_equity=10000, risk_fraction=0.025, fee_bps=4, max_leverage=5), contract=identity)
        store.db.executescript('\n            CREATE TABLE IF NOT EXISTS mr_ticks(\n                id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, price REAL NOT NULL,\n                qty REAL NOT NULL, buy INTEGER NOT NULL);\n            CREATE INDEX IF NOT EXISTS mr_tick_time ON mr_ticks(ts);\n            CREATE TABLE IF NOT EXISTS mr_sessions(day TEXT PRIMARY KEY, payload TEXT NOT NULL);\n        ')
        files = [Path(__file__), *[Path(inspect.getfile(x)) for x in (build_basic_volume_profile, PaperOrderManager, StateStore, Config, BinancePublic)]]
        code_hash = hashlib.sha256(b''.join((p.read_bytes() for p in files))).hexdigest()
        with store.transaction():
            store.db.execute("INSERT OR IGNORE INTO metadata VALUES ('mr_code_hash',?)", (code_hash,))
            if store.db.execute("SELECT value FROM metadata WHERE key='mr_code_hash'").fetchone()[0] != code_hash:
                raise ValueError('Paper source changed: use a new version/database; do not reset the ledger')

    def cursor(self):
        row = self.store.db.execute('SELECT * FROM mr_ticks ORDER BY id DESC LIMIT 1').fetchone()
        return dict(agg_trade_id=row['id'], timestamp_ms=row['ts'], price=row['price'], quantity=row['qty'], buy=bool(row['buy']), symbol=SYMBOL) if row else None

    def freeze(self, tick):
        day, start, opening, end = session_bounds(tick['timestamp_ms'])
        rows = self.store.db.execute('SELECT * FROM mr_ticks WHERE ts>=? AND ts<? ORDER BY id', (start, opening)).fetchall()
        reason = 'valid'
        if not rows:
            reason = 'profile_no_data'
        elif max(rows[0]['ts'] - start, opening - rows[-1]['ts']) > 60000:
            reason = 'profile_boundary_gap'
        elif any((b['ts'] - a['ts'] > 300000 or b['id'] != a['id'] + 1 for a, b in zip(rows, rows[1:]))):
            reason = 'profile_internal_gap'
        elif tick['timestamp_ms'] - opening > 60000:
            reason = 'ny_open_gap'
        state = dict(day=day, profile_start_ms=start, opening_ms=opening, end_ms=end, opening_price=tick['price'], previous_price=tick['price'], consumed=False, quality=reason, profile_rows=len(rows))
        if reason == 'valid':
            vp = build_basic_volume_profile(pd.DataFrame(dict(price=[r['price'] for r in rows], qty=[r['qty'] for r in rows])), n_bins=50, value_area_pct=0.7)
            poc, val, vah = (float(vp[k]) for k in ('poc_price', 'val', 'vah'))
            state.update(poc=poc, val=val, vah=vah, profile_volume=float(vp['total_volume']))
            if not 0 < val < poc < vah:
                state['quality'] = 'degenerate_profile'
            state.update(side=int(np.sign(poc - tick['price'])), location=tick['price'] < val or tick['price'] > vah)
        self.store.record(ACCOUNT, 'session_frozen', state)
        self.store.db.execute('DELETE FROM mr_ticks WHERE ts<?', (start,))
        return state

    def process(self, tick, *, origin='stream', now_ms=None):
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if tick.get('symbol') != SYMBOL or type(tick.get('buy')) is not bool:
            raise ValueError('Invalid symbol/side')
        if any((type(tick.get(k)) is not int or tick[k] < 0 for k in ('timestamp_ms', 'agg_trade_id'))):
            raise ValueError('Invalid event identity')
        if any((isinstance(tick.get(k), bool) or not isinstance(tick.get(k), (float, int)) or (not math.isfinite(tick[k])) or (tick[k] <= 0) for k in ('price', 'quantity'))):
            raise ValueError('Invalid price/quantity')
        with self.store.transaction():
            previous = self.cursor()
            if previous and tick['agg_trade_id'] <= previous['agg_trade_id']:
                old = self.store.db.execute('SELECT * FROM mr_ticks WHERE id=?', (tick['agg_trade_id'],)).fetchone()
                if old and (old['ts'], old['price'], old['qty'], bool(old['buy'])) != (tick['timestamp_ms'], tick['price'], tick['quantity'], tick['buy']):
                    raise ValueError('Conflicting duplicate')
                return
            if previous and (tick['agg_trade_id'] != previous['agg_trade_id'] + 1 or tick['timestamp_ms'] < previous['timestamp_ms']):
                raise UnrecoverableGap('Feed must be repaired before advancing the ledger')
            fresh = origin == 'stream' and 0 <= now_ms - tick['timestamp_ms'] <= 5000
            self.manager.tick(ACCOUNT, tick, allow_entry=fresh)
            day, _, _, _ = session_bounds(tick['timestamp_ms'])
            old = self.store.db.execute('SELECT payload FROM mr_sessions WHERE day=?', (day,)).fetchone()
            state = json.loads(old[0]) if old else self.freeze(tick)
            if previous and tick['timestamp_ms'] - previous['timestamp_ms'] > 300000:
                state['quality'] = 'observation_large_trade_gap'
            if state['quality'] == 'valid' and (not state['consumed']) and state['side']:
                side = state['side']
                boundary = state['val'] if side == 1 else state['vah']
                price = tick['price']
                touched = price == boundary or (state['previous_price'] - boundary) * (price - boundary) < 0
                if touched:
                    state['consumed'] = True
                    risk = side * (state['poc'] - price)
                    geometry = risk > 0 and 2 * price - state['poc'] > 0
                    selected = fresh and state['location'] and geometry and (100 * risk / price >= 1)
                    ts = tick['timestamp_ms']
                    deadline = min(ts + 300000, state['end_ms'] - 1)
                    if ts + 1 <= deadline and geometry:
                        intent = dict(sample_id=f'{ACCOUNT}:{day}', session_day=day, symbol=SYMBOL, direction='long' if side == 1 else 'short', stop=2 * price - state['poc'], entry_reference=price, frozen_poc=state['poc'], feature_as_of_ms=ts, entry_eligible_timestamp_ms=ts + 1, entry_deadline_timestamp_ms=deadline, force_exit_timestamp_ms=state['end_ms'], strategy=ACCOUNT, route='paper')
                        self.manager.submit(ACCOUNT, intent, snapshot=dict(state, feature_as_of_ms=ts, origin=origin, fresh=fresh), risk_fraction=0.025, selected=bool(selected))
                    else:
                        self.store.record(ACCOUNT, 'first_touch_skipped', dict(day=day, timestamp_ms=ts, reason='invalid_geometry_or_no_remaining_entry_time'))
            state['previous_price'] = tick['price']
            self.store.db.execute('INSERT OR REPLACE INTO mr_sessions VALUES (?,?)', (day, encode(state)))
            self.store.db.execute('INSERT INTO mr_ticks VALUES (?,?,?,?,?)', (tick['agg_trade_id'], tick['timestamp_ms'], tick['price'], tick['quantity'], int(tick['buy'])))
