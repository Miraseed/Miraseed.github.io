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
    if len(df) > 100:
        df.to_csv(fp, index_label='date')
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
    if len(df) > 100:
        df.to_csv(fp, index_label='date')
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
        # 铁矿石主连日线（i0），替代 io_iv 的期权合约价格
        k = _sina_futures_kline('i0', 'i0_daily.csv')
        return k, atm_series('i')
    # ETF：新浪日线真实 OHLC
    k = _sina_index_kline(sina, f'{sina}_daily.csv', 1500)
    return k, atm_series(prod)


def build():
    out = {'updated': datetime.datetime.now(CST).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S'),
           'products': {}}
    for name, cfg in PRODUCTS.items():
        try:
            k, iv = ohlc_of(name)
            if k is None or iv is None or not len(k):
                print(f'  [kline跳过] {name}: 数据缺失')
                continue
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
            out['products'][name] = dict(th=cfg['pct_th'], iv_th=cfg['iv_th'],
                                         bars=bars, pct=pcts, sig=sigs)
            print(f'  [kline] {name}: {len(bars)} 根, 信号日 {sum(sigs)} 个')
        except Exception as e:
            print(f'  [kline错误] {name}: {e}')
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    sz = os.path.getsize(OUT) / 1024
    print(f'kline.json: {len(out["products"])} 标的, {sz:.0f}KB')


if __name__ == '__main__':
    build()
