"""Daily paper-trading records for the swing strategies we keep watching.

Run once after each US close. For every strategy it fills yesterday's decisions at today's open,
applies intraday stops, decides at today's close (the same signal code as the backtests, from
swing_strategies), and writes:
  paper/<strategy>.json  - that strategy's simulated $1,300 account
  docs/data.json         - everything the dashboard shows

Each strategy has its own account and its own HSBC quota, so they can be compared side by side.
Missed days are caught up on the next run. Prices come from Yahoo daily bars (no API key needed).
"""

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import holidays
import numpy as np
import pandas as pd
import yfinance as yf

from .config import ROOT
from .costs import HsbcCosts
from .swing import Panel, Signals, sma
from .swing_strategies import LukPullback, Pullback3, ema
from .universe import get_universe

ET = ZoneInfo("America/New_York")
BENCH = "QQQ"
PAPER_DIR = ROOT / "paper"
UNIVERSE_PATH = PAPER_DIR / "universe.csv"  # last good member list, for when the sources are unreachable
DATA_PATH = ROOT / "docs" / "data.json"
PAGE_PATH = ROOT / "docs" / "index.html"
PAGE_SOURCE = ROOT / "src" / "quantify" / "dashboard.html"
PAGE_SKELETON = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                 '</head><body>\n{body}\n</body></html>\n')


def usd(v: float) -> str:
    return f"${v:,.2f}"


# ---------------------------------------------------------------- calendar

def next_session(after: date) -> date:
    d = after + timedelta(days=1)
    hol = set(holidays.financial_holidays("NYSE", years=[d.year, d.year + 1]))
    while d.weekday() >= 5 or d in hol:
        d += timedelta(days=1)
    return d


def open_time(day: date) -> datetime:
    """The regular-session open in New York time; the dashboard converts it to the viewer's zone."""
    return datetime.combine(day, time(9, 30), tzinfo=ET)


# ---------------------------------------------------------------- data

def fetch_panel(symbols: list[str], period: str = "2y") -> tuple[Panel, pd.DataFrame, pd.DataFrame]:
    """Yahoo daily bars -> (adjusted Panel, raw closes, split ratios). Only finished sessions."""
    raw = yf.download(symbols, period=period, interval="1d", auto_adjust=False, actions=True,
                      group_by="ticker", progress=False, threads=True)
    got = [s for s in symbols if s in raw.columns.get_level_values(0)]
    # Yahoo sometimes leaves columns out for some tickers (seen on GitHub's runners): no split
    # column means no split, no adjusted close means no adjustment.
    fallback = {"Stock Splits": lambda df: pd.Series(0.0, index=df.index), "Adj Close": lambda df: df["Close"]}
    patched = set()
    fields = {}
    for name in ("Open", "High", "Low", "Close", "Adj Close", "Volume", "Stock Splits"):
        cols = {}
        for s in got:
            df = raw[s]
            if name in df.columns:
                cols[s] = df[name]
            elif name in fallback and "Close" in df.columns:
                cols[s] = fallback[name](df)
                patched.add(s)
        fields[name] = pd.DataFrame(cols)
    if patched:
        print(f"Yahoo 少给了部分字段，已按默认值补上：{', '.join(sorted(patched))}")
    idx = pd.to_datetime(fields["Close"].index).date
    for f in fields.values():
        f.index = idx
    now = datetime.now(ET)
    if now.time() < time(16, 15):  # today's bar is still forming
        keep = [d for d in idx if d < now.date()]
        fields = {k: v.loc[keep] for k, v in fields.items()}
    close = fields["Close"]
    have = close.iloc[-1].notna().mean() if len(close) else 0
    if len(close) < 250 or have < 0.9:
        raise RuntimeError(f"Yahoo 数据不完整（{len(close)} 天，最后一天 {have:.0%} 的股票有数据），本次不更新")
    factor = close / fields["Adj Close"]  # raw = adjusted * factor
    panel = Panel(fields["Open"] / factor, fields["High"] / factor, fields["Low"] / factor,
                  fields["Adj Close"], fields["Volume"] * factor, factor)
    splits = fields["Stock Splits"].replace(0, np.nan)
    return panel, close, splits


def _members(cfg: dict) -> list[str]:
    try:
        symbols = get_universe(cfg, refresh=True)
    except RuntimeError:
        if not UNIVERSE_PATH.exists():
            raise
        print("成分股来源都无法访问，使用上一次保存的名单")
        return pd.read_csv(UNIVERSE_PATH)["symbol"].tolist()
    PAPER_DIR.mkdir(exist_ok=True)
    pd.DataFrame({"symbol": symbols}).to_csv(UNIVERSE_PATH, index=False)
    return symbols


class Market:
    """Today's prices in the units the account needs: raw (as traded) and split-aware."""

    def __init__(self, panel: Panel, close_raw: pd.DataFrame, splits: pd.DataFrame):
        self.panel, self.close_raw, self.splits = panel, close_raw, splits

    def raw(self, field: str, d: date, sym: str) -> float:
        p = self.panel
        return float(getattr(p, field).at[d, sym] * p.factor.at[d, sym])

    def split_ratio(self, sym: str, after: date, upto: date) -> float:
        s = self.splits[sym].dropna() if sym in self.splits else pd.Series(dtype=float)
        s = s[(s.index > after) & (s.index <= upto)]
        return float(s.prod()) if len(s) else 1.0

    def position_now(self, pos: dict, d: date) -> tuple[float, float, float]:
        """(shares, entry price, stop) in today's share terms, after any split since the entry."""
        r = self.split_ratio(pos["symbol"], date.fromisoformat(pos["entry_date"]), d)
        stop = pos.get("stop")
        return pos["shares"] * r, pos["entry_price"] / r, (stop / r if stop is not None else None)


# ---------------------------------------------------------------- strategies

@dataclass
class Strategy:
    key: str
    name: str
    tagline: str
    signals: Callable[[Panel], Signals]
    exit_reason: str
    rules: list[str]
    backtest: dict
    candidate_cols: list[dict]
    watch_title: str
    watch_sub: str
    watch_cols: list[dict]
    describe: Callable[[Market, Signals, date, str], dict]
    watchlist: Callable[[Market, Signals, date], pd.DataFrame]
    position_rule: Callable[[Market, dict, date], str]
    note: str = ""


def _ret(c: pd.Series, n: int) -> float:
    return round((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 2)


# 3-day pullback -------------------------------------------------------------

P3_PARAMS = {"down_days": 3, "max_hold": 10}


def _p3_describe(m: Market, sig: Signals, d: date, s: str) -> dict:
    c = m.panel.close[s].loc[:d]
    return {"symbol": s, "close": round(float(m.close_raw.at[d, s]), 2), "chg3_pct": _ret(c, 3),
            "above_sma200_pct": round((c.iloc[-1] / c.iloc[-200:].mean() - 1) * 100, 1)}


def _p3_watch(m: Market, sig: Signals, d: date) -> pd.DataFrame:
    c = m.panel.close
    down = (c < c.shift()).astype(int)
    two = (down.rolling(2).sum() == 2) & (down.rolling(3).sum() < 3) & (c > sma(c, 200))
    rows = []
    for s in [s for s in two.columns if s != BENCH and bool(two.at[d, s])]:
        cs = c[s].loc[:d]
        rows.append({"symbol": s, "close": round(float(m.close_raw.at[d, s]), 2), "chg2_pct": _ret(cs, 2),
                     "above_sma200_pct": round((cs.iloc[-1] / cs.iloc[-200:].mean() - 1) * 100, 1)})
    df = pd.DataFrame(rows, columns=["symbol", "close", "chg2_pct", "above_sma200_pct"])
    return df.sort_values("chg2_pct").reset_index(drop=True)


def _p3_rule(m: Market, pos: dict, d: date) -> str:
    high = m.raw("high", d, pos["symbol"])
    return (f"下一个交易日收盘若高于 [[{usd(high)}]]（今天最高价），再下一个交易日开盘卖出；"
            f"持满 {P3_PARAMS['max_hold']} 天也会卖出。")


PULLBACK3 = Strategy(
    key="pullback3", name="三连跌回调", tagline="长期上升趋势中的短线超跌反弹",
    signals=lambda p: Pullback3.signals(p, P3_PARAMS), exit_reason="收盘高于前一天最高价",
    rules=["股价在 200 日均线之上，连续 3 天收跌：下一个交易日开盘买入。",
           "同一天有多只时，买 3 日跌幅最深、而且买得起的那只。",
           "收盘高于前一天最高价：下一个交易日开盘卖出。",
           "最多持有 10 天；只在免佣额度够用时买入。"],
    # Survivorship-free backtest 2016-01 to 2026-10, fixed $1,300 per trade, net of all costs.
    backtest={"trades": 562, "avg_ret_pct": 0.357, "median_ret_pct": 0.878, "win_rate": 0.664,
              "avg_win_pct": 2.41, "avg_loss_pct": -3.70, "hold_days_median": 3,
              "trades_per_month": 4.3, "worst_pct": -40.3, "best_pct": 16.2},
    candidate_cols=[{"key": "chg3_pct", "label": "3 日涨跌", "fmt": "pct", "signed": True},
                    {"key": "above_sma200_pct", "label": "高于 200 日线", "fmt": "pct1"}],
    watch_title="观察名单", watch_sub="已连续 2 天收跌。下一个交易日收盘低于今天收盘价，就会出现信号。",
    watch_cols=[{"key": "close", "label": "今天收盘", "fmt": "usd"},
                {"key": "chg2_pct", "label": "2 日涨跌", "fmt": "pct", "signed": True},
                {"key": "above_sma200_pct", "label": "高于 200 日线", "fmt": "pct1"}],
    describe=_p3_describe, watchlist=_p3_watch, position_rule=_p3_rule,
)


# Martin Luk-style pullback ---------------------------------------------------

LUK_PARAMS = {"support": "ema9", "trail": "ema21", "max_stop": 0.04, "trim_r": None}


def _luk_setup(c: pd.DataFrame) -> pd.DataFrame:
    e9, e21, e50 = ema(c, 9), ema(c, 21), ema(c, 50)
    strong = (c / c.shift(21) - 1 >= 0.30) | (c / c.shift(63) - 1 >= 0.30) | (c / c.shift(126) - 1 >= 0.30)
    return strong & (e9 > e21) & (e21 > e50) & (e21 > e21.shift(5))


def _luk_describe(m: Market, sig: Signals, d: date, s: str) -> dict:
    c = m.panel.close[s].loc[:d]
    close, low = float(m.close_raw.at[d, s]), m.raw("low", d, s)
    return {"symbol": s, "close": round(close, 2), "stop": round(low, 2),
            "stop_pct": round((low / close - 1) * 100, 2), "ret63_pct": _ret(c, 63)}


def _luk_watch(m: Market, sig: Signals, d: date) -> pd.DataFrame:
    c = m.panel.close
    setup = _luk_setup(c)
    e9 = ema(c, 9)
    rows = []
    for s in [s for s in c.columns if s != BENCH and bool(setup.at[d, s])]:
        gap = (c.at[d, s] / e9.at[d, s] - 1) * 100
        if 0 <= gap <= 3:
            rows.append({"symbol": s, "close": round(float(m.close_raw.at[d, s]), 2),
                         "support": round(float(e9.at[d, s] * m.panel.factor.at[d, s]), 2),
                         "gap_pct": round(gap, 2), "ret63_pct": _ret(c[s].loc[:d], 63)})
    df = pd.DataFrame(rows, columns=["symbol", "close", "support", "gap_pct", "ret63_pct"])
    return df.sort_values("gap_pct").reset_index(drop=True)


def _luk_rule(m: Market, pos: dict, d: date) -> str:
    _, _, stop = m.position_now(pos, d)
    trail = float(ema(m.panel.close[[pos["symbol"]]], 21).at[d, pos["symbol"]] * m.panel.factor.at[d, pos["symbol"]])
    return (f"止损 [[{usd(stop)}]]：盘中跌破就按止损价卖出。"
            f"收盘跌破 21 日 EMA（今天 [[{usd(trail)}]]），下一个交易日开盘卖出。没有持有天数上限。")


LUK = Strategy(
    key="luk", name="Martin Luk 风格回调", tagline="强势股回踩均线后买入，紧止损、让赢家跑",
    signals=lambda p: LukPullback.signals(p, LUK_PARAMS), exit_reason="收盘跌破 21 日 EMA",
    rules=["近 1/3/6 个月任一期间涨幅 ≥30%，9 日 EMA > 21 日 EMA > 50 日 EMA，且 21 日 EMA 向上。",
           "当天最低价跌破 9 日 EMA，但收盘收回到它之上、并收在当天振幅的上半部：下一个交易日开盘买入。",
           "止损设在信号当天的最低价；开盘价离止损超过 4%（或已跌破）就不买。",
           "收盘跌破 21 日 EMA：下一个交易日开盘卖出。同一天有多只时，买 3 个月涨幅最大的。"],
    # Survivorship-free backtest 2016-01 to 2026-10 (Nasdaq-100), same costs; see README.
    backtest={"trades": 240, "avg_ret_pct": 0.617, "median_ret_pct": -1.454, "win_rate": 0.238,
              "avg_win_pct": 9.49, "avg_loss_pct": -2.15, "hold_days_median": 2,
              "trades_per_month": 1.8, "worst_pct": -11.3, "best_pct": 59.5},
    candidate_cols=[{"key": "stop", "label": "止损（今天最低）", "fmt": "usd"},
                    {"key": "stop_pct", "label": "止损距离", "fmt": "pct"},
                    {"key": "ret63_pct", "label": "3 个月涨幅", "fmt": "pct1"}],
    watch_title="接近支撑的强势股",
    watch_sub="已满足强势条件，收盘离 9 日 EMA 不到 3%。下一个交易日盘中跌破 9 日 EMA 后收回，就会出现信号。",
    watch_cols=[{"key": "close", "label": "今天收盘", "fmt": "usd"},
                {"key": "support", "label": "9 日 EMA", "fmt": "usd"},
                {"key": "gap_pct", "label": "离支撑", "fmt": "pct1"},
                {"key": "ret63_pct", "label": "3 个月涨幅", "fmt": "pct1"}],
    describe=_luk_describe, watchlist=_luk_watch, position_rule=_luk_rule,
    note="胜率约 24%，大多数交易是小亏，靠少数大赚的交易拉高平均。连续亏损 10 笔以上在回测里很常见。",
)

STRATEGIES = [PULLBACK3, LUK]


# ---------------------------------------------------------------- state

def state_path(key: str):
    return PAPER_DIR / f"{key}.json"


def new_state(capital: float, start: date) -> dict:
    return {"start_date": start.isoformat(), "capital": capital, "cash": capital, "position": None,
            "pending": [], "trades": [], "skipped": [], "equity_curve": [], "quota_used_hkd": {},
            "months_charged": [], "fees_paid": 0.0, "last_processed": None}


def load_state(key: str) -> dict | None:
    legacy = PAPER_DIR / "state.json"  # before there was more than one strategy
    if key == PULLBACK3.key and legacy.exists() and not state_path(key).exists():
        legacy.rename(state_path(key))
    path = state_path(key)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_state(key: str, state: dict) -> None:
    PAPER_DIR.mkdir(exist_ok=True)
    state_path(key).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- daily processing

def process_day(st: Strategy, state: dict, d: date, m: Market, sig: Signals, costs: HsbcCosts,
                cfg: dict) -> None:
    slip = cfg["execution"]["slippage_pct"]
    month = d.strftime("%Y-%m")
    panel = m.panel

    def charge_month():
        if month not in state["months_charged"]:
            state["months_charged"].append(month)
            state["cash"] -= costs.monthly_fee_usd()
            state["fees_paid"] += costs.monthly_fee_usd()

    def close_position(px: float, reason: str):
        pos = state["position"]
        shares, entry_px, _ = m.position_now(pos, d)
        fees = costs.fill(month, shares * px, int(shares))
        state["cash"] += shares * px - fees
        state["fees_paid"] += fees
        pnl = shares * (px - entry_px) - fees - pos["entry_fees"]
        state["trades"].append({
            "symbol": pos["symbol"], "entry_date": pos["entry_date"], "entry_price": round(entry_px, 2),
            "exit_date": d.isoformat(), "exit_price": round(px, 2), "shares": round(shares, 4),
            "held_days": pos["held_days"], "reason": reason, "fees": round(fees + pos["entry_fees"], 2),
            "pnl": round(pnl, 2), "ret_pct": round(pnl / (shares * entry_px) * 100, 3)})
        state["position"] = None
        charge_month()

    def has_open(sym):
        return sym in panel.open.columns and np.isfinite(panel.open.at[d, sym])

    # 1) Fill yesterday's decisions at today's open: sells first, then buys.
    for order in sorted(state["pending"], key=lambda o: o["action"] != "sell"):
        sym = order["symbol"]
        if not has_open(sym):
            state["skipped"].append({"date": d.isoformat(), "symbol": sym, "reason": "当天没有行情数据"})
            continue
        if order["action"] == "sell" and state["position"]:
            close_position(m.raw("open", d, sym) * (1 - slip), order["reason"])
        elif order["action"] == "buy" and state["position"] is None:
            px = m.raw("open", d, sym) * (1 + slip)
            stop = order.get("stop")
            if stop is not None and sig.max_stop is not None and not (0 < (px - stop) / px <= sig.max_stop):
                why = "开盘已跌破止损价" if px <= stop else f"开盘价离止损超过 {sig.max_stop:.0%}"
                state["skipped"].append({"date": d.isoformat(), "symbol": sym, "reason": why})
                continue
            shares = int(state["cash"] // px)
            if shares < 1:
                state["skipped"].append({"date": d.isoformat(), "symbol": sym, "reason": "开盘价高于可用资金"})
                continue
            if cfg["broker"]["respect_quota"] and not costs.fits_quota(month, shares * px * 2 * 1.05):
                state["skipped"].append({"date": d.isoformat(), "symbol": sym, "reason": "本月免佣额度不足"})
                continue
            fees = costs.fill(month, shares * px, shares)
            state["cash"] -= shares * px + fees
            state["fees_paid"] += fees
            state["position"] = {"symbol": sym, "entry_date": d.isoformat(), "entry_price": round(px, 4),
                                 "shares": shares, "held_days": 0, "entry_fees": fees, "stop": stop}
            charge_month()
    state["pending"] = []

    # 2) Intraday stop (a gap below it fills at the open).
    pos = state["position"]
    if pos and pos.get("stop") is not None and has_open(pos["symbol"]):
        _, _, stop = m.position_now(pos, d)
        if m.raw("low", d, pos["symbol"]) <= stop:
            close_position(min(m.raw("open", d, pos["symbol"]), stop) * (1 - slip), "盘中跌破止损")

    # 3) Decisions at today's close (as in the backtest: exit first, then a new entry).
    pos = state["position"]
    exiting = False
    if pos:
        charge_month()
        pos["held_days"] += 1
        sym = pos["symbol"]
        reason = (st.exit_reason if bool(sig.exit.at[d, sym])
                  else f"持有满 {sig.max_hold} 天" if pos["held_days"] >= sig.max_hold else None)
        if reason:
            state["pending"].append({"action": "sell", "symbol": sym, "reason": reason,
                                     "ref_price": round(float(m.close_raw.at[d, sym]), 2)})
            exiting = True
    if pos is None or exiting:
        cash_after = state["cash"] + (m.position_now(pos, d)[0] * float(m.close_raw.at[d, pos["symbol"]]) if exiting else 0)
        for c in candidates(st, m, sig, d):
            sym = c["symbol"]
            if exiting and sym == pos["symbol"]:
                continue
            if c["close"] > cash_after:
                continue  # the backtest skips unaffordable picks; live we take the best affordable one
            order = {"action": "buy", "symbol": sym, "reason": f"{st.name}信号", "ref_price": c["close"],
                     "est_shares": int(cash_after // (c["close"] * (1 + slip)))}
            if sig.stop_price is not None:
                order["stop"] = round(m.raw("low", d, sym), 4)
            state["pending"].append(order)
            break

    # 4) Mark to market.
    equity = state["cash"]
    if state["position"]:
        shares, _, _ = m.position_now(state["position"], d)
        equity += shares * float(m.close_raw.at[d, state["position"]["symbol"]])
    state["equity_curve"].append({"date": d.isoformat(), "equity": round(equity, 2),
                                  "qqq": round(float(m.close_raw.at[d, BENCH]), 2)})
    state["quota_used_hkd"] = dict(costs._used_hkd)
    state["last_processed"] = d.isoformat()


def candidates(st: Strategy, m: Market, sig: Signals, d: date) -> list[dict]:
    """Today's entry signals, best first by the strategy's own score."""
    syms = [s for s in sig.entry.columns if s != BENCH and bool(sig.entry.at[d, s])]
    syms.sort(key=lambda s: float(sig.score.at[d, s]), reverse=True)
    return [st.describe(m, sig, d, s) for s in syms]


# ---------------------------------------------------------------- entry point

def run(cfg: dict, start: date | None = None, reset: bool = False, only: str | None = None) -> dict:
    m = Market(*fetch_panel(_members(cfg) + [BENCH]))
    days = list(m.panel.close.index)
    last_day = days[-1]
    views = []
    for st in STRATEGIES:
        touch = only is None or only == st.key
        state = None if (reset and touch) else load_state(st.key)
        if state is None:
            state = new_state(float(cfg["account"]["capital_usd"]), (start if touch else None) or last_day)
        costs = HsbcCosts.from_config(cfg)
        costs._used_hkd.update(state["quota_used_hkd"])
        sig = st.signals(m.panel)
        start_d = date.fromisoformat(state["start_date"])
        done = date.fromisoformat(state["last_processed"]) if state["last_processed"] else None
        for d in days:
            if d >= start_d and (done is None or d > done):
                process_day(st, state, d, m, sig, costs, cfg)
        save_state(st.key, state)
        views.append(strategy_view(st, state, m, sig, last_day, cfg))

    nxt = next_session(last_day)
    view = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"), "as_of": last_day.isoformat(),
            "next_session": nxt.isoformat(), "next_open": open_time(nxt).isoformat(timespec="minutes"),
            "strategies": views}
    DATA_PATH.parent.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps(view, ensure_ascii=False, indent=1), encoding="utf-8")
    PAGE_PATH.write_text(PAGE_SKELETON.format(body=PAGE_SOURCE.read_text(encoding="utf-8")), encoding="utf-8")
    return view


def strategy_view(st: Strategy, state: dict, m: Market, sig: Signals, d: date, cfg: dict) -> dict:
    cap = state["capital"]
    curve = state["equity_curve"]
    equity = curve[-1]["equity"] if curve else cap
    q0 = curve[0]["qqq"] if curve else None
    pos_view = None
    if state["position"]:
        p = state["position"]
        shares, entry_px, stop = m.position_now(p, d)
        last = float(m.close_raw.at[d, p["symbol"]])
        pos_view = {"symbol": p["symbol"], "entry_date": p["entry_date"], "entry_price": round(entry_px, 2),
                    "shares": round(shares, 4), "last": round(last, 2), "held_days": p["held_days"],
                    "max_hold": sig.max_hold if sig.max_hold < 1000 else None,
                    "stop": round(stop, 2) if stop is not None else None,
                    "unrealized": round(shares * (last - entry_px), 2),
                    "unrealized_pct": round((last / entry_px - 1) * 100, 2),
                    "rule": st.position_rule(m, p, d)}
    trades = state["trades"]
    quota = cfg["broker"]["monthly_free_quota_hkd"]
    used = state["quota_used_hkd"].get(d.strftime("%Y-%m"), 0.0)
    return {
        "key": st.key, "name": st.name, "tagline": st.tagline, "note": st.note, "rules": st.rules,
        "start_date": state["start_date"],
        "account": {"capital": cap, "equity": round(equity, 2), "cash": round(state["cash"], 2),
                    "return_pct": round((equity / cap - 1) * 100, 2),
                    "qqq_return_pct": round((curve[-1]["qqq"] / q0 - 1) * 100, 2) if q0 else 0.0,
                    "fees_paid": round(state["fees_paid"], 2), "quota_used_hkd": round(used), "quota_hkd": quota,
                    "round_trips_left": math.floor(max(quota - used, 0) / (equity * 2 * cfg["account"]["usd_hkd"]))},
        "tonight": state["pending"],
        "position": pos_view,
        "candidate_cols": st.candidate_cols,
        "candidates": candidates(st, m, sig, d)[:10],
        "watch_title": st.watch_title, "watch_sub": st.watch_sub, "watch_cols": st.watch_cols,
        "watchlist": st.watchlist(m, sig, d).head(10).to_dict("records"),
        "trades": list(reversed(trades)),
        "skipped": list(reversed(state["skipped"]))[:10],
        "equity_curve": curve,
        "live_stats": _live_stats(trades),
        "backtest": st.backtest,
    }


def _live_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    r = np.array([t["ret_pct"] for t in trades])
    return {"trades": len(trades), "avg_ret_pct": round(float(r.mean()), 3),
            "median_ret_pct": round(float(np.median(r)), 3), "win_rate": round(float((r > 0).mean()), 3),
            "total_pnl": round(sum(t["pnl"] for t in trades), 2)}
