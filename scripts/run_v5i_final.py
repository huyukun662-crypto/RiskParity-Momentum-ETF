# -*- coding: utf-8 -*-
"""
v5i 最终版一键复现
参数：mc=0.9, vt=0.13, vt_win=20, basket=B3, n_mom=3, trailing_dd=-0.08
产出：IS / OOS / Full 三窗口的 nav / rebalances / switch / metrics / png
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import matplotlib.pyplot as plt
import pandas as pd

from strategy_a_share_etf_rotation import ETF_POOL, fetch_all
from strategy_v5e_capped import ParamsV5E, run_backtest_v5e

OUT = ROOT / "results"
OUT.mkdir(parents=True, exist_ok=True)

plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False


def run_and_save(start, end, tag, p, prices):
    r = run_backtest_v5e(prices, start, end, p)
    pd.DataFrame({"strategy": r.nav, "benchmark": r.benchmark_nav}) \
        .to_csv(OUT / f"nav_{tag}.csv", encoding="utf-8-sig")

    rows = []
    for rb in r.rebalances:
        for c, w in (rb["target"] or {"(空仓)": 0}).items():
            rows.append({"date": rb["date"], "kind": rb.get("kind"),
                         "ts_code": c, "name": ETF_POOL.get(c, c),
                         "weight": w, "nav": rb["nav"]})
    pd.DataFrame(rows).to_csv(OUT / f"rebalances_{tag}.csv",
                              index=False, encoding="utf-8-sig")
    pd.Series(r.metrics).to_csv(OUT / f"metrics_{tag}.csv",
                                header=["value"], encoding="utf-8-sig")

    if r.switch_log:
        sw = [{"date": s["date"],
               "hot_out": ",".join(s["hot_out"]),
               "new_target": ",".join(f"{k}:{v:.2f}" for k, v in s["new_target"].items())}
              for s in r.switch_log]
        pd.DataFrame(sw).to_csv(OUT / f"switch_{tag}.csv",
                                index=False, encoding="utf-8-sig")

    plt.figure(figsize=(12, 6))
    plt.plot(r.nav.index, r.nav.values, label="v5i_final 策略",
             color="#e377c2", linewidth=2)
    plt.plot(r.benchmark_nav.index, r.benchmark_nav.values,
             label="沪深300", linestyle="--", color="gray")
    plt.title(f"v5i_final (mc={p.max_commodity_weight} + vt={p.vol_target} + trail={p.trailing_dd}) — {tag}",
              fontsize=11)
    plt.xlabel("日期"); plt.ylabel("净值")
    plt.legend(); plt.grid(True, alpha=0.3); plt.tight_layout()
    plt.savefig(OUT / f"nav_{tag}.png", dpi=120); plt.close()

    n_w = sum(1 for x in r.rebalances if x["kind"] == "weekly")
    n_rg = sum(1 for x in r.rebalances if x["kind"] == "weekly_regime")
    n_tr = sum(1 for x in r.rebalances if x["kind"] == "weekly_trail")
    print(f"\n--- {tag}  {start} ~ {end} ---")
    for k, v in r.metrics.items():
        if isinstance(v, float):
            print(f"  {k:22s} {v:8.4f}")
    print(f"  周度调仓(进攻): {n_w}   Regime 防御: {n_rg}   Trail 防御: {n_tr}   日内高低切: {len(r.switch_log)}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max_cmd",    type=float, default=0.9)
    ap.add_argument("--vol_target", type=float, default=0.13)
    ap.add_argument("--vt_window",  type=int,   default=20)
    ap.add_argument("--trail_dd",   type=float, default=-0.08)
    ap.add_argument("--basket",     default="B3_diversified")
    ap.add_argument("--n_mom",      type=int,   default=3)
    args = ap.parse_args()

    prices = fetch_all("2015-06-01", "2026-04-21")

    p = ParamsV5E(
        n_momentum=args.n_mom,
        enable_weight_caps=False,
        max_category_weight=1.0,
        max_commodity_weight=args.max_cmd,
        enable_vol_targeting=True,
        vol_target=args.vol_target,
        vol_target_window=args.vt_window,
        defense_basket_name=args.basket,
        trailing_dd=args.trail_dd,
        trailing_recovery=0.03,
        trailing_max_days=20,
    )
    print(f"[v5i_final] max_cmd={args.max_cmd}  vt={args.vol_target}  vt_win={args.vt_window}  "
          f"trail={args.trail_dd}  basket={args.basket}  n_mom={args.n_mom}")
    print(f"           regime: lookback={p.regime_lookback}, threshold={p.regime_threshold}")

    run_and_save("2020-01-01", "2026-04-21", "v5i_final_full", p, prices)
    run_and_save("2018-01-01", "2023-12-31", "v5i_final_is", p, prices)
    run_and_save("2024-01-01", "2026-04-21", "v5i_final_oos", p, prices)

    print("\n全部保存完成 →", OUT)


if __name__ == "__main__":
    main()
