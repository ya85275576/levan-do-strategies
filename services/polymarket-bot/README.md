# Polymarket YES+NO=$1 互补套利机器人

扫描 [Polymarket](https://polymarket.com) 上所有活跃的二元预测市场，
寻找 **YES + NO 买入价之和低于 $1** 的无风险套利机会。

## 原理

```
Polymarket 上每个二元预测市场有两个代币：
  - YES 代币：事件发生时兑付 $1
  - NO 代币：事件未发生时兑付 $1

正常情况下：YES 价格 + NO 价格 ≈ $1

当市场出现偏差时（例如 YES=$0.45, NO=$0.50）：
  总成本 = $0.45 + $0.50 = $0.95
  到期兑付 = $1.00
  无风险利润 = $0.05 (5.26%)
```

## 架构

```
Polymarket CLOB API (https://clob.polymarket.com)
       │
       ▼  GET /markets (列出所有活跃市场)
       │  GET /book?token_id=X (查询订单簿)
PolymarketClient (polymarket_api.py)
       │
       ▼
┌─────────────────────────────────────────────────────┐
│  ArbitrageStrategy (strategy.py) — 策略引擎          │
│  ├── ArbitrageStrategyParams  — 策略参数             │
│  ├── ArbitrageSignalType       — 信号类型枚举        │
│  └── ArbitrageStrategy         — 状态机 + 扫描逻辑   │
└─────────────────────────────────────────────────────┘
       │
       ▼  (可选包装)
ArbitrageScanner (arbitrage_scanner.py) — 兼容包装层
       │
       ▼
Reporter (reporter.py)
       │  - 控制台日志
       │  - Slack / Discord Webhook
       ▼
状态文件 (/tmp/polymarket-arbitrage-state.json)
机会记录 (/tmp/polymarket-arbitrage-opportunities.json)
       │
       ▼
Web Dashboard (services/webhook/server.js)
  GET  /polymarket       — 仪表板页面
  GET  /api/polymarket    — JSON API
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `config.py` | 配置管理器 — 从环境变量读取所有参数 |
| `polymarket_api.py` | Polymarket CLOB API 客户端 — 市场查询 + 订单簿查询 |
| `strategy.py` | **策略引擎** — ArbitrageStrategy 类（与 bot/strategy.py 同模式） |
| `arbitrage_scanner.py` | 扫描器包装层（委托给 strategy.py，保持向后兼容） |
| `reporter.py` | 报告输出 — 控制台 + Slack/Discord Webhook |
| `main.py` | 主程序 — CLI 入口 |
| `ecosystem.config.cjs` | PM2 进程管理配置 |
| `requirements.txt` | Python 依赖 |

## 安装

```bash
cd services/polymarket-bot
pip install -r requirements.txt
```

## 快速开始

### 执行一次扫描

```bash
python main.py
```

### 持续循环扫描

```bash
python main.py --loop
```

### 扫描并发送 Slack/Discord 通知

```bash
ARBITRAGE_WEBHOOK_URL=https://hooks.slack.com/services/xxx python main.py --scan-and-notify
```

### 列出所有活跃市场

```bash
python main.py --list-markets
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DRY_RUN` | `true` | 模拟模式（仅扫描不下单） |
| `ARBITRAGE_THRESHOLD` | `0.98` | YES+NO < 0.98 时触发 |
| `TRADE_SIZE` | `100.0` | 每次模拟交易金额 (USDC) |
| `SCAN_INTERVAL_SEC` | `60` | 扫描间隔（秒） |
| `MAX_PAGES` | `5` | API 翻页数量（每页 100 个市场） |
| `MIN_LIQUIDITY_USDC` | `100.0` | 最低盘口流动性 |
| `MIN_YES_PRICE` | `0.02` | 最小 YES 价格过滤 |
| `MAX_YES_PRICE` | `0.98` | 最大 YES 价格过滤 |
| `MIN_NO_PRICE` | `0.02` | 最小 NO 价格过滤 |
| `MAX_NO_PRICE` | `0.98` | 最大 NO 价格过滤 |
| `POLYMARKET_CLOB_API` | `https://clob.polymarket.com` | CLOB API URL |
| `ARBITRAGE_WEBHOOK_URL` | (空) | Slack/Discord Webhook 通知 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `STATE_FILE` | `/tmp/polymarket-arbitrage-state.json` | 状态文件路径 |
| `OPPORTUNITIES_FILE` | `/tmp/polymarket-arbitrage-opportunities.json` | 机会记录文件 |

## 示例输出

```
2025-01-15 12:00:00.000 [INFO] polymarket.scanner: ============================================================
2025-01-15 12:00:00.000 [INFO] polymarket.scanner: 🔍 扫描轮次 #1
2025-01-15 12:00:00.000 [INFO] polymarket.scanner: ============================================================
2025-01-15 12:00:02.000 [INFO] polymarket.scanner: [扫描] 共获取 150 个二元活跃市场
2025-01-15 12:00:30.000 [INFO] polymarket.scanner: [扫描] 完成: 扫描 150 个市场, 发现 3 个套利机会
2025-01-15 12:00:30.000 [INFO] polymarket.scanner: ------------------------------------------------------------
2025-01-15 12:00:30.000 [INFO] polymarket.scanner: 📊 扫描报告
2025-01-15 12:00:30.000 [INFO] polymarket.scanner:    本轮发现: 3 个机会 (新增: 2 个)
2025-01-15 12:00:30.000 [INFO] polymarket.scanner: ------------------------------------------------------------
2025-01-15 12:00:30.000 [INFO] polymarket.scanner: 排名 | 问题                    | YES买入  | NO买入   | 成本   | 利润%
2025-01-15 12:00:30.000 [INFO] polymarket.scanner:    1 | Will BTC exceed $100k... |   0.4500 |   0.5000 | 0.9500 |  5.26%
2025-01-15 12:00:30.000 [INFO] polymarket.scanner:    2 | Will ETH merge by Q3... |   0.2300 |   0.7400 | 0.9700 |  3.09%
2025-01-15 12:00:30.000 [INFO] polymarket.scanner:    3 | Will SOL > $200...      |   0.3500 |   0.6200 | 0.9700 |  3.09%
```

## PM2 部署

```bash
cd services/polymarket-bot

# 启动（模拟模式，默认）
pm2 start ecosystem.config.cjs

# 启动（实盘）
pm2 start ecosystem.config.cjs --env production

# 查看日志
pm2 logs polymarket-arbitrage

# 查看状态
pm2 status

# 重启
pm2 restart polymarket-arbitrage

# 停止
pm2 stop polymarket-arbitrage
```

## Web Dashboard

Polymarket 套利机器人的状态已集成到 Webhook 服务的仪表板中：

| 路由 | 说明 |
|------|------|
| `GET /polymarket` | Polymarket 套利仪表板页面 |
| `GET /api/polymarket` | Polymarket 状态 JSON API |
| `GET /` 或 `/dashboard` | LE VAN DO® 主仪表板 |

仪表板会实时显示：
- 扫描轮次、累计机会数、已知市场数
- 每条套利机会的 YES/NO 价格、成本、利润率
- 最大/平均利润率统计
- PM2 进程运行状态
- YES+NO=$1 原理说明

## 设计模式

### strategy.py（策略引擎）

遵循 `services/bot/strategy.py` 的设计模式：

| bot/strategy.py | polymarket-bot/strategy.py |
|-----------------|---------------------------|
| `StrategyParams` | `ArbitrageStrategyParams` |
| `SignalType` | `ArbitrageSignalType` |
| `LeVanDoStrategy` | `ArbitrageStrategy` |
| `StrategyState` | `ArbitrageStrategyState` |
| `analyze()` → SignalType | `scan_markets()` → List[ArbitrageOpportunity] |
| `on_signal` 回调 | `on_signal` 回调 |

### arbitrage_scanner.py（兼容包装层）

保持对旧代码的向后兼容，内部委托给 `ArbitrageStrategy`。

### Web Dashboard 集成

通过文件系统 IPC（JSON 状态文件）与 Node.js Webhook 服务通信，与现有 LE VAN DO® 机器人相同的模式。

## License

MPL-2.0
