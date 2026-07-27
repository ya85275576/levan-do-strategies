# 定时检查 — Cron 配置

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# 每 15 分钟检查一次（东京服务器），结果追加到日志文件
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state >> /var/log/bot-check.log 2>&1

# 每 5 分钟检查一次（开发/调试用）
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state

# 每 15 分钟检查并发送简洁通知到日志
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --notify --save-state 2>&1 | tail -5 >> /var/log/bot-notify.log

# 每 15 分钟检查并通过 Webhook 推送（替换为你的 webhook URL）
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --webhook-url=https://hooks.example.com/webhook --save-state >> /var/log/bot-check.log 2>&1
```

## npm scripts

```bash
# 标准检查（完整报告带颜色）
cd /root/levan-do-strategies/services && npm run check

# 简洁通知模式（单行 + 新增信号列表）
npm run check:notify

# Webhook JSON 输出到 stdout
npm run check:webhook

# 完整模式：增量对比 + 通知 + webhook JSON
npm run check:full
```

## CLI 选项

| 选项 | 说明 |
|------|------|
| `--url=http://...` | 指定 API 地址（默认: http://43.133.210.83:3000） |
| `--json` | 输出完整 JSON 结果到 stdout |
| `--save-state` | 保存信号快照到 `.check-state.json`，用于下次增量对比 |
| `--notify` | 单行简洁通知模式，有新增信号时列出最近 3 条 |
| `--webhook` | 输出 webhook payload（JSON）到 stdout |
| `--webhook-url=URL` | POST webhook payload 到指定 URL（兼容 Slack/Discord/Telegram） |

## Webhook Payload 格式

推送的 JSON payload 包含以下字段：

```json
{
  "type": "new_signals | heartbeat | error",
  "title": "📡 LE VAN DO® — 3 个新信号",
  "message": "简洁文本消息（兼容 Slack/Discord）",
  "timestamp": "2026-07-25T12:00:00.000Z",
  "serverUrl": "http://43.133.210.83:3000",

  "signals": {
    "total": 42,
    "newSignals": 3,
    "counts": { "longE": 15, "shortE": 10, "longX": 10, "shortX": 7 },
    "recent": [
      { "time": "...", "type": "longE", "symbol": "BTCUSDT", "price": "65420.5" }
    ]
  },

  "processes": {
    "allOnline": true,
    "count": 1,
    "offline": [],
    "details": [ { "name": "webhook", "status": "online", "pid": 1234, "cpu": 0.5, "memory": 12345678 } ]
  },

  "exchange": {
    "name": "OKX", "network": "TESTNET", "status": "connected",
    "dryRun": true, "leverage": 1, "accountStatus": "USDT 余额: $10000.00"
  },

  "positions": [
    { "symbol": "BTCUSDT", "side": "long", "size": 0.001, "entry_price": 65420.5, "current_price": 66800.0, "pnl": 1.38, "pnl_pct": 2.1 }
  ],

  "system": {
    "memory": { "usagePercent": 45 },
    "disk": { "usagePercent": 62 },
    "cpu": { "loadAvg": [0.5, 0.3, 0.2], "cores": 4 },
    "warnings": []
  },

  "healthy": true,

  "slack": { "text": "...", "blocks": [...] },
  "discord": { "embeds": [...] }
}
```

## 查看检查日志

```bash
# 查看最近检查结果
tail -f /var/log/bot-check.log

# 查看简洁通知日志
tail -f /var/log/bot-notify.log

# 查看上次记录的状态
cat /root/levan-do-strategies/services/.check-state.json
```

## 手动运行

```bash
cd /root/levan-do-strategies/services

# 标准输出（带颜色）
node scripts/check-status.js

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 保存状态并增量对比
node scripts/check-status.js --save-state

# 简洁通知模式
node scripts/check-status.js --notify

# Webhook payload 输出到 stdout
node scripts/check-status.js --webhook

# 完整模式：检查 + 通知 + webhook 推送
node scripts/check-status.js --save-state --notify --webhook

# 推送 webhook 到外部 URL
node scripts/check-status.js --webhook-url=https://hooks.example.com/webhook

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000
```

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。  
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。

## 通知周期建议

| 频率 | 场景 | 命令 |
|------|------|------|
| 每 15 分钟 | 标准定时检查 | `--save-state` |
| 每 15 分钟 | 简洁通知 (无新增不打扰) | `--notify --save-state` |
| 每 30 分钟 | Webhook 推送到 Slack/Discord | `--webhook-url=URL --save-state` |
| 每 15 分钟 | 完整模式 (本地+webhook) | `npm run check:full` |
