# -*- coding: utf-8 -*-
"""
============================================================================
 企业微信群机器人 推送模块
============================================================================
 当某标的【首次触发卖出信号】时，推送一条 markdown 卡片到企业微信群。
 支持：
   - 信号新增推送（去重：同一标的同一信号只推一次，除非信号消失后再出现）
   - 每日收盘摘要推送（可选）
   - 失败重试 + 超时保护（推送失败绝不影响主服务）
   - 无 Webhook 配置时静默跳过（本地调试用）

 配置方式（二选一，优先 env）：
   1) 环境变量  WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
   2) 配置文件  web/config.json  ->  {"wecom_webhook": "https://..."}
============================================================================
"""
import os
import json
import time
import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, 'config.json')
# 状态文件：优先用环境变量 PUSH_STATE（Pages 侧指向 docs/push_state.json 以便提交回仓库），
# 否则用本目录（本地服务用 web/push_state.json）
STATE = os.environ.get('PUSH_STATE') or os.path.join(HERE, 'push_state.json')
UA = 'Mozilla/5.0 (compatible; ETF-Option-Monitor/1.0)'
TIMEOUT = 10


# ---------------------------------------------------------------- 配置
def get_webhook():
    """取 webhook 地址：环境变量优先，其次 config.json"""
    v = os.environ.get('WECOM_WEBHOOK', '').strip()
    if v:
        return v
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, 'r', encoding='utf-8') as f:
                return str(json.load(f).get('wecom_webhook', '')).strip()
        except Exception:
            return ''
    return ''


def _load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_state(st):
    try:
        with open(STATE, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ---------------------------------------------------------------- 底层发送
def send_markdown(content, webhook=None, retries=2):
    """发送 markdown 消息到企业微信群。返回 (ok, msg)"""
    url = webhook or get_webhook()
    if not url:
        return False, 'no webhook configured'
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    last = ''
    for i in range(retries + 1):
        try:
            r = requests.post(url, json=payload,
                              headers={'User-Agent': UA}, timeout=TIMEOUT)
            j = r.json()
            if j.get('errcode') == 0:
                return True, 'ok'
            last = f"errcode={j.get('errcode')} errmsg={j.get('errmsg')}"
        except Exception as e:
            last = str(e)[:120]
        time.sleep(1.5 * (i + 1))
    return False, last


def send_text(content, webhook=None, retries=2):
    """发送纯文本消息（用于摘要）"""
    url = webhook or get_webhook()
    if not url:
        return False, 'no webhook configured'
    payload = {"msgtype": "text", "text": {"content": content}}
    last = ''
    for i in range(retries + 1):
        try:
            r = requests.post(url, json=payload,
                              headers={'User-Agent': UA}, timeout=TIMEOUT)
            j = r.json()
            if j.get('errcode') == 0:
                return True, 'ok'
            last = f"errcode={j.get('errcode')} errmsg={j.get('errmsg')}"
        except Exception as e:
            last = str(e)[:120]
        time.sleep(1.5 * (i + 1))
    return False, last


# ---------------------------------------------------------------- 信号推送
def _fmt_pct(v, digits=1):
    try:
        return f'{float(v)*100:.{digits}f}%'
    except Exception:
        return '—'


def build_signal_card(row, run_at=None):
    """把一条信号行渲染成企业微信 markdown 卡片"""
    run_at = run_at or datetime.datetime.now()
    name = row.get('标的', '')
    sig = str(row.get('信号', ''))
    emoji = '🔴' if '★' in sig else '⚪'
    lines = [
        f'## {emoji} 卖方开仓信号 · {name}',
        f'**信号**：<font color="warning">{sig}</font>',
        f'**ATM IV**：{_fmt_pct(row.get("ATM_IV"))}　'
        f'**IV分位**：{_fmt_pct(row.get("IV分位"), 0)}',
        f'**标的现价**：{row.get("标的现价", "—")}',
    ]
    # 期限结构
    ts_ = row.get('期限结构')
    if ts_ is not None and str(ts_) not in ('', 'nan', 'None'):
        try:
            ts_txt = f'{float(ts_)*100:+.1f}%'
            lines.append(f'**期限结构**：{ts_txt}（近月−次近月）　{row.get("结构提示", "")}')
        except Exception:
            pass
    # 建议
    if row.get('建议合约') and str(row.get('建议合约')) != '—':
        lines.append(f'**建议合约**：{row.get("建议合约")}　'
                     f'Δ{row.get("建议Delta", "—")}　剩{row.get("剩余天数", "—")}天')
    if row.get('触发原因') and str(row.get('触发原因')) != '—':
        lines.append(f'**触发原因**：{row.get("触发原因")}')
    if row.get('建议策略'):
        lines.append(f'\n> {row.get("建议策略")}')
    lines.append(f'\n<font color="comment">{run_at:%Y-%m-%d %H:%M} · 期权卖方监控</font>')
    return '\n'.join(lines)


def build_daily_digest(df, run_at=None, fired_today=None):
    """每日收盘摘要：当日累计触发信号 + 各标的 IV 分位一览

    fired_today：当日累计触发过的标的（来自 state['pushed']），
    即便收盘时信号已回落，也会在日报里列出，保证不漏报。
    """
    run_at = run_at or datetime.datetime.now()
    hits = df[df['信号'].astype(str).str.contains('★')] if '信号' in df.columns else df.iloc[0:0]
    cur = {str(r.get('标的', '')): r for _, r in hits.iterrows()}
    gone = {k for k in (fired_today or {}) if k not in cur}   # 盘中触发过、收盘已回落
    lines = [f'## 📊 卖方监控日报 · {run_at:%m-%d %H:%M}',
             f'**当日触发 {len(cur) + len(gone)} 个标的'
             + (f'（现仍触发 {len(cur)}）' if gone else '') + '**', '']
    if len(hits) or gone:
        for _, r in hits.iterrows():
            lines.append(f'- 🔴 **{r.get("标的")}**　IV分位 {_fmt_pct(r.get("IV分位"), 0)}　'
                         f'{r.get("建议合约", "")}')
        for name in sorted(gone):
            lines.append(f'- ⚪ **{name}**　盘中曾触发，收盘已回落至阈值下方')
    else:
        lines.append('- 今日无标的触发开仓阈值')
    lines.append('\n**各标的 IV 分位**')
    for _, r in df.iterrows():
        lines.append(f'- {r.get("标的")}：{_fmt_pct(r.get("IV分位"), 0)}'
                     f'（{_fmt_pct(r.get("ATM_IV"))}）')
    return '\n'.join(lines)


def push_new_signals(df, run_at=None, dry=False):
    """检测信号并推送。返回推送结果列表。

    去重逻辑（v2）：「当日已推送集合」——
      state 记 {pushed_date: 'YYYY-MM-DD', pushed: {标的: 信号文本}}。
      - 同一标的**当日只推一次**（无论信号是否抖动/消失重现）
      - 跨自然日自动清零，次日首个信号正常推送
      - 修复旧逻辑「逐轮对比」在高频刷新下因信号抖动导致的漏推
    """
    run_at = run_at or datetime.datetime.now()
    today = run_at.strftime('%Y-%m-%d')
    st = _load_state()
    # 跨日清零；兼容旧版 state（只有 last_sig 时视为当天已推，避免重复轰炸）
    if st.get('pushed_date') != today:
        st['pushed'] = {}
        st['pushed_date'] = today
    pushed_today = st.get('pushed', {})
    results = []
    for _, r in df.iterrows():
        name = str(r.get('标的', ''))
        sig = str(r.get('信号', ''))
        if '★' not in sig:
            continue                       # 非信号不推
        if name in pushed_today:
            continue                       # 当日已推过，跳过（不因抖动重复）
        card = build_signal_card(r, run_at)
        if dry:
            results.append((name, True, 'dry-run'))
            pushed_today[name] = sig
            continue
        ok, msg = send_markdown(card)
        results.append((name, ok, msg))
        if ok:
            pushed_today[name] = sig       # 仅推送成功才记账，失败下轮重试
        else:
            print(f'   [推送失败] {name}: {msg}', flush=True)
    if not dry:
        st['pushed'] = pushed_today
        st['pushed_date'] = today
        st['last_sig'] = {str(r.get('标的', '')): str(r.get('信号', ''))
                          for _, r in df.iterrows()}
        st['last_run'] = run_at.strftime('%Y-%m-%d %H:%M')
        _save_state(st)
    return results


def push_digest(df, run_at=None, dry=False):
    run_at = run_at or datetime.datetime.now()
    card = build_daily_digest(df, run_at)
    if dry:
        return True, card
    return send_markdown(card)


def notify_error(msg, dry=False):
    """异常告警：服务连续取数失败时推送"""
    content = (f'## ⚠️ 期权监控服务异常\n'
               f'**时间**：{datetime.datetime.now():%Y-%m-%d %H:%M}\n'
               f'**详情**：{msg}')
    if dry:
        return True, content
    return send_markdown(content)


if __name__ == '__main__':
    # 自测：打印待推送卡片样式
    demo = {'标的': '科创50ETF', '信号': '★ 卖出认沽', 'ATM_IV': 0.31, 'IV分位': 0.86,
            '标的现价': 1.2345, '期限结构': 0.062, '结构提示': '近月明显更贵(陡峭)，近端波动升温',
            '建议合约': '科创50ETF 认沽 K≈1.15', '建议Delta': -0.15, '剩余天数': 42,
            '触发原因': 'IV分位 86% ≥ 80%',
            '建议策略': '卖出 科创50ETF 虚值认沽 K≈1.15（Δ-0.15，剩42天）；权利金 +50% 止盈 / −50% 止损。'}
    print(build_signal_card(demo))
    print('\n' + '=' * 50 + '\n')
    import pandas as pd
    d = pd.DataFrame([demo, {'标的': '铁矿石期权', '信号': '观望', 'ATM_IV': 0.19, 'IV分位': 0.42}])
    print(build_daily_digest(d))
