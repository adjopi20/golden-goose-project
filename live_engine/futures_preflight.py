"""One-shot production read-only preflight; never consumes signals or places orders."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
from math import lcm
from pathlib import Path
import time

from live_engine.adapters.binance_futures_readonly import BinanceFuturesReadOnly, PreflightError


def number(value, *, signed=False):
    if isinstance(value, bool):
        raise PreflightError('Boolean where an exchange amount was expected')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise PreflightError('Invalid numeric exchange/configuration field') from None
    if not result.is_finite() or abs(result) > Decimal('1e20') or (result and result.as_tuple().exponent < -18):
        raise PreflightError('Nonfinite or unsupported exchange/configuration precision')
    if not signed and result < 0:
        raise PreflightError('Negative exchange/configuration amount')
    return result


def positive(value):
    result = number(value)
    if result <= 0:
        raise PreflightError('Expected a positive exchange/configuration amount')
    return result


def one(rows, field, value):
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise PreflightError('Expected a list of exchange records')
    found = [r for r in rows if r.get(field) == value]
    if len(found) != 1:
        raise PreflightError('Missing or duplicate required exchange record')
    return found[0]


def quantity_rules(symbol):
    filters = symbol.get('filters')
    lot = one(filters, 'filterType', 'LOT_SIZE')
    market = one(filters, 'filterType', 'MARKET_LOT_SIZE')
    price = one(filters, 'filterType', 'PRICE_FILTER')
    notional = one(filters, 'filterType', 'MIN_NOTIONAL')
    steps = [number(r['stepSize']) for r in (lot, market)]
    steps = [s for s in steps if s > 0]
    if not steps:
        raise PreflightError('No usable quantity step')
    places = max(0, *[-s.as_tuple().exponent for s in steps])
    scale = 10 ** places
    step = Decimal(lcm(*[int(s * scale) for s in steps])) / scale
    minimum = max(number(r['minQty']) for r in (lot, market))
    maxima = [number(r['maxQty']) for r in (lot, market)]
    maxima = [v for v in maxima if v > 0]
    if not maxima or minimum > min(maxima):
        raise PreflightError('Invalid quantity bounds')
    return dict(step=step, min_qty=minimum, max_qty=min(maxima),
                min_notional=positive(notional['notional']), tick_size=positive(price['tickSize']))


def minimum_geometry(spec, bid, ask, equity, risks, tp_fraction, cap):
    """Feasibility bounds, NOT entry sizing or authorization for an order.

    Both entry and TP obey quantity filters; reduce-only TP is not assumed to
    require entry MIN_NOTIONAL. Contract rounding policy still needs approval.
    """
    bid, ask, equity = positive(bid), positive(ask), positive(equity)
    fraction, cap = positive(tp_fraction), positive(cap)
    if bid > ask or not 0 < fraction < 1:
        raise PreflightError('Invalid quote or TP fraction')
    rules = quantity_rules(spec)
    # Find a grid that represents the exact fractional split, including 10%.
    _, denominator = fraction.as_integer_ratio()
    split_step = rules['step'] * denominator
    required = max(rules['min_qty'] / fraction, rules['min_qty'] / (1 - fraction),
                   rules['min_notional'] / bid)
    qty = (required / split_step).to_integral_value(rounding=ROUND_CEILING) * split_step
    if qty <= 0 or qty > rules['max_qty'] or qty * ask > equity * cap:
        raise PreflightError('No exact-split minimum position fits the contract notional cap')
    budgets = []
    for risk in risks:
        risk = positive(risk)
        if risk >= 1:
            raise PreflightError('Risk fraction must be below one')
        budget = equity * risk
        budgets.append(dict(risk_fraction=str(risk), price_risk_budget=str(budget),
                            maximum_stop_distance_price=str(budget / qty),
                            maximum_stop_distance_pct=str(100 * budget / (qty * ask))))
    return dict(quantity_step=str(rules['step']), tick_size=str(rules['tick_size']),
                entry_min_notional=str(rules['min_notional']),
                minimum_exact_split_quantity=str(qty), tp1_quantity=str(qty * fraction),
                remainder_quantity=str(qty * (1 - fraction)),
                entry_notional_at_ask=str(qty * ask), risk_bounds=budgets,
                interpretation='Bounds only. Recompute from actual entry/POC; never widen risk to force a trade. '
                               'Exact-split rounding is a conservative feasibility scenario, not an approved live sizing rule.')


def load_config(path):
    c = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    # This first pilot is fixed to the explicitly chosen asset and contract.
    expected = dict(schema_version=1, symbol='ETHUSDC', margin_asset='USDC',
                    contract='TF-PRENY-ETH-001', tp1_r='1', tp1_fraction='0.1',
                    allowed_risk_fractions=['0.0025', '0.005'], max_notional_multiple='5',
                    allocated_equity='145', mode='read-only-preflight')
    if c != expected:
        raise PreflightError('Pilot configuration differs from the approved ETHUSDC/145 contract; review first')
    return c


def preflight(client, config, *, public_only=False):
    c = config
    client.sync_time()
    exchange = client.get('/fapi/v1/exchangeInfo')
    if not isinstance(exchange, dict):
        raise PreflightError('Unexpected exchange-info response')
    spec = one(exchange.get('symbols'), 'symbol', c['symbol'])
    if (spec.get('status'), spec.get('contractType'), spec.get('marginAsset'), spec.get('quoteAsset')) != (
            'TRADING', 'PERPETUAL', c['margin_asset'], c['margin_asset']):
        raise PreflightError('Selected USD-M perpetual unavailable or margin/quote asset changed')
    blockers, account_summary = [], None
    if not public_only:
        cfg = client.get('/fapi/v1/accountConfig')
        account = client.get('/fapi/v3/account')
        positions = client.get('/fapi/v3/positionRisk')
        orders = client.get('/fapi/v1/openOrders')
        algos = client.get('/fapi/v1/openAlgoOrders')
        settings = one(client.get('/fapi/v1/symbolConfig', symbol=c['symbol']), 'symbol', c['symbol'])
        fees = client.get('/fapi/v1/commissionRate', symbol=c['symbol'])
        if not isinstance(cfg, dict) or not isinstance(account, dict) or not isinstance(fees, dict):
            raise PreflightError('Unexpected authenticated account response')
        if fees.get('symbol') != c['symbol']:
            raise PreflightError('Commission symbol mismatch')
        if not all(isinstance(x, list) and all(isinstance(r, dict) for r in x) for x in (positions, orders, algos)):
            raise PreflightError('Unexpected position/order response; cannot assert account is flat')
        asset = one(account.get('assets'), 'asset', c['margin_asset'])
        available, wallet = number(asset['availableBalance']), number(asset['walletBalance'], signed=True)
        if cfg.get('canTrade') is not True:
            blockers.append('account_canTrade_not_true')
        if cfg.get('dualSidePosition') is not False:
            blockers.append('hedge_mode_or_unknown_requires_review')
        if cfg.get('multiAssetsMargin') is not False:
            blockers.append('multi_asset_margin_or_unknown_requires_review')
        active = sum(number(r['positionAmt'], signed=True) != 0 for r in positions)
        if active or orders or algos:
            blockers.append('account_not_flat_or_has_open_orders')
        if available < positive(c['allocated_equity']):
            blockers.append('insufficient_available_USDC_for_145_allocation')
        leverage = settings.get('leverage')
        if type(leverage) is not int or leverage <= 0 or settings.get('marginType') not in ('CROSSED', 'ISOLATED'):
            raise PreflightError('Unknown leverage or margin mode')
        account_summary = dict(margin_asset=c['margin_asset'], wallet_balance=str(wallet),
                               available_balance=str(available), active_position_count=active,
                               open_order_count=len(orders), open_algo_order_count=len(algos),
                               current_leverage=leverage, margin_type=settings['marginType'],
                               maker_fee_rate=str(number(fees['makerCommissionRate'])),
                               taker_fee_rate=str(number(fees['takerCommissionRate'])),
                               api_trade_permission='not_proven_by_GET; no test/live order submitted')
    quote = client.get('/fapi/v1/ticker/bookTicker', symbol=c['symbol'])
    if not isinstance(quote, dict) or quote.get('symbol') != c['symbol'] or type(quote.get('time')) is not int:
        raise PreflightError('Missing or wrong-symbol BBO timestamp')
    age = int(time.time() * 1000) + client.offset - quote['time']
    if not -1000 <= age <= 10000:
        raise PreflightError('BBO stale/future; retry preflight later')
    geometry = minimum_geometry(spec, quote['bidPrice'], quote['askPrice'], c['allocated_equity'],
                                c['allowed_risk_fractions'], c['tp1_fraction'], c['max_notional_multiple'])
    return dict(schema_version=1, stage='read_only_preflight', real_orders=False, live_ready=False,
                read_only_checks='public_only' if public_only else ('blocked' if blockers else 'pass'),
                checked_at=datetime.now(timezone.utc).isoformat(), symbol=c['symbol'], contract=c['contract'],
                allocated_equity=c['allocated_equity'], account=account_summary, blockers=blockers,
                quote=dict(bid=quote['bidPrice'], ask=quote['askPrice'], age_ms=age), minimum_geometry=geometry,
                remaining_gates=['approved actual-entry/stop/partial rounding and margin policy',
                                 'durable real-order adapter, native stop and exchange reconciliation',
                                 'disconnect/restart/partial-fill/unknown-response recovery tests',
                                 'single live account writer, alerts, backup/restore and explicit activation'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--api-key-file', type=Path)
    p.add_argument('--private-key-file', type=Path)
    p.add_argument('--passphrase-file', type=Path)
    p.add_argument('--public-only', action='store_true')
    a = p.parse_args()
    client = BinanceFuturesReadOnly(api_key_file=a.api_key_file, private_key_file=a.private_key_file,
                                  passphrase_file=a.passphrase_file)
    try:
        config = load_config(a.config)
        if not a.public_only:
            client.load_credentials()
        result = preflight(client, config, public_only=a.public_only)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 2 if result['blockers'] else 0
    except PreflightError as error:
        print(json.dumps(dict(stage='read_only_preflight', live_ready=False, real_orders=False, error=str(error))))
        return 2
    except (KeyError, TypeError, ValueError, OSError, ArithmeticError):
        print(json.dumps(dict(stage='read_only_preflight', live_ready=False, real_orders=False,
                              error='Malformed/missing configuration or venue response; details redacted')))
        return 2
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
