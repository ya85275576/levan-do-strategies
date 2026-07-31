# 5分钟套利子模块整合说明 (Benjam1nCup)

> 对应任务: 整合 Benjam1nCup/Polymarket-trading-bot-python-V2 的
> 5分钟套利、狙击、动量、阶梯策略到 HighTempTation。

## 背景: 为什么是"本地子模块"而非 git submodule

上游仓库 `Benjam1nCup/Polymarket-trading-bot-python-V2` 目前**只有 README
营销页，没有任何源代码**（2026-07-31 核验，`main` 分支仅 1 个文件）。
因此无法作为 git submodule 指向可用的上游代码。本仓库按 README 中
12 个机器人的策略文档（Endcycle Sniper / Liquidity Momentum / Arbitrage /
Ladder / Stair 等）独立实现了四大类策略，目录结构即"子模块"形态：

```
HighTempTation/
├── polymarket_5min_bot/            # ← 子模块 (独立可运行)
│   ├── config.py                   # PM5_* 环境变量配置
│   ├── markets.py                  # 真实市场扫描 + 模拟市场回退
│   ├── clob.py                     # 轻量 CLOB 客户端 (DRY_RUN 模拟撮合)
│   ├── obi.py                      # Order Book Influence 计算
│   ├── spot_price.py               # 现货价轮询 (Coinbase)
│   ├── engine.py                   # FiveMinEngine 主循环
│   └── strategies/                 # base/arbitrage/sniper/momentum/ladder
├── adapters/
│   └── polymarket_5min_adapter.py  # ← 统一适配层
├── account_manager.py              # ← 统一账户管理 (单例)
├── shared_risk.py                  # ← 共享风控规则 (单例)
└── scripts/verify_5min_integration.py  # DRY_RUN 验证 (19 项)
```

## 四大策略

### 🧲 套利 (ARB) — 互补套利
- 从订单簿读 YES/NO 中间价，组合成本 = YES + NO
- 组合成本 ≤ `PM5_ARB_COMBINED_TARGET` (0.95) 且 edge ≥ `PM5_ARB_MIN_EDGE` (1.5%) 时：
  - Buy1 = 买高概率侧（价格高的一侧）
  - Buy2 = 立即买对面（`PM5_ARB_LEG2_MAX_WAIT` 内），组合锁定 (1 − combined) 收益
- 结算赎回 $1.00 → 每周期吃 3~5 分价差

### 🎯 狙击 (SNIPER) — Endcycle Sniper
- 结算前 `PM5_SNIPER_WINDOW_SEC` (45s) 进入窗口
- 高概率侧价格 ≥ `PM5_SNIPER_PRICE_THRESH` (0.95) 买入
- 保护: 价格 < `PM5_SNIPER_MIN_PRICE` (0.80) 拒绝（不接飞刀）

### ⚡ 动量 (MOMENTUM) — 流动性动量
- `obi.py` 计算订单簿不均衡 OBI ∈ [−1, 1]（买卖压力）
- OBI 方向 + 现货价 vs 行权价偏离方向**双确认**才开仓（防假突破）
- 主腿 + 50% 互补对冲腿（控制尾部风险）
- 冷却 `PM5_MOMENTUM_COOLDOWN` 秒

### 🪜 阶梯 (LADDER + STAIR)
- Ladder 做市: YES+NO 组合 bid 价 < 1 − `PM5_LADDER_MIN_SPREAD` 时双向挂买单，
  结算前 `PM5_LADDER_STOP_BEFORE_END` 秒停止
- Stair 出场: 结算前 2 分钟按 `PM5_STAIR_STEPS` 批、每批让步
  `PM5_STAIR_STEP_OFFSET` 依次挂卖单，流动性感知退场

## 共享风控 (`shared_risk.py`, SharedRiskGate 单例)

天气 Bot 与 5min Bot 共用**同一实例**，任何一侧触发即同时熔断：

| 规则 | 环境变量 | 默认 |
|------|---------|------|
| 日亏损上限 | `MAX_DAILY_LOSS_PCT` × `INITIAL_CAPITAL` | 5% × $10000 |
| 总持仓上限 | `MAX_CONCURRENT` | 30 |
| 连亏熔断 | 连续 5 笔亏损 → 冷却 `SHARED_RISK_CIRCUIT_COOLDOWN` | 1800s |
| 单笔金额上限 | 资金 × 5% | $500 |

桥接机制:
- 5min 侧: `GuardedExecutor` 每个信号执行前调用 `risk.check()`，
  被拒信号进入 `engine.history` (状态 RISK_BLOCKED)
- 天气侧: 适配器 `_sync_weather_pnl()` 每 60s 增量上报天气引擎
  `total_pnl` 变化到共享风控 → 天气亏损也计入熔断判定

## 统一账户 (`account_manager.py`, AccountManager 单例)

- **全局订单锁**: `asyncio.Lock`，同一时刻只有一个订单在飞 →
  杜绝天气与 5min 并发下单的 nonce 冲突
- **nonce 全局递增**: 每订单唯一编号（实盘 CLOB nonce 用）
- **余额统一记账**: 两个 bot 共享同一 `INITIAL_CAPITAL` 记账，
  不会各算各的余额
- 订单门控 `order_gate()`: 锁内检查日亏损 + 余额充足 + 发放 nonce；
  被拒抛 `OrderGateBlocked`

## 看板整合

- `api_server.py`: 新增 `GET /api/5min`（适配器 status: 策略统计 /
  持仓 / 信号 / shared_risk / account），`/api/metrics` 也内嵌 5min 摘要
- `dashboard/dashboard.py` (Streamlit): 新增「⚡ 5分钟套利」标签页
  （指标卡 / 策略分布 / 持仓 / 活跃市场 / 信号流 / 共享风控/账户 JSON）
- HTML 看板 `dashboard.html` 不受影响

## DRY_RUN 验证

```bash
cd HighTempTation
python3 scripts/verify_5min_integration.py
# 预期: 20 项检查全部 ✅ (见脚本内 [1]-[7] 编号对应集成需求)
```

验证覆盖:
1. 子模块导入 + 四大策略注册 (ARB/SNIPER/MOMENTUM/LADDER/STAIR)
2. 适配层构造 + GuardedExecutor 门控注入
3. requirements.txt 依赖合并 (websockets/ccxt/polymarket-client)
4. 共享风控: 日亏损熔断 / 总仓位限制 / **结算释放仓位 (防只增不减)** / 与引擎联动拦截
5. 统一账户: 单例 / nonce 递增 / 余额不足拦截
6. 看板数据通路: /api/5min 数据源
7. DRY_RUN: 全程模拟撮合、结算、信号链路（含狙击窗口与套利双腿）

## 启动

```bash
# 集成模式 (推荐): 主进程内启动, 共享风控/账户/看板
PM5_ENABLED=true python3 bot.py

# 独立调试 (勿与集成模式同时运行)
python3 -m polymarket_5min_bot

# PM2 独立进程 (调试用)
pm2 start ecosystem.config.cjs --only polymarket-5min-standalone
```

## 实盘注意 (DRY_RUN=false)

1. 需要 L2 签名密钥: `POLYMARKET_API_KEY/SECRET/PASSPHRASE` +
   `POLYMARKET_PRIVATE_KEY`（环境变量，勿写入 .env 提交）
2. `clob.py::_sign_order_hash()` 的 EIP-712 订单签名目前是占位实现，
   **实盘前必须接入 py_eth_signing / py-clob-client 的签名逻辑**
3. 真实 5min 市场周期性开放，空缺时自动用模拟市场（仅 DRY_RUN 有意义，
   实盘会因 CLOB 订单簿 404 自动拒单，不会裸下单）
