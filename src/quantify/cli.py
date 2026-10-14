import argparse
import copy
import itertools
from datetime import date

import numpy as np
import pandas as pd

from .backtest import benchmark_return, prepare, simulate
from .config import data_dir, load_config
from .data import get_source
from . import noise, notify, paper, swing, wide
from .strategies import STRATEGIES
from .swing_strategies import SWING_STRATEGIES
from .report import print_recent, print_report, print_yearly, summarize
from .universe import get_universe, historical_universe

# Parameters are chosen on the in-sample years only; the out-of-sample years are the honest test.
IN_SAMPLE_END = date(2022, 12, 31)
OUT_SAMPLE_START = date(2023, 1, 1)


def cmd_download(cfg: dict, args) -> None:
    symbols = get_universe(cfg, refresh=True)
    print(f"纳斯达克 100：{len(symbols)} 只股票，从 {cfg['data']['source']} 下载/更新 5 分钟 K 线…")
    get_source(cfg).update([cfg["data"]["benchmark"]] + symbols)


def cmd_backtest(cfg: dict, args) -> None:
    print("扫描数据中…")
    prep = prepare(cfg)
    r = simulate(prep, cfg)
    print_report(r, benchmark_return(prep, r.first_date, r.last_date), "全部区间")
    print_yearly(r)
    print_recent(r)
    out = data_dir(cfg) / "trades.csv"
    r.trades.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n全部交易明细已保存到 {out}")


def _row(m: dict) -> dict:
    return {"笔数": m["trades"], "胜率": f"{m['win_rate']:.0%}", "费前%": round(m["avg_gross_pct"], 3),
            "每笔%": round(m["avg_ret_pct"], 3),
            "总收益": f"{m['total_ret']:+.1%}", "回撤": f"{m['max_dd']:.1%}"}


# Pass rule, fixed before looking at any results.
MIN_OOS_TRADES = 100
# Trading windows tried in sweeps: (last entry, exit by), ET.
WINDOWS = {"3小时": ("11:30", "12:30"), "全天": ("15:30", "16:00")}


def _plain(v):
    return v.item() if hasattr(v, "item") else v


def _sweep_one(cfg: dict, name: str) -> dict:
    strategy = STRATEGIES[name]
    cfg = copy.deepcopy(cfg)
    cfg["account"]["compound"] = False   # fixed size: an early losing streak can't distort later years
    cfg["strategy"]["name"] = name
    rows = []
    for fvals in itertools.product(*strategy.GRID_FEATURES.values()):
        c = copy.deepcopy(cfg)
        c["strategy"][name].update(dict(zip(strategy.GRID_FEATURES, fvals)))
        print(f"  扫描 {dict(zip(strategy.GRID_FEATURES, fvals))}…", flush=True)
        prep = prepare(c, verbose=False)
        windows = getattr(strategy, "WINDOWS", list(WINDOWS))
        for window, svals in itertools.product(windows, itertools.product(*strategy.GRID_SIM.values())):
            c["session"]["last_entry"], c["session"]["exit_by"] = WINDOWS[window]
            c["strategy"].update(dict(zip(strategy.GRID_SIM, svals)))
            ins = summarize(simulate(prep, c, end=IN_SAMPLE_END))
            oos = summarize(simulate(prep, c, start=OUT_SAMPLE_START))
            rows.append({**dict(zip(strategy.GRID_FEATURES, fvals)), "窗口": window,
                         **dict(zip(strategy.GRID_SIM, svals)),
                         **{f"样本内{k}": v for k, v in _row(ins).items()},
                         **{f"样本外{k}": v for k, v in _row(oos).items()},
                         "_ins": ins["avg_ret_pct"] if ins["trades"] else float("-inf"),
                         "_oos": oos["avg_ret_pct"], "_oos_n": oos["trades"]})
    df = pd.DataFrame(rows)
    shown = df.drop(columns=["_ins", "_oos", "_oos_n"])
    print(f"\n【{strategy.NAME}】样本内 = 2016–2022（选参数用），样本外 = 2023 至今（检验用）\n")
    print(shown.to_string(index=False))
    shown.to_csv(data_dir(cfg) / f"sweep_{name}.csv", index=False, encoding="utf-8-sig")

    best = df.loc[df["_ins"].idxmax()]
    params = {k: _plain(best[k]) for k in [*strategy.GRID_FEATURES, "窗口", *strategy.GRID_SIM]}
    share_pos = (df["_oos"] > 0).mean()
    checks = {
        "最佳参数样本外为正": best["_oos"] > 0,
        "过半组合样本外为正": share_pos >= 0.5,
        f"样本外 ≥ {MIN_OOS_TRADES} 笔": best["_oos_n"] >= MIN_OOS_TRADES,
    }
    print(f"\n样本内最好的参数：{params}")
    print(f"  → 样本内每笔 {best['_ins']:+.3f}%，样本外每笔 {best['_oos']:+.3f}%（{best['_oos_n']} 笔）")
    print(f"样本外每笔为正的组合：{(df['_oos'] > 0).sum()} / {len(df)}")
    for k, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {k}")
    passed = all(checks.values())
    print("结论：" + ("通过" if passed else "未通过"))
    return {"策略": strategy.NAME, "组合数": len(df), "样本外为正": f"{(df['_oos'] > 0).sum()}/{len(df)}",
            "最佳参数": params, "样本内每笔%": round(best["_ins"], 3), "样本外每笔%": round(best["_oos"], 3),
            "样本外笔数": int(best["_oos_n"]), "结论": "通过" if passed else "未通过"}


def cmd_sweep(cfg: dict, args) -> None:
    """Parameter grid per strategy, scored separately on in-sample and out-of-sample years."""
    names = list(STRATEGIES) if args.strategy == "all" else [args.strategy or cfg["strategy"]["name"]]
    summary = []
    for name in names:
        print(f"\n========== {STRATEGIES[name].NAME}（{name}）==========", flush=True)
        summary.append(_sweep_one(cfg, name))
    if len(summary) > 1:
        print("\n========== 汇总 ==========")
        print(pd.DataFrame(summary).drop(columns=["最佳参数"]).to_string(index=False))


# Swing pass rule, fixed before looking at any swing results. Fewer, longer trades, so a lower minimum.
MIN_OOS_TRADES_SWING = 50


def _swing_cfg(cfg: dict) -> dict:
    cfg = copy.deepcopy(cfg)
    cfg["account"]["compound"] = False
    return cfg


def _stock_lists(cfg: dict, pit: bool) -> tuple[list[str], list[str]]:
    """(in-sample stocks, out-of-sample stocks).

    pit=True uses the members at the start of each period (2016 / 2023), so the test can't
    benefit from knowing which companies later succeeded; otherwise today's members for both.
    """
    if pit:
        return historical_universe(cfg, 2016), historical_universe(cfg, 2023)
    now = get_universe(cfg)
    return now, now


def _swing_sweep_one(cfg: dict, panel, name: str, pit: bool) -> dict:
    strat = SWING_STRATEGIES[name]
    ins_stocks, oos_stocks = _stock_lists(cfg, pit)
    rows = []
    for vals in itertools.product(*strat.GRID.values()):
        prm = dict(zip(strat.GRID, vals))
        periods = []
        for stocks in (ins_stocks, oos_stocks):
            syms = ["QQQ"] if prm["universe"] == "QQQ" else [c for c in stocks if c != "QQQ"]
            sub = panel.subset(["QQQ"] + syms)  # ranking strategies must only see this universe
            periods.append((sub, strat.signals(sub, prm), syms))
        (pi, si, yi), (po, so, yo) = periods
        ins = summarize(swing.simulate(pi, si, cfg, end=IN_SAMPLE_END, symbols=yi))
        oos = summarize(swing.simulate(po, so, cfg, start=OUT_SAMPLE_START, symbols=yo))
        rows.append({**prm, **{f"样本内{k}": v for k, v in _row(ins).items()},
                     **{f"样本外{k}": v for k, v in _row(oos).items()},
                     "_ins": ins["avg_ret_pct"] if ins["trades"] else float("-inf"),
                     "_oos": oos["avg_ret_pct"], "_oos_n": oos["trades"]})
    df = pd.DataFrame(rows)
    shown = df.drop(columns=["_ins", "_oos", "_oos_n"])
    which = "当时的成分股（2016 年初 / 2023 年初名单）" if pit else "今天的成分股"
    print(f"\n【{strat.NAME}】{strat.DOC}\n样本内 = 2016–2022，样本外 = 2023 至今；股票池：{which}\n")
    print(shown.to_string(index=False))
    shown.to_csv(data_dir(cfg) / f"swing_sweep_{name}{'_pit' if pit else ''}.csv", index=False, encoding="utf-8-sig")
    best = df.loc[df["_ins"].idxmax()]
    params = {k: _plain(best[k]) for k in strat.GRID}
    checks = {
        "最佳参数样本外为正": best["_oos"] > 0,
        "过半组合样本外为正": (df["_oos"] > 0).mean() >= 0.5,
        f"样本外 ≥ {MIN_OOS_TRADES_SWING} 笔": best["_oos_n"] >= MIN_OOS_TRADES_SWING,
    }
    print(f"\n样本内最好的参数：{params}")
    print(f"  → 样本内每笔 {best['_ins']:+.3f}%，样本外每笔 {best['_oos']:+.3f}%（{best['_oos_n']} 笔）")
    print(f"样本外每笔为正的组合：{(df['_oos'] > 0).sum()} / {len(df)}")
    for k, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {k}")
    passed = all(checks.values())
    print("结论：" + ("通过" if passed else "未通过"))
    return {"策略": strat.NAME, "组合数": len(df), "样本外为正": f"{(df['_oos'] > 0).sum()}/{len(df)}",
            "最佳参数": params, "样本内每笔%": round(best["_ins"], 3), "样本外每笔%": round(best["_oos"], 3),
            "样本外笔数": int(best["_oos_n"]), "样本外总收益": best["样本外总收益"],
            "结论": "通过" if passed else "未通过"}


def cmd_swing_sweep(cfg: dict, args) -> None:
    """Parameter grid for swing strategies, in-sample vs out-of-sample, fixed position size."""
    cfg = _swing_cfg(cfg)
    panel = swing.load_panel(cfg)
    names = list(SWING_STRATEGIES) if args.strategy == "all" else [args.strategy]
    summary = [_swing_sweep_one(cfg, panel, n, args.pit) for n in names]
    q = swing.benchmark_return(panel, "QQQ", _first_day_on_or_after(panel, OUT_SAMPLE_START), panel.close.index[-1])
    print(f"\n参考：样本外期间持有 QQQ {q:+.1%}")
    if len(summary) > 1:
        print("\n========== 波段汇总 ==========")
        print(pd.DataFrame(summary).drop(columns=["最佳参数"]).to_string(index=False))


WIDE_SLIPPAGE = 0.001  # smaller, faster stocks have wider spreads than the Nasdaq-100


def _significance(trades: pd.DataFrame, label: str) -> None:
    x = trades["ret_pct"].to_numpy()
    if len(x) < 3:
        return
    rng = np.random.default_rng(0)
    t = x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))
    p_le0 = np.mean(rng.choice(x, size=(10000, len(x))).mean(axis=1) <= 0)
    print(f"  {label}：{len(x)} 笔，每笔 {x.mean():+.3f}%，t={t:.2f}，'其实不赚钱'的概率 {p_le0:.0%}，"
          f"去掉最好 3 笔后 {np.sort(x)[:-3].mean():+.3f}%")


def cmd_wide_sweep(cfg: dict, args) -> None:
    """Luk-style pullback on the whole market (daily universe, delisted stocks included)."""
    cfg = _swing_cfg(cfg)
    cfg["execution"]["slippage_pct"] = args.slippage
    print("加载全市场日线…", flush=True)
    panel = wide.load_wide_panel(cfg)
    stocks = [c for c in panel.close.columns if c != "QQQ"]
    print(f"曾满足股票池条件的股票：{len(stocks)} 只；滑点每边 {args.slippage:.2%}", flush=True)
    strat = wide.LukWide
    rows, results = [], {}
    for vals in itertools.product(*strat.GRID.values()):
        prm = dict(zip(strat.GRID, vals))
        sig = strat.signals(panel, prm)
        ri = swing.simulate(panel, sig, cfg, end=IN_SAMPLE_END, symbols=stocks)
        ro = swing.simulate(panel, sig, cfg, start=OUT_SAMPLE_START, symbols=stocks)
        ins, oos = summarize(ri), summarize(ro)
        results[vals] = (ri, ro)
        rows.append({**prm, **{f"样本内{k}": v for k, v in _row(ins).items()},
                     **{f"样本外{k}": v for k, v in _row(oos).items()},
                     "_ins": ins["avg_ret_pct"] if ins["trades"] else float("-inf"),
                     "_oos": oos["avg_ret_pct"], "_oos_n": oos["trades"]})
    df = pd.DataFrame(rows)
    shown = df.drop(columns=["_ins", "_oos", "_oos_n"])
    print(f"\n【{strat.NAME}】{strat.DOC}\n样本内 = 2016–2022，样本外 = 2023 至今\n")
    print(shown.to_string(index=False))
    shown.to_csv(data_dir(cfg) / "wide_sweep_luk.csv", index=False, encoding="utf-8-sig")
    best_i = df["_ins"].idxmax()
    best = df.loc[best_i]
    params = {k: _plain(best[k]) for k in strat.GRID}
    checks = {
        "最佳参数样本外为正": best["_oos"] > 0,
        "过半组合样本外为正": (df["_oos"] > 0).mean() >= 0.5,
        f"样本外 ≥ {MIN_OOS_TRADES_SWING} 笔": best["_oos_n"] >= MIN_OOS_TRADES_SWING,
    }
    print(f"\n样本内最好的参数：{params}")
    print(f"  → 样本内每笔 {best['_ins']:+.3f}%，样本外每笔 {best['_oos']:+.3f}%（{best['_oos_n']} 笔）")
    print(f"样本外每笔为正的组合：{(df['_oos'] > 0).sum()} / {len(df)}")
    for k, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {k}")
    print("结论：" + ("通过" if all(checks.values()) else "未通过"))
    ri, ro = list(results.values())[best_i]
    print("\n最佳参数的统计显著性：")
    _significance(ri.trades, "样本内")
    _significance(ro.trades, "样本外")
    print_yearly(type(ri)(pd.concat([ri.trades, ro.trades]), ri.skips, 0, ri.start_capital, 0, None, None, 0))
    ro.trades.to_csv(data_dir(cfg) / "wide_luk_best_oos_trades.csv", index=False, encoding="utf-8-sig")


NOISE_GRID = {"stop": ["opp_band", "ub_vwap"], "check_every": [30, 60], "lookback": [10, 14, 20]}


def cmd_noise_sweep(cfg: dict, args) -> None:
    """QQQ full-day intraday momentum (noise-area breakout), in-sample vs out-of-sample."""
    cfg = copy.deepcopy(cfg)
    cfg["account"]["compound"] = False
    rows = []
    for lb in NOISE_GRID["lookback"]:
        df = noise.features(cfg, lookback=lb)
        for stop, every in itertools.product(NOISE_GRID["stop"], NOISE_GRID["check_every"]):
            prm = {"stop": stop, "check_every": every}
            ins = summarize(noise.simulate(df, cfg, prm, end=IN_SAMPLE_END))
            oos = summarize(noise.simulate(df, cfg, prm, start=OUT_SAMPLE_START))
            rows.append({"lookback": lb, **prm, **{f"样本内{k}": v for k, v in _row(ins).items()},
                         **{f"样本外{k}": v for k, v in _row(oos).items()},
                         "_ins": ins["avg_ret_pct"], "_oos": oos["avg_ret_pct"], "_oos_n": oos["trades"]})
    df = pd.DataFrame(rows)
    print("\n【QQQ 全天日内动量（噪声区间突破）】只做多，收盘前平仓\n样本内 = 2016–2022，样本外 = 2023 至今\n")
    print(df.drop(columns=["_ins", "_oos", "_oos_n"]).to_string(index=False))
    best = df.loc[df["_ins"].idxmax()]
    checks = {"最佳参数样本外为正": best["_oos"] > 0, "过半组合样本外为正": (df["_oos"] > 0).mean() >= 0.5,
              f"样本外 ≥ {MIN_OOS_TRADES} 笔": best["_oos_n"] >= MIN_OOS_TRADES}
    print(f"\n样本内最好的参数：{ {k: _plain(best[k]) for k in NOISE_GRID} }")
    print(f"  → 样本内每笔 {best['_ins']:+.3f}%，样本外每笔 {best['_oos']:+.3f}%（{best['_oos_n']} 笔）")
    for k, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {k}")
    print("结论：" + ("通过" if all(checks.values()) else "未通过"))


def _first_day_on_or_after(panel, day):
    return next(d for d in panel.close.index if d >= day)


def cmd_paper(cfg: dict, args) -> None:
    """Daily paper-trading update for the watched swing strategies (run after each US close)."""
    start = date.fromisoformat(args.start) if args.start else None
    v = paper.run(cfg, start=start, reset=args.reset, only=args.only)
    print(f"数据截至 {v['as_of']}（美东收盘），下一个交易日 {v['next_session']}，美东时间 09:30 开盘")
    for s in v["strategies"]:
        a = s["account"]
        print(f"\n【{s['name']}】模拟账户 ${a['equity']:,.2f}（{a['return_pct']:+.2f}%，同期 QQQ {a['qqq_return_pct']:+.2f}%），"
              f"本月免佣额度已用 HK${a['quota_used_hkd']:,} / {a['quota_hkd']:,}")
        if s["position"]:
            p = s["position"]
            print(f"  持仓：{p['symbol']} {p['shares']:g} 股，成本 ${p['entry_price']}，现价 ${p['last']}"
                  f"（{p['unrealized_pct']:+.2f}%），已持有 {p['held_days']} 天")
        for o in s["tonight"]:
            verb = "买入" if o["action"] == "buy" else "卖出"
            extra = f" 约 {o['est_shares']} 股" if o["action"] == "buy" else ""
            stop = f"，止损 ${o['stop']:.2f}" if o.get("stop") else ""
            print(f"  下次开盘{verb} {o['symbol']}{extra}（参考价 ${o['ref_price']}，{o['reason']}{stop}）")
        if not s["tonight"]:
            print("  下次开盘：无操作")
        print(f"  信号 {len(s['candidates'])} 只，{s['watch_title']} {len(s['watchlist'])} 只")
    print("\n仪表盘数据已写入 docs/data.json")


def cmd_notify(cfg: dict, args) -> None:
    """Telegram push: the daily summary, a failure notice, or finding the chat id during setup."""
    if args.find_chat:
        chat = notify.find_chat_id()
        print(f"找到对话：TELEGRAM_CHAT_ID={chat}" if chat else "没找到对话。先在 Telegram 里给机器人发一条 /start，再试一次。")
    elif args.failed is not None:
        notify.send_failure(args.failed)
    elif notify.send_daily(force=args.force):
        print("推送已发送")


def main() -> None:
    p = argparse.ArgumentParser(prog="quantify", description="美股日内交易信号工具")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("download", help="下载/更新纳斯达克 100 的 5 分钟 K 线")
    sub.add_parser("backtest", help="按 config.yaml 回测")
    sw = sub.add_parser("sweep", help="参数稳健性测试（样本内/样本外）")
    sw.add_argument("strategy", nargs="?", choices=[*STRATEGIES, "all"],
                    help="要测试的策略，all = 全部；默认用 config.yaml 里的策略")
    ss = sub.add_parser("swing-sweep", help="波段策略参数检验（样本内/样本外）")
    ss.add_argument("strategy", choices=[*SWING_STRATEGIES, "all"])
    ss.add_argument("--pit", action="store_true", help="用当时的成分股（消除幸存者偏差）")
    sub.add_parser("noise-sweep", help="QQQ 全天日内动量策略检验")
    ws = sub.add_parser("wide-sweep", help="全市场强势股回调买入（Martin Luk 风格）检验")
    ws.add_argument("--slippage", type=float, default=WIDE_SLIPPAGE, help="每边滑点，默认 0.001")
    pp = sub.add_parser("paper", help="每日模拟交易更新：三连跌回调、Martin Luk 风格回调（美股收盘后运行）")
    pp.add_argument("--reset", action="store_true", help="清空模拟账户，重新开始")
    pp.add_argument("--start", help="模拟开始日期 YYYY-MM-DD（默认最近一个交易日）")
    pp.add_argument("--only", choices=[st.key for st in paper.STRATEGIES], help="只重置/重新开始这一个策略")
    nt = sub.add_parser("notify", help="发送 Telegram 推送（每日摘要）")
    nt.add_argument("--find-chat", action="store_true", help="设置时用：找出你和机器人的对话编号")
    nt.add_argument("--force", action="store_true", help="没有新的交易日也推送")
    nt.add_argument("--failed", metavar="说明", help="发送「自动更新失败」的提醒")
    args = p.parse_args()
    cfg = load_config()
    {"download": cmd_download, "backtest": cmd_backtest, "sweep": cmd_sweep,
     "swing-sweep": cmd_swing_sweep, "noise-sweep": cmd_noise_sweep,
     "wide-sweep": cmd_wide_sweep, "paper": cmd_paper, "notify": cmd_notify}[args.cmd](cfg, args)
