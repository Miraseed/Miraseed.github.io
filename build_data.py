# -*- coding: utf-8 -*-
"""
============================================================================
 GitHub Actions 版 · 数据生成脚本
============================================================================
 在 GitHub Actions 的虚拟机里运行：
   1. 复用 code/monitor.py 的取数与信号逻辑（实时抓交易所行情）
   2. 把结果写成 docs/data.json
   3. 若有新触发信号，推送到企业微信（webhook 来自仓库 Secret：WECOM_WEBHOOK）
   4. （可选）提交 data.json 回仓库 —— 由 workflow 负责

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
# 两种布局：仓库根目录（code/ 在脚本同级）或 web/github_action/（code/ 在上两级）
if os.path.isdir(os.path.join(HERE, 'code')):
    CODE = os.path.join(HERE, 'code')
else:
    CODE = os.path.join(PARENT, 'code')
for p in (CODE, HERE, PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

OUT = os.path.join(HERE, 'docs', 'data.json')
HIST_JSON = os.path.join(HERE, 'docs', 'history.json')   # 历史信号跨次累积
STATE_JSON = os.path.join(HERE, 'docs', 'push_state.json')  # 推送去重状态（提交回仓库）

os.makedirs(os.path.dirname(OUT), exist_ok=True)


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
    """周一~周五 9:15-15:15 粗判交易时段（与 app.py 同口径）"""
    t = t or datetime.datetime.now()
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    return (9 * 60 + 15) <= hm <= (15 * 60 + 15)


def main():
    now = datetime.datetime.now()
    import monitor as MON
    df = MON.main(write_xlsx=False)
    if df is None or not len(df):
        print('取数失败，保留旧数据'); return

    rows = records(df)
    n_sig = int(sum(1 for r in rows if '★' in str(r.get('信号', ''))))

    # ---- 历史信号累积（跨 Actions 运行保存在 docs/history.json）----
    hist = load_json(HIST_JSON, [])
    today = now.strftime('%Y-%m-%d')
    hist = [h for h in hist if str(h.get('日期', ''))[:10] != today or str(h.get('标的', ''))]
    # 当天已有该标的信号则更新，否则追加
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

    # ---- 合约IV对照 ----
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
        'pool_size': len(rows), 'n_signal': n_sig,
        'cost_sec': None, 'refresh_interval': None,
        'market_open': is_market_open(now),
        'push_enabled': bool(os.environ.get('WECOM_WEBHOOK')),
        'rows': rows, 'atm_table': atm_table, 'history': list(reversed(hist)),
        'server_time': now.strftime('%Y-%m-%d %H:%M:%S'),
        'data_date': today, 'error': '',
    }
    save_json(OUT, snap)
    save_json(HIST_JSON, hist)
    print(f"data.json 已生成：{len(rows)} 标的，{n_sig} 信号，历史 {len(hist)} 条")

    # ---- 企业微信推送（新信号去重）----
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
