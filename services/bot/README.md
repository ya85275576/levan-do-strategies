# LE VAN DO® OKX 原生交易机器人

直接通过 **OKX REST API 轮询行情数据**驱动交易，**无需 WebSocket，无需 TradingView**。
将 LE VAN DO® Swing Signals 策略从 Pine Script v5 移植到 Python。

## 架构

```
OKX REST API (轮询 K 线, 每 15 分钟)
       │  GET /api/v5/market/candles?bar=15m&limit=100
       ▼
RestApiDataFeed ──── CandleAggregator (tfmult=18)
       │                  ↓ 聚合为高周期 K 线
       │             LeVanDoStrategy (状态机)
       │                  ↓ 信号 (longE/shortE/TP/SL)
       │             SignalHandler
       │                  ↓
OkxOrderManager ──── OKX REST API (下单)
```

> **为什么用 REST API 而非 WebSocket？** 东京服务器 IP 无法连接 OKX WebSocket（被封锁），
> REST API 使用标准 HTTPS，不易被封锁，且实现更简单可靠。
>
> 如果 REST API 也失败，会自动回退到模拟数据源（仅 DRY_RUN 模式）。

## 模块说明

| 文件 | 说明 |
|------|------|
| `config.py` | 配置管理器 — 从环境变量读取所有参数 |
| `market_data.py` | OKX REST API 轮询 + K 线聚合 + 回退到模拟数据 |
| `indicators.py` | 技术指标计算 (HA/Renko/EMA/ATR/RSI/Sideways) |
| `strategy.py` | LE VAN DO 策略引擎 — 状态机 |
| `order_manager.py` | OKX REST API 订单执行器 |
| `bot.py` | 主程序 — 整合所有模块 |
| `ecosystem.config.cjs` | PM2 进程管理配置（.cjs 因父级 package.json 的 type: module） |

## 快速开始

### 1. 安装依赖

```bash
cd services/bot
pip install -r requirements.txt
```

### 2. 配置环境变量

支持通过环境变量或 `.env` 文件配置。关键变量：

```bash
# 交易所
export EXCHANGE_NETWORK=testnet          # testnet | production
export OKX_API_KEY=your-api-key
export OKX_API_SECRET=your-api-secret
export OKX_API_PASSPHRASE=your-passphrase

# 运行模式
export DRY_RUN=true                       # true=模拟, false=实盘

# 策略参数
export SETUP_TYPE=Open/Close             # Open/Close | Renko
export TPS_TYPE=Trailing                 # ATR | Trailing | Options
export SIDEWAYS_FILTER="No Filtering"    # 7 种过滤模式

# 交易对
export TRADING_SYMBOL=BTC-USDT
```

### 3. 运行

```bash
# 模拟模式（默认）
python bot.py

# 实盘模式
DRY_RUN=false python bot.py
```

### 4. 使用 PM2（推荐）

```bash
# 安装 PM2
npm install -g pm2

# 启动（模拟模式）
pm2 start ecosystem.config.cjs

# 启动（实盘）
pm2 start ecosystem.config.cjs --env production

# 查看日志
pm2 logs le-van-do-bot

# 查看状态
pm2 status
```

## 策略参数详解

### 交易模式

| 环境变量 | 选项 | 说明 |
|---------|------|------|
| `SETUP_TYPE` | `Open/Close`, `Renko` | Heikin Ashi K 线信号 / Renko EMA 交叉信号 |
| `TPS_TYPE` | `ATR`, `Trailing`, `Options` | ATR 三级止盈止损 / Trailing 追踪 / 仅开仓 |
| `TF_MULT` | 整数 (默认 18) | 高时间框架倍数 |

### Sideways 过滤器（7 种）

| 环境变量值 | 说明 |
|-----------|------|
| `No Filtering` | 不过滤 |
| `Filter with Atr` | 仅 ATR 过滤（ATR >= ATR_MA 时允许交易） |
| `Filter with RSI` | 仅 RSI 过滤（RSI 超买/超卖时允许交易） |
| `Atr or RSI` | ATR 或 RSI 任一成立 |
| `Atr and RSI` | ATR 和 RSI 同时成立 |
| `Entry Only in sideways market(By ATR or RSI)` | 仅在横盘时入场（反向过滤） |
| `Entry Only in sideways market(By ATR and RSI)` | 横盘且两者同时成立 |

### 风险管理

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `ATR_LENGTH` | 20 | ATR 计算周期 |
| `PROFIT_FACTOR` | 2.5 | 止盈倍数（TP = profitFactor × ATR） |
| `STOP_FACTOR` | 1.0 | 止损倍数 |
| `TP1_QTY_PCT` | 50.0 | TP1 平仓百分比 |
| `TP2_QTY_PCT` | 30.0 | TP2 平仓百分比 |
| `TP3_QTY_PCT` | 20.0 | TP3 平仓百分比 |
| `DEFAULT_LEVERAGE` | 1 | 杠杆倍数 |

## 从 Pine Script 移植说明

策略逻辑对应关系：

| Pine Script | Python 实现 | 文件 |
|------------|-------------|------|
| `ticker.heikinashi()` | `heikin_ashi()` | indicators.py |
| `ticker.renko("ATR")` | `RenkoBuilder` | indicators.py |
| `ta.atr()` | `atr()` | indicators.py |
| `ta.rsi()` | `rsi()` | indicators.py |
| `ta.ema()` | `ema()` | indicators.py |
| `condition` 状态机 | `StrategyState.condition` | strategy.py |
| `strategy.entry/exit` | `SignalHandler.handle_signal()` | bot.py |
| `request.security(HA, tf)` | `CandleAggregator` + Heikin Ashi | market_data.py |

## 与现有 Webhook 服务的关系

- **Webhook 服务** (`services/webhook/server.js`)：继续运行，处理 TradingView 警报
- **本机器人** (`services/bot/bot.py`)：新增的 OKX 原生策略引擎，独立运行
- 两者共享同一组环境变量（OKX API 凭据），互不干扰

## License

MPL-2.0
