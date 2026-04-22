# -*- coding: utf-8 -*-
"""
A 股 ETF 轮动策略 v5：激进版（A 股 + 商品）

相对 v2 的改动：
  1. 池子剔除跨境 QDII（纳指/标普/恒生/恒生科技/中概）与全部债券 ETF
  2. n_momentum 3 → 2（集中火力）
  3. momentum_window 20 → 10（响应更快）
  4. vol_high 0.28 → 0.35（放开高波商品）
  5. 权重：动量加权（w ∝ score）替代 inv-vol，进一步偏向最强趋势
  6. 无单标/类别上限（allow 100% 单仓）
  7. RSI 过热阈值 78 → 82，logbias 阈值 ×1.3（减少频繁切出）

仅供研究参考，不构成投资建议。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from strategy_a_share_etf_rotation import (
    BENCHMARK, ETF_POOL, RESULTS_DIR, fetch_all,
    annual_volatility, compute_metrics,
    mean_abs_correlation, momentum_score,
)
from strategy_v2_high_low_switch import (
    CATEGORY, OVERHEAT, compute_logbias_rsi, is_overheat,
)

# ----------------- v5 池子：剔除跨境 + 债券 ----------------- #
EXCLUDE_V5 = {
    # 跨境
    "513100.SH", "513500.SH", "159920.SZ", "513180.SH", "513050.SH",
    # 债券（激进策略不做防御）
    "511010.SH", "511260.SH", "511090.SH", "511030.SH",
    "511360.SH", "511380.SH", "159396.SZ",
}
ETF_POOL_V5 = {k: v for k, v in ETF_POOL.items() if k not in EXCLUDE_V5}


@dataclass
class ParamsV5:
    init_cash: float = 1_000_000
    vol_window: int = 180
    corr_window: int = 500
    momentum_window: int = 10          # 原 20
    vol_low: float = 0.08
    vol_high: float = 0.35             # 原 0.28，放开高波商品
    n_corr: int = 5
    n_momentum: int = 2                # 原 3，集中
    rebalance_freq: str = "W-FRI"
    transaction_cost: float = 0.0005
    min_hist_days: int = 90
    ema_window: int = 30
    rsi_window: int = 14
    logbias_overheat_mult: float = 1.3   # 放宽
    rsi_overheat: float = 82.0           # 放宽
    enable_high_low_switch: bool = True
    weighting: str = "momentum"          # "momentum" 或 "inv_vol"
    # —— 大盘择时滤波 ——
    enable_regime_filter: bool = False
    regime_lookback: int = 60            # 沪深300 N 日收益
    regime_threshold: float = -0.05      # <此值触发"防御"
    regime_defensive_etf: str = "518880.SH"   # 防御时全仓黄金


@dataclass
class BTResult:
    nav: pd.Series
    benchmark_nav: pd.Series
    rebalances: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    switch_log: list = field(default_factory=list)


def momentum_weights(scores: pd.Series) -> pd.Series:
    """动量加权：w_i ∝ score_i（只对正 score 加权）。"""
    s = scores.clip(lower=0)
    tot = s.sum()
    if tot <= 0:
        # 退化：全部等权
        return pd.Series(1.0 / len(scores), index=scores.index)
    return s / tot


def run_backtest_v5(prices: pd.DataFrame, start: str, end: str, p: ParamsV5) -> BTResult:
    # 只用 v5 池
    keep = [c for c in prices.columns if c in ETF_POOL_V5 or c == BENCHMARK]
    prices = prices[keep].loc[start:end].copy()
    if BENCHMARK not in prices.columns:
        raise ValueError(f"基准 {BENCHMARK} 缺失")
    etf_cols = [c for c in prices.columns if c != BENCHMARK]
    etf_prices = prices[etf_cols]
    returns = np.log(etf_prices / etf_prices.shift(1))

    logbias, rsi, rsi_diff = compute_logbias_rsi(etf_prices, p)

    cal = prices.index
    weekly_dates = set(prices.resample(p.rebalance_freq).last().index.intersection(cal))

    cash = p.init_cash
    positions: dict[str, float] = {}
    nav_series = pd.Series(index=cal, dtype=float)
    rebalances = []
    switch_log = []

    current_candidates: list[str] = []
    current_targets: dict[str, float] = {}
    current_vol: pd.Series = pd.Series(dtype=float)
    current_mom: pd.Series = pd.Series(dtype=float)

    min_hist = p.min_hist_days
    fee = p.transaction_cost

    def _rebalance_to(today, target_weights):
        nonlocal cash, positions
        for c, sh in list(positions.items()):
            if c in target_weights:
                continue
            px = prices.loc[today, c]
            if np.isnan(px):
                continue
            cash += sh * px * (1 - fee)
            positions.pop(c)
        port = cash + sum(sh * prices.loc[today, c] for c, sh in positions.items()
                          if not np.isnan(prices.loc[today, c]))
        for c, w in target_weights.items():
            px = prices.loc[today, c]
            if np.isnan(px) or px <= 0 or w <= 0:
                continue
            tgt_val = port * w
            cur_val = positions.get(c, 0.0) * px
            diff = tgt_val - cur_val
            if diff > 0:
                buy_amt = diff / (1 + fee)
                positions[c] = positions.get(c, 0.0) + buy_amt / px
                cash -= buy_amt * (1 + fee)
            elif diff < 0:
                sell_shares = (-diff) / px
                positions[c] = positions.get(c, 0.0) - sell_shares
                cash += (-diff) * (1 - fee)

    def _weight_from(picks, vol, mom):
        if p.weighting == "inv_vol":
            w = 1.0 / vol.loc[picks]
            return (w / w.sum()).to_dict()
        # 默认动量加权
        s = mom.reindex(picks).clip(lower=0)
        if s.sum() <= 0:
            return {c: 1.0 / len(picks) for c in picks}
        return (s / s.sum()).to_dict()

    for today in cal:
        mv = cash + sum(sh * prices.loc[today, c] for c, sh in positions.items()
                        if not np.isnan(prices.loc[today, c]))
        nav_series.loc[today] = mv

        # ---- 周度调仓 ----
        if today in weekly_dates:
            # 大盘择时滤波：若 regime 触发，全仓黄金
            if p.enable_regime_filter:
                bench = prices[BENCHMARK]
                if today in bench.index:
                    hist = bench.loc[:today]
                    if len(hist) > p.regime_lookback:
                        ret_n = hist.iloc[-1] / hist.iloc[-p.regime_lookback - 1] - 1
                        if ret_n < p.regime_threshold:
                            gold = p.regime_defensive_etf
                            if gold in prices.columns and not np.isnan(prices.loc[today, gold]):
                                current_targets = {gold: 1.0}
                                current_candidates = [gold]
                                _rebalance_to(today, current_targets)
                                rebalances.append({"date": today, "kind": "weekly_regime",
                                                   "target": dict(current_targets), "nav": mv})
                                continue

            hist_len = etf_prices.loc[:today].notna().sum()
            universe = [c for c in etf_cols
                        if hist_len[c] >= min_hist and not np.isnan(prices.loc[today, c])]
            if len(universe) < p.n_corr + 2:
                current_candidates, current_targets = [], {}
                continue

            vol = annual_volatility(returns[universe], p.vol_window).loc[today].dropna()
            vol = vol[(vol >= p.vol_low) & (vol <= p.vol_high)]
            if len(vol) < p.n_corr:
                current_candidates, current_targets = [], {}
                continue

            mean_corr = mean_abs_correlation(returns, p.corr_window, today, list(vol.index))
            if mean_corr.empty:
                current_candidates, current_targets = [], {}
                continue
            candidates = mean_corr.nsmallest(p.n_corr).index.tolist()
            mom = momentum_score(etf_prices, today, p.momentum_window, candidates).dropna()

            mom_sorted = mom.sort_values(ascending=False)
            picked = []
            for c in mom_sorted.index:
                if mom_sorted[c] <= 0:
                    break
                if p.enable_high_low_switch:
                    cat = CATEGORY.get(c, "stock")
                    if is_overheat(logbias.loc[today, c], rsi.loc[today, c],
                                   rsi_diff.loc[today, c], cat, p):
                        continue
                picked.append(c)
                if len(picked) >= p.n_momentum:
                    break

            current_candidates = candidates
            current_vol = vol
            current_mom = mom
            if picked:
                current_targets = _weight_from(picked, vol, mom)
            else:
                current_targets = {}

            _rebalance_to(today, current_targets)
            rebalances.append({"date": today, "kind": "weekly",
                               "target": dict(current_targets), "nav": mv})
            continue

        # ---- 日度高低切 ----
        if not p.enable_high_low_switch or not current_candidates or not positions:
            continue

        hot = []
        for c in list(positions.keys()):
            cat = CATEGORY.get(c, "stock")
            if is_overheat(logbias.loc[today, c], rsi.loc[today, c],
                           rsi_diff.loc[today, c], cat, p):
                hot.append(c)
        if not hot:
            continue

        mom_now = momentum_score(etf_prices, today, p.momentum_window, current_candidates).dropna()
        replacement_pool = []
        for c in current_candidates:
            if c in positions and c not in hot:
                continue
            cat = CATEGORY.get(c, "stock")
            if is_overheat(logbias.loc[today, c], rsi.loc[today, c],
                           rsi_diff.loc[today, c], cat, p):
                continue
            if c not in mom_now or mom_now[c] <= 0:
                continue
            lb = logbias.loc[today, c]
            if np.isnan(lb):
                continue
            replacement_pool.append((c, lb))
        replacement_pool.sort(key=lambda x: x[1])

        new_targets_list = [c for c in positions.keys() if c not in hot]
        for c, _lb in replacement_pool:
            if len(new_targets_list) >= p.n_momentum:
                break
            if c not in new_targets_list:
                new_targets_list.append(c)

        if set(new_targets_list) == set(positions.keys()) - set(hot):
            continue

        if not new_targets_list:
            new_target = {}
        else:
            new_target = _weight_from(new_targets_list, current_vol, mom_now)

        _rebalance_to(today, new_target)
        switch_log.append({"date": today, "hot_out": hot,
                           "new_in": [c for c in new_targets_list if c not in positions],
                           "new_target": new_target})
        rebalances.append({"date": today, "kind": "intraday_switch",
                           "target": new_target, "nav": mv})

    nav = nav_series.dropna() / p.init_cash
    bench = prices[BENCHMARK] / prices[BENCHMARK].iloc[0]
    bench = bench.reindex(nav.index)
    return BTResult(nav=nav, benchmark_nav=bench, rebalances=rebalances,
                    metrics=compute_metrics(nav, bench), switch_log=switch_log)


def save_v5(result: BTResult, tag: str):
    pd.DataFrame({"strategy": result.nav, "benchmark": result.benchmark_nav}) \
        .to_csv(RESULTS_DIR / f"nav_{tag}.csv", encoding="utf-8-sig")
    rows = []
    for r in result.rebalances:
        for c, w in (r["target"] or {"(空仓)": 0}).items():
            rows.append({"date": r["date"], "kind": r.get("kind"), "ts_code": c,
                         "name": ETF_POOL.get(c, c), "weight": w, "nav": r["nav"]})
    pd.DataFrame(rows).to_csv(RESULTS_DIR / f"rebalances_{tag}.csv",
                              index=False, encoding="utf-8-sig")
    pd.Series(result.metrics).to_csv(RESULTS_DIR / f"metrics_{tag}.csv",
                                     header=["value"], encoding="utf-8-sig")
    if result.switch_log:
        sw = []
        for r in result.switch_log:
            sw.append({"date": r["date"],
                       "hot_out": ",".join(r["hot_out"]),
                       "new_target": ",".join(f"{k}:{v:.2f}" for k, v in r["new_target"].items())})
        pd.DataFrame(sw).to_csv(RESULTS_DIR / f"switch_{tag}.csv",
                                index=False, encoding="utf-8-sig")
    plt.figure(figsize=(12, 6))
    plt.plot(result.nav.index, result.nav.values, label="策略 v5 (激进版)", linewidth=2, color="#d62728")
    plt.plot(result.benchmark_nav.index, result.benchmark_nav.values,
             label="沪深300基准", linestyle="--", color="gray")
    plt.title(f"A 股 ETF 激进轮动 v5 (A 股 + 商品) - {tag}", fontsize=13)
    plt.xlabel("日期"); plt.ylabel("净值")
    plt.legend(); plt.grid(True, alpha=0.3); plt.tight_layout()
    plt.savefig(RESULTS_DIR / f"nav_{tag}.png", dpi=120); plt.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["is", "oos", "full"], default="full")
    ap.add_argument("--tag", default="v5_aggressive")
    ap.add_argument("--n_momentum", type=int, default=2)
    ap.add_argument("--momentum_window", type=int, default=10)
    ap.add_argument("--vol_high", type=float, default=0.35)
    ap.add_argument("--weighting", choices=["momentum", "inv_vol"], default="momentum")
    ap.add_argument("--rsi_overheat", type=float, default=82.0)
    ap.add_argument("--logbias_mult", type=float, default=1.3)
    ap.add_argument("--no-switch", action="store_true")
    ap.add_argument("--regime", action="store_true", help="启用大盘择时滤波")
    ap.add_argument("--regime_lookback", type=int, default=60)
    ap.add_argument("--regime_threshold", type=float, default=-0.05)
    args = ap.parse_args()

    if args.mode == "is":
        start, end = "2018-01-01", "2023-12-31"
    elif args.mode == "oos":
        start, end = "2024-01-01", "2026-04-21"
    else:
        start, end = "2020-01-01", "2026-04-21"

    # 预加载从 2015 起以保证 corr_window/vol_window 就绪
    fetch_start = "2015-06-01"
    prices = fetch_all(fetch_start, end)

    p = ParamsV5(
        n_momentum=args.n_momentum,
        momentum_window=args.momentum_window,
        vol_high=args.vol_high,
        weighting=args.weighting,
        rsi_overheat=args.rsi_overheat,
        logbias_overheat_mult=args.logbias_mult,
        enable_high_low_switch=not args.no_switch,
        enable_regime_filter=args.regime,
        regime_lookback=args.regime_lookback,
        regime_threshold=args.regime_threshold,
    )

    print(f"[v5] mode={args.mode} {start} ~ {end}")
    print(f"     n_mom={p.n_momentum}  mom_win={p.momentum_window}  "
          f"vol_high={p.vol_high}  weighting={p.weighting}")
    print(f"     池子：{len(ETF_POOL_V5)} 只（A 股 + 商品）")

    res = run_backtest_v5(prices, start, end, p)
    print("\n--- 指标 ---")
    for k, v in res.metrics.items():
        if isinstance(v, float):
            print(f"  {k:20s} {v:8.4f}")
        else:
            print(f"  {k:20s} {v}")
    print(f"  周度调仓 {sum(1 for r in res.rebalances if r['kind']=='weekly')} 次")
    print(f"  日内切换 {len(res.switch_log)} 次")

    tag = f"{args.tag}_{args.mode}"
    save_v5(res, tag)
    print(f"\n保存：{RESULTS_DIR}/nav_{tag}.*")


if __name__ == "__main__":
    main()
