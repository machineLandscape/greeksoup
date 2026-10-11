"""Manual free stock, ETF and exact standard-option quote refresh for Meridian."""
from __future__ import annotations
import argparse
import copy
import ctypes
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import urllib.parse
import urllib.request
import risk_refresh
from concurrent.futures import ThreadPoolExecutor, as_completed

def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def quote(symbol, expected_type=None):
    if not re.fullmatch(r'[A-Z0-9.\-^=]{1,32}', symbol):
        raise ValueError('Unsupported symbol')
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + urllib.parse.quote(symbol, safe='') + '?interval=1d&range=5d'
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    meta = result['chart']['result'][0]['meta']
    if expected_type and meta.get('instrumentType') != expected_type:
        raise ValueError('Provider instrument type does not match')
    price, stamp = meta.get('regularMarketPrice'), meta.get('regularMarketTime')
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        raise ValueError('No valid price')
    if not isinstance(stamp, (int, float)) or stamp > datetime.now(timezone.utc).timestamp()+300:
        raise ValueError('No valid quote time')
    if meta.get('currency') != 'USD':
        raise ValueError('Quote is not in USD')
    if meta.get('symbol', '').upper() != symbol.upper():
        raise ValueError('Provider symbol does not match')
    return {'price': str(price), 'as_of': datetime.fromtimestamp(stamp, timezone.utc).isoformat(), 'source': 'Yahoo Finance chart / regular-market quote'}

def option_symbol(option):
    """Only explicitly identified standard contracts; no adjusted-root guesses."""
    root = option.get('underlying', '')
    if not re.fullmatch(r'[A-Z]{1,6}', root):
        raise ValueError('Adjusted or unidentified contract: broker quote required')
    count = Decimal(str(option.get('contracts', 0)))
    if not count.is_finite() or count <= 0 or count != count.to_integral_value():
        raise ValueError('Contract quantity unavailable')
    multiplier = Decimal(str(option.get('multiplier') or 0))
    shares = Decimal(str(option.get('shares_covered') or 0))
    if multiplier not in (0, 100) or (multiplier != 100 and shares != count * 100):
        raise ValueError('Standard contract deliverable not verified')
    if option.get('position_side') not in ('LONG', 'SHORT'):
        raise ValueError('Position direction unavailable')
    kind = {'CALL': 'C', 'PUT': 'P'}.get(option.get('option_type'))
    if not kind:
        raise ValueError('Option type unavailable')
    expiry = datetime.strptime(option.get('expiration', ''), '%Y-%m-%d')
    strike = Decimal(str(option.get('strike', 0))) * 1000
    if not strike.is_finite() or strike <= 0 or strike != strike.to_integral_value() or strike > 99999999:
        raise ValueError('Invalid strike')
    compact = root + expiry.strftime('%y%m%d') + kind + str(int(strike)).zfill(8)
    broker = option.get('broker_symbol', '').split()
    if broker:
        if len(broker) == 1:
            if broker[0] != compact:
                raise ValueError('Broker contract identity conflicts')
        elif len(broker) != 4 or broker[:3] != [root, expiry.strftime('%y%m%d'), kind] or Decimal(broker[3]) * 1000 != strike:
            raise ValueError('Broker contract identity conflicts')
    return compact

def revalue_options(holdings, fetch=None):
    fetch = fetch or quote
    out, updated, report = copy.deepcopy(holdings), 0, []
    cache = {}
    for option in out.get('supplemental', {}).get('option_contracts', []):
        label = option.get('broker_symbol') or option.get('underlying') or 'Unknown option'
        try:
            if option.get('status') in ('CLOSED', 'EXPIRED', 'ASSIGNED'):
                raise ValueError('Contract is recorded as closed')
            symbol = option_symbol(option)
            if symbol not in cache:
                try:
                    cache[symbol] = fetch(symbol, expected_type='OPTION')
                except Exception as error:
                    cache[symbol] = error
            q = cache[symbol]
            if isinstance(q, Exception):
                raise q
            quote_date = datetime.fromisoformat(q['as_of'].replace('Z', '+00:00'))
            old_date = option.get('quote_as_of') or option.get('as_of')
            if old_date:
                prior = datetime.fromisoformat(old_date.replace('Z', '+00:00'))
                if prior.tzinfo is None:
                    prior = prior.replace(tzinfo=timezone.utc)
                if quote_date < prior:
                    raise ValueError('Available option trade is older than the saved mark')
            price = Decimal(q['price'])
            if not price.is_finite() or price <= 0:
                raise ValueError('No valid option trade price')
            sign = -1 if option['position_side'] == 'SHORT' else 1
            option['mark_price'] = str(price)
            option['market_value'] = str(sign * price * Decimal(str(option['contracts'])) * 100)
            option['quote_as_of'] = q['as_of']
            option['quote_source'] = 'Yahoo Finance exact option contract / last reported regular-market trade'
            option['quote_observed_at'] = datetime.now(timezone.utc).isoformat()
            option['quoted_contract_symbol'] = symbol
            option['valuation_method'] = 'DATED_OPTION_LAST_TRADE_X_100_X_CONTRACTS; SIGNED_BY_POSITION_SIDE'
            age = (datetime.now(timezone.utc) - quote_date).total_seconds() / 86400
            option['price_update_status'] = 'DATED_LAST_TRADE' if age <= 3 else 'STALE_LAST_TRADE'
            option.pop('price_update_reason', None)
            updated += 1
            report.append(label + ': $' + str(price) + ' | last trade ' + q['as_of'] + (' | STALE' if age > 3 else ''))
        except Exception as error:
            option['price_update_status'] = 'NOT_UPDATED'
            option['price_update_reason'] = str(error)
            report.append(label + ': NOT UPDATED (' + str(error) + ')')
    return out, updated, report

def revalue(holdings, intelligence, quotes):
    out = copy.deepcopy(intelligence)
    old = {(p['account_id'], p['instrument_id']): p for p in out.get('risk', {}).get('positions', [])}
    rows, updated = [], 0
    for p in holdings['positions']:
        identity = (p['account_id'], p['instrument_id'])
        prior = old.get(identity, {})
        q = quotes.get(p['instrument_id']) if p.get('asset_type') in ('STOCK', 'ETF') and p.get('currency', 'USD') == 'USD' else None
        prior_dates = [datetime.fromisoformat(d.replace('Z', '+00:00')) for d in prior.get('mark_as_of', []) if d]
        # Do not replace a later dated mark with an older provider quote.
        if q and prior_dates and datetime.fromisoformat(q['as_of']) < max(prior_dates):
            q = None
        if q:
            row = {'account_id': identity[0], 'instrument_id': identity[1], 'quantity': p['quantity'],
                   'market_value': str(Decimal(str(p['quantity'])) * Decimal(q['price'])),
                   'mark_status': 'COMPLETE', 'comparison_status': ['DATED_PUBLIC_MARK'],
                   'mark_as_of': [q['as_of']], 'source_ref': q['source']}
            updated += 1
        elif prior and Decimal(str(prior.get('quantity', 'NaN'))) == Decimal(str(p['quantity'])):
            row = copy.deepcopy(prior)
            row['comparison_status'] = ['CARRIED_FORWARD_DATED_MARK']
        else:
            row = {'account_id': identity[0], 'instrument_id': identity[1], 'quantity': p['quantity'],
                   'market_value': None, 'mark_status': 'MISSING', 'comparison_status': ['UNAVAILABLE'], 'mark_as_of': []}
        rows.append(row)
    for row, position in zip(rows, holdings['positions']):
        row['currency'] = position.get('currency')
    currencies = {r.get('currency') for r in rows}
    comparable = (bool(rows) and None not in currencies and len(currencies) == 1
                  and all(r.get('market_value') is not None and r.get('mark_status') == 'COMPLETE'
                          and len(r.get('mark_as_of', [])) == 1 for r in rows)
                  and len({d for r in rows for d in r.get('mark_as_of', [])}) == 1)
    total = sum((Decimal(r['market_value']) for r in rows if r['market_value'] is not None), Decimal(0)) if len(currencies) == 1 and None not in currencies and any(r['market_value'] is not None for r in rows) else None
    for r in rows:
        r['weight_pct'] = str(Decimal(r['market_value']) * 100 / total) if comparable and total and total > 0 else None
    out['holdings_snapshot_id'], out['holdings_as_of'] = holdings['snapshot_id'], holdings['as_of']
    out['risk'] = {'method': 'DATED_POSITION_EXPOSURE_CONCENTRATION', 'marked_market_value': str(total) if total is not None else None,
                   'coverage_status': 'COMPLETE_DATED_POSITIONS' if comparable else 'PARTIAL_OR_INCOMPARABLE',
                   'weight_scope': 'SUPPLIED_POSITIONS_ONLY_EXCLUDES_CASH_AND_SUPPLEMENTS',
                   'full_portfolio_risk_available': False,
                   'marked_positions': sum(r['market_value'] is not None for r in rows),
                   'missing_or_partial_positions': sum(r['market_value'] is None for r in rows), 'positions': rows}
    return out, updated

def history_sample(h, i, stamp, benchmark):
    if any(a.get('currency') != 'USD' for a in h.get('accounts', [])) or any(p.get('currency') != 'USD' for p in h['positions']):
        return None  # No implicit currency conversion.
    accounts = []
    for a in h['accounts']:
        positions = [p for p in i['risk']['positions'] if p['account_id'] == a['account_id']]
        options = [o for o in h.get('supplemental', {}).get('option_contracts', []) if o['account_id'] == a['account_id']]
        if a.get('cash') is None or any(p['market_value'] is None for p in positions) or any(o.get('market_value') is None for o in options):
            return None  # Never append a deceptively complete total.
        securities = sum((Decimal(p['market_value']) for p in positions), Decimal(0))
        option_value = sum((Decimal(str(o['market_value'])) for o in options), Decimal(0))
        cash = Decimal(str(a['cash']))
        dates = set(d for p in positions for d in p.get('mark_as_of', []))
        dates.update(o.get('quote_as_of') or o.get('as_of') for o in options)
        accounts.append({'account_id': a['account_id'], 'securities_value': str(securities), 'option_value': str(option_value),
                         'cash': str(cash), 'account_value': str(securities+option_value+cash),
                         'mark_as_of': sorted(d for d in dates if d), 'valuation_status': 'DATED_STOCK_AND_OPTION_MARKS; CHECK_SOURCE_DATES; CASH_UNCHANGED'})
    principal = h.get('supplemental', {}).get('principal') or {}
    if principal.get('statement_total') is not None:
        value = str(principal['statement_total'])
        accounts.append({'account_id': principal.get('account_id', 'principal_roth_401k'), 'securities_value': value,
                         'cash': '0', 'option_value': '0', 'account_value': value, 'mark_as_of': [principal.get('statement_as_of')],
                         'valuation_status': 'HISTORICAL_STATEMENT_CARRIED_IN_TOTAL'})
    sample = {'recorded_at': stamp, 'holdings_snapshot_id': h['snapshot_id'], 'holdings_as_of': h['as_of'],
              'accounts': accounts, 'total_portfolio_value': str(sum(Decimal(a['account_value']) for a in accounts)),
              'currency': 'USD', 'live_trade_authority': False}
    if benchmark:
        sample['benchmark'] = {'symbol': 'VOO', 'name': 'Vanguard S&P 500 ETF', 'price': benchmark['price'],
                               'retrieved_at': stamp, 'quote_time': benchmark['as_of'], 'source': benchmark['source']}
    return sample

def refresh(desk):
    operations = desk / 'data/research/plugins/meridian-operations'
    hp, ip, vp = [operations / x for x in ('holdings.json', 'intelligence.json', 'valuation_history.json')]
    originals = {p: p.read_bytes() for p in (hp, ip, vp)}
    h, i, history = load(hp), load(ip), load(vp)
    symbols = sorted({p['instrument_id'] for p in h['positions'] if p.get('asset_type') in ('STOCK', 'ETF') and p.get('currency', 'USD') == 'USD'} | {'VOO'})
    print('Refreshing stocks, ETFs and exact standard option contracts using free public quotes.\nNo broker connection, trades, subscriptions, or paid requests.\n')
    quotes = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = {pool.submit(quote, symbol): symbol for symbol in symbols}
        for future in as_completed(tasks):
            symbol = tasks[future]
            try:
                quotes[symbol] = future.result()
                print(symbol + ': $' + quotes[symbol]['price'] + ' | ' + quotes[symbol]['as_of'])
            except Exception as error:
                print(symbol + ': NOT UPDATED (' + str(error) + ')')
    out, updated = revalue(h, i, quotes)
    print('\nOPTION CONTRACTS (last reported trades, not executable bid/ask marks)')
    option_holdings, option_count, option_report = revalue_options(h)
    for line in option_report:
        print(line)
    if not updated and not option_count:
        raise RuntimeError('No usable quotes received. Portfolio files were not changed.')
    h = option_holdings
    stamp = datetime.now(timezone.utc).isoformat()
    sample = history_sample(h, out, stamp, quotes.get('VOO'))
    analytics_path = operations.parent / 'meridian-risk' / 'analytics.json'
    analytics = None
    risk_error = None
    if analytics_path.parent.is_dir():
        originals[analytics_path] = analytics_path.read_bytes() if analytics_path.exists() else None
        try:
            analytics = risk_refresh.rebuild(h, out)
        except Exception as error:
            risk_error = str(error)
            print('RISK NOT UPDATED: '+risk_error)
    if sample:
        history.setdefault('samples', []).append(sample)
    backup = operations / ('price-refresh-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
    backup.mkdir()
    (backup / 'quotes.json').write_text(json.dumps({'retrieved_at': stamp, 'quotes': quotes}, indent=2), encoding='utf-8')
    updates = {ip: out, hp: h}
    if analytics is not None:
        updates[analytics_path] = analytics
    if sample:
        updates[vp] = history
    for p in updates:
        if originals[p] is not None:
            (backup / p.name).write_bytes(originals[p])
    for p, original in originals.items():
        if (p.read_bytes() if p.exists() else None) != original:
            raise RuntimeError('Portfolio changed during refresh. No update written; try again.')
    written = []
    try:
        for p, data in updates.items():
            temporary = p.with_suffix('.refresh.tmp')
            temporary.write_text(json.dumps(data, indent=2, allow_nan=False), encoding='utf-8')
            os.replace(temporary, p)
            written.append(p)
    except Exception:
        for p in written:
            if originals[p] is None:
                p.unlink()
            else:
                p.write_bytes(originals[p])
        raise
    print('\nUpdated ' + str(updated) + ' of ' + str(len(h['positions'])) + ' account positions.')
    print('Updated ' + str(option_count) + ' of ' + str(len(h.get('supplemental', {}).get('option_contracts', []))) + ' option records.')
    print('Option prices are dated last trades, NOT live bid/ask midpoints; illiquid contracts can have old trades.')
    print('Other positions retain their original dates or show unavailable. Quotes may be delayed.')
    print('Weekends use the latest available regular-session price, not a new weekend price.')
    print('Holdings quantities, contract counts, premiums received, cash and Principal values were NOT changed.')
    print('Holdings snapshot: ' + h['as_of'] + '. Missing trades still need reconciliation.')
    print('Portfolio history ' + ('updated.' if sample else 'not appended because some values are unavailable.'))
    if analytics:
        print('Risk Board recalculated: '+analytics['data']['window'])
        print('Risk model uses current stock/ETF weights plus cash; supplemental options and Principal remain excluded.')
    else:
        print('Risk Board not updated: '+(risk_error or 'Risk Board folder not installed')+'. Unmatched historical metrics remain unavailable.')
    print('Refresh the Portfolio or Risk Board browser tab with Ctrl+F5.')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desk', type=Path, default=Path.home() / 'GreekSoup-Meridian')
    parser.add_argument('--pause', action='store_true')
    args = parser.parse_args()
    handle = None
    try:
        if os.name == 'nt':
            kernel = ctypes.windll.kernel32
            kernel.CreateMutexW.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.CreateMutexW(None, False, 'Local\\MeridianIntradayValuation')
            if not handle or kernel.WaitForSingleObject(handle, 0) not in (0, 0x80):
                raise RuntimeError('Another portfolio refresh is running; try again later.')
        refresh(args.desk)
    except Exception as error:
        print('\nREFRESH FAILED: ' + str(error))
        return 1
    finally:
        if handle:
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
        if args.pause:
            input('\nPress Enter to close this window...')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
