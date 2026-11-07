"""AI 交易台: each evening an AI reads the market and decides what to trade at the next open (paper only).

Runs on GitHub Actions after the Tokyo server has booked the session (agent_book): the server is too small for
TradingAgents, and GitHub can't reach Binance. About 70 Claude Sonnet calls on the subscription:
  1. market brief (index moves + policy and market headlines)          1 call
  2. screen ~190 stock perpetuals from a table of numbers              1 call
  3. TradingAgents on up to 3 candidates                               ~22 calls each
  4. portfolio decision                                                1 call
then the hard rules in agent_book. If Claude fails, the day is skipped (held positions keep their stops and
time limits); no other model is mixed into the record.

Writes agent/decisions/<session>.json and refreshes docs/agent.json. Run as `python -m quantify.agent_decide`.
"""

import os
import shutil
import tempfile
import time
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
import yfinance as yf
from pydantic import BaseModel, Field

from . import agent_book as ab
from .ai_opinion import RATING_ZH
from .config import ROOT
from .data import _load_env

DEEP_N = int(os.environ.get("AGENT_DEEP_N", "3"))
MODEL = "sonnet"
MODEL_LABEL = "Claude Sonnet（订阅）"
REPORTS_DIR = ROOT / "docs" / "agent" / "reports"
TA_MEMORY = ab.AGENT_DIR / "ta_memory.md"
CACHE_DIR = ROOT / ".cache" / "agent_ta"
MACRO = {"SPY": "标普 500 ETF", "QQQ": "纳指 100 ETF", "IWM": "罗素 2000 ETF", "^VIX": "VIX 波动率",
         "^TNX": "10 年美债收益率", "DX-Y.NYB": "美元指数", "CL=F": "原油", "GC=F": "黄金", "BTC-USD": "比特币"}
NEWS_QUERIES = ["stock market today", "Federal Reserve", "tariffs trade policy", "White House economy",
                "inflation jobs report", "earnings outlook"]
LONG_OK, SHORT_OK = {"Buy", "Overweight"}, {"Underweight", "Sell"}


# ---------------------------------------------------------------- inputs

def _pct(a: float, b: float) -> float:
    return round((a / b - 1) * 100, 2)


def stock_table(universe: list[dict], marks: dict, as_of: date) -> pd.DataFrame:
    """One row of numbers per tradable stock, as of `as_of`'s close. Drops stocks whose perpetual price
    doesn't match the US ticker (a wrong mapping) or whose data is stale."""
    tick = sorted({u["ticker"] for u in universe})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        raw = yf.download(tick, period="1y", interval="1d", auto_adjust=True, progress=False, threads=True)
    close, vol = raw["Close"], raw["Volume"]
    close.index, vol.index = close.index.date, vol.index.date
    close, vol = close.loc[:as_of], vol.loc[:as_of]
    rows = []
    for u in universe:
        c = close.get(u["ticker"])
        if c is None:
            continue
        c = c.dropna()
        if len(c) < 60 or c.index[-1] != as_of:
            continue
        mark = marks.get(u["symbol"], {}).get("mark")
        if not mark or not 0.85 < mark / c.iloc[-1] < 1.15:
            continue
        v = vol[u["ticker"]].reindex(c.index)
        rows.append({
            "ticker": u["ticker"], "symbol": u["symbol"], "close": round(float(c.iloc[-1]), 2),
            "r1": _pct(c.iloc[-1], c.iloc[-2]), "r5": _pct(c.iloc[-1], c.iloc[-6]), "r20": _pct(c.iloc[-1], c.iloc[-21]),
            "r60": _pct(c.iloc[-1], c.iloc[-61]), "vs50": _pct(c.iloc[-1], c.iloc[-50:].mean()),
            "vs200": _pct(c.iloc[-1], c.iloc[-200:].mean()) if len(c) >= 200 else None,
            "vol20": round(float(c.pct_change().iloc[-20:].std() * 252 ** 0.5 * 100), 1),
            "volx": round(float(v.iloc[-5:].mean() / v.iloc[-50:].mean()), 2) if v.iloc[-50:].mean() else None,
            "off_high": _pct(c.iloc[-1], c.iloc[-252:].max()),
            "funding": round(marks[u["symbol"]]["funding_rate"] * 100, 4)})
    return pd.DataFrame(rows).set_index("ticker")


def macro_table(as_of: date) -> str:
    lines = []
    for t, name in MACRO.items():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                c = yf.Ticker(t).history(period="3mo", auto_adjust=True)["Close"].dropna()
            c.index = c.index.date
            c = c.loc[:as_of]
            lines.append(f"{name}（{t}）：{c.iloc[-1]:.2f}，1 日 {_pct(c.iloc[-1], c.iloc[-2]):+.2f}%，"
                         f"5 日 {_pct(c.iloc[-1], c.iloc[-6]):+.2f}%，20 日 {_pct(c.iloc[-1], c.iloc[-21]):+.2f}%")
        except Exception:  # noqa: BLE001 - one missing series shouldn't stop the day
            lines.append(f"{name}（{t}）：没有取到数据")
    return "\n".join(lines)


def headlines(as_of: date, limit: int = 40) -> str:
    cutoff = datetime.combine(as_of, datetime.min.time()).timestamp() - 2 * 86400
    seen, out = set(), []
    for q in NEWS_QUERIES:
        try:
            news = yf.Search(q, news_count=10).news
        except Exception:  # noqa: BLE001
            continue
        for n in news:
            title, ts = n.get("title"), n.get("providerPublishTime") or 0
            if not title or title in seen or ts < cutoff:
                continue
            seen.add(title)
            when = datetime.fromtimestamp(ts, ab.ET).strftime("%m-%d %H:%M")
            out.append((ts, f"[{when} 美东] {title}（{n.get('publisher', '')}）"))
    out.sort(reverse=True)
    return "\n".join(x for _, x in out[:limit]) or "（这次没有取到新闻标题）"


# ---------------------------------------------------------------- Claude

class Brief(BaseModel):
    regime: Literal["risk_on", "neutral", "risk_off"] = Field(description="明天开盘的整体环境：偏进攻、中性、偏防守")
    summary: str = Field(description="中文，150 字以内：大盘、利率、政策面的要点")
    drivers: list[str] = Field(description="中文，最多 5 条：最影响明天走势的因素")
    favored: list[str] = Field(description="中文：相对看好的板块或主题，可以为空")
    avoid: list[str] = Field(description="中文：相对回避的板块或主题，可以为空")


class Pick(BaseModel):
    ticker: str = Field(description="表中的股票代码")
    direction: Literal["long", "short"]
    why: str = Field(description="中文，60 字以内")


class HeldView(BaseModel):
    ticker: str
    view: Literal["keep", "close"]
    why: str = Field(description="中文，60 字以内")


class Screen(BaseModel):
    picks: list[Pick] = Field(description="最多 3 只新候选，不能是已持有的股票；没有好机会可以为空")
    held: list[HeldView] = Field(description="对每个已有持仓的看法")


class Order(BaseModel):
    ticker: str
    action: Literal["open_long", "open_short", "close"]
    weight: float = Field(0, description="开仓时占权益的比例，0–0.33；平仓填 0")
    stop_pct: float = Field(0, description="开仓时止损距离，0.02–0.08；平仓填 0")
    hold_days: int = Field(0, description="开仓时最多持有的交易日，1–10；平仓填 0")
    confidence: Literal["low", "medium", "high"]
    reason: str = Field(description="中文，80 字以内")


class Plan(BaseModel):
    orders: list[Order] = Field(description="明天开盘要做的操作；什么都不做就留空")
    note: str = Field(description="中文，100 字以内：今天决策的总体思路")


class Claude:
    def __init__(self):
        from tradingagents.llm_clients.claude_code_client import ChatClaudeCode
        cli = shutil.which("claude")
        if not cli:
            raise RuntimeError("找不到 claude 命令行")
        self.llm = ChatClaudeCode(model=MODEL, cli_path=cli, timeout=900, max_retries=1)
        self.calls = 0

    def ask(self, schema, system: str, user: str):
        from langchain_core.messages import HumanMessage, SystemMessage
        self.calls += 1
        return self.llm.with_structured_output(schema).invoke([SystemMessage(system), HumanMessage(user)])


RULES_TEXT = (
    f"硬规则（程序会拒绝违反的指令）：1 倍杠杆，持仓总值不超过权益；最多同时 {ab.MAX_NAMES} 只，每只不超过权益的 "
    f"{ab.MAX_WEIGHT:.0%}；每笔止损距离 {ab.STOP_RANGE[0]:.0%}–{ab.STOP_RANGE[1]:.0%}；最多持有 {ab.MAX_HOLD} 个交易日；"
    f"每周新开仓不超过 {ab.MAX_NEW_PER_WEEK} 笔；资金费超过 {ab.FUNDING_LIMIT:.1%} 的一方不开仓；"
    "做多必须有 TradingAgents 的买入或增持评级，做空必须有减持或卖出评级。"
    f"成本：每边手续费 {ab.FEE:.2%} 加滑点 {ab.SLIP:.2%}，多头通常要付资金费。")


def _positions_text(state: dict) -> str:
    if not state["positions"]:
        return "（空仓）"
    out = []
    for p in state["positions"]:
        last = p.get("last", p["entry_price"])
        out.append(f"{p['ticker']} {'多' if p['side'] == 'long' else '空'} {p['qty']} 股，开仓 {p['entry_price']:.2f}，"
                   f"现价 {last:.2f}（{ab._sgn(p['side']) * (last / p['entry_price'] - 1) * 100:+.2f}%），"
                   f"止损 {p['stop']:.2f}，已持有 {p['held']}/{p['hold_days']} 天"
                   + ("，下一个开盘将平仓：" + p["close_next"] if p.get("close_next") else ""))
    return "\n".join(out)


def deep_analysis(ticker: str, as_of: date) -> dict:
    """TradingAgents on one stock as of the close; returns its rating, a summary and the saved report."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(output_language="Chinese", llm_provider="claude_code", quick_think_llm=MODEL, deep_think_llm=MODEL,
               llm_max_retries=1, results_dir=str(CACHE_DIR / "results"), data_cache_dir=str(CACHE_DIR / "data"),
               memory_log_path=str(TA_MEMORY))
    graph = TradingAgentsGraph(config=cfg)
    state, rating = graph.propagate(ticker, as_of.isoformat())
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORTS_DIR / f"{as_of.isoformat()}_{ticker}.html"
    with tempfile.TemporaryDirectory() as tmp:
        graph.save_reports(state, ticker, save_path=Path(tmp), html=True)
        pages = sorted(Path(tmp).rglob("*.html"))
        if pages:
            shutil.copy(pages[0], report)
    text = str(state.get("final_trade_decision") or "")
    return {"rating": rating, "rating_zh": RATING_ZH.get(rating, "需复核"), "summary": text[:2500],
            "report": f"agent/reports/{report.name}" if report.exists() else ""}


# ---------------------------------------------------------------- the hard rules on the AI's plan

def check_plan(plan: Plan, state: dict, deep: dict, table: pd.DataFrame, marks: dict, session: date) -> tuple[list, list]:
    equity = state["curve"][-1]["equity"] if state["curve"] else state["capital"]
    held = {p["ticker"]: p for p in state["positions"]}
    accepted, rejected = [], []
    closing = set()
    for o in plan.orders:
        if o.action == "close":
            if o.ticker in held and o.ticker not in closing:
                closing.add(o.ticker)
                accepted.append({"action": "close", "ticker": o.ticker, "symbol": held[o.ticker]["symbol"], "reason": o.reason})
            else:
                rejected.append({**o.model_dump(), "why": "没有这只持仓"})
    remaining = [p for t, p in held.items() if t not in closing]
    names = len(remaining)
    gross = sum(p["qty"] * p.get("last", p["entry_price"]) for p in remaining) / equity if equity else 1
    trial = {**state, "positions": list(state["positions"])}
    for o in plan.orders:
        if o.action == "close":
            continue
        side = "long" if o.action == "open_long" else "short"
        d = deep.get(o.ticker)
        why = None
        if o.ticker not in table.index:
            why = "不在可交易列表里"
        elif not d or not d.get("rating"):
            why = "没有深入分析过"
        elif side == "long" and d["rating"] not in LONG_OK:
            why = f"评级是{d['rating_zh']}，不能做多"
        elif side == "short" and d["rating"] not in SHORT_OK:
            why = f"评级是{d['rating_zh']}，不能做空"
        order = {"action": "open", "ticker": o.ticker, "symbol": table.at[o.ticker, "symbol"] if o.ticker in table.index else "",
                 "side": side, "weight": round(o.weight, 4), "stop_pct": round(o.stop_pct, 4), "hold_days": o.hold_days,
                 "confidence": o.confidence, "rating": d.get("rating") if d else None, "reason": o.reason}
        why = why or ab.check_open(order, trial, session, marks)
        if not why and names >= ab.MAX_NAMES:
            why = f"已持有或将持有 {ab.MAX_NAMES} 只"
        if not why and gross + o.weight > ab.MAX_GROSS + 1e-9:
            why = "持仓总值会超过权益（不加杠杆）"
        if why:
            rejected.append({**order, "why": why})
            continue
        accepted.append(order)
        names += 1
        gross += o.weight
        trial["positions"] = trial["positions"] + [{"ticker": o.ticker, "entry_date": session.isoformat()}]
    return accepted, rejected


# ---------------------------------------------------------------- the day

def decide(state: dict, as_of: date, session: date) -> dict:
    uni = ab.load_json(ab.UNIVERSE_PATH)["symbols"]
    marks = ab.load_json(ab.MARKET_PATH)["marks"]
    claude = Claude()
    t0 = time.time()
    out = {"as_of": as_of.isoformat(), "for_session": session.isoformat(), "model": MODEL_LABEL, "status": "ok",
           "orders": [], "rejected": [], "deep": []}
    try:
        table = stock_table(uni, marks, as_of)
        if len(table) < 100:
            raise RuntimeError(f"只取到 {len(table)} 只股票的数据，今天不决策")
        out["universe"] = len(table)
        brief = claude.ask(Brief, "你是美股宏观策略分析师。只根据给出的市场数据和新闻标题判断明天美股开盘时的整体环境，"
                                  "不要编造数据。用中文回答。",
                           f"日期：{as_of}（美东收盘后）\n\n市场数据：\n{macro_table(as_of)}\n\n最近两天的新闻标题：\n{headlines(as_of)}")
        out["brief"] = brief.model_dump()
        held = {p["ticker"] for p in state["positions"]}
        cols = "ticker,close,r1,r5,r20,r60,vs50,vs200,vol20,volx,off_high,funding"
        legend = ("列说明：r1/r5/r20/r60 = 1/5/20/60 日涨跌 %；vs50/vs200 = 相对 50/200 日均线 %；vol20 = 20 日年化波动 %；"
                  "volx = 近 5 日成交量 / 近 50 日均量；off_high = 离一年最高价 %；funding = 当前资金费率 %（正数时多头付费）。")
        screen = claude.ask(Screen, "你是美股短线交易员，管理一个模拟账户。只能交易下表里的股票（币安美股永续合约），1 倍杠杆，"
                                    f"可以做多也可以做空，每笔持有 1–{ab.MAX_HOLD} 个交易日。选出最值得在下一个交易日开盘交易的最多 "
                                    f"{DEEP_N} 只股票，说明方向和理由；对已有持仓给出继续持有或平仓的看法。不要把已持有的股票列为新候选。"
                                    "没有把握可以少选或不选。用中文回答。",
                            f"大盘简报：{brief.model_dump_json()}\n\n当前持仓：\n{_positions_text(state)}\n\n{legend}\n"
                            + table.reset_index()[cols.split(",")].to_csv(index=False))
        out["screen"] = screen.model_dump()
        picks = [p for p in screen.picks if p.ticker in table.index and p.ticker not in held][:DEEP_N]
        deep = {}
        for p in picks:
            try:
                r = deep_analysis(p.ticker, as_of)
            except Exception as e:  # noqa: BLE001 - one failed stock shouldn't stop the others
                r = {"rating": None, "rating_zh": "分析失败", "summary": "", "report": "", "error": f"{type(e).__name__}: {str(e)[:200]}"}
            deep[p.ticker] = r
            out["deep"].append({"ticker": p.ticker, "direction": p.direction, **{k: v for k, v in r.items() if k != "summary"}})
        analyses = "\n\n".join(f"【{t}】评级：{r['rating_zh']}（{r['rating']}）\n{r['summary']}" for t, r in deep.items()) or "（今天没有深入分析）"
        equity = state["curve"][-1]["equity"] if state["curve"] else state["capital"]
        plan = claude.ask(Plan, "你是这个模拟账户的组合经理。根据大盘简报、粗筛结论和 TradingAgents 的深入分析，决定下一个交易日开盘的操作。"
                                f"{RULES_TEXT} 只有深入分析过的股票才能开仓。可以什么都不做。用中文写理由。",
                          f"日期：{as_of} 收盘后，操作在 {session} 美东 09:35 成交。\n账户权益 {equity:,.2f} USDT，现金 {state['cash']:,.2f}。\n"
                          f"当前持仓：\n{_positions_text(state)}\n\n大盘简报：{brief.model_dump_json()}\n\n粗筛：{screen.model_dump_json()}\n\n"
                          f"深入分析：\n{analyses}\n\n候选的资金费率 %："
                          + ", ".join(f"{t} {table.at[t, 'funding']}" for t in deep if t in table.index))
        out["plan"] = plan.model_dump()
        out["orders"], out["rejected"] = check_plan(plan, state, deep, table, marks, session)
    except Exception as e:  # noqa: BLE001 - Claude down or out of quota: skip the day, keep the record clean
        out["status"] = "skipped"
        out["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    out["claude_calls_outside_tradingagents"] = claude.calls
    out["minutes"] = round((time.time() - t0) / 60, 1)
    out["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    return out


def run(force: bool = False) -> dict | None:
    _load_env()
    state = ab.load_json(ab.STATE_PATH)
    if not state:
        print("还没有记账状态（agent/state.json），先在服务器上运行 agent_book")
        return None
    as_of = date.fromisoformat(state["last_session"])
    path = ab.DECISIONS_DIR / f"{as_of.isoformat()}.json"
    if path.exists() and not force:
        print(f"{as_of} 的决策已经有了")
        return None
    session = ab.next_session(as_of)
    out = decide(state, as_of, session)
    ab.save_json(path, out)
    ab.write_view(state)
    if out["status"] != "ok":
        print(f"今天跳过：{out['error']}")
    else:
        print(f"{session} 开盘：" + ("；".join(f"{o['action']} {o['ticker']}" + (f" {o['side']} {o['weight']:.0%}" if o["action"] == "open" else "")
                                          for o in out["orders"]) or "不操作")
              + (f"；被规则拒绝 {len(out['rejected'])} 条" if out["rejected"] else "") + f"，用时 {out['minutes']} 分钟")
    return out


if __name__ == "__main__":
    import sys
    run(force="--force" in sys.argv)
