# HighTempTation 高阶优化包（第二波）

> 订单簿微观结构 · 跨市场/跨期套利 · ML 残差学习 · 订单状态机 FSM · Walk-Forward 回测 · 混沌工程

本包在既有 v6/v7 实盘策略（OBI/流动性窗口/偏差校准/凯利仓位/健康检查）之上，
落地用户提出的第二波高阶优化。所有模块**无重依赖**（仅 numpy/scipy/sklearn 可选），
LightGBM/XGBoost 未安装时自动降级，可独立运行、可单测、可逐步接入实盘。

```
tools/hightemptation_live/highopt/
├── __init__.py          # 包导出
├── microstructure.py    # ① 订单簿微观结构
├── arbitrage.py         # ② 跨市场/跨期套利
├── ml_residual.py       # ③ ML 残差学习
├── order_fsm.py         # ④ 订单状态机 FSM
├── walk_forward.py      # ⑤ Walk-Forward 回测
├── chaos.py             # ⑥ 混沌工程
├── runner.py            # 一键自检（合成数据全模块验证）
└── HIGHOPT_README.md    # 本文档
```

---

## ① 订单簿微观结构 `microstructure.py`

| 能力 | 实现 | 说明 |
|------|------|------|
| LOB 形状建模 | `OrderBookShape` | 对 bid/ask 档位拟合指数衰减（size≈a·e^(−b·d)）与累计深度线性斜率 |
| 深度斜率检测 | `OrderBookShape.depth_slope()` | 累计深度 vs 距中间价距离的最小二乘斜率；近端 ±5¢ 流动性判 THICK/NORMAL/THIN |
| Square-Root Law | `SquareRootLawImpact` | ΔP = k·σ·√(Q/V)，含 `max_qty_for_budget` 反解与 `calibrate_k` 历史校准 |
| VPIN 逆向选择 | `VPINFilter` | 成交量同步知情交易概率（tick 规则分类 + 等量桶），高 VPIN 过滤开仓 |
| 开仓闸门 | `MicrostructureGate` | 深度/形状/冲击/VPIN 四合一过滤，风格对齐 v6 的 `_try_open_position` |

```python
gate = MicrostructureGate(vpin=VPINFilter())
ok, reasons, metrics = gate.check(snapshot, qty=50, sigma=0.02, volume=50000)
```

## ② 跨市场/跨期套利 `arbitrage.py`

| 能力 | 实现 | 说明 |
|------|------|------|
| 桶分割套利 | `BucketPartitionArb` | 同 (城市,日期) 全部桶 ΣYES 应 ≈ 1，偏离超成本带 → 全买/全卖锁定 |
| 相邻桶 Put-Call Parity | `AdjacentBucketParity` | 每桶 YES+NO≈1；合并桶可加性 YES[a,b]+YES[b,c]≈YES[a,c]（三腿套利） |
| 期限结构 | `TermStructureMonitor` | 同标的多到期日价格曲线斜率，检测倒挂（backwardation）/升水（contango） |
| 多平台价差 | `MultiPlatformSpread` | 跨平台最低卖价 vs 最高买价，价差>双边成本+Gas → 低买高卖 |
| 汇总扫描 | `ArbitrageScanner` | 一键对桶组/期限/多平台运行全部检查 |

> ⚠️ 理论修正：相邻桶 YES 价格本身**不**满足单调性（分布可在中间桶取峰），
> 旧式“低桶 yes > 高桶 yes 即套利”是伪约束，本实现不采用。

## ③ ML 残差学习 `ml_residual.py`

| 能力 | 实现 | 说明 |
|------|------|------|
| 物理模型残差修正 | `MLResidualLearner` | target = actual − forecast_mu；预测残差后修正 mu → 修正桶概率 |
| 后端自动降级 | `_detect_backend` | lightgbm → xgboost → sklearn GradientBoosting → 均值回退 |
| 特征工程 | `ResidualFeatureBuilder` | mu/σ/集合离散度/日期/城市/偏差/偏度，训练与推理特征一致 |
| 在线学习 | `online_update()` | 滚动窗口（FIFO，默认 500 条）+ 全量重训，适应季节漂移 |
| NLP 增强 | `NLPEnhancement` | 新闻标题关键词情绪打分（中英双语词表 + 否定翻转），±1°C 微调预报 |

```python
learner = MLResidualLearner()          # 自动探测后端
learner.fit(rows)                      # rows 含 mu/sigma/actual...
corr = learner.predict(row)            # 残差修正量
corrected_mu = row["mu"] + corr
learner.online_update(new_rows)        # 在线学习
```

## ④ 订单状态机 FSM `order_fsm.py`

| 能力 | 实现 | 说明 |
|------|------|------|
| Client Order ID 幂等 | `OrderFSM.submit(intent_key=...)` | 同一意图重复提交返回同一订单，绝不双开 |
| 幽灵订单防护 | `UNKNOWN` 态 + `resolve_ghost()` | 提交无响应/超时 → UNKNOWN → 重查/撤单/告警，禁止盲目重发 |
| 部分成交处理 | `_apply_fill()` | filled_qty 累计 + 移动平均价，剩余 < ε → FILLED |
| 仓位对账 | `PositionReconciler` | 本地成交累计 vs 交易所持仓，差异 WARN/CRITICAL + 回调 |
| 适配器 | `ExchangeAdapter` 接口 + `SimExchangeAdapter` | 实盘实现 Polymarket CLOB / OKX REST；模拟器可注入 fail/ghost/延迟 |

状态图：`NEW → SUBMITTED ⇄ PARTIALLY_FILLED → FILLED`，`REJECTED/CANCELLED/EXPIRED` 终结，`UNKNOWN` 为幽灵风险态。

## ⑤ Walk-Forward 回测 `walk_forward.py`

| 能力 | 实现 | 说明 |
|------|------|------|
| Point-in-Time 数据 | `PointInTimeLoader` | ts≤t 视图二分切片 + `assert_no_lookahead` 前视偏差断言 |
| Walk-Forward | `WalkForwardBacktester` | 滚动 训练/验证/测试 折叠，每折独立拟合模型再评估测试集 |
| 成本敏感性 | `CostSensitivityAnalyzer` | 扫描 手续费×滑点×Gas 网格，线性插值求盈亏平衡手续费 |
| 夏普/Calmar 稳定性 | `StabilityMetrics` | 滚动夏普/Calmar，输出均值/标准差/最差折叠/正收益折叠占比 + STABLE/UNSTABLE 判定 |

```python
report = WalkForwardBacktester(n_folds=6, model_factory=make_model).run(records)
report.stability   # {'sharpe': {...}, 'calmar': {...}, 'verdict': ...}
```

## ⑥ 混沌工程 `chaos.py`

| 能力 | 实现 | 说明 |
|------|------|------|
| 故障注入 | `FaultInjector` | API 延迟 / 404 / 500 / 丢包（ConnectionError）/ 余额突变（递归改写返回体） |
| 熔断器 | `CircuitBreaker` | CLOSED → OPEN（连续失败阈值）→ HALF_OPEN（冷却后试探）→ CLOSED；OPEN 时快速失败 + 降级回调 |
| 场景验证 | `ChaosVerifier` | 5 场景断言矩阵：延迟不误熔断、404/500/丢包触发熔断、余额突变被检出 |

```python
verifier = ChaosVerifier(failure_threshold=3, cooldown=2.0)
report = await verifier.run_all()      # {'latency': {...}, 'http_500': {...}, ...}
```

## 数据持久化

`db_manager.py` 新增 5 张表（`CREATE TABLE IF NOT EXISTS`，向后兼容）：

- `fsm_orders` — 订单台账（client_order_id 唯一约束 = 幂等落库）
- `microstructure_snapshots` — 微观结构指标快照
- `arbitrage_signals` — 套利信号日志
- `ml_residuals` — 残差学习记录（含特征 JSON）
- `chaos_events` — 混沌事件与熔断状态

## 自检

```bash
cd tools/hightemptation_live
python3 -m highopt.runner                # 全模块合成数据验证，退出码 0/1
python3 -m highopt.runner --db demo.db   # 同时写入 SQLite 演示表
```

当前环境：numpy/scipy/sklearn 可用 → 残差学习自动走 `sklearn_gbr` 后端；
安装 `lightgbm`/`xgboost` 后自动切换，无需改代码。

## 接入实盘建议（顺序）

1. **FaultInjector + CircuitBreaker 包裹交易所适配器**（`order_fsm.ExchangeAdapter` 实现），
   混沌场景纳入健康检查 `health_check.py`；
2. `OrderFSM` 替换 `hightemptation_live_v6.py` 中的裸下单路径，开仓记录 `fsm_orders`；
3. `MicrostructureGate` 并入 `_try_open_position` 过滤链（v6 已有 OBI，可叠加 VPIN/冲击）；
4. `ArbitrageScanner` 在扫描循环中对同城市桶组做周期检查，信号写 `arbitrage_signals`；
5. `MLResidualLearner` 用 `calibration` 表历史 (model_prob, realized_prob) 训练，修正 `bucket_prob`；
6. `WalkForwardBacktester` 对回测框架（`tools/backtest/hightemptation_full_v3.py`）做折叠化改造。
