# 定时检查 — Cron 配置

## 项目路径

```
PROJECT_DIR=/root/.tds/workspaces/019f9d39-efc0-7192-bc45-69a384871183/services
```

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# ============================================================
# 每 15 分钟检查一次（东京服务器），结果追加到日志文件
# ============================================================
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/.tds/workspaces/019f9d39-efc0-7192-bc45-69a384871183/services && /usr/bin/node scripts/check-status.js --save-state >> /var/log/bot-check.log 2>&1

# ============================================================
# 每 15 分钟 + 通知模式（简洁输出，适合配合第三方通知服务）
# ============================================================
# 使用 --notify 标志输出单行通知（可 pipe 到 notify-send / webhook）
  */15 *   *   *   *    cd /root/.tds/workspaces/019f9d39-efc0-7192-bc45-69a384871183/services && /usr/bin/node scripts/check-status.js --save-state --notify >> /var/log/bot-check-notify.log 2>&1

# ============================================================
# 每 5 分钟检查一次（开发/调试用）
# ============================================================
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/.tds/workspaces/019f9d39-efc0-7192-bc45-69a384871183/services && /usr/bin/node scripts/check-status.js --save-state

# ============================================================
# 每 15 分钟 + Webhook 通知（POST 到外部通知服务）
# ============================================================
# 将 --webhook 替换为实际的通知 URL（如 Slack Webhook、Telegram Bot 等）
  */15 *   *   *   *    cd /root/.tds/workspaces/019f9d39-efc0-7192-bc45-69a384871183/services && /usr/bin/node scripts/check-status.js --save-state --webhook=https://hooks.example.com/notify >> /var/log/bot-check.log 2>&1
```

## npm 脚本（推荐）

在 `services/` 目录下使用 npm 脚本执行：

```bash
# 标准检查
npm run check-status

# 保存状态并增量对比
npm run check-status:save

# JSON 格式输出
npm run check-status:json

# 通知模式（简洁输出）
npm run check-status:notify
```

## 查看检查日志

```bash
# 查看最近检查结果
tail -f /var/log/bot-check.log

# 查看通知日志（--notify 模式）
tail -f /var/log/bot-check-notify.log

# 查看上次记录的状态
cat /root/.tds/workspaces/019f9d39-efc0-7192-bc45-69a384871183/services/.check-state.json
```

## 手动运行

```bash
cd /root/.tds/workspaces/019f9d39-efc0-7192-bc45-69a384871183/services

# 标准输出（带颜色）
node scripts/check-status.js

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 保存状态并增量对比
node scripts/check-status.js --save-state

# 通知模式（简洁输出单行，适合通知系统）
node scripts/check-status.js --notify

# 保存状态 + 通知模式
node scripts/check-status.js --save-state --notify

# 通知模式 + 完整报告
node scripts/check-status.js --save-state --notify --verbose

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000

# POST 通知到 Webhook
node scripts/check-status.js --save-state --webhook=https://hooks.example.com/notify
```

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。  
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。

状态文件内容示例：

```json
{
  "lastCheck": "2026-07-26T12:00:00.000Z",
  "totalSignals": 42,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true
}
```

## 通知模式输出格式

使用 `--notify` 标志时，输出为单行格式，便于日志解析或管道传输：

### 有新信号时
```
[NOTIFY] 🔔 新信号 +3 条 | 总计 42 条 | 最近: longE/BTC-USDT shortE/ETH-USDT longX/SOL-USDT | 进程 ✅ 交易所 ✅ 系统 ✅
```

### 无新信号时
```
[NOTIFY] 📭 无新信号 | 总计 42 条 | 进程 ✅ 交易所 ✅ 系统 ✅
```

### 进程异常时
```
[NOTIFY] 📭 无新信号 | 总计 42 条 | 进程 ❌ 交易所 ✅ 系统 ✅
```

## Webhook 通知格式

使用 `--webhook=URL` 时，POST 请求体为 JSON：

```json
{
  "timestamp": "2026-07-26T12:00:00.000Z",
  "server": "http://43.133.210.83:3000",
  "newSignals": 3,
  "totalSignals": 42,
  "processesOnline": true,
  "exchangeOnline": true,
  "systemHealthy": true,
  "recentSignals": [
    { "time": "...", "type": "longE", "symbol": "BTC-USDT", "price": "65420.5" }
  ]
}
```

## 故障排查

### 脚本执行超时
```bash
# 检查服务器是否可达
curl -s -o /dev/null -w "%{http_code}" http://43.133.210.83:3000/health

# 检查 PM2 进程
pm2 status
```

### 状态文件损坏
```bash
# 删除状态文件以重置
rm -f /root/.tds/workspaces/019f9d39-efc0-7192-bc45-69a384871183/services/.check-state.json
```
