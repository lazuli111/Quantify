import pandas as pd

from .backtest import Result


def summarize(r: Result) -> dict:
    t = r.trades
    n = len(t)
    wins, losses = (t[t["pnl"] > 0], t[t["pnl"] <= 0]) if n else (t, t)
    eq = pd.concat([pd.Series([r.start_capital]), t["equity_after"]]) if n else pd.Series([r.start_capital])
    return {
        "trades": n,
        "win_rate": len(wins) / n if n else 0.0,
        "avg_ret_pct": t["ret_pct"].mean() if n else 0.0,
        "avg_gross_pct": t["gross_pct"].mean() if n else 0.0,
        "avg_win": wins["pnl"].mean() if len(wins) else 0.0,
        "avg_loss": losses["pnl"].mean() if len(losses) else 0.0,
        "total_pnl": r.end_equity - r.start_capital,
        "total_ret": r.end_equity / r.start_capital - 1,
        "max_dd": (eq / eq.cummax() - 1).min(),
        "fees": t["fees"].sum() if n else 0.0,
    }


def print_report(r: Result, bench: float | None, title: str = "回测结果") -> None:
    m = summarize(r)
    t = r.trades
    print(f"\n===== {title}：{r.first_date} → {r.last_date} =====")
    print(f"候选信号 {r.n_candidates} 个，实际交易 {m['trades']} 笔")
    if r.skips:
        print("跳过原因：" + "，".join(f"{k} {v}" for k, v in r.skips.most_common()))
    print(f"胜率            {m['win_rate']:.0%}")
    print(f"平均每笔收益    {m['avg_ret_pct']:+.3f}%（扣费前、含滑点 {m['avg_gross_pct']:+.3f}%）")
    print(f"平均盈利/亏损   ${m['avg_win']:.2f} / ${m['avg_loss']:.2f}")
    print(f"总盈亏          ${m['total_pnl']:+.2f}（{m['total_ret']:+.1%}）")
    print(f"最大回撤        {m['max_dd']:.1%}")
    print(f"费用合计        ${m['fees']:.2f}（其中月费 ${r.monthly_fees:.2f}）")
    if bench is not None:
        print(f"同期持有 QQQ    {bench:+.1%}")
    if len(t):
        print("退出方式：" + "，".join(f"{k} {v}" for k, v in t["reason"].value_counts().items()))


def print_yearly(r: Result) -> None:
    t = r.trades
    if t.empty:
        return
    print("\n按年份：")
    print("  年份  笔数  胜率  平均每笔   盈亏")
    for year, g in t.groupby(pd.to_datetime(t["date"]).dt.year):
        print(f"  {year}  {len(g):4d}  {(g['pnl'] > 0).mean():4.0%}  {g['ret_pct'].mean():+7.3f}%  ${g['pnl'].sum():+8.2f}")


def print_recent(r: Result, n: int = 10) -> None:
    t = r.trades
    if t.empty:
        return
    print(f"\n最近 {n} 笔：")
    cols = ["date", "symbol", "entry_time", "entry", "stop", "target", "exit_time", "exit", "reason", "shares", "pnl"]
    print(t[cols].tail(n).to_string(index=False))
