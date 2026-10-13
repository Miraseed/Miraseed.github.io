# -*- coding: utf-8 -*-
"""
============================================================================
 GitHub Actions 版 · 数据生成脚本（全实时版）
============================================================================
 全部 7 个标的都走新浪实时链接口（秒级），不再依赖交易所日终文件：
   - 5 个 ETF      : 新浪 ETF 期权链（CON_OP）
   - 中证1000 股指 : 新浪中金所期权链（OptionService, product=MO&exchange=cffex）
   - 铁矿石期权    : 新浪商品期权链（OptionService, product=i_o&exchange=dce）
 一律用 BS 反解 IV + Delta 口径筛平值，历史分位仍用 panel 历史（口径与回测一致）。

 每条行情都带「行情时间戳」（行情时间戳），便于追溯与复盘。

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

TARGET_DELTA = -0.15          # 建议合约的目标 Delta（与 monitor 一致，虚值认沽）


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


def _atm_iv(ch):
    """从期权链取 ATM IV（Delta 口径平值中位数，dte<=120）。"""
    if ch is None or not len(ch):
        return np.nan
    base = ch[ch['dte'] <= 120] if 'dte' in ch.columns else ch
    if not len(base):
        base = ch
    atm = base[(base['delta'].abs() >= 0.35) & (base['delta'].abs() <= 0.65)]
    if not len(atm):
        atm = base
    iv = atm['iv'].dropna()
    return float(iv.median()) if len(iv) else np.nan


def _pick_put(ch):
    """在认沽里挑最接近目标 Delta 的合约，作为建议合约。"""
    if ch is None or not len(ch) or 'put' not in ch.columns:
        return None
    puts = ch[ch['put']].copy()
    if 'iv' in puts.columns:
        puts = puts[puts['iv'].notna()]
    if not len(puts):
        return None
    puts['dd'] = (puts['delta'] - TARGET_DELTA).abs()
    b = puts.sort_values('dd').iloc[0]
    return dict(strike=float(b['strike']),
                delta=(float(b['delta']) if np.isfinite(b['delta']) else None),
                dte=(int(b['dte']) if 'dte' in puts.columns and np.isfinite(b['dte']) else None))


def _row_from_chain(name, ch, source_label):
    """把一条实时期权链 -> 一行看板记录（含建议合约与行情时间戳）。"""
    now = datetime.datetime.now()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')
    iv = _atm_iv(ch)
    if not np.isfinite(iv):
        return dict(标的=name, 状态='数据缺失', S=None, 昨收=None, 涨跌幅=None,
                    ATM_IV=None, IV分位=None, 信号='—',
                    建议策略='实时链取数失败，请稍后重试', 建议合约='—',
                    建议Delta=None, 剩余天数=None,
                    近月IV=None, 次近月IV=None, 期限结构=None,
                    结构提示='—', 数据源='实时', 触发原因='—',
                    更新时间=now_str[:16], 行情时间戳=now_str)
    import intraday as ID
    pct = ID.iv_percentile(name, iv)
    cfg = ID.POOL.get(name, {})
    th = cfg.get('pct_th', 0.8)
    trig = bool(np.isfinite(pct) and pct >= th)

    S = float(ch['S'].iloc[0]) if 'S' in ch.columns and pd.notna(ch['S'].iloc[0]) else np.nan
    S_pct = float(ch['S_pct'].iloc[0]) if 'S_pct' in ch.columns and pd.notna(ch['S_pct'].iloc[0]) else np.nan
    p = _pick_put(ch)
    if p and p.get('strike') is not None:
        contract = f"{name} 认沽 K≈{p['strike']:.4g}"
        delta_v, dte_v = p['delta'], p['dte']
    else:
        contract, delta_v, dte_v = '—', None, None

    if trig:
        sig = '★ 卖出认沽'
        strat = (f"卖出 {name} 虚值认沽"
                 + (f" K≈{p['strike']:.4g}" if p else '')
                 + "；权利金 +50% 止盈 / −50% 止损；剩余≤7天移仓。保证金占用建议 ≤ 账户 50%。")
        reason = f'IV分位 {pct*100:.0f}% ≥ {th*100:.0f}%'
    else:
        sig = '观望'
        gap = f'；距阈值还差 {(th-pct)*100:.0f} 个百分点' if np.isfinite(pct) else ''
        strat = f'IV 未达阈值（分位<{th*100:.0f}%），暂不操作{gap}'
        reason = '—'

    return dict(
        标的=name, 状态='正常',
        S=round(S, 4) if np.isfinite(S) else None,
        昨收=None,
        涨跌幅=round(S_pct, 4) if np.isfinite(S_pct) else None,
        ATM_IV=round(iv, 4),
        IV分位=round(pct, 3) if np.isfinite(pct) else None,
        信号=sig, 建议策略=strat, 建议合约=contract,
        建议行权价=(p['strike'] if p else None),
        建议Delta=(round(delta_v, 3) if delta_v is not None else None),
        剩余天数=(dte_v if dte_v is not None else None),
        近月IV=None, 次近月IV=None, 期限结构=None,
        结构提示=source_label,
        数据源='实时', 触发原因=reason,
        更新时间=now_str[:16], 行情时间戳=now_str)


def _commodity_iv_fill(ch, dte=None):
    """给铁矿石链补 IV/Delta（用期货现货价反解）。"""
    import sina_option as SO
    if not len(ch):
        return ch
    ch = ch.copy()
    S, S_pct = np.nan, np.nan
    for code in ('nf_I0', 'nf_I2601', 'nf_i0'):
        try:
            s, _, p = SO.spot_quote(code)
            if np.isfinite(s) and s > 0:
                S, S_pct = s, p
                break
        except Exception:
            continue
    if not np.isfinite(S):
        S = float(pd.to_numeric(ch['strike'], errors='coerce').median())
    if dte is None:
        ds = pd.to_numeric(ch['dte'], errors='coerce').dropna() if 'dte' in ch.columns else pd.Series(dtype=float)
        dte = int(ds.median()) if len(ds) else 30
    ch['T'] = max(dte, 1) / 365.0
    ch['dte'] = dte
    ch['iv'] = [SO._bs_iv(m, S, k, t, c)
                for m, k, t, c in zip(ch['price'], ch['strike'], ch['T'], ch['cp'])]
    ch['delta'] = [SO._bs_delta(S, k, t, iv, c)
                   for k, t, iv, c in zip(ch['strike'], ch['T'], ch['iv'], ch['cp'])]
    ch['S'] = S
    ch['S_pct'] = S_pct
    ch['put'] = ch['cp'].eq('P')
    return ch


def _commodity_chain_with_iv():
    """铁矿石近月链（看板用）：取最近一个未到期的有效月份。"""
    import sina_option as SO
    best = None
    for pz in ('i2610', 'i2611', 'i2612', 'i2701', 'i2705', 'i2709'):
        try:
            ch = SO.commodity_chain('i_o', 'dce', pz)
        except Exception:
            ch = pd.DataFrame()
        if ch is None or not len(ch):
            continue
        ds = pd.to_numeric(ch['dte'], errors='coerce').dropna()
        if not len(ds) or int(ds.median()) < 0:
            continue
        nz = int((pd.to_numeric(ch['price'], errors='coerce') > 0).sum())
        if nz < 2:
            continue
        best = _commodity_iv_fill(ch)      # 第一个未到期且有报价的月份即最近月
        break
    return best if best is not None else pd.DataFrame()


def _commodity_months_with_iv():
    """铁矿石全部有报的月份链（对照表用），返回 {pinzhong: chain}。"""
    import sina_option as SO
    out = {}
    for pz in ('i2610', 'i2611', 'i2612', 'i2701', 'i2705', 'i2709'):
        try:
            ch = SO.commodity_chain('i_o', 'dce', pz)
        except Exception:
            ch = pd.DataFrame()
        if ch is not None and len(ch):
            nz = int((pd.to_numeric(ch['price'], errors='coerce') > 0).sum())
            if nz >= 2:
                out[pz] = _commodity_iv_fill(ch)
    return out


# ---- 合约IV对照（全实时）------------------------------------------------
# 顺序必须与信号看板一致（用户诉求 3）
ATM_ORDER = ['科创50ETF', '创业板ETF', '上证50ETF', '沪深300ETF',
             '中证500ETF', '中证1000股指', '铁矿石期权']
# 看板标的 -> (日终 panel 里的 product 键, 新浪链取法)
ATM_PRODUCT = {'科创50ETF': '科创50ETF', '创业板ETF': '创业板ETF',
               '上证50ETF': '上证50ETF', '沪深300ETF': '沪深300ETF',
               '中证500ETF': '中证500ETF', '中证1000股指': 'MO', '铁矿石期权': 'i'}


def _atm_of_chain(ch):
    """从一条链里取平值 IV / 平值行权价 / 样本数（口径与原 panel 一致）。

    平值样本 = 按 |K-S| 排序取最接近现货的若干档（认购+认沽合计最多 10 档），
    这样实时链档位少时也能取到有意义的 ATM IV，不会被误判为「无样本」。
    """
    if ch is None or not len(ch) or 'strike' not in ch.columns:
        return None
    c = ch.copy()
    if 'iv' not in c.columns or c['iv'].notna().sum() == 0:
        return None
    S = float(c['S'].iloc[0]) if 'S' in c.columns and pd.notna(c['S'].iloc[0]) else np.nan
    if not np.isfinite(S):
        return None
    c = c[c['iv'].notna()].copy()
    if not len(c):
        return None
    c['_m'] = (c['strike'] - S).abs()
    c = c.sort_values('_m')
    atm = c.head(10)                       # 最接近平值的 10 档
    return dict(iv=_clean(atm['iv'].median()),
                strike=_clean(atm['strike'].iloc[0]),
                n=int(len(atm)))


def build_atm_table():
    """合约IV对照表：7 标的 × 近月/次近月/远月，全部走实时链，带实时时间戳。"""
    import sina_option as SO
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    out = []
    for name in ATM_ORDER:
        # 1) 取该标的的全部月份链
        if name == '中证1000股指':
            months = SO.index_option_months('MO', 'cffex')
            chains = {}
            for code in months:
                try:
                    ch = SO.index_option_chain('MO', 'cffex', code)
                except Exception:
                    ch = None
                if ch is not None and len(ch):
                    # 按合约月分组
                    key = str(ch['month'].iloc[0]) if 'month' in ch.columns else code
                    chains[key] = ch
        elif name == '铁矿石期权':
            chains = _commodity_months_with_iv()
        else:
            import intraday as ID
            code = ID.POOL.get(name, {}).get('code', '')
            chains = {}
            seen = set()
            for k, _kk, m in SO.etf_option_months(code):
                if m in seen:
                    continue
                seen.add(m)
                try:
                    ch = SO.etf_option_chain(code, m)
                except Exception:
                    ch = None
                if ch is not None and len(ch):
                    chains[m] = ch
                    ch['put'] = ch['cp'].eq('P')
        # 2) 每个月份算一条
        for mkey in sorted(chains.keys()):
            ch = chains[mkey]
            a = _atm_of_chain(ch)
            if not a:
                continue
            dte = None
            if 'dte' in ch.columns:
                ds = pd.to_numeric(ch['dte'], errors='coerce').dropna()
                dte = int(ds.median()) if len(ds) else None
            if dte is not None and dte < 0:
                continue                       # 已到期的合约月不列
            # 低流动性：实时链本身档位有限（约 8~12 档），故以「<5 档 / 超远月 / IV 异常」判定
            low = bool(a['n'] < 5 or (dte is not None and dte > 200)
                       or (a['iv'] is not None and (a['iv'] < 0.05 or a['iv'] > 1.5)))
            out.append({
                '合约月': str(mkey), '品种': name,
                'ATM_IV': a['iv'], '平值行权价': a['strike'],
                '剩余天数': dte, '样本数': a['n'],
                '行情时间戳': now_str, '低流动性': low})
    print(f"atm_table: {len(out)} 行（全实时 {now_str}）")
    return out


def fetch_all():
    """取全部标的的实时链 -> list[row_dict]（顺序 = POOL 顺序）。"""
    import sina_option as SO
    import intraday as ID
    rows = []
    for name in list(ID.POOL.keys()):
        cfg = ID.POOL.get(name, {})
        code = cfg.get('code', '')
        try:
            if name == '中证1000股指':
                ch = SO.index_option_chain('MO', 'cffex')
                if len(ch):
                    ch = ch.copy()
                    ch['put'] = ch['cp'].eq('P')
                rows.append(_row_from_chain(name, ch, '实时链（新浪·中金所 MO）'))
            elif name == '铁矿石期权':
                ch = _commodity_chain_with_iv()
                rows.append(_row_from_chain(name, ch, '实时链（新浪·大商所）'))
            else:
                ch = SO.etf_option_chain(code)
                if len(ch):
                    ch = ch.copy()
                    ch['put'] = ch['cp'].eq('P')
                rows.append(_row_from_chain(name, ch, '实时链（新浪 BS 反解）'))
        except Exception as e:
            print(f'  {name} 取数异常: {type(e).__name__}: {str(e)[:70]}')
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            rows.append(dict(标的=name, 状态='异常', S=None, 昨收=None, 涨跌幅=None,
                             ATM_IV=None, IV分位=None, 信号='—',
                             建议策略=f'取数异常：{str(e)[:60]}', 建议合约='—',
                             建议Delta=None, 剩余天数=None,
                             近月IV=None, 次近月IV=None, 期限结构=None,
                             结构提示='—', 数据源='实时', 触发原因='—',
                             更新时间=now_str[:16], 行情时间戳=now_str))
    return rows


def main():
    now = datetime.datetime.now()
    market_open = is_market_open(now)

    rows = fetch_all()
    rows = [r for r in rows if r]
    if not rows:
        print('取数失败，保留旧数据')
        return

    n_sig = int(sum(1 for r in rows if '★' in str(r.get('信号', ''))))
    n_real = sum(1 for r in rows if r.get('数据源') == '实时')

    # ---- 历史信号累积（带秒级时间戳）----
    hist = load_json(HIST_JSON, [])
    today = now.strftime('%Y-%m-%d')
    by_key = {(str(h.get('日期', ''))[:10], str(h.get('标的', ''))): h for h in hist}
    for r in rows:
        if '★' in str(r.get('信号', '')):
            by_key[(today, str(r.get('标的', '')))] = dict(
                日期=today, 时间戳=r.get('行情时间戳') or r.get('更新时间'),
                标的=r.get('标的'), S=r.get('S'),
                ATM_IV=r.get('ATM_IV'), IV分位=r.get('IV分位'),
                信号=r.get('信号'), 建议合约=r.get('建议合约'),
                建议Delta=r.get('建议Delta'), 剩余天数=r.get('剩余天数'),
                触发原因=r.get('触发原因'))
    hist = list(by_key.values())[-500:]

    # ---- 合约IV对照（全部走实时链，带实时时间戳）----
    atm_table = build_atm_table()
    panel_date = now.strftime('%Y-%m-%d')

    snap = {
        'ok': True, 'last_run': now.strftime('%Y-%m-%d %H:%M:%S'),
        'pool_size': len(rows), 'n_signal': n_sig, 'n_realtime': n_real,
        'cost_sec': None, 'refresh_interval': None,
        'market_open': market_open,
        'push_enabled': bool(os.environ.get('WECOM_WEBHOOK')),
        'rows': rows, 'atm_table': atm_table, 'history': list(reversed(hist)),
        'panel_date': panel_date,
        'server_time': now.strftime('%Y-%m-%d %H:%M:%S'),
        'data_date': today, 'error': '',
    }
    save_json(OUT, snap)
    save_json(HIST_JSON, hist)
    print(f"data.json 已生成：{len(rows)} 标的（实时 {n_real} 个），{n_sig} 信号，历史 {len(hist)} 条")
    for r in rows:
        print(f"  {str(r.get('标的')):<12s} IV={r.get('ATM_IV')} 分位={r.get('IV分位')} "
              f"合约={r.get('建议合约')} {r.get('信号')}")

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
