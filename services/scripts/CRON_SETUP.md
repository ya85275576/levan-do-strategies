# 定时检查 — Cron 配置

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# 每 15 分钟检查一次（东京服务器），结果追加到日志文件
# 注意：将下方的 */15 改为实际 cron 表达式
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state >> /var/log/bot-check.log 2>&1

# 每 5 分钟检查一次（开发/调试用）
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state

# 简洁通知行模式（适合追加到 IM/通知频道）
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state --notify >> /var/log/bot-notify.log 2>&1

# 完整报告 + Webhook 推送
# 先设置 WEBHOOK_URL 环境变量指向外部服务端点
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && WEBHOOK_URL=https://hooks.example.com/okx-bot node scripts/check-status.js --save-state --webhook >> /var/log/bot-check.log 2>&1
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

# 仅输出简洁通知行（单行，适合 IM/通知）
node scripts/check-status.js --notify

# 保存状态 + 通知行
node scripts/check-status.js --save-state --notify

# 推送状态到外部 Webhook
node scripts/check-status.js --webhook

# 指定自定义 Webhook URL
node scripts/check-status.js --webhook-url=https://hooks.example.com/okx-bot

# 指定自定义 API URL
node scripts/check-status.js --url=http://localhost:3000

# 完整模式：保存状态 + 通知行 + webhook 推送
node scripts/check-status.js --save-state --notify --webhook
```

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。

## 环境变量

| 变量 | 说明 |
|------|------|
| `API_URL` | API 端点地址，默认 `http://43.133.210.83:3000` |
| `WEBHOOK_URL` | 外部 Webhook 推送地址，用于 `--webhook` 模式 |

## 通知行格式示例

```
🟢 🧪 OKX Bot: 在线 | 信号:42 [+3] | 持仓:2 | 内存:45.2% | 最新:longE@BTC
🔴 🧪 OKX Bot: 断开 | 信号:42 | 空仓 | 内存:45.2%
❌ OKX Bot: 检查失败 — 连接服务器失败: fetch failed
```
