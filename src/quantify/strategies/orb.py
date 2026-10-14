"""Opening Range Breakout, long only.

Signal: the first bar after the opening range that closes above the range high (previous bar
closed at or below it), above VWAP if required. Stop: the range low.
"""

import pandas as pd

from ..data import parse_hhmm
from .common import minutes, signal_frame

NAME = "开盘区间突破"
GRID_FEATURES = {"or_minutes": [15, 30]}
GRID_SIM = {"min_rvol": [1.0, 1.5, 2.0], "target_r": [1.5, 2.0, None]}


def signals(df: pd.DataFrame, p: dict, cfg: dict) -> pd.DataFrame:
    or_end = minutes(parse_hhmm(cfg["session"]["open"])) + p["or_minutes"]
    in_or = df["tmin"] < or_end
    rng = df[in_or].groupby("date").agg(or_high=("high", "max"), or_low=("low", "min"))
    df = df.join(rng, on="date")
    cond = (
        ~in_or
        & (df["close"] > df["or_high"])
        & (df["prev_bar_close"] <= df["or_high"])
    )
    if p["require_above_vwap"]:
        cond &= df["close"] > df["vwap"]
    return signal_frame(df, cond, stop=df["or_low"])
