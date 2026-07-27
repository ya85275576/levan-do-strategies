# 定时检查 — Cron 配置

## 命令行参数

| 参数 | 说明 |
|------|------|
| (无) | 默认：完整彩色报告输出 |
| `--json` | JSON 格式输出（程序化解析） |
| `--save-state` | 自动保存状态到文件，下次运行增量对比 |
| `--notify` | **简洁通知模式**：单行摘要，适合日志/通知 |
| `--webhook` | **Webhook JSON 模式**：输出 Slack/Discord 兼容 payload 到 stdout |
| `--webhook-url=URL` | **Webhook POST 模式**：发送 JSON payload 到外部 URL |
| `--full` | **完整模式**：save-state + notify 同时启用 |
| `--url=URL` | 指定服务器地址（默认 `http://43.133.210.83:3000`） |

## npm scripts（推荐）

```bash
cd /root/levan-do-strategies/services

# 标准检查
npm run check

# 检查并保存状态
npm run check:save

# 简洁通知（单行日志）
npm run check:notify

# 输出 webhook JSON
npm run check:webhook

# 完整模式（保存状态 + 通知，适合 cron）
npm run check:full
```

## 安装定时任务

### 方案一：完整模式（推荐，每 15 分钟）

```bash
# 编辑 crontab
crontab -e

# 每 15 分钟：完整检查 + 保存状态 + 单行通知，日志追加
  */15 *   *   *   *    cd /root/levan-do-strategies/services && npm run check:full >> /var/log/bot-check.log 2>&1
```

### 方案二：带 Webhook 推送（每 15 分钟）

```bash
# 需要先配置 Webhook URL（Slack / Discord / 自建）
# 将 YOUR_WEBHOOK_URL 替换为实际地址
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --full --webhook-url=YOUR_WEBHOOK_URL >> /var/log/bot-check.log 2>&1
```

### 方案三：仅保存状态（静默模式）

```bash
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state > /dev/null 2>&1
```

## 查看检查日志

```bash
# 查看最近检查结果
tail -f /var/log/bot-check.log

# 查看上次记录的状态
cat /root/levan-do-strategies/services/.check-state.json
```

## 手动运行示例

```bash
cd /root/levan-do-strategies/services

# 标准输出（带颜色）
npm run check

# 简洁单行通知
npm run check:notify

# 保存状态并增量对比
npm run check:save

# 完整模式（类似 cron 输出）
npm run check:full

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000

# 推送至 Slack/Discord
node scripts/check-status.js --webhook-url=https://hooks.slack.com/services/XXX/YYY/ZZZ
```

## 输出示例

### `--notify` / `--full` 输出
```
✅ [OKX Bot] 42条累计 | longE BTCUSDT $67890.00 @ 2026-01-15 14:30:05 | 🟢 系统正常 📈 +3新信号
```

无新信号时：
```
✅ [OKX Bot] 42条累计 | longE BTCUSDT $67890.00 @ 2026-01-15 14:30:05 | 🟢 系统正常 ➡️ 无新信号
```

### `--webhook` 输出
```json
{
  "text": "LE VAN DO® OKX Bot 状态报告",
  "attachments": [{
    "color": "#36a64f",
    "fields": [
      { "title": "🤖 进程状态", "value": "✅ 正常运行", "short": true },
      { "title": "📊 累计信号", "value": "42 条", "short": true },
      { "title": "📈 新增信号", "value": "✅ 新增 3 条信号", "short": true },
      { "title": "🖥️ 系统健康", "value": "🟢 正常", "short": true },
      { "title": "📡 最近信号", "value": "...", "short": false }
    ]
  }]
}
```

### `--webhook-url` 推送到 Slack 效果
- 正常：绿色附件 + 状态字段
- 异常：红色附件 + 错误详情
- 检查失败：直接发送失败通知

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。  
下次使用 `--save-state` 或 `--full` 时，脚本会自动对比信号增量并在报告中显示。

```json
{
  "lastCheck": "2026-01-15T14:30:05.000Z",
  "totalSignals": 42,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true
}
```
