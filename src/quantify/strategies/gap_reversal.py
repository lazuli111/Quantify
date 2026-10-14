"""Gap-down reversal, long only (mean reversion).

Signal: the stock opened at least min_gap below yesterday's close, then after 09:40 a bar closes
back above both today's open and VWAP. Stop: the day's low so far. Built-in target: yesterday's
close (gap fill), used when target_r is "signal".
"""

import pandas as pd

from .common import signal_frame

NAME = "低开反转"
GRID_FEATURES = {"min_gap": [0.02, 0.03, 0.05]}
GRID_SIM = {"min_rvol": [1.0, 1.5], "target_r": ["signal", None], "max_stop_pct": [0.015, 0.03]}


def signals(df: pd.DataFrame, p: dict, cfg: dict) -> pd.DataFrame:
    reclaim = (df["close"] > df["day_open"]) & (df["close"] > df["vwap"])
    cond = (
        (df["gap"] <= -p["min_gap"])
        & (df["tmin"] >= 9 * 60 + 40)
        & reclaim
        & (df["close"] < df["prev_close"])
    )
    return signal_frame(df, cond, stop=df["low_so_far"], target=df["prev_close"])
