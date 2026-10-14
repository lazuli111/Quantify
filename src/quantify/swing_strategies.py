"""Swing strategies (hold for days). Each takes the daily Panel and a parameter dict and returns
Signals. All are long only, decided at the close and traded at the next open.

GRID lists the parameters a sweep tries. "universe" is handled by the sweep itself:
"个股" = the Nasdaq-100 stocks, "QQQ" = the ETF alone (free of survivorship bias).
"""

import pandas as pd

from .swing import Panel, Signals, atr, rsi, sma

BENCH = "QQQ"
FOREVER = 10**6


def _stocks(p: Panel) -> list[str]:
    return [c for c in p.close.columns if c != BENCH]


def _rebalance_days(index, freq: str) -> pd.Series:
    """True on the last trading day of each week ("W") or month ("M")."""
    s = pd.Series(pd.to_datetime(index), index=index)
    key = s.dt.to_period(freq)
    return key != key.shift(-1)


class Rsi2:
    NAME = "RSI(2) 超跌反弹"
    DOC = "长期上升趋势（收盘在 200 日均线上）中，RSI(2) 跌到极低时买入，反弹后卖出"
    GRID = {"universe": ["个股", "QQQ"], "rsi_th": [5, 10], "exit": ["sma5", "rsi70"], "max_hold": [5, 10]}

    @staticmethod
    def signals(p: Panel, prm: dict) -> Signals:
        c = p.close
        r = rsi(c, 2)
        entry = (c > sma(c, 200)) & (r < prm["rsi_th"])
        exit_ = (c > sma(c, 5)) if prm["exit"] == "sma5" else (r > 70)
        return Signals(entry, -r, exit_, None, prm["max_hold"])


class Pullback3:
    NAME = "三连跌回调"
    DOC = "长期上升趋势中连续 3 天收跌时买入，收盘高于前一天最高价或到期卖出"
    GRID = {"universe": ["个股", "QQQ"], "down_days": [3, 4], "max_hold": [5, 10]}

    @staticmethod
    def signals(p: Panel, prm: dict) -> Signals:
        c = p.close
        down = (c < c.shift()).astype(int)
        streak = down.rolling(prm["down_days"]).sum() == prm["down_days"]
        entry = (c > sma(c, 200)) & streak
        exit_ = c > p.high.shift()
        deeper = -(c / c.shift(prm["down_days"]) - 1)  # deeper pullback preferred
        return Signals(entry, deeper, exit_, None, prm["max_hold"])


class Donchian:
    NAME = "新高突破（趋势跟踪）"
    DOC = "收盘创 N 日新高且 50 日均线在 200 日均线上方时买入；跌破 M 日低点或触发 ATR 止损时卖出"
    GRID = {"universe": ["个股"], "n_high": [20, 55], "m_low": [10, 20], "stop_atr": [2, 3]}

    @staticmethod
    def signals(p: Panel, prm: dict) -> Signals:
        c = p.close
        entry = (c >= c.rolling(prm["n_high"]).max()) & (sma(c, 50) > sma(c, 200))
        exit_ = c < c.rolling(prm["m_low"]).min().shift()
        return Signals(entry, c / c.shift(63) - 1, exit_, atr(p, 20) * prm["stop_atr"], FOREVER)


class MomentumRotation:
    NAME = "动量轮动"
    DOC = "QQQ 在 200 日均线上方时，定期持有过去 N 天涨幅最大的个股；跌出前 K 名或大盘转弱时换掉"
    GRID = {"universe": ["个股"], "lookback": [63, 126], "freq": ["W", "M"], "keep_top": [1, 5]}

    @staticmethod
    def signals(p: Panel, prm: dict) -> Signals:
        c = p.close
        stocks = _stocks(p)
        mom = c / c.shift(prm["lookback"]) - 1
        market_ok = c[BENCH] > sma(c[[BENCH]], 200)[BENCH]
        eligible = (c > sma(c, 100)).mul(market_ok, axis=0)
        rank = mom[stocks].where(eligible[stocks].astype(bool)).rank(axis=1, ascending=False)
        rank = rank.reindex(columns=c.columns)
        reb = _rebalance_days(c.index, prm["freq"])
        entry = (rank == 1).mul(reb, axis=0).astype(bool)
        exit_ = (rank.isna() | (rank > prm["keep_top"])).mul(reb, axis=0).astype(bool)
        return Signals(entry, mom, exit_, None, FOREVER)


class TrendQQQ:
    NAME = "QQQ 均线择时（参考）"
    DOC = "QQQ 收盘在 N 日均线上方就持有，跌破就空仓。交易极少，作为低维护的参考"
    GRID = {"universe": ["QQQ"], "ma": [100, 200]}

    @staticmethod
    def signals(p: Panel, prm: dict) -> Signals:
        c = p.close
        above = c > sma(c, prm["ma"])
        return Signals(above, c * 0, ~above, None, FOREVER)


def ema(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


class LukPullback:
    """Approximation of Martin Luk's pullback buying, from public (second-hand) descriptions.

    Known before day t (as of t-1's close): the stock is up >= 30% over 1, 3 or 6 months, and
    EMA9 > EMA21 > EMA50 with EMA21 rising. Day t: the low dips below the support EMA (yesterday's
    value) but the close is back above it, in the upper half of the day's range ("intraday flush
    that reclaims support"). Buy at t+1's open, stop at day t's low, skip if that stop is more than
    max_stop away. Exit on a close below the trailing EMA; optionally sell half at 3R first.
    """
    NAME = "强势股回调买入（Martin Luk 风格）"
    DOC = "近 1/3/6 个月涨 ≥30% 且均线多头的强势股，日内跌破 9/21 日 EMA 后收回时买入，止损设在当天低点，收盘跌破追踪均线卖出"
    GRID = {"universe": ["个股"], "support": ["ema9", "ema21"], "trail": ["ema9", "ema21"],
            "max_stop": [0.04, 0.07], "trim_r": [None, 3]}

    @staticmethod
    def signals(p: Panel, prm: dict) -> Signals:
        c, h, lo = p.close, p.high, p.low
        e = {n: ema(c, n) for n in (9, 21, 50)}
        strong = ((c / c.shift(21) - 1 >= 0.30) | (c / c.shift(63) - 1 >= 0.30)
                  | (c / c.shift(126) - 1 >= 0.30))
        trend = (e[9] > e[21]) & (e[21] > e[50]) & (e[21] > e[21].shift(5))
        support = e[int(prm["support"][3:])].shift()
        entry = (strong.shift().fillna(False).astype(bool) & trend.shift().fillna(False).astype(bool)
                 & (lo < support) & (c > support) & (c >= (h + lo) / 2))
        exit_ = c < e[int(prm["trail"][3:])]
        return Signals(entry, c / c.shift(63) - 1, exit_, None, FOREVER, stop_price=lo,
                       max_stop=prm["max_stop"], trim_r=prm["trim_r"])


SWING_STRATEGIES = {
    "rsi2": Rsi2,
    "pullback3": Pullback3,
    "donchian": Donchian,
    "momentum": MomentumRotation,
    "qqq_trend": TrendQQQ,
    "luk": LukPullback,
}
