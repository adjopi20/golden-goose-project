"""Public-data zone monitor. No exchange credentials, portfolio or execution."""
import argparse
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import signal
import threading
import time

from . import data
from .engine import configuration
from .notify import deliver
from .storage import archive, backup, connect, get_meta, message, set_meta


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
        raise data.DataError('Invalid monitor Z-score/price')
    for model in config['models']:
        name = model['id']
        low, high = float(row[name + '_entry']), float(row[name + '_exit'])
        if not math.isfinite(low) or not math.isfinite(high) or low >= high:
            raise data.DataError('Monitor threshold warmup incomplete or invalid')
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
    now = data.utc(now)
    when = now.isoformat()
    expected = (now.date() - timedelta(days=1)).isoformat()
    last = get_meta(c, 'last_data_date')
    if last == expected:
        return report(c)
    if last and last > expected:
        raise data.DataError('Clock moved backward; refusing older monitor date')
    start = (data.pd.Timestamp(last) - data.pd.Timedelta(days=7)).strftime('%Y-%m-%d') if last else data.ORIGIN
    incoming = data.fetch(start, expected)
    with c: archive(c, incoming, when)
    with c:
        c.execute('BEGIN IMMEDIATE')
        latest = data.prepare(data.merged_history(c, incoming, when), config).iloc[-1]
        if not data.fresh(latest['date'], now):
            raise data.DataError(f"Expected UTC data {expected}; newest is {latest['date']}")
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
    now = data.utc(now)
    try:
        return cycle(c, now)
    except (ValueError, RuntimeError, OSError) as error:
        label = str(error) if isinstance(error, data.DataError) else type(error).__name__
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
    now = data.utc(now)
    beat, day = get_meta(c, 'heartbeat'), get_meta(c, 'last_data_date')
    return bool(beat and day and 0 <= (now - data.utc(beat)).total_seconds() < 300
                and 0 <= (now.date() - data.pd.Timestamp(day).date()).days <= 2
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
            with c: set_meta(c, 'heartbeat', data.utc().isoformat())
            notify(c, data.utc())
            print(json.dumps(report(c), indent=2, allow_nan=False))
        else:
            stopped = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stopped.set())
            signal.signal(signal.SIGINT, lambda *_: stopped.set())
            next_attempt = 0
            while not stopped.is_set():
                now = data.utc()
                if refresh_due(c, now) and time.monotonic() >= next_attempt:
                    next_attempt = time.monotonic() + 3600
                    try:
                        checked_cycle(c, now)
                        print(json.dumps(report(c), allow_nan=False), flush=True)
                    except (ValueError, RuntimeError, OSError):
                        print('Public MVRV data check failed; hourly retry; no exchange orders.', flush=True)
                with c: set_meta(c, 'heartbeat', data.utc().isoformat())
                notify(c, data.utc())
                stopped.wait(60)
    except (ValueError, RuntimeError, OSError) as error:
        parser.error(str(error) if isinstance(error, data.DataError) else type(error).__name__ + '; check monitor configuration/data')
    finally:
        c.close()


if __name__ == '__main__':
    main()
