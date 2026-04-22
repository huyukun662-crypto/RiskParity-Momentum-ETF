# -*- coding: utf-8 -*-
"""
A 股 ETF 轮动策略：风险平价（波动率过滤 + 低相关性筛选）+ 动量共振评分
复现自 OpenAlphas「年化收益 52% ETF 轮动策略」，标的换为 A 股跨资产 ETF。
仅供研究参考，不构成投资建议。
"""
from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import akshare as ak
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from tqdm import tqdm

# ---------------- 全局配置 ---------------- #
TUSHARE_TOKEN = os.environ.get(
    "TUSHARE_TOKEN",
    "ddd1b26b20ff085ac9b60c9bd902ae76bbff60910863e8cc0168da53",
)
ROOT = Path(__file__).parent
CACHE_DIR = ROOT / "cache"
RESULTS_DIR = ROOT / "results"
CACHE_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

# 中文字体
plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False

ETF_POOL: dict[str, str] = {
    # 宽基（A 股）
    "510300.SH": "沪深300", "510050.SH": "上证50", "510500.SH": "中证500",
    "512100.SH": "中证1000ETF", "588000.SH": "科创50ETF", "588080.SH": "科创板50",
    "159915.SZ": "创业板ETF", "159952.SZ": "创业板ETF广发", "159949.SZ": "创业板50",
    "563360.SH": "中证A500ETF",
    # 宽基（跨境）
    "513100.SH": "纳指ETF", "513500.SH": "标普500ETF", "159920.SZ": "恒生ETF",
    "513180.SH": "恒生科技ETF", "513050.SH": "中概互联ETF",
    # 风格因子
    "515080.SH": "中证红利ETF招商", "159525.SZ": "红利低波ETF富国",
    # 科技成长
    "512480.SH": "半导体ETF国联安", "515980.SH": "人工智能ETF", "515230.SH": "软件ETF国泰",
    "515880.SH": "通信ETF国泰", "159779.SZ": "消费电子ETF", "159890.SZ": "云计算ETF招商",
    "159869.SZ": "游戏ETF华夏", "159851.SZ": "金融科技ETF华宝", "562500.SH": "机器人ETF华夏",
    # 先进制造
    "159755.SZ": "电池ETF广发", "515030.SH": "新能源车ETF华夏", "159857.SZ": "光伏ETF",
    "159326.SZ": "电网设备ETF华夏", "159227.SZ": "航空航天ETF华夏", "512660.SH": "军工ETF国泰",
    "563230.SH": "卫星ETF富国",
    # 医疗/消费
    "159992.SZ": "创新药ETF", "512010.SH": "医药ETF", "159883.SZ": "医疗器械ETF",
    "515170.SH": "食品ETF华夏", "512690.SH": "酒ETF鹏华", "159867.SZ": "畜牧ETF",
    "561120.SH": "家电ETF富国", "159855.SZ": "影视ETF",
    # 金融/地产
    "512800.SH": "银行ETF华宝", "159892.SZ": "非银ETF", "159841.SZ": "证券ETF",
    "512880.SH": "证券ETF国泰", "512200.SZ": "房地产ETF",
    # 周期/资源
    "159713.SZ": "稀土ETF富国", "159980.SZ": "有色ETF大成", "515210.SH": "钢铁ETF国泰",
    "515220.SH": "煤炭ETF国泰", "159870.SZ": "化工ETF", "516750.SH": "建材ETF富国",
    "561760.SH": "油气ETF博时", "561360.SH": "石油ETF国泰",
    # 商品/避险
    "518880.SH": "黄金ETF", "159934.SZ": "黄金ETF易方达", "159985.SZ": "豆粕ETF华夏",
    # 债券
    "511010.SH": "国债ETF国泰", "511260.SH": "十年国债ETF国泰", "511090.SH": "30年国债ETF鹏扬",
    "511030.SH": "公司债ETF平安", "511360.SH": "短融ETF海富通", "511380.SH": "可转债ETF博时",
    "159396.SZ": "信用债ETF博时",
}
BENCHMARK = "510300.SH"


@dataclass
class Params:
    init_cash: float = 1_000_000
    vol_window: int = 250
    corr_window: int = 500
    momentum_window: int = 20
    vol_low: float = 0.08
    vol_high: float = 0.28
    n_corr: int = 5
    n_momentum: int = 2
    rebalance_freq: str = "W-FRI"
    transaction_cost: float = 0.0005  # 单边万5（A股ETF佣金+轻微滑点的合理估计）
    min_hist_days: int = 90  # 新上市 ETF 最少历史天数（与 vol min_periods 对齐，允许上市 ~4 月即入池）


# ---------------- 数据层（akshare） ---------------- #
def fetch_one(ts_code: str, start: str, end: str, force: bool = False) -> pd.DataFrame:
    """拉取单只 ETF 后复权收盘价。ts_code 形如 510300.SH / 159915.SZ。"""
    cache_file = CACHE_DIR / f"{ts_code}.parquet"
    if cache_file.exists() and not force:
        df = pd.read_parquet(cache_file)
        if df.index.min() <= pd.Timestamp(start) and df.index.max() >= pd.Timestamp(end) - pd.Timedelta(days=10):
            return df.loc[start:end]

    symbol = ts_code.split(".")[0]
    exch = ts_code.split(".")[1].lower()
    s = start.replace("-", "")
    e = end.replace("-", "")

    df = None
    # 尝试 1: 东财后复权
    for attempt in range(2):
        try:
            tmp = ak.fund_etf_hist_em(symbol=symbol, period="daily",
                                      start_date=s, end_date=e, adjust="hfq")
            if tmp is not None and not tmp.empty:
                df = tmp.rename(columns={"日期": "trade_date", "收盘": "close"})
                break
        except Exception:
            time.sleep(1.5)
    # 尝试 2: Sina（不复权，A 股 ETF 分红对价格影响小）
    if df is None or df.empty:
        try:
            tmp = ak.fund_etf_hist_sina(symbol=f"{exch}{symbol}")
            if tmp is not None and not tmp.empty:
                tmp = tmp.rename(columns={"date": "trade_date"})
                df = tmp[["trade_date", "close"]]
        except Exception as err:
            print(f"[error] {ts_code} sina 也失败: {err}")
            return pd.DataFrame()

    if df is None or df.empty:
        print(f"[warn] {ts_code} 无数据（可能未上市）")
        return pd.DataFrame()

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").set_index("trade_date")[["close"]].astype(float)
    df.to_parquet(cache_file)
    return df.loc[start:end]


def fetch_all(start: str, end: str, force: bool = False) -> pd.DataFrame:
    """拉取全池+基准，返回宽表（列=ts_code，索引=交易日，值=后复权收盘价）。"""
    frames = {}
    codes = list(ETF_POOL.keys())
    if BENCHMARK not in codes:
        codes.append(BENCHMARK)
    for code in tqdm(codes, desc="fetch"):
        d = fetch_one(code, start, end, force=force)
        if not d.empty:
            frames[code] = d["close"]
    prices = pd.concat(frames, axis=1).sort_index()
    # 所有 ETF 对齐到交易日并集；前值填充做停牌处理
    prices = prices.ffill(limit=5)
    return prices


# ---------------- 因子层 ---------------- #
def annual_volatility(returns: pd.DataFrame, window: int) -> pd.DataFrame:
    return returns.rolling(window, min_periods=max(60, window // 2)).std() * np.sqrt(252)


def mean_abs_correlation(returns: pd.DataFrame, window: int, asof: pd.Timestamp,
                         universe: list[str]) -> pd.Series:
    """对 universe 内每只 ETF 计算与其它的平均绝对相关系数。"""
    sub = returns.loc[:asof, universe].tail(window).dropna(how="all", axis=1)
    if sub.shape[1] < 2:
        return pd.Series(dtype=float)
    corr = sub.corr().abs()
    n = corr.shape[1]
    # 扣掉对角线 1.0 后平均
    return (corr.sum(axis=0) - 1.0) / (n - 1)


def momentum_score(prices: pd.DataFrame, asof: pd.Timestamp, window: int,
                   universe: list[str]) -> pd.Series:
    """对每只 ETF: ln(P) 对 t 做一元回归，score = (exp(β·252)-1) * R²。"""
    sub = prices.loc[:asof, universe].tail(window)
    scores = {}
    t = np.arange(window, dtype=float)
    t_mean = t.mean()
    for col in sub.columns:
        y = sub[col].values
        if np.isnan(y).sum() > window * 0.2 or (y <= 0).any():
            continue
        y = np.log(y)
        # 简单 OLS
        y_mean = y.mean()
        cov = ((t - t_mean) * (y - y_mean)).sum()
        var_t = ((t - t_mean) ** 2).sum()
        beta = cov / var_t
        alpha = y_mean - beta * t_mean
        y_fit = alpha + beta * t
        ss_res = ((y - y_fit) ** 2).sum()
        ss_tot = ((y - y_mean) ** 2).sum()
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        if r2 < 0:
            r2 = 0
        scores[col] = (np.exp(beta * 252) - 1) * r2
    return pd.Series(scores)


def inv_vol_weights(vols: pd.Series) -> pd.Series:
    inv = 1.0 / vols
    return inv / inv.sum()


# ---------------- 回测引擎 ---------------- #
@dataclass
class BacktestResult:
    nav: pd.Series
    benchmark_nav: pd.Series
    rebalances: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def run_backtest(prices: pd.DataFrame, start: str, end: str, p: Params) -> BacktestResult:
    prices = prices.loc[start:end].copy()
    if prices.empty:
        raise ValueError("价格区间为空")
    if BENCHMARK not in prices.columns:
        raise ValueError(f"基准 {BENCHMARK} 缺失")
    etf_cols = [c for c in prices.columns if c != BENCHMARK]
    etf_prices = prices[etf_cols]
    returns = np.log(etf_prices / etf_prices.shift(1))

    # 交易日列表
    cal = prices.index
    # 调仓日 = 每周五（或当周最后一个交易日）
    weekly = prices.resample(p.rebalance_freq).last().index
    rebalance_dates = [d for d in weekly if d in cal]

    cash = p.init_cash
    positions: dict[str, float] = {}  # ts_code -> shares
    nav_series = pd.Series(index=cal, dtype=float)
    rebalances = []

    min_hist = p.min_hist_days  # 放宽到 90 日，让新上市 ETF 上市即入池（vol 用 min_periods=90 保护）
    for today in cal:
        # 先结算当日组合市值
        mv = cash + sum(sh * prices.loc[today, c] for c, sh in positions.items()
                         if not np.isnan(prices.loc[today, c]))
        nav_series.loc[today] = mv

        if today not in rebalance_dates:
            continue

        # 动态 universe：该日前有 ≥ min_hist 根K线且当日价格不是 NaN
        hist_len = etf_prices.loc[:today].notna().sum()
        universe = [c for c in etf_cols
                    if hist_len[c] >= min_hist and not np.isnan(prices.loc[today, c])]
        if len(universe) < p.n_corr + 2:
            continue

        # 1) 波动率过滤
        vol = annual_volatility(returns[universe], p.vol_window).loc[today]
        vol = vol.dropna()
        vol = vol[(vol >= p.vol_low) & (vol <= p.vol_high)]
        if len(vol) < p.n_corr:
            continue

        # 2) 平均绝对相关性 → 取最低 n_corr 只
        mean_corr = mean_abs_correlation(returns, p.corr_window, today, list(vol.index))
        if mean_corr.empty:
            continue
        candidates = mean_corr.nsmallest(p.n_corr).index.tolist()

        # 3) 动量打分 → 取最高 n_momentum 只
        mom = momentum_score(etf_prices, today, p.momentum_window, candidates)
        mom = mom.dropna()
        if len(mom) < 1:
            continue
        top = mom[mom > 0].nlargest(p.n_momentum).index.tolist()
        if not top:  # 无正动量 → 空仓
            # 清掉持仓
            for c, sh in list(positions.items()):
                px = prices.loc[today, c]
                if np.isnan(px):
                    continue
                cash += sh * px * (1 - p.transaction_cost)
                del positions[c]
            rebalances.append({"date": today, "target": {}, "nav": mv})
            continue

        # 4) 波动率倒数加权
        w = inv_vol_weights(vol.loc[top])
        target = {c: float(w[c]) for c in top}

        # 先卖出不在 target 的
        for c, sh in list(positions.items()):
            if c in target:
                continue
            px = prices.loc[today, c]
            if np.isnan(px):
                continue
            cash += sh * px * (1 - p.transaction_cost)
            del positions[c]

        # 重新计算组合现值，再把 in-target 的调整到目标权重
        portfolio_value = cash + sum(sh * prices.loc[today, c]
                                     for c, sh in positions.items()
                                     if not np.isnan(prices.loc[today, c]))
        for c, w_target in target.items():
            px = prices.loc[today, c]
            if np.isnan(px) or px <= 0:
                continue
            target_value = portfolio_value * w_target
            current_value = positions.get(c, 0.0) * px
            diff = target_value - current_value
            if diff > 0:  # 买入
                buy_amount = diff / (1 + p.transaction_cost)
                shares = buy_amount / px
                positions[c] = positions.get(c, 0.0) + shares
                cash -= buy_amount * (1 + p.transaction_cost)
            elif diff < 0:  # 卖出
                sell_amount = -diff
                shares = sell_amount / px
                positions[c] = positions.get(c, 0.0) - shares
                cash += sell_amount * (1 - p.transaction_cost)

        rebalances.append({"date": today, "target": target, "nav": portfolio_value})

    nav_series = nav_series.dropna() / p.init_cash
    bench_nav = prices[BENCHMARK] / prices[BENCHMARK].iloc[0]
    bench_nav = bench_nav.reindex(nav_series.index)

    metrics = compute_metrics(nav_series, bench_nav)
    return BacktestResult(nav=nav_series, benchmark_nav=bench_nav,
                          rebalances=rebalances, metrics=metrics)


def compute_metrics(nav: pd.Series, bench: pd.Series) -> dict:
    ret = nav.pct_change().dropna()
    bench_ret = bench.pct_change().dropna()
    n = len(ret)
    annual = (nav.iloc[-1] / nav.iloc[0]) ** (252 / n) - 1 if n > 0 else 0
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
    dd = (nav / nav.cummax() - 1).min()
    calmar = annual / abs(dd) if dd != 0 else 0
    bench_annual = (bench.iloc[-1] / bench.iloc[0]) ** (252 / n) - 1 if n > 0 else 0
    excess = annual - bench_annual
    return {
        "annual_return": round(annual, 4),
        "benchmark_annual": round(bench_annual, 4),
        "excess_return": round(excess, 4),
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(dd, 4),
        "calmar": round(calmar, 2),
        "bench_max_drawdown": round((bench / bench.cummax() - 1).min(), 4),
    }


# ---------------- 绘图与输出 ---------------- #
def plot_nav(result: BacktestResult, title: str, save_path: Path):
    plt.figure(figsize=(12, 6))
    plt.plot(result.nav.index, result.nav.values, label="策略净值", linewidth=2)
    plt.plot(result.benchmark_nav.index, result.benchmark_nav.values,
             label="沪深300基准", linestyle="--")
    plt.title(title, fontsize=14)
    plt.xlabel("日期")
    plt.ylabel("净值")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(save_path, dpi=120)
    plt.close()


def save_outputs(result: BacktestResult, tag: str):
    pd.DataFrame({"strategy": result.nav, "benchmark": result.benchmark_nav}) \
        .to_csv(RESULTS_DIR / f"nav_{tag}.csv", encoding="utf-8-sig")
    # 调仓表
    rows = []
    for r in result.rebalances:
        for c, w in (r["target"] or {"(空仓)": 0}).items():
            rows.append({"date": r["date"], "ts_code": c,
                         "name": ETF_POOL.get(c, c), "weight": w, "nav": r["nav"]})
    pd.DataFrame(rows).to_csv(RESULTS_DIR / f"rebalances_{tag}.csv",
                              index=False, encoding="utf-8-sig")
    pd.Series(result.metrics).to_csv(RESULTS_DIR / f"metrics_{tag}.csv",
                                     header=["value"], encoding="utf-8-sig")
    plot_nav(result, f"A股ETF轮动策略净值曲线 ({tag.upper()})",
             RESULTS_DIR / f"nav_{tag}.png")


# ---------------- 主入口 ---------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["is", "oos", "full"], default="full")
    ap.add_argument("--fetch", action="store_true", help="强制重新下载")
    args = ap.parse_args()

    ranges = {
        "is": ("2013-06-01", "2019-12-31"),   # 多预留 vol+corr 窗口
        "oos": ("2018-06-01", "2026-04-21"),  # 预留窗口，回测只从 2020 开始
        "full": ("2013-06-01", "2026-04-21"),
    }
    fetch_start, fetch_end = ranges[args.mode]
    prices = fetch_all(fetch_start, fetch_end, force=args.fetch)
    print(f"价格矩阵: {prices.shape}, {prices.index.min().date()} ~ {prices.index.max().date()}")

    p = Params()
    bt_windows = {
        "is": ("2015-01-01", "2019-12-31"),
        "oos": ("2020-01-01", "2026-04-21"),
        "full": ("2015-01-01", "2026-04-21"),
    }

    if args.mode == "full":
        for tag in ["is", "oos"]:
            s, e = bt_windows[tag]
            print(f"\n=== {tag.upper()} {s} ~ {e} ===")
            r = run_backtest(prices, s, e, p)
            print(r.metrics)
            save_outputs(r, tag)
    else:
        s, e = bt_windows[args.mode]
        print(f"\n=== {args.mode.upper()} {s} ~ {e} ===")
        r = run_backtest(prices, s, e, p)
        print(r.metrics)
        save_outputs(r, args.mode)


if __name__ == "__main__":
    main()
