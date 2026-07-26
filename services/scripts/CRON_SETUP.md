# 定时检查 — Cron 配置

## npm Script 快速命令

```bash
cd /root/le-van-do-strategies/services

# 标准全量报告（带颜色）
npm run check

# 简洁通知行（适合 Telegram/Push 通知）
npm run check:notify

# 推送到 Webhook（兼容 Slack/Discord/Telegram 格式）
npm run check:webhook

# 全量报告 + 状态保存
npm run check:full
```

## 手动运行

```bash
cd /root/le-van-do-strategies/services

# 标准输出（带颜色）
node scripts/check-status.js

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 保存状态并增量对比（推荐定时任务使用）
node scripts/check-status.js --save-state

# 简洁通知行（仅输出一行关键摘要）
node scripts/check-status.js --notify --save-state

# 推送到外部 Webhook（输出 JSON payload 到 stdout，适合管道）
node scripts/check-status.js --webhook --save-state

# 推送到 Slack/Discord/Telegram Webhook
node scripts/check-status.js --webhook --webhook-url=https://hooks.slack.com/services/xxx

# 自定义 API URL
node scripts/check-status.js --url=http://localhost:3000
node scripts/check-status.js --url=http://43.133.210.83:3000
```

## 安装定时任务

```bash
# 编辑 crontab
crontab -e
```

### 推荐方案：每 15 分钟检查 + 状态保存（增量对比）

```
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/le-van-do-strategies/services && node scripts/check-status.js --save-state >> /var/log/bot-check.log 2>&1
```

### 方案二：每 15 分钟检查 + 增量对比 + 简洁通知（适合通知栏）

```
  */15 *   *   *   *    cd /root/le-van-do-strategies/services && node scripts/check-status.js --notify --save-state >> /var/log/bot-check.log 2>&1
```

### 方案三：每 15 分钟检查 + 推送到 Webhook（Slack/Discord/Telegram）

```
  */15 *   *   *   *    cd /root/le-van-do-strategies/services && node scripts/check-status.js --webhook --save-state >> /var/log/bot-check.log 2>&1
```

### 方案四：双重推送（每 5 分钟精简 + 每 30 分钟完整）

```
  */5  *   *   *   *    cd /root/le-van-do-strategies/services && node scripts/check-status.js --notify --save-state >> /var/log/bot-check.log 2>&1
  */30 *   *   *   *    cd /root/le-van-do-strategies/services && node scripts/check-status.js --webhook --save-state >> /var/log/bot-check.log 2>&1
```

### 方案五：开发/调试用（每 5 分钟）

```
  */5  *   *   *   *    cd /root/le-van-do-strategies/services && node scripts/check-status.js --save-state
```

## 各模式输出示例

### `--notify` 模式（单行简洁输出）

```
[OKX Bot] ✅ · 信号:42 · 🆕+3 · 进程✅ · 交易✅ · 系统✅ · L↑ BTCUSDT $68520
[OKX Bot] ✅ · 信号:42 · 🟢无新 · 进程✅ · 交易✅ · 系统✅
[OKX Bot] ⚠️ · 信号:35 · 🟢无新 · 进程❌ · 交易✅ · 系统✅
```

### `--webhook` 模式（输出 / POST 兼容 Payload）

- 不带 `--webhook-url=`：输出 JSON payload 到 stdout（可 `|` 管道到其他程序）
- 带 `--webhook-url=`：POST 到指定 Webhook 地址（Slack/Discord/Telegram 兼容）

Payload 格式：

```json
{
  "text": "✅ OKX Bot 运行正常 | 信号 42 (+3)",
  "attachments": [{
    "color": "good",
    "title": "✅ OKX Bot 运行正常",
    "fields": [
      { "title": "信号总数", "value": "42", "short": true },
      { "title": "新增信号", "value": "+3", "short": true },
      { "title": "进程状态", "value": "✅ 正常", "short": true },
      { "title": "交易所",   "value": "✅ 已连接", "short": true },
      { "title": "系统健康", "value": "✅ 正常", "short": true },
      { "title": "最新信号", "value": "L↑ BTCUSDT $68520\nS↓ ETHUSDT $3520", "short": false }
    ],
    "ts": 1711440000
  }]
}
```

## 查看检查日志

```bash
# 查看最近检查结果
tail -f /var/log/bot-check.log

# 查看上次记录的状态
cat /root/le-van-do-strategies/services/.check-state.json
```

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。

```json
{
  "lastCheck": "2026-07-25T12:00:00.000Z",
  "totalSignals": 42,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true
}
```

## 服务器地址

| 环境 | URL |
|------|-----|
| **东京服务器** | http://43.133.210.83:3000 |
| **本地开发** | http://localhost:3000 |

## 监控告警参考

| 异常状况 | 建议处理 |
|----------|---------|
| ❌ 进程离线 | 登录服务器执行 `pm2 restart all` |
| ❌ 交易所断开 | 检查 API Key 是否过期、网络是否正常 |
| 🔴 内存 > 80% | 考虑扩容或检查内存泄漏 |
| 🔴 CPU 负载过高 | 检查是否有异常进程 |
| 🆕 新增信号 | 关注最新交易信号，检查持仓变化 |
