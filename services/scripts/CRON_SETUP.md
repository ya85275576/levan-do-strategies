# 定时检查 — Cron 配置

## 安装定时任务

```bash
# 编辑 crontab
crontab -e
```

### 推荐：每 15 分钟完全检查（保存状态 + 通知）

```bash
# 每 15 分钟执行一次完整检查，结果追加到日志文件
# 当检测到新信号时，输出会包含「+N 新信号」；无新信号时输出「无新信号」
# 输出到终端的只有一行简洁通知，适用于快速浏览日志
*/15 *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --full >> /var/log/bot-check.log 2>&1
```

### 每 5 分钟快速检查（开发/调试用）

```bash
*/5  *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --notify
```

### 发送到 Slack / Discord Webhook

```bash
# 每 15 分钟检查并通过 Slack Webhook 发送报告
*/15 *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --webhook-url=https://hooks.slack.com/services/xxx/xxx/xxx
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

# 标准完整报告（带颜色）
node scripts/check-status.js
# 或
npm run check

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 保存状态并增量对比（推荐用于 cron）
node scripts/check-status.js --save-state
# 或
npm run check:save

# 单行简洁通知模式（适合日志/推送）
node scripts/check-status.js --notify
# 或
npm run check:notify

# 输出 Slack/Discord 兼容 JSON payload 到 stdout
node scripts/check-status.js --webhook
# 或
npm run check:webhook

# 发送报告到外部 Webhook URL
node scripts/check-status.js --webhook-url=https://hooks.slack.com/services/xxx/xxx/xxx

# 全量模式：同时保存状态并输出通知（cron 推荐）
node scripts/check-status.js --full
# 或
npm run check:full

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000
```

## 输出模式说明

| 模式 | 命令 | 输出内容 | 推荐场景 |
|------|------|---------|---------|
| **默认** | `--save-state` | 完整彩色报告，含进程/交易所/信号/持仓/系统资源 | 交互式终端查看 |
| **JSON** | `--json` | 完整数据结构 JSON | 程序化解析 / 二次处理 |
| **通知** | `--notify` | 一行简洁摘要 + 新信号详情（如有） | 日志 / 终端快速浏览 |
| **Webhook stdout** | `--webhook` | Slack/Discord 兼容 JSON payload | 管道到其他工具 |
| **Webhook POST** | `--webhook-url=URL` | POST JSON payload 到指定 URL | Slack / Discord / Telegram 集成 |
| **全量** | `--full` | 保存状态 + 通知输出（--save-state + --notify） | **Cron 定时任务推荐** |

### 全量模式（--full）详解

`--full` 模式是专为定时任务设计的组合模式，相当于 `--save-state --notify`：

1. ✅ **自动保存状态**：将当前信号总数写入 `.check-state.json`，下次运行时自动对比增量
2. ✅ **简洁输出**：输出一行通知摘要（含新信号数/进程/交易所/系统状态）
3. ✅ **有新信号时展开详情**：如果检测到新信号，自动列出最近信号详情
4. ✅ **系统异常时显示完整报告**：如果进程/交易所/系统有异常，自动附带完整报告

**无新信号时的典型输出：**
```
LE VAN DO® | 📡 无新信号 (共 42) · ✅ 进程正常 · 🔗 已连接 · 🖥️ 系统正常
  ℹ️ 无新信号产生 — 上次检查后无变化
```

**有新信号时的典型输出：**
```
LE VAN DO® | 📡 +3 新信号 (共 45) · 📗多开 BTCUSDT · ✅ 进程正常 · 🔗 已连接 · 🖥️ 系统正常
  📗 多头开仓 BTCUSDT @ $67500.00 — 2024/03/20 14:30:00
  📕 空头开仓 ETHUSDT @ $3450.00 — 2024/03/20 14:15:00
  📘 多头平仓 SOLUSDT @ $180.00 — 2024/03/20 14:00:00
```

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。

下次使用 `--save-state` 或 `--full` 时，脚本会自动对比信号增量并在报告中显示：

```json
{
  "lastCheck": "2024-03-20T06:30:00.000Z",
  "totalSignals": 42,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true
}
```
