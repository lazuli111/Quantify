"""Daily paper-trading record for the 3-day pullback swing strategy.

Run once after each US close. It fills yesterday's decisions at today's open, decides at today's
close (same rules as the backtest, via swing_strategies.Pullback3), and writes:
  paper/state.json  - the simulated account (cash, position, pending orders, trades, quota)
  docs/data.json    - everything the dashboard shows

Missed days are caught up on the next run. Prices come from Yahoo daily bars (no API key needed).
"""

import json
import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import holidays
import numpy as np
import pandas as pd
import yfinance as yf

from .config import ROOT
from .costs import HsbcCosts
from .swing import Panel, sma
from .swing_strategies import Pullback3
from .universe import get_universe

ET, HK = ZoneInfo("America/New_York"), ZoneInfo("Asia/Hong_Kong")
PARAMS = {"down_days": 3, "max_hold": 10}
BENCH = "QQQ"
STATE_PATH = ROOT / "paper" / "state.json"
UNIVERSE_PATH = ROOT / "paper" / "universe.csv"  # last good member list, for when the sources are unreachable
DATA_PATH = ROOT / "docs" / "data.json"
PAGE_PATH = ROOT / "docs" / "index.html"
PAGE_SOURCE = ROOT / "src" / "quantify" / "dashboard.html"
PAGE_SKELETON = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                 '</head><body>\n{body}\n</body></html>\n')

# Survivorship-free backtest of the same rules, 2016-01 to 2026-10, fixed $1,300 per trade, net of
# HSBC costs and 0.05% slippage (see README). Shown next to the live record for comparison.
BACKTEST_REF = {"trades": 562, "avg_ret_pct": 0.357, "median_ret_pct": 0.878, "win_rate": 0.664,
                "avg_win_pct": 2.41, "avg_loss_pct": -3.70, "hold_days_median": 3,
                "trades_per_month": 4.3, "worst_pct": -40.3, "best_pct": 16.2}


# ---------------------------------------------------------------- calendar

def _nyse_holidays(year: int) -> set[date]:
    return set(holidays.financial_holidays("NYSE", years=[year, year + 1]))


def next_session(after: date) -> date:
    d = after + timedelta(days=1)
    hol = _nyse_holidays(d.year)
    while d.weekday() >= 5 or d in hol:
        d += timedelta(days=1)
    return d


def open_time_hk(day: date) -> datetime:
    return datetime.combine(day, time(9, 30), tzinfo=ET).astimezone(HK)


# ---------------------------------------------------------------- data

def fetch_panel(symbols: list[str], period: str = "2y") -> tuple[Panel, pd.DataFrame, pd.DataFrame]:
    """Yahoo daily bars -> (adjusted Panel, raw closes, split ratios). Only finished sessions."""
    raw = yf.download(symbols, period=period, interval="1d", auto_adjust=False, actions=True,
                      group_by="ticker", progress=False, threads=True)
    fields = {}
    for name in ("Open", "High", "Low", "Close", "Adj Close", "Volume", "Stock Splits"):
        fields[name] = pd.DataFrame({s: raw[s][name] for s in symbols if s in raw.columns.get_level_values(0)})
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


# ---------------------------------------------------------------- state

def new_state(capital: float, start: date) -> dict:
    return {"start_date": start.isoformat(), "capital": capital, "cash": capital, "position": None,
            "pending": [], "trades": [], "skipped": [], "equity_curve": [], "quota_used_hkd": {},
            "months_charged": [], "fees_paid": 0.0, "last_processed": None}


def load_state() -> dict | None:
    return json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else None


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- daily processing

def _split_ratio(splits: pd.DataFrame, sym: str, after: date, upto: date) -> float:
    s = splits[sym].dropna() if sym in splits else pd.Series(dtype=float)
    s = s[(s.index > after) & (s.index <= upto)]
    return float(s.prod()) if len(s) else 1.0


def _position_now(pos: dict, splits: pd.DataFrame, d: date) -> tuple[float, float]:
    """(shares, entry price) in today's share terms, after any split since the entry."""
    r = _split_ratio(splits, pos["symbol"], date.fromisoformat(pos["entry_date"]), d)
    return pos["shares"] * r, pos["entry_price"] / r


def process_day(state: dict, d: date, panel: Panel, close_raw: pd.DataFrame, splits: pd.DataFrame,
                sig, costs: HsbcCosts, cfg: dict) -> None:
    slip = cfg["execution"]["slippage_pct"]
    month = d.strftime("%Y-%m")
    fee_month = costs.monthly_fee_usd()

    def raw_open(sym):
        return panel.open.at[d, sym] * panel.factor.at[d, sym]

    def charge_month():
        if month not in state["months_charged"]:
            state["months_charged"].append(month)
            state["cash"] -= fee_month
            state["fees_paid"] += fee_month

    # 1) Fill yesterday's decisions at today's open: sells first, then buys.
    for order in sorted(state["pending"], key=lambda o: o["action"] != "sell"):
        sym = order["symbol"]
        if sym not in panel.open.columns or not np.isfinite(panel.open.at[d, sym]):
            state["skipped"].append({"date": d.isoformat(), "symbol": sym, "reason": "当天没有行情数据"})
            continue
        if order["action"] == "sell" and state["position"]:
            pos = state["position"]
            shares, entry_px = _position_now(pos, splits, d)
            px = raw_open(sym) * (1 - slip)
            fees = costs.fill(month, shares * px, int(shares))
            state["cash"] += shares * px - fees
            state["fees_paid"] += fees
            pnl = shares * (px - entry_px) - fees - pos["entry_fees"]
            state["trades"].append({
                "symbol": sym, "entry_date": pos["entry_date"], "entry_price": round(entry_px, 2),
                "exit_date": d.isoformat(), "exit_price": round(px, 2), "shares": round(shares, 4),
                "held_days": pos["held_days"], "reason": order["reason"], "fees": round(fees + pos["entry_fees"], 2),
                "pnl": round(pnl, 2), "ret_pct": round(pnl / (shares * entry_px) * 100, 3)})
            state["position"] = None
            charge_month()
        elif order["action"] == "buy" and state["position"] is None:
            px = raw_open(sym) * (1 + slip)
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
                                 "shares": shares, "held_days": 0, "entry_fees": fees}
            charge_month()
    state["pending"] = []

    # 2) Decisions at today's close (identical to the backtest: exit first, then a new entry).
    pos = state["position"]
    exiting = False
    if pos:
        charge_month()
        pos["held_days"] += 1
        sym = pos["symbol"]
        if sig.exit.at[d, sym]:
            reason = "收盘高于前一天最高价"
        elif pos["held_days"] >= sig.max_hold:
            reason = f"持有满 {sig.max_hold} 天"
        else:
            reason = None
        if reason:
            state["pending"].append({"action": "sell", "symbol": sym, "reason": reason,
                                     "ref_price": round(float(close_raw.at[d, sym]), 2)})
            exiting = True
    if pos is None or exiting:
        cash_after = state["cash"] + (_position_now(pos, splits, d)[0] * close_raw.at[d, pos["symbol"]] if exiting else 0)
        for sym in candidates(panel, close_raw, sig, d)["symbol"]:
            if exiting and sym == pos["symbol"]:
                continue
            px = float(close_raw.at[d, sym])
            if px > cash_after:
                continue  # the backtest skips unaffordable picks; live we take the best affordable one
            state["pending"].append({"action": "buy", "symbol": sym, "reason": "三连跌回调信号",
                                     "ref_price": round(px, 2), "est_shares": int(cash_after // (px * (1 + slip)))})
            break

    # 3) Mark to market.
    equity = state["cash"]
    if state["position"]:
        shares, _ = _position_now(state["position"], splits, d)
        equity += shares * float(close_raw.at[d, state["position"]["symbol"]])
    state["equity_curve"].append({"date": d.isoformat(), "equity": round(equity, 2),
                                  "qqq": round(float(close_raw.at[d, BENCH]), 2)})
    state["quota_used_hkd"] = dict(costs._used_hkd)
    state["last_processed"] = d.isoformat()


def candidates(panel: Panel, close_raw: pd.DataFrame, sig, d: date) -> pd.DataFrame:
    """Today's entry signals, best first (deepest 3-day pullback)."""
    syms = [s for s in sig.entry.columns if s != BENCH and bool(sig.entry.at[d, s])]
    rows = [_describe(panel, close_raw, s, d) | {"score": float(sig.score.at[d, s])} for s in syms]
    df = pd.DataFrame(rows, columns=["symbol", "close", "chg3_pct", "above_sma200_pct", "score"])
    return df.sort_values("score", ascending=False).reset_index(drop=True)


def watchlist(panel: Panel, close_raw: pd.DataFrame, d: date) -> pd.DataFrame:
    """Stocks one more down day away from a signal: above the 200-day average, down 2 days in a row."""
    c = panel.close
    down = (c < c.shift()).astype(int)
    two = (down.rolling(2).sum() == 2) & (down.rolling(3).sum() < 3) & (c > sma(c, 200))
    syms = [s for s in two.columns if s != BENCH and bool(two.at[d, s])]
    rows = []
    for s in syms:
        row = _describe(panel, close_raw, s, d)
        cs = panel.close[s].loc[:d]
        row["chg2_pct"] = round((cs.iloc[-1] / cs.iloc[-3] - 1) * 100, 2)
        row["trigger_below"] = row["close"]  # a third lower close completes the signal
        rows.append(row)
    df = pd.DataFrame(rows, columns=["symbol", "close", "chg3_pct", "above_sma200_pct", "chg2_pct", "trigger_below"])
    return df.sort_values("chg2_pct").reset_index(drop=True)


def _describe(panel: Panel, close_raw: pd.DataFrame, s: str, d: date) -> dict:
    c = panel.close[s].loc[:d]
    return {"symbol": s, "close": round(float(close_raw.at[d, s]), 2),
            "chg3_pct": round((c.iloc[-1] / c.iloc[-4] - 1) * 100, 2),
            "above_sma200_pct": round((c.iloc[-1] / c.iloc[-200:].mean() - 1) * 100, 1)}


# ---------------------------------------------------------------- entry point

def _members(cfg: dict) -> list[str]:
    try:
        symbols = get_universe(cfg, refresh=True)
    except RuntimeError:
        if not UNIVERSE_PATH.exists():
            raise
        print("成分股来源都无法访问，使用上一次保存的名单")
        return pd.read_csv(UNIVERSE_PATH)["symbol"].tolist()
    UNIVERSE_PATH.parent.mkdir(exist_ok=True)
    pd.DataFrame({"symbol": symbols}).to_csv(UNIVERSE_PATH, index=False)
    return symbols


def run(cfg: dict, start: date | None = None, reset: bool = False) -> dict:
    symbols = _members(cfg)
    panel, close_raw, splits = fetch_panel(symbols + [BENCH])
    days = list(panel.close.index)
    last_day = days[-1]

    state = None if reset else load_state()
    if state is None:
        state = new_state(float(cfg["account"]["capital_usd"]), start or last_day)

    costs = HsbcCosts.from_config(cfg)
    costs._used_hkd.update(state["quota_used_hkd"])
    sig = Pullback3.signals(panel, PARAMS)
    start_d = date.fromisoformat(state["start_date"])
    done = date.fromisoformat(state["last_processed"]) if state["last_processed"] else None
    for d in days:
        if d >= start_d and (done is None or d > done):
            process_day(state, d, panel, close_raw, splits, sig, costs, cfg)
    save_state(state)
    view = build_view(state, panel, close_raw, splits, sig, last_day, cfg)
    DATA_PATH.parent.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps(view, ensure_ascii=False, indent=1), encoding="utf-8")
    PAGE_PATH.write_text(PAGE_SKELETON.format(body=PAGE_SOURCE.read_text(encoding="utf-8")), encoding="utf-8")
    return view


def build_view(state: dict, panel: Panel, close_raw: pd.DataFrame, splits: pd.DataFrame, sig,
               d: date, cfg: dict) -> dict:
    nxt = next_session(d)
    cap = state["capital"]
    curve = state["equity_curve"]
    equity = curve[-1]["equity"] if curve else cap
    q0 = curve[0]["qqq"] if curve else None
    pos_view = None
    if state["position"]:
        p = state["position"]
        shares, entry_px = _position_now(p, splits, d)
        last = float(close_raw.at[d, p["symbol"]])
        high_raw = float(panel.high.at[d, p["symbol"]] * panel.factor.at[d, p["symbol"]])
        pos_view = {"symbol": p["symbol"], "entry_date": p["entry_date"], "entry_price": round(entry_px, 2),
                    "shares": round(shares, 4), "last": round(last, 2), "held_days": p["held_days"],
                    "max_hold": PARAMS["max_hold"], "unrealized": round(shares * (last - entry_px), 2),
                    "unrealized_pct": round((last / entry_px - 1) * 100, 2), "exit_trigger": round(high_raw, 2)}
    trades = state["trades"]
    month = d.strftime("%Y-%m")
    quota = cfg["broker"]["monthly_free_quota_hkd"]
    used = state["quota_used_hkd"].get(month, 0.0)
    return {
        "generated_at": datetime.now(HK).strftime("%Y-%m-%d %H:%M"),
        "as_of": d.isoformat(),
        "next_session": nxt.isoformat(),
        "next_open_hk": open_time_hk(nxt).strftime("%m月%d日 %H:%M"),
        "next_open_hk_time": open_time_hk(nxt).strftime("%H:%M"),
        "start_date": state["start_date"],
        "params": PARAMS,
        "account": {"capital": cap, "equity": round(equity, 2), "cash": round(state["cash"], 2),
                    "return_pct": round((equity / cap - 1) * 100, 2),
                    "qqq_return_pct": round((curve[-1]["qqq"] / q0 - 1) * 100, 2) if q0 else 0.0,
                    "fees_paid": round(state["fees_paid"], 2),
                    "quota_used_hkd": round(used), "quota_hkd": quota,
                    "round_trips_left": math.floor(max(quota - used, 0) / (equity * 2 * cfg["account"]["usd_hkd"]))},
        "tonight": state["pending"],
        "position": pos_view,
        "candidates": candidates(panel, close_raw, sig, d).head(10).to_dict("records"),
        "watchlist": watchlist(panel, close_raw, d).head(10).to_dict("records"),
        "trades": list(reversed(trades)),
        "skipped": list(reversed(state["skipped"]))[:10],
        "equity_curve": curve,
        "live_stats": _live_stats(trades),
        "backtest": BACKTEST_REF,
    }


def _live_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    r = np.array([t["ret_pct"] for t in trades])
    return {"trades": len(trades), "avg_ret_pct": round(float(r.mean()), 3),
            "median_ret_pct": round(float(np.median(r)), 3), "win_rate": round(float((r > 0).mean()), 3),
            "total_pnl": round(sum(t["pnl"] for t in trades), 2)}
