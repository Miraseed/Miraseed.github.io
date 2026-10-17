# -*- coding: utf-8 -*-
"""
K线复盘数据生成：每个标的的日K + 历史「IV 分位卖沽信号日」标记。

输出 docs/kline.json：
{
  "updated": "2026-10-15 10:00:00",
  "products": {
    "科创50ETF": {
      "th": 0.65, "iv_th": null,
      "bars":  [[date, open, high, low, close], ...],   # 最近 ~500 交易日
      "pct":   [0.42, ...],                              # 与 bars 对齐的历史 IV 分位（0-1，None=不足）
      "sig":   [0, 1, 0, ...]                            # 1 = 该日 IV 分位 ≥ 阈值（卖沽信号日）
    }, ...
  }
}

口径与回测/看板一致：
- 历史每日 ATM IV 取 atm_by_month.csv 按 DTE 优先级选月（创业板用 159915 代理序列）
- 分位 = 滚动 250 交易日（不含当日，min_periods=100）
- 信号日 = pct ≥ pct_th 且（如配置）atm_iv ≥ iv_th —— 即当日看板会亮 ★ 的日子

标的价格来源（统一为真实 OHLC 日线，且能取到最新交易日）：
- ETF（上证50/沪深300/中证500/科创50/创业板）：新浪 CN_MarketDataService 日线
- 中证1000 指数：新浪 CN_MarketDataService 日线（sh000852）
- 铁矿石：新浪期货主连日线（i0 = 铁矿石主连）
面板仅用于提供 IV 序列（atm_by_month / 159915_ivproxy）。
"""
import os, json, datetime
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
# 路径候选：Pages 部署根目录(code/) → 本地仓库(web/github_action → 仓库根/code)
_p = HERE
CODE = None
for _ in range(3):
    if os.path.isdir(os.path.join(_p, 'code', 'panel')):
        CODE = os.path.join(_p, 'code')
        break
    _p = os.path.dirname(_p)
if CODE is None:
    CODE = os.path.join(os.path.dirname(HERE), 'code')
PANEL = os.path.join(CODE, 'panel')
OUT = os.path.join(HERE, 'docs', 'kline.json')
OUT_TAIL = os.path.join(HERE, 'docs', 'kline_tail.json')
TAILN = 8          # 尾段根数（实时刷新用，前端 base+tail 合并）
CST = datetime.timezone(datetime.timedelta(hours=8))

LOOKBACK, MINP, NBARS = 250, 100, 1500
START_DATE = '2020-11-16'   # 科创50ETF 上市首日，复盘起点

# 与 monitor/intraday POOL 一致的阈值（复盘用当前阈值回算历史信号日）
# prod 为 ATM 面板里的 product 键；sina 为新浪日线代码（ETF/指数）
PRODUCTS = {
    '科创50ETF':    dict(prod='科创50ETF',  sina='sh588000', pct_th=0.65, iv_th=None),
    '创业板ETF':    dict(prod='159915',    sina='sz159915', pct_th=0.55, iv_th=None),
    '上证50ETF':    dict(prod='上证50ETF',  sina='sh510050', pct_th=0.80, iv_th=None),
    '沪深300ETF':   dict(prod='沪深300ETF', sina='sh510300', pct_th=0.75, iv_th=None),
    '中证500ETF':   dict(prod='中证500ETF', sina='sh510500', pct_th=0.85, iv_th=None),
    '中证1000股指':  dict(prod='MO',        sina='sh000852', pct_th=0.75, iv_th=0.30),
    '铁矿石期权':    dict(prod='i',         sina='i0',       pct_th=0.75, iv_th=None),
}


def _dte_rank(dte):
    if 30 <= dte <= 45: return (0, abs(dte - 37))
    if 45 < dte <= 60:  return (1, dte)
    if 14 <= dte < 30:  return (2, abs(dte - 22))
    if dte > 60:        return (3, dte)
    return (4, dte)


def atm_series(prod):
    """每日口径月 ATM IV（DTE 优先级选月）。创业板返回 None（调用方用代理）。"""
    fp = os.path.join(PANEL, 'atm_by_month.csv')
    d = pd.read_csv(fp)
    d = d[d['product'] == prod]
    if not len(d):
        return None
    d['date'] = pd.to_datetime(d['date'])
    picks = []
    for day, grp in d.groupby('date'):
        best, best_r = None, (99, 9999)
        for iv, dte in zip(grp['atm_iv'], grp['dte']):
            if np.isfinite(iv) and np.isfinite(dte):
                r = _dte_rank(float(dte))
                if r < best_r:
                    best_r, best = r, float(iv)
        if best is not None:
            picks.append((day, best))
    s = pd.Series({k: v for k, v in picks}).sort_index()
    return s if len(s) >= 20 else None


def rolling_pct(series):
    v = series.values
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        w = v[max(0, i - LOOKBACK):i]
        w = w[np.isfinite(w)]
        if len(w) >= MINP and np.isfinite(v[i]):
            out[i] = float((w < v[i]).mean())
    return pd.Series(out, index=series.index)


def _sina_futures_kline(symbol='i0', cache_name='i0_daily.csv'):
    """新浪期货主连日线（真实 OHLC），gbk 编码 + JSONP 正则提取。
    返回 date/open/high/low/close df；失败返回 None。"""
    fp = os.path.join(PANEL, cache_name)
    if os.path.exists(fp):
        d = pd.read_csv(fp, parse_dates=['date'])
        if len(d) > 100:
            return d.set_index('date')
    import urllib.request, re as _re
    url = ('https://stock2.finance.sina.com.cn/futures/api/jsonp.php/'
           f'var%20_i=/InnerFuturesNewService.getDailyKLine?symbol={symbol}')
    req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn/',
                                               'User-Agent': 'Mozilla/5.0'})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read().decode('gbk', 'ignore')
    except Exception:
        return None
    m = _re.search(r'\((\[.*\])\)', raw, _re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(1))
    except Exception:
        return None
    if not arr:
        return None
    rows = []
    for r in arr:
        try:
            rows.append((pd.Timestamp(r['d']), float(r['o']), float(r['h']),
                         float(r['l']), float(r['c'])))
        except Exception:
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close'])
    df = df.sort_values('date').set_index('date').drop_duplicates()
    _today = pd.Timestamp(datetime.datetime.now(CST).date())
    df_cache = df[df.index < _today]
    if len(df_cache) > 100:
        df_cache.to_csv(fp, index_label='date')
    return df



def _sina_index_kline(symbol, cache_name, datalen=1023):
    """新浪指数/ETF 日线（真实 OHLC），缓存 CSV。失败返回 None。"""
    fp = os.path.join(PANEL, cache_name)
    if os.path.exists(fp):
        d = pd.read_csv(fp, parse_dates=['date'])
        if len(d) > 100:
            return d.set_index('date')
    import urllib.request
    url = (f'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService'
           f'.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}')
    req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn/',
                                               'User-Agent': 'Mozilla/5.0'})
    try:
        arr = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception:
        return None
    if not arr:
        return None
    df = pd.DataFrame([(pd.Timestamp(r['day']), float(r['open']), float(r['high']),
                        float(r['low']), float(r['close'])) for r in arr],
                      columns=['date', 'open', 'high', 'low', 'close'])
    df = df.sort_values('date').set_index('date').drop_duplicates()
    # 当日盘中 bar 不落缓存（避免未完成 K 线被永久固化）
    _today = pd.Timestamp(datetime.datetime.now(CST).date())
    df_cache = df[df.index < _today]
    if len(df_cache) > 100:
        df_cache.to_csv(fp, index_label='date')
    return df


def _east_futures_kline(secid='114.im', cache_name='im_daily.csv', lmt=1500):
    """东财期货主连日线（铁矿石主连 114.im 与新浪 i0 同源），自带当日实时 bar。
    带 3 次重试；失败回退新浪主连日线（不含当日，但至少保证历史完整）。
    返回 date/open/high/low/close df；失败返回 None。"""
    import urllib.request, time as _t
    fp = os.path.join(PANEL, cache_name)
    rows = None
    for attempt in range(3):
        url = (f'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}'
               f'&klt=101&fqt=0&lmt={lmt}&end=20500101&fields1=f1,f2,f3'
               '&fields2=f51,f52,f53,f54,f55,f56')
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                                   'Referer': 'https://quote.eastmoney.com/'})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=30).read())
            kl = d.get('data', {}).get('klines') or []
            if kl:
                tmp = []
                for line in kl:      # date,open,close,high,low,volume
                    p = line.split(',')
                    try:
                        tmp.append((pd.Timestamp(p[0]), float(p[1]), float(p[3]),
                                    float(p[4]), float(p[2])))
                    except Exception:
                        continue
                if tmp:
                    rows = tmp
                    break
        except Exception:
            pass
        _t.sleep(2 + attempt * 2)
    if rows is None:
        print('  [诊断] 东财 114.im 日线 3 次重试均失败')
    src = '东财'
    if rows is None:
        # 回退：新浪主连日线（历史完整，但盘中不含当日）
        k = _sina_futures_kline('i0', '_i0_fallback.csv')
        if k is None:
            if os.path.exists(fp):   # 再退一步：用缓存
                return pd.read_csv(fp, parse_dates=['date']).set_index('date')
            return None
        return k
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close'])
    df = df.sort_values('date').set_index('date').drop_duplicates()
    _today = pd.Timestamp(datetime.datetime.now(CST).date())
    df_cache = df[df.index < _today]   # 当日实时 bar 不落缓存
    if len(df_cache) > 100:
        df_cache.to_csv(fp, index_label='date')
    return df


def ohlc_of(name):
    """返回 (df[date,open,high,low,close], iv_series)。
    K 线价格统一走新浪真实 OHLC 日线；IV 序列来自面板。"""
    cfg = PRODUCTS[name]
    prod, sina = cfg['prod'], cfg['sina']
    if prod == '159915':
        k = _sina_index_kline(sina, '159915_daily_ohlc.csv', 1500)
        iv = pd.read_csv(os.path.join(PANEL, '159915_ivproxy.csv'),
                         parse_dates=['date']).set_index('date')['iv_proxy']
        return k, iv
    if prod == 'MO':
        # 中证1000 指数真实 OHLC：新浪日线（腾讯接口对指数返回 501）
        k = _sina_index_kline(sina, '000852_daily.csv', 1500)
        return k, atm_series('MO')
    if prod == 'i':
        # 铁矿石主连日线（东财 114.im，与新浪 i0 同源且盘中自带当日实时 bar）
        k = _east_futures_kline('114.im', 'im_daily.csv')
        return k, atm_series('i')
    # ETF：新浪日线真实 OHLC
    k = _sina_index_kline(sina, f'{sina}_daily.csv', 1500)
    return k, atm_series(prod)


def _sina_realtime(names):
    """批量取新浪实时行情，拼当日盘中 bar。
    返回 {name: dict(open, high, low, last, date)}；取不到的标的不在返回值里。"""
    import urllib.request
    qs, q2n = [], {}
    for n in names:
        cfg = PRODUCTS[n]
        q = cfg['sina'] if cfg['prod'] != 'i' else 'nf_i0'
        qs.append(q)
        q2n[q] = n
    url = 'https://hq.sinajs.cn/list=' + ','.join(qs)
    req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn/',
                                               'User-Agent': 'Mozilla/5.0'})
    try:
        raw = urllib.request.urlopen(req, timeout=15).read().decode('gbk', 'ignore')
    except Exception:
        return {}
    out = {}
    for line in raw.strip().splitlines():
        if '=' not in line:
            continue
        q = line.split('hq_str_')[1].split('=')[0].strip()
        body = line.split('=', 1)[1].strip().strip(';').strip('"')
        f = body.split(',') if body else []
        n = q2n.get(q)
        if not n:
            continue
        try:
            if q.startswith('nf_'):
                # 期货：名称,开,高,低,昨结,买价,卖价,持仓?,...,现价,日期,时间
                if len(f) < 10 or not f[1]:
                    continue
                last = next((x for x in (f[8], f[3]) if x and float(x) > 0), None)
                d = dict(open=float(f[1]), high=float(f[2]), low=float(f[3]),
                         last=float(last), date=(f[-2] if len(f) >= 3 else ''))
            else:
                # 股票/ETF/指数：名称,今开,昨收,现价,最高,最低,...,日期,时间
                if len(f) < 32 or not f[1]:
                    continue
                d = dict(open=float(f[1]), high=float(f[4]), low=float(f[5]),
                         last=float(f[3]), date=f[30])
            if d['last'] and d['last'] > 0:
                out[n] = d
        except Exception:
            continue
    return out


def _today_iv_from_dashboard():
    """从看板 data.json 取各标的实时 ATM IV（仅当看板是今日数据才有效）。
    返回 {标的: iv}。"""
    fp = os.path.join(HERE, 'docs', 'data.json')
    try:
        d = json.load(open(fp, encoding='utf-8'))
    except Exception:
        return {}
    today = str(datetime.datetime.now(CST).date())
    stamp = str(d.get('last_run') or '')[:10]
    if stamp != today:
        return {}   # 看板数据不是今日的（如本地旧快照），不采用
    out = {}
    for r in d.get('rows', []):
        try:
            if r.get('ATM_IV') and r.get('状态') == '正常':
                out[r['标的']] = float(r['ATM_IV'])
        except Exception:
            continue
    return out


def build():
    out = {'updated': datetime.datetime.now(CST).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S'),
           'products': {}}
    today = pd.Timestamp(datetime.datetime.now(CST).date())
    # 当日实时：盘后日线缓存可能已含今日；盘中则用实时接口拼当日 bar + 看板实时 IV
    rt = _sina_realtime(list(PRODUCTS))
    dash_iv = _today_iv_from_dashboard()
    for name, cfg in PRODUCTS.items():
        try:
            k, iv = ohlc_of(name)
            if k is None or iv is None or not len(k):
                print(f'  [kline跳过] {name}: 数据缺失')
                continue
            live_bar = None
            if k.index[-1] < today and rt.get(name):
                r = rt[name]   # 实时接口返回的当日盘中 bar
                live_bar = pd.Series({'open': r['open'], 'high': r['high'],
                                      'low': r['low'], 'close': r['last']})
            if live_bar is not None:
                k = pd.concat([k, live_bar.to_frame().T.rename(lambda _: today)])
                k = k[~k.index.duplicated(keep='last')].sort_index()
                # 当日实时 IV（看板同源），拼到 IV 序列尾部
                if name in dash_iv:
                    iv = pd.concat([iv, pd.Series([dash_iv[name]], index=[today])])
            iv = iv.reindex(iv.index.union(k.index)).ffill().reindex(k.index)
            pct = rolling_pct(iv)
            sig = ((pct >= cfg['pct_th'])
                   & (True if cfg['iv_th'] is None else (iv >= cfg['iv_th'])))
            sig = sig.where(pct.notna(), False)
            # 复盘起点锚定 2020-11-16（科创50ETF 上市首日），且不超过 NBARS
            k = k[k.index >= pd.Timestamp(START_DATE)].tail(NBARS)
            bars, pcts, sigs = [], [], []
            for day, r in k.iterrows():
                p = pct.get(day)
                bars.append([str(day.date()),
                             round(float(r['open']), 4), round(float(r['high']), 4),
                             round(float(r['low']), 4), round(float(r['close']), 4)])
                pcts.append(round(float(p), 3) if p == p and p is not None else None)
                sigs.append(int(bool(sig.get(day))))
            iv_from = str(iv.dropna().index.min().date()) if iv.notna().any() else None
            out['products'][name] = dict(th=cfg['pct_th'], iv_th=cfg['iv_th'],
                                         iv_from=iv_from,
                                         live=str(k.index[-1].date()) == str(today.date()),
                                         bars=bars, pct=pcts, sig=sigs)
            tag = '（含实时当日）' if out['products'][name]['live'] else ''
            print(f'  [kline] {name}: {len(bars)} 根{tag}, 信号日 {sum(sigs)} 个')
        except Exception as e:
            print(f'  [kline错误] {name}: {e}')
    # 紧凑列式编码：日期差分 + 分位整数化 + 信号位图（体积约降 50%，手机加载更快）
    out['v'] = 2
    for name, p in out['products'].items():
        days = [pd.Timestamp(b[0]) for b in p['bars']]
        p['d0'] = p['bars'][0][0]
        p['dd'] = [1] + [int((days[i] - days[i-1]).days) for i in range(1, len(days))]
        p['o'] = [b[1] for b in p['bars']]
        p['h'] = [b[2] for b in p['bars']]
        p['l'] = [b[3] for b in p['bars']]
        p['c'] = [b[4] for b in p['bars']]
        p['p'] = [-1 if v is None else round(v*100) for v in p['pct']]
        p['s'] = ''.join(str(v) for v in p['sig'])
        del p['bars'], p['pct'], p['sig']
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    sz = os.path.getsize(OUT) / 1024
    # 尾段文件：最近 TAILN 根（含实时当日），前端每次轻量拉取
    tail = {'updated': out['updated'], 'v': 2, 'n': TAILN, 'products': {}}
    for name, p in out['products'].items():
        N = len(p['c'])
        m = min(TAILN, N)
        # d0 必须是尾段第一根的日期：d0 + 前段差分之和（dd[i] 为第 i 根与前一根的日历日差）
        first_day = pd.Timestamp(p['d0']) + pd.Timedelta(days=sum(p['dd'][1:N-m+1]))
        tail['products'][name] = {
            'th': p['th'], 'iv_th': p['iv_th'], 'iv_from': p['iv_from'],
            'live': p['live'],
            'd0': str(first_day.date()),
            'dd': p['dd'][-m:],
            'o': p['o'][-m:], 'h': p['h'][-m:], 'l': p['l'][-m:], 'c': p['c'][-m:],
            'p': p['p'][-m:], 's': p['s'][-m:],
        }
    with open(OUT_TAIL, 'w', encoding='utf-8') as f:
        json.dump(tail, f, ensure_ascii=False, separators=(',', ':'))
    szt = os.path.getsize(OUT_TAIL) / 1024
    print(f'kline.json: {len(out["products"])} 标的, {sz:.0f}KB + 尾段 {szt:.1f}KB')


if __name__ == '__main__':
    build()
