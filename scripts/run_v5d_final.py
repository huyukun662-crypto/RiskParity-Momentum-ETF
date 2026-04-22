# -*- coding: utf-8 -*-
"""
v5d 最终版一键复现脚本
参数：lookback=120, threshold=-0.08, n_momentum=3, defensive=黄金(518880)
池子：64 只全池（含跨境 QDII），IS 严格选参
输出：deliverables_v5d/results/ 下 nav / rebalances / metrics / switch / 净值图
"""
from __future__ import annotations
import sys
from pathlib import Path

# 让脚本可以从 scripts/ 目录直接运行，自动找到 src/ 下的策略模块
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import matplotlib.pyplot as plt
import pandas as pd

from strategy_a_share_etf_rotation import fetch_all, ETF_POOL
import strategy_v5_aggressive as v5
from strategy_v5_aggressive import ParamsV5, run_backtest_v5

# 放开全池（v5_aggressive 原本排除了跨境，这里恢复全池）
v5.EXCLUDE_V5 = set()
v5.ETF_POOL_V5 = dict(ETF_POOL)

OUT = ROOT / "results"
OUT.mkdir(exist_ok=True)

plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False


def run_and_save(start, end, tag, p):
    prices = fetch_all("2015-06-01", end)
    r = run_backtest_v5(prices, start, end, p)

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
    plt.plot(r.nav.index, r.nav.values, label="v5d_final 策略", color="#d62728", linewidth=2)
    plt.plot(r.benchmark_nav.index, r.benchmark_nav.values,
             label="沪深300", linestyle="--", color="gray")
    plt.title(f"v5d_final (全池 + 120日择时 + 黄金防御) — {tag}", fontsize=13)
    plt.xlabel("日期"); plt.ylabel("净值")
    plt.legend(); plt.grid(True, alpha=0.3); plt.tight_layout()
    plt.savefig(OUT / f"nav_{tag}.png", dpi=120); plt.close()

    n_w = sum(1 for x in r.rebalances if x["kind"] == "weekly")
    n_rg = sum(1 for x in r.rebalances if x["kind"] == "weekly_regime")
    print(f"\n--- {tag}  {start} ~ {end} ---")
    for k, v in r.metrics.items():
        if isinstance(v, float):
            print(f"  {k:22s} {v:8.4f}")
    print(f"  周度调仓(进攻): {n_w}   防御触发: {n_rg}   日内高低切: {len(r.switch_log)}")
    return r


if __name__ == "__main__":
    p = ParamsV5(
        n_momentum=3,
        momentum_window=20,
        vol_low=0.08, vol_high=0.28, n_corr=5,
        weighting="inv_vol",
        rsi_overheat=78.0, logbias_overheat_mult=1.0,
        enable_high_low_switch=True,
        enable_regime_filter=True,
        regime_lookback=120,
        regime_threshold=-0.08,
        regime_defensive_etf="518880.SH",
    )
    print("[v5d_final] 参数：lookback=120, threshold=-0.08, n=3, 黄金防御")
    print(f"           池子：{len(v5.ETF_POOL_V5)} 只（64 只全池）")

    # Full 窗口
    run_and_save("2020-01-01", "2026-04-21", "v5d_final_full", p)
    # IS 段验证
    run_and_save("2018-01-01", "2023-12-31", "v5d_final_is", p)
    # OOS 段只读验证
    run_and_save("2024-01-01", "2026-04-21", "v5d_final_oos", p)

    print("\n全部保存完成 →", OUT)
