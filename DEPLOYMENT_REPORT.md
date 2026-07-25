# Webhook 服务部署报告 — 东京服务器 (43.133.210.83)

> 部署时间: 2026-07-25T12:38 UTC+8
> 部署者: AI 代理 (交易所运维)
> 模式: OKX 模拟模式 (DRY_RUN=true)

---

## 1. 服务器环境

| 项目 | 值 |
|------|-----|
| **服务器 IP** | 43.133.210.83 |
| **内网 IP** | 10.7.0.12 |
| **操作系统** | Ubuntu 24.04.4 LTS (Noble Numbat) |
| **内核** | 6.8.0-124-generic x86_64 |
| **Node.js** | v22.23.1 |
| **npm** | 10.9.8 |
| **PM2** | 7.0.3 |
| **Caddy** | v2.11.4 (已启用, 提供 HTTPS) |
| **防火墙** | 未启用 (由云平台安全组控制) |
| **磁盘** | 50GB (已用 12GB, 剩余 36GB) |
| **内存** | 2GB (可用 ~1GB) |

## 2. 部署结构

```
/root/levan-do-strategies/
├── services/
│   ├── webhook/server.js     ← Express Webhook 服务 (主入口)
│   ├── exchange/okx.js       ← OKX V5 API 封装
│   ├── exchange/index.js     ← 交易所工厂
│   ├── signals/parser.js     ← TradingView 信号解析器
│   ├── config/exchange.json  ← 交易所默认配置
│   ├── config/index.js       ← 配置管理器
│   └── .env                  ← 环境变量配置
└── .pm2/dump.pm2             ← PM2 进程列表
```

## 3. 服务配置

**`.env` 文件:**

| 变量 | 值 | 说明 |
|------|-----|------|
| `EXCHANGE_TYPE` | `okx` | OKX 后端 |
| `EXCHANGE_NETWORK` | `testnet` | 测试网模式 |
| `DRY_RUN` | `true` | ✅ 模拟模式 — 不实际连接交易所 |
| `PORT` | `3000` | 监听端口 |
| `HOST` | `0.0.0.0` | 监听所有网络接口 |
| `DEFAULT_ORDER_TYPE` | `market` | 默认市价单 |
| `DEFAULT_LEVERAGE` | `1` | 1 倍杠杆 |
| `POSITION_MODE` | `isolated` | 逐仓模式 |

> **注意**: 模拟模式不需要真实的 OKX API 密钥，所有操作仅输出日志。

## 4. 进程管理 (PM2)

| 项目 | 值 |
|------|-----|
| **进程名** | `webhook` |
| **PID** | 53583 |
| **运行模式** | fork |
| **内存占用** | ~70 MB |
| **状态** | ✅ online |
| **开机自启** | ✅ 已配置 `pm2 startup systemd` |
| **日志路径** | `/root/.pm2/logs/webhook-{out,error}.log` |

**PM2 管理命令:**

```bash
pm2 status                    # 查看进程状态
pm2 logs webhook              # 查看实时日志
pm2 restart webhook           # 重启服务
pm2 stop webhook              # 停止服务
pm2 delete webhook            # 删除进程
```

## 5. 服务端点

| 端点 | 方法 | 说明 |
|------|------|------|
| **`http://43.133.210.83:3000/health`** | GET | 健康检查 + 账户状态 |
| **`http://43.133.210.83:3000/webhook`** | POST | TradingView 信号接收 |

### 健康检查示例

```bash
curl http://43.133.210.83:3000/health
```

响应:
```json
{
  "status": "ok",
  "exchange": "OKX / MT5",
  "exchangeType": "okx",
  "network": "TESTNET",
  "orderType": "market",
  "dryRun": true,
  "accountStatus": "USDT 余额: $0.00",
  "allowedSymbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
}
```

### Webhook 信号测试

```bash
# 开多信号
curl -X POST http://43.133.210.83:3000/webhook \
  -H "Content-Type: application/json" \
  -d '{"signal":"longE","symbol":"BTCUSDT","price":"50000"}'

# 开空信号
curl -X POST http://43.133.210.83:3000/webhook \
  -H "Content-Type: application/json" \
  -d '{"signal":"shortE","symbol":"ETHUSDT","price":"3500"}'

# 平多信号
curl -X POST http://43.133.210.83:3000/webhook \
  -H "Content-Type: application/json" \
  -d '{"signal":"longX","symbol":"BTCUSDT"}'

# 平空信号
curl -X POST http://43.133.210.83:3000/webhook \
  -H "Content-Type: application/json" \
  -d '{"signal":"shortX","symbol":"ETHUSDT"}'
```

## 6. 测试结果

### 6.1 健康检查 ✅

```
GET /health → 200 OK
  - 交易所: OKX
  - 网络: TESTNET
  - 模拟模式: 已启用
  - 服务器时间: 正常
```

### 6.2 Webhook 开多信号 (longE BTCUSDT) ✅

```json
{
  "success": true,
  "signal": "longE",
  "symbol": "BTCUSDT",
  "orderResult": {
    "code": "0",
    "msg": "模拟模式 — 请求已记录（未实际发送）"
  }
}
```

### 6.3 Webhook 风控测试 ✅

```
频率限制: 订单请求过于频繁，请间隔 1000ms 以上
→ 风控正常运行
```

### 6.4 Webhook 平仓信号 (longX BTCUSDT) ✅

```json
{
  "success": true,
  "signal": "longX",
  "symbol": "BTCUSDT",
  "orderResult": {
    "code": "0",
    "msg": "模拟平仓成功: BTC-USDT long 0.001"
  }
}
```

## 7. 模拟日志摘要

```
[OKX] 🧪 模拟模式已启用 — 所有操作仅输出日志，不实际连接交易所
[OKX] 📤 下单: [BTC-USDT] Buy 0.001 @ market
[OKX] ✅ 订单成功: orderId=sim-1784983117271
[OKX] [模拟持仓] BTC-USDT: 0.0010
[OKX] 📤 平仓: BTC-USDT
[OKX] [模拟] 平仓 BTC-USDT (long): 0.001
```

## 8. 安全说明

- **OKX API 密钥**: 未配置（模拟模式不需要）
- **防火墙**: 服务器未启用 iptables/ufw，由云平台安全组控制
- **建议**: 如果开放外网访问 3000 端口，请在云平台安全组限制来源 IP
- **HTTPS**: Caddy 已运行在 80/443 端口（`shtdjf.indevs.in`），可通过 Caddy 反向代理添加 HTTPS 保护

## 9. 一键重启

```bash
cd /root/levan-do-strategies/services && pm2 restart webhook
```

---

## 部署结论

| 检查项 | 状态 |
|--------|------|
| Node.js 运行环境 | ✅ v22.23.1 |
| Git Clone 仓库 | ✅ levan-do-strategies |
| npm install | ✅ 72 packages, 0 漏洞 |
| .env 配置 (DRY_RUN=true) | ✅ 已配置 |
| PM2 进程管理 | ✅ 运行中 + 开机自启 |
| GET /health | ✅ HTTP 200 |
| POST /webhook (开仓) | ✅ 模拟下单成功 |
| POST /webhook (平仓) | ✅ 模拟平仓成功 |
| 风控验证 | ✅ 频率限制正常 |
| 模拟日志 | ✅ 所有操作仅输出日志 |

**服务地址**: `http://43.133.210.83:3000`
**健康检查**: `GET http://43.133.210.83:3000/health`
**Webhook 端点**: `POST http://43.133.210.83:3000/webhook`
