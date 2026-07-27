# 🕐 定时检查 — Crontab 配置

## 简介

`scripts/check-status.js` 支持定时检查 OKX 交易机器人的信号状态，并通过多种模式输出结果。

## 定时任务配置

### 基础检查（每 15 分钟）

```bash
crontab -e
```

添加以下行：

```cron
# LE VAN DO® 状态检查 — 每 15 分钟
*/15 * * * * cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --save-state >> /var/log/levando-check.log 2>&1
```

### 完整模式（每 30 分钟，含简洁通知 + 增量检测）

```cron
# LE VAN DO® 完整检查 — 每 30 分钟（有新信号时输出详细报告）
*/30 * * * * cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --full >> /var/log/levando-check.log 2>&1
```

### 带 Webhook 推送（每 15 分钟，发送到 Discord/Slack）

```cron
# LE VAN DO® Webhook 推送 — 每 15 分钟
*/15 * * * * cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --save-state --webhook-url=https://hooks.slack.com/services/xxx/yyy/zzz >> /var/log/levando-webhook.log 2>&1
```

## 使用模式

| 模式 | 命令 | 输出 | 适用场景 |
|------|------|------|----------|
| **基础检查** | `npm run check` | 详细报告 | 手动查看完整状态 |
| **增量检测** | `npm run check:save` | 详细报告 + 保存状态 | 定时任务，对比上次信号数 |
| **简洁通知** | `npm run check:notify` | 单行摘要 | 推送到 Telegram/Pushover |
| **Webhook JSON** | `npm run check:webhook` | Slack/Discord 兼容 JSON 到 stdout | 管道传给其他工具 |
| **完整模式** | `npm run check:full` | 简洁通知 + 状态保存，有新信号时附详细报告 | 定时任务首选 |

## 状态文件

脚本自动维护 `.check-state.json`，用于增量检测：

```json
{
  "lastCheck": "2026-07-27T17:00:00.000Z",
  "totalSignals": 820,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true
}
```

## 无新信号时的行为

- `--notify` / `--full` 模式：输出 `📭 无新信号` 标识
- `--save-state` 模式：报告末尾显示「无新信号」
- Webhook 模式：`新增信号` 字段值为 `0 (无新信号)`，附件颜色为灰色 `#cccccc`
