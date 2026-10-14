"""Stocks-in-play opening momentum, long only.

Signal: the first 5-minute bar of the day closes up (close > open). The backtest ranks all such
stocks by opening relative volume and takes the most unusual one; entry is the 09:35 open.
Stop: stop_atr x the 14-day ATR below entry, or the first bar's low when stop_atr is null.
"""

import pandas as pd

from ..data import parse_hhmm
from .common import minutes, signal_frame

NAME = "热门股开盘跟进"
GRID_FEATURES = {"stop_atr": [0.1, 0.25, None]}
GRID_SIM = {"min_rvol": [1.0, 1.5, 2.0], "target_r": [2.0, None]}


def signals(df: pd.DataFrame, p: dict, cfg: dict) -> pd.DataFrame:
    first = df["tmin"] == minutes(parse_hhmm(cfg["session"]["open"]))
    cond = first & (df["close"] > df["open"]) & df["atr"].notna()
    if p["stop_atr"]:
        return signal_frame(df, cond, stop_dist=df["atr"] * p["stop_atr"])
    return signal_frame(df, cond, stop=df["low"])
