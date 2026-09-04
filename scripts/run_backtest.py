"""基于 tweets.db 的真实历史回测。

对每条可用周事件，沿其真实 history_pricing 时间线复现生产策略逻辑并结算，
统计：覆盖 / 触发 / 胜率 / PnL / 最大回撤 / 跳过原因分布。

用法：
    python scripts/run_backtest.py [--db tweets.db] [--pricer SimpleMonteCarlo]
        [--out results/backtest.json]

输出：
    - 控制台汇总
    - results/backtest.json（覆盖统计 + 每事件明细 + 汇总指标）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

sys_path = str(Path(__file__).resolve().parent.parent)
import sys  # noqa: E402

if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from polymarket_arb.backtest.engine import BacktestEngine  # noqa: E402
from polymarket_arb.backtest.loader import (  # noqa: E402
    DEFAULT_PRICER,
    WEEKLY_PREFIX,
    load_dataset,
)
from polymarket_arb.config import AppConfig  # noqa: E402


def market_coverage(db: str, pricer: str) -> list[dict]:
    """列出所有周事件及其可用性（元数据 / 定价行数）。"""
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT slug, event_json FROM events WHERE slug LIKE ? ORDER BY slug",
        (WEEKLY_PREFIX,),
    ).fetchall()
    out = []
    for slug, raw in rows:
        meta_ok = False
        tc = None
        try:
            jdata = json.loads(raw)
            tc = jdata.get("tweetCount")
            meta_ok = bool(tc and (jdata.get("startTime") or jdata.get("startDate")) and jdata.get("endDate"))
        except Exception:
            meta_ok = False
        n_rows = con.execute(
            "SELECT COUNT(*) FROM history_pricing WHERE event_slug=? AND pricer_name=?",
            (slug, pricer),
        ).fetchone()[0]
        n_ts = con.execute(
            "SELECT COUNT(DISTINCT timestamp) FROM history_pricing WHERE event_slug=? AND pricer_name=?",
            (slug, pricer),
        ).fetchone()[0]
        if not meta_ok:
            reason = "缺结算数/时间元数据"
        elif n_rows == 0:
            reason = "无该 pricer 定价行"
        else:
            reason = "OK"
        out.append({
            "slug": slug, "tweet_count": tc, "pricing_rows": n_rows,
            "pricing_timestamps": n_ts, "meta_ok": meta_ok, "eligible": reason == "OK",
            "reason": reason,
        })
    con.close()
    return out


def collect_skips(results) -> Counter:
    c = Counter()
    for r in results:
        c.update(r.skipped_reasons)
    return c


def sweep(db: str, pricer: str, cost_c: float, sweeps: list[tuple],
          slug_scope: list[str] | None = None, poll_sec: float = 60.0) -> list[dict]:
    """轻量参数灵敏度扫描：只探关键门/概率参数，不做遍历式强拟合。

    sweeps: [(参数路径, [取值])]，例如 [("p_min",[0.6,0.65,0.7])]
    """
    from itertools import product
    from polymarket_arb.backtest.engine import BacktestEngine

    ds = load_dataset(db, pricer, resample_sec=poll_sec)
    evs = [e for e in ds.events if not slug_scope or e.slug in slug_scope]
    keys = [k for k, _ in sweeps]
    grid = []
    for combo in product(*[vals for _, vals in sweeps]):
        cfg = AppConfig(env="backtest", dry_run=True)
        for k, v in zip(keys, combo):
            cfg.prob.__setattr__(k, v) if k in vars(cfg.prob) else \
                cfg.gate.__setattr__(k, v) if k in vars(cfg.gate) else \
                cfg.burst.__setattr__(k, v) if k in vars(cfg.burst) else \
                cfg.legs.__setattr__(k, v) if k in vars(cfg.legs) else \
                cfg.sizing.__setattr__(k, v)
        engine = BacktestEngine(cfg, trade_cost_c=cost_c)
        trades = [r for ev in evs for r in engine.run_event(ev, ds).trades]
        pnls = [t.pnl_usd for t in trades]
        n = len(trades)
        grid.append({
            **dict(zip(keys, combo)),
            "n_trades": n,
            "win_rate": round(sum(1 for t in trades if t.win) / n, 3) if n else None,
            "pnl_usd": round(sum(pnls), 2),
            "avg_pnl_usd": round(sum(pnls) / n, 2) if n else None,
        })
    return grid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tweets.db")
    ap.add_argument("--pricer", default=DEFAULT_PRICER)
    ap.add_argument("--out", default="results/backtest.json")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--cost-c", type=float, default=1.0)
    ap.add_argument("--sweep", action="store_true",
                    help="跑关键参数灵敏度扫描（calibration 用）")
    ap.add_argument("--poll-sec", type=float, default=60.0,
                    help="盘中轮询/重采样步长秒（默认 60=1 分钟）")
    ap.add_argument("--slugs", default="",
                    help="只回测指定事件（逗号分隔完整 slug 或其后缀，空格可留）；默认全部可用")
    args = ap.parse_args()

    slug_scope: list[str] | None = None
    if args.slugs.strip():
        slug_scope = [s.strip() for s in args.slugs.split(",") if s.strip()]

    db = str(Path(args.db).resolve())
    coverage = market_coverage(db, args.pricer)
    eligible = [c for c in coverage if c["eligible"]]
    ineligible = [c for c in coverage if not c["eligible"]]

    print("=" * 72)
    print(f"数据覆盖：共 {len(coverage)} 个周事件 | 可用 {len(eligible)} | "
          f"不可用 {len(ineligible)}")
    for c in ineligible:
        print(f"  排除 {c['slug'].replace('elon-musk-of-tweets-', '')}: "
              f"{c['reason']} (rows={c['pricing_rows']})")

    # 运行回测
    cfg = AppConfig(env="backtest", dry_run=True)
    engine = BacktestEngine(cfg, trade_cost_c=args.cost_c)
    dataset = load_dataset(db, args.pricer, resample_sec=args.poll_sec)
    by_slug = {e.slug: e for e in dataset.events}
    order = [c["slug"] for c in eligible
             if c["slug"] in by_slug and (not slug_scope
                 or any(sfn in c["slug"] for sfn in slug_scope))]

    results, trades_all = [], []
    per_event = []
    for slug in order:
        ev = by_slug[slug]
        res = engine.run_event(ev, dataset)
        results.append(res)
        per_event.append({
            "slug": slug,
            "n_steps": ev.n_steps(),
            "n_trades": res.n_trades,
            "skipped": res.skipped_reasons,
            "summary": res.summary(),
        })
        trades_all.extend(res.trades)

    agg = {
        "n_trades": len(trades_all),
        "win_rate": None,
        "pnl_usd": None,
        "avg_pnl_usd": None,
        "max_drawdown_usd": None,
        "total_fee_usd": None,
    }
    if trades_all:
        wins = sum(1 for t in trades_all if t.win)
        pnls = [t.pnl_usd for t in trades_all]
        cum = peak = 0.0
        max_dd = 0.0
        for pnl in pnls:
            cum += pnl
            peak = max(peak, cum)
            max_dd = min(max_dd, cum - peak)
        agg = {
            "n_trades": len(trades_all),
            "win_rate": round(wins / len(trades_all), 4),
            "pnl_usd": round(sum(pnls), 2),
            "avg_pnl_usd": round(sum(pnls) / len(pnls), 2),
            "max_drawdown_usd": round(max_dd, 2),
            "total_fee_usd": round(sum(t.fee_usd for t in trades_all), 2),
            "avg_x_c": round(sum(t.x_c for t in trades_all) / len(trades_all), 2),
            "avg_p": round(sum(t.p for t in trades_all) / len(trades_all), 3),
            "avg_hours_to_end": round(
                sum(t.hours_to_end for t in trades_all) / len(trades_all), 2),
        }

    skip_dist = dict(collect_skips(results).most_common())
    payload = {
        "args": {"db": db, "pricer": args.pricer, "equity": args.equity,
                 "cost_c": args.cost_c, "poll_sec": args.poll_sec,
                 "slug_scope": slug_scope, "config": "backtest"},
        "config": {
            "prob": cfg.prob.asdict(),
            "burst": cfg.burst.__dict__,
            "gate": cfg.gate.__dict__,
            "legs": cfg.legs.__dict__,
            "sizing": cfg.sizing.__dict__,
        },
        "coverage": {"total": len(coverage), "eligible": len(eligible),
                     "ineligible": len(ineligible), "list": coverage},
        "skip_reasons": skip_dist,
        "aggregate": agg,
        "per_event": per_event,
        "trades": [{
            "event": t.event_slug,
            "entry_ts": round(t.entry_time_ts, 1),
            "x_c": t.x_c, "p": t.p,
            "hours_to_end": t.hours_to_end,
            "hot_bucket": t.hot_bucket,
            "hot_won": t.hot_won,
            "exit": t.exit,
            "budget_usd": round(t.budget_usd, 2),
            "pnl_usd": round(t.pnl_usd, 2),
            "fee_usd": round(t.fee_usd, 4),
            "win": t.win,
            "legs": [l.as_dict() for l in t.legs],
        } for t in trades_all],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    print("=" * 72)
    print("跳过原因分布（覆盖不可入场的样本点）:")
    for reason, cnt in skip_dist.items():
        print(f"  {reason:<20} {cnt}")
    print("=" * 72)
    print("汇总指标:", json.dumps(agg, ensure_ascii=False))
    print("结果已写入:", out_path)

    if args.sweep:
        sweeps = [
            ("p_min", [0.60, 0.65, 0.70, 0.75]),
            ("delta", [4, 6, 8, 10]),
        ]
        print("=" * 72)
        print("关键参数灵敏度扫描 (p_min × delta):")
        res = sweep(db, args.pricer, args.cost_c, sweeps,
                    slug_scope=slug_scope, poll_sec=args.poll_sec)
        payload["sweep"] = res
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"  {'p_min':<6}{'delta':<7}{'trades':<8}{'win_rate':<10}{'pnl_usd':<12}{'avg_pnl':<10}")
        for r in res:
            print(f"  {r['p_min']:<6}{r['delta']:<7}{r['n_trades']:<8}"
                  f"{str(r['win_rate']):<10}{r['pnl_usd']!s:<12}{r['avg_pnl_usd']!s:<10}")


if __name__ == "__main__":
    main()