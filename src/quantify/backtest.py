"""Bar-by-bar backtest of one cash account trading the strategy's signals with HSBC costs.

prepare() scans every symbol once (the expensive part); simulate() replays the account over any
date range and parameter set that doesn't change the features, so sweeps stay fast.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .costs import HsbcCosts
from .data import get_source, parse_hhmm
from .strategies import STRATEGIES
from .strategies.common import base_features, minutes
from .universe import get_universe

BAR = pd.Timedelta(minutes=5)


@dataclass
class Prepared:
    signals: pd.DataFrame          # symbol, date, ts, rvol, stop, stop_dist, target
    bars: dict                     # (symbol, date) -> numpy arrays of that day's bars from the first signal to the close
    trading_days: list[date]       # days with a valid relative-volume baseline
    benchmark: pd.DataFrame        # daily open/close of the benchmark


@dataclass
class Result:
    trades: pd.DataFrame
    skips: Counter
    n_candidates: int
    start_capital: float
    end_equity: float
    first_date: date | None
    last_date: date | None
    monthly_fees: float


def prepare(cfg: dict, symbols: list[str] | None = None, verbose: bool = True) -> Prepared:
    name = cfg["strategy"]["name"]
    strategy, params = STRATEGIES[name], cfg["strategy"][name]
    bench_sym = cfg["data"]["benchmark"]
    symbols = symbols or get_universe(cfg)
    src = get_source(cfg)
    all_sigs, day_bars, days = [], {}, set()
    for i, (sym, raw) in enumerate(src.iter_bars(symbols), 1):
        df = base_features(raw, cfg)
        days.update(df.loc[df["avg_cum_dv"].notna(), "date"].unique())
        sig = strategy.signals(df, params, cfg)
        if sig.empty:
            continue
        all_sigs.append(sig.assign(symbol=sym, tmin=sig["ts"].dt.hour * 60 + sig["ts"].dt.minute))
        first_sig = df["date"].map(sig.groupby("date")["ts"].min())
        keep = df[df["ts"] >= first_sig]
        utc_ts = keep["ts"].dt.tz_convert("UTC").dt.tz_localize(None).astype("datetime64[ns]")
        for d, g in keep.assign(ts=utc_ts).groupby("date", sort=False):
            day_bars[(sym, d)] = {c: g[c].to_numpy() for c in ("ts", "tmin", "open", "high", "low", "close")}
        if verbose and i % 20 == 0:
            print(f"  已扫描 {i} 只股票…", flush=True)
    bench = next(src.iter_bars([bench_sym]), (None, None))[1]
    bench_daily = (bench.assign(date=bench["ts"].dt.date).groupby("date")
                        .agg(open=("open", "first"), close=("close", "last"))) if bench is not None else None
    sigs = pd.concat(all_sigs, ignore_index=True) if all_sigs else pd.DataFrame(
        columns=["symbol", "date", "ts", "tmin", "rvol", "stop", "stop_dist", "target"])
    return Prepared(sigs, day_bars, sorted(days), bench_daily)


def _walk(b: dict, start: int, exit_i: int, stop: float, target: float | None, slip: float):
    """Follow bars start..exit_i-1 (exit_i = first bar at/after exit_by); return (exit_price, exit_time, reason).

    If stop and target are both touched inside one bar we assume the stop came first (conservative).
    """
    for i in range(start, exit_i):
        if b["low"][i] <= stop:
            return min(stop, b["open"][i]) * (1 - slip), b["ts"][i] + BAR, "止损"
        if target is not None and b["high"][i] >= target:
            return target, b["ts"][i] + BAR, "止盈"
    return b["close"][exit_i - 1] * (1 - slip), b["ts"][exit_i - 1] + BAR, "到时平仓"


def simulate(prep: Prepared, cfg: dict, start: date | None = None, end: date | None = None) -> Result:
    s, br = cfg["strategy"], cfg["broker"]
    slip = cfg["execution"]["slippage_pct"]
    target_r = s["target_r"]
    last_entry = minutes(parse_hhmm(cfg["session"]["last_entry"]))
    exit_by = minutes(parse_hhmm(cfg["session"]["exit_by"]))

    c = prep.signals
    c = c[(c["rvol"] >= s["min_rvol"]) & (c["tmin"] < last_entry)]
    if start:
        c = c[c["date"] >= start]
    if end:
        c = c[c["date"] <= end]
    # First qualifying signal per symbol-day; earliest first, ties go to the strongest rvol.
    c = c.groupby(["symbol", "date"], sort=False).head(1)
    c = c.sort_values(["date", "ts", "rvol"], ascending=[True, True, False])

    costs = HsbcCosts.from_config(cfg)
    start_cap = equity = float(cfg["account"]["capital_usd"])
    trades, skips, months_paid = [], Counter(), set()
    monthly_fees = 0.0

    for d, day in c.groupby("date", sort=True):
        n_today, busy_until = 0, None
        month = d.strftime("%Y-%m")
        for row in day.itertuples():
            if n_today >= s["max_trades_per_day"]:
                break
            if busy_until is not None and row.ts < busy_until:
                skips["已有持仓"] += 1
                continue
            b = prep.bars[(row.symbol, d)]
            sig_utc = row.ts.tz_convert("UTC").tz_localize(None).to_datetime64().astype("datetime64[ns]")
            i = int(np.searchsorted(b["ts"], sig_utc, side="right"))
            exit_i = int(np.searchsorted(b["tmin"], exit_by))
            if i >= exit_i:
                continue
            entry = b["open"][i] * (1 + slip)
            stop = entry - row.stop_dist if pd.notna(row.stop_dist) else row.stop
            risk = entry - stop
            if risk <= 0 or risk / entry > s["max_stop_pct"]:
                skips["止损距离太大"] += 1
                continue
            if target_r == "signal":
                target = row.target if pd.notna(row.target) else None
            else:
                target = entry + target_r * risk if target_r else None
            if target is not None and target <= entry:
                skips["已到目标价"] += 1
                continue
            shares = int((equity if cfg['account']['compound'] else start_cap) // entry)
            if shares < 1:
                skips["股价高于本金"] += 1
                continue
            if br["respect_quota"] and not costs.fits_quota(month, shares * entry * 2 * 1.02):
                skips["免佣额度不足"] += 1
                continue

            exit_px, exit_ts, reason = _walk(b, i, exit_i, stop, target, slip)
            fees = costs.fill(month, shares * entry, shares) + costs.fill(month, shares * exit_px, shares)
            if month not in months_paid:
                months_paid.add(month)
                fees += costs.monthly_fee_usd()
                monthly_fees += costs.monthly_fee_usd()
            pnl = shares * (exit_px - entry) - fees
            entry_ts = pd.Timestamp(b["ts"][i]).tz_localize("UTC").tz_convert(row.ts.tz)
            exit_ts = pd.Timestamp(exit_ts).tz_localize("UTC").tz_convert(row.ts.tz)
            trades.append({
                "date": d, "symbol": row.symbol, "signal_time": row.ts.strftime("%H:%M"),
                "entry_time": entry_ts.strftime("%H:%M"), "entry": round(entry, 2),
                "stop": round(stop, 2), "target": round(target, 2) if target else None,
                "exit_time": exit_ts.strftime("%H:%M"), "exit": round(exit_px, 2), "reason": reason,
                "shares": shares, "rvol": round(row.rvol, 2), "fees": round(fees, 2),
                "pnl": round(pnl, 2), "ret_pct": round(pnl / (shares * entry) * 100, 3),
                "gross_pct": round((exit_px / entry - 1) * 100, 3),
                "equity_after": round(equity + pnl, 2),
                "quota_left_hkd": round(costs.remaining_hkd(month)),
            })
            equity += pnl
            n_today += 1
            busy_until = exit_ts

    days = [x for x in prep.trading_days if (not start or x >= start) and (not end or x <= end)]
    return Result(pd.DataFrame(trades), skips, len(c), start_cap, equity,
                  days[0] if days else None, days[-1] if days else None, monthly_fees)


def benchmark_return(prep: Prepared, first: date, last: date) -> float | None:
    q = prep.benchmark
    if q is None:
        return None
    q = q[(q.index >= first) & (q.index <= last)]
    return q["close"].iloc[-1] / q["open"].iloc[0] - 1 if len(q) else None
