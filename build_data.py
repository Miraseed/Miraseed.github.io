# -*- coding: utf-8 -*-
"""
============================================================================
 GitHub Actions 版 · 数据生成脚本（高频实时版）
============================================================================
 在 GitHub Actions 的虚拟机里运行：
   1. 5 个 ETF：走新浪实时期权链 -> BS 反解 IV（1~2 秒，盘中实时）
   2. 中证1000股指 / 铁矿石期权：新浪无对应链，仍走交易所日终口径
   3. 历史分位统一用 panel 历史（交易所日终口径），保证与回测一致
   4. 结果写成 docs/data.json

 用法： python build_data.py
 输出： docs/data.json
============================================================================
"""
import os
import sys
import json
import datetime
import traceback

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if os.path.isdir(os.path.join(HERE, 'code')):
    CODE = os.path.join(HERE, 'code')
else:
    CODE = os.path.join(PARENT, 'code')
for p in (CODE, HERE, PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

OUT = os.path.join(HERE, 'docs', 'data.json')
HIST_JSON = os.path.join(HERE, 'docs', 'history.json')
STATE_JSON = os.path.join(HERE, 'docs', 'push_state.json')

os.makedirs(os.path.dirname(OUT), exist_ok=True)

# 新浪实时能覆盖的 ETF（其余标的走交易所日终）
SINA_ETF = {'科创50ETF', '创业板ETF', '上证50ETF', '沪深300ETF', '中证500ETF'}


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (pd.Timestamp, datetime.datetime, datetime.date)):
        return str(v)[:19]
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return None if (f != f or f in (float('inf'), float('-inf'))) else f
    if hasattr(v, 'item'):
        try:
            return _clean(v.item())
        except Exception:
            return str(v)
    return v


def records(df):
    if df is None or len(df) == 0:
        return []
    return [{k: _clean(v) for k, v in r.items()} for r in df.to_dict('records')]


def load_json(fp, default):
    if os.path.exists(fp):
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(fp, obj):
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def is_market_open(t=None):
    t = t or datetime.datetime.now()
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    return ((9 * 60 + 30) <= hm <= (11 * 60 + 30)) or ((13 * 60) <= hm <= (15 * 60))


def _sina_rows():
    """新浪实时链：返回 {标的: row_dict}（含涨跌幅、建议策略等）"""
    import intraday as ID
    snap = ID.fetch_sina_snapshot(verbose=False)
    out = {}
    for name, v in snap.items():
        iv = v.get('ATM_IV')
        if iv is None or not np.isfinite(iv):
            continue
        pct = ID.iv_percentile(name, iv)
        cfg = ID.POOL.get(name, {})
        th = cfg.get('pct_th', 0.8)
        trig = bool(np.isfinite(pct) and pct >= th)
        if trig:
            sig = '★ 卖出认沽'
            strat = (f'卖出 {name} 虚值认沽；权利金 +50% 止盈 / −50% 止损；'
                     f'剩余≤7天移仓。保证金占用建议 ≤ 账户 50%。')
            reason = f'IV分位 {pct*100:.0f}% ≥ {th*100:.0f}%'
        else:
            sig = '观望'
            gap = f'；距阈值还差 {(th-pct)*100:.0f} 个百分点' if np.isfinite(pct) else ''
            strat = f'IV 未达阈值（分位<{th*100:.0f}%），暂不操作{gap}'
            reason = '—'
        out[name] = dict(
            标的=name, 状态='正常',
            S=_clean(v.get('S')), 昨收=None,
            涨跌幅=_clean(v.get('S_pct')),
            ATM_IV=_clean(iv),
            IV分位=(_clean(pct) if np.isfinite(pct) else None),
            信号=sig, 建议策略=strat, 建议合约='—',
            建议Delta=None, 剩余天数=None,
            近月IV=None, 次近月IV=None, 期限结构=None,
            结构提示='实时链（新浪 BS 反解）',
            数据源='实时', 触发原因=reason,
            更新时间=datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))
    return out


def _exchange_rows():
    """交易所日终口径取数（较慢，约 30~60s）。

    结果按「交易日 + 小时」缓存到 docs/ex_hist_cache.json：
    日终文件一天只更新一次，高频刷新时直接读缓存，避免每次重跑拖慢。
    缓存失效条件：跨交易日，或距离上次取数超过 CACHE_TTL_MIN 分钟。
    """
    import monitor as MON
    CACHE = os.path.join(HERE, 'docs', 'ex_cache.json')
    CACHE_TTL_MIN = 30
    now = datetime.datetime.now()
    today = now.strftime('%Y-%m-%d')

    cache = load_json(CACHE, {})
    if cache.get('date') == today and cache.get('rows'):
        try:
            age = (now - datetime.datetime.strptime(cache['at'], '%Y-%m-%d %H:%M:%S')).total_seconds() / 60
            if age < CACHE_TTL_MIN:
                print(f'[交易所日终] 命中缓存（{age:.0f} 分钟前）')
                return {str(r['标的']): r for r in cache['rows']}
        except Exception:
            pass

    df = MON.main(write_xlsx=False)
    if df is None or not len(df):
        # 取数失败时，若当天有缓存则先用着
        if cache.get('date') == today and cache.get('rows'):
            print('[交易所日终] 取数失败，回退当日缓存')
            return {str(r['标的']): r for r in cache['rows']}
        return {}
    rows = records(df)
    save_json(CACHE, {'date': today, 'at': now.strftime('%Y-%m-%d %H:%M:%S'), 'rows': rows})
    return {str(r['标的']): r for r in rows}


def main():
    now = datetime.datetime.now()
    market_open = is_market_open(now)

    # ---- 1) 新浪实时（ETF）----
    rows_sina = {}
    try:
        rows_sina = _sina_rows()
    except Exception as e:
        print(f'[新浪实时] 失败：{type(e).__name__}: {e}')
        traceback.print_exc()

    # ---- 2) 交易所日终（补新浪未覆盖的标的：中证1000/铁矿石；新浪全挂时也兜底）----
    # 注：中证1000、铁矿石不在 SINA_ETF 里，必须每次取；否则页面会缺这两个标的。
    need_exchange = True
    rows_ex = {}
    try:
        rows_ex = _exchange_rows()
    except Exception as e:
        print(f'[交易所日终] 失败：{type(e).__name__}: {e}')

    # 组装：交易所打底，新浪覆盖（新浪优先）
    merged = {}
    merged.update(rows_ex)
    merged.update(rows_sina)
    try:
        import monitor as MON
        order = list(MON.POOL.keys())
    except Exception:
        order = list(merged.keys())
    rows = [merged[k] for k in order if k in merged]
    for k in merged:
        if k not in order:
            rows.append(merged[k])

    if not rows:
        print('取数失败，保留旧数据')
        return

    n_sig = int(sum(1 for r in rows if '★' in str(r.get('信号', ''))))
    src_cnt = sum(1 for r in rows if r.get('数据源') == '实时')

    # ---- 历史信号累积 ----
    hist = load_json(HIST_JSON, [])
    today = now.strftime('%Y-%m-%d')
    by_key = {(str(h.get('日期', ''))[:10], str(h.get('标的', ''))): h for h in hist}
    for r in rows:
        if '★' in str(r.get('信号', '')):
            by_key[(today, str(r.get('标的', '')))] = dict(
                日期=today, 标的=r.get('标的'), S=r.get('S'),
                ATM_IV=r.get('ATM_IV'), IV分位=r.get('IV分位'),
                信号=r.get('信号'), 建议合约=r.get('建议合约'),
                建议Delta=r.get('建议Delta'), 剩余天数=r.get('剩余天数'),
                触发原因=r.get('触发原因'))
    hist = list(by_key.values())[-300:]

    # ---- 合约IV对照（来自日终 panel）----
    atm_csv = os.path.join(CODE, 'panel', 'atm_by_month.csv')
    atm_table = []
    if os.path.exists(atm_csv):
        a = pd.read_csv(atm_csv, parse_dates=['date'])
        if len(a):
            latest = a[a['date'] == a['date'].max()].sort_values(['product', 'month'])
            PROD_CN = {'i': '铁矿石期权', 'MO': '中证1000股指', '科创50ETF': '科创50ETF',
                       '上证50ETF': '上证50ETF', '沪深300ETF': '沪深300ETF',
                       '中证500ETF': '中证500ETF', '创业板ETF': '创业板ETF'}
            for _, r in latest.iterrows():
                iv, n = r['atm_iv'], int(r['n']) if pd.notna(r['n']) else 0
                dte = int(r['dte']) if pd.notna(r['dte']) else -1
                atm_table.append({
                    '合约月': str(r['month']), '品种': PROD_CN.get(str(r['product']), str(r['product'])),
                    'ATM_IV': _clean(iv),
                    '平值行权价': _clean(r['atm_strike']),
                    '剩余天数': dte if dte >= 0 else None, '样本数': n,
                    '低流动性': bool(n < 15 or dte > 200 or (pd.notna(iv) and (iv < 0.05 or iv > 1.5)))})

    snap = {
        'ok': True, 'last_run': now.strftime('%Y-%m-%d %H:%M:%S'),
        'pool_size': len(rows), 'n_signal': n_sig, 'n_realtime': src_cnt,
        'cost_sec': None, 'refresh_interval': None,
        'market_open': market_open,
        'push_enabled': bool(os.environ.get('WECOM_WEBHOOK')),
        'rows': rows, 'atm_table': atm_table, 'history': list(reversed(hist)),
        'server_time': now.strftime('%Y-%m-%d %H:%M:%S'),
        'data_date': today, 'error': '',
    }
    save_json(OUT, snap)
    save_json(HIST_JSON, hist)
    print(f"data.json 已生成：{len(rows)} 标的（实时 {src_cnt} 个），{n_sig} 信号，历史 {len(hist)} 条")

    # ---- 企业微信推送 ----
    hook = os.environ.get('WECOM_WEBHOOK', '').strip()
    if hook:
        sys.path.insert(0, os.path.join(CODE, 'web') if os.path.isdir(os.path.join(CODE, 'web')) else HERE)
        import push as PUSH
        state = load_json(STATE_JSON, {'last_sig': {}})
        prev = state.get('last_sig', {})
        new_sig, pushed = {}, []
        for r in rows:
            name, sig = str(r.get('标的', '')), str(r.get('信号', ''))
            new_sig[name] = sig
            if '★' not in sig or prev.get(name) == sig:
                continue
            card = PUSH.build_signal_card(r, now)
            ok, msg = PUSH.send_markdown(card, webhook=hook)
            pushed.append(f'{name}:{"ok" if ok else "fail"}')
            print(f'推送 {name}: {msg}')
        state.update(last_sig=new_sig, last_run=snap['last_run'])
        save_json(STATE_JSON, state)
        print('推送结果：', ', '.join(pushed) or '无新信号')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
