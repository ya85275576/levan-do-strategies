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

# 简洁通知行（适合推送/短信）
node scripts/check-status.js --notify

# POST 结果到外部 webhook 服务
node scripts/check-status.js --webhook=https://your-webhook.example.com/endpoint

# 组合使用：保存状态 + 通知行 + webhook
node scripts/check-status.js --save-state --notify --webhook=https://your-webhook.example.com/endpoint

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000
```

## 标志说明

| 标志 | 说明 |
|------|------|
| `--json` | JSON 格式输出（程序化解析） |
| `--save-state` | 保存状态到文件，下次运行时增量对比 |
| `--notify` | 输出简洁通知行（一行内概括核心状态） |
| `--webhook=<URL>` | POST 结果到指定 URL（JSON body） |

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。  
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。
