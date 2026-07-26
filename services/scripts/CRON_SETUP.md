# 定时检查 — Cron 配置

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# ======= 标准配置 =======

# 每 15 分钟检查一次，保存状态用于增量对比，输出追加到日志
*/15 * * * * cd /root/.tds/workspaces/019f9e4c-06fc-7781-9897-b8a579addece/services && /usr/bin/node scripts/check-status.js --save-state >> /var/log/bot-check.log 2>&1

# 如需简洁通知行推送（配合 --webhook），每 15 分钟一次
*/15 * * * * cd /root/.tds/workspaces/019f9e4c-06fc-7781-9897-b8a579addece/services && /usr/bin/node scripts/check-status.js --save-state --notify >> /var/log/bot-check.log 2>&1

# ======= 调试配置 =======

# 每 5 分钟检查一次（开发/调试用）
*/5  * * * * cd /root/.tds/workspaces/019f9e4c-06fc-7781-9897-b8a579addece/services && /usr/bin/node scripts/check-status.js --save-state

# ======= Webhook 推送 =======

# 每 15 分钟检查并推送到外部 webhook 服务
# 将 https://your-webhook.url 替换为实际地址
# */15 * * * * cd /root/.tds/workspaces/019f9e4c-06fc-7781-9897-b8a579addece/services && /usr/bin/node scripts/check-status.js --save-state --webhook=https://your-webhook.url >> /var/log/bot-check.log 2>&1
```

## 查看检查日志

```bash
# 查看最近检查结果
tail -f /var/log/bot-check.log

# 查看上次记录的状态
cat /root/.tds/workspaces/019f9e4c-06fc-7781-9897-b8a579addece/services/.check-state.json
```

## 手动运行

```bash
cd /root/.tds/workspaces/019f9e4c-06fc-7781-9897-b8a579addece/services

# 标准输出（带颜色）
npm run check-status

# 保存状态并增量对比
npm run check-status:save

# 简洁通知行（适合推送/消息通知）
npm run check-status:notify

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 保存状态 + 简洁通知行
node scripts/check-status.js --save-state --notify

# 保存状态 + 推送到外部 webhook
node scripts/check-status.js --save-state --webhook=https://your-webhook.url

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000
```

## CLI 选项

| 参数 | 说明 |
|------|------|
| `--url=<URL>` | 指定 API 地址（默认 `http://43.133.210.83:3000`） |
| `--json` | JSON 格式输出 |
| `--save-state` | 保存状态到 `.check-state.json`，下次运行时增量对比 |
| `--notify` | 输出一行简洁通知（可与 `--save-state` 组合） |
| `--webhook=<URL>` | POST 简洁通知到外部 webhook 服务 |

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。

状态格式：
```json
{
  "lastCheck": "2026-07-26T12:00:00.000Z",
  "totalSignals": 44,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true
}
```
