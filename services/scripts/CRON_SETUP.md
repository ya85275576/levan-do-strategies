# 定时检查 — Cron 配置

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# 每 15 分钟检查一次（东京服务器），结果追加到日志文件
# 注意：将下方的 */15 改为实际 cron 表达式
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state >> /var/log/bot-check.log 2>&1

# 每 5 分钟检查一次，附带通知（完整模式：保存状态 + 通知一行）
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --full >> /var/log/bot-check.log 2>&1

# 发送到 Slack/Discord Webhook（每 15 分钟）
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --webhook-url=https://hooks.slack.com/services/xxx/yyy/zzz >> /var/log/bot-check.log 2>&1
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

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 保存状态并增量对比
npm run check:save

# 简洁通知一行（用于日志聚合）
npm run check:notify

# 输出 Slack/Discord 兼容 JSON payload
npm run check:webhook

# 完整模式：保存状态 + 通知一行
npm run check:full

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000
```

## 输出模式详解

| 模式 | 命令 | 说明 |
|------|------|------|
| 默认 | `npm run check` 或 `node scripts/check-status.js` | 完整彩色表格报告 |
| JSON | `--json` | 完整数据对象 |
| 保存状态 | `--save-state` / `npm run check:save` | 保存信号快照，下次增量对比 |
| 通知 | `--notify` / `npm run check:notify` | 单行简洁状态通知 |
| Webhook | `--webhook` / `npm run check:webhook` | 输出 Slack/Discord 兼容 JSON
| Webhook URL | `--webhook-url=URL` | POST JSON 到外部 Webhook
| 完整模式 | `--full` / `npm run check:full` | `--save-state` + `--notify` 合并 |

> **💡 提示**：`--full` 模式适合 cron 定时任务，既保存增量状态，又输出一行简洁状态供日志检索。

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。  
下次使用 `--save-state` 或 `--full` 时，脚本会自动对比信号增量并在报告中显示。

## 定时任务示例：发送到通知机器人

```bash
# 编辑 crontab
crontab -e

# 每 15 分钟：完整模式（保存状态 + 通知一行）
*/15 * * * * cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --full >> /var/log/bot-check.log 2>&1

# 每 1 小时：发送到 Discord Webhook
0 * * * * cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --webhook-url=https://discord.com/api/webhooks/xxx/yyy >> /var/log/bot-webhook.log 2>&1
```
