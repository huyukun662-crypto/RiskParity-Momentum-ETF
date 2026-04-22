# -*- coding: utf-8 -*-
"""
A 股 ETF 轮动策略 v4：v2 + 类别/单标集中度约束 + 2019 预热

关键改动（相对 v2 switch-on 万5 基线）：
  1. 类别权重上限 max_category_weight（stock/commodity/dividend/bond 任意一类）
  2. 单标权重上限 max_single_weight
  3. 预热期 warmup_start → eval_start：预热期跑策略但不计入 nav/指标

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
    annual_volatility, compute_metrics, inv_vol_weights, mean_abs_correlation, momentum_score,
)
from strategy_v2_high_low_switch import (
    CATEGORY, OVERHEAT, compute_logbias_rsi, is_overheat,
)

plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False


@dataclass
class ParamsV4:
    # —— v2 基础参数（万5手续费版）——
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
    rsi_overheat: float = 78.0
    logbias_overheat_mult: float = 1.0
    enable_high_low_switch: bool = True
    # —— v4 新增 ——
    max_category_weight: float = 0.5   # 单类别上限
    max_single_weight: float = 0.5     # 单标上限
    enable_weight_caps: bool = True


# ---------------- v4 新增：权重约束 ---------------- #
def apply_weight_caps(target: dict[str, float],
                       candidates: list[str],
                       categories: dict[str, str],
                       vol: pd.Series,
                       max_cat: float = 0.5,
                       max_single: float = 0.5,
                       max_iter: int = 5) -> dict[str, float]:
    """
    对目标权重施加"类别上限 + 单标上限"约束。
    算法：
      1) 先裁 max_single：超限者缩到 max_single，溢出权重 → 尝试分给候选池中其他标的（按 1/vol 分配）
      2) 再裁 max_cat：同一类别合计超限者按比例缩放，溢出权重 → 分给其他类别候选
      3) 最多 max_iter 轮。若无法分出则留现金（总和 < 1）。
    """
    if not target:
        return {}

    w = dict(target)
    cand_set = set(candidates) | set(w.keys())

    def _pour_excess(excess: float, blocked: set[str], blocked_cats: set[str]) -> float:
        """把多余权重 excess 按 1/vol 分给候选池中不在 blocked 且不在 blocked_cats 的标的。
        返回没能分出去的部分。"""
        pool = [c for c in cand_set
                if c not in blocked
                and categories.get(c, "stock") not in blocked_cats
                and c in vol.index and not np.isnan(vol.get(c, np.nan))]
        if not pool:
            return excess
        inv = pd.Series({c: 1.0 / vol[c] for c in pool})
        inv /= inv.sum()
        allocated = 0.0
        for c, share in inv.items():
            add = excess * share
            cur = w.get(c, 0.0)
            room_single = max_single - cur
            if room_single <= 0:
                continue
            give = min(add, room_single)
            w[c] = cur + give
            allocated += give
        return excess - allocated

    for _ in range(max_iter):
        changed = False

        # --- 单标上限 ---
        over_single = {c: ww for c, ww in w.items() if ww > max_single + 1e-9}
        if over_single:
            for c, ww in over_single.items():
                excess = ww - max_single
                w[c] = max_single
                # 把 excess 分到其他单标
                leftover = _pour_excess(excess, blocked={c}, blocked_cats=set())
                # leftover 留现金（不强制分配到同类）
                changed = True

        # --- 类别上限 ---
        cat_sum: dict[str, float] = {}
        for c, ww in w.items():
            if ww <= 0:
                continue
            cat_sum[categories.get(c, "stock")] = cat_sum.get(categories.get(c, "stock"), 0.0) + ww
        over_cat = {cat: s for cat, s in cat_sum.items() if s > max_cat + 1e-9}
        if over_cat:
            for cat, s in over_cat.items():
                scale = max_cat / s
                # 缩放该类所有标的
                for c in list(w.keys()):
                    if categories.get(c, "stock") == cat:
                        w[c] = w[c] * scale
                excess = s - max_cat
                # 分到其他类别
                blocked = {c for c in w if categories.get(c, "stock") == cat}
                leftover = _pour_excess(excess, blocked=blocked, blocked_cats={cat})
                changed = True

        if not changed:
            break

    # 归一化保护：若总和 < 1 留现金，超过 1 则按比例缩回到 1
    total = sum(w.values())
    if total > 1.0:
        w = {c: ww / total for c, ww in w.items()}
    # 丢弃接近 0 的条目
    w = {c: ww for c, ww in w.items() if ww > 1e-4}
    return w


# ---------------- v4 回测（含预热） ---------------- #
@dataclass
class BTResultV4:
    nav: pd.Series
    benchmark_nav: pd.Series
    rebalances: list = field(default_factory=list)
    switch_log: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def run_backtest_v4(prices: pd.DataFrame,
                    warmup_start: str,
                    eval_start: str,
                    end: str,
                    p: ParamsV4,
                    enable_warmup_trading: bool = False) -> BTResultV4:
    """
    warmup_start: 历史数据起点（用于因子计算，需 >=corr_window 天）
    eval_start:   正式评估/交易开始日（nav 从此归一化）
    end:          回测结束
    enable_warmup_trading: 是否在 warmup_start~eval_start 期间执行交易（True=v2风格预热，False=仅用历史算因子）

    当 enable_warmup_trading=False 时，2020-01-01 当天强制触发初始调仓（inv-vol 初仓）
    """
    # prices 保留完整历史（warmup_start 作为因子计算起点，交易从 eval_start 开始）
    prices_full = prices.loc[warmup_start:end].copy()
    if BENCHMARK not in prices_full.columns:
        raise ValueError(f"基准 {BENCHMARK} 缺失")
    etf_cols = [c for c in prices_full.columns if c != BENCHMARK]
    etf_prices = prices_full[etf_cols]
    returns = np.log(etf_prices / etf_prices.shift(1))
    logbias, rsi, rsi_diff = compute_logbias_rsi(etf_prices, p)

    eval_ts = pd.Timestamp(eval_start)

    # 交易日 cal：取决于是否启用预热交易
    if enable_warmup_trading:
        cal = prices_full.index
    else:
        cal = prices_full.loc[eval_start:].index

    # weekly 调仓日 在 cal 范围内
    weekly = set(prices_full.loc[cal.min():cal.max()].resample(p.rebalance_freq).last().index.intersection(cal))
    # 若不预热交易：强制 eval_ts 当天触发首次调仓（若 eval_ts 本身不是周五也强加入）
    if not enable_warmup_trading:
        # 找 eval_ts 之后的第一个交易日作为初始调仓日
        first_trade_day = cal[0] if len(cal) > 0 else eval_ts
        weekly.add(first_trade_day)
    prices = prices_full  # 别名保持向后兼容

    cash = p.init_cash
    positions: dict[str, float] = {}
    mv_series = pd.Series(index=cal, dtype=float)  # 含预热期全部 mv（如有）
    rebalances, switch_log = [], []
    # 引用 weekly 而不是 weekly_dates（函数后续用 today in weekly 判断）
    current_cand, current_vol = [], pd.Series(dtype=float)
    fee = p.transaction_cost

    def rebalance_to(today, target):
        nonlocal cash
        # 卖不在目标的
        for c in list(positions.keys()):
            if c in target:
                continue
            px = prices.loc[today, c]
            if np.isnan(px):
                continue
            cash += positions.pop(c) * px * (1 - fee)
        port = cash + sum(sh * prices.loc[today, c] for c, sh in positions.items()
                          if not np.isnan(prices.loc[today, c]))
        for c, w in target.items():
            px = prices.loc[today, c]
            if np.isnan(px) or px <= 0 or w <= 0:
                continue
            tgt = port * w
            cur = positions.get(c, 0.0) * px
            diff = tgt - cur
            if diff > 0:
                amt = diff / (1 + fee)
                positions[c] = positions.get(c, 0.0) + amt / px
                cash -= amt * (1 + fee)
            elif diff < 0:
                positions[c] = positions.get(c, 0.0) - (-diff) / px
                cash += (-diff) * (1 - fee)

    min_hist = p.min_hist_days

    for today in cal:
        mv = cash + sum(sh * prices.loc[today, c] for c, sh in positions.items()
                        if not np.isnan(prices.loc[today, c]))
        mv_series.loc[today] = mv

        # 仅在评估期记录 rebalances / switch_log
        in_eval = today >= eval_ts

        # ---- 周度选股 ----
        if today in weekly:
            hist_len = etf_prices.loc[:today].notna().sum()
            universe = [c for c in etf_cols
                        if hist_len[c] >= min_hist and not np.isnan(prices.loc[today, c])]
            if len(universe) < p.n_corr + 2:
                continue
            vol = annual_volatility(returns[universe], p.vol_window).loc[today].dropna()
            vol = vol[(vol >= p.vol_low) & (vol <= p.vol_high)]
            if len(vol) < p.n_corr:
                continue
            mc = mean_abs_correlation(returns, p.corr_window, today, list(vol.index))
            if mc.empty:
                continue
            cand = mc.nsmallest(p.n_corr).index.tolist()
            mom = momentum_score(etf_prices, today, p.momentum_window, cand).dropna()
            picked = []
            for c in mom.sort_values(ascending=False).index:
                if mom[c] <= 0:
                    break
                if p.enable_high_low_switch:
                    if is_overheat(logbias.loc[today, c], rsi.loc[today, c],
                                   rsi_diff.loc[today, c], CATEGORY.get(c, "stock"), p):
                        continue
                picked.append(c)
                if len(picked) >= p.n_momentum:
                    break
            current_cand = cand
            current_vol = vol
            if picked:
                w = inv_vol_weights(vol.loc[picked])
                target = {c: float(w[c]) for c in picked}
            else:
                target = {}

            # v4 权重约束：对 target + cand 的并集参与再分配
            if p.enable_weight_caps and target:
                target = apply_weight_caps(target, cand, CATEGORY, vol,
                                            max_cat=p.max_category_weight,
                                            max_single=p.max_single_weight)

            rebalance_to(today, target)
            if in_eval:
                rebalances.append({"date": today, "kind": "weekly",
                                   "target": dict(target), "nav": mv})
            continue

        # ---- 日度高低切 ----
        if not p.enable_high_low_switch or not current_cand or not positions:
            continue
        hot = [c for c in positions
               if is_overheat(logbias.loc[today, c], rsi.loc[today, c],
                              rsi_diff.loc[today, c], CATEGORY.get(c, "stock"), p)]
        if not hot:
            continue
        mom_now = momentum_score(etf_prices, today, p.momentum_window, current_cand).dropna()
        pool = []
        for c in current_cand:
            if c in positions and c not in hot:
                continue
            if is_overheat(logbias.loc[today, c], rsi.loc[today, c],
                           rsi_diff.loc[today, c], CATEGORY.get(c, "stock"), p):
                continue
            if c not in mom_now or mom_now[c] <= 0:
                continue
            lb = logbias.loc[today, c]
            if np.isnan(lb):
                continue
            pool.append((c, lb))
        pool.sort(key=lambda x: x[1])

        new_targets = [c for c in positions if c not in hot]
        for c, _ in pool:
            if len(new_targets) >= p.n_momentum:
                break
            if c not in new_targets:
                new_targets.append(c)
        if set(new_targets) == set(positions.keys()):
            continue
        if new_targets:
            avail = current_vol.reindex(new_targets).dropna()
            if avail.empty:
                continue
            w = inv_vol_weights(avail)
            new_target = {c: float(w[c]) for c in avail.index}
        else:
            new_target = {}

        # v4 约束
        if p.enable_weight_caps and new_target:
            new_target = apply_weight_caps(new_target, current_cand, CATEGORY, current_vol,
                                            max_cat=p.max_category_weight,
                                            max_single=p.max_single_weight)

        rebalance_to(today, new_target)
        if in_eval:
            switch_log.append({"date": today, "hot_out": hot, "new_target": new_target})
            rebalances.append({"date": today, "kind": "intraday_switch",
                               "target": new_target, "nav": mv})

    # 归一化：从 eval_start 开始
    eval_mv = mv_series.loc[mv_series.index >= eval_ts]
    if eval_mv.empty:
        raise ValueError(f"eval_start {eval_start} 之后没有数据")
    nav = eval_mv / eval_mv.iloc[0]
    bench = prices[BENCHMARK].reindex(nav.index)
    bench = bench / bench.iloc[0]
    return BTResultV4(nav=nav, benchmark_nav=bench,
                      rebalances=rebalances, switch_log=switch_log,
                      metrics=compute_metrics(nav, bench))


# ---------------- 输出 ---------------- #
def save_v4(r: BTResultV4, tag: str):
    pd.DataFrame({"strategy": r.nav, "benchmark": r.benchmark_nav}) \
        .to_csv(RESULTS_DIR / f"nav_{tag}.csv", encoding="utf-8-sig")
    rows = []
    for x in r.rebalances:
        for c, w in (x["target"] or {"(空仓)": 0}).items():
            rows.append({"date": x["date"], "kind": x["kind"], "ts_code": c,
                         "name": ETF_POOL.get(c, c), "weight": w, "nav": x["nav"]})
    pd.DataFrame(rows).to_csv(RESULTS_DIR / f"rebalances_{tag}.csv",
                              index=False, encoding="utf-8-sig")
    if r.switch_log:
        sw = [{"date": x["date"], "hot_out": ",".join(x["hot_out"]),
               "new_target": ",".join(f"{k}:{v:.2f}" for k, v in x["new_target"].items())}
              for x in r.switch_log]
        pd.DataFrame(sw).to_csv(RESULTS_DIR / f"switch_{tag}.csv",
                                index=False, encoding="utf-8-sig")
    pd.Series(r.metrics).to_csv(RESULTS_DIR / f"metrics_{tag}.csv",
                                 header=["value"], encoding="utf-8-sig")
    plt.figure(figsize=(12, 6))
    plt.plot(r.nav.index, r.nav.values, label="策略 v4", linewidth=2)
    plt.plot(r.benchmark_nav.index, r.benchmark_nav.values,
             label="沪深300基准", linestyle="--")
    plt.title(f"A股ETF v4 ({tag.upper()})  ann={r.metrics['annual_return']:.2%} "
              f"sharpe={r.metrics['sharpe']:.2f} dd={r.metrics['max_drawdown']:.2%}",
              fontsize=12)
    plt.xlabel("日期"); plt.ylabel("净值")
    plt.legend(); plt.grid(True, alpha=0.3); plt.tight_layout()
    plt.savefig(RESULTS_DIR / f"nav_{tag}.png", dpi=120); plt.close()


# ---------------- 主入口 ---------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["is", "oos", "full"], default="full")
    ap.add_argument("--warmup", default=None, help="因子计算起点 YYYY-MM-DD（默认提前 500 交易日）")
    ap.add_argument("--warmup_trading", action="store_true", help="启用预热期交易（默认关闭，仅用于因子计算）")
    ap.add_argument("--max_cat", type=float, default=0.5)
    ap.add_argument("--max_single", type=float, default=0.5)
    ap.add_argument("--n_momentum", type=int, default=3)
    ap.add_argument("--tag", default="v4_default")
    args = ap.parse_args()

    # 对各 mode 的默认窗口
    windows = {
        "is":   ("2018-01-01", "2023-12-31"),
        "oos":  ("2024-01-01", "2026-04-21"),
        "full": ("2020-01-01", "2026-04-21"),
    }
    eval_start, end = windows[args.mode]
    # warmup_start 默认提前 ~2.5 年（约 500 交易日，满足 corr_window）
    if args.warmup:
        warmup_start = args.warmup
    else:
        from pandas.tseries.offsets import BDay
        warmup_start = (pd.Timestamp(eval_start) - BDay(600)).strftime("%Y-%m-%d")

    prices = fetch_all("2015-06-01", "2026-04-21")
    p = ParamsV4(max_category_weight=args.max_cat,
                 max_single_weight=args.max_single,
                 n_momentum=args.n_momentum)
    print(f"\n=== v4 [{args.tag}] mode={args.mode}  因子起点={warmup_start}  "
          f"交易起点={eval_start} ~ {end}  预热交易={args.warmup_trading}  ===")
    print(f"  max_cat={p.max_category_weight}  max_single={p.max_single_weight}  "
          f"n_mom={p.n_momentum}")
    r = run_backtest_v4(prices, warmup_start, eval_start, end, p,
                         enable_warmup_trading=args.warmup_trading)
    print(f"  metrics: {r.metrics}")
    print(f"  调仓 {sum(1 for x in r.rebalances if x['kind']=='weekly')} 次，"
          f"高低切 {len(r.switch_log)} 次")
    save_v4(r, args.tag)


if __name__ == "__main__":
    main()
