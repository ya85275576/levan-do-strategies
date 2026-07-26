# 定时检查 — Cron 配置

## npm 脚本快捷方式

```bash
# 标准检查
npm run check

# 保存状态 + 增量对比
npm run check:save

# 简洁通知行（适合推送/Telegram）
npm run check:notify

# JSON 格式输出
npm run check:json
```

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# 每 15 分钟检查一次（东京服务器），保存状态并输出通知行，追加到日志
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --save-state --notify >> /var/log/bot-check.log 2>&1

# 每 5 分钟检查一次（开发/调试用）
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --save-state
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
npm run check:json

# 保存状态并增量对比
npm run check:save

# 简洁通知行（适合推送/Telegram 通知）
npm run check:notify

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000

# POST 结果到外部 Webhook 服务
node scripts/check-status.js --webhook https://your-webhook.example.com/hook
```

## 全部 CLI 选项

| 选项 | 说明 |
|------|------|
| `--url=<url>` | 指定 API 服务器地址（默认: `http://43.133.210.83:3000`） |
| `--json` | JSON 格式输出 |
| `--save-state` | 保存状态到 `.check-state.json`，下次执行时增量对比 |
| `--notify` | 简洁通知行输出（一行摘要，适合推送） |
| `--webhook <url>` | POST 结果到指定 Webhook URL |

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。

### 状态文件格式

```json
{
  "lastCheck": "2026-07-27T01:00:32.000Z",
  "totalSignals": 819,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true
}
```

## 通知行示例

```
[Bot状态] 📊 无新信号 | ✅ 进程正常 | 🟢 系统健康
[Bot状态] 📊 +3 新信号 (共822) | 📗 多头开仓 BTC-USDT | ✅ 进程正常 | 🟢 系统健康
```
