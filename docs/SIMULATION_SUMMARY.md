# 首个策略全流程跑通 — 模拟交易验证总结

## 完成目标

> 选一个现有策略，完成从策略代码→参数配置→交易所对接→模拟交易验证的完整闭环，产出操作日志。

## 已选策略

**LE VAN DO® - Swing Signals & Overlays Private™ 7.9-X**

| 项目 | 内容 |
|------|------|
| 源代码 | `strategies/LE_VAN_DO_Swing_Signals_7.9-X.pine` (1045 行, Pine Script v5) |
| 结构参数 | `strategies/LE_VAN_DO_Swing_Signals_7.9-X/params.json` |
| 版本记录 | `strategies/LE_VAN_DO_Swing_Signals_7.9-X/version.json` |
| 参数配置 | `configs/LE_VAN_DO_Swing_Signals_7.9-X/config.json` (8 个分类, 40+ 参数) |

## 验证链路

```
TradingView 策略警报
     │
     ▼  HTTP POST (模拟 webhook 请求)
Webhook 验证 → 信号解析器 (parseSignal)
     │
     ▼
OKX 交易所客户端 (模拟 dry-run 模式)
     │
     ▼
开仓/平仓/持仓管理 → 操作日志输出
```

## 各环节验证内容

### 1. 策略代码 ✅
- Pine Script v5 源代码已部署，支持 Open/Close 和 Renko 双模式
- 4 种信号类型: `longE` (开多), `shortE` (开空), `longX` (平多), `shortX` (平空)
- 3 种止盈止损模式: ATR, Trailing, Options
- 7 种横盘过滤模式
- 三级止盈 (TP1/TP2/TP3) + 止损

### 2. 参数配置 ✅
- `configs/` 目录下完整的 JSON 参数配置
- 包含: meta, tradingMode, filterSettings, renkoSettings, riskManagement, webhookMessages, displayOptions, reservedParams
- 每个参数标注了 type/pineInput/source/description

### 3. 交易所对接 ✅
| 组件 | 文件 | 功能 |
|------|------|------|
| 配置管理器 | `services/config/index.js` | 读取 .env + exchange.json，含 dryRun 模拟模式 |
| 交易所默认配置 | `services/config/exchange.json` | OKX 测试网/实盘端点，BTC/ETH/SOL 交易对 |
| OKX 客户端 | `services/exchange/okx.js` | V5 REST API 封装，HMAC-SHA256 签名，下单/平仓/持仓 |
| 交易所工厂 | `services/exchange/index.js` | 单例模式获取客户端实例 |
| Webhook 服务 | `services/webhook/server.js` | Express 服务，POST /webhook + GET /health |
| 请求验证 | `services/webhook/validator.js` | 密钥验证 + 信号解析 + 白名单风控 |
| 信号解析器 | `services/signals/parser.js` | JSON/字符串信号解析，兼容旧版 Legacy 格式 |

### 4. 模拟交易验证 ✅
见 `SERVICES_RUN_LOG.md` 完整操作日志。

#### 测试场景（6 个信号）:
1. **多头开市价单** (longE, BTCUSDT, $65,420.50)
2. **空头开市价单** (shortE, ETHUSDT, $3,520.80)
3. **多头平仓** (longX, BTCUSDT, $66,800.00)
4. **空头平仓** (shortX, ETHUSDT, $3,380.00)
5. **Legacy 纯字符串兼容** ("Long Entry")
6. **限价单测试** (longE, SOLUSDT, limit @ $145.30)

#### 风控验证:
- 下单频率限制 (≥1000ms) ✅
- 交易对白名单 (BTC/ETH/SOL) ✅
- 最大持仓价值 ($10,000) ✅
- 最大杠杆 (10x) ✅

#### 信号兼容性验证（8 种 Legacy 格式）:
| 输入 | 解析结果 | 状态 |
|------|---------|------|
| "Go Long" | → longE | ✅ |
| "Go Short" | → shortE | ✅ |
| "Long Exit" | → longX | ✅ |
| "Short Exit" | → shortX | ✅ |
| "Long TP1" | → tp1 | ✅ |
| "Short SL" | → sl | ✅ |
| "Short TP1" | → tp1 | ✅ |
| "Long SL" | → sl | ✅ |

## 本次新增/修改的文件

| 文件 | 说明 |
|------|------|
| `services/.env` | 模拟验证环境变量 (DRY_RUN=true) |
| `services/scripts/simulate.js` | 全流程模拟验证脚本 |
| `SERVICES_RUN_LOG.md` | 完整操作日志 |
| `docs/SIMULATION_SUMMARY.md` | 本验证总结文档 |
| `services/config/index.js` | 新增 dryRun 配置支持 |
| `services/exchange/okx.js` | 新增模拟模式 (dry-run)，含虚拟持仓/订单记录 |
| `services/signals/parser.js` | 修复大小写不敏感的 camelCase 匹配、强化 TP/SL Legacy 信号兼容 |

## 运行方式

```bash
# 模拟模式验证
cd services && node scripts/simulate.js
# 或
cd services && npm run simulate

# 启动 Webhook 服务（模拟模式）
cd services && npm start

# 实盘模式（需配置真实 OKX API Key）
cd services && npm run start:live
```
