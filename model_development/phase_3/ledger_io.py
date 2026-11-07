"""Shared validated ledger input. No imports from models or strategy code."""
from pathlib import Path
from datetime import date
import csv
import hashlib
import json
import math
import re
import numpy as np


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')


def write_csv(path, rows):
    if not rows:
        raise ValueError('No results to write')
    with Path(path).open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def config_load(path):
    path = Path(path).resolve()
    cfg = read_json(path)
    # Config-relative paths, independent of the shell's current directory.
    root = (path.parent / cfg.get('path_root', '.')).resolve()
    if cfg.get('schema_version') != 1:
        raise ValueError('Expected schema_version=1')
    if not cfg.get('datasets'):
        raise ValueError('No datasets')
    return cfg, root


def equal(actual, expected):
    if isinstance(expected, list):
        return any(equal(actual, x) for x in expected)
    if isinstance(expected, (int, float)):
        try:
            return math.isclose(float(actual), expected, rel_tol=1e-10, abs_tol=1e-12)
        except (TypeError, ValueError):
            return False
    return str(actual) == str(expected)


def sources(cfg, root):
    """Accept explicit trades.jsonl or select rows in a shared-engine sweep CSV."""
    names = set()
    for spec in cfg['datasets']:
        if spec.get('completion_marker') and not (root/spec['completion_marker']).is_file():
            raise ValueError(f"Source run is incomplete: {spec['completion_marker']}")
        if 'ledger' in spec:
            selected = [(spec['name'], (root/spec['ledger']).resolve())]
        else:
            with (root/spec['comparison_csv']).open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            rows = [r for r in rows if all(equal(r.get(k), v) for k, v in spec['where'].items())]
            if not rows or len(rows) != spec.get('expected_count', len(rows)):
                raise ValueError(f"Unexpected selection count for {spec['name']}: {len(rows)}")
            base = (root/spec['ledger_root']).resolve()
            selected = []
            for row in rows:
                label = row['label']
                path = (base/label/'trades.jsonl').resolve()
                if not path.is_relative_to(base):
                    raise ValueError('Unsafe sweep label')
                selected.append((spec['name']+'__'+label, path))
        for name, path in selected:
            if name in names:
                raise ValueError(f'Duplicate dataset: {name}')
            names.add(name)
            yield name, path, spec


def ledger_load(path, spec):
    raw = Path(path).read_bytes()
    rows = [json.loads(x) for x in raw.decode('utf-8').splitlines() if x.strip()]
    if not rows:
        raise ValueError(f'Empty ledger: {path}')
    rows.sort(key=lambda r: r['entry_timestamp_ms'])
    start, end = date.fromisoformat(spec['start_date']), date.fromisoformat(spec['end_date'])
    days = (end-start).days+1
    if days < 1:
        raise ValueError('Reversed dates')
    previous_exit, seen = -1, set()
    vals = {k: [] for k in ('r', 'fee_r', 'distance', 'offset', 'stop_remaining')}
    for row in rows:
        sid = row['sample_id']
        if sid in seen or row['entry_timestamp_ms'] <= previous_exit:
            raise ValueError(f'Duplicate or overlapping position: {sid}')
        seen.add(sid)
        previous_exit = row['exit_timestamp_ms']
        if previous_exit < row['entry_timestamp_ms']:
            raise ValueError('Exit precedes entry')
        q, entry, stop = float(row['quantity']), float(row['entry_price']), float(row['stop'])
        risk, pnl, fee = (float(row[k]) for k in ('actual_initial_risk', 'pnl', 'fees'))
        equity = float(row['equity_before_entry'])
        if not all(math.isfinite(x) for x in (q,entry,stop,risk,pnl,fee,equity)):
            raise ValueError('Nonfinite ledger value')
        if min(q, entry, risk, equity) <= 0 or fee < 0:
            raise ValueError('Invalid quantity/entry/risk/equity/fee')
        if not math.isclose(q*abs(entry-stop), risk, rel_tol=1e-7, abs_tol=1e-8):
            raise ValueError('Risk must mean initial price-stop distance times quantity')
        r = pnl/risk
        if not math.isfinite(float(row['r'])) or not math.isclose(r, float(row['r']), rel_tol=1e-7, abs_tol=1e-8):
            raise ValueError('Net R and PnL/risk disagree')
        if 'gross_pnl_before_costs' in row and not math.isclose(float(row['gross_pnl_before_costs'])-fee,pnl,rel_tol=1e-7,abs_tol=1e-8):
            raise ValueError('This adapter requires gross - fees = net; adapt other costs explicitly')
        offset = (date.fromisoformat(row['session_day'])-start).days
        if not 0 <= offset < days or offset in vals['offset']:
            raise ValueError('Outside declared session window or multiple trades/session')
        stopped = row.get('stop_trigger_timestamp_ms') is not None or 'stop' in str(row.get('exit_reason','')).lower()
        if stopped and row.get('tp1_filled') and 'remaining_exit_quantity' not in row:
            raise ValueError('Stopped partial trade requires remaining_exit_quantity')
        remaining = float(row.get('remaining_exit_quantity', q))/q
        if not 0 <= remaining <= 1+1e-8:
            raise ValueError('Invalid remaining quantity')
        for key,value in dict(r=r,fee_r=fee/risk,distance=abs(entry-stop)/entry,
                              offset=offset,stop_remaining=remaining if stopped else 0.).items():
            vals[key].append(value)
    vals = {k: np.array(v, dtype=int if k=='offset' else float) for k,v in vals.items()}
    return dict(**vals,days=days,rows=rows,sha256=hashlib.sha256(raw).hexdigest(),path=str(path),start=str(start),end=str(end))








def output_begin(path):
    path=Path(path)
    if path.exists() and any(path.iterdir()):
        raise ValueError(f'Output must be new or empty (avoid mixing runs): {path}')
    path.mkdir(parents=True,exist_ok=True)
    return path


def receipt(cfg, source_rows, tool, args):
    return dict(schema_version=1,tool=tool,config=cfg,arguments=vars(args),sources=source_rows,
                limitations='Exploratory historical conditional resampling, not fresh OOS or a future probability forecast. Closed-trade equity, no mark-to-market, funding/liquidation model, size impact or rounding. No combined account.',
                code_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')})
