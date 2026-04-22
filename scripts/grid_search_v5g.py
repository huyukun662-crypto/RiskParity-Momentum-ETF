# -*- coding: utf-8 -*-
"""
v5g IS 扫描（R1 精细化 vt × mc × vt_window）
基于 v5f 最佳 (path=1, mc=0.7, vt=0.15, vt_win=20) 四周围扫描：
  vt        ∈ {0.13, 0.15, 0.17}
  mc        ∈ {0.7, 0.8, 0.9}
  vt_window ∈ {20, 30}
合计 3 × 3 × 2 = 18 组

基线 v5f: Full Sh 1.88, Ann 24.35%, DD -8.92%, Cal 2.73
目标：Sh ≥ 1.95 且 DD ≤ -9% 且 Ann ≥ 25%
"""
from __future__ import annotations

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import itertools
import pandas as pd
from tqdm import tqdm

from strategy_a_share_etf_rotation import fetch_all, RESULTS_DIR
from strategy_v5e_capped import ParamsV5E, run_backtest_v5e

IS_START, IS_END = "2018-01-01", "2023-12-31"
OOS_START, OOS_END = "2024-01-01", "2026-04-21"
FULL_START, FULL_END = "2020-01-01", "2026-04-21"

GRID = {
    "vt":        [0.13, 0.15, 0.17],
    "mc":        [0.7, 0.8, 0.9],
    "vt_window": [20, 30],
}


def build(vt: float, mc: float, vt_win: int) -> ParamsV5E:
    return ParamsV5E(
        enable_weight_caps=False,
        max_category_weight=1.0,
        max_commodity_weight=mc,
        enable_vol_targeting=True,
        vol_target=vt,
        vol_target_window=vt_win,
        defense_basket_name="B3_diversified",
    )


def metrics_row(label: dict, r) -> dict:
    return {**label, **r.metrics,
            "n_weekly": sum(1 for x in r.rebalances if x["kind"] == "weekly"),
            "n_regime": sum(1 for x in r.rebalances if x["kind"] == "weekly_regime"),
            "n_switch": len(r.switch_log)}


def main():
    prices = fetch_all("2015-06-01", OOS_END)
    combos = list(itertools.product(GRID["vt"], GRID["mc"], GRID["vt_window"]))
    print(f"v5g IS 扫描：{len(combos)} 组")

    is_rows = []
    for vt, mc, vw in tqdm(combos, desc="IS"):
        r = run_backtest_v5e(prices, IS_START, IS_END, build(vt, mc, vw))
        is_rows.append(metrics_row({"vt": vt, "mc": mc, "vt_window": vw}, r))
    df = pd.DataFrame(is_rows)
    df.to_csv(RESULTS_DIR / "grid_search_v5g_is.csv", index=False, encoding="utf-8-sig")

    cols = ["vt", "mc", "vt_window",
            "annual_return", "sharpe", "max_drawdown", "calmar",
            "n_weekly", "n_regime", "n_switch"]
    print("\n=== IS Top 10 by Sharpe ===")
    print(df.sort_values("sharpe", ascending=False).head(10)[cols].to_string(index=False))
    print("\n=== IS Top 10 by Calmar ===")
    print(df.sort_values("calmar", ascending=False).head(10)[cols].to_string(index=False))

    # 取 Sharpe Top 1 / Calmar Top 1 / MinDD 候选
    cands = [df.sort_values("sharpe", ascending=False).iloc[0],
             df.sort_values("calmar", ascending=False).iloc[0]]
    ok = df[df["sharpe"] >= 1.60]
    if not ok.empty:
        cands.append(ok.sort_values("max_drawdown", ascending=False).iloc[0])
    cand_df = pd.DataFrame(cands).drop_duplicates(subset=["vt", "mc", "vt_window"])

    print(f"\n=== OOS + Full 验证（候选 {len(cand_df)} 组）===")
    oos_rows, full_rows = [], []
    for _, c in cand_df.iterrows():
        p = build(float(c.vt), float(c.mc), int(c.vt_window))
        r_oos = run_backtest_v5e(prices, OOS_START, OOS_END, p)
        r_full = run_backtest_v5e(prices, FULL_START, FULL_END, p)
        lbl = {"vt": c.vt, "mc": c.mc, "vt_window": c.vt_window}
        oos_rows.append(metrics_row(lbl, r_oos))
        full_rows.append(metrics_row(lbl, r_full))

        tag = f"v5g_vt{c.vt}_mc{c.mc}_w{c.vt_window}"
        pd.DataFrame({"strategy": r_full.nav, "benchmark": r_full.benchmark_nav}) \
            .to_csv(RESULTS_DIR / f"nav_{tag}_full.csv", encoding="utf-8-sig")

        print(f"\n--- vt={c.vt} mc={c.mc} vt_win={c.vt_window} ---")
        print(f"  IS   Sh={c.sharpe:.2f} Ann={c.annual_return:.2%} "
              f"DD={c.max_drawdown:.2%} Cal={c.calmar:.2f}")
        print(f"  OOS  Sh={r_oos.metrics['sharpe']:.2f} Ann={r_oos.metrics['annual_return']:.2%} "
              f"DD={r_oos.metrics['max_drawdown']:.2%} Cal={r_oos.metrics['calmar']:.2f}")
        print(f"  FULL Sh={r_full.metrics['sharpe']:.2f} Ann={r_full.metrics['annual_return']:.2%} "
              f"DD={r_full.metrics['max_drawdown']:.2%} Cal={r_full.metrics['calmar']:.2f}")

    pd.DataFrame(oos_rows).to_csv(RESULTS_DIR / "grid_search_v5g_oos.csv",
                                  index=False, encoding="utf-8-sig")
    pd.DataFrame(full_rows).to_csv(RESULTS_DIR / "grid_search_v5g_full.csv",
                                   index=False, encoding="utf-8-sig")
    print("\n保存完成。")


if __name__ == "__main__":
    main()
