"""Whole-market universe (all exchange-listed US stocks since 2016, delisted ones included) for
strategies that hunt fast movers outside the Nasdaq-100.

The universe is decided every day from data available at the time (price, dollar volume, average
daily range), so no hindsight list of "winners" enters the test.
"""

import pandas as pd

from .config import data_dir
from .swing import Panel, Signals, load_panel
from .swing_strategies import FOREVER, ema

# Daily universe rules (as of the previous close).
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 20e6   # 20-day average
MIN_ADR = 0.04             # 20-day average of high/low - 1


def _load_rows(cfg: dict) -> pd.DataFrame:
    files = sorted((data_dir(cfg) / "wide_1d").glob("*.parquet"))
    df = pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)
    return df.drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"])


def load_wide_panel(cfg: dict) -> Panel:
    """Panel of every stock that met the universe rules on at least one day, plus QQQ."""
    df = _load_rows(cfg)
    dv = (df["close"] * df["volume"]).groupby(df["symbol"]).transform(lambda x: x.rolling(20).mean())
    adr = (df["high"] / df["low"] - 1).groupby(df["symbol"]).transform(lambda x: x.rolling(20).mean())
    ok = (df["close"] >= MIN_PRICE) & (dv >= MIN_DOLLAR_VOLUME) & (adr >= MIN_ADR)
    keep = set(df.loc[ok, "symbol"])
    df = df[df["symbol"].isin(keep)]
    wide = {c: df.pivot(index="date", columns="symbol", values=c).astype("float32")
            for c in ("open", "high", "low", "close", "volume", "factor")}
    f = wide["factor"].ffill()
    p = Panel(*(wide[c] / f for c in ("open", "high", "low", "close")), wide["volume"] * f, f)
    q = load_panel(cfg).subset(["QQQ"])  # benchmark, from the Nasdaq-100 data
    return Panel(*(pd.concat([getattr(p, n), getattr(q, n).reindex(p.close.index)], axis=1)
                   for n in ("open", "high", "low", "close", "volume", "factor")))


class LukWide:
    """Luk-style pullback on the whole market: the same entry/stop/exit as LukPullback, but the
    candidates are each day's liquid, fast-moving leaders (top rs_top by 3-month return)."""
    NAME = "强势股回调买入（全市场，Martin Luk 风格）"
    DOC = ("每天从全美股中挑股价≥5、日均成交额≥2000万、日均波幅≥4%、3 个月涨幅排名前 rs_top 的强势股；"
           "日内跌破 9/21 日 EMA 后收回时买入，止损设在当天低点，收盘跌破追踪均线卖出")
    GRID = {"support": ["ema9", "ema21"], "trail": ["ema9", "ema21"], "max_stop": [0.04, 0.07],
            "rs_top": [0.02, 0.10]}

    @staticmethod
    def signals(p: Panel, prm: dict) -> Signals:
        c, h, lo = p.close, p.high, p.low
        raw = c * p.factor
        dv = (raw * (p.volume / p.factor)).rolling(20, min_periods=20).mean()
        adr = (h / lo - 1).rolling(20, min_periods=20).mean()
        eligible = (raw >= MIN_PRICE) & (dv >= MIN_DOLLAR_VOLUME) & (adr >= MIN_ADR)
        eligible["QQQ"] = False
        r63 = c / c.shift(63) - 1
        rank = r63.where(eligible).rank(axis=1, pct=True)
        leader = eligible & (rank >= 1 - prm["rs_top"])

        e = {n: ema(c, n) for n in (9, 21, 50)}
        strong = (c / c.shift(21) - 1 >= 0.30) | (r63 >= 0.30) | (c / c.shift(126) - 1 >= 0.30)
        trend = (e[9] > e[21]) & (e[21] > e[50]) & (e[21] > e[21].shift(5))
        setup = (leader & strong & trend).shift().fillna(False).astype(bool)
        support = e[int(prm["support"][3:])].shift()
        entry = setup & (lo < support) & (c > support) & (c >= (h + lo) / 2)
        exit_ = c < e[int(prm["trail"][3:])]
        return Signals(entry, r63, exit_, None, FOREVER, stop_price=lo, max_stop=prm["max_stop"])
