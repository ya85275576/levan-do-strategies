# 定时检查 — Cron 配置

## NPM 脚本（推荐）

```bash
# 详细报告 + 状态保存
cd /root/levan-do-strategies/services && npm run check:full

# 简洁通知行（适合 crontab 追日志）
cd /root/levan-do-strategies/services && npm run check:notify

# 检查 + Webhook 推送
cd /root/levan-do-strategies/services && npm run check:webhook

# 快速检查（无状态保存）
cd /root/levan-do-strategies/services && npm run check
```

## 安装定时任务

```bash
# 编辑 crontab
crontab -e
```

### 方案 A：每 15 分钟检查，简洁通知追日志（推荐）

```
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state --notify >> /var/log/bot-check.log 2>&1
```

### 方案 B：每 15 分钟检查 + Webhook 推送

```
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && WEBHOOK_URL=https://your-webhook.example.com/hook node scripts/check-status.js --save-state --webhook >> /var/log/bot-check.log 2>&1
```

### 方案 C：每 5 分钟调试用

```
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state --notify
```

## 查看检查日志

```bash
# 查看最近检查结果
tail -f /var/log/bot-check.log

# 查看上次记录的状态
cat /root/levan-do-strategies/services/.check-state.json
```

## 手动运行

```bash
cd /root/levan-do-strategies/services

# 详细报告（带颜色）
node scripts/check-status.js

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 保存状态并增量对比
node scripts/check-status.js --save-state

# 简洁通知行
node scripts/check-status.js --save-state --notify

# Webhook 推送（需设置 WEBHOOK_URL 环境变量）
WEBHOOK_URL=https://hooks.example.com/hook node scripts/check-status.js --save-state --webhook

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000

# 指定自定义 Webhook URL
node scripts/check-status.js --save-state --notify --webhook --webhook-url=https://hooks.example.com/hook
```

## 参数说明

| 参数 | 说明 |
|------|------|
| `--url=<url>` | 服务器 API 地址（默认 http://43.133.210.83:3000） |
| `--json` | JSON 格式输出 |
| `--save-state` | 保存状态到 `.check-state.json`，支持增量对比 |
| `--notify` | 输出一行简洁通知（适合推送/日志行） |
| `--webhook` | POST 到外部 Webhook 服务 |
| `--webhook-url=<url>` | 指定 Webhook 推送地址（优先于环境变量） |

## 环境变量

| 变量 | 说明 |
|------|------|
| `API_URL` | 服务器 API 地址（默认 http://43.133.210.83:3000） |
| `WEBHOOK_URL` | Webhook 推送地址（默认空） |

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。  
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。

## 通知行示例

```
OKX机器人 | 无新信号，累计 42 | 进程✅ 交易所✅ 系统✅
OKX机器人 | +3 新信号 (多头开2/空头开1)，累计 45 | 进程✅ 交易所✅ 系统✅
OKX机器人 | +1 新信号 (多头开1)，累计 43 | 进程✅ 交易所⚠️ 系统✅
```

## Webhook Payload 示例

```json
{
  "timestamp": "2026-07-27T03:00:00.000Z",
  "server": "http://43.133.210.83:3000",
  "status": "healthy",
  "signals": {
    "total": 42,
    "newSignals": 3,
    "counts": { "longE": 10, "shortE": 8, "longX": 12, "shortX": 12 },
    "recent": [
      { "type": "longE", "symbol": "BTCUSDT", "price": "65420.5", "time": "..." }
    ]
  },
  "processes": { "online": true, "exchange": true, "system": true },
  "notifyLine": "OKX机器人 | +3 新信号 (多头开2/空头开1)，累计 45 | 进程✅ 交易所✅ 系统✅"
}
```
