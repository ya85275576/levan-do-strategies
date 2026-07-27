# 定时检查 — Cron 配置

## 可用 NPM Scripts

```bash
# 完整报告（默认）
npm run check

# 完整报告 + 保存状态（增量对比）
npm run check:save

# 单行简洁通知 + 保存状态
npm run check:notify

# Webhook JSON payload 输出到 stdout
npm run check:webhook

# 完整模式（--save-state + --notify 同时生效）
npm run check:full
```

## CLI 参数说明

| 参数 | 说明 |
|------|------|
| `--url=URL` | 自定义 API 地址 (默认 `http://43.133.210.83:3000`) |
| `--json` | JSON 格式输出 |
| `--save-state` | 自动保存状态到文件，下次运行时增量对比信号数量 |
| `--notify` | 单行简洁通知输出（适合日志/Telegram/Push） |
| `--webhook` | 输出 Webhook JSON payload 到 stdout（Slack/Discord 格式） |
| `--webhook-url=URL` | POST Webhook JSON payload 到指定 URL（兼容 Slack/Discord） |
| `--full` | 同时开启 `--save-state` + `--notify` |

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# === 方案一：每 15 分钟检查，简洁通知到日志 ===
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && npm run check:notify >> /var/log/bot-check.log 2>&1

# === 方案二：每 5 分钟检查，完整报告（开发/调试用） ===
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/levan-do-strategies/services && npm run check:save

# === 方案三：发送 Webhook 到 Slack/Discord ===
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --notify --webhook-url=https://hooks.slack.com/services/xxx/xxx/xxx >> /var/log/bot-check.log 2>&1

# === 方案四：每 30 分钟，完整检查 + 保存状态 ===
# Min Hour Day Mon Week  command
  */30 *   *   *   *    cd /root/levan-do-strategies/services && npm run check:full >> /var/log/bot-check.log 2>&1
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

# 标准输出（带颜色）
node scripts/check-status.js

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 保存状态并增量对比
node scripts/check-status.js --save-state

# 单行简洁通知
node scripts/check-status.js --notify

# 单行通知 + 保存状态
node scripts/check-status.js --notify --save-state

# 输出 Webhook payload 到 stdout
node scripts/check-status.js --webhook

# POST Webhook 到 Slack/Discord
node scripts/check-status.js --webhook-url=https://hooks.slack.com/services/YOUR/WEBHOOK/URL

# 完整模式
node scripts/check-status.js --full

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000
```

## 输出格式示例

### 简洁通知模式 (`--notify`)
```
🤖 Bot Check | ✅ 进程正常 | 🔗 交易所已连接 | 📡🆕 📊 45 (+3) | 🖥️ 系统正常 | 🕐 07-25 14:30:00
```

### 无新信号时
```
🤖 Bot Check | ✅ 进程正常 | 🔗 交易所已连接 | 📡 📊 45 无新信号 | 🖥️ 系统正常 | 🕐 07-25 14:30:00
```

### Webhook Payload (`--webhook`)
```json
{
  "text": "🤖 LE VAN DO® — OKX 交易机器人状态报告",
  "attachments": [
    {
      "color": "#36a64f",
      "title": "📋 概览",
      "fields": [
        { "title": "检查时间", "value": "2026-07-25 14:30:00", "short": true },
        { "title": "信号总数", "value": "45", "short": true }
      ]
    }
  ]
}
```

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。

```json
{
  "lastCheck": "2026-07-25T14:30:00.000Z",
  "totalSignals": 45,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true
}
```

下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。

## 结合 Push/Telegram 通知

对于 Push 通知（如 Bark / Pushover / PushDeer），可以将 `--notify` 的输出通过管道发送：

```bash
# Bash 函数方式
node scripts/check-status.js --notify --save-state | xargs -I{} curl -s "https://api.day.app/YOUR_BARK_KEY/{}"

# 或写入临时文件再 POST
node scripts/check-status.js --notify --save-state > /tmp/bot-status.txt
curl -s -X POST "https://api.telegram.org/botYOUR_TOKEN/sendMessage" \
  -d "chat_id=YOUR_CHAT_ID" \
  -d "text=$(cat /tmp/bot-status.txt)"
```
