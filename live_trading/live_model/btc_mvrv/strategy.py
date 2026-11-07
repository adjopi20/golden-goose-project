"""BTC fixed 0/7 and four-year P5/P95: frozen signal-only monitor."""
from utils.model_registry import verify_model as _verify_model
_PACKAGE_CONTRACT = _verify_model(__file__)
"""Shared, daily, immutable-vintage Coin Metrics source and causal thresholds."""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
METRICS = ["PriceUSD", "CapMrktCurUSD", "CapMVRVCur"]
ORIGIN = "2010-07-18"


class DataError(ValueError):
    pass


def utc(value=None):
    value = datetime.now(timezone.utc) if value is None else pd.Timestamp(value).to_pydatetime()
    if value.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return value.astimezone(timezone.utc)


def fetch(start, end, session=requests):
    params = dict(assets="btc", metrics=",".join(METRICS), frequency="1d",
                  start_time=start, end_time=end, page_size=10000, paging_from="start")
    rows, seen_tokens = [], set()
    for _ in range(100):
        try:
            response = session.get(URL, params=params, timeout=(10, 60))
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise DataError(f"Coin Metrics request failed ({type(error).__name__})") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise DataError("Malformed Coin Metrics response")
        rows.extend(payload["data"])
        token = payload.get("next_page_token")
        if not token:
            return normalize(pd.DataFrame(rows))
        if token in seen_tokens:
            raise DataError("Repeated API pagination token")
        seen_tokens.add(token)
        params["next_page_token"] = token
    raise DataError("API pagination exceeded safety limit")


def normalize(frame):
    if not {"time", *METRICS} <= set(frame.columns) or frame.empty:
        raise DataError("Missing required daily metrics")
    frame = frame[["time", *METRICS]].copy()
    dates = pd.to_datetime(frame.time, utc=True, errors="raise")
    if not (dates == dates.dt.normalize()).all():
        raise DataError("Expected daily UTC timestamps")
    frame["time"] = dates.dt.strftime("%Y-%m-%d")
    frame = frame.sort_values("time").reset_index(drop=True)
    if frame.time.duplicated().any():
        raise DataError("Duplicate daily observation")
    frame[METRICS] = frame[METRICS].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(frame[METRICS].to_numpy()).all() or (frame[METRICS] <= 0).any().any():
        raise DataError("Incomplete, nonfinite or nonpositive observation")
    return frame


def merged_history(connection, incoming, fetched_at):
    """Validate before committing. Revisions are archived, never silently used."""
    incoming = normalize(incoming)
    old = pd.read_sql_query("SELECT date AS time, price AS PriceUSD, cap AS CapMrktCurUSD, ratio AS CapMVRVCur FROM raw ORDER BY date", connection)
    if not old.empty:
        overlap = old.merge(incoming, on="time", suffixes=("_old", "_new"))
        for metric in METRICS:
            if not np.isclose(overlap[metric + "_old"], overlap[metric + "_new"], rtol=1e-12, atol=1e-10).all():
                raise DataError("Historical revision detected; archived fetch requires operator review")
        incoming = incoming.loc[~incoming.time.isin(old.time)]
    combined = pd.concat([old, incoming], ignore_index=True).sort_values("time").reset_index(drop=True)
    if combined.empty or combined.time.iloc[0] != ORIGIN:
        raise DataError(f"Full indicator history must start {ORIGIN}")
    if list(combined.time) != list(pd.date_range(ORIGIN, combined.time.iloc[-1]).strftime("%Y-%m-%d")):
        raise DataError("Daily history has a gap")
    for date, price, cap, ratio in incoming.itertuples(index=False, name=None):
        connection.execute("INSERT INTO raw VALUES(?,?,?,?,?)", (date, price, cap, ratio, fetched_at))
    return combined


def prepare(raw, config):
    raw = normalize(raw)
    if raw.time.iloc[0] != config["history_origin"]:
        raise DataError("Indicator origin mismatch")
    if list(raw.time) != list(pd.date_range(raw.time.iloc[0], raw.time.iloc[-1]).strftime("%Y-%m-%d")):
        raise DataError("Indicator history is not contiguous")
    cap = raw.CapMrktCurUSD
    z = (cap - cap / raw.CapMVRVCur) / cap.expanding(min_periods=2).std(ddof=0)
    daily = pd.DataFrame({"date": raw.time, "price": raw.PriceUSD, "z": z}).iloc[1:].reset_index(drop=True)
    if not np.isfinite(daily[["price", "z"]]).all().all():
        raise DataError("Invalid derived Z-score")
    for model in config["models"]:
        if "window" in model:
            past = daily.z.shift(1).rolling(model["window"], min_periods=model["window"])
            daily[model["id"] + "_entry"] = past.quantile(model["low"])
            daily[model["id"] + "_exit"] = past.quantile(model["high"])
        else:
            daily[model["id"] + "_entry"] = model["entry"]
            daily[model["id"] + "_exit"] = model["exit"]
    return daily


def fresh(day, now):
    return str(day) == (utc(now).date() - timedelta(days=1)).isoformat()


def next_close(day, evaluated_at):
    # Row D marks D's UTC close at D+1 midnight. Historical next-row close is
    # D+2 midnight; publication delay may require an even later future close.
    label_due = pd.Timestamp(day, tz="UTC") + pd.Timedelta(days=2)
    actual_due = pd.Timestamp(utc(evaluated_at)).normalize() + pd.Timedelta(days=1)
    return max(label_due, actual_due).isoformat()

def configuration():
    from pathlib import Path
    config = json.loads(Path(__file__).with_name("model_config.json").read_text())
    if config != {
        "contract": "btc-mvrv-dual-v1", "history_origin": "2010-07-18", "fee_bps": 10,
        "models": [{"id": "fixed_0_7", "weight": "0.5", "entry": 0, "exit": 7},
                   {"id": "percentile_4y_p5_p95", "weight": "0.5", "window": 1460, "low": .05, "high": .95}]}:
        raise ValueError("Frozen config changed; create and test a new contract version")
    return config
from utils.engine.state.mvrv import archive, backup, connect, get_meta, message, set_meta
from utils.engine.monitoring.mvrv_notify import deliver

import argparse
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import signal
import threading
import time



def bind(c):
    if get_meta(c, 'contract') or get_meta(c, 'venue_identity') or any(
            c.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
            for table in ('accounts', 'actions', 'fills', 'marks')):
        raise ValueError('Signal monitor refuses funded/trading databases; use its dedicated volume')
    config = configuration()
    fingerprint = json.dumps(config, sort_keys=True)
    old = get_meta(c, 'monitor_contract')
    if old is not None and old != fingerprint:
        raise ValueError('Monitor rules changed; preserve existing state and review')
    with c: set_meta(c, 'monitor_contract', fingerprint)
    return config


def zones(row, config):
    result = []
    z, price = float(row['z']), float(row['price'])
    if not math.isfinite(z) or not math.isfinite(price) or price <= 0:
        raise DataError('Invalid monitor Z-score/price')
    for model in config['models']:
        name = model['id']
        low, high = float(row[name + '_entry']), float(row[name + '_exit'])
        if not math.isfinite(low) or not math.isfinite(high) or low >= high:
            raise DataError('Monitor threshold warmup incomplete or invalid')
        zone = 'BUY_ZONE' if z < low else 'SELL_ZONE' if z >= high else 'WAIT'
        result.append(dict(model=name, z=z, entry=low, exit=high, zone=zone))
    return result


def report(c):
    day = get_meta(c, 'last_data_date')
    return dict(mode='signal_monitor', orders_enabled=False, capital_required=False,
                last_data_date=day, evaluated_at=get_meta(c, 'last_success_at'),
                price_usd=json.loads(get_meta(c, 'monitor_price_usd', 'null')),
                models=[dict(model=r['account'], z=r['z'], entry=r['entry'], exit=r['exit'], zone=r['action'])
                        for r in c.execute('SELECT * FROM decisions WHERE date=? ORDER BY account', (day,))],
                error=get_meta(c, 'error', ''), notification_error=get_meta(c, 'notification_error', ''),
                unsent_notifications=c.execute('SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL').fetchone()[0],
                interpretation='Indicator zones only; manual holdings are unknown. No buy/sell is executed.')


def cycle(c, now=None):
    config = bind(c)
    now = utc(now)
    when = now.isoformat()
    expected = (now.date() - timedelta(days=1)).isoformat()
    last = get_meta(c, 'last_data_date')
    if last == expected:
        return report(c)
    if last and last > expected:
        raise DataError('Clock moved backward; refusing older monitor date')
    start = (pd.Timestamp(last) - pd.Timedelta(days=7)).strftime('%Y-%m-%d') if last else ORIGIN
    incoming = fetch(start, expected)
    with c: archive(c, incoming, when)
    with c:
        c.execute('BEGIN IMMEDIATE')
        latest = prepare(merged_history(c, incoming, when), config).iloc[-1]
        if not fresh(latest['date'], now):
            raise DataError(f"Expected UTC data {expected}; newest is {latest['date']}")
        models = zones(latest, config)
        lines = ["BTC MVRV — MONITOR SINYAL (TANPA TRANSAKSI)",
                 f"Data UTC: {expected} | Dicek WIB: {(now + timedelta(hours=7)).strftime('%Y-%m-%d %H:%M')}",
                 f"BTC referensi Coin Metrics: ${float(latest['price']):,.2f}",
                 f"MVRV Z-score: {float(latest['z']):.6f}"]
        labels = {'BUY_ZONE': 'ZONA BELI', 'SELL_ZONE': 'ZONA JUAL', 'WAIT': 'TUNGGU'}
        for model in models:
            c.execute('INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?,?,?)',
                      (model['model'], expected, when, model['z'], model['entry'], model['exit'], model['zone']))
            name = '0/7' if model['model'] == 'fixed_0_7' else '4 tahun P5/P95'
            lines.append(f"{name}: {labels[model['zone']]} | beli Z < {model['entry']:.6f}; jual Z >= {model['exit']:.6f}")
        lines.append('Status zona, bukan perintah order. Posisi manual tidak dilacak; tidak ada transaksi otomatis.')
        # Never deliver an old queued BUY-zone message as today's status.
        c.execute("UPDATE outbox SET delivered_at=? WHERE delivered_at IS NULL AND (id LIKE 'signal-error:%' OR (id LIKE 'signal-status:%' AND id!=?))",
                  ('superseded:' + when, 'signal-status:' + expected))
        message(c, 'signal-status:' + expected, '\n'.join(lines))
        set_meta(c, 'last_data_date', expected)
        set_meta(c, 'last_success_at', when)
        set_meta(c, 'monitor_price_usd', json.dumps(float(latest['price'])))
        set_meta(c, 'error', '')
    return report(c)


def notify(c, now):
    try:
        deliver(c, now.isoformat())
        with c: set_meta(c, 'notification_error', '')
    except (ValueError, RuntimeError, OSError) as error:
        with c: set_meta(c, 'notification_error', type(error).__name__ + ': Telegram delivery failed; outbox retained')


def checked_cycle(c, now=None):
    now = utc(now)
    try:
        return cycle(c, now)
    except (ValueError, RuntimeError, OSError) as error:
        label = str(error) if isinstance(error, DataError) else type(error).__name__
        with c:
            set_meta(c, 'error', label)
            message(c, 'signal-error:' + now.date().isoformat(),
                    'BTC MVRV monitor: data/status belum valid. ' + label + '. Tidak ada transaksi. Status terakhir: ' + str(get_meta(c, 'last_data_date')))
        raise


def refresh_due(c, now):
    # Coin Metrics prior-day inputs often arrive after 08:00 WIB.
    expected = (now.date() - timedelta(days=1)).isoformat()
    return now.hour >= 3 and get_meta(c, 'last_data_date') != expected


def healthy(c, now=None):
    now = utc(now)
    beat, day = get_meta(c, 'heartbeat'), get_meta(c, 'last_data_date')
    return bool(beat and day and 0 <= (now - utc(beat)).total_seconds() < 300
                and 0 <= (now.date() - pd.Timestamp(day).date()).days <= 2
                and not get_meta(c, 'error') and not get_meta(c, 'notification_error'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'once', 'report', 'health', 'backup'])
    parser.add_argument('--database', type=Path, default=Path(os.getenv('DATABASE', '/data/signals.sqlite')))
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    if args.action in {'report', 'health', 'backup'} and not args.database.exists():
        parser.error('Monitor database does not exist yet')
    c = connect(args.database)
    try:
        bind(c)
        if args.action == 'health':
            raise SystemExit(0 if healthy(c) else 1)
        if args.action == 'report':
            print(json.dumps(report(c), indent=2, allow_nan=False))
        elif args.action == 'backup':
            if not args.destination: parser.error('Use a NEW --destination')
            backup(c, args.destination)
            print('Signal monitor online backup complete')
        elif args.action == 'once':
            checked_cycle(c)
            with c: set_meta(c, 'heartbeat', utc().isoformat())
            notify(c, utc())
            print(json.dumps(report(c), indent=2, allow_nan=False))
        else:
            stopped = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stopped.set())
            signal.signal(signal.SIGINT, lambda *_: stopped.set())
            next_attempt = 0
            while not stopped.is_set():
                now = utc()
                if refresh_due(c, now) and time.monotonic() >= next_attempt:
                    next_attempt = time.monotonic() + 3600
                    try:
                        checked_cycle(c, now)
                        print(json.dumps(report(c), allow_nan=False), flush=True)
                    except (ValueError, RuntimeError, OSError):
                        print('Public MVRV data check failed; hourly retry; no exchange orders.', flush=True)
                with c: set_meta(c, 'heartbeat', utc().isoformat())
                notify(c, utc())
                stopped.wait(60)
    except (ValueError, RuntimeError, OSError) as error:
        parser.error(str(error) if isinstance(error, DataError) else type(error).__name__ + '; check monitor configuration/data')
    finally:
        c.close()


if __name__ == '__main__':
    main()
