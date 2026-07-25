# LE VAN DO® 交易所 API 连接配置

> 本文档描述从 TradingView 策略警报 → Webhook 接收 → OKX 交易所挂单的完整链路配置。

---

## 📋 架构概览

```
┌─────────────────┐     HTTP POST      ┌──────────────────────┐     REST API      ┌──────────────┐
│   TradingView    │  ──────────────►   │  Webhook 接收服务     │  ─────────────►  │    OKX       │
│   Pine Script    │   alert_message    │  localhost:3000      │                  │   交易所     │
│   (策略警报)      │                    │  POST /webhook       │                   │  (模拟盘/实盘)│
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
| **交易所** | OKX（已在 `configs/` 中预设） |
| **账户类型** | 统一账户 |
| **API 权限** | 需要开通 **交易** 权限 |

### 2. API 凭据（必须）

> ⚠️ **安全提示**：API Key、Secret 和 Passphrase **不要** 写在代码或 `.env` 文件中提交到 Git。
> 请通过团队 **Secrets 页面** 配置，仅允许交易所运维代理使用。

OKX 使用 **三件套认证**，需要添加三个 Secrets：

| Secret 名称 | 说明 | 获取位置 |
|-------------|------|----------|
| `OKX_API_KEY` | API Key | OKX → 账户 → API → 创建 API Key |
| `OKX_API_SECRET` | Secret Key | 创建 API Key 时生成（仅显示一次） |
| `OKX_API_PASSPHRASE` | Passphrase | 创建 API Key 时设置的访问密码 |

**API 权限建议**：
| 权限 | 是否必需 | 说明 |
|------|---------|------|
| 读取 | ✅ 必需 | 获取余额、检查持仓 |
| 交易 | ✅ 必需 | 执行开仓/平仓 |
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
# 交易环境：testnet（模拟盘）或 production（实盘）
EXCHANGE_NETWORK=testnet

# OKX API 凭据（先在 Secrets 页面配置，然后在环境变量中引用）
OKX_API_KEY=你的_API_KEY
OKX_API_SECRET=你的_API_SECRET
OKX_API_PASSPHRASE=你的_PASSPHRASE

# Webhook 安全密钥（生成一个随机字符串）
WEBHOOK_SECRET=your-random-secret-string

# 服务器
PORT=3000

# 默认订单类型：market 或 limit
DEFAULT_ORDER_TYPE=market

# 默认杠杆
DEFAULT_LEVERAGE=1
```

> 💡 **模拟盘建议**：首次运行时务必使用 `EXCHANGE_NETWORK=testnet`，
> 到 [OKX 模拟盘](https://www.okx.com) 注册模拟账户获取 API Key。

### 3. 启动服务

```bash
# 模拟盘模式（推荐先测试）
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
  "exchange": "OKX",
  "network": "TESTNET",
  "orderType": "market",
  "serverTime": { "code": "0", "data": [{ "ts": "1711440000000" }] },
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

OKX 模拟盘与实盘使用相同的 API 端点 `https://www.okx.com`，凭据区分：

| 环境变量值 | 模式 | 说明 |
|-----------|------|------|
| `testnet`（默认） | 🟡 模拟盘 | 使用 OKX 模拟账户的 API Key（demo 账户） |
| `production` | 🔴 实盘 | 使用主账户的 API Key（真实资金） |

### 分别启动

```bash
# 模拟盘（推荐用于验证链路）
EXCHANGE_NETWORK=testnet npm start

# 实盘
EXCHANGE_NETWORK=production npm start

# 或使用预设脚本
npm run start:testnet
npm run start:live
```

### 模拟盘设置步骤

1. 访问 [OKX](https://www.okx.com) 登录或注册
2. 右上角头像 → 「模拟交易」进入模拟盘环境
3. 在模拟盘环境中创建 API Key（账户 → API）
4. 设置 Passphrase，获取 Key 和 Secret
5. 在 Secrets 页面添加这三个凭据
6. 在 `.env` 中设置 `EXCHANGE_NETWORK=testnet`
7. 启动服务并发送测试警报

> ⚠️ 注意：模拟盘 API Key 必须在 **模拟盘环境** 中创建，主账户的 Key 无法用于模拟盘。

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
> - 可能因价格未触及而无法成交

## 🔐 安全注意事项

### 1. API Key 保护
- **必须** 通过团队 Secrets 页面配置 `OKX_API_KEY`、`OKX_API_SECRET`、`OKX_API_PASSPHRASE`
- `.env` 文件已加入 `.gitignore`，切勿提交到 Git
- 定期轮换 API Key

### 2. IP 白名单
在 OKX API 管理页面，将部署服务器的 IP 添加到白名单。

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
[OKX] ⚙️ 杠杆已设置: BTC-USDT 1x (isolated)
[OKX] 📤 下单: [BTC-USDT] buy 0.001 @ market
[OKX] ✅ 订单成功: orderId=123456789
[Webhook] ✅ 交易完成
```

## 📁 文件结构

```
services/
├── README.md                       ← 本文档（交易所连接配置文档）
├── package.json                    ← Node.js 依赖
├── .env.example                    ← 环境变量模板
├── config/
│   ├── exchange.json               ← 交易所连接默认配置（OKX）
│   └── index.js                    ← 配置管理器（环境变量 + 默认值）
├── exchange/
│   ├── okx.js                      ← OKX V5 API 封装
│   └── index.js                    ← 交易所工厂（单例）
├── signals/
│   └── parser.js                   ← TradingView 信号解析器
└── webhook/
    ├── server.js                   ← Express Webhook HTTP 服务
    └── validator.js                ← 请求验证器
```

## 🔮 支持的交易对

OKX 使用带连字符的交易对格式（合约内部自动转换）：

| 代码格式 | OKX 合约 ID | 最小交易量 | 精度 |
|---------|-------------|-----------|------|
| BTCUSDT | BTC-USDT | 0.001 | 小数点后 1 位 |
| ETHUSDT | ETH-USDT | 0.01 | 小数点后 1 位 |
| SOLUSDT | SOL-USDT | 0.1 | 小数点后 2 位 |

可在 `config/exchange.json` 的 `symbols` 字段中扩展更多交易对。

---

## 🤖 MT5 執行後端（新增）

> MT5 (MetaTrader 5) 作為第二執行後端，與既有 OKX 服務並存。
> 透過 `EXCHANGE_TYPE=mt5` 環境變數切換。

### 架構

```
TradingView 策略警報
        │
        ▼  HTTP POST (alert_message)
Webhook 接收端點 (POST /webhook)
        │
        ▼  信號解析
信號解析器 (longE/shortE/longX/shortX) ← parser.js (不變)
        │
        ▼  EXCHANGE_TYPE=mt5
exchange/index.js 工廠
        │
        ├── okx.js (既有 OKX，不受影響)
        └── mt5.js (Node.js IPC 封裝)
                │
                ▼  stdin/stdout JSON
        services/mt5/server.py (Python IPC Server)
                │
                ▼  MetaTrader5 API
        MT5 終端 (僅 Windows)
```

### 檔案結構

```
services/
├── mt5/
│   ├── bridge.py          ← Python MetaTrader5 封裝（核心模組）
│   ├── server.py          ← Python IPC Server（stdin/stdout JSON 協議）
│   └── test_simulate.py   ← Python 端模擬測試
├── exchange/
│   ├── mt5.js             ← Node.js IPC 封裝（與 okx.js 同層級）
│   ├── okx.js             ← 既有 OKX（不變）
│   └── index.js           ← 工廠（支援 okx / mt5 切換）
└── scripts/
    └── simulate-mt5.js    ← MT5 模擬驗證腳本
```

### 前置條件

| 環境 | 需求 |
|------|------|
| **Windows**（完整功能） | 安裝 MT5 終端、登入帳戶、啟用自動交易；`pip install MetaTrader5` |
| **Linux/macOS**（僅模擬） | 設定 `DRY_RUN=true`，無需 MT5 終端 |

### MT5 帳戶憑證

在 Secrets 頁面（或 `.env`）配置：

| 變數 | 說明 | 範例 |
|------|------|------|
| `MT5_ACCOUNT` | MT5 帳戶號碼 | `12345678` |
| `MT5_PASSWORD` | MT5 帳戶密碼 | `MySecureP@ss` |
| `MT5_SERVER` | 交易伺服器名稱 | `ICMarkets-Demo` |
| `MT5_PATH` | MT5 終端 exe 路徑（選填） | `C:\Program Files\...\terminal64.exe` |

> ⚠️ MT5 帳戶須先在 MT5 終端登入，並在「工具 → 選項 → 自動交易」中啟用自動交易。

### 快速開始

#### 1. 模擬模式（無需 MT5）

```bash
cd services
EXCHANGE_TYPE=mt5 DRY_RUN=true node scripts/simulate-mt5.js
```

#### 2. 啟動 Webhook 服務（MT5 後端）

```bash
# 模擬模式
EXCHANGE_TYPE=mt5 DRY_RUN=true npm run start:mt5

# 測試網（需 Windows MT5 終端已連線）
npm run start:mt5:testnet

# 實盤
npm run start:mt5:live
```

#### 3. Python 端直接測試

```bash
DRY_RUN=true python3 -m services.mt5.test_simulate
```

### 切換執行後端

| 環境變數 | 執行後端 |
|---------|---------|
| `EXCHANGE_TYPE=okx`（預設） | OKX V5 API |
| `EXCHANGE_TYPE=mt5` | MT5 Python MetaTrader5 |

既有 OKX 服務完全不受影響，兩者可隨時切換。

### 支援的 npm 腳本

```bash
npm run simulate          # OKX 模擬測試
npm run simulate:mt5      # MT5 模擬測試
npm start                 # OKX 服務（預設）
npm run start:mt5         # MT5 服務
npm run start:mt5:testnet # MT5 測試網
npm run start:mt5:live    # MT5 實盤
```

### 限制

1. **平台限制**：`MetaTrader5` Python 套件僅支援 Windows。Linux/macOS 需使用 `DRY_RUN=true` 模擬模式。
2. **交易品種**：MT5 使用 6 字元格式（如 `BTCUSD`），模組自動從 `BTCUSDT` 轉換。實際品種名稱需依券商設定調整。
3. **槓桿設定**：MT5 槓桿由帳戶/券商設定，無法透過 API 動態修改。`setLeverage()` 僅記錄日誌。
4. **限價單**：支援市價單與限價單，但限價單需確保價格在 MT5 價格範圍內。

---

> **LE VAN DO® - Swing Signals & Overlays Private™ 7.9-X**
> 对接策略版本: 7.9-X（更多版本见 `configs/` 和 `strategies/`）
