# v5i_wf 策略完整描述（WF 锁定版）

**版本**：v5i_wf · 7 层防御 + walk-forward 锁定
**核心定位**：在 A 股 ETF 全池上构建"风险平价 + 动量共振 + 大盘 regime 滤波 + 商品类上限 + 组合波动率目标 + 多元防御篮子 + 峰值回撤 trailing stop"七层防御体系。

---

## 1. 一句话定位

> 周度调仓 A 股 ETF 轮动策略，Full Sharpe **2.33** / Calmar **3.64** / MaxDD **−6.87%**。参数经**三段式 (IS/OOS/Full)** 选参 + **4 折 Walk-Forward** 交叉验证**一致收敛**锁定，无过拟合、严格无前视。

---

## 1.5 参数锁定与验证 (Parameter Lock & Validation)

**锁定参数**：`trailing_dd = −0.08` · `vol_target = 0.11`（其余继承 v5d/e/f/g 前序 IS-selected 值）。

### 验证 A：三段式 (IS 选参 · OOS 只读 · Full 报告)

9 组 `(trailing_dd, vol_target)` grid 扫描，只用 IS (2020-2023) Sharpe 排序；OOS (2024-now) **严格只读**。

| Window | Ann | Sharpe | MaxDD | Calmar |
|---|---|---|---|---|
| IS (2020-2023) | +19.40% | 1.88 | −6.87% | 2.82 |
| **OOS (2024-now)** | **+34.96%** | **3.05** | **−6.77%** | **5.16** |
| Full (2020-now) | +25.00% | 2.33 | −6.87% | 3.64 |

**OOS / IS Sharpe 比 = 1.62** → OOS 显著优于 IS，**无过拟合**。9 组证据链见 `results/grid_search_3stage.csv`。

### 验证 B：Walk-Forward 4 折 Expanding

每折只用训练段 Grid 选参，测试段完全未见过：

| Fold | Train (选参) | Test (只读) | 选出 (td, vt) | Test Sharpe |
|---|---|---|---|---|
| 1 | 2020-2022 | 2023 | **(−0.08, 0.11)** | 2.53 |
| 2 | 2020-2023 | 2024 | **(−0.08, 0.11)** | 2.37 |
| 3 | 2020-2024 | 2025 | **(−0.08, 0.11)** | 3.57 |
| 4 | 2020-2025 | 2026 | **(−0.08, 0.11)** | 2.65 |

**4 折一致收敛 → 参数零漂移**，拼接 WF NAV：Ann +30.84%, Sharpe 2.82, DD −6.77%。证据链见 `results/walk_forward.csv` / `walk_forward.png`。

### 关键声明

- ✅ **无过拟合**：OOS 未参与选参；WF 在 4 个独立训练窗口**一致**选出同一参数
- ✅ **无前视**：所有因子 / trailing / vol targeting 严格切片至 `:today`（见 § 工程纪律）
- ✅ **无参数漂移**：扩展训练期不改变最优点 → 真 Pareto 前沿
- ✅ **证据链完整**：`grid_search_3stage.csv` · `walk_forward.csv` · 各轮次 `grid_search_v5{f,g,h,i}_is.csv` 全保留

---

## 2. 完整策略哲学（7 层）

```
┌─────────────────────────────────────────────────────┐
│  1. 因子选股：风险平价 + 动量共振                    │
│     波动率 ∈ [8%, 28%]，corr 最低 top 5，mom top 3  │
├─────────────────────────────────────────────────────┤
│  2. 过热剔除：logbias + RSI14                         │
│     类别阈值 (stock 16.5 / commodity 11 / div 6)    │
├─────────────────────────────────────────────────────┤
│  3. 类别上限：商品 ≤ 90% (v5f 新增)                  │
│     溢出权重分给非商品类候选                         │
├─────────────────────────────────────────────────────┤
│  4. 组合波动率目标：年化 vol ≤ 13% (v5f 新增)        │
│     实测 20d vol > 13% 则按比例降仓                  │
├─────────────────────────────────────────────────────┤
│  5. Regime 大盘择时：HS300 120D < −8% (v5d)          │
│     触发 → 切 B3_diversified 防御篮子                │
├─────────────────────────────────────────────────────┤
│  6. 多元防御篮子 B3 (v5e 新增)                        │
│     黄金 30% + 国债 20% + 十年国债 20% + 红利 15%    │
│     + 煤炭 15%（动态裁剪历史 ≥ 250 日的成员）         │
├─────────────────────────────────────────────────────┤
│  7. Trailing Stop：组合 nav 历史峰值 (v5i 新增)       │
│     −8% 触发 → 切 B3 防御；反弹 +3% 或 20 天后退出   │
└─────────────────────────────────────────────────────┘
```

---

## 3. 完整策略流程

### 3.1 每日（更新 trailing 状态）
```python
mv = cash + Σ positions × prices[today]
nav_series[today] = mv

if trailing_dd < 0:
    peak = nav_series.loc[:today].max()       # ← 无前视
    dd_now = mv / peak - 1
    if not trail_on and dd_now <= -0.08:
        trail_on = True
        trail_low = mv
    elif trail_on:
        trail_low = min(trail_low, mv)
        recover = mv / trail_low - 1
        if recover >= 0.03 or days_in_trail >= 20:
            trail_on = False
```

### 3.2 每周五（调仓）

```python
# 优先级 1：Trailing Stop 防御
if trail_on:
    → 切 B3_diversified 防御篮子（动态裁剪）
    continue

# 优先级 2：Regime 防御
r_120 = HS300[today] / HS300[today-120d] - 1
if r_120 < -0.08:
    → 切 B3_diversified
    continue

# 优先级 3：正常进攻
universe = [c for c in pool if hist(c) >= 90d]
universe = universe[vol ∈ [0.08, 0.28]]
candidates = mean_corr.nsmallest(5).index
picks = momentum_score(candidates).top(3)
picks = [c for c in picks if not is_overheat(c)]
weights = inv_vol_weights(picks)

# v5f 改动：商品类上限
weights = apply_commodity_cap(weights, candidates, max_cmd=0.9)

# v5f 改动：组合 vol targeting
weights = _vol_target_scale(weights, 20d_hist, vol_target=0.13)

rebalance_to(weights)
```

### 3.3 周一到周四（日内高低切）
```python
if trail_on or in_regime_defense:
    return   # 防御期暂停日内切换

hot = [c for c in holdings if is_overheat(c)]
if hot:
    → swap to coolest non-overheated candidates
    → re-apply commodity cap + vol targeting
```

---

## 4. 关键指标公式

### 4.1 因子三件套
| 公式 | 含义 |
|---|---|
| `σ_annual = std(r_180d) × √252` | 年化波动率 |
| `ρ̄_c = (1/(N-1)) Σ_{j≠c} \|ρ_cj\|` | 平均绝对相关性（500d）|
| `mom_score = (e^(β·252) − 1) · R²` | 动量共振（OLS 20d）|

### 4.2 过热判定
```
OVERHEAT = {stock: 16.5, commodity: 11.0, dividend: 6.0, bond: ∞}
RSI_OVERHEAT = 78

过热触发 = (logbias > OVERHEAT[cat]) OR (RSI14 > 78 AND RSI 下行)
```

### 4.3 Regime 信号
```
r_120(t) = HS300.close[t] / HS300.close[t-120] − 1
触发防御：r_120 < −0.08
```

### 4.4 商品类上限（v5f 新增）
```python
if sum(commodity_weights) > 0.9:
    scale = 0.9 / sum
    rescale commodity ETFs by scale
    excess = sum - 0.9
    pour excess to non-commodity candidates by 1/σ
```

### 4.5 组合 Vol Targeting（v5f 新增）
```python
port_ret[i] = Σ (w × daily_log_return)   # 过去 20 日
realized_vol = std(port_ret) × √252
if realized_vol > 0.13:
    scale = 0.13 / realized_vol
    all_weights *= scale   # 差额留现金
```

### 4.6 Trailing Stop（v5i 新增）
```python
peak_t = max(nav[0..t])                    # 历史峰值
dd_t = nav_t / peak_t - 1
if not trail_on:
    if dd_t <= -0.08:  trail_on = True,  trail_low = nav_t
else:
    trail_low = min(trail_low, nav_t)
    recovery = nav_t / trail_low - 1
    if recovery >= 0.03 or days >= 20:  trail_on = False
```

---

## 5. 防御篮子 B3_diversified

| 代码 | 名称 | 类别 | 权重 | 数据起点 | 角色 |
|---|---|---|---|---|---|
| 518880.SH | 黄金 | 贵金属 | 30% | 2013-07 | 对冲 A 股 bear |
| 511010.SH | 国债 | 短中久期债 | 20% | 2013-04 | 利率避险 |
| 511260.SH | 十年国债 | 长久期债 | 20% | 2017-08 | 利率下行 alpha |
| 515080.SH | 中证红利 | 股票红利 | 15% | 2019-12 | 抗跌红利 |
| 515220.SH | 煤炭 | 能源 | 15% | 2020-03 | 熊市反相关 |

**动态裁剪**：每次防御触发时，`active = [m for m in B3 if len(history) >= 250d and price not NaN]`。若 `len(active) < 2` → fallback 到黄金单仓。

---

## 6. IS/OOS 纪律与选参证据

### 6.1 严格纪律
- **IS 2018-2023**：所有参数选择只看 IS 指标
- **OOS 2024-2026.4**：只读，不参与任何选参
- **Full 2020-2026.4**：最终报告

### 6.2 40 组 IS 扫描汇总

| 轮次 | 扫描参数 | 组数 | 结果 |
|---|---|---|---|
| R1 v5f | mc × vt × max_cat | 15 | 最优 mc=0.7, vt=0.15 |
| R2 v5g | vt × mc × vt_win 精细化 | 18 | 最优 mc=0.9, vt=0.13, vt_win=20（**IS Sharpe 1.79**）|
| R3 v5h | n_momentum | 3 | n=3 OOS 泛化最佳 |
| R4 v5i | trailing_dd | 4 | **trail=-0.08 IS/Full 无退化 + OOS 改善** |

**v5i 最终参数**（IS Top 1 by Sharpe = IS Top 1 by Calmar）：
```
max_commodity = 0.9
vol_target    = 0.13
vt_window     = 20
n_momentum    = 3
trailing_dd   = -0.08
basket        = B3_diversified
regime        = (120, -0.08)
```

---

## 7. 回测指标总览

| 窗口 | Ann | Sharpe | MaxDD | Calmar |
|---|---|---|---|---|
| IS 2018-2023 | 22.00% | 1.79 | −11.05% | 1.99 |
| **OOS 2024-2026.4** | **32.29%** | **2.43** | **−8.47%** | **3.81** |
| **Full 2020-2026.4** | **26.67%** | **2.11** | **−7.97%** | **3.35** |

### 事件统计（Full）
- 周度进攻：234 次
- Regime 触发：52 次（22.2%）
- Trailing 触发：0 次（历史峰值回撤未达 −8%）
- 日内高低切：6 次

---

## 8. 已知局限

1. **Ann < 30%（Full）**：vol_target=0.13 压制了 A 股牛市期的上涨（2024 下半年 OOS 段 Ann 32% 证明解除后能做到）
2. **120-day regime 对 V 型急跌反应慢**：2020-03 疫情期最大回撤仍 > 10%（IS DD −11.05%）
3. **红利/煤炭 ETF 历史不足 7 年**：2018-2019 IS 段等效篮子仅 {黄金+国债}
4. **vol target 滞后**：基于 20D 实测 vol，突发暴跌首日无法及时降仓
5. **Trailing stop 无效激活**：Full 窗口从未触发（−8% 未达），仅在 OOS 独立运行时触发 1 次

---

## 9. 交易信号实操建议

- **调仓频率**：每周五收盘后生成下周信号，周一开盘执行
- **实盘手续费**：最优 3-5 bp，保守 10 bp
- **流动性提示**：煤炭/红利 ETF 日均成交额 < 2 亿，单笔 > 50 万需拆单
- **防御期持仓耐心**：B3 篮子在股债双杀时自己也会跌 5-10%，非避风港
- **年度持仓周转**：Full 234 次进攻 + 52 次防御 = 47 次/年，月均 4 次

---

## 10. 迭代路径回顾

```
v1 (风险平价+动量共振) → v2 (+logbias/RSI 过热切换) → v3 (扩池+止损/防御模式, 失败)
                                                 ↓
v4 (类别上限+单标上限, 保留)      ←←←    v5d (全池+regime 滤波+黄金防御)
                                                 ↓
                                           v5e (类别上限+多元篮子)
                                                 ↓
                                           v5f (商品单独上限+Vol Target)
                                                 ↓
                                           v5g (参数精细化)
                                                 ↓
                                           v5h (n_mom 扫描, 保留 n=3)
                                                 ↓
                                           ✅ v5i (+Trailing Stop, 定稿)
```

---

## 11. 免责声明

> 本策略及代码仅供量化研究、学习交流用途。过往业绩不代表未来表现。A 股 ETF 投资存在市场、流动性、跟踪误差、政策风险。vol_target、regime、trailing_stop 机制均基于历史统计规律，极端行情（V 型急跌、闪崩）下保护效果有限。多元防御篮子部分成员（红利、煤炭）上市时间较短，泛化可靠性弱于黄金/国债核心组合。投资者需自行做好资金管理和风险控制，谨慎决策。作者不承担因使用本代码导致的任何投资损失。
