# 定时检查 — Cron 配置

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# ---- 基本模式 ----
# 每 15 分钟检查一次，结果追加到日志文件
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && npm run check:save >> /var/log/bot-check.log 2>&1

# ---- 完整模式（保存状态 + 通知输出） ----
# 每 15 分钟检查，输出简洁通知行到日志
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && npm run check:full >> /var/log/bot-check.log 2>&1

# ---- 带外部 Webhook 推送 ----
# 每 15 分钟检查，有新信号时推送到 Slack/Discord Webhook URL
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state --webhook-url=https://hooks.slack.com/services/xxx/yyy/zzz >> /var/log/bot-check.log 2>&1

# 每 5 分钟检查一次（开发/调试用）
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state
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
npm run check

# 保存状态并增量对比
npm run check:save

# 简洁通知模式（单行输出）
npm run check:notify

# Webhook JSON 输出（Slack/Discord 兼容）
npm run check:webhook

# 完整模式（保存状态 + 通知）
npm run check:full

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 指定自定义 URL
node scripts/check-status.js --save-state --url=http://localhost:3000

# 发送到外部 Webhook URL
node scripts/check-status.js --save-state --webhook-url=https://hooks.example.com/webhook
```

## 命令行选项

| 选项 | 描述 |
|------|------|
| `--help` | 显示帮助信息 |
| `--url=<URL>` | 自定义 API URL（默认: http://43.133.210.83:3000） |
| `--json` | JSON 格式输出 |
| `--save-state` | 保存状态到 `.check-state.json`，下次运行时增量对比 |
| `--notify` | 单行简洁通知输出（适合推送通知） |
| `--webhook` | 输出 Slack/Discord 兼容的 JSON payload 到 stdout |
| `--webhook-url=<URL>` | POST Slack/Discord 兼容 JSON payload 到指定 URL |
| `--full` | 同时开启 --save-state + --notify |

## 模式说明

### `--notify`（简洁通知）
输出一行简洁的状态文本，适合发送到推送通知服务（如 Bark、Pushover、Telegram Bot）。

示例输出：
```
📡 5 个新信号 | 416 条累计 | 进程正常 | 已连接 | 🟢 MEM 40% | 最近: longE BTC-USDT $65000
```

### `--webhook`（Webhook JSON）
输出 Slack/Discord 兼容的 JSON payload，可直接 pipe 到其他工具。

```bash
node scripts/check-status.js --webhook | jq .
```

### `--webhook-url=URL`（推送通知）
将 Slack/Discord 格式的 JSON payload POST 到外部 Webhook URL（如 Slack Incoming Webhook、Discord Webhook）。

```bash
node scripts/check-status.js --save-state --webhook-url=https://hooks.slack.com/services/T00/B00/xxx
```

### `--full`（完整模式）
组合 `--save-state` 和 `--notify`，同时保存状态并输出简洁通知行。适合 cron 定时任务。

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。
下次使用 `--save-state` 或 `--full` 时，脚本会自动对比信号增量并在报告中显示。

状态文件格式：
```json
{
  "lastCheck": "2026-07-27T08:00:00.000Z",
  "totalSignals": 411,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true
}
```

## 无新信号处理

当上次检查后没有新增信号时，报告会在以下位置显示「无新信号」：
- 信号统计区：累计信号后标注 `(无新信号)`
- 结论区：显示 `📭 无新信号 — 上次检查后无新增信号`
- `--notify` 模式：显示 `✅ 无新信号`
