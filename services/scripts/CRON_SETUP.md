# 定时检查 — Cron 配置

## 安装定时任务

```bash
# 编辑 crontab
crontab -e

# 每 15 分钟检查一次（东京服务器），简洁通知输出
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state --notify >> /var/log/bot-check.log 2>&1

# 每 5 分钟检查一次（调试用）
# Min Hour Day Mon Week  command
  */5  *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state

# 每 30 分钟检查一次并推送 Webhook（如 Discord/Telegram Bot）
# Min Hour Day Mon Week  command
  */30 *   *   *   *    cd /root/levan-do-strategies/services && node scripts/check-status.js --save-state --webhook --webhook-url=https://hooks.example.com/hook
```

## npm scripts（推荐使用）

```bash
cd /root/levan-do-strategies/services

# 标准检查 + 保存状态（增量对比）
npm run check

# 简洁通知行输出（适合日志/Telegram）
npm run check:notify

# 推送结果到 Webhook
npm run check:webhook

# 全量模式：保存状态 + 通知 + Webhook
npm run check:full
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

# 简洁通知行（一行概要）
node scripts/check-status.js --save-state --notify

# 推送结果到 Webhook（默认 POST 到服务器 /webhook）
node scripts/check-status.js --save-state --webhook

# 指定 Webhook URL
node scripts/check-status.js --save-state --webhook --webhook-url=https://hooks.example.com/hook

# 全量模式
node scripts/check-status.js --save-state --notify --webhook

# 指定自定义 API URL
node scripts/check-status.js --url=http://localhost:3000
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_URL` | `http://43.133.210.83:3000` | 机器人 API 地址 |
| `WEBHOOK_URL` | 空（使用 `API_URL` + `/webhook`） | Webhook 推送目标 URL |

## 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。  
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。

## 输出示例

### 简洁通知行（--notify）
```
[14:30] ✅ 🤖 Σ42 📨longE@BTCUSDT 💾45% 💿32% ⚙️2/2 💼1
[14:45] ✅ 🤖 📈+3 Σ45 📨shortE@ETHUSDT 💾47% 💿32% ⚙️2/2 💼2
```

字段含义：`[时间] 整体状态 机器人状态 信号增量 累计信号 最新信号 内存% 磁盘% 进程数 持仓数`

### Webhook 推送（--webhook）
POST 到指定 URL 的 JSON payload，包含完整的 Slack/Discord 兼容 attachments 格式：
- 进程状态（每个进程 CPU/内存）
- 交易所连接状态
- 信号统计（累计 + 增量）
- 信号分布（longE/shortE/longX/shortX）
- 当前持仓
- 系统资源（内存/磁盘/CPU）
- 最近 5 条新信号详情（如有增量）

### 标准报告（无参数）
完整彩色终端报告，包含 6 个部分：进程状态、交易所状态、信号统计、持仓信息、系统资源、交易对行情。

## 注意事项

1. 脚本默认连接 `http://43.133.210.83:3000`，确保服务器已启动且防火墙放行
2. `--webhook` 模式默认 POST 到 `API_URL + /webhook`，可通过 `--webhook-url=` 或 `WEBHOOK_URL` 环境变量覆盖
3. 建议配合 `crontab` 每 15-30 分钟执行一次 `npm run check:notify` 或 `npm run check:full`
4. `--save-state` 必须与状态文件配合使用；首次运行无状态文件时，将只显示当前快照
5. 简洁通知行（`--notify`）无颜色转义码，适合日志文件或消息推送平台
