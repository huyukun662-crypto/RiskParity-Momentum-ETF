# -*- coding: utf-8 -*-
"""
A 股 ETF 轮动策略 v2：周度候选池 + 日度高低切
基于 v1（风险平价 + 动量共振），借鉴 34.12 notebook 的 logbias / RSI 过热机制。

架构：
  - 每周五：按原 v1 流程生成 **候选池（n_corr 只低相关 ETF）** 与 **周度目标集（n_momentum 只）**
  - 每个交易日：
      1) 对当前持仓检查过热：
         logbias > OVERHEAT[cat]  或  rsi14 > 78 且 rsi 在下行
      2) 过热者被"切出"，在候选池中寻找"低位"替代：
         未过热 + logbias 最小 + 动量得分 > 0
      3) 按 1/σ 倒数重新分配权重

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
    annual_volatility, compute_metrics, inv_vol_weights,
    mean_abs_correlation, momentum_score, save_outputs as _v1_save,
)

# ---------------- ETF 分类（按 34.12 notebook 思路）---------------- #
#   stock:     宽基 / 行业 / 成长 / 消费 / 医药 / 地产 / 金融 / 科技 / 跨境股票
#   commodity: 黄金 / 白银 / 原油 / 豆粕 / 有色 / 稀土 / 钢铁 / 煤炭 / 化工 / 石油
#   dividend:  红利 / 红利低波
#   bond:      债券（不做过热判定，避免误杀防御仓）
CATEGORY: dict[str, str] = {
    # dividend
    "515080.SH": "dividend", "159525.SZ": "dividend",
    # commodity
    "518880.SH": "commodity", "159934.SZ": "commodity", "159985.SZ": "commodity",
    "159713.SZ": "commodity", "159980.SZ": "commodity", "515210.SH": "commodity",
    "515220.SH": "commodity", "159870.SZ": "commodity", "561760.SH": "commodity",
    "561360.SH": "commodity",
    # bond
    "511010.SH": "bond", "511260.SH": "bond", "511090.SH": "bond",
    "511030.SH": "bond", "511360.SH": "bond", "511380.SH": "bond", "159396.SZ": "bond",
}
# 其余全部归为 stock
for _c in ETF_POOL:
    CATEGORY.setdefault(_c, "stock")

OVERHEAT = {"stock": 16.5, "commodity": 11.0, "dividend": 6.0, "bond": 999.0}
RSI_OVERHEAT = 78.0


@dataclass
class ParamsV2:
    # —— v1 参数（沿用 2018-2023 调参最优）——
    init_cash: float = 1_000_000
    vol_window: int = 180
    corr_window: int = 500
    momentum_window: int = 20
    vol_low: float = 0.08
    vol_high: float = 0.28
    n_corr: int = 5
    n_momentum: int = 3
    rebalance_freq: str = "W-FRI"
    transaction_cost: float = 0.0005  # 单边万5
    min_hist_days: int = 90  # 新上市 ETF 最少历史天数
    # —— v2 新增参数 ——
    ema_window: int = 30          # logbias 的 EMA 窗口
    rsi_window: int = 14
    logbias_overheat_mult: float = 1.0   # OVERHEAT 阈值倍率（调优用）
    rsi_overheat: float = 78.0
    enable_high_low_switch: bool = True  # 总开关


# ---------------- 指标计算 ---------------- #
def compute_logbias_rsi(prices: pd.DataFrame, p: ParamsV2) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """返回 (logbias, rsi14, rsi_diff) 三张表，列与 prices 一致。"""
    log_close = np.log(prices)
    log_ema = log_close.ewm(span=p.ema_window, adjust=False, min_periods=p.ema_window).mean()
    logbias = (log_close - log_ema) * 100

    # RSI14
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_g = gain.ewm(alpha=1 / p.rsi_window, adjust=False, min_periods=p.rsi_window).mean()
    avg_l = loss.ewm(alpha=1 / p.rsi_window, adjust=False, min_periods=p.rsi_window).mean()
    rs = avg_g / avg_l.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.where(avg_l.ne(0), 100).where(avg_g.ne(0), 0)
    rsi_diff = rsi.diff()  # >0 上行，<0 下行
    return logbias, rsi, rsi_diff


def is_overheat(logbias_t: float, rsi_t: float, rsi_diff_t: float,
                cat: str, p: ParamsV2) -> bool:
    """判定某只 ETF 在 t 日是否过热：
        (1) logbias > OVERHEAT 且 logbias_slope<0 （暂用 rsi_diff 作为 slope 代理）
        (2) rsi14 > RSI_OVERHEAT 且 rsi 在下行
    """
    if np.isnan(logbias_t) or np.isnan(rsi_t):
        return False
    overheat_th = OVERHEAT.get(cat, 999.0) * p.logbias_overheat_mult
    logbias_hot = (logbias_t > overheat_th)
    rsi_hot = (rsi_t > p.rsi_overheat) and (np.isnan(rsi_diff_t) or rsi_diff_t < 0)
    return logbias_hot or rsi_hot


# ---------------- 回测引擎（v2） ---------------- #
@dataclass
class BTResult:
    nav: pd.Series
    benchmark_nav: pd.Series
    rebalances: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    switch_log: list = field(default_factory=list)  # 日度高低切事件


def run_backtest_v2(prices: pd.DataFrame, start: str, end: str, p: ParamsV2) -> BTResult:
    prices = prices.loc[start:end].copy()
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

    # 周度状态：候选池 + 基础目标（周度选出）
    current_candidates: list[str] = []          # 5 只候选池
    current_targets: dict[str, float] = {}      # {code: inv-vol weight}，周度基础目标
    current_universe_vol: pd.Series = pd.Series(dtype=float)  # 候选池当日波动率

    min_hist = getattr(p, "min_hist_days", 90)
    fee = p.transaction_cost

    def _liquidate_all(today):
        nonlocal cash, positions
        for c, sh in list(positions.items()):
            px = prices.loc[today, c]
            if not np.isnan(px):
                cash += sh * px * (1 - fee)
            positions.pop(c)

    def _rebalance_to(today, target_weights):
        """调仓到 target_weights（code -> weight，已归一化）"""
        nonlocal cash, positions
        # 卖出不在目标的
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

    for today in cal:
        # ---- 当日估值 ----
        mv = cash + sum(sh * prices.loc[today, c] for c, sh in positions.items()
                        if not np.isnan(prices.loc[today, c]))
        nav_series.loc[today] = mv

        # ---- (A) 周度调仓：重算候选池与周度目标 ----
        if today in weekly_dates:
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

            # 周度目标：动量 top n + **过热过滤**
            mom_sorted = mom.sort_values(ascending=False)
            picked = []
            for c in mom_sorted.index:
                if mom_sorted[c] <= 0:
                    break
                if p.enable_high_low_switch:
                    cat = CATEGORY.get(c, "stock")
                    if is_overheat(logbias.loc[today, c], rsi.loc[today, c],
                                   rsi_diff.loc[today, c], cat, p):
                        continue  # 周度选股就跳过过热
                picked.append(c)
                if len(picked) >= p.n_momentum:
                    break

            current_candidates = candidates
            current_universe_vol = vol  # 用于后续 inv-vol 加权
            if picked:
                w = inv_vol_weights(vol.loc[picked])
                current_targets = {c: float(w[c]) for c in picked}
            else:
                current_targets = {}

            _rebalance_to(today, current_targets)
            rebalances.append({"date": today, "kind": "weekly",
                               "target": dict(current_targets), "nav": mv})
            continue

        # ---- (B) 日度高低切 ----
        if not p.enable_high_low_switch or not current_candidates:
            continue
        if not positions:
            continue

        # 检查当前持仓是否有过热
        hot = []
        for c in list(positions.keys()):
            cat = CATEGORY.get(c, "stock")
            if is_overheat(logbias.loc[today, c], rsi.loc[today, c],
                           rsi_diff.loc[today, c], cat, p):
                hot.append(c)
        if not hot:
            continue

        # 在候选池中寻找"低位"替代：logbias 最小 + 未过热 + 动量得分 > 0
        mom_now = momentum_score(etf_prices, today, p.momentum_window, current_candidates).dropna()
        replacement_pool = []
        for c in current_candidates:
            if c in positions and c not in hot:
                continue  # 已持仓且不过热，保留
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
        # 按 logbias 升序排 → 低位优先
        replacement_pool.sort(key=lambda x: x[1])

        # 构造新 target set
        new_targets_list = [c for c in positions.keys() if c not in hot]
        for c, _lb in replacement_pool:
            if len(new_targets_list) >= p.n_momentum:
                break
            if c not in new_targets_list:
                new_targets_list.append(c)

        if set(new_targets_list) == set(positions.keys()) and not hot:
            continue  # 实际无变化

        # 按 inv-vol 重新分配权重（用候选池的当日 vol）
        if new_targets_list:
            avail_vol = current_universe_vol.reindex(new_targets_list).dropna()
            if avail_vol.empty:
                continue
            w = inv_vol_weights(avail_vol)
            new_target = {c: float(w[c]) for c in avail_vol.index}
        else:
            new_target = {}

        _rebalance_to(today, new_target)
        switch_log.append({"date": today, "hot_out": hot,
                            "new_in": [c for c in new_targets_list if c not in positions_before(positions, hot)],
                            "new_target": new_target})
        rebalances.append({"date": today, "kind": "intraday_switch",
                           "target": new_target, "nav": mv})

    nav = nav_series.dropna() / p.init_cash
    bench = prices[BENCHMARK] / prices[BENCHMARK].iloc[0]
    bench = bench.reindex(nav.index)
    return BTResult(nav=nav, benchmark_nav=bench,
                    rebalances=rebalances, metrics=compute_metrics(nav, bench),
                    switch_log=switch_log)


def positions_before(positions, removed):
    """辅助：移除 removed 后的代码集"""
    return set(positions.keys()) - set(removed)


# ---------------- 输出 ---------------- #
def save_v2(result: BTResult, tag: str):
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
    # switch log
    if result.switch_log:
        sw = []
        for r in result.switch_log:
            sw.append({"date": r["date"],
                       "hot_out": ",".join(r["hot_out"]),
                       "new_target": ",".join(f"{k}:{v:.2f}" for k, v in r["new_target"].items())})
        pd.DataFrame(sw).to_csv(RESULTS_DIR / f"switch_{tag}.csv",
                                index=False, encoding="utf-8-sig")
    plt.figure(figsize=(12, 6))
    plt.plot(result.nav.index, result.nav.values, label="策略净值 v2", linewidth=2)
    plt.plot(result.benchmark_nav.index, result.benchmark_nav.values,
             label="沪深300基准", linestyle="--")
    plt.title(f"A股ETF轮动策略 v2 (logbias+RSI过热日内切换) - {tag.upper()}", fontsize=13)
    plt.xlabel("日期"); plt.ylabel("净值")
    plt.legend(); plt.grid(True, alpha=0.3); plt.tight_layout()
    plt.savefig(RESULTS_DIR / f"nav_{tag}.png", dpi=120); plt.close()


# ---------------- 主入口 ---------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["is", "oos", "full"], default="full")
    ap.add_argument("--no-switch", action="store_true", help="关闭高低切做对照")
    args = ap.parse_args()

    prices = fetch_all("2015-06-01", "2026-04-21")
    p = ParamsV2(enable_high_low_switch=not args.no_switch)
    tag_suffix = "_noswitch" if args.no_switch else ""

    windows = {
        "is": ("2018-01-01", "2023-12-31"),
        "oos": ("2024-01-01", "2026-04-21"),
        "full": ("2018-01-01", "2026-04-21"),
    }
    to_run = [args.mode] if args.mode != "full" else ["is", "oos"]
    for tag in to_run:
        s, e = windows[tag]
        print(f"\n=== {tag.upper()}  {s} ~ {e}   switch={p.enable_high_low_switch} ===")
        r = run_backtest_v2(prices, s, e, p)
        print(f"  metrics: {r.metrics}")
        print(f"  日度高低切次数: {len(r.switch_log)}")
        save_v2(r, f"v2_{tag}{tag_suffix}")


if __name__ == "__main__":
    main()
