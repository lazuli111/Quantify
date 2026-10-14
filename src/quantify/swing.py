"""Multi-day (swing) backtest: signals at the close, orders at the next open, one position at a time.

Daily OHLC come from our regular-session 5-minute bars, made continuous across splits and
dividends with Alpaca's adjustment factors (so multi-day returns include dividends). Share counts use the raw (unadjusted) price, so "can $1,300 buy one share?"
is answered with the price of the time.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .backtest import Result
from .config import data_dir
from .costs import HsbcCosts
from .data import get_source
from .universe import all_symbols


# ---------------------------------------------------------------- data

@dataclass
class Panel:
    """Dates x symbols tables of adjusted prices, plus the adjustment factor (raw = adj * factor)."""
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    factor: pd.DataFrame

    def subset(self, symbols: list[str]) -> "Panel":
        cols = [c for c in dict.fromkeys(symbols) if c in self.close.columns]
        return Panel(*(getattr(self, f)[cols] for f in ("open", "high", "low", "close", "volume", "factor")))


# Corporate actions the data vendor's adjustment factors miss (found by reviewing every daily move
# above 45%; the rest were real news). ("end", day): history after `day` is a different security.
# ("ratio", day, k): from `day` on, one share equals k of the old ones.
MANUAL_FIXES = {
    "LMCA": ("end", "2016-04-15"),   # Liberty Media recapitalization into tracking stocks
    "LMCK": ("end", "2016-04-15"),
    "AZN": ("ratio", "2026-02-02", 2.0),  # ADR replaced by ordinary shares, 1 new = 2 old
}


def _apply_fixes(d: pd.DataFrame, sym: str) -> pd.DataFrame:
    fix = MANUAL_FIXES.get(sym)
    if not fix:
        return d
    day = pd.Timestamp(fix[1]).date()
    if fix[0] == "end":
        return d[d["date"] <= day]
    d = d.copy()
    d.loc[d["date"] >= day, "factor"] *= fix[2]
    return d


def build_daily(cfg: dict, refresh: bool = False) -> pd.DataFrame:
    path = data_dir(cfg) / "daily.parquet"
    if path.exists() and not refresh:
        return pd.read_parquet(path)
    symbols = [cfg["data"]["benchmark"]] + all_symbols(cfg)
    factors = pd.read_parquet(data_dir(cfg) / "adj_factors.parquet")
    src = get_source(cfg)
    frames, have_5m = [], set()
    for sym, bars in src.iter_bars(symbols):
        have_5m.add(sym)
        d = (bars.assign(date=bars["ts"].dt.date).groupby("date")
                 .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                      close=("close", "last"), volume=("volume", "sum")))
        f = factors[factors["symbol"] == sym].set_index("date")["factor"]
        d["factor"] = f.reindex(d.index).ffill().bfill()
        frames.append(d.reset_index().assign(symbol=sym))
    # Former members without 5-minute history: Alpaca daily bars (same regular-session OHLC).
    for sym in symbols:
        daily_file = src.daily_path(sym)
        if sym not in have_5m and daily_file.exists():
            d = pd.read_parquet(daily_file)
            d["date"] = d.pop("ts").dt.date
            frames.append(d.assign(symbol=sym))
    df = pd.concat([_apply_fixes(f, f["symbol"].iloc[0]) for f in frames], ignore_index=True)
    df.to_parquet(path)
    return df


def load_panel(cfg: dict) -> Panel:
    df = build_daily(cfg)
    wide = {c: df.pivot(index="date", columns="symbol", values=c).sort_index() for c in
            ("open", "high", "low", "close", "volume", "factor")}
    f = wide["factor"]
    return Panel(*(wide[c] / f for c in ("open", "high", "low", "close")), wide["volume"] * f, f)


# ---------------------------------------------------------------- indicators

def sma(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.rolling(n, min_periods=n).mean()


def rsi(x: pd.DataFrame, n: int) -> pd.DataFrame:
    d = x.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn)


def atr(p: Panel, n: int) -> pd.DataFrame:
    prev = p.close.shift()
    tr = np.maximum(p.high, prev) - np.minimum(p.low, prev)
    return tr.rolling(n, min_periods=n).mean()


@dataclass
class Signals:
    """Decided at each day's close; acted on at the next day's open.

    entry      bool: buy candidates
    score      higher = preferred when several symbols qualify
    exit       bool: sell the held symbol
    stop_dist  optional: stop distance below the entry price (in adjusted price units)
    max_hold   sell after this many trading days regardless
    stop_price optional: absolute stop (adjusted price) set on the signal day, e.g. that day's low
    max_stop   skip the entry if the stop is more than this fraction below the entry price
    trim_r     sell trim_frac of the shares once the price reaches entry + trim_r x initial risk
    """
    entry: pd.DataFrame
    score: pd.DataFrame
    exit: pd.DataFrame
    stop_dist: pd.DataFrame | None
    max_hold: int
    stop_price: pd.DataFrame | None = None
    max_stop: float | None = None
    trim_r: float | None = None
    trim_frac: float = 0.5


# ---------------------------------------------------------------- simulation

def simulate(p: Panel, sig: Signals, cfg: dict, start: date | None = None, end: date | None = None,
             symbols: list[str] | None = None) -> Result:
    slip = cfg["execution"]["slippage_pct"]
    respect_quota = cfg["broker"]["respect_quota"]
    compound = cfg["account"]["compound"]
    costs = HsbcCosts.from_config(cfg)
    start_cap = equity = float(cfg["account"]["capital_usd"])

    days = [d for d in p.close.index if (not start or d >= start) and (not end or d <= end)]
    cols = symbols or list(p.close.columns)
    entry_ok = sig.entry[cols].fillna(False)
    score = sig.score[cols]
    exit_sig = sig.exit.fillna(False)

    trades, skips, months_paid = [], Counter(), set()
    monthly_fees = 0.0
    pos = None             # dict(sym, entry_day, entry_adj, shares, factor, stop, held, ...)
    pending_exit = False
    pending_entry = None   # symbol to buy at the next open

    def charge_month(d):
        nonlocal monthly_fees
        m = d.strftime("%Y-%m")
        if m not in months_paid:
            months_paid.add(m)
            monthly_fees += costs.monthly_fee_usd()
            return costs.monthly_fee_usd()
        return 0.0

    def close_position(d, px_adj, reason):
        nonlocal pos, equity
        m = d.strftime("%Y-%m")
        raw_px = px_adj * p.factor.at[d, pos["sym"]]
        raw_shares = pos["shares"] * pos["factor"] / p.factor.at[d, pos["sym"]]  # grows after a split
        fees = pos["fees"] + charge_month(d) + costs.fill(m, raw_shares * raw_px, int(raw_shares))
        gross = pos["shares"] * pos["factor"] * (px_adj - pos["entry_adj"]) + pos["realized"]
        pnl = gross - fees
        notional = pos["orig_shares"] * pos["entry_raw"]
        trades.append({
            "date": pos["entry_day"], "symbol": pos["sym"], "exit_date": d, "held_days": pos["held"],
            "entry": round(pos["entry_raw"], 2), "exit": round(raw_px, 2), "reason": reason,
            "stop": round(pos["stop"] * pos["factor"], 2), "trimmed": pos["trimmed"],
            "shares": pos["orig_shares"], "fees": round(fees, 2), "pnl": round(pnl, 2),
            "ret_pct": round(pnl / notional * 100, 3),
            "gross_pct": round(gross / (pos["orig_shares"] * pos["factor"] * pos["entry_adj"]) * 100, 3),
            "equity_after": round(equity + pnl, 2), "quota_left_hkd": round(costs.remaining_hkd(m)),
        })
        equity += pnl
        pos = None

    for d in days:
        m = d.strftime("%Y-%m")
        # 0) A held stock whose data ended (acquired / delisted): sell at its last close.
        if pos and not np.isfinite(p.open.at[d, pos["sym"]]):
            last = p.close[pos["sym"]].loc[:d].dropna()
            close_position(last.index[-1], last.iloc[-1] * (1 - slip), "数据中断")
            pending_exit = False
        # 1) Orders decided at yesterday's close fill at today's open.
        if pos and pending_exit:
            close_position(d, p.open.at[d, pos["sym"]] * (1 - slip), pending_exit)
        pending_exit = False
        if pending_entry and pos is None:
            sym = pending_entry
            o = p.open.at[d, sym]
            if np.isfinite(o):
                entry_adj = o * (1 + slip)
                entry_raw = entry_adj * p.factor.at[d, sym]
                shares = int((equity if compound else start_cap) // entry_raw)
                if shares < 1:
                    skips["股价高于本金"] += 1
                elif respect_quota and not costs.fits_quota(m, shares * entry_raw * 2 * 1.05):
                    skips["免佣额度不足"] += 1
                else:
                    if sig.stop_price is not None:
                        stop = sig.stop_price.at[prev_day, sym]
                    else:
                        sd = sig.stop_dist.at[prev_day, sym] if sig.stop_dist is not None else np.nan
                        stop = entry_adj - sd if np.isfinite(sd) else -np.inf
                    if sig.max_stop is not None and not (0 < (entry_adj - stop) / entry_adj <= sig.max_stop):
                        skips["开盘价离止损太远或已跌破"] += 1
                    else:
                        fees = costs.fill(m, shares * entry_raw, shares)
                        pos = {"sym": sym, "entry_day": d, "entry_adj": entry_adj, "entry_raw": entry_raw,
                               "shares": shares, "orig_shares": shares, "factor": p.factor.at[d, sym],
                               "fees": fees, "held": 0, "stop": stop, "risk": entry_adj - stop,
                               "realized": 0.0, "trimmed": False}
        pending_entry = None

        # 2) Intraday stop.
        if pos:
            pos["fees"] += charge_month(d)
            lo, o = p.low.at[d, pos["sym"]], p.open.at[d, pos["sym"]]
            if np.isfinite(lo) and lo <= pos["stop"]:
                close_position(d, min(o, pos["stop"]) * (1 - slip), "止损")
        # 2b) Partial profit at trim_r x risk (limit order; a gap above it fills at the open).
        if pos and sig.trim_r and not pos["trimmed"]:
            target = pos["entry_adj"] + sig.trim_r * pos["risk"]
            n = int(pos["shares"] * sig.trim_frac)
            if 1 <= n < pos["shares"] and p.high.at[d, pos["sym"]] >= target:
                px = max(p.open.at[d, pos["sym"]], target)
                pos["realized"] += n * pos["factor"] * (px - pos["entry_adj"])
                pos["fees"] += costs.fill(m, n * pos["factor"] * px, n)
                pos["shares"] -= n
                pos["trimmed"] = True

        # 3) Decisions at today's close.
        if pos:
            pos["held"] += 1
            if exit_sig.at[d, pos["sym"]]:
                pending_exit = "卖出信号"
            elif pos["held"] >= sig.max_hold:
                pending_exit = "持有到期"
        if pos is None or pending_exit:
            row = entry_ok.loc[d]
            if row.any():
                cand = score.loc[d][row.values].dropna()
                if pos is not None:
                    cand = cand.drop(pos["sym"], errors="ignore")
                if len(cand):
                    pending_entry = cand.idxmax()
        prev_day = d

    if pos:  # mark the open position to the last close so results include it
        close_position(days[-1], p.close.at[days[-1], pos["sym"]] * (1 - slip), "回测结束")
    return Result(pd.DataFrame(trades), skips, int(entry_ok.loc[days].values.sum()), start_cap, equity,
                  days[0] if days else None, days[-1] if days else None, monthly_fees)


def benchmark_return(p: Panel, sym: str, first: date, last: date) -> float:
    c = p.close[sym]
    return c.loc[last] / p.open[sym].loc[first] - 1
