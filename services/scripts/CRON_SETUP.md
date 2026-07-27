# 定时检查 — Cron 配置

## NPM Scripts 快捷方式

```bash
cd /root/levan-do-strategies/services

# 标准检查 + 保存状态（增量对比）
npm run check

# 简洁通知（适合日志轮转）
npm run check:notify

# 输出 Webhook JSON 到 stdout
npm run check:webhook

# 全量检查 + 简洁通知
npm run check:full
```

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# 每 15 分钟检查一次（东京服务器），结果追加到日志文件
# 注意：将下方的 */15 改为实际 cron 表达式
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --save-state >> /var/log/bot-check.log 2>&1

# 每 15 分钟检查一次，简洁通知到日志
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --save-state --notify >> /var/log/bot-notify.log 2>&1

# 每 30 分钟发送 Webhook 到外部服务（如 Slack/Discord/Telegram Bot）
# Min Hour Day Mon Week  command
  */30 *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --save-state --webhook-url=https://hooks.example.com/bot-status >> /var/log/bot-webhook.log 2>&1

# 每 5 分钟检查一次（开发/调试用）
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --save-state
```

## 查看检查日志

```bash
# 查看最近检查结果
tail -f /var/log/bot-check.log

# 查看简洁通知日志
tail -f /var/log/bot-notify.log

# 查看 Webhook 投递日志
tail -f /var/log/bot-webhook.log

# 查看上次记录的状态
cat /root/levan-do-strategies/services/.check-state.json
```

## 手动运行

```bash
cd /root/levan-do-strategies/services

# 标准输出（带颜色）
node scripts/check-status.js

# 标准输出 + 保存状态增量对比
node scripts/check-status.js --save-state

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 单行简洁通知（适合 Telegram / 日志）
node scripts/check-status.js --notify

# Webhook JSON payload 输出到 stdout
node scripts/check-status.js --webhook

# POST Webhook payload 到外部 URL（如 Slack Webhook）
node scripts/check-status.js --webhook-url=https://hooks.slack.com/services/xxx/yyy/zzz

# 组合使用
node scripts/check-status.js --save-state --notify

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000
node scripts/check-status.js --url=http://localhost:3000 --webhook-url=http://localhost:9000/hook
```

## Webhook Payload 格式

`--webhook` 输出的 JSON payload 结构如下（兼容 Slack/Discord/Telegram Bot 解析）：

```json
{
  "id": "uuid",
  "event": "bot_status_check",
  "timestamp": "2026-07-27T04:00:00.000Z",
  "server": {
    "url": "http://43.133.210.83:3000",
    "uptime": "20h 19m",
    "hostname": "VM-0-12-ubuntu"
  },
  "processes": [
    {
      "name": "le-van-do-bot",
      "status": "online",
      "pid": 12345,
      "uptime": "4h 8m",
      "restartCount": 0,
      "cpu": 0,
      "memory": 167043072
    }
  ],
  "exchange": {
    "name": "OKX / MT5",
    "network": "TESTNET",
    "isTestnet": true,
    "dryRun": true,
    "status": "connected"
  },
  "signals": {
    "total": 818,
    "newSinceLastCheck": 5,
    "counts": { "longE": 106, "shortE": 36, "longX": 188, "shortX": 81 },
    "recent": [
      { "time": "...", "type": "longE", "symbol": "BTC-USDT", "price": "50000" }
    ]
  },
  "positions": [
    { "symbol": "BTC-USDT", "side": "long", "size": 0.1, "price": 50000 }
  ],
  "system": {
    "memory": { "total": 2063228928, "used": 948785152, "usagePercent": 46 },
    "disk": { "total": 52721041408, "used": 13956423680, "usagePercent": 26 },
    "cpu": { "cores": 2, "loadAvg": [0.14, 0.31, 0.23] }
  },
  "healthy": {
    "all": true,
    "processes": true,
    "exchange": true,
    "system": true
  }
}
```

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。  
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。

## 通知模式对比

| 模式 | 命令行 | 输出内容 | 用途 |
|------|--------|----------|------|
| 标准 | `(无)` | 完整彩色报告 | 终端查看全部信息 |
| JSON | `--json` | 完整 JSON 对象 | 程序化解析 |
| 通知 | `--notify` | 单行简洁文本 | 日志/Telegram/Discord |
| Webhook | `--webhook` | 结构化 JSON payload | CI/CD 管道、外部系统 |
| Webhook URL | `--webhook-url=URL` | POST JSON 到指定 URL | Slack/Discord/Telegram Bot |
| 增量 | `--save-state` | 报告中显示 +N 新信号 | 定时任务 |
