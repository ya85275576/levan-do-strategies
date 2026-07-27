# 定时检查 — Cron 配置

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# 每 15 分钟检查一次（东京服务器），完整模式（保存状态 + 通知输出）
# 注意：将下方的 */15 改为实际 cron 表达式
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --full >> /var/log/bot-check.log 2>&1

# 每 5 分钟检查一次（开发/调试用）
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --full

# 发送到 Slack/Discord Webhook（通知 + 推送）
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --full --webhook-url=https://hooks.slack.com/services/xxx >> /var/log/bot-check.log 2>&1
```

## CLI 选项

| 选项 | 说明 |
|------|------|
| `（无参数）` | 标准输出（带颜色格式化报告） |
| `--url=http://localhost:3000` | 指定 API 地址 |
| `--json` | JSON 格式输出 |
| `--save-state` | 自动保存状态到文件，下次对比增量 |
| `--notify` | 单行简洁通知输出 |
| `--webhook` | 输出 Slack/Discord 兼容 JSON payload 到 stdout |
| `--webhook-url=URL` | POST 结果到外部 Webhook（Slack/Discord/自建） |
| `--full` | save-state + notify 同时开启 |

## npm scripts

```bash
cd /root/levan-do-strategies/services

npm run check              # 标准检查
npm run check:save         # 检查并保存状态
npm run check:notify       # 简洁通知
npm run check:webhook      # Webhook JSON 输出
npm run check:full         # 完整模式（保存状态 + 通知）
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

# 单行通知
node scripts/check-status.js --notify

# Webhook JSON 输出
node scripts/check-status.js --webhook

# 发送到外部 Webhook
node scripts/check-status.js --webhook-url=https://hooks.slack.com/services/T00/B00/xxxxx

# 完整模式
node scripts/check-status.js --full

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000

# 完整模式 + 发送到 Discord
node scripts/check-status.js --full --webhook-url=https://discord.com/api/webhooks/xxx/yyy
```

## 查看检查日志

```bash
# 查看最近检查结果
tail -f /var/log/bot-check.log

# 查看上次记录的状态
cat /root/levan-do-strategies/services/.check-state.json
```

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。
下次使用 `--save-state` 或 `--full` 时，脚本会自动对比信号增量并在报告中显示。

状态文件内容示例：

```json
{
  "lastCheck": "2026-07-25T12:30:00.000Z",
  "totalSignals": 42,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true
}
```

## 通知输出示例

### `--notify` 模式（有新信号）
```
📡 LE VAN DO® 信号报告 | 新信号 +3 | 累计 42 | 最新: shortE ETH-USDT $3450.50 | 进程 ✅ 正常运行 | 交易所 ✅ 已连接
```

### `--notify` 模式（无新信号）
```
📡 LE VAN DO® 信号报告 | 无新信号 (累计 42) | 进程 ✅ 正常运行 | 交易所 ✅ 已连接
```

### `--webhook` 模式输出（Slack/Discord 兼容）
```json
{
  "text": "📡 LE VAN DO® 信号报告 | 无新信号 (累计 42) | 进程 ✅ 正常运行 | 交易所 ✅ 已连接",
  "username": "LE VAN DO® Bot",
  "icon_emoji": ":robot_face:",
  "attachments": [
    {
      "color": "#3fb950",
      "title": "LE VAN DO® — OKX 交易机器人状态报告",
      "fields": [
        { "title": "检查时间", "value": "2026/07/25 20:30:00", "short": true },
        { "title": "信号总数", "value": "42", "short": true },
        { "title": "新增信号", "value": "0", "short": true }
      ],
      "footer": "LE VAN DO® Bot Check"
    }
  ]
}
```

## Webhook 集成（Slack / Discord）

### Slack

1. 创建 Slack App → Incoming Webhooks → 获取 Webhook URL
2. 运行：`node scripts/check-status.js --full --webhook-url=https://hooks.slack.com/services/T00/B00/xxxxx`

### Discord

1. 服务器设置 → 整合 → Webhooks → 新建 Webhook → 复制 URL
2. 运行：`node scripts/check-status.js --full --webhook-url=https://discord.com/api/webhooks/xxx/yyy`

### 自建 Webhook 接收端

```bash
# 使用 nc 快速测试接收
nc -l -p 9999

# 发送到本地
node scripts/check-status.js --webhook-url=http://localhost:9999/webhook
```
