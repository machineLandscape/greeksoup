"""Read-only, current-weight portfolio risk from dated public adjusted closes."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
import statistics
import urllib.parse
import urllib.request

def history(symbol):
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + urllib.parse.quote(symbol, safe='') + '?interval=1d&range=2y'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=25) as response:
        item = json.load(response)['chart']['result'][0]
    if item.get('meta', {}).get('symbol', '').upper() != symbol.upper():
        raise ValueError('Historical symbol mismatch: ' + symbol)
    prices = (item.get('indicators', {}).get('adjclose') or [{}])[0].get('adjclose')
    if not prices:
        raise ValueError('Adjusted history unavailable: ' + symbol)
    now = datetime.now(timezone.utc)
    result = {}
    for timestamp, price in zip(item.get('timestamp', []), prices):
        date = datetime.fromtimestamp(timestamp, timezone.utc).date()
        # Never treat an unfinished US daily bar as a completed daily close.
        if date > now.date() or (date == now.date() and now.hour < 21):
            continue
        if isinstance(price, (int, float)) and math.isfinite(price) and price > 0:
            result[date.isoformat()] = float(price)
    return result

def covariance(a, b):
    ma, mb = statistics.mean(a), statistics.mean(b)
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/(len(a)-1)

def stats(a, benchmark):
    var, bv = covariance(a,a), covariance(benchmark,benchmark)
    cov = covariance(a,benchmark)
    return {'beta': cov/bv if bv>0 else None, 'vol': math.sqrt(max(0,var)*252)*100,
            'corr': cov/math.sqrt(var*bv) if var>0 and bv>0 else None}

def drawdown(returns):
    level = peak = 1.0
    worst = 0.0
    for r in returns:
        level *= 1+r
        peak = max(peak,level)
        worst = min(worst,level/peak-1)
    return worst*100

def calculate(holdings, intelligence, histories):
    if holdings.get('snapshot_id') != intelligence.get('holdings_snapshot_id'):
        raise ValueError('Holdings and exposure snapshots differ')
    rows = intelligence['risk']['positions']
    actual = {(p['account_id'],p['instrument_id']):p for p in holdings['positions']}
    if len(rows) != len(actual) or {(p['account_id'],p['instrument_id']) for p in rows} != set(actual):
        raise ValueError('Risk rows do not match actual holdings')
    grouped = {}
    for p in rows:
        h = actual[p['account_id'],p['instrument_id']]
        if h.get('currency','USD') != 'USD' or p.get('market_value') is None or p.get('mark_status') != 'COMPLETE':
            raise ValueError('Missing, partial or non-USD position mark')
        if float(h['quantity']) != float(p['quantity']):
            raise ValueError('Position quantities differ')
        value = float(p['market_value'])
        if not math.isfinite(value):
            raise ValueError('Invalid position mark')
        grouped[p['instrument_id']] = grouped.get(p['instrument_id'],0)+value
    if not holdings.get('accounts') or any(a.get('cash') is None or a.get('currency','USD')!='USD' for a in holdings['accounts']):
        raise ValueError('Cash unavailable or non-USD')
    cash = sum(float(a['cash']) for a in holdings['accounts'])
    nav = cash+sum(grouped.values())
    if not math.isfinite(nav) or nav<=0:
        raise ValueError('Portfolio value must be positive')
    active = [s for s,v in grouped.items() if v!=0]
    bench = histories.get('^GSPC') or {}
    if len(bench)<41:
        raise ValueError('Benchmark history is unavailable or too short')
    dates = sorted(bench)[-253:]
    # Use one contiguous suffix of the benchmark grid. Do not bridge missing
    # trading dates or substitute zero returns for missing positions.
    start = 0
    for index,date in enumerate(dates):
        if any(date not in histories.get(symbol,{}) for symbol in active):
            start=index+1
    dates=dates[start:]
    if len(dates)<41:
        missing=[s for s in active if len(histories.get(s,{}))<41 or not dates]
        raise ValueError('Fewer than 40 common daily returns; incomplete histories: '+', '.join(missing))
    def returns(series):
        return [series[b]/series[a]-1 for a,b in zip(dates,dates[1:])]
    benchmark=returns(bench)
    series={s:returns(histories[s]) for s in active}
    book_returns=[sum(grouped[s]/nav*series[s][j] for s in active) for j in range(len(benchmark))]
    positions=[]
    for s,value in sorted(grouped.items()):
        metric=stats(series[s],benchmark) if s in series else {'beta':None,'vol':None,'corr':None}
        positions.append({'code':s,'name':'','kinds':['EQ'],'exposure':value,'weight':value/nav*100,
                          **metric,'mdd':drawdown(series[s]) if s in series else None,
                          'n':len(benchmark) if s in series else 0,'low_sample':len(benchmark)<120 or s not in series})
    codes=[p['code'] for p in positions]
    matrix=[[stats(series[a],series[b])['corr'] if a in series and b in series else None for b in codes] for a in codes]
    metrics=stats(book_returns,benchmark)
    book={'key':'meridian_portfolio','label':'Meridian actual portfolio','currency':'$','bench':'^GSPC',
          'nav':nav,'cash':cash,'gross':sum(abs(v) for v in grouped.values()),**metrics,'mdd':drawdown(book_returns),
          'positions':positions,'matrix':{'codes':codes,'vals':matrix},
          'sectors':[{'name':'Unclassified','exposure':sum(grouped.values()),'pct':sum(grouped.values())/nav*100}],
          'note':'Current-weight adjusted-close model. Options and Principal excluded. No missing returns filled with zero.'}
    return {'at':datetime.now(timezone.utc).timestamp(),'data':{'books':[book],
            'window':str(len(benchmark))+' common daily returns, '+dates[0]+' through '+dates[-1]+'; adjusted closes',
            'history_start':dates[0],'history_end':dates[-1],
            'source':'Yahoo Finance adjusted daily closes; current Meridian exposures',
            'holdings_snapshot_id':holdings['snapshot_id'],'stale':False}}

def rebuild(holdings,intelligence):
    symbols={p['instrument_id'] for p in intelligence['risk']['positions'] if p.get('market_value') is not None and float(p['market_value'])!=0}|{'^GSPC'}
    histories={}
    print('\nRecalculating risk from daily price history...')
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks={pool.submit(history,s):s for s in sorted(symbols)}
        for task in as_completed(tasks):
            symbol=tasks[task]
            try:
                histories[symbol]=task.result()
            except Exception as error:
                print('History unavailable for '+symbol+': '+str(error))
    return calculate(holdings,intelligence,histories)
