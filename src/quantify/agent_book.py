"""AI 交易台 (paper): an AI picks US stocks each evening; this module books what it would have traded.

Runs on the Tokyo server after the US close (deploy/daily.sh), the only machine here that can reach Binance.
Fills, stops and funding use Binance's live-site public prices for the stock perpetuals; nothing is ever
ordered. The decisions come from agent_decide (GitHub Actions), committed to agent/decisions/<session>.json
after one session's close, for the next session's open.

Kept light (no pandas): it shares a 1.6 GB machine with trading bots. Run as `python -m quantify.agent_book`.
"""

import json
import math
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import holidays
import requests

from .config import ROOT

ET = ZoneInfo("America/New_York")
AGENT_DIR = ROOT / "agent"
STATE_PATH = AGENT_DIR / "state.json"
UNIVERSE_PATH = AGENT_DIR / "universe.json"
MARKET_PATH = AGENT_DIR / "market.json"
DECISIONS_DIR = AGENT_DIR / "decisions"
HALT_PATH = AGENT_DIR / "HALT"   # manual switch: while this file exists, no new positions
VIEW_PATH = ROOT / "docs" / "agent.json"
PAGE_PATH = ROOT / "docs" / "agent.html"
PAGE_SOURCE = ROOT / "src" / "quantify" / "agent_page.html"
API = "https://fapi.binance.com"
BENCH = "QQQUSDT"
SKIP_BASES = {"STXX"}   # its price doesn't match the US ticker of that name (checked 2026-11-07)
YAHOO_ALIAS = {"BRKB": "BRK-B"}

# Hard rules, enforced in code whatever the AI says (also checked when the decision is made).
CAPITAL = 5000.0
MAX_NAMES = 3
MAX_WEIGHT = 0.33
MAX_GROSS = 1.0           # total position value <= equity: no leverage
STOP_RANGE = (0.02, 0.08)
MAX_HOLD = 10             # sessions
DAILY_LOSS_HALT = 0.03    # a session that loses more: no new positions at the next open
DRAWDOWN_HALT = 0.20      # from the peak: close everything and stop until resumed by hand
MAX_NEW_PER_WEEK = 4
NO_REVERSE_SESSIONS = 2   # no flipping a stock within 2 sessions of closing it (unless it was stopped out)
FUNDING_LIMIT = 0.001     # don't open on the side that would pay more than 0.1% per funding
FEE = 0.0005              # taker fee each side (assumed)
SLIP = 0.0005             # slippage each side (assumed)
FILL_AFTER_OPEN = timedelta(minutes=5)
MIN_NOTIONAL = 5.0

RULES = [
    "每天美股收盘后，AI 读大盘和政策新闻，从约 190 只美股永续合约里粗筛，再用 TradingAgents 深入分析最多 3 只，最后决定开仓或平仓。",
    "下一个交易日美东 09:35，按币安正式站的价格记账成交；不在交易所下单。",
    f"不加杠杆：持仓总值不超过权益；最多同时 {MAX_NAMES} 只，每只不超过权益的 {MAX_WEIGHT:.0%}。",
    f"每笔必须带止损，距离 {STOP_RANGE[0]:.0%}–{STOP_RANGE[1]:.0%}，盘中触及即按止损价成交（周末也算）；最多持有 {MAX_HOLD} 个交易日。",
    "做多需要 TradingAgents 评级为买入或增持，做空需要减持或卖出；持有评级不开仓。",
    f"单日亏损超过 {DAILY_LOSS_HALT:.0%}：下一个交易日不开新仓。从峰值回撤 {DRAWDOWN_HALT:.0%}：全部平仓并停机，要人工恢复。",
    f"每周新开仓不超过 {MAX_NEW_PER_WEEK} 笔；平仓后 {NO_REVERSE_SESSIONS} 个交易日内不反向开仓（止损除外）；"
    f"资金费超过 {FUNDING_LIMIT:.1%} 的一方不开仓。",
    f"成本：手续费每边 {FEE:.2%}、滑点每边 {SLIP:.2%}，资金费按实际费率每 8 小时结算。",
]


# ---------------------------------------------------------------- calendar (NYSE)

def _holidays(year: int) -> set:
    return set(holidays.financial_holidays("NYSE", years=[year - 1, year, year + 1]))


def is_session(d: date) -> bool:
    return d.weekday() < 5 and d not in _holidays(d.year)


def next_session(d: date) -> date:
    d += timedelta(days=1)
    while not is_session(d):
        d += timedelta(days=1)
    return d


def prev_session(d: date) -> date:
    d -= timedelta(days=1)
    while not is_session(d):
        d -= timedelta(days=1)
    return d


def _ms(d: date, t: time) -> int:
    return int(datetime.combine(d, t, tzinfo=ET).timestamp() * 1000)


def fill_ms(d: date) -> int:
    return int((datetime.combine(d, time(9, 30), tzinfo=ET) + FILL_AFTER_OPEN).timestamp() * 1000)


def close_ms(d: date) -> int:
    return _ms(d, time(16, 0))


def latest_closed_session(now: datetime | None = None) -> date:
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    d = now.date()
    if is_session(d) and now.time() >= time(16, 15):
        return d
    return prev_session(d)


def _et(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, ET).isoformat(timespec="minutes")


# ---------------------------------------------------------------- Binance public data

def _get(path: str, **params):
    for attempt in range(4):
        try:
            r = requests.get(API + path, params=params, timeout=30)
            if r.status_code in (418, 429) or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == 3:
                raise
            _time.sleep(5 * (attempt + 1))


def fetch_universe() -> list[dict]:
    """US stock and ETF perpetuals (USDT margin) that are trading, with their US tickers."""
    out = []
    for s in _get("/fapi/v1/exchangeInfo")["symbols"]:
        if (s.get("underlyingType") != "EQUITY" or s["marginAsset"] != "USDT" or s["status"] != "TRADING"
                or s["baseAsset"] in SKIP_BASES):
            continue
        lot = next((f for f in s["filters"] if f["filterType"] == "MARKET_LOT_SIZE"), None) or \
            next(f for f in s["filters"] if f["filterType"] == "LOT_SIZE")
        out.append({"symbol": s["symbol"], "ticker": YAHOO_ALIAS.get(s["baseAsset"], s["baseAsset"]),
                    "step": float(lot["stepSize"]), "min_qty": float(lot["minQty"])})
    return sorted(out, key=lambda x: x["symbol"])


def fetch_marks(symbols: set[str]) -> dict:
    return {x["symbol"]: {"mark": float(x["markPrice"]), "funding_rate": float(x["lastFundingRate"] or 0),
                          "next_funding": int(x["nextFundingTime"] or 0)}
            for x in _get("/fapi/v1/premiumIndex") if x["symbol"] in symbols}


def klines(symbol: str, start: int, end: int) -> list[tuple]:
    """1-minute bars (open time ms, open, high, low, close) with start <= open time < end."""
    out, t = [], start
    while t < end:
        rows = _get("/fapi/v1/klines", symbol=symbol, interval="1m", startTime=t, endTime=end - 1, limit=1500)
        if not rows:
            break
        out += [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]
        t = int(rows[-1][0]) + 60_000
        if len(rows) < 1500:
            break
    return out


def funding(symbol: str, start: int, end: int) -> list[tuple]:
    """Funding events (time ms, rate, mark price) with start <= time < end."""
    rows = _get("/fapi/v1/fundingRate", symbol=symbol, startTime=start, endTime=end - 1, limit=1000)
    return [(int(r["fundingTime"]), float(r["fundingRate"]), float(r.get("markPrice") or 0)) for r in rows]


# ---------------------------------------------------------------- state

def new_state(last_session: date) -> dict:
    return {"capital": CAPITAL, "cash": CAPITAL, "start": None, "last_session": last_session.isoformat(),
            "positions": [], "trades": [], "curve": [], "peak": CAPITAL, "halted": None, "no_new_for": None,
            "bench": None, "fees": 0.0, "funding": 0.0, "events": []}


def load_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def load_decision(session: date) -> dict | None:
    return load_json(DECISIONS_DIR / f"{session.isoformat()}.json")


def _week(d: str) -> tuple:
    return date.fromisoformat(d).isocalendar()[:2]


def opens_this_week(state: dict, d: date) -> int:
    wk = d.isocalendar()[:2]
    entries = [p["entry_date"] for p in state["positions"]] + [t["entry_date"] for t in state["trades"]]
    return sum(1 for e in entries if _week(e) == wk)


def recently_closed(state: dict, ticker: str, d: date) -> dict | None:
    """The last trade in `ticker` if it closed within NO_REVERSE_SESSIONS sessions before `d`."""
    limit = d
    for _ in range(NO_REVERSE_SESSIONS):
        limit = prev_session(limit)
    for t in reversed(state["trades"]):
        if t["ticker"] == ticker:
            return t if date.fromisoformat(t["exit_date"]) >= limit else None
    return None


def check_open(order: dict, state: dict, d: date, marks: dict | None = None) -> str | None:
    """Why an open order breaks a hard rule (None if it doesn't). `d` is the session it would fill in."""
    side = order["side"]
    if state.get("halted") or HALT_PATH.exists():
        return "已停机，不开新仓"
    if state.get("no_new_for") == d.isoformat():
        return f"上一个交易日亏损超过 {DAILY_LOSS_HALT:.0%}，今天不开新仓"
    if any(p["ticker"] == order["ticker"] for p in state["positions"]):
        return "已经持有这只"
    if not 0 < order.get("weight", 0) <= MAX_WEIGHT + 1e-9:
        return f"仓位必须在 0–{MAX_WEIGHT:.0%} 之间"
    if not STOP_RANGE[0] - 1e-9 <= order.get("stop_pct", 0) <= STOP_RANGE[1] + 1e-9:
        return f"止损距离必须在 {STOP_RANGE[0]:.0%}–{STOP_RANGE[1]:.0%} 之间"
    if not 1 <= order.get("hold_days", 0) <= MAX_HOLD:
        return f"持有天数必须在 1–{MAX_HOLD} 之间"
    if opens_this_week(state, d) >= MAX_NEW_PER_WEEK:
        return f"本周已开 {MAX_NEW_PER_WEEK} 笔"
    last = recently_closed(state, order["ticker"], d)
    if last and last["side"] != side and last["reason"] != "止损":
        return f"{NO_REVERSE_SESSIONS} 个交易日内刚平仓，不反向开仓"
    if marks is not None:
        rate = marks.get(order["symbol"], {}).get("funding_rate", 0.0)
        if (side == "long" and rate > FUNDING_LIMIT) or (side == "short" and rate < -FUNDING_LIMIT):
            return f"资金费 {rate:+.3%}，开仓方要付费过多"
    return None


# ---------------------------------------------------------------- booking one session

def _sgn(side: str) -> int:
    return 1 if side == "long" else -1


def _value(p: dict, px: float) -> float:
    return _sgn(p["side"]) * p["qty"] * (px - p["entry_price"])


def _close(state: dict, p: dict, px: float, ms: int, reason: str, d: date) -> None:
    px *= 1 - SLIP * _sgn(p["side"])
    fee = p["qty"] * px * FEE
    pnl_px = _value(p, px)
    state["cash"] += pnl_px - fee
    state["fees"] += fee
    pnl = pnl_px - fee - p["entry_fee"] + p["funding"]
    state["trades"].append({
        "symbol": p["symbol"], "ticker": p["ticker"], "side": p["side"], "qty": p["qty"],
        "entry_date": p["entry_date"], "entry_time": p["entry_time"], "entry_price": round(p["entry_price"], 4),
        "exit_date": d.isoformat(), "exit_time": _et(ms), "exit_price": round(px, 4), "reason": reason,
        "held": p["held"], "fees": round(fee + p["entry_fee"], 4), "funding": round(p["funding"], 4),
        "pnl": round(pnl, 2), "ret_pct": round(pnl / (p["qty"] * p["entry_price"]) * 100, 3),
        "decision": p["decision"], "rating": p.get("rating"), "why": p.get("why", "")})
    state["positions"].remove(p)


def _walk(state: dict, p: dict, bars: list, events: list, start: int, end: int, d: date) -> bool:
    """Funding and the stop for one position over [start, end). False if it was stopped out."""
    feed = sorted([(t, 0, e) for t, *e in events if start <= t < end] +
                  [(b[0], 1, b) for b in bars if start <= b[0] < end], key=lambda x: (x[0], x[1]))
    for t, kind, x in feed:
        if kind == 0:
            rate, mark = x
            mark = mark or p.get("last") or p["entry_price"]
            pay = -_sgn(p["side"]) * p["qty"] * mark * rate
            p["funding"] += pay
            state["cash"] += pay
            state["funding"] += pay
        else:
            _, o, h, lo, c = x
            hit = lo <= p["stop"] if p["side"] == "long" else h >= p["stop"]
            if hit:
                px = min(o, p["stop"]) if p["side"] == "long" else max(o, p["stop"])
                _close(state, p, px, t, "止损", d)
                return False
            p["last"] = c
    return True


def _bar_at(bars: list, ms: int):
    return next((b for b in bars if b[0] >= ms and b[0] < ms + 15 * 60_000), None)


def process_session(state: dict, d: date, universe: dict, log: list) -> None:
    prev = date.fromisoformat(state["last_session"])
    t0, tf, tc = close_ms(prev), fill_ms(d), close_ms(d)
    decision = load_decision(prev) or {}
    opens = [o for o in decision.get("orders", []) if o["action"] == "open"]
    closes = {o["ticker"]: o.get("reason", "AI 决定平仓") for o in decision.get("orders", []) if o["action"] == "close"}
    must = {p["symbol"] for p in state["positions"]} | {BENCH}
    bars, fund = {}, {}
    for s in must | {o["symbol"] for o in opens}:
        try:
            bars[s], fund[s] = klines(s, t0, tc), funding(s, t0, tc)
        except requests.RequestException:
            if s in must:
                raise  # without prices for what is held the day can't be booked; the next run retries
            bars[s], fund[s] = [], []

    # 1) Before today's fill: funding and stops on what was held overnight (weekends included).
    for p in list(state["positions"]):
        _walk(state, p, bars[p["symbol"]], fund[p["symbol"]], t0, tf, d)

    # 2) At the fill: closes first (the AI's, expiries, a drawdown stop), then opens.
    for p in list(state["positions"]):
        why = p.pop("close_next", None) or closes.get(p["ticker"])
        if why:
            b = _bar_at(bars[p["symbol"]], tf)
            if b:
                _close(state, p, b[1], b[0], why, d)
            else:
                log.append({"date": d.isoformat(), "ticker": p["ticker"], "what": "平仓推迟", "why": "开盘后没有成交数据"})
                p["close_next"] = why
    equity = state["cash"] + sum(_value(p, p.get("last", p["entry_price"])) for p in state["positions"])
    for o in opens:
        why = check_open(o, state, d)
        b = _bar_at(bars[o["symbol"]], tf)
        if not why and b is None:
            why = "开盘后没有成交数据"
        if not why and len(state["positions"]) >= MAX_NAMES:
            why = f"已持有 {MAX_NAMES} 只"
        gross = sum(p["qty"] * p.get("last", p["entry_price"]) for p in state["positions"])
        if not why and gross + o["weight"] * equity > MAX_GROSS * equity + 1e-6:
            why = "持仓总值会超过权益（不加杠杆）"
        if not why:
            px = b[1] * (1 + SLIP * _sgn(o["side"]))
            step = universe.get(o["symbol"], {}).get("step", 0.01)
            qty = math.floor(o["weight"] * equity / px / step) * step
            qty = round(qty, 8)
            if qty * px < MIN_NOTIONAL:
                why = "金额太小"
        if why:
            log.append({"date": d.isoformat(), "ticker": o["ticker"], "what": "没有开仓", "why": why})
            continue
        fee = qty * px * FEE
        state["cash"] -= fee
        state["fees"] += fee
        sign = _sgn(o["side"])
        state["positions"].append({
            "symbol": o["symbol"], "ticker": o["ticker"], "side": o["side"], "qty": qty, "entry_price": px,
            "entry_date": d.isoformat(), "entry_time": _et(b[0]), "entry_fee": fee, "funding": 0.0,
            "stop": px * (1 - sign * o["stop_pct"]), "stop_pct": o["stop_pct"], "hold_days": o["hold_days"],
            "held": 0, "last": px, "decision": prev.isoformat(), "rating": o.get("rating"), "why": o.get("reason", "")})

    # 3) After the fill until the close: funding and stops on everything held.
    for p in list(state["positions"]):
        _walk(state, p, bars[p["symbol"]], fund[p["symbol"]], tf, tc, d)

    # 4) Benchmark: QQQ perpetual, 1x long from the first session's fill, same costs and funding.
    qb = bars[BENCH]
    if state["bench"] is None:
        b = _bar_at(qb, tf)
        if b:
            px = b[1] * (1 + SLIP)
            state["bench"] = {"units": CAPITAL / (px * (1 + FEE)), "cash": 0.0, "start_price": px, "last": px}
    if state["bench"]:
        bn = state["bench"]
        for t, rate, mark in fund[BENCH]:
            if tf <= t < tc or (state["start"] and t0 <= t < tf):
                bn["cash"] -= bn["units"] * (mark or bn["last"]) * rate
        if qb:
            bn["last"] = qb[-1][4]

    # 5) Mark at the close; the end-of-day rules.
    for p in state["positions"]:
        p["held"] += 1
        if p["held"] >= min(p["hold_days"], MAX_HOLD):
            p["close_next"] = f"持有满 {p['held']} 天"
    equity = state["cash"] + sum(_value(p, p["last"]) for p in state["positions"])
    prev_eq = state["curve"][-1]["equity"] if state["curve"] else CAPITAL
    bench_eq = state["bench"]["cash"] + state["bench"]["units"] * state["bench"]["last"] if state["bench"] else CAPITAL
    state["curve"].append({"date": d.isoformat(), "equity": round(equity, 2), "bench": round(bench_eq, 2)})
    state["peak"] = max(state["peak"], equity)
    state["no_new_for"] = next_session(d).isoformat() if equity < prev_eq * (1 - DAILY_LOSS_HALT) else None
    if equity <= state["peak"] * (1 - DRAWDOWN_HALT) and not state["halted"]:
        state["halted"] = {"date": d.isoformat(), "why": f"从峰值回撤超过 {DRAWDOWN_HALT:.0%}"}
        for p in state["positions"]:
            p["close_next"] = "回撤停机，全部平仓"
    state["start"] = state["start"] or d.isoformat()
    state["last_session"] = d.isoformat()
    state["events"] = (state["events"] + log)[-50:]


# ---------------------------------------------------------------- view for the web page and pushes

def build_view(state: dict) -> dict:
    curve = state["curve"]
    equity = curve[-1]["equity"] if curve else state["capital"]
    bench = curve[-1]["bench"] if curve else state["capital"]
    last = date.fromisoformat(state["last_session"])
    decision = load_decision(last)
    positions = []
    for p in state["positions"]:
        last_px = p.get("last", p["entry_price"])
        positions.append({k: p.get(k) for k in ("symbol", "ticker", "side", "qty", "entry_date", "rating", "why", "hold_days", "held")} | {
            "entry_price": round(p["entry_price"], 2), "last": round(last_px, 2), "stop": round(p["stop"], 2),
            "value": round(p["qty"] * last_px, 2), "unrealized": round(_value(p, last_px) + p["funding"] - p["entry_fee"], 2),
            "unrealized_pct": round(_sgn(p["side"]) * (last_px / p["entry_price"] - 1) * 100, 2),
            "close_next": p.get("close_next")})
    trades = state["trades"]
    rets = [t["ret_pct"] for t in trades]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "as_of": state["last_session"], "next_session": next_session(last).isoformat(),
        "start": state["start"],
        "account": {"capital": state["capital"], "equity": round(equity, 2), "cash": round(state["cash"], 2),
                    "return_pct": round((equity / state["capital"] - 1) * 100, 2),
                    "bench_return_pct": round((bench / state["capital"] - 1) * 100, 2),
                    "drawdown_pct": round((equity / state["peak"] - 1) * 100, 2),
                    "gross_pct": round(sum(p["value"] for p in positions) / equity * 100, 1) if equity else 0,
                    "fees": round(state["fees"], 2), "funding": round(state["funding"], 2),
                    "halted": state["halted"] or ({"why": "手动停机（agent/HALT）"} if HALT_PATH.exists() else None),
                    "no_new_for": state["no_new_for"]},
        "positions": positions,
        "decision": decision,
        "trades": list(reversed(trades)),
        "stats": {"trades": len(rets), "win_rate": round(sum(r > 0 for r in rets) / len(rets), 3) if rets else None,
                  "avg_ret_pct": round(sum(rets) / len(rets), 3) if rets else None,
                  "pnl": round(sum(t["pnl"] for t in trades), 2)},
        "curve": curve,
        "events": list(reversed(state["events"]))[:20],
        "rules": RULES,
    }


PAGE_SKELETON = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                 '</head><body>\n{body}\n</body></html>\n')


def write_view(state: dict | None = None) -> dict:
    state = state or load_json(STATE_PATH)
    view = build_view(state)
    save_json(VIEW_PATH, view)
    PAGE_PATH.write_text(PAGE_SKELETON.format(body=PAGE_SOURCE.read_text(encoding="utf-8")), encoding="utf-8")
    return view


def run(now: datetime | None = None) -> dict:
    """Refresh the tradable list and prices, book every session closed since the last run, write the view."""
    uni = fetch_universe()
    save_json(UNIVERSE_PATH, {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "symbols": uni})
    syms = {u["symbol"] for u in uni} | {BENCH}
    save_json(MARKET_PATH, {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "marks": fetch_marks(syms)})
    target = latest_closed_session(now)
    state = load_json(STATE_PATH) or new_state(target)  # first run: start booking from the next session
    by_symbol = {u["symbol"]: u for u in uni}
    booked = []
    while date.fromisoformat(state["last_session"]) < target:
        d = next_session(date.fromisoformat(state["last_session"]))
        log: list = []
        process_session(state, d, by_symbol, log)
        booked.append(d.isoformat())
    if booked or not STATE_PATH.exists():
        save_json(STATE_PATH, state)
    view = write_view(state)
    a = view["account"]
    print(f"AI 交易台：记账到 {state['last_session']}（本次 {', '.join(booked) or '无新交易日'}），"
          f"权益 {a['equity']:,.2f} USDT（{a['return_pct']:+.2f}%，同期 QQQ {a['bench_return_pct']:+.2f}%），持仓 {len(view['positions'])} 只")
    return view


if __name__ == "__main__":
    run()
