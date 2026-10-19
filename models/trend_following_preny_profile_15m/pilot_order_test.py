"""ETH pilot permission probe. POST only /order/test; no matching-engine endpoint."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlencode

import requests

from live_engine.adapters.binance_futures_readonly import BinanceFuturesReadOnly, PreflightError
from live_engine.adapters.binance_futures_execution import safe_venue_reason
from live_engine.futures_preflight import load_config, number, one, positive, preflight

TEST_URL = 'https://fapi.binance.com/fapi/v1/order/test'


def submit_test_market_request(client, quantity, side):
    if side not in ('BUY', 'SELL'):
        raise PreflightError('Probe side must be BUY or SELL')
    quantity = positive(quantity)
    client.load_credentials()
    if type(client.offset) is not int:
        raise PreflightError('Synchronize time before test-only request')
    fields = dict(symbol='ETHUSDC', side=side, positionSide='BOTH', type='MARKET', quantity=str(quantity),
                  newClientOrderId='ggp-test-'+secrets.token_hex(8)+'-'+side.lower(),
                  newOrderRespType='RESULT', timestamp=int(time.time()*1000)+client.offset, recvWindow=5000)
    fields['signature'] = base64.b64encode(client._signer.sign(urlencode(fields).encode('ascii'))).decode('ascii')
    started = time.perf_counter_ns()
    try:
        r = client.http.request('POST', TEST_URL, data=fields, headers={'X-MBX-APIKEY':client._key},
                                timeout=(5,15), allow_redirects=False)
    except requests.RequestException:
        raise PreflightError('Test-only order connection/timeout failure; not retried') from None
    elapsed = (time.perf_counter_ns()-started)/1000000
    try:
        body = r.json()
    except ValueError:
        raise PreflightError('Test-only order returned invalid JSON; not retried') from None
    code = body.get('code') if isinstance(body,dict) and type(body.get('code')) is int else None
    if r.status_code != 200 or (code is not None and code < 0):
        reason = safe_venue_reason(body.get('msg') if isinstance(body,dict) else None,
                                   (client._key, fields['signature']))
        raise PreflightError(f'Test-only order HTTP {r.status_code}, code {code}; {reason or "no safe venue reason available"}')
    if not isinstance(body,dict):
        raise PreflightError('Unexpected test-only response; inspect account before continuing')
    # Production /order/test can return an empty dummy quantity, unlike real orders.
    raw_executed = body.get('executedQty')
    executed = None if raw_executed in (None,'') else number(raw_executed)
    if executed:
        raise PreflightError('Unexpected execution reported by test-only response; inspect account')
    return dict(side=side, quantity=str(quantity), endpoint='/fapi/v1/order/test', accepted=True,
                request_duration_ms=elapsed, real_order=False,
                reported_executed_quantity=None if executed is None else str(executed))


def probe(client, config):
    report = preflight(client, config)
    if report['blockers']:
        raise PreflightError('Read-only preflight blocked; no test-only POST attempted')
    quantity = report['minimum_geometry']['minimum_exact_split_quantity']
    results = [submit_test_market_request(client,quantity,side) for side in ('BUY','SELL')]
    positions = client.get('/fapi/v3/positionRisk')
    orders = client.get('/fapi/v1/openOrders')
    algos = client.get('/fapi/v1/openAlgoOrders')
    if not all(isinstance(rows,list) and all(isinstance(r,dict) for r in rows) for rows in (positions,orders,algos)):
        raise PreflightError('Cannot verify post-test account state')
    if any(number(r['positionAmt'],signed=True) for r in positions) or orders or algos:
        raise PreflightError('Post-test account not flat/order-free; operator inspection required')
    wallet = number(one(client.get('/fapi/v3/account')['assets'],'asset','USDC')['walletBalance'],signed=True)
    if wallet != number(report['account']['wallet_balance'],signed=True):
        raise PreflightError('Wallet changed during test-only probe; inspect before continuing')
    return dict(stage='test_order_probe', real_orders=False, live_ready=False, symbol='ETHUSDC',
                allocated_equity=config['allocated_equity'], test_results=results,
                active_positions_after=0, open_orders_after=0, open_algo_orders_after=0,
                wallet_before=report['account']['wallet_balance'],wallet_after=str(wallet),
                native_stop_validation='not_performed; different live conditional-order endpoint')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=Path('/app/config/preflight.json'))
    p.add_argument('--private-key-file',type=Path,default=Path('/run/secrets/key.pem'))
    args = p.parse_args()
    client = BinanceFuturesReadOnly(api_key=os.environ.get('BINANCE_API_KEY'),private_key_file=args.private_key_file)
    try:
        print(json.dumps(probe(client,load_config(args.config)),indent=2,allow_nan=False),flush=True)
        return 0
    except PreflightError as error:
        print(json.dumps(dict(real_orders=False,live_ready=False,error=str(error))),flush=True)
        return 2
    except (KeyError,TypeError,ValueError,OSError,ArithmeticError):
        print(json.dumps(dict(real_orders=False,live_ready=False,error='Probe configuration/response invalid; details redacted')),flush=True)
        return 2
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
