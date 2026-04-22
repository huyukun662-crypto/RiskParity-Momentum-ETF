# -*- coding: utf-8 -*-
"""
v5f IS 扫描：路径 1 (差异化商品上限 × vol targeting) + 路径 2 (细化 max_cat)
共 15 组。固定 basket=B3_diversified, regime=(120, -0.08), n_mom=3。
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

# 路径 1：max_commodity × vol_target
PATH1_COMMODITY = [0.5, 0.6, 0.7]
PATH1_VOL_TARGET = [0.15, 0.18, 0.22, None]   # None = off
# 路径 2：细化 max_cat
PATH2_MAX_CAT = [0.85, 0.9, 0.95]


def build_path1(max_cmd: float, vol_t) -> ParamsV5E:
    return ParamsV5E(
        enable_weight_caps=False,     # 路径 1 关闭总类别上限
        max_category_weight=1.0,
        max_commodity_weight=max_cmd,
        enable_vol_targeting=(vol_t is not None),
        vol_target=(vol_t or 0.18),
        vol_target_window=20,
        defense_basket_name="B3_diversified",
    )


def build_path2(max_cat: float) -> ParamsV5E:
    return ParamsV5E(
        enable_weight_caps=True,
        max_category_weight=max_cat,
        max_commodity_weight=1.0,     # 商品单独上限关闭
        enable_vol_targeting=False,
        defense_basket_name="B3_diversified",
    )


def metrics_row(label: dict, r) -> dict:
    return {**label, **r.metrics,
            "n_weekly":  sum(1 for x in r.rebalances if x["kind"] == "weekly"),
            "n_regime":  sum(1 for x in r.rebalances if x["kind"] == "weekly_regime"),
            "n_switch":  len(r.switch_log)}


def main():
    prices = fetch_all("2015-06-01", OOS_END)

    combos = []
    for mc, vt in itertools.product(PATH1_COMMODITY, PATH1_VOL_TARGET):
        combos.append(("path1", {"path": "1", "max_cmd": mc,
                                 "vol_target": (vt if vt is not None else "off"),
                                 "max_cat": "1.0"},
                       build_path1(mc, vt)))
    for mc_total in PATH2_MAX_CAT:
        combos.append(("path2", {"path": "2", "max_cmd": 1.0,
                                 "vol_target": "off",
                                 "max_cat": mc_total},
                       build_path2(mc_total)))

    print(f"v5f IS 扫描：{len(combos)} 组（路径 1 × {len(PATH1_COMMODITY)*len(PATH1_VOL_TARGET)} + 路径 2 × {len(PATH2_MAX_CAT)}）")

    is_rows = []
    for kind, label, p in tqdm(combos, desc="IS"):
        r = run_backtest_v5e(prices, IS_START, IS_END, p)
        is_rows.append(metrics_row(label, r))
    df_is = pd.DataFrame(is_rows)
    df_is.to_csv(RESULTS_DIR / "grid_search_v5f_is.csv", index=False, encoding="utf-8-sig")

    cols = ["path", "max_cmd", "vol_target", "max_cat",
            "annual_return", "sharpe", "max_drawdown", "calmar",
            "n_weekly", "n_regime", "n_switch"]
    print("\n=== IS Top 10 by Sharpe ===")
    print(df_is.sort_values("sharpe", ascending=False).head(10)[cols].to_string(index=False))
    print("\n=== IS Top 10 by Calmar ===")
    print(df_is.sort_values("calmar", ascending=False).head(10)[cols].to_string(index=False))
    ok = df_is[df_is["sharpe"] >= 1.60]
    print(f"\n=== IS MinDD (Sharpe >= 1.60, {len(ok)} 组) ===")
    if not ok.empty:
        print(ok.sort_values("max_drawdown", ascending=False).head(10)[cols].to_string(index=False))

    # 3 候选
    cands = [df_is.sort_values("sharpe", ascending=False).iloc[0],
             df_is.sort_values("calmar", ascending=False).iloc[0]]
    if not ok.empty:
        cands.append(ok.sort_values("max_drawdown", ascending=False).iloc[0])
    cand_df = pd.DataFrame(cands).drop_duplicates(
        subset=["path", "max_cmd", "vol_target", "max_cat"])

    print(f"\n=== OOS + Full 验证（候选 {len(cand_df)} 组）===")
    oos_rows, full_rows = [], []
    for _, c in cand_df.iterrows():
        if c.path == "1":
            vt = c.vol_target if c.vol_target != "off" else None
            p = build_path1(float(c.max_cmd), vt if vt is None else float(vt))
        else:
            p = build_path2(float(c.max_cat))
        r_oos = run_backtest_v5e(prices, OOS_START, OOS_END, p)
        r_full = run_backtest_v5e(prices, FULL_START, FULL_END, p)
        lbl = {k: c[k] for k in cols[:4]}
        oos_rows.append(metrics_row(lbl, r_oos))
        full_rows.append(metrics_row(lbl, r_full))

        tag = f"v5f_{c.path}_mc{c.max_cmd}_vt{c.vol_target}_cat{c.max_cat}"
        pd.DataFrame({"strategy": r_full.nav, "benchmark": r_full.benchmark_nav}) \
            .to_csv(RESULTS_DIR / f"nav_{tag}_full.csv", encoding="utf-8-sig")

        print(f"\n--- path={c.path} mc={c.max_cmd} vt={c.vol_target} cat={c.max_cat} ---")
        print(f"  IS   Sh={c.sharpe:.2f} Ann={c.annual_return:.2%} "
              f"DD={c.max_drawdown:.2%} Cal={c.calmar:.2f}")
        print(f"  OOS  Sh={r_oos.metrics['sharpe']:.2f} Ann={r_oos.metrics['annual_return']:.2%} "
              f"DD={r_oos.metrics['max_drawdown']:.2%} Cal={r_oos.metrics['calmar']:.2f}")
        print(f"  FULL Sh={r_full.metrics['sharpe']:.2f} Ann={r_full.metrics['annual_return']:.2%} "
              f"DD={r_full.metrics['max_drawdown']:.2%} Cal={r_full.metrics['calmar']:.2f}")

    pd.DataFrame(oos_rows).to_csv(RESULTS_DIR / "grid_search_v5f_oos.csv",
                                  index=False, encoding="utf-8-sig")
    pd.DataFrame(full_rows).to_csv(RESULTS_DIR / "grid_search_v5f_full.csv",
                                   index=False, encoding="utf-8-sig")
    print("\n保存完成。")


if __name__ == "__main__":
    main()
