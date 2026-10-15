"""AI second opinion on each new buy signal, from TradingAgents running on DeepSeek.

Only recorded: the paper accounts never act on it. After a few months the dashboard can compare
the trades the AI agreed with against the ones it didn't, which is a fair test because every
opinion is formed before the outcome is known (unlike a backtest, where the model has already
read about the period it is judging).

Needs the optional dependency (`uv sync --extra ai`) and DEEPSEEK_API_KEY.
"""

import json
import os
import shutil
import tempfile
import time
from datetime import date
from pathlib import Path

import requests

from .config import ROOT
from .data import _load_env
from .paper import DATA_PATH, PAPER_DIR, next_session

OPINIONS_PATH = PAPER_DIR / "ai_opinions.json"
MEMORY_PATH = PAPER_DIR / "ai_memory.md"  # TradingAgents' own log of past calls, kept across runs
REPORTS_DIR = ROOT / "docs" / "ai"
CACHE_DIR = ROOT / ".cache" / "tradingagents"
MODEL = "deepseek-flash"
RATING_ZH = {"Buy": "买入", "Overweight": "增持", "Hold": "持有", "Underweight": "减持", "Sell": "卖出"}
AGREES = {"Buy", "Overweight"}  # a rating that supports buying


def load_opinions() -> list[dict]:
    return json.loads(OPINIONS_PATH.read_text(encoding="utf-8")) if OPINIONS_PATH.exists() else []


def _balance(key: str) -> tuple[float, str] | None:
    """DeepSeek account balance, to measure what a run cost."""
    try:
        r = requests.get("https://api.deepseek.com/user/balance", timeout=20,
                         headers={"Authorization": f"Bearer {key}"})
        info = r.json()["balance_infos"][0]
        return float(info["total_balance"]), info["currency"]
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return None


def _graph():
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    cfg = DEFAULT_CONFIG.copy()
    cfg.update(
        llm_provider="deepseek", quick_think_llm=MODEL, deep_think_llm=MODEL,
        output_language="Chinese",
        max_tokens=8000,        # deepseek-flash can otherwise run away and hang (TradingAgents #1204)
        llm_max_retries=5,
        results_dir=str(CACHE_DIR / "results"), data_cache_dir=str(CACHE_DIR / "data"),
        memory_log_path=str(MEMORY_PATH),
    )
    return TradingAgentsGraph(config=cfg)


def _opine(graph, symbol: str, day: date) -> tuple[str, str]:
    """Run the agents for one ticker as of `day`'s close; return (rating, report path under docs/)."""
    state, rating = graph.propagate(symbol, day.isoformat())
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORTS_DIR / f"{day.isoformat()}_{symbol}.html"
    with tempfile.TemporaryDirectory() as tmp:
        graph.save_reports(state, symbol, save_path=Path(tmp), html=True)
        pages = sorted(Path(tmp).rglob("*.html"))
        if pages:
            shutil.copy(pages[0], report)
    return rating, (f"ai/{report.name}" if report.exists() else "")


def run() -> list[dict]:
    """Form an opinion on every buy signal in docs/data.json that doesn't have one yet."""
    _load_env()
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print("没有设置 DEEPSEEK_API_KEY，跳过 AI 意见")
        return []
    view = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    day = date.fromisoformat(view["as_of"])
    wanted: dict[str, list[str]] = {}
    for s in view["strategies"]:
        for o in s["tonight"]:
            if o["action"] == "buy":
                wanted.setdefault(o["symbol"], []).append(s["name"])
    opinions = load_opinions()
    done = {(o["date"], o["symbol"]) for o in opinions}
    todo = {sym: names for sym, names in wanted.items() if (day.isoformat(), sym) not in done}
    if not todo:
        print("没有需要 AI 意见的新信号")
        return []

    graph = _graph()
    added = []
    for sym, names in todo.items():
        before, t0 = _balance(key), time.time()
        print(f"AI 分析 {sym}（{'、'.join(names)}）…", flush=True)
        rating, report = _opine(graph, sym, day)
        after = _balance(key)
        cost = round(before[0] - after[0], 4) if before and after and before[1] == after[1] else None
        rec = {"date": day.isoformat(), "symbol": sym, "strategies": names, "rating": rating,
               "rating_zh": RATING_ZH.get(rating, "需复核"), "agrees": rating in AGREES, "report": report,
               "model": MODEL, "minutes": round((time.time() - t0) / 60, 1),
               "cost": cost, "currency": before[1] if before else None}
        print(f"  → {rec['rating_zh']}（{rating}），用时 {rec['minutes']} 分钟"
              + (f"，花费 {cost} {rec['currency']}" if cost is not None else ""), flush=True)
        opinions.append(rec)
        added.append(rec)
        OPINIONS_PATH.write_text(json.dumps(opinions, ensure_ascii=False, indent=1), encoding="utf-8")
    attach(view, opinions)
    DATA_PATH.write_text(json.dumps(view, ensure_ascii=False, indent=1), encoding="utf-8")
    return added


def attach(view: dict, opinions: list[dict] | None = None) -> dict:
    """Add the AI opinions to the dashboard data: on tonight's orders, on finished trades, and as
    agree-vs-disagree stats per strategy."""
    opinions = load_opinions() if opinions is None else opinions
    by_signal = {(o["date"], o["symbol"]): o for o in opinions}
    # a trade entered on session N came from a signal on the session before N
    by_entry = {(next_session(date.fromisoformat(o["date"])).isoformat(), o["symbol"]): o for o in opinions}
    keep = ("rating", "rating_zh", "agrees", "report")
    for s in view["strategies"]:
        for o in s["tonight"]:
            op = by_signal.get((view["as_of"], o["symbol"])) if o["action"] == "buy" else None
            o["ai"] = {k: op[k] for k in keep} if op else None
        groups = {True: [], False: []}
        for t in s["trades"]:
            op = by_entry.get((t["entry_date"], t["symbol"]))
            t["ai"] = {k: op[k] for k in keep} if op else None
            if op and op["rating"] in RATING_ZH:
                groups[op["agrees"]].append(t["ret_pct"])
        p = s.get("position")
        if p:
            op = by_entry.get((p["entry_date"], p["symbol"]))
            p["ai"] = {k: op[k] for k in keep} if op else None
        s["ai_stats"] = {("agree" if k else "disagree"): {"trades": len(v), "avg_ret_pct": round(sum(v) / len(v), 3) if v else None}
                         for k, v in groups.items()}
    costs = [o["cost"] for o in opinions if o.get("cost") is not None]
    view["ai"] = {"model": MODEL, "runs": len(opinions), "total_cost": round(sum(costs), 2) if costs else None,
                  "currency": next((o["currency"] for o in opinions if o.get("currency")), None)}
    return view
