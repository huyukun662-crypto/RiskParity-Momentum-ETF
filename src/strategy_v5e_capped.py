# -*- coding: utf-8 -*-
"""
A 股 ETF 轮动 v5e — v5d 的 risk-cap 升级版

相对 v5d 的两处改动：
  改动 A：类别集中度上限 max_category_weight（默认 0.7）
          任意类别（stock/commodity/dividend/bond）合计权重 ≤ 上限
          超过则按比例缩放，剩余权重分给其他类别候选（复用 v4 的 apply_weight_caps）
  改动 B：多元避险篮子 defense_basket（替代单一黄金 518880）
          regime 触发时配置 {黄金, 国债, 十年国债, 红利, 煤炭} 的预设组合
          带动态裁剪：只纳入历史 ≥ 250 天的成员，权重按原比例归一化

其余因子全部继承 v5d（lookback=120, threshold=−0.08, n_mom=3, 全池 64 只）。

仅供研究参考，不构成投资建议。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
from strategy_v4_risk_cap import apply_weight_caps
import strategy_v5_aggressive as v5

# v5e 默认放开跨境（全池）——与 v5d 一致
EXCLUDE_V5E: set[str] = set()
ETF_POOL_V5E = {k: v for k, v in ETF_POOL.items() if k not in EXCLUDE_V5E}


# ---------------- 防御篮子预设 ---------------- #
DEFENSE_BASKETS: dict[str, dict[str, float]] = {
    "B0_gold_only":     {"518880.SH": 1.00},                                                # v5d baseline
    "B1_gold_bond":     {"518880.SH": 0.50, "511010.SH": 0.25, "511260.SH": 0.25},
    "B2_gold_bond_div": {"518880.SH": 0.40, "511010.SH": 0.20, "511260.SH": 0.20,
                         "515080.SH": 0.20},
    "B3_diversified":   {"518880.SH": 0.30, "511010.SH": 0.20, "511260.SH": 0.20,
                         "515080.SH": 0.15, "515220.SH": 0.15},
    "B4_bond_heavy":    {"518880.SH": 0.25, "511010.SH": 0.25, "511260.SH": 0.50},
}


def active_defense_basket(basket: dict[str, float],
                          prices: pd.DataFrame,
                          today: pd.Timestamp,
                          min_hist: int = 250) -> dict[str, float]:
    """对防御篮子做动态成员裁剪：只保留历史 >= min_hist 天且当日价格非 NaN 的成员，
    然后在 active 成员间按原比例重新归一化。
    若 active 成员数 < 2，退化到纯黄金 518880（v5d 老行为）。
    """
    active: dict[str, float] = {}
    for c, w in basket.items():
        if c not in prices.columns:
            continue
        ser = prices[c].loc[:today]
        if ser.notna().sum() < min_hist:
            continue
        if np.isnan(prices.loc[today, c]):
            continue
        active[c] = w
    if len(active) < 2:
        # fallback：只用黄金
        return {"518880.SH": 1.0}
    tot = sum(active.values())
    return {c: w / tot for c, w in active.items()}


@dataclass
class ParamsV5E:
    # —— v5d 继承 ——
    init_cash: float = 1_000_000
    vol_window: int = 180
    corr_window: int = 500
    momentum_window: int = 20
    vol_low: float = 0.08
    vol_high: float = 0.28
    n_corr: int = 5
    n_momentum: int = 3
    rebalance_freq: str = "W-FRI"
    transaction_cost: float = 0.0005
    min_hist_days: int = 90
    ema_window: int = 30
    rsi_window: int = 14
    logbias_overheat_mult: float = 1.0
    rsi_overheat: float = 78.0
    enable_high_low_switch: bool = True
    weighting: str = "inv_vol"
    # —— regime 滤波（v5d 选定参数，冻结）——
    enable_regime_filter: bool = True
    regime_lookback: int = 120
    regime_threshold: float = -0.08
    # —— v5e 新增 A：类别上限 ——
    enable_weight_caps: bool = True
    max_category_weight: float = 0.7
    max_single_weight: float = 1.0   # 不限单标（v5d 本身没有单标问题）
    # —— v5e 新增 B：防御篮子 ——
    defense_basket_name: str = "B1_gold_bond"
    defense_min_hist: int = 250
    # —— v5f 新增 C：差异化类别上限（只对商品类单独加严）——
    max_commodity_weight: float = 1.0   # 1.0 = 关闭（默认等同 v5e）
    # —— v5f 新增 D：组合级 vol targeting ——
    enable_vol_targeting: bool = False
    vol_target: float = 0.18            # 年化 vol 目标
    vol_target_window: int = 20         # 历史回看
    # —— v5i 新增 E：Trailing Stop（基于组合 nav 历史峰值回撤）——
    trailing_dd: float = 0.0            # 触发阈值（负数，如 -0.08 = 峰值回撤 8%）。0.0 = 关闭
    trailing_recovery: float = 0.03     # 退出阈值（正数，从触发低点反弹 X%）
    trailing_max_days: int = 20         # 最大防御持续天数


def apply_commodity_cap(target: dict[str, float],
                        candidates: list[str],
                        categories: dict[str, str],
                        vol: pd.Series,
                        max_commodity: float) -> dict[str, float]:
    """v5f 改动 C：对 target 权重中的商品类做单独上限。
    逻辑：若 sum(commodity weights) > max_commodity，按比例缩放商品类权重，
    溢出部分按 1/σ 分给候选池中的**非商品类** ETF（不超 max_single=1）。
    无合适非商品类候选时剩余留现金。
    """
    if max_commodity >= 1.0 or not target:
        return dict(target)
    cmd_sum = sum(w for c, w in target.items()
                  if categories.get(c, "stock") == "commodity")
    if cmd_sum <= max_commodity + 1e-9:
        return dict(target)
    w = dict(target)
    # 缩放商品类
    scale = max_commodity / cmd_sum
    for c in list(w.keys()):
        if categories.get(c, "stock") == "commodity":
            w[c] = w[c] * scale
    excess = cmd_sum - max_commodity
    # 溢出权重按 1/σ 分给非商品类候选
    pool = [c for c in candidates
            if categories.get(c, "stock") != "commodity"
            and c in vol.index and not np.isnan(vol.get(c, np.nan))]
    if not pool:
        return w   # 留现金
    inv = pd.Series({c: 1.0 / vol[c] for c in pool})
    inv = inv / inv.sum()
    for c, share in inv.items():
        w[c] = w.get(c, 0.0) + excess * share
    return w


def _vol_target_scale(target: dict[str, float], etf_prices: pd.DataFrame,
                      today: pd.Timestamp, window: int, vol_target: float
                      ) -> dict[str, float]:
    """v5f 改动 D：组合级 vol targeting。
    计算目标组合在过去 window 日的实测年化波动率，若 > vol_target，则按
    scale = vol_target / 实测 vol 对所有权重等比缩小（差额留现金）。
    为避免成员数据不足时误触发，要求所有成员都有 ≥ window 日历史。
    """
    if not target or vol_target <= 0:
        return target
    members = list(target.keys())
    # 取 window 日历史
    hist = etf_prices.loc[:today, members].tail(window + 1)
    if len(hist) < window + 1 or hist.isna().any().any():
        return target
    # 每日组合收益 = Σ w_i × r_i
    log_ret = np.log(hist / hist.shift(1)).iloc[1:]
    w_vec = pd.Series(target, index=members)
    port_ret = (log_ret * w_vec).sum(axis=1)
    realized_vol = port_ret.std() * np.sqrt(252)
    if realized_vol <= vol_target + 1e-9:
        return target
    scale = vol_target / realized_vol
    return {c: w * scale for c, w in target.items()}


@dataclass
class BTResultV5E:
    nav: pd.Series
    benchmark_nav: pd.Series
    rebalances: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    switch_log: list = field(default_factory=list)


def run_backtest_v5e(prices: pd.DataFrame, start: str, end: str, p: ParamsV5E) -> BTResultV5E:
    keep = [c for c in prices.columns if c in ETF_POOL_V5E or c == BENCHMARK]
    prices = prices[keep].loc[start:end].copy()
    if BENCHMARK not in prices.columns:
        raise ValueError(f"基准 {BENCHMARK} 缺失")
    etf_cols = [c for c in prices.columns if c != BENCHMARK]
    etf_prices = prices[etf_cols]
    returns = np.log(etf_prices / etf_prices.shift(1))

    logbias, rsi, rsi_diff = compute_logbias_rsi(etf_prices, p)

    cal = prices.index
    weekly_dates = set(prices.resample(p.rebalance_freq).last().index.intersection(cal))

    basket_profile = DEFENSE_BASKETS[p.defense_basket_name]

    cash = p.init_cash
    positions: dict[str, float] = {}
    nav_series = pd.Series(index=cal, dtype=float)
    rebalances = []
    switch_log = []

    current_candidates: list[str] = []
    current_targets: dict[str, float] = {}
    current_vol: pd.Series = pd.Series(dtype=float)

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

    def _weight_from(picks, vol):
        if p.weighting == "inv_vol":
            w = 1.0 / vol.loc[picks]
            return (w / w.sum()).to_dict()
        # momentum 分支（v5e 默认不用，保留兼容）
        return {c: 1.0 / len(picks) for c in picks}

    # v5i trailing stop 状态机（严格只用当日及历史数据，无前视）
    trail_on = False
    trail_low = None           # 进入防御后的最低 mv
    trail_days = 0             # 已在防御的天数计数

    for today in cal:
        mv = cash + sum(sh * prices.loc[today, c] for c, sh in positions.items()
                        if not np.isnan(prices.loc[today, c]))
        nav_series.loc[today] = mv

        # ---- v5i: trailing stop 状态更新（每日） ----
        # 仅用 nav_series.loc[:today]（含今日）计算峰值 —— 无前视
        if p.trailing_dd < 0:
            nav_so_far = nav_series.loc[:today].dropna()
            if len(nav_so_far) >= 20:  # 至少 20 日历史避免早期误触发
                peak = nav_so_far.max()
                dd_now = mv / peak - 1
                if not trail_on and dd_now <= p.trailing_dd:
                    # 触发 trailing defense
                    trail_on = True
                    trail_low = mv
                    trail_days = 0
                elif trail_on:
                    trail_days += 1
                    trail_low = min(trail_low, mv)
                    recover_pct = mv / trail_low - 1
                    if recover_pct >= p.trailing_recovery or trail_days >= p.trailing_max_days:
                        trail_on = False

        # ---- 周度调仓 ----
        if today in weekly_dates:
            # v5i: trailing defense 优先（在 regime 之前）
            if trail_on:
                active_basket = active_defense_basket(
                    basket_profile, prices, today, p.defense_min_hist)
                current_targets = active_basket
                current_candidates = list(active_basket.keys())
                _rebalance_to(today, current_targets)
                rebalances.append({"date": today, "kind": "weekly_trail",
                                   "target": dict(current_targets),
                                   "nav": mv})
                continue

            # regime 触发 → 多元防御篮子
            if p.enable_regime_filter:
                bench = prices[BENCHMARK]
                if today in bench.index:
                    hist = bench.loc[:today]
                    if len(hist) > p.regime_lookback:
                        ret_n = hist.iloc[-1] / hist.iloc[-p.regime_lookback - 1] - 1
                        if ret_n < p.regime_threshold:
                            active_basket = active_defense_basket(
                                basket_profile, prices, today, p.defense_min_hist)
                            current_targets = active_basket
                            current_candidates = list(active_basket.keys())
                            _rebalance_to(today, current_targets)
                            rebalances.append({"date": today, "kind": "weekly_regime",
                                               "target": dict(current_targets),
                                               "basket": p.defense_basket_name, "nav": mv})
                            continue

            # 正常进攻流程
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
            if picked:
                target = _weight_from(picked, vol)
                # v5e 改动 A：类别上限
                if p.enable_weight_caps:
                    target = apply_weight_caps(
                        target, candidates, CATEGORY, vol,
                        max_cat=p.max_category_weight,
                        max_single=p.max_single_weight)
                # v5f 改动 C：商品类单独上限（可叠加在 max_cat 之后）
                if p.max_commodity_weight < 1.0:
                    target = apply_commodity_cap(
                        target, candidates, CATEGORY, vol,
                        max_commodity=p.max_commodity_weight)
                # v5f 改动 D：组合级 vol targeting
                if p.enable_vol_targeting:
                    target = _vol_target_scale(target, etf_prices, today,
                                               p.vol_target_window, p.vol_target)
                current_targets = target
            else:
                current_targets = {}

            _rebalance_to(today, current_targets)
            rebalances.append({"date": today, "kind": "weekly",
                               "target": dict(current_targets), "nav": mv})
            continue

        # ---- 日度高低切（进攻模式内） ----
        if trail_on:
            continue   # v5i: trailing defense 期间暂停日内切换
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
            new_target = _weight_from(new_targets_list, current_vol)
            if p.enable_weight_caps:
                new_target = apply_weight_caps(
                    new_target, current_candidates, CATEGORY, current_vol,
                    max_cat=p.max_category_weight,
                    max_single=p.max_single_weight)
            # v5f C：商品单独上限（日内高低切后也重新应用）
            if p.max_commodity_weight < 1.0:
                new_target = apply_commodity_cap(
                    new_target, current_candidates, CATEGORY, current_vol,
                    max_commodity=p.max_commodity_weight)
            # v5f D：vol targeting
            if p.enable_vol_targeting:
                new_target = _vol_target_scale(new_target, etf_prices, today,
                                               p.vol_target_window, p.vol_target)

        _rebalance_to(today, new_target)
        switch_log.append({"date": today, "hot_out": hot,
                           "new_in": [c for c in new_targets_list if c not in positions],
                           "new_target": new_target})
        rebalances.append({"date": today, "kind": "intraday_switch",
                           "target": new_target, "nav": mv})

    nav = nav_series.dropna() / p.init_cash
    bench = prices[BENCHMARK] / prices[BENCHMARK].iloc[0]
    bench = bench.reindex(nav.index)
    return BTResultV5E(nav=nav, benchmark_nav=bench, rebalances=rebalances,
                      metrics=compute_metrics(nav, bench), switch_log=switch_log)
