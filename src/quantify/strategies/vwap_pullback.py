"""VWAP pullback in an intraday uptrend, long only.

Signal: the stock has traded at least min_move above today's open, then a bar dips to within
touch_tol of VWAP, holds above it (close > VWAP) and closes up (close > open). Not before 10:00.
Stop: that bar's low.
"""

import pandas as pd

from .common import signal_frame

NAME = "VWAP 回踩"
GRID_FEATURES = {"min_move": [0.01, 0.02], "touch_tol": [0.001, 0.003]}
GRID_SIM = {"min_rvol": [1.0, 1.5, 2.0], "target_r": [2.0, None]}


def signals(df: pd.DataFrame, p: dict, cfg: dict) -> pd.DataFrame:
    cond = (
        (df["tmin"] >= 10 * 60)
        & (df["high_so_far"] >= df["day_open"] * (1 + p["min_move"]))
        & (df["low"] <= df["vwap"] * (1 + p["touch_tol"]))
        & (df["close"] > df["vwap"])
        & (df["close"] > df["open"])
    )
    return signal_frame(df, cond, stop=df["low"])
