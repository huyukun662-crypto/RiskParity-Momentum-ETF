# -*- coding: utf-8 -*-
"""
v5d: 放回跨境 QDII 的终极激进版
EXCLUDE 清空 → 64 只全池 + regime 防御滤波
IS 扫 lookback × threshold × defensive × n_momentum
"""
from __future__ import annotations

import sys
import itertools
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
from tqdm import tqdm

from strategy_a_share_etf_rotation import fetch_all, RESULTS_DIR, ETF_POOL
import strategy_v5_aggressive as v5
from strategy_v5_aggressive import ParamsV5, run_backtest_v5

# 临时清空 EXCLUDE_V5，让全池可用
v5.EXCLUDE_V5 = set()
v5.ETF_POOL_V5 = dict(ETF_POOL)

GRID = {
    "lookback":  [60, 90, 120],
    "threshold": [-0.03, -0.05, -0.08],
    "defensive": ["518880.SH", "511090.SH"],
    "n_mom":     [2, 3],
}


def build(lb, th, defn, n_mom):
    return ParamsV5(
        n_momentum=n_mom, momentum_window=20,
        vol_high=0.28, vol_low=0.08, n_corr=5,
        weighting="inv_vol",
        rsi_overheat=78.0, logbias_overheat_mult=1.0,
        enable_high_low_switch=True,
        enable_regime_filter=True,
        regime_lookback=lb, regime_threshold=th,
        regime_defensive_etf=defn,
    )


def run_one(lb, th, defn, n_mom, start, end, prices):
    r = run_backtest_v5(prices, start, end, build(lb, th, defn, n_mom))
    row = {
        "lookback": lb, "threshold": th, "defensive": defn, "n_mom": n_mom,
        **r.metrics,
        "n_weekly": sum(1 for x in r.rebalances if x["kind"] == "weekly"),
        "n_regime": sum(1 for x in r.rebalances if x["kind"] == "weekly_regime"),
    }
    return row, r


def main():
    prices = fetch_all("2015-06-01", "2026-04-21")
    combos = list(itertools.product(GRID["lookback"], GRID["threshold"],
                                    GRID["defensive"], GRID["n_mom"]))
    print(f"IS 扫描：{len(combos)} 组 (全池 64 只)")
    is_rows = []
    for c in tqdm(combos, desc="IS"):
        row, _ = run_one(*c, "2018-01-01", "2023-12-31", prices)
        is_rows.append(row)
    df = pd.DataFrame(is_rows)
    df.to_csv(RESULTS_DIR / "grid_search_v5d_is.csv", index=False, encoding="utf-8-sig")
    cols = ["lookback", "threshold", "defensive", "n_mom",
            "annual_return", "sharpe", "max_drawdown", "calmar", "n_regime"]
    print("\n=== IS Top 10 by Calmar ===")
    print(df.sort_values("calmar", ascending=False).head(10)[cols].to_string(index=False))
    print("\n=== IS Top 10 by Sharpe ===")
    print(df.sort_values("sharpe", ascending=False).head(10)[cols].to_string(index=False))

    cand = pd.concat([
        df.sort_values("calmar", ascending=False).head(3),
        df.sort_values("sharpe", ascending=False).head(2),
    ]).drop_duplicates(subset=["lookback", "threshold", "defensive", "n_mom"])

    print(f"\n=== OOS + Full 验证 ===")
    for _, c in cand.iterrows():
        lb, th, defn, nm = int(c.lookback), float(c.threshold), c.defensive, int(c.n_mom)
        oos, _ = run_one(lb, th, defn, nm, "2024-01-01", "2026-04-21", prices)
        full, res = run_one(lb, th, defn, nm, "2020-01-01", "2026-04-21", prices)
        tag = f"v5d_lb{lb}_th{th:+.2f}_{defn.split('.')[0]}_n{nm}"
        pd.DataFrame({"strategy": res.nav, "benchmark": res.benchmark_nav}) \
            .to_csv(RESULTS_DIR / f"nav_{tag}_full.csv", encoding="utf-8-sig")
        print(f"\n--- lb={lb}, th={th}, def={defn}, n={nm} ---")
        print(f"  IS   Sharpe={c.sharpe:.2f} Ann={c.annual_return:.2%} DD={c.max_drawdown:.2%} Calmar={c.calmar:.2f}")
        print(f"  OOS  Sharpe={oos['sharpe']:.2f} Ann={oos['annual_return']:.2%} DD={oos['max_drawdown']:.2%} Calmar={oos['calmar']:.2f}")
        print(f"  FULL Sharpe={full['sharpe']:.2f} Ann={full['annual_return']:.2%} DD={full['max_drawdown']:.2%} Calmar={full['calmar']:.2f}")


if __name__ == "__main__":
    main()
