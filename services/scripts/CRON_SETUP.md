# 定时检查 — Cron 配置

## 快速开始（npm scripts）

```bash
cd /root/levan-do-strategies/services

# 标准检查（详细报告）
npm run check

# 保存状态并增量对比
npm run check:save

# 简洁通知行（适合推送/通知栏）
npm run check:notify

# 发送结果到外部 Webhook 服务
npm run check:webhook

# 完整模式：保存状态 + 通知行 + Webhook
npm run check:full
```

## CLI 参数说明

| 参数 | 说明 |
|---|---|
| `--save-state` | 保存当前信号总数到 `.check-state.json`，下次运行时自动对比增量 |
| `--notify` | 输出一行简洁通知（无颜色代码），适合推送、Telegram、Discord 等 |
| `--webhook` | 将检查结果 POST 发送到默认 Webhook 地址 |
| `--webhook-url=URL` | 指定自定义 Webhook 接收地址 |
| `--json` | JSON 格式输出 |
| `--url=URL` | 指定检查的服务器地址 |

环境变量：`CHECK_WEBHOOK_URL` — 设置默认 Webhook 地址

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# 每 15 分钟完整检查一次，结果追加到日志文件
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && npm run check:save >> /var/log/bot-check.log 2>&1

# 每 5 分钟带通知的检查（开发/调试用）
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/levan-do-strategies/services && npm run check:notify
```

## --notify 输出示例

一行简洁格式，适合推送通知：

```
🤖 LE VAN DO® | ✅进程 🔗交易 🖥️系统 | 📡1025信号(+3新增) | latest: shortE ZAMA-USDT-SWAP $0.05 | 内存44% 磁盘26% | 2026/07/27 04:00:00
```

无新增信号时：

```
🤖 LE VAN DO® | ✅进程 🔗交易 🖥️系统 | 📡1025信号(无新增) | 内存44% 磁盘26% | 2026/07/27 04:00:00
```

系统异常时（进程/交易所/资源任一不正常）：

```
🤖 LE VAN DO® | ❌进程 🔗交易 🖥️系统 | 📡1025信号 | 内存44% 磁盘26% | 2026/07/27 04:00:00
```

## Webhook 发送格式

`--webhook` 会 POST 以下 JSON 到目标地址：

```json
{
  "timestamp": "2026-07-27T04:00:00.000Z",
  "serverUrl": "http://43.133.210.83:3000",
  "summary": {
    "totalSignals": 1025,
    "newSignals": 3,
    "processesOnline": true,
    "exchangeOnline": true,
    "systemHealthy": true
  },
  "notifyLine": "🤖 LE VAN DO® | ✅进程 🔗交易 🖥️系统 | 📡1025信号(+3新增) | ...",
  "alert": false
}
```

`alert: true` 表示系统存在异常，接收方可根据此字段触发告警。

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

# 简洁通知行
node scripts/check-status.js --notify

# 发送到 Webhook
node scripts/check-status.js --webhook

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000

# 完整模式
node scripts/check-status.js --save-state --notify --webhook --url=http://localhost:3000
```

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。  
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。
