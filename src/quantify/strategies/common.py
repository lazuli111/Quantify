"""Features shared by all strategies, computed on one symbol's regular-session 5-minute bars.

Every column only uses information available at the close of its own bar (or from earlier days).
"""

import pandas as pd


def minutes(t) -> int:
    return t.hour * 60 + t.minute


def base_features(bars: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    df = bars.sort_values("ts").reset_index(drop=True)
    df["date"] = df["ts"].dt.date
    df["tmin"] = df["ts"].dt.hour * 60 + df["ts"].dt.minute
    day = df.groupby("date", sort=False)

    # VWAP, and relative volume in dollars so stock splits (raw prices) don't fake a volume spike:
    # today's cumulative dollar volume vs. the average at the same time over the previous N sessions.
    dollar_vol = (df["high"] + df["low"] + df["close"]) / 3 * df["volume"]
    df["cum_dv"] = dollar_vol.groupby(df["date"]).cumsum()
    df["vwap"] = df["cum_dv"] / day["volume"].cumsum()
    n = cfg["strategy"]["rvol_lookback_days"]
    df["avg_cum_dv"] = (df.groupby("tmin", sort=False)["cum_dv"]
                          .transform(lambda x: x.shift(1).rolling(n, min_periods=n).mean()))
    df["rvol"] = df["cum_dv"] / df["avg_cum_dv"]

    df["prev_bar_close"] = day["close"].shift(1)
    df["day_open"] = day["open"].transform("first")
    df["low_so_far"] = day["low"].cummin()
    df["high_so_far"] = day["high"].cummax()

    # Daily context from previous sessions: prior close, overnight gap, 14-day ATR.
    daily = day.agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    prev_close = daily["close"].shift(1)
    true_range = pd.concat([daily["high"], prev_close], axis=1).max(axis=1) \
        - pd.concat([daily["low"], prev_close], axis=1).min(axis=1)
    daily_ctx = pd.DataFrame({"prev_close": prev_close,
                              "atr": true_range.rolling(14).mean().shift(1)})
    df = df.join(daily_ctx, on="date")
    df["gap"] = df["day_open"] / df["prev_close"] - 1
    return df


def signal_frame(df: pd.DataFrame, cond: pd.Series, stop=None, stop_dist=None, target=None) -> pd.DataFrame:
    """Standard signal table: one row per signal bar.

    stop      absolute stop price, or
    stop_dist distance below the (not yet known) entry price
    target    absolute target price (used when target_r is "signal"), optional
    """
    out = df.loc[cond, ["date", "ts", "rvol"]].copy()
    nan = float("nan")
    out["stop"] = stop[cond] if stop is not None else nan
    out["stop_dist"] = stop_dist[cond] if stop_dist is not None else nan
    out["target"] = target[cond] if target is not None else nan
    return out[out["rvol"].notna()]
