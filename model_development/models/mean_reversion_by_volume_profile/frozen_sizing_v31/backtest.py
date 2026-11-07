"""Full-history LTC mean reversion: frozen location/distance rules, causal ATR sizing.

Run from the repository root. See README.md; old research-run files are not inputs.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict, deque
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

NY = ZoneInfo("America/New_York")
DAY = 86_400_000
COLUMNS = ("timestamp", "price", "qty", "agg_trade_id")
ACTIVE = ("tp_first", "sl_first", "time_exit")
CASES = {1: ("union", "location"), 2: ("intersection", "location"),
         3: ("intersection", "atr_high"), 4: ("union", "atr_high"),
         5: ("union", "location_and_atr"), 6: ("intersection", "location_and_atr")}
RULES = dict(version="frozen_sizing_v31", profile_bins=50, value_area_fraction=.7,
             distance_min_pct=1., location="outside_va_strict", reward_risk=1.,
             fee_bps_per_side=4., normal_risk_fraction=.005, max_notional_equity=5.,
             atr_days=30, atr_quantile=2/3, calibration_months=8,
             calibration_min_valid_days=60, calibration_step_months=2,
             calibration_anchor="January/March/May/July/September/November, midnight NY",
             missing_atr="normal sizing; preserve entries", atr_smoother="SMA",
             max_internal_gap_seconds=300, max_boundary_gap_seconds=60,
             entry="v21 open-to-POC bias, first selected-edge touch from either direction",
             exit="observed TP/SL trigger print or last print before next 09:30 NY",
             training_selection="none: opening location always outside VA",
             provenance="Local archives; exchange/product provenance unverified")
LEGACY_CODE_SHA256 = "86a1d8f5accb3b2d53d7cc0dfccfcda94d3d2ac5786cad808b9eed1cd76c26bf"
DATA_PREPARATION = "v31.1: remove repeated aggregate IDs only when timestamp/price/qty agree exactly; preserve first-occurrence order; record raw/used/dropped counts"


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stamp(value):
    return int(pd.Timestamp(value).value//1_000_000)


def ny_midnight(value):
    return stamp(pd.Timestamp(value).tz_localize(NY))


def bounds(day):
    return tuple(int(datetime.combine(day+timedelta(days=i), time(9, 30), NY).timestamp()*1000)
                 for i in (-1, 0, 1))


def iso(ms):
    return datetime.fromtimestamp(int(ms)/1000, NY).isoformat()


def empty_arrays():
    return [np.array([], dtype=t) for t in (np.int64, float, float, np.int64)]


def unique_trades(arrays):
    """Drop verified copies, preserving raw order; conflicts still fail explicitly."""
    ids = arrays[3]
    if len(ids) < 2 or (np.diff(ids) > 0).all():
        return arrays, 0
    _, first, inverse = np.unique(ids, return_index=True, return_inverse=True)
    originals = first[inverse]
    repeated = np.flatnonzero(np.arange(len(ids)) != originals)
    for name, values in zip(COLUMNS[:3], arrays[:3]):
        mismatch = repeated[values[repeated] != values[originals[repeated]]]
        if len(mismatch):
            raise ValueError(f"Conflicting repeated aggregate ID {int(ids[mismatch[0]])}: different {name}; source repair required")
    keep = np.sort(first)
    return [a[keep] for a in arrays], len(ids)-len(keep)


def compatible_resume(previous, current):
    if previous == current:
        return False
    if previous.get("code_sha256") == LEGACY_CODE_SHA256 and "data_preparation" not in previous:
        upgraded = dict(previous, code_sha256=current["code_sha256"], data_preparation=DATA_PREPARATION)
        if upgraded == current:
            return True
    raise ValueError("Resume requires unchanged sources, rules, indicators and environment; only the known original v31 code can upgrade to this duplicate-handling patch")


def write_json_atomic(path, value):
    pending = path.with_suffix(path.suffix+".tmp")
    pending.write_text(json.dumps(value, indent=2), encoding="utf-8")
    pending.replace(path)


class Archive:
    """Same bounded row-group window pattern as observe_half_pullback.WindowReader.

    Multiple non-overlapping Parquet files are supported; overlapping archives fail
    explicitly rather than double-counting or silently deduplicating transactions.
    """
    def __init__(self, folder):
        self.files, self.groups, self.sources = [], [], []
        self.cache, self.groups_read = OrderedDict(), 0
        paths = sorted(Path(folder).rglob("*.parquet"))
        if not paths:
            raise ValueError(f"No Parquet archives in {folder}")
        try:
            for path in paths:
                f = pq.ParquetFile(path)
                self.files.append(f)
                if not set(COLUMNS).issubset(f.schema_arrow.names) or not f.metadata.num_rows:
                    raise ValueError(f"Missing columns or empty archive: {path}")
                for col in COLUMNS:
                    dtype = str(f.schema_arrow.field(col).type)
                    allowed = ("int64", "int32") if col in ("timestamp", "agg_trade_id") else ("int64", "double", "float", "int32")
                    if dtype not in allowed:
                        raise ValueError(f"Numeric epoch-ms/price/qty/id columns required: {path}: {col}={dtype}")
                index = f.schema_arrow.names.index("timestamp")
                ranges = []
                for n in range(f.num_row_groups):
                    s = f.metadata.row_group(n).column(index).statistics
                    if s is None or not s.has_min_max or s.null_count:
                        raise ValueError(f"Timestamp min/max statistics without nulls required: {path}, group {n}")
                    ranges.append((int(s.min), int(s.max)))
                lo, hi = np.array(ranges).T
                if (lo > hi).any() or (lo[1:] < hi[:-1]).any():
                    raise ValueError(f"Unordered row-group timestamps: {path}")
                if lo.min() < 946684800000 or hi.max() >= 4102444800000:
                    raise ValueError(f"Expected millisecond timestamps between 2000 and 2100: {path}")
                record = dict(path=str(path.resolve()), bytes=path.stat().st_size,
                              mtime_ns=path.stat().st_mtime_ns, rows=f.metadata.num_rows,
                              first_ms=int(lo.min()), last_ms=int(hi.max()))
                self.sources.append(record)
                self.groups.extend((a, b, len(self.files)-1, n) for n, (a, b) in enumerate(ranges))
            self.sources.sort(key=lambda x: x["first_ms"])
            for a, b in zip(self.sources, self.sources[1:]):
                if a["last_ms"] >= b["first_ms"]:
                    raise ValueError(f"Overlapping archives: {a['path']} and {b['path']}")
            self.groups.sort(key=lambda x: (x[0], x[2], x[3]))
            self.mins = np.array([r[0] for r in self.groups])
            self.maxs = np.array([r[1] for r in self.groups])
            self.first_ms, self.last_ms = int(self.mins.min()), int(self.maxs.max())
        except BaseException:
            self.close()
            raise

    def window(self, start, end):
        parts = []
        for i in np.flatnonzero((self.maxs >= start) & (self.mins < end)):
            i = int(i)
            if i not in self.cache:
                _, _, file_no, group_no = self.groups[i]
                t = self.files[file_no].read_row_group(group_no, columns=list(COLUMNS))
                if any(t[c].null_count for c in COLUMNS):
                    raise ValueError(f"Null transaction field in row group {i}")
                self.cache[i] = [t[c].combine_chunks().to_numpy() for c in COLUMNS]
                self.groups_read += 1
                if len(self.cache) > 8:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(i)
            a = self.cache[i]
            keep = (a[0] >= start) & (a[0] < end)
            parts.append([v[keep] for v in a])
        return [np.concatenate([p[i] for p in parts]) for i in range(4)] if parts else empty_arrays()

    def unchanged(self):
        return all(Path(r["path"]).stat().st_size == r["bytes"] and
                   Path(r["path"]).stat().st_mtime_ns == r["mtime_ns"] for r in self.sources)

    def close(self):
        self.cache.clear()
        for f in self.files:
            f.close()


def quality(arrays, start, end, boundaries=True):
    ts, price, qty, ids = arrays
    if not len(ts):
        return "no_data"
    if (np.diff(ts) < 0).any() or (np.diff(ids) <= 0).any():
        i = int(np.flatnonzero((np.diff(ts) < 0) | (np.diff(ids) <= 0))[0])
        raise ValueError(f"Raw ordering conflict at indices {i}/{i+1}: timestamps {int(ts[i])}/{int(ts[i+1])}, "
                         f"aggregate IDs {int(ids[i])}/{int(ids[i+1])}; source repair required")
    if not np.isfinite(price).all() or not np.isfinite(qty).all() or (price <= 0).any() or (qty <= 0).any():
        return "invalid_price_or_volume"
    if (np.diff(ids) != 1).any():
        return "aggregate_id_gaps"
    if boundaries and len(ts) > 1 and np.diff(ts).max() > RULES["max_internal_gap_seconds"]*1000:
        return "large_trade_gap"
    if boundaries and max(ts[0]-start, end-ts[-1]) > RULES["max_boundary_gap_seconds"]*1000:
        return "boundary_gap"
    return "valid"


def daily_bar(arrays, start, first_ms, last_ms, previous_id):
    ts, p, _, ids = arrays
    reason = quality(arrays, start, start+DAY, boundaries=False)
    # The first/last raw print may be away from midnight even in a complete
    # archive. Permit <=60s at dataset ends, matching existing session tolerance.
    covered = (first_ms <= start+60_000 and last_ms >= start+DAY-60_000)
    if len(ids) and previous_id is not None and ids[0] != previous_id+1:
        reason = "aggregate_id_gap_at_utc_boundary"
    row = dict(start_ms=start, available_ms=start+DAY, valid=bool(covered and reason == "valid"),
               quality=reason if covered else "partial_source_day", observations=len(ts))
    if len(p):
        row.update(first_id=int(ids[0]), last_id=int(ids[-1]),
                   first_observation_ms=int(ts[0]), last_observation_ms=int(ts[-1]))
        if np.isfinite(p).all() and (p > 0).all():
            row.update(open=float(p[0]), high=float(p.max()), low=float(p.min()), close=float(p[-1]))
    return row


def analyse(day, arrays, builder):
    start, opening, end = bounds(day)
    ts, price, qty, ids = arrays
    i, j, k = np.searchsorted(ts, (start, opening, end))
    row = dict(session_date=str(day), valid_session=False, profile_start_ms=start,
               opening_ms=opening, end_ms=end, period_hours=(end-opening)/3_600_000)
    for label, left, right, lo, hi in (("profile", i, j, start, opening),
                                      ("observation", j, k, opening, end)):
        reason = quality([a[left:right] for a in arrays], lo, hi)
        if reason != "valid":
            return dict(row, status=label+"_"+reason)
    if ids[j] != ids[j-1]+1:
        return dict(row, status="aggregate_id_gap_at_ny_open")
    vp = builder(pd.DataFrame(dict(price=price[i:j], qty=qty[i:j])), n_bins=50, value_area_pct=.7)
    poc, val, vah = (float(vp[c]) for c in ("poc_price", "val", "vah"))
    row.update(poc=poc, val=val, vah=vah, profile_volume=float(vp["total_volume"]),
               profile_rows=int(j-i), observation_rows=int(k-j), opening_price=float(price[j]))
    if not 0 < val < poc < vah:
        return dict(row, status="degenerate_profile")
    p, t, trade_ids = price[j:k], ts[j:k], ids[j:k]
    side = int(np.sign(poc-p[0]))
    row.update(valid_session=True, location=bool(p[0] < val or p[0] > vah), side=side)
    if not side:
        return dict(row, status="open_at_poc")
    edge, boundary = ("val", val) if side == 1 else ("vah", vah)
    previous = np.r_[p[0], p[:-1]]
    hits = np.flatnonzero((p == boundary) | ((previous-boundary)*(p-boundary) < 0))
    if not len(hits):
        return dict(row, status="no_selected_edge_touch")
    a = int(hits[0])
    entry, risk = float(p[a]), float(side*(poc-p[a]))
    row.update(touch_edge=edge, touch_boundary=boundary, entry=entry, entry_ms=int(t[a]),
               entry_agg_trade_id=int(trade_ids[a]))
    if risk <= 0 or 2*entry-poc <= 0:
        return dict(row, status="invalid_first_touch_geometry")
    delta = side*(p[a+1:]-entry)
    barriers = np.flatnonzero((delta >= risk) | (delta <= -risk))
    b = a+1+int(barriers[0]) if len(barriers) else len(p)-1
    status = ("tp_first" if side*(p[b]-entry) >= risk else "sl_first") if len(barriers) else "time_exit"
    exit_price = float(p[b])
    gross = float(side*(exit_price-entry)/risk)
    cost = RULES["fee_bps_per_side"]*.0001*(entry+exit_price)/risk
    distance = 100*risk/entry
    return dict(row, status=status, risk_price=risk, sl=2*entry-poc, tp=poc,
                exit_ms=int(t[b]), exit_agg_trade_id=int(trade_ids[b]), exit_price=exit_price,
                gross_r=gross, net_r=gross-cost, fee_r=cost, distance_pct=distance,
                distance=bool(distance >= RULES["distance_min_pct"]),
                hold_minutes=float((t[b]-t[a])/60_000))


def read_checkpoint(path, resume):
    if not path.exists():
        return []
    if not resume:
        raise FileExistsError(path)
    raw = path.read_bytes()
    complete = raw.rfind(b"\n")+1
    if complete != len(raw):
        print(f"Discarding incomplete final checkpoint line: {path.name}", flush=True)
        with path.open("r+b") as f:
            f.truncate(complete)
    rows = [json.loads(line) for line in raw[:complete].splitlines()]
    days = [r["utc_start_ms"] for r in rows]
    if len(days) > 1 and not np.all(np.diff(days) == DAY):
        raise ValueError("Checkpoint days are duplicated, missing or unordered")
    return rows


def raw_pass(archive, path, builder, resume):
    rows = read_checkpoint(path, resume)
    first = archive.first_ms//DAY*DAY
    last = archive.last_ms//DAY*DAY+DAY  # flush final partial NY session visibly
    begin = rows[-1]["utc_start_ms"]+DAY if rows else first
    if rows and rows[0]["utc_start_ms"] != first:
        raise ValueError("Checkpoint does not start at the archive's first UTC day")
    buffer = deque(maxlen=3)
    previous_id = None
    if rows:
        previous_id = next((r["daily"]["last_id"] for r in reversed(rows) if "last_id" in r["daily"]), None)
    for d in range(max(first, begin-2*DAY), begin, DAY):
        a, _ = unique_trades(archive.window(d, d+DAY))
        buffer.append(a)
        if len(a[3]):
            previous_id = int(a[3][-1])
    first_session = datetime.fromtimestamp(archive.first_ms/1000, NY).date()
    last_session = datetime.fromtimestamp(archive.last_ms/1000, NY).date()
    with path.open("a", encoding="utf-8") as log:
        for d in range(begin, last+1, DAY):
            raw = archive.window(d, d+DAY)
            a, dropped = unique_trades(raw)
            bar = daily_bar(a, d, archive.first_ms, archive.last_ms, previous_id)
            bar.update(raw_observations=len(raw[0]), exact_duplicates_removed=dropped)
            if dropped:
                print(f"{path.stem}: {pd.Timestamp(d, unit='ms').date()} | "
                      f"removed {dropped:,} identical repeated trades; raw archive unchanged", flush=True)
            buffer.append(a)
            if len(a[3]):
                previous_id = int(a[3][-1])
            session_day = pd.Timestamp(d, unit="ms", tz="UTC").date()-timedelta(days=1)
            session = None
            if first_session <= session_day <= last_session:
                combined = [np.concatenate([b[c] for b in buffer]) for c in range(4)]
                if (np.diff(combined[0]) < 0).any() or (np.diff(combined[3]) <= 0).any():
                    raise ValueError("Raw ordering/duplicate failure across UTC days")
                session = analyse(session_day, combined, builder)
            record = dict(utc_start_ms=d, daily=bar, session=session)
            log.write(json.dumps(record, allow_nan=False)+"\n")
            log.flush()
            rows.append(record)
            if len(rows) % 30 == 0 or d == last:
                print(f"{path.stem}: {pd.Timestamp(d, unit='ms').date()} | "
                      f"days {len(rows)} | row groups read {archive.groups_read}", flush=True)
    if not archive.unchanged():
        raise ValueError("Source archives changed during the backtest")
    sessions = pd.DataFrame([r["session"] for r in rows if r["session"] is not None])
    return sessions, pd.DataFrame([r["daily"] for r in rows])


def atr_states(bars, true_range):
    b = bars.sort_values("start_ms").reset_index(drop=True).copy()
    for c in ("open", "high", "low", "close"):
        if c not in b:
            b[c] = np.nan
    ohlc = b[["high", "low", "close"]].where(b.valid)
    tr = true_range(ohlc.high, ohlc.low, ohlc.close).where(b.valid & b.valid.shift(1, fill_value=False))
    b["true_range"] = tr
    b["atr30_pct"] = 100*tr.rolling(30, min_periods=30).mean()/ohlc.close
    return b


def block_start(value):
    v = pd.Timestamp(value)
    return pd.Timestamp(v.year, 1+2*((v.month-1)//2), 1)


def add_regimes(trades, states, first_ms):
    d = trades.copy().reset_index(drop=True)
    cuts = []
    first_day = pd.Timestamp(first_ms, unit="ms", tz="UTC").tz_convert(NY).tz_localize(None)
    earliest = block_start(first_day)
    latest = block_start(pd.Timestamp(states.available_ms.max(), unit="ms", tz="UTC").tz_convert(NY).tz_localize(None))
    for start in pd.date_range(earliest, latest, freq="2MS"):
        train_start = start-pd.DateOffset(months=8)
        known = states.loc[states.available_ms.ge(ny_midnight(train_start)) &
                           states.available_ms.lt(ny_midnight(start)), "atr30_pct"].dropna()
        full_history = first_ms <= ny_midnight(train_start)
        supported = full_history and len(known) >= RULES["calibration_min_valid_days"]
        cuts.append(dict(block_start=str(start.date()), block_end_exclusive=str((start+pd.DateOffset(months=2)).date()),
                         train_start=str(train_start.date()), train_end_exclusive=str(start.date()),
                         train_days=len(known), full_eight_month_history=bool(full_history),
                         supported=bool(supported), high_cut=float(known.quantile(2/3)) if supported else np.nan))
    cut_table = pd.DataFrame(cuts)
    if d.empty:
        for col in ("atr30_pct", "atr_high_cut", "macro_available_ms"):
            d[col] = pd.Series(dtype=float)
        for col in ("atr_ready", "atr_high"):
            d[col] = pd.Series(dtype=bool)
        d["atr_block_start"] = pd.Series(dtype=str)
        return d, cut_table
    pos = np.searchsorted(states.available_ms.to_numpy(), d.entry_ms.to_numpy(), side="right")-1
    if (pos < 0).any():
        raise ValueError("No completed UTC daily state before an entry")
    latest_states = states.iloc[pos]
    d["macro_available_ms"] = latest_states.available_ms.to_numpy()
    if not (d.macro_available_ms <= d.entry_ms).all():
        raise ValueError("Future ATR state joined to entry")
    if not (d.entry_ms-d.macro_available_ms < DAY).all():
        raise ValueError("Stale macro state joined to entry")
    d["atr30_pct"] = latest_states.atr30_pct.to_numpy()
    # The calibration block follows the actual entry timestamp, including entries
    # after midnight in a session opened on the preceding date.
    d["atr_block_start"] = [str(block_start(pd.Timestamp(v, unit="ms", tz="UTC").tz_convert(NY).tz_localize(None)).date()) for v in d.entry_ms]
    d["atr_high_cut"] = d.atr_block_start.map(cut_table.set_index("block_start").high_cut)
    d["atr_ready"] = d.atr30_pct.notna() & d.atr_high_cut.notna()
    d["atr_high"] = d.atr_ready & d.atr30_pct.gt(d.atr_high_cut)
    return d, cut_table


def scenarios():
    result = [("baseline_union", "union", "none", 1.),
              ("baseline_intersection", "intersection", "none", 1.)]
    result += [(f"case{n}_x{m:g}", parent, condition, m)
               for n, (parent, condition) in CASES.items() for m in (1.5, 2.)]
    return result


def simulate(trades, parent, condition, multiplier):
    mask = trades.distance | trades.location if parent == "union" else trades.distance & trades.location
    d = trades.loc[mask].sort_values("entry_ms").reset_index(drop=True).copy()
    flag = (d.location if condition == "location" else d.atr_high if condition == "atr_high" else
            d.location & d.atr_high if condition == "location_and_atr" else pd.Series(False, index=d.index))
    distance = d.risk_price/d.entry
    normal = np.minimum(.005, 5*distance)
    target = .005*np.where(flag, multiplier, 1.)
    actual = np.minimum(target, 5*distance)
    returns = actual*d.net_r
    if (returns <= -1).any():
        raise ValueError("Account exhausted in print-proxy simulation; no liquidation model")
    curve = np.r_[1., np.cumprod(1+returns)]
    if not np.isfinite(curve).all() or (curve <= 0).any():
        raise ValueError("Non-finite or exhausted account")
    d["boost_condition"] = flag
    d["normal_risk_fraction"], d["target_risk_fraction"], d["actual_risk_fraction"] = normal, target, actual
    d["actual_size_multiple"] = actual/normal
    d["notional_equity"] = actual/distance
    d["cap_binding"] = actual < target-1e-15
    d["net_trade_return_fraction"] = returns
    d["capital_weighted_net_r"] = d.net_r*actual/.005
    d["equity_before"], d["equity_after"] = curve[:-1], curve[1:]
    d["net_cash_pnl"] = curve[:-1]*returns
    d["fee_cash"] = curve[:-1]*actual*d.fee_r
    d["closed_drawdown_fraction"] = 1-curve[1:]/np.maximum.accumulate(curve)[1:]
    if len(d) > 1 and (d.entry_ms.iloc[1:].to_numpy() <= d.exit_ms.iloc[:-1].to_numpy()).any():
        raise ValueError("Overlapping trades in one account")
    return d


def metrics(d):
    n = len(d)
    curve = np.r_[1., np.cumprod(1+d.net_trade_return_fraction.to_numpy())]
    dd = float(100*(1-curve/np.maximum.accumulate(curve)).max())
    wins, losses = d.net_r[d.net_r > 0].sum(), -d.net_r[d.net_r < 0].sum()
    raw = d.net_r.to_numpy()
    pos_mean = lambda s: float(s.mean()) if n else None
    return dict(trades=n, boost_condition_trades=int(d.boost_condition.sum()),
                actual_increased_trades=int((d.actual_risk_fraction > d.normal_risk_fraction+1e-15).sum()),
                capped_trades=int(d.cap_binding.sum()), atr_ready_trades=int(d.atr_ready.sum()),
                atr_unavailable_trades=int((~d.atr_ready).sum()),
                gross_wr_pct=100*pos_mean(d.gross_r > 0) if n else None,
                net_wr_pct=100*pos_mean(d.net_r > 0) if n else None,
                mean_gross_r=pos_mean(d.gross_r), mean_net_r=pos_mean(d.net_r),
                median_net_r=float(np.median(raw)) if n else None, total_net_r=float(raw.sum()),
                weighted_total_net_r=float(d.capital_weighted_net_r.sum()),
                risk_weighted_mean_net_r=float((d.actual_risk_fraction*d.net_r).sum()/d.actual_risk_fraction.sum()) if n else None,
                net_account_return_pct=float(100*(curve[-1]-1)), max_closed_dd_pct=dd,
                return_over_closed_dd=float(100*(curve[-1]-1)/dd) if dd else None,
                profit_factor_net_r=float(wins/losses) if losses else None,
                mean_net_position_pct=100*pos_mean(d.net_r*d.risk_price/d.entry) if n else None,
                mean_target_pct=pos_mean(d.distance_pct), mean_fee_r=pos_mean(d.fee_r),
                tp=int(d.status.eq("tp_first").sum()), sl=int(d.status.eq("sl_first").sum()),
                time_exits=int(d.status.eq("time_exit").sum()))


def validate_ledger(d):
    equity = 1.
    for r in d.itertuples():
        units = equity*r.actual_risk_fraction/r.risk_price
        fees = units*(r.entry+r.exit_price)*.0004
        pnl = units*r.side*(r.exit_price-r.entry)-fees
        np.testing.assert_allclose([r.net_r, r.net_cash_pnl, r.equity_before, r.equity_after, r.fee_cash],
                                   [r.gross_r-r.fee_r, pnl, equity, equity+pnl, fees], atol=1e-10, rtol=1e-10)
        if r.notional_equity > 5+1e-10 or not r.opening_ms <= r.entry_ms <= r.exit_ms < r.end_ms:
            raise ValueError("Notional cap or execution time violated")
        equity += pnl


def period_labels(dates, kind):
    dates = pd.to_datetime(dates)
    if kind == "year":
        return dates.dt.year.astype(str)
    if kind == "semester":
        return dates.dt.year.astype(str)+"-H"+((dates.dt.month-1)//6+1).astype(str)
    return dates.map(lambda d: str(block_start(d).date()))


def reports(all_sessions, all_trades, out):
    summaries, common_rows, period_rows, ledgers, stability = [], [], [], [], []
    valid_sets = [set(g.loc[g.valid_session, "session_date"]) for _, g in all_sessions.groupby("symbol")]
    common = set.intersection(*valid_sets)
    for symbol in sorted(all_sessions.symbol.unique()):
        raw = all_trades.loc[all_trades.symbol.eq(symbol)]
        dates = all_sessions.loc[all_sessions.symbol.eq(symbol), "session_date"]
        coverage = dict(first_session=dates.min(), last_session=dates.max(),
                        valid_sessions=int(all_sessions.loc[all_sessions.symbol.eq(symbol), "valid_session"].sum()))
        for scenario, parent, condition, mult in scenarios():
            ledger = simulate(raw, parent, condition, mult)
            validate_ledger(ledger)
            meta = dict(symbol=symbol, scenario=scenario, parent=parent, condition=condition, multiplier=mult)
            summaries.append(dict(meta, **coverage, **metrics(ledger)))
            ledgers.append(ledger.assign(**meta))
            shared = ledger.loc[ledger.session_date.isin(common)]
            common_rows.append(dict(meta, common_valid_sessions=len(common), **metrics(shared)))
            by_two_month = []
            for kind in ("year", "semester", "two_month"):
                labels = period_labels(dates, kind)
                trade_labels = period_labels(ledger.session_date, kind)
                for label in sorted(labels.unique()):
                    p = ledger.loc[trade_labels.eq(label)]
                    count = int((labels == label).sum())
                    period_dates = dates.loc[labels.eq(label)]
                    valid_count = int(all_sessions.loc[all_sessions.symbol.eq(symbol) &
                        all_sessions.session_date.isin(set(period_dates)), "valid_session"].sum())
                    r = dict(meta, kind=kind, period=label, sessions=count, valid_sessions=valid_count,
                             first_session=period_dates.min(), last_session=period_dates.max(), **metrics(p))
                    period_rows.append(r)
                    if kind == "two_month":
                        by_two_month.append(r)
            nonempty = [r for r in by_two_month if r["trades"]]
            stability.append(dict(meta, two_month_periods=len(by_two_month), nonempty_periods=len(nonempty),
                                  positive_account_periods=sum(r["net_account_return_pct"] > 0 for r in nonempty),
                                  negative_account_periods=sum(r["net_account_return_pct"] < 0 for r in nonempty),
                                  positive_unweighted_r_periods=sum(r["total_net_r"] > 0 for r in nonempty),
                                  periods_under_10_trades=sum(r["trades"] < 10 for r in nonempty),
                                  worst_two_month_return_pct=min((r["net_account_return_pct"] for r in nonempty), default=None),
                                  best_two_month_return_pct=max((r["net_account_return_pct"] for r in nonempty), default=None),
                                  worst_two_month_mean_net_r=min((r["mean_net_r"] for r in nonempty), default=None),
                                  best_two_month_mean_net_r=max((r["mean_net_r"] for r in nonempty), default=None)))
    summary, periods = pd.DataFrame(summaries), pd.DataFrame(period_rows)
    ledger = pd.concat(ledgers, ignore_index=True)
    for symbol in all_sessions.symbol.unique():
        for mult in (1.5, 2.):
            a = ledger.loc[ledger.symbol.eq(symbol) & ledger.scenario.eq(f"case3_x{mult:g}")]
            b = ledger.loc[ledger.symbol.eq(symbol) & ledger.scenario.eq(f"case6_x{mult:g}")]
            np.testing.assert_array_equal(a.entry_agg_trade_id, b.entry_agg_trade_id)
            np.testing.assert_allclose(a.actual_risk_fraction, b.actual_risk_fraction)
    summary.to_csv(out/"summary.csv", index=False)
    pd.DataFrame(common_rows).to_csv(out/"common_dates_summary.csv", index=False)
    periods.to_csv(out/"period_metrics.csv", index=False)
    pd.DataFrame(stability).to_csv(out/"stability.csv", index=False)
    ledger.to_parquet(out/"sizing_ledger.parquet", index=False)
    all_trades.to_parquet(out/"trades.parquet", index=False)
    all_sessions.to_csv(out/"sessions.csv", index=False)
    all_sessions.groupby(["symbol", "status"]).size().rename("sessions").reset_index().to_csv(out/"quality.csv", index=False)
    lines = ["# Full-history frozen LTC sizing — v31", "", "Status: completed and accounting checks passed.", "",
             "Full available raw history, including partial/invalid sessions in quality.csv. "
             "This is historical robustness evidence, not a new temporal holdout. "
             "Old v30 returns are not expected to match: location is now fixed outside VA, dates expand, and initial ATR warm-up uses normal risk.", "",
             "Normal equity risk 0.5%; boosts 0.75% / 1%; RR 1:1; fees 4 bps per side; notional cap 5x. "
             "Cases 3 and 6 are identical; case 2 boosts every intersection trade. "
             "Trade count, gross/net WR and unweighted R are unchanged by sizing within the same parent model.", "",
             "ATR is SMA30 true range / last completed UTC daily close. High means > train q(2/3). "
             "Every odd calendar month at NY midnight, recompute thresholds using the previous eight calendar months. "
             "A full source-history window and >=60 valid ATR states are required. "
             "Before availability, entries remain at normal sizing. Thresholds are pair-specific and use no P&L.", "",
             "Verified repeated aggregate IDs with identical timestamp, price and qty are counted once before profiles/ATR. "
             "Raw archives are unchanged; conflicting repeats or remaining ordering problems still stop the run. "
             "Per-pair raw_quality.csv records source rows, used rows and copies removed. Missing days remain excluded, never filled.", "",
             "| Pair | Scenario | Trades | Boost | Gross WR | Mean net R | Weighted net R | Return | Closed DD |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in summary.to_dict("records"):
        f = lambda k, suffix="": "—" if pd.isna(r[k]) else f"{r[k]:.3f}{suffix}"
        lines.append(f"| {r['symbol']} | {r['scenario']} | {r['trades']} | {r['boost_condition_trades']} | "
                     +" | ".join(f(k, "%" if "pct" in k else "") for k in
                                 ("gross_wr_pct", "mean_net_r", "weighted_total_net_r", "net_account_return_pct", "max_closed_dd_pct"))+" |")
    lines += ["", "## Stability by time", "",
              "period_metrics.csv includes every calendar year, semester and two-month block, including zero-entry periods. "
              "Periods are attributed to NY session-open date; all entries are closed before the next NY open. "
              "Returns/DD within each period reset equity to 1, while sizing_ledger.parquet carries the full continuous account. "
              "Weighted R uses normal 0.5%-risk units at each trade's equity; total_net_r is unweighted trade R. "
              "Partial first/last periods remain included; read first_session/last_session in summary.csv. "
              "Common-date summary uses the intersection of valid NY session dates across the two pairs, with original causal ATR labels.", "",
              "| Pair | Scenario | Period | Trades | Mean net R | Return | Closed DD |", "|---|---|---|---:|---:|---:|---:|"]
    for r in periods.loc[periods.kind.eq("year")].to_dict("records"):
        mean = "—" if pd.isna(r["mean_net_r"]) else f"{r['mean_net_r']:+.3f}"
        lines.append(f"| {r['symbol']} | {r['scenario']} | {r['period']} | {r['trades']} | {mean} | "
                     f"{r['net_account_return_pct']:+.2f}% | {r['max_closed_dd_pct']:.2f}% |")
    lines += ["", "## Limits", "",
              "Execution uses observed touch/trigger prints, not guaranteed boundary fills. "
              "Spread, latency, market impact, funding, liquidation and intratrade DD are unmodeled. "
              "Venue/product provenance is not established by file names. Fees are the requested common assumption. "
              "Results for LTCUSDC and LTCUSDT share the same underlying and are not independent confirmations. "
              "A larger return under sizing can reflect larger risk; compare DD and risk_weighted_mean_net_r. "
              "No automatic stable/unstable label is assigned from a small trade count."]
    (out/"REPORT.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    return summary, len(ledger)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[4], help="Repository root")
    p.add_argument("--output", type=Path, required=True, help="New run folder, relative to --root or absolute")
    p.add_argument("--resume", action="store_true", help="Reuse completed UTC days with unchanged sources/rules/environment; known original-v31 checkpoints can upgrade")
    args = p.parse_args(argv)
    root = args.root.resolve()
    out = (root/args.output).resolve()
    if any(out.is_relative_to((root/"storage"/s).resolve()) for s in ("ltcusdc", "ltcusdt")):
        raise ValueError("Output must not be inside either input folder")
    sys.path.insert(0, str(root))
    from indicator.volume_profile import build_basic_volume_profile
    from indicator.trend import true_range
    archives = {}
    try:
        for symbol in ("LTCUSDC", "LTCUSDT"):
            archives[symbol] = Archive(root/"storage"/symbol.lower())
        snapshot = dict(rules=RULES, sources={s: a.sources for s, a in archives.items()},
                        code_sha256=sha(__file__),
                        data_preparation=DATA_PREPARATION,
                        dependencies={str(Path(sys.modules[f.__module__].__file__).resolve()):
                                      sha(sys.modules[f.__module__].__file__) for f in (build_basic_volume_profile, true_range)},
                        raw_fingerprint="path/size/mtime/Parquet row count/timestamp statistics; no full raw content hash",
                        environment=dict(python=sys.version, numpy=np.__version__, pandas=pd.__version__))
        if args.resume:
            if not out.is_dir():
                raise ValueError("Resume requires an existing output folder")
            previous = json.loads((out/"config_snapshot.json").read_text())
            if compatible_resume(previous, snapshot):
                backup = out/"config_snapshot.original_v31.json"
                if backup.exists() and json.loads(backup.read_text()) != previous:
                    raise ValueError("Existing original-v31 snapshot backup differs; refusing overwrite")
                if not backup.exists():
                    write_json_atomic(backup, previous)
                write_json_atomic(out/"config_snapshot.json", snapshot)
                print("Compatible v31 checkpoint upgraded; original snapshot preserved. Completed days will be reused.", flush=True)
        else:
            out.mkdir(parents=True, exist_ok=False)
            (out/"config_snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        # An interrupted re-report must never retain a successful completion marker.
        (out/"audit.json").write_text(json.dumps(dict(status="running")), encoding="utf-8")
        sessions, trades, counts = [], [], {}
        for symbol, archive in archives.items():
            print(f"{symbol}: {sum(r['rows'] for r in archive.sources):,} raw rows; full archive", flush=True)
            daily_sessions, bars = raw_pass(archive, out/(symbol+"_checkpoint.jsonl"), build_basic_volume_profile, args.resume)
            # Original v31 stopped before it could checkpoint any duplicate day;
            # legacy rows therefore have raw count == used count and zero copies.
            if "raw_observations" not in bars:
                bars["raw_observations"] = bars.observations
            bars["raw_observations"] = bars.raw_observations.fillna(bars.observations).astype(np.int64)
            if "exact_duplicates_removed" not in bars:
                bars["exact_duplicates_removed"] = 0
            bars["exact_duplicates_removed"] = bars.exact_duplicates_removed.fillna(0).astype(np.int64)
            if not (bars.raw_observations == bars.observations+bars.exact_duplicates_removed).all():
                raise ValueError("Raw/used/duplicate checkpoint counts do not reconcile")
            if int(bars.raw_observations.sum()) != sum(r["rows"] for r in archive.sources):
                raise ValueError("UTC-day scan did not account for every source transaction")
            counts[symbol] = dict(raw_rows=int(bars.raw_observations.sum()),
                                  used_rows=int(bars.observations.sum()),
                                  exact_duplicates_removed=int(bars.exact_duplicates_removed.sum()),
                                  sessions=len(daily_sessions), valid_daily_bars=int(bars.valid.sum()))
            raw_quality = bars[["start_ms", "quality", "valid", "raw_observations", "observations", "exact_duplicates_removed"]].copy()
            raw_quality.insert(0, "utc_date", pd.to_datetime(raw_quality.start_ms, unit="ms").dt.strftime("%Y-%m-%d"))
            raw_quality.to_csv(out/(symbol+"_raw_quality.csv"), index=False)
            states = atr_states(bars, true_range)
            selected = daily_sessions.loc[daily_sessions.status.isin(ACTIVE)].copy()
            required = ("location", "distance", "entry_ms", "exit_ms", "risk_price", "entry", "gross_r", "net_r", "fee_r", "distance_pct",
                        "entry_agg_trade_id", "exit_agg_trade_id", "exit_price", "side", "opening_ms", "end_ms", "status", "session_date")
            for c in required:
                if c not in selected:
                    selected[c] = pd.Series(dtype=bool if c in ("location", "distance") else float)
            selected["location"] = selected.location.astype(bool)
            selected["distance"] = selected.distance.astype(bool)
            selected, cuts = add_regimes(selected, states, archive.first_ms)
            states.to_parquet(out/(symbol+"_daily_atr.parquet"), index=False)
            cuts.to_csv(out/(symbol+"_atr_cutpoints.csv"), index=False)
            daily_sessions["symbol"], selected["symbol"] = symbol, symbol
            sessions.append(daily_sessions)
            trades.append(selected)
        all_sessions, all_trades = pd.concat(sessions, ignore_index=True), pd.concat(trades, ignore_index=True)
        if all_sessions.duplicated(["symbol", "session_date"]).any():
            raise ValueError("Duplicate session")
        if not all(a.unchanged() for a in archives.values()):
            raise ValueError("Source changed before report completion")
        summary, steps = reports(all_sessions, all_trades, out)
        outputs = {f.name: sha(f) for f in out.iterdir() if f.is_file() and f.name != "audit.json"}
        (out/"audit.json").write_text(json.dumps(dict(status="passed", raw_pass_completed=True,
            accounting_steps_checked=steps, sessions=len(all_sessions), parent_trades=len(all_trades),
            sources_unchanged=True, source_counts=counts, outputs=outputs), indent=2), encoding="utf-8")
        print(summary[["symbol", "scenario", "trades", "mean_net_r", "net_account_return_pct", "max_closed_dd_pct"]].to_string(index=False), flush=True)
        print(f"Completed: {out/'REPORT.md'}", flush=True)
    finally:
        for a in archives.values():
            a.close()


if __name__ == "__main__":
    main()
