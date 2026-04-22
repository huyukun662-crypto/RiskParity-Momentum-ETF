# -*- coding: utf-8 -*-
"""
v5i (R3): 在 v5g 最优配置基础上叠加 Trailing Stop
固定：mc=0.9, vt=0.13, vt_win=20, basket=B3, n_mom=3
扫描：trailing_dd ∈ {off, -0.06, -0.08, -0.10}  # 4 组

**严格无前视**：trailing 检查基于 nav_series.loc[:today]（含今日），
peak 用 .max() 计算，action 在当日收盘后决定、次日执行（与周度调仓同步）。

基线 v5g (trailing=off): Full Sh 2.11, Ann 26.67%, DD -7.97%, Cal 3.35
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
from tqdm import tqdm

from strategy_a_share_etf_rotation import fetch_all, RESULTS_DIR
from strategy_v5e_capped import ParamsV5E, run_backtest_v5e

IS_START, IS_END = "2018-01-01", "2023-12-31"
OOS_START, OOS_END = "2024-01-01", "2026-04-21"
FULL_START, FULL_END = "2020-01-01", "2026-04-21"

TRAILING_DDS = [0.0, -0.06, -0.08, -0.10]   # 0.0 = off


def build(trail_dd: float) -> ParamsV5E:
    return ParamsV5E(
        n_momentum=3,
        enable_weight_caps=False,
        max_category_weight=1.0,
        max_commodity_weight=0.9,
        enable_vol_targeting=True,
        vol_target=0.13,
        vol_target_window=20,
        defense_basket_name="B3_diversified",
        trailing_dd=trail_dd,
        trailing_recovery=0.03,
        trailing_max_days=20,
    )


def metrics_row(label: dict, r) -> dict:
    return {**label, **r.metrics,
            "n_weekly":  sum(1 for x in r.rebalances if x["kind"] == "weekly"),
            "n_regime":  sum(1 for x in r.rebalances if x["kind"] == "weekly_regime"),
            "n_trail":   sum(1 for x in r.rebalances if x["kind"] == "weekly_trail"),
            "n_switch":  len(r.switch_log)}


def main():
    prices = fetch_all("2015-06-01", OOS_END)
    print(f"v5i IS 扫描：{len(TRAILING_DDS)} 组 (trailing_dd ∈ {TRAILING_DDS})")

    is_rows, oos_rows, full_rows = [], [], []
    for td in tqdm(TRAILING_DDS, desc="scan"):
        p = build(td)
        r_is = run_backtest_v5e(prices, IS_START, IS_END, p)
        r_oos = run_backtest_v5e(prices, OOS_START, OOS_END, p)
        r_full = run_backtest_v5e(prices, FULL_START, FULL_END, p)
        lbl = {"trail_dd": td}
        is_rows.append(metrics_row(lbl, r_is))
        oos_rows.append(metrics_row(lbl, r_oos))
        full_rows.append(metrics_row(lbl, r_full))

        tag = f"v5i_trail{td}"
        pd.DataFrame({"strategy": r_full.nav, "benchmark": r_full.benchmark_nav}) \
            .to_csv(RESULTS_DIR / f"nav_{tag}_full.csv", encoding="utf-8-sig")

        n_trail_full = sum(1 for x in r_full.rebalances if x["kind"] == "weekly_trail")
        print(f"\n--- trail_dd={td} ---")
        print(f"  IS   Sh={r_is.metrics['sharpe']:.2f} Ann={r_is.metrics['annual_return']:.2%} "
              f"DD={r_is.metrics['max_drawdown']:.2%} Cal={r_is.metrics['calmar']:.2f}")
        print(f"  OOS  Sh={r_oos.metrics['sharpe']:.2f} Ann={r_oos.metrics['annual_return']:.2%} "
              f"DD={r_oos.metrics['max_drawdown']:.2%} Cal={r_oos.metrics['calmar']:.2f}")
        print(f"  FULL Sh={r_full.metrics['sharpe']:.2f} Ann={r_full.metrics['annual_return']:.2%} "
              f"DD={r_full.metrics['max_drawdown']:.2%} Cal={r_full.metrics['calmar']:.2f}  "
              f"trail_trigs={n_trail_full}")

    pd.DataFrame(is_rows).to_csv(RESULTS_DIR / "grid_search_v5i_is.csv",
                                 index=False, encoding="utf-8-sig")
    pd.DataFrame(oos_rows).to_csv(RESULTS_DIR / "grid_search_v5i_oos.csv",
                                  index=False, encoding="utf-8-sig")
    pd.DataFrame(full_rows).to_csv(RESULTS_DIR / "grid_search_v5i_full.csv",
                                   index=False, encoding="utf-8-sig")
    print("\n保存完成。基线 v5g (trail=off): Full Sh 2.11, Ann 26.67%, DD -7.97%, Cal 3.35")


if __name__ == "__main__":
    main()
