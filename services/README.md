# LE VAN DO® 交易所 API 连接配置

> 本文档描述从 TradingView 策略警报 → Webhook 接收 → 交易所挂单的完整链路配置。

---

## 📋 架构概览

```
┌─────────────────┐     HTTP POST      ┌──────────────────────┐     REST API      ┌──────────────┐
│   TradingView    │  ──────────────►   │  Webhook 接收服务     │  ─────────────►  │   Bybit      │
│   Pine Script    │   alert_message    │  localhost:3000      │                  │   交易所     │
│   (策略警报)      │                    │  POST /webhook       │                   │  (测试网/实盘)│
└─────────────────┘                    └──────────────────────┘                   └──────────────┘
                                               │
                                               ▼
                                        ┌──────────────────┐
                                        │  信号解析器       │
                                        │  → longE/shortE  │
                                        │  → longX/shortX  │
                                        └──────────────────┘
```

## 🔧 前置条件

### 1. 交易所账户

| 项目 | 值 |
|------|-----|
| **交易所** | Bybit（已在 `configs/` 中预设） |
| **账户类型** | 统一交易账户（Unified Trading Account） |
| **API 权限** | 需要开通 **合约交易** 和 **API 交易** 权限 |

### 2. API 凭据（必须）

> ⚠️ **安全提示**：API Key 和 Secret **不要** 写在代码或 `.env` 文件中提交到 Git。
> 请通过团队 **Secrets 页面** 配置，仅允许交易所运维代理使用。

需要添加的两个 Secrets：

| Secret 名称 | 说明 | 获取位置 |
|-------------|------|----------|
| `BYBIT_API_KEY` | Bybit API Key | Bybit → 账户 → API 管理 |
| `BYBIT_API_SECRET` | Bybit API Secret | 创建 API Key 时生成（仅显示一次） |

**API 权限建议**：
| 权限 | 是否必需 | 说明 |
|------|---------|------|
| 读取钱包/订单 | ✅ 必需 | 获取余额、检查持仓 |
| 合约交易 | ✅ 必需 | 执行开仓/平仓 |
| 提现 | ❌ 关闭 | 安全起见，永远不要开放提现权限 |

## 🚀 快速开始

### 1. 安装依赖

```bash
cd services
npm install
```

### 2. 配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env
```

编辑 `.env` 文件：

```ini
# 交易环境：testnet（测试网）或 production（实盘）
EXCHANGE_NETWORK=testnet

# Bybit API 凭据（先在 Secrets 页面配置，然后在环境变量中引用）
BYBIT_API_KEY=你的_API_KEY
BYBIT_API_SECRET=你的_API_SECRET

# Webhook 安全密钥（生成一个随机字符串）
WEBHOOK_SECRET=your-random-secret-string

# 服务器
PORT=3000

# 默认订单类型：market 或 limit
DEFAULT_ORDER_TYPE=market

# 默认杠杆
DEFAULT_LEVERAGE=1
```

> 💡 **测试网建议**：首次运行时务必使用 `EXCHANGE_NETWORK=testnet`，
> 到 [Bybit Testnet](https://testnet.bybit.com/) 获取测试网 API Key 和测试 USDT。

### 3. 启动服务

```bash
# 测试网模式（推荐先测试）
npm start
# 或
npm run start:testnet

# 实盘模式
npm run start:live
```

### 4. 验证服务

```bash
# 健康检查
curl http://localhost:3000/health
```

成功响应示例：
```json
{
  "status": "ok",
  "exchange": "BYBIT",
  "network": "TESTNET",
  "orderType": "market",
  "serverTime": { "retCode": 0, "result": { "timeSecond": "1711440000" } },
  "accountStatus": "USDT 余额: $10000.00",
  "allowedSymbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
}
```

## 📡 TradingView 警报配置

### 1. 在 TradingView 中配置 Webhook

在 Pine Script 策略的「警报(Alert)」设置中：

| 设置项 | 值 |
|--------|-----|
| **Webhook URL** | `http://<你的服务器IP>:3000/webhook` |
| **消息体格式** | JSON（见下方） |
| **附加 Header** | `x-webhook-secret: <你的 WEBHOOK_SECRET>` |
| **触发频率** | `once_per_bar_close` |

### 2. 警报消息格式

推荐使用 **JSON 格式**，包含信号标识和交易参数：

#### 开仓信号示例

```json
{
  "signal": "longE",
  "symbol": "BTCUSDT",
  "price": "50000",
  "tp1": "51250",
  "tp2": "52500",
  "tp3": "53750",
  "sl": "48750"
}
```

```json
{
  "signal": "shortE",
  "symbol": "ETHUSDT",
  "price": "3000",
  "tp1": "2925",
  "tp2": "2850",
  "tp3": "2775",
  "sl": "3075"
}
```

#### 平仓信号示例

```json
{
  "signal": "longX",
  "symbol": "BTCUSDT"
}
```

```json
{
  "signal": "shortX",
  "symbol": "SOLUSDT"
}
```

### 3. Pine Script 中集成 Webhook 消息

LE VAN DO® 策略已将信号变量定义为 `longE` / `shortE` / `longX` / `shortX`。
在策略的警报消息中，推荐使用以下格式发送：

```
message = '{"signal": "{{strategy.order.alert_message}}", "symbol": "{{ticker}}", "price": "{{close}}"}'
```

> 注意：Pine Script 中的 `alert_message` 参数可以直接传入信号标识字符串（如 `"longE"`），
> 解析器会自动兼容纯字符串格式。

## 🔄 测试网 / 实盘切换

### 切换方式

通过环境变量 `EXCHANGE_NETWORK` 控制：

| 环境变量值 | 模式 | BASE URL | 说明 |
|-----------|------|----------|------|
| `testnet`（默认） | 🟡 测试网 | `api-testnet.bybit.com` | 无实际资金风险 |
| `production` | 🔴 实盘 | `api.bybit.com` | 真实资金交易 |

### 分别启动

```bash
# 测试网（推荐用于验证链路）
NODE_ENV=testnet npm start

# 实盘
NODE_ENV=production npm start

# 或使用预设脚本
npm run start:testnet
npm run start:live
```

### 测试网设置步骤

1. 访问 [Bybit Testnet](https://testnet.bybit.com/) 注册测试账户
2. 创建 API Key（设置 → API）
3. 获取测试 USDT（水龙头会空投测试资金）
4. 在 `.env` 中填入测试网 API Key
5. 启动服务并发送测试警报

## 📦 订单类型支持

| 订单类型 | 代码值 | 说明 | 适用场景 |
|---------|--------|------|---------|
| **市价单** | `market` | 以当前市场最优价立即成交 | 快速入场/出场，信号产生时立即执行 |
| **限价单** | `limit` | 以指定价格或更优价格成交 | 希望以特定价格入场，等待流动性 |

### 配置方式

在 `.env` 中设置默认订单类型：

```ini
DEFAULT_ORDER_TYPE=market   # 默认使用市价单
```

或者通过 TradingView 警报消息中的 `orderType` 字段临时覆盖：

```json
{
  "signal": "longE",
  "symbol": "BTCUSDT",
  "orderType": "limit",
  "price": "49500"
}
```

> ⚠️ 限价单注意事项：
> - 必须同时提供 `price` 字段
> - 限价单默认为 PostOnly 模式（只做 maker，不吃单）
> - 可能因价格未触及而无法成交

## 🔐 安全注意事项

### 1. API Key 保护
- **必须** 通过团队 Secrets 页面配置 `BYBIT_API_KEY` 和 `BYBIT_API_SECRET`
- `.env` 文件已加入 `.gitignore`，切勿提交到 Git
- 定期轮换 API Key

### 2. IP 白名单
在 Bybit API 管理页面，将部署服务器的 IP 添加到白名单。

### 3. Webhook 密钥
- 设置一个足够复杂的 `WEBHOOK_SECRET`（建议 32 位以上随机字符串）
- TradingView 警报请求中通过 Header `x-webhook-secret` 携带此密钥
- 服务端验证不匹配的请求直接返回 401

### 4. 风控限制

| 限制项 | 默认值 | 可调整 |
|--------|--------|--------|
| 最小下单间隔 | 1000ms | `exchange.json` → `riskLimits.minOrderIntervalMs` |
| 最大持仓价值 | $10,000 | `exchange.json` → `riskLimits.maxPositionSize` |
| 最大杠杆 | 10x | `exchange.json` → `riskLimits.maxLeverage` |
| 最大日亏损 | $500 | `exchange.json` → `riskLimits.maxDailyLoss` |

## 🧪 调试与测试

### 手动发送测试信号

```bash
# 测试开多信号
curl -X POST http://localhost:3000/webhook \
  -H "Content-Type: application/json" \
  -H "x-webhook-secret: your-webhook-secret" \
  -d '{"signal":"longE","symbol":"BTCUSDT","price":"50000"}'

# 测试开空信号
curl -X POST http://localhost:3000/webhook \
  -H "Content-Type: application/json" \
  -H "x-webhook-secret: your-webhook-secret" \
  -d '{"signal":"shortE","symbol":"ETHUSDT","price":"3000"}'

# 测试平仓信号（先确保有持仓）
curl -X POST http://localhost:3000/webhook \
  -H "Content-Type: application/json" \
  -H "x-webhook-secret: your-webhook-secret" \
  -d '{"signal":"longX","symbol":"BTCUSDT"}'
```

### 检查服务日志

服务运行时会输出详细日志：

```
[2024-01-01T00:00:00.000Z] POST /webhook — 127.0.0.1
[Webhook] 📩 收到新警报
[Webhook] ✅ 信号验证通过: longE BTCUSDT
[Webhook] 🚀 执行开仓: Buy BTCUSDT
[Bybit] ⚙️ 杠杆已设置: BTCUSDT 1x (isolated)
[Bybit] 📤 下单: [BTCUSDT] Buy 0.001 @ market
[Bybit] ✅ 订单成功: orderId=xxxxxx
[Webhook] ✅ 交易完成
```

## 📁 文件结构

```
services/
├── README.md                       ← 本文档（交易所连接配置文档）
├── package.json                    ← Node.js 依赖
├── .env.example                    ← 环境变量模板
├── config/
│   ├── exchange.json               ← 交易所连接默认配置
│   └── index.js                    ← 配置管理器（环境变量 + 默认值）
├── exchange/
│   ├── bybit.js                    ← Bybit V5 API 封装
│   └── index.js                    ← 交易所工厂（单例）
├── signals/
│   └── parser.js                   ← TradingView 信号解析器
└── webhook/
    ├── server.js                   ← Express Webhook HTTP 服务
    └── validator.js                ← 请求验证器
```

## 🔮 支持的交易对

| 交易对 | 最小交易量 | 精度 |
|--------|-----------|------|
| BTCUSDT | 0.001 | 小数点后 2 位 |
| ETHUSDT | 0.01 | 小数点后 2 位 |
| SOLUSDT | 0.1 | 小数点后 3 位 |

可在 `config/exchange.json` 的 `symbols` 字段中扩展更多交易对。

---

> **LE VAN DO® - Swing Signals & Overlays Private™ 7.9-X**
> 对接策略版本: 7.9-X（更多版本见 `configs/` 和 `strategies/`）
