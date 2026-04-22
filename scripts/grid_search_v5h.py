# -*- coding: utf-8 -*-
"""
v5h (R2): 固定 v5g 最优 (vt=0.13, mc=0.9, vt_win=20, basket=B3)，扫 n_momentum ∈ {3, 4, 5}
基线 v5g: Full Sh 2.11, Ann 26.67%, DD -7.97%, Cal 3.35
假设：n=4/5 更分散可能降 DD + 升 Sharpe
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

N_MOMS = [3, 4, 5]


def build(n_mom: int) -> ParamsV5E:
    return ParamsV5E(
        n_momentum=n_mom,
        enable_weight_caps=False,
        max_category_weight=1.0,
        max_commodity_weight=0.9,
        enable_vol_targeting=True,
        vol_target=0.13,
        vol_target_window=20,
        defense_basket_name="B3_diversified",
    )


def metrics_row(label: dict, r) -> dict:
    return {**label, **r.metrics,
            "n_weekly": sum(1 for x in r.rebalances if x["kind"] == "weekly"),
            "n_regime": sum(1 for x in r.rebalances if x["kind"] == "weekly_regime"),
            "n_switch": len(r.switch_log)}


def main():
    prices = fetch_all("2015-06-01", OOS_END)
    print(f"v5h IS 扫描：{len(N_MOMS)} 组 (n_mom ∈ {N_MOMS})")

    is_rows, oos_rows, full_rows = [], [], []
    for n in tqdm(N_MOMS, desc="scan"):
        p = build(n)
        r_is = run_backtest_v5e(prices, IS_START, IS_END, p)
        r_oos = run_backtest_v5e(prices, OOS_START, OOS_END, p)
        r_full = run_backtest_v5e(prices, FULL_START, FULL_END, p)
        is_rows.append(metrics_row({"n_mom": n}, r_is))
        oos_rows.append(metrics_row({"n_mom": n}, r_oos))
        full_rows.append(metrics_row({"n_mom": n}, r_full))

        tag = f"v5h_nmom{n}"
        pd.DataFrame({"strategy": r_full.nav, "benchmark": r_full.benchmark_nav}) \
            .to_csv(RESULTS_DIR / f"nav_{tag}_full.csv", encoding="utf-8-sig")

        print(f"\n--- n_mom={n} ---")
        print(f"  IS   Sh={r_is.metrics['sharpe']:.2f} Ann={r_is.metrics['annual_return']:.2%} "
              f"DD={r_is.metrics['max_drawdown']:.2%} Cal={r_is.metrics['calmar']:.2f}")
        print(f"  OOS  Sh={r_oos.metrics['sharpe']:.2f} Ann={r_oos.metrics['annual_return']:.2%} "
              f"DD={r_oos.metrics['max_drawdown']:.2%} Cal={r_oos.metrics['calmar']:.2f}")
        print(f"  FULL Sh={r_full.metrics['sharpe']:.2f} Ann={r_full.metrics['annual_return']:.2%} "
              f"DD={r_full.metrics['max_drawdown']:.2%} Cal={r_full.metrics['calmar']:.2f}")

    pd.DataFrame(is_rows).to_csv(RESULTS_DIR / "grid_search_v5h_is.csv",
                                 index=False, encoding="utf-8-sig")
    pd.DataFrame(oos_rows).to_csv(RESULTS_DIR / "grid_search_v5h_oos.csv",
                                  index=False, encoding="utf-8-sig")
    pd.DataFrame(full_rows).to_csv(RESULTS_DIR / "grid_search_v5h_full.csv",
                                   index=False, encoding="utf-8-sig")
    print("\n保存完成。基线 v5g (n_mom=3): Full Sh 2.11, Ann 26.67%, DD -7.97%, Cal 3.35")


if __name__ == "__main__":
    main()
