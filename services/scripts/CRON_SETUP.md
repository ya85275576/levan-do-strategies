# 定时检查 — Cron 配置

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# ========== 推荐配置 ==========

# 每 15 分钟检查一次（标准模式），结果追加到日志文件
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state >> /var/log/bot-check.log 2>&1

# 每 15 分钟全量模式（保存状态 + 通知输出），适合与通知系统配合
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --full >> /var/log/bot-check-full.log 2>&1

# ========== 调试/开发 ==========

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

# 标准输出（带颜色，完整报告）
npm run check
# 或
node scripts/check-status.js

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 保存状态并增量对比信号
npm run check:save
# 或
node scripts/check-status.js --save-state

# 通知模式（单行简洁输出，适合 Push 通知）
npm run check:notify
# 或
node scripts/check-status.js --notify

# Webhook 模式（输出 Slack/Discord 兼容 JSON payload 到 stdout）
npm run check:webhook
# 或
node scripts/check-status.js --webhook

# 全量模式（保存状态 + 通知输出，一键完成）
npm run check:full
# 或
node scripts/check-status.js --full

# 发送 Webhook 到外部 URL（如 Slack/Discord Webhook）
node scripts/check-status.js --webhook-url=https://hooks.slack.com/services/xxx/yyy/zzz

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000
```

## 输出模式说明

| 模式 | 命令 | 输出内容 | 适用场景 |
|------|------|----------|----------|
| 标准 | `npm run check` | 带颜色的完整报告（含进程/交易所/信号/持仓/系统） | 手动查看 |
| 保存状态 | `npm run check:save` | 完整报告 + 保存 .check-state.json 用于增量对比 | Cron 定时 + 日志 |
| 通知 | `npm run check:notify` | 单行简洁输出（含状态图标+信号增量） | Push 通知、Telegram Bot |
| Webhook | `npm run check:webhook` | JSON payload 输出到 stdout | 管道到其他程序 |
| 全量 | `npm run check:full` | 保存状态 + 通知输出（同时完成） | 一键执行 |
| Webhook URL | `--webhook-url=URL` | POST JSON payload 到外部 Webhook | Slack/Discord 集成 |

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。

```json
{
  "lastCheck": "2025-01-15T10:30:00.000Z",
  "totalSignals": 42,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true
}
```

下次使用 `--save-state` 或 `--full` 时，脚本会自动加载该文件，对比信号增量并在报告中显示。

## 通知模式输出示例

```
✅ 🔗 🖥️ 📡+3 [longE,shortE,longX] 总信号:42 | 2025-01-15 06:30:00
```

格式说明：
- `✅/⚠️` — 进程状态
- `🔗/🔌` — 交易所连接
- `🖥️/💥` — 系统健康
- `📡+N` / `📡-` — 新增信号数（`📡-` 表示无新信号）
- `[type,...]` — 最近信号类型
- `总信号:N` — 累计信号数
- 时间戳（北京时间）

## Webhook Payload 示例

```json
{
  "embeds": [{
    "title": "LE VAN DO® — OKX 交易机器人状态报告",
    "url": "http://43.133.210.83:3000",
    "color": 65280,
    "fields": [
      { "name": "✅ bot", "value": "PID: 1234 | CPU: 2.1% | 内存: 85.3 MB | 运行: 3 天 12 小时", "inline": true },
      { "name": "🔗 交易所", "value": "OKX | ✅ 已连接 | production", "inline": false },
      { "name": "📡 信号统计", "value": "总信号: 42 | longE: 10 | shortE: 8 | longX: 15 | shortX: 9\n新增信号: +3", "inline": false },
      { "name": "🖥️ 系统资源", "value": "内存: 🟢 45.2% (2.3 GB / 5.1 GB)\n磁盘: 🟢 62.1% (45.2 GB / 72.8 GB)\nCPU: 2 核心 | 负载: 0.85", "inline": false }
    ],
    "footer": { "text": "检查时间: 2025-01-15 06:30:00" },
    "timestamp": "2025-01-15T22:30:00.000Z"
  }]
}
```

该 Payload 兼容 Slack Webhook 和 Discord Webhook 格式。
