"""Nasdaq-100 constituents: Nasdaq's official list, Wikipedia as fallback, cached to disk."""

import io
import re

import pandas as pd
import requests

from .config import data_dir

HEADERS = {"User-Agent": "Mozilla/5.0 quantify-research", "Accept": "application/json"}


def _from_nasdaq() -> list[str]:
    r = requests.get("https://api.nasdaq.com/api/quote/list-type/nasdaq100", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return [row["symbol"] for row in r.json()["data"]["data"]["rows"]]


def _from_wikipedia() -> list[str]:
    r = requests.get("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies", headers=HEADERS, timeout=15)
    r.raise_for_status()
    for t in pd.read_html(io.StringIO(r.text)):
        if "Ticker" in t.columns and len(t) > 90:
            return t["Ticker"].astype(str).tolist()
    raise RuntimeError("Nasdaq-100 table not found on Wikipedia")


WIKI_API = "https://en.wikipedia.org/w/api.php"


def _wiki_revision_before(title: str, when: str) -> int:
    r = requests.get(WIKI_API, headers=HEADERS, timeout=20, params={
        "action": "query", "prop": "revisions", "titles": title, "rvlimit": 1, "rvstart": when,
        "rvdir": "older", "rvprop": "ids", "format": "json", "redirects": 1}).json()
    return next(iter(r["query"]["pages"].values()))["revisions"][0]["revid"]


def _constituents_at(when: str) -> list[str]:
    """Nasdaq-100 members as listed on Wikipedia's Nasdaq-100 article just before `when`."""
    revid = _wiki_revision_before("Nasdaq-100", when)
    html = requests.get("https://en.wikipedia.org/w/index.php", headers=HEADERS,
                        params={"oldid": revid}, timeout=30).text
    for t in pd.read_html(io.StringIO(html)):
        if "Ticker" in t.columns and len(t) > 90:
            return sorted(t["Ticker"].astype(str))
    # Older revisions list members as "#[[Company]] (TICKER)" lines under ==Components==.
    r = requests.get(WIKI_API, headers=HEADERS, timeout=20, params={
        "action": "query", "prop": "revisions", "revids": revid, "rvprop": "content",
        "rvslots": "main", "format": "json"}).json()
    text = next(iter(r["query"]["pages"].values()))["revisions"][0]["slots"]["main"]["*"]
    section = text[text.index("==Components=="):text.index("===Historical Components===")]
    return sorted(set(re.findall(r"^#.*\(([A-Z.]+)\)\s*$", section, flags=re.M)))


# Same company, new ticker: Alpaca serves the whole history under the current one.
RENAMED = {"FB": "META", "PCLN": "BKNG"}


def historical_universe(cfg: dict, year: int) -> list[str]:
    """Members at the start of `year`, to test without knowing which companies later succeeded."""
    cache = data_dir(cfg) / f"nasdaq100_{year}.csv"
    if cache.exists():
        symbols = pd.read_csv(cache)["symbol"].tolist()
    else:
        symbols = _constituents_at(f"{year}-01-01T00:00:00Z")
        pd.DataFrame({"symbol": symbols}).to_csv(cache, index=False)
    return sorted({RENAMED.get(s, s) for s in symbols})


def all_symbols(cfg: dict) -> list[str]:
    """Current members plus the 2016 and 2023 members (for survivorship-free tests)."""
    return sorted(set(get_universe(cfg)) | set(historical_universe(cfg, 2016))
                  | set(historical_universe(cfg, 2023)))


def get_universe(cfg: dict, refresh: bool = False) -> list[str]:
    cache = data_dir(cfg) / "nasdaq100.csv"
    if cache.exists() and not refresh:
        return pd.read_csv(cache)["symbol"].tolist()
    for source in (_from_nasdaq, _from_wikipedia):
        try:
            symbols = sorted(set(source()))
            break
        except Exception as e:  # noqa: BLE001 - try the next source
            print(f"成分股来源 {source.__name__} 失败：{e}")
    else:
        raise RuntimeError("无法获取纳斯达克 100 成分股")
    pd.DataFrame({"symbol": symbols}).to_csv(cache, index=False)
    return symbols


# ---------------------------------------------------------------- whole market (Alpha Vantage)

AV_URL = "https://www.alphavantage.co/query"
AV_EXCHANGES = {"NYSE", "NASDAQ", "NYSE MKT", "BATS"}


def _av_listing(cfg: dict, state: str, day: str | None) -> pd.DataFrame:
    """Alpha Vantage LISTING_STATUS: stocks listed on `day` (state=active), or all delisted ones."""
    import os
    import time

    from .data import _load_env

    cache = data_dir(cfg) / "av_listings" / f"{state}_{day or 'latest'}.csv"
    cache.parent.mkdir(exist_ok=True)
    if cache.exists():
        return pd.read_csv(cache)
    _load_env()
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not key:
        raise RuntimeError("在 .env 中找不到 ALPHAVANTAGE_API_KEY")
    params = {"function": "LISTING_STATUS", "state": state, "apikey": key}
    if day:
        params["date"] = day
    r = requests.get(AV_URL, params=params, timeout=60)
    r.raise_for_status()
    if not r.text.startswith("symbol"):
        raise RuntimeError("Alpha Vantage 返回的不是名单（可能超出每日次数限制）")
    cache.write_text(r.text, encoding="utf-8")
    time.sleep(13)  # free plan: stay well under 5 requests/minute
    return pd.read_csv(cache)


def av_historical_stocks(cfg: dict) -> pd.DataFrame:
    """Every common stock listed on a major US exchange at the start of any year since 2016,
    plus every delisted one: (symbol, name, exchange, ipoDate, delistingDate)."""
    days = [f"{y}-01-05" for y in range(2016, pd.Timestamp.now().year + 1)]
    frames = [_av_listing(cfg, "active", d) for d in days] + [_av_listing(cfg, "delisted", None)]
    df = pd.concat(frames, ignore_index=True)
    df = df[(df["assetType"] == "Stock") & df["exchange"].isin(AV_EXCHANGES)
            & df["symbol"].astype(str).str.fullmatch(r"[A-Z]{1,5}")]
    delisted = pd.to_datetime(df["delistingDate"], errors="coerce")
    df = df[delisted.isna() | (delisted >= "2016-01-01")]
    return df.drop_duplicates("symbol", keep="last")
