"""Telegram push after each daily update: last night's fills, tonight's orders, account summary.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (from .env locally, GitHub secrets in Actions).
Without them it prints the message and skips sending.
"""

import json
import os
from html import escape

import requests

from .data import _load_env
from .paper import DATA_PATH

DASHBOARD_URL = "https://lazuli111.github.io/Quantify/"
API = "https://api.telegram.org/bot{token}/{method}"
WEEKDAYS = "一二三四五六日"


def _md(iso: str) -> str:
    _, m, d = iso.split("-")
    return f"{int(m)}/{int(d)}"


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def _pct(v: float) -> str:
    return f"{v:+.2f}%"


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
                else:
                    lines.append(f"▶ 开盘<b>卖出 {o['symbol']}</b>（{escape(o['reason'])}）")
        else:
            lines.append("下次开盘没有操作")
        if p:
            lines.append(f"持有 {p['symbol']} {p['shares']:g} 股，{_pct(p['unrealized_pct'])}，第 {p['held_days']} 天")
        lines.append(f"账户 {_usd(a['equity'])}（{_pct(a['return_pct'])}，同期 QQQ {_pct(a['qqq_return_pct'])}）")
    lines += ["", f'<a href="{DASHBOARD_URL}">打开观察台</a>']
    return "\n".join(lines)


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


def send_daily() -> bool:
    return send(build_message(json.loads(DATA_PATH.read_text(encoding="utf-8"))))


def send_failure(details: str) -> bool:
    return send(f"<b>回调观察台：今天的自动更新失败了</b>\n模拟账户没有改动。{escape(details)}")


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
