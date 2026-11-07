"""Telegram push after each daily update: last night's fills, tonight's orders, account summary.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (from .env locally, GitHub secrets in Actions).
Without them it prints the message and skips sending.
"""

import json
import os
from html import escape

import requests

from .ai_opinion import load_opinions
from .config import ROOT
from .data import _load_env
from .paper import DATA_PATH

DASHBOARD_URL = "https://lazuli111.github.io/Quantify/"
AGENT_PATH = ROOT / "docs" / "agent.json"
REGIME_ZH = {"risk_on": "偏进攻", "neutral": "中性", "risk_off": "偏防守"}
SIDE_ZH = {"long": "多", "short": "空"}
API = "https://api.telegram.org/bot{token}/{method}"
WEEKDAYS = "一二三四五六日"


def _md(iso: str) -> str:
    _, m, d = iso.split("-")
    return f"{int(m)}/{int(d)}"


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def _pct(v: float) -> str:
    return f"{v:+.2f}%"


def stance(ai: dict) -> str:
    return "同意买入" if ai["agrees"] else "中立" if ai["rating"] == "Hold" else "不同意买入"


def build_message(v: dict) -> str:
    from datetime import date

    nxt = date.fromisoformat(v["next_session"])
    lines = [f"<b>回调观察台 · {_md(v['next_session'])}（周{WEEKDAYS[nxt.weekday()]}）</b>",
             "美东时间 09:30 开盘"]
    as_of = v["as_of"]
    for s in v["strategies"]:
        a = s["account"]
        lines += ["", f"<b>【{escape(s['name'])}】</b>"]

        # What happened at the last session's open (fills and skipped orders).
        fills = []
        for t in s["trades"]:
            if t["exit_date"] == as_of:
                fills.append(f"卖出 {t['symbol']} @ {_usd(t['exit_price'])}，这笔 {_pct(t['ret_pct'])}（{escape(t['reason'])}）")
        p = s["position"]
        if p and p["entry_date"] == as_of:
            fills.append(f"买入 {p['symbol']} {p['shares']:g} 股 @ {_usd(p['entry_price'])}")
        for k in s["skipped"]:
            if k["date"] == as_of:
                fills.append(f"没有买 {k['symbol']}：{escape(k['reason'])}")
        if fills:
            lines.append(f"{_md(as_of)} 成交：" + "；".join(fills))

        if s["tonight"]:
            for o in s["tonight"]:
                if o["action"] == "buy":
                    stop = f"，止损 {_usd(o['stop'])}" if o.get("stop") else ""
                    lines.append(f"▶ 开盘<b>买入 {o['symbol']}</b> 约 {o['est_shares']} 股，参考价 {_usd(o['ref_price'])}{stop}")
                    ai = o.get("ai")
                    if ai:
                        lines.append(f"   AI 意见：{ai['rating_zh']}（{stance(ai)}，仅供参考）")
                else:
                    lines.append(f"▶ 开盘<b>卖出 {o['symbol']}</b>（{escape(o['reason'])}）")
        else:
            lines.append("下次开盘没有操作")
        if p:
            lines.append(f"持有 {p['symbol']} {p['shares']:g} 股，{_pct(p['unrealized_pct'])}，第 {p['held_days']} 天")
        lines.append(f"账户 {_usd(a['equity'])}（{_pct(a['return_pct'])}，同期 QQQ {_pct(a['qqq_return_pct'])}）")
    lines += agent_lines(as_of)
    lines += ["", f'<a href="{DASHBOARD_URL}">打开观察台</a>']
    return "\n".join(lines)


def _agent_view() -> dict | None:
    return json.loads(AGENT_PATH.read_text(encoding="utf-8")) if AGENT_PATH.exists() else None


def agent_lines(as_of: str) -> list[str]:
    """The AI 交易台 part of the daily push: what it filled that session, its positions and account."""
    a = _agent_view()
    if not a or a["as_of"] != as_of or not a.get("start"):
        return []
    acc = a["account"]
    fills = [f"开{SIDE_ZH[p['side']]}仓 {p['ticker']} {p['qty']:g} @ {p['entry_price']:,.2f}"
             for p in a["positions"] if p["entry_date"] == as_of]
    fills += [f"平仓 {t['ticker']}，这笔 {_pct(t['ret_pct'])}（{escape(t['reason'])}）"
              for t in a["trades"] if t["exit_date"] == as_of]
    lines = ["", "<b>【AI 交易台】</b>"]
    if fills:
        lines.append(f"{_md(as_of)} 成交：" + "；".join(fills))
    held = "，".join(f"{p['ticker']}（{SIDE_ZH[p['side']]}）{_pct(p['unrealized_pct'])}" for p in a["positions"])
    lines.append(f"持仓：{held}" if held else "空仓")
    lines.append(f"账户 {acc['equity']:,.2f} USDT（{_pct(acc['return_pct'])}，同期 QQQ {_pct(acc['bench_return_pct'])}）。下次开盘的决策稍后单独推送")
    return lines


def send_agent_decision() -> bool:
    """AI 交易台's decision for the next open, sent from GitHub when the AI has finished."""
    from datetime import date

    a = _agent_view()
    d = a and a.get("decision")
    if not d:
        print("还没有 AI 交易台的决策，不推送")
        return False
    nxt = date.fromisoformat(d["for_session"])
    lines = [f"<b>AI 交易台 · {_md(d['for_session'])}（周{WEEKDAYS[nxt.weekday()]}）开盘</b>"]
    if d["status"] != "ok":
        lines.append(f"这次 AI 没有完成决策，不开新仓；已有持仓照常按止损和到期规则处理。原因：{escape(d.get('error', ''))}")
    else:
        b = d.get("brief", {})
        lines.append(f"大盘：{REGIME_ZH.get(b.get('regime'), '')}。{escape(b.get('summary', ''))}")
        rating = {x["ticker"]: x["rating_zh"] for x in d.get("deep", [])}
        for o in d["orders"]:
            if o["action"] == "close":
                lines.append(f"▶ <b>平仓 {o['ticker']}</b>：{escape(o['reason'])}")
            else:
                lines.append(f"▶ <b>做{SIDE_ZH[o['side']]} {o['ticker']}</b>，仓位 {o['weight']:.0%}，止损 {o['stop_pct']:.1%}，"
                             f"最多持有 {o['hold_days']} 天（TradingAgents：{rating.get(o['ticker'], '—')}）")
                lines.append(f"   {escape(o['reason'])}")
        if not d["orders"]:
            lines.append("不操作")
        if d["rejected"]:
            lines.append("被规则拒绝：" + "；".join(f"{r['ticker']}（{escape(r['why'])}）" for r in d["rejected"]))
    acc = a["account"]
    lines.append(f"账户 {acc['equity']:,.2f} USDT（{_pct(acc['return_pct'])}，同期 QQQ {_pct(acc['bench_return_pct'])}）")
    lines += ["", f'<a href="{DASHBOARD_URL}agent.html">打开 AI 交易台</a>（模拟记账，不下单）']
    return send("\n".join(lines))


def _credentials() -> tuple[str | None, str | None]:
    _load_env()
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def send(text: str) -> bool:
    token, chat = _credentials()
    if not token or not chat:
        print("没有设置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，只打印不发送：\n" + text)
        return False
    r = requests.post(API.format(token=token, method="sendMessage"), timeout=30,
                      json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
    if not r.ok:
        raise RuntimeError(f"Telegram 发送失败：HTTP {r.status_code} {r.json().get('description', '')}")
    return True


def send_daily(force: bool = False) -> bool:
    """Push the summary, but only when the update booked a new session (not on holidays or reruns)."""
    v = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if not v.get("new_day") and not force:
        print("这次更新没有新的交易日（休市或重复运行），不推送")
        return False
    return send(build_message(v))


def send_failure(details: str) -> bool:
    return send(f"<b>回调观察台：今天的自动更新失败了</b>\n模拟账户没有改动。{escape(details)}")


def send_note(text: str) -> bool:
    return send(f"<b>回调观察台</b>\n{escape(text)}")


def send_ai_opinions() -> bool:
    """The AI runs on GitHub after the server's daily update, so its opinions arrive as a second message."""
    v = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    opinions = [o for o in load_opinions() if o["date"] == v["as_of"]]
    if not opinions:
        print("这个交易日没有 AI 意见，不推送")
        return False
    lines = [f"<b>回调观察台 · AI 第二意见（{_md(v['next_session'])} 开盘）</b>"]
    for o in opinions:
        report = f' <a href="{DASHBOARD_URL}{o["report"]}">报告</a>' if o["report"] else ""
        lines.append(f"{o['symbol']}：{o['rating_zh']}（{stance(o)}，仅供参考）{report}")
    return send("\n".join(lines))


def find_chat_id() -> str | None:
    """The chat id of whoever last messaged the bot (send it /start first)."""
    token, _ = _credentials()
    if not token:
        raise RuntimeError("在 .env 中找不到 TELEGRAM_BOT_TOKEN")
    r = requests.get(API.format(token=token, method="getUpdates"), timeout=30)
    r.raise_for_status()
    for u in reversed(r.json().get("result", [])):
        msg = u.get("message") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            return str(chat["id"])
    return None
