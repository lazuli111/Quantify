"""Intraday bars, regular session only, raw (unadjusted) prices so share counts match what the
account could actually buy at the time.

Sources:
- alpaca: full-market SIP history back to 2016 (free plan; needs keys in .env). One parquet per symbol.
- yahoo:  last 60 days only, no account needed. Merged into one cached parquet.
"""

import os
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from typing import Iterator

import pandas as pd
import requests
import yfinance as yf

from .config import ROOT, data_dir

ET = "America/New_York"
SESSION_OPEN, SESSION_CLOSE = time(9, 30), time(16, 0)
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def parse_hhmm(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()


def _regular_session(df: pd.DataFrame) -> pd.DataFrame:
    t = df["ts"].dt.time
    return df[(t >= SESSION_OPEN) & (t < SESSION_CLOSE)]


def _cut_at_long_gap(df: pd.DataFrame, max_gap_days: int = 30) -> pd.DataFrame:
    """Drop everything after the first gap of more than max_gap_days without trading.

    After a delisting the old ticker is sometimes reused by another company (e.g. BBBY), and
    Alpaca serves both under one symbol; the gap marks where the original company ended.
    """
    days = pd.Series(df["ts"].dt.normalize().unique())
    gaps = days.diff() > pd.Timedelta(days=max_gap_days)
    if not gaps.any():
        return df
    cutoff = days[gaps.idxmax()]
    return df[df["ts"] < cutoff].reset_index(drop=True)


# ---------------------------------------------------------------- Alpaca

ALPACA_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
ALPACA_HISTORY_START = "2016-01-01"


def _load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class _RateLimiter:
    """Free plan allows 200 requests/min; stay a little under it."""

    def __init__(self, per_minute: int = 180):
        self.interval = 60 / per_minute
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = _time.monotonic()
            delay = max(self.next_at - now, 0)
            self.next_at = max(now, self.next_at) + self.interval
        _time.sleep(delay)


class AlpacaSource:
    def __init__(self, cfg: dict):
        _load_env()
        key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("在 .env 中找不到 ALPACA_API_KEY / ALPACA_SECRET_KEY")
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.dir = data_dir(cfg) / "alpaca_5m"
        self.dir.mkdir(exist_ok=True)
        self.limiter = _RateLimiter()

    def path(self, symbol: str):
        return self.dir / f"{symbol}.parquet"

    def _get(self, session: requests.Session, url: str, params: dict) -> dict:
        # 403/429/5xx and timeouts have all been seen as temporary throttling: back off and retry.
        last_error = ""
        for attempt in range(7):
            self.limiter.wait()
            try:
                r = session.get(url, headers=self.headers, params=params, timeout=60)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_error = type(e).__name__
            else:
                if r.status_code not in (403, 429) and r.status_code < 500:
                    r.raise_for_status()
                    return r.json()
                last_error = f"HTTP {r.status_code}"
            _time.sleep(5 * 2 ** attempt)
        raise RuntimeError(f"多次重试仍失败（{last_error}）")

    def update_symbol(self, symbol: str) -> int:
        path = self.path(symbol)
        old = pd.read_parquet(path) if path.exists() else None
        start = (old["ts"].max() + pd.Timedelta(minutes=5)).tz_convert("UTC").isoformat() \
            if old is not None and len(old) else f"{ALPACA_HISTORY_START}T00:00:00Z"
        # Only finished sessions (which also keeps us outside the free plan's 15-minute delay).
        now = pd.Timestamp.now(tz=ET)
        end = now.normalize() + (pd.Timedelta(hours=16) if now.time() >= time(16, 20) else pd.Timedelta(0))
        end = end.tz_convert("UTC").isoformat()
        params = {"timeframe": "5Min", "start": start, "end": end, "limit": 10000,
                  "adjustment": "raw", "feed": "sip"}
        rows = []
        with requests.Session() as s:
            while True:
                page = self._get(s, ALPACA_URL.format(symbol=symbol), params)
                rows += page.get("bars") or []
                token = page.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
        if rows:
            new = pd.DataFrame(rows).rename(columns={"t": "ts", "o": "open", "h": "high",
                                                     "l": "low", "c": "close", "v": "volume"})
            new["ts"] = pd.to_datetime(new["ts"], utc=True).dt.tz_convert(ET)
            new = _regular_session(new[COLUMNS])
            df = new if old is None else pd.concat([old, new])
            df = df.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
            df.to_parquet(path)
        return len(rows)

    def update_adj_factors(self, symbols: list[str]) -> None:
        """Cumulative adjustment factor per day (raw price / split-and-dividend-adjusted price).

        Daily OHLC themselves come from our regular-session 5-minute bars; this only supplies the
        factor that makes them continuous across splits and dividends (e.g. KDP's 2018 special
        dividend), so multi-day returns are total returns.
        """
        out = self.dir.parent / "adj_factors.parquet"
        frames = []
        end = pd.Timestamp.now(tz=ET).normalize().tz_convert("UTC").isoformat()
        with requests.Session() as s:
            for sym in symbols:
                closes = {}
                for adj in ("raw", "all"):
                    page = self._get(s, ALPACA_URL.format(symbol=sym), {
                        "timeframe": "1Day", "start": f"{ALPACA_HISTORY_START}T00:00:00Z", "end": end,
                        "limit": 10000, "adjustment": adj, "feed": "sip"})
                    bars = page.get("bars") or []
                    closes[adj] = pd.Series({b["t"][:10]: b["c"] for b in bars})
                f = closes["raw"] / closes["all"]
                frames.append(pd.DataFrame({"symbol": sym, "date": pd.to_datetime(f.index).date, "factor": f.values}))
        pd.concat(frames, ignore_index=True).to_parquet(out)
        print(f"复权系数已保存（{len(frames)} 只）：{out}")

    def daily_path(self, symbol: str):
        return self.dir.parent / "alpaca_1d" / f"{symbol}.parquet"

    def update_daily(self, symbols: list[str]) -> None:
        """Regular-session daily bars (raw prices) plus the adjustment factor, for symbols where we
        only need daily data (former index members in the swing tests)."""
        self.daily_path("X").parent.mkdir(exist_ok=True)
        end = pd.Timestamp.now(tz=ET).normalize().tz_convert("UTC").isoformat()
        with requests.Session() as s:
            for i, sym in enumerate(symbols, 1):
                frames = {}
                for adj in ("raw", "all"):
                    page = self._get(s, ALPACA_URL.format(symbol=sym), {
                        "timeframe": "1Day", "start": f"{ALPACA_HISTORY_START}T00:00:00Z", "end": end,
                        "limit": 10000, "adjustment": adj, "feed": "sip"})
                    frames[adj] = pd.DataFrame(page.get("bars") or [])
                if frames["raw"].empty:
                    print(f"[{i}/{len(symbols)}] {sym}: 没有数据", flush=True)
                    continue
                raw = frames["raw"].merge(frames["all"][["t", "c"]], on="t", suffixes=("", "_adj"))
                df = pd.DataFrame({
                    "ts": pd.to_datetime(raw["t"], utc=True).dt.tz_convert(ET),
                    "open": raw["o"], "high": raw["h"], "low": raw["l"], "close": raw["c"],
                    "volume": raw["v"], "factor": raw["c"] / raw["c_adj"]})
                df = _cut_at_long_gap(df)
                df.to_parquet(self.daily_path(sym))
                print(f"[{i}/{len(symbols)}] {sym}: {len(df)} 天，{df['ts'].iloc[0]:%Y-%m-%d} → {df['ts'].iloc[-1]:%Y-%m-%d}", flush=True)

    def update_daily_bulk(self, symbols: list[str], out_dir, chunk: int = 100, prefix: str = "chunk") -> None:
        """Daily bars (raw + adjustment factor) for thousands of symbols, 100 per request.

        One parquet per chunk in out_dir; existing chunks are skipped, so it can be resumed.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        url = "https://data.alpaca.markets/v2/stocks/bars"
        end = pd.Timestamp.now(tz=ET).normalize().tz_convert("UTC").isoformat()
        groups = [symbols[i:i + chunk] for i in range(0, len(symbols), chunk)]
        with requests.Session() as s:
            for k, group in enumerate(groups, 1):
                path = out_dir / f"{prefix}_{k:04d}.parquet"
                if path.exists():
                    continue
                frames = {}
                for adj in ("raw", "all"):
                    params = {"symbols": ",".join(group), "timeframe": "1Day", "limit": 10000,
                              "start": f"{ALPACA_HISTORY_START}T00:00:00Z", "end": end,
                              "adjustment": adj, "feed": "sip"}
                    rows = []
                    while True:
                        page = self._get(s, url, params)
                        for sym, bars in (page.get("bars") or {}).items():
                            rows += [(sym, b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bars]
                        if not page.get("next_page_token"):
                            break
                        params["page_token"] = page["next_page_token"]
                    frames[adj] = pd.DataFrame(rows, columns=["symbol", "t", "open", "high", "low", "close", "volume"])
                raw = frames["raw"].merge(frames["all"][["symbol", "t", "close"]], on=["symbol", "t"],
                                          suffixes=("", "_adj"))
                raw["ts"] = pd.to_datetime(raw.pop("t"), utc=True).dt.tz_convert(ET)
                raw["factor"] = raw["close"] / raw.pop("close_adj")
                parts = [_cut_at_long_gap(g.sort_values("ts")) for _, g in raw.groupby("symbol")]
                out = pd.concat(parts, ignore_index=True) if parts else raw
                out["date"] = out.pop("ts").dt.date
                out.to_parquet(path)
                print(f"[{k}/{len(groups)}] {out['symbol'].nunique()} 只，{len(out):,} 根日线", flush=True)

    def update(self, symbols: list[str], workers: int = 3) -> None:
        done = 0

        def job(sym):
            nonlocal done
            try:
                n = self.update_symbol(sym)
                msg = f"{n:,} 根新 K 线"
            except Exception as e:  # noqa: BLE001 - report and move on to the next symbol
                msg = f"失败：{e}"
            done += 1
            print(f"[{done}/{len(symbols)}] {sym}: {msg}", flush=True)

        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(job, symbols))

    def iter_bars(self, symbols: list[str]) -> Iterator[tuple[str, pd.DataFrame]]:
        for sym in symbols:
            if self.path(sym).exists():
                yield sym, _cut_at_long_gap(pd.read_parquet(self.path(sym)))


# ---------------------------------------------------------------- Yahoo

class YahooSource:
    def __init__(self, cfg: dict):
        self.path = data_dir(cfg) / "bars_5m.parquet"

    def update(self, symbols: list[str]) -> None:
        tickers = [s.replace(".", "-") for s in symbols]
        raw = yf.download(tickers, period="60d", interval="5m", prepost=False,
                          group_by="ticker", auto_adjust=False, progress=False, threads=True)
        frames = []
        for sym, tk in zip(symbols, tickers):
            if tk not in raw.columns.get_level_values(0):
                continue
            d = raw[tk].dropna(subset=["Open", "High", "Low", "Close"]).rename(columns=str.lower)
            frames.append(d[COLUMNS[1:]].assign(symbol=sym))
        new = pd.concat(frames).rename_axis("ts").reset_index()
        new["ts"] = new["ts"].dt.tz_convert(ET)
        now = pd.Timestamp.now(tz=ET)
        if now.time() < time(16, 5):
            new = new[new["ts"].dt.date < now.date()]
        if self.path.exists():
            new = pd.concat([pd.read_parquet(self.path), new])
        new = new.drop_duplicates(["symbol", "ts"], keep="last").sort_values(["symbol", "ts"])
        new.reset_index(drop=True).to_parquet(self.path)
        print(f"完成：{new['symbol'].nunique()} 只，{new['ts'].dt.date.nunique()} 个交易日")

    def iter_bars(self, symbols: list[str]) -> Iterator[tuple[str, pd.DataFrame]]:
        if not self.path.exists():
            raise FileNotFoundError("还没有数据，请先运行：uv run quantify download")
        df = pd.read_parquet(self.path)
        for sym, g in df[df["symbol"].isin(symbols)].groupby("symbol"):
            yield sym, g[COLUMNS].reset_index(drop=True)


def get_source(cfg: dict):
    return {"alpaca": AlpacaSource, "yahoo": YahooSource}[cfg["data"]["source"]](cfg)
