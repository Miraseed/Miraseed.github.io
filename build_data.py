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
import re
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
CST = datetime.timezone(datetime.timedelta(hours=8))   # 北京时间（Actions 跑在 UTC）

# 合约IV对照（按月）历史面板：列 [date, product, month, atm_iv, atm_strike, dte, n]
ATM_PANEL = os.path.join(CODE, 'panel', 'atm_by_month.csv')


def now_cn():
    """返回北京时间 datetime（naive，避免前端再转换）。"""
    return datetime.datetime.now(CST).replace(tzinfo=None)


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
    t = t or now_cn()
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    return ((9 * 60 + 30) <= hm <= (11 * 60 + 30)) or ((13 * 60) <= hm <= (15 * 60))


def _atm_sample(ch, n=10):
    """取「平值样本」：按 |K−S| 排序，最接近现货的 n 档（认购+认沽合计）。

    看板与合约对照表**共用**这一个选档逻辑，保证同一合约月的 ATM IV
    在两张表里完全一致（避免同数据两套口径造成困惑）。

    返回 (atm 切片, 现货价 S) 或 (None, nan)。
    """
    if ch is None or not len(ch) or 'strike' not in ch.columns:
        return None, np.nan
    c = ch.copy()
    if 'iv' not in c.columns or c['iv'].notna().sum() == 0:
        return None, np.nan
    S = float(c['S'].iloc[0]) if 'S' in c.columns and pd.notna(c['S'].iloc[0]) else np.nan
    if not np.isfinite(S):
        return None, np.nan
    c = c[c['iv'].notna()].copy()
    if not len(c):
        return None, np.nan
    c['_m'] = (c['strike'] - S).abs()
    return c.sort_values('_m').head(n), S


def _atm_of_chain(ch):
    """从一条链里取平值 IV / 平值行权价 / 样本数（对照表口径）。"""
    atm, _ = _atm_sample(ch)
    if atm is None or not len(atm):
        return None
    return dict(iv=_clean(atm['iv'].median()),
                strike=_clean(atm['strike'].iloc[0]),
                n=int(len(atm)))


def _atm_iv(ch):
    """从期权链取 ATM IV。

    **统一采用「合约对照表」口径**（最近平值 10 档 IV 中位数），保证看板的
    `ATM IV` / `期限结构` 与对照表逐行对得上。
    """
    atm, _ = _atm_sample(ch)
    if atm is None or not len(atm):
        return np.nan
    iv = atm['iv'].dropna()
    return float(iv.median()) if len(iv) else np.nan


def _short_month(label):
    """合约月标签统一为 4 位数字（如 2610 / 2611 / 2701）。

    品种名已在同一行展示，无需重复；'2026-10' -> '2610'，'MO2611' -> '2611'，
    'i2701' -> '2701'。
    """
    if not label:
        return ''
    s = str(label)
    m = re.search(r'(\d{4})(?!\d)', s.replace('-', ''))
    if re.search(r'\d{4}-\d{2}', s):
        y, mm = s.split('-')
        return f'{y[2:]}{mm}'
    m = re.search(r'(\d{4})$', s)
    return m.group(1) if m else s


def _dte_rank(dte):
    """剩余天数优先级（用户口径）：30-45 > 45-60 > 14-30 > >60 > <14。

    返回 (档位序号, 距理想中枢的偏移)；序号越小越优先。
    """
    if dte is None or not np.isfinite(dte):
        return (99, 9999)
    d = float(dte)
    if 30 <= d <= 45:
        return (0, abs(d - 37.5))
    if 45 < d <= 60:
        return (1, abs(d - 52.5))
    if 14 <= d < 30:
        return (2, abs(d - 22))
    if d > 60:
        return (3, d - 60)
    return (4, 14 - d)          # < 14 天


# ---- 同口径历史序列（按 DTE 优先级选月）----------------------------------
# 背景：临近到期（如剩 3 天）的合约 ATM IV 会被 gamma/时间价值急剧推高，
# 与其它月份不可比。为让「IV 分位 / ATM IV / 期限结构」口径一致且不受
# 近月临近到期干扰，历史序列与实时值都统一取「按 DTE 优先级选中的那个月」。
_PCT_SERIES_CACHE = {}


def _pct_hist_series(name):
    """返回某标的历史 ATM IV 序列（每个交易日按 DTE 优先级选出的那个月）。

    口径与看板实时值完全一致：都会挑「30-45 > 45-60 > 14-30 > >60 > <14」的月。
    数据来自 panel/atm_by_month.csv（列 date/product/month/atm_iv/dte）。

    读不到时返回 None，调用方回退到旧的整条 panel 序列。
    「创业板ETF」没有分月历史（只有 159915_ivproxy.csv 单条代理序列），
    故它始终走回退口径 —— 这与回测口径一致，属预期行为。
    """
    if name in _PCT_SERIES_CACHE:
        return _PCT_SERIES_CACHE[name]
    out = None
    try:
        prod = ATM_PRODUCT.get(name, name)
        if os.path.exists(ATM_PANEL):
            d = pd.read_csv(ATM_PANEL)
        else:                                    # 兼容只有 .gz 的情况
            gz = ATM_PANEL + '.gz'
            if not os.path.exists(gz):
                _PCT_SERIES_CACHE[name] = None
                return None
            d = pd.read_csv(gz)
        d = d[d['product'] == prod]
        if len(d) and 'dte' in d.columns and 'atm_iv' in d.columns:
            d = d.copy()
            d['date'] = pd.to_datetime(d['date'])
            picks = []
            for day, grp in d.groupby('date'):
                best, best_rank = None, (99, 9999)
                for iv, dte in zip(grp['atm_iv'], grp['dte']):
                    if not np.isfinite(iv) or not np.isfinite(dte):
                        continue
                    r = _dte_rank(float(dte))
                    if r < best_rank:
                        best_rank, best = r, float(iv)
                if best is not None:
                    picks.append((day, best))
            if len(picks) >= 20:
                picks.sort(key=lambda x: x[0])
                out = pd.Series([p[1] for p in picks],
                                index=[p[0] for p in picks])
    except Exception as e:
        print(f'  [同口径历史] {name} 读取失败，回退默认口径: {e}')
        out = None
    _PCT_SERIES_CACHE[name] = out
    return out


def iv_pct_same_basis(name, iv_now, lookback=250):
    """同口径历史分位：拿「按优先级选月」的历史序列与当前 IV 比。

    返回 (分位, 来源说明)；序列不可用时返回 (None, '面板缺失')。
    """
    if iv_now is None or not np.isfinite(iv_now):
        return None, '当前IV缺失'
    h = _pct_hist_series(name)
    if h is None or len(h) < 20:
        return None, '同口径历史不足'
    h = h.tail(lookback)
    return float((h < iv_now).mean()), f'同口径 {len(h)} 日'


def _month_label(name, ch):
    """从链里取合约月标签，简洁形式（2610 / MO2611 / i2612）。"""
    if ch is None or not len(ch):
        return ''
    for col in ('month', 'cid'):
        if col in ch.columns and len(ch):
            v = str(ch[col].iloc[0])
            if not v:
                continue
            if col == 'cid':
                m = re.search(r'(\d{4})(?=[CPcp]\d)', v)
                if m:
                    return m.group(1)
            m = re.search(r'(\d{4})', v)
            if m:
                return m.group(1)
    return ''


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


# ---- 价格展示精度 --------------------------------------------------------
# 商品期货（铁矿石）按同花顺期货通口径保留 1 位小数；指数/ETF 沿用原逻辑。
def _price_decimals(name, value):
    """返回该标的「展示价格」应保留的小数位数（与前端一致）。"""
    if '铁矿石' in str(name):
        return 1
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 4
    return 2 if (np.isfinite(v) and abs(v) >= 1000) else 4


def _row_from_chain(name, chains, source_label):
    """把「某标的的多月链」-> 一行看板记录。

    chains: {月标签: DataFrame}，按月份升序。
    建议合约：在所有月份里挑「DTE 优先级最高且有合适 Delta 认沽」的那个月；
    期限结构：近月 IV - 次近月 IV。
    """
    now = now_cn()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(chains, pd.DataFrame):        # 兼容单链调用
        chains = {_month_label(name, chains): chains}
    chains = {k: v for k, v in (chains or {}).items() if v is not None and len(v)}
    if not chains:
        return dict(标的=name, 状态='数据缺失', S=None, 价格位数=4, 昨收=None, 涨跌幅=None,
                    ATM_IV=None, IV分位=None, 信号='—',
                    建议策略='实时链取数失败，请稍后重试', 建议合约='—',
                    建议Delta=None, 剩余天数=None, 建议合约月=None,
                    虚值程度=None,
                    近月IV=None, 次近月IV=None, 期限结构=None,
                    结构提示='—', 数据源='实时', 触发原因='—',
                    更新时间=now_str[:16], 行情时间戳=now_str)

    # 主月 = 最近月（用于看板 ATM IV，与期限结构口径自洽）
    months = sorted(chains.keys())
    main_key = months[0]
    main = chains[main_key]

    # 建议合约：在全部月份里，按 (DTE优先级, Delta接近度) 挑最优认沽
    best_put, best_key, best_rank, best_dd = None, None, 99, 9e9
    for k in months:
        ch = chains[k]
        puts = ch[ch['put']] if 'put' in ch.columns else ch.iloc[0:0]
        if not len(puts):
            continue
        ds = pd.to_numeric(ch['dte'], errors='coerce').dropna() if 'dte' in ch.columns else pd.Series(dtype=float)
        dte = float(ds.median()) if len(ds) else None
        r = _dte_rank(dte)
        p = _pick_put(ch)
        if not p or p.get('strike') is None:
            continue
        dd = abs((p['delta'] if p['delta'] is not None else 0) - TARGET_DELTA)
        if (r[0], dd) < (best_rank, best_dd):
            best_put, best_key, best_rank, best_dd = p, k, r[0], dd
    if best_put is None:                        # 退化：用主月
        best_key = main_key
        best_put = _pick_put(main)

    # 主月 = 建议合约所在月（而非机械的最近月）。
    # 理由：临近到期（如剩 3 天）的合约 IV 会被急剧推高，与其它月不可比；
    # 用「建议合约月」作为看板 ATM IV / 期限结构 / 分位的口径，
    # 既与建议合约一致，又避免近月临近到期的失真。
    if best_key in chains:
        main_key = best_key
    main = chains[main_key]

    iv = _atm_iv(main)
    import intraday as ID
    # 分位优先用「同口径历史」（历史也按 DTE 优先级选月），保证与实时值可比；
    # 缺面板时回退到原来的整条 panel 序列。
    pct, pct_src = iv_pct_same_basis(name, iv)
    if pct is None:
        pct = ID.iv_percentile(name, iv) if np.isfinite(iv) else np.nan
        pct_src = '回退 panel 全序列'
    cfg = ID.POOL.get(name, {})
    th = cfg.get('pct_th', 0.8)
    trig = bool(np.isfinite(pct) and pct >= th)

    S = float(main['S'].iloc[0]) if 'S' in main.columns and pd.notna(main['S'].iloc[0]) else np.nan
    S_pct = float(main['S_pct'].iloc[0]) if 'S_pct' in main.columns and pd.notna(main['S_pct'].iloc[0]) else np.nan

    # 期限结构：与看板口径一致 —— 「建议合约月 − 其次月」。
    # 若建议合约月就是最近月，则取紧随其后的那个月做次月。
    if main_key in months:
        mi = months.index(main_key)
    else:
        mi = 0
    near_iv = _atm_iv(chains[months[mi]]) if len(months) > mi else np.nan
    nxt_iv = _atm_iv(chains[months[mi + 1]]) if len(months) > mi + 1 else np.nan
    # 先把近月/次近月舍入到展示精度，再用它们算期限结构，
    # 保证前端「近月 - 次近月」与「期限结构」字段严格一致（避免 ±0.0001 的验算误差）。
    near_iv = round(near_iv, 4) if np.isfinite(near_iv) else np.nan
    nxt_iv = round(nxt_iv, 4) if np.isfinite(nxt_iv) else np.nan
    term = (near_iv - nxt_iv) if (np.isfinite(near_iv) and np.isfinite(nxt_iv)) else np.nan

    mlabel = _short_month(best_key or main_key or '')
    # 虚值程度 = |行权价 − 标的价格| / 标的价格（认沽虚值即 K 低于 S，展示取正值）
    otm = np.nan
    if best_put and best_put.get('strike') is not None and np.isfinite(S) and S > 0:
        otm = abs(float(best_put['strike']) - S) / S
    otm_txt = f'（虚{otm*100:.1f}%）' if np.isfinite(otm) else ''
    if best_put and best_put.get('strike') is not None:
        contract = f"{mlabel} 认沽 K≈{best_put['strike']:.4g}{otm_txt}"
        delta_v, dte_v = best_put['delta'], best_put['dte']
    else:
        contract, delta_v, dte_v = '—', None, None

    if trig:
        sig = '★ 卖出认沽'
        strat = (f"卖出 {name} {mlabel} 虚值认沽"
                 + (f" K≈{best_put['strike']:.4g}" if best_put else '')
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
        价格位数=_price_decimals(name, S),   # 铁矿石=1；其余 >=1000 用 2 位、否则 4 位
        昨收=None,
        涨跌幅=round(S_pct, 4) if np.isfinite(S_pct) else None,
        ATM_IV=round(iv, 4) if np.isfinite(iv) else None,
        IV分位=round(pct, 3) if np.isfinite(pct) else None,
        信号=sig, 建议策略=strat, 建议合约=contract,
        建议合约月=mlabel,
        建议行权价=(best_put['strike'] if best_put else None),
        虚值程度=(round(otm, 4) if np.isfinite(otm) else None),   # |K−S|/S，认沽虚值取正值
        建议Delta=(round(delta_v, 3) if delta_v is not None else None),
        剩余天数=(dte_v if dte_v is not None else None),
        近月IV=(near_iv if np.isfinite(near_iv) else None),
        次近月IV=(nxt_iv if np.isfinite(nxt_iv) else None),
        期限结构=(round(term, 4) if np.isfinite(term) else None),
        分位口径=pct_src,
        口径合约月=mlabel,          # 看板 ATM IV / 分位 / 期限结构 统一取该月
        结构提示=f'{source_label}（口径：{mlabel}；分位 {pct_src}）' if mlabel else source_label,
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
                # 统一月标签为纯数字（i2612 -> 2612），与 ETF / 中证1000 口径一致
                mm = re.search(r'(\d{4})', pz)
                out[mm.group(1) if mm else pz] = _commodity_iv_fill(ch)
    return out


# ---- 合约IV对照（全实时）------------------------------------------------
# 顺序必须与信号看板一致（用户诉求 3）
ATM_ORDER = ['科创50ETF', '创业板ETF', '上证50ETF', '沪深300ETF',
             '中证500ETF', '中证1000股指', '铁矿石期权']
# 看板标的 -> (日终 panel 里的 product 键, 新浪链取法)
ATM_PRODUCT = {'科创50ETF': '科创50ETF', '创业板ETF': '创业板ETF',
               '上证50ETF': '上证50ETF', '沪深300ETF': '沪深300ETF',
               '中证500ETF': '中证500ETF', '中证1000股指': 'MO', '铁矿石期权': 'i'}


def _chains_for(name):
    """取某标的的「多月链」：{月标签: DataFrame}，按月份升序。

    - ETF：逐月拉新浪 ETF 链（2610/2611/2612/2703…）
    - 中证1000：逐月拉中金所 MO 链
    - 铁矿石：逐月拉大商所链
    所有链都补齐 iv/delta/S 列。
    """
    import sina_option as SO
    import intraday as ID
    out = {}
    if name == '中证1000股指':
        for code in SO.index_option_months('MO', 'cffex'):
            try:
                ch = SO.index_option_chain('MO', 'cffex', code)
            except Exception:
                ch = None
            if ch is not None and len(ch):
                ch = ch.copy()
                ch['put'] = ch['cp'].eq('P')
                # 统一月标签为纯数字（MO2611 -> 2611），与 ETF 的 2611 口径一致
                mm = re.search(r'(\d{4})(?=[CPcp]?\d*$)', str(code))
                out[mm.group(1) if mm else str(code)] = ch
    elif name == '铁矿石期权':
        out = _commodity_months_with_iv()
    else:
        code = ID.POOL.get(name, {}).get('code', '')
        seen = set()
        for _k, key, m in SO.etf_option_months(code):
            if m in seen:
                continue
            seen.add(m)
            ch = None
            for _try in range(3):               # 接口偶发空返回，重试
                try:
                    ch = SO.etf_option_chain(code, m)
                except Exception:
                    ch = None
                if ch is not None and len(ch):
                    break
            if ch is not None and len(ch):
                ch = ch.copy()
                ch['put'] = ch['cp'].eq('P')
                # 统一月标签为纯数字（2026-10 -> 2610），与中证1000/铁矿石口径一致
                out[_short_month(m)] = ch
    # 只保留有报价且未到期的月
    clean = {}
    for k, ch in out.items():
        nz = int((pd.to_numeric(ch['price'], errors='coerce') > 0).sum()) if 'price' in ch.columns else len(ch)
        ds = pd.to_numeric(ch['dte'], errors='coerce').dropna() if 'dte' in ch.columns else pd.Series(dtype=float)
        dte = float(ds.median()) if len(ds) else None
        if nz >= 2 and (dte is None or dte >= 0):
            clean[k] = ch
    return clean


def build_atm_table():
    """合约IV对照表：7 标的 × 近月/次近月/远月，全部走实时链，带实时时间戳。

    同时标出「口径月」——即该标的看板 ATM IV / IV分位 / 期限结构 实际采用的合约月
    （按 DTE 优先级选出，通常与建议合约同月），便于用户对齐核对。
    """
    now_str = now_cn().strftime('%Y-%m-%d %H:%M:%S')
    out = []
    for name in ATM_ORDER:
        chains = _chains_for(name)
        # 先按 DTE 优先级定出该标的的「口径月」
        ref_key, ref_rank = None, (99, 9999)
        for k in sorted(chains.keys()):
            c = chains[k]
            ds = pd.to_numeric(c['dte'], errors='coerce').dropna() if 'dte' in c.columns else pd.Series(dtype=float)
            if not len(ds):
                continue
            r = _dte_rank(float(ds.median()))
            if r < ref_rank:
                ref_rank, ref_key = r, k
        # 每个月份算一条
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
            # 临近到期（<14 天）：IV 易被明显推高，单独标记以免误读
            near_expiry = bool(dte is not None and dte < 14)
            out.append({
                '合约月': str(mkey), '品种': name,
                'ATM_IV': a['iv'], '平值行权价': a['strike'],
                '剩余天数': dte, '样本数': a['n'],
                '口径月': bool(mkey == ref_key),
                '临近到期': near_expiry,
                '行情时间戳': now_str, '低流动性': low})
    print(f"atm_table: {len(out)} 行（全实时 {now_str}）")
    return out


def fetch_all():
    """取全部标的的多月实时链 -> list[row_dict]（顺序 = POOL 顺序）。"""
    import intraday as ID
    rows = []
    for name in list(ID.POOL.keys()):
        try:
            chains = _chains_for(name)
            if name == '中证1000股指':
                label = '实时链（新浪·中金所 MO）'
            elif name == '铁矿石期权':
                label = '实时链（新浪·大商所）'
            else:
                label = '实时链（新浪 BS 反解）'
            rows.append(_row_from_chain(name, chains, label))
        except Exception as e:
            print(f'  {name} 取数异常: {type(e).__name__}: {str(e)[:70]}')
            now_str = now_cn().strftime('%Y-%m-%d %H:%M:%S')
            rows.append(dict(标的=name, 状态='异常', S=None, 价格位数=_price_decimals(name, None), 昨收=None, 涨跌幅=None,
                             ATM_IV=None, IV分位=None, 信号='—',
                             建议策略=f'取数异常：{str(e)[:60]}', 建议合约='—',
                             建议Delta=None, 剩余天数=None, 建议合约月=None,
                             虚值程度=None,
                             近月IV=None, 次近月IV=None, 期限结构=None,
                             结构提示='—', 数据源='实时', 触发原因='—',
                             更新时间=now_str[:16], 行情时间戳=now_str))
    return rows


def main():
    now = now_cn()
    market_open = is_market_open(now)

    rows = fetch_all()
    rows = [r for r in rows if r]
    if not rows:
        print('取数失败，保留旧数据')
        return

    n_sig = int(sum(1 for r in rows if '★' in str(r.get('信号', ''))))
    n_real = sum(1 for r in rows if r.get('数据源') == '实时')
    # 保护：实时取数成功的标的太少（多为限流/网络故障），保留旧数据，
    # 避免用残缺数据覆盖线上（宁可展示稍旧的完整数据）。
    n_ok = sum(1 for r in rows if r.get('状态') == '正常' and r.get('ATM_IV') is not None)
    if n_ok < max(3, len(rows) // 2):
        print(f'实时取数仅成功 {n_ok}/{len(rows)} 个标的，疑似限流/网络故障，保留旧数据')
        return

    # ---- 历史信号累积（带秒级时间戳）----
    hist = load_json(HIST_JSON, [])
    today = now.strftime('%Y-%m-%d')
    # 清洗：丢弃缺时间戳或建议合约的残缺旧记录（早期版本生成的）
    hist = [h for h in hist
            if h.get('时间戳') and h.get('建议合约') and h.get('建议合约') != '—']
    # 规范化：建议合约统一为「<合约月> 认沽 K≈…」（品种名已在「标的」列，不重复）。
    # 早期记录带品种前缀且无合约月，无法还原 -> 丢弃。
    clean_hist = []
    for h in hist:
        c = str(h.get('建议合约', ''))
        mk = str(h.get('建议合约月') or '')
        if not mk:
            mm = re.search(r'(?<!\d)(\d{4})(?!\d)', c)
            # 仅当形如「2611 认沽」这类本身就是月标签时才采纳
            if mm and re.match(r'^\d{4}\s*认沽', c):
                mk = mm.group(1)
        if not mk:
            continue                     # 无法确定合约月的旧记录，丢弃
        km = re.search(r'K≈([\d.]+)', c)
        h['建议合约月'] = mk
        if km:
            # 优先复用已有虚值后缀；旧记录没有则由「虚值程度」字段或 K/S 现算
            ot = re.search(r'（虚([\d.]+)%）', c)
            v = h.get('虚值程度')
            if ot is None and v is None:
                try:
                    kk, ss = float(km.group(1)), float(h.get('S'))
                    if ss > 0:
                        v = abs(kk - ss) / ss
                except (TypeError, ValueError):
                    v = None
            if ot is not None:
                ot_pct = ot.group(1)
            elif v is not None:
                ot_pct = f'{float(v)*100:.1f}'
            else:
                ot_pct = None
            h['建议合约'] = (f"{mk} 认沽 K≈{km.group(1)}（虚{ot_pct}%）" if ot_pct else
                            f"{mk} 认沽 K≈{km.group(1)}")
        else:
            h['建议合约'] = f"{mk} 认沽"
        clean_hist.append(h)
    hist = clean_hist
    by_key = {(str(h.get('日期', ''))[:10], str(h.get('标的', ''))): h for h in hist}
    for r in rows:
        if '★' in str(r.get('信号', '')):
            by_key[(today, str(r.get('标的', '')))] = dict(
                日期=today, 时间戳=r.get('行情时间戳') or r.get('更新时间'),
                标的=r.get('标的'), S=r.get('S'),
                ATM_IV=r.get('ATM_IV'), IV分位=r.get('IV分位'),
                信号=r.get('信号'), 建议合约=r.get('建议合约'),
                建议合约月=r.get('建议合约月'),
                建议Delta=r.get('建议Delta'), 剩余天数=r.get('剩余天数'),
                虚值程度=r.get('虚值程度'),
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
