"""Full-day intraday momentum on QQQ ("noise area" breakout), long only.

Adapted from Zarattini, Aziz & Barbon, "Beat the Market: An Effective Intraday Momentum Strategy
for S&P500 ETF (SPY)" (2024). For each time of day, sigma = the average absolute move from the
open over the previous 14 sessions. Upper band = max(open, prev close) * (1 + sigma), lower band =
min(open, prev close) * (1 - sigma). Decisions only at check times (every 30 or 60 minutes from
10:00): buy when the price is above the upper band; sell when it falls below the stop
(the lower band, or max(upper band, VWAP)); always flat at the close. The paper also goes short
and uses leverage; this version does neither.

Position size is the whole account in fractional terms (in practice QQQM, a lower-priced ETF on
the same index, gets close to that with $1,300).
"""

from collections import Counter
from datetime import date

import numpy as np
import pandas as pd

from .backtest import Result
from .costs import HsbcCosts
from .data import get_source

SYMBOL = "QQQ"


def features(cfg: dict, lookback: int = 14) -> pd.DataFrame:
    bars = next(get_source(cfg).iter_bars([SYMBOL]))[1].sort_values("ts").reset_index(drop=True)
    df = bars.assign(date=bars["ts"].dt.date, tmin=bars["ts"].dt.hour * 60 + bars["ts"].dt.minute)
    day = df.groupby("date", sort=False)
    df["day_open"] = day["open"].transform("first")
    prev_close = day["close"].last().shift(1)
    df["prev_close"] = df["date"].map(prev_close)
    # The price at the end of each bar is its close; tag bars by the time their close happens.
    df["t_end"] = df["tmin"] + 5
    move = (df["close"] / df["day_open"] - 1).abs()
    df["sigma"] = (move.groupby(df["t_end"]).transform(
        lambda x: x.shift(1).rolling(lookback, min_periods=lookback).mean()))
    df["upper"] = np.maximum(df["day_open"], df["prev_close"]) * (1 + df["sigma"])
    df["lower"] = np.minimum(df["day_open"], df["prev_close"]) * (1 - df["sigma"])
    tp_vol = (df["high"] + df["low"] + df["close"]) / 3 * df["volume"]
    df["vwap"] = tp_vol.groupby(df["date"]).cumsum() / day["volume"].cumsum()
    return df


def simulate(df: pd.DataFrame, cfg: dict, prm: dict, start: date | None = None,
             end: date | None = None) -> Result:
    slip = cfg["execution"]["slippage_pct"]
    costs = HsbcCosts.from_config(cfg)
    start_cap = equity = float(cfg["account"]["capital_usd"])
    compound = cfg["account"]["compound"]
    every = prm["check_every"]
    trades, skips, months_paid, monthly_fees = [], Counter(), set(), 0.0

    sel = df[df["sigma"].notna()]
    if start:
        sel = sel[sel["date"] >= start]
    if end:
        sel = sel[sel["date"] <= end]

    for d, g in sel.groupby("date", sort=True):
        m = d.strftime("%Y-%m")
        o, c, t_end = g["open"].to_numpy(), g["close"].to_numpy(), g["t_end"].to_numpy()
        up, lo, vw = g["upper"].to_numpy(), g["lower"].to_numpy(), g["vwap"].to_numpy()
        pos = None  # (entry_price, shares, entry_index)
        for i in range(len(g) - 1):
            is_check = t_end[i] >= 600 and (t_end[i] - 600) % every == 0 and t_end[i] < 960
            if not is_check:
                continue
            if pos is None and c[i] > up[i]:
                entry = o[i + 1] * (1 + slip)
                size = equity if compound else start_cap
                shares = size / entry
                if cfg["broker"]["respect_quota"] and not costs.fits_quota(m, size * 2 * 1.02):
                    skips["免佣额度不足"] += 1
                    continue
                pos = (entry, shares, i + 1)
            elif pos is not None:
                stop = lo[i] if prm["stop"] == "opp_band" else max(up[i], vw[i])
                if c[i] < stop:
                    _close(trades, costs, pos, o[i + 1] * (1 - slip), g, i + 1, m, "止损", equity)
                    equity += trades[-1]["pnl"]
                    pos = None
        if pos is not None:
            _close(trades, costs, pos, c[-1] * (1 - slip), g, len(g) - 1, m, "收盘平仓", equity)
            equity += trades[-1]["pnl"]
        if trades and trades[-1]["date"] == d and m not in months_paid:
            months_paid.add(m)
            fee = costs.monthly_fee_usd()
            monthly_fees += fee
            t = trades[-1]
            t["fees"] += fee
            t["pnl"] -= fee
            t["ret_pct"] = round(t["pnl"] / (t["shares"] * t["entry"]) * 100, 3)
            t["equity_after"] -= fee
            equity -= fee

    days = sorted(sel["date"].unique())
    return Result(pd.DataFrame(trades), skips, len(trades), start_cap, equity,
                  days[0] if days else None, days[-1] if days else None, monthly_fees)


def _close(trades, costs, pos, exit_px, g, j, month, reason, equity):
    entry, shares, i0 = pos
    fees = costs.fill(month, shares * entry, int(shares)) + costs.fill(month, shares * exit_px, int(shares))
    pnl = shares * (exit_px - entry) - fees
    trades.append({
        "date": g["date"].iloc[0], "symbol": SYMBOL,
        "entry_time": g["ts"].iloc[i0].strftime("%H:%M"), "exit_time": g["ts"].iloc[j].strftime("%H:%M"),
        "entry": round(entry, 2), "exit": round(exit_px, 2),
        "reason": reason, "shares": round(shares, 3), "fees": round(fees, 2), "pnl": round(pnl, 2),
        "ret_pct": round(pnl / (shares * entry) * 100, 3),
        "gross_pct": round((exit_px / entry - 1) * 100, 3),
        "equity_after": round(equity + pnl, 2), "quota_left_hkd": round(costs.remaining_hkd(month)),
    })
