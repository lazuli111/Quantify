"""Afternoon momentum, long only (needs the full-day window).

Signal: at signal_at, the stock is up at least min_ret from yesterday's close, above VWAP and
within 0.3% of the day's high. Hold to the close. Stop: VWAP.
"""

import pandas as pd

from ..data import parse_hhmm
from .common import minutes, signal_frame

NAME = "尾盘动量"
GRID_FEATURES = {"signal_at": ["13:55", "14:55"], "min_ret": [0.01, 0.02]}
GRID_SIM = {"min_rvol": [1.0, 1.5], "target_r": [None]}
WINDOWS = ["全天"]


def signals(df: pd.DataFrame, p: dict, cfg: dict) -> pd.DataFrame:
    cond = (
        (df["tmin"] == minutes(parse_hhmm(p["signal_at"])))
        & (df["close"] >= df["prev_close"] * (1 + p["min_ret"]))
        & (df["close"] > df["vwap"])
        & (df["close"] >= df["high_so_far"] * 0.997)
    )
    return signal_frame(df, cond, stop=df["vwap"])
