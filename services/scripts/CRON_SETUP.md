# 定时检查 & 报告 — Cron 配置

## 1. OKX 交易机器人状态检查（每 15 分钟）

```bash
# 编辑 crontab
crontab -e

# 每 15 分钟检查一次（东京服务器），结果追加到日志文件
# Min Hour Day Mon Week  command
  */15 *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/check-status.js --save-state >> /var/log/bot-check.log 2>&1
```

### 查看检查日志

```bash
# 查看最近检查结果
tail -f /var/log/bot-check.log

# 查看上次记录的状态
cat /root/levan-do-strategies/services/.check-state.json
```

### 手动运行

```bash
cd /root/levan-do-strategies/services

# 标准输出（带颜色）
node scripts/check-status.js

# JSON 格式（程序化解析）
node scripts/check-status.js --json

# 保存状态并增量对比
node scripts/check-status.js --save-state

# 指定自定义 URL
node scripts/check-status.js --url=http://localhost:3000
```

### 状态文件

脚本会在 `services/.check-state.json` 自动保存最近一次检查的快照。  
下次使用 `--save-state` 时，脚本会自动对比信号增量并在报告中显示。

---

## 2. Polymarket 模拟交易 Bot 状态报告（每 30 分钟）

```bash
# 编辑 crontab
crontab -e

# 每 30 分钟报告一次（东京服务器），结果追加到日志文件
# Min Hour Day Mon Week  command
  */30 *   *   *   *    cd /root/levan-do-strategies/services && /usr/bin/node scripts/polymarket-report.js --save-state >> /var/log/polymarket-report.log 2>&1
```

### 报告内容

报告包含 7 个模块：

1. **运行状态** — PM2 进程状态、运行模式（DRY_RUN）、运行时长
2. **钱包权益** — 初始资金、当前权益、可用余额、冻结资金、峰值、回撤、手续费、资本周转
3. **持仓概况** — 活跃持仓数量、已结算笔数、活跃持仓明细（市场、方向、价格、投入）
4. **交易统计** — 总交易笔数、胜/负、胜率、总盈亏、今日盈亏、策略扫描统计
5. **最近结算盈亏** — 最近已结算市场的盈亏明细
6. **异常与警告** — 进程异常、回撤过深、巨额亏损、扫描错误率、持仓风险提示
7. **策略配置** — 初始资金、交易比例、最大持仓、扫描间隔、合併回收等

### 查看报告日志

```bash
# 查看最近报告
tail -f /var/log/polymarket-report.log

# 查看上次保存的状态快照
cat /root/levan-do-strategies/services/.polymarket-state.json
```

### 手动运行

```bash
cd /root/levan-do-strategies/services

# 标准输出（带颜色）
node scripts/polymarket-report.js

# JSON 格式（程序化解析）
node scripts/polymarket-report.js --json

# 保存状态并增量对比
node scripts/polymarket-report.js --save-state

# 纯文本输出（无 ANSI 颜色，适合日志文件）
node scripts/polymarket-report.js --no-color
```

### 状态文件

脚本会在 `services/.polymarket-state.json` 自动保存最近一次报告的快照，  
包含运行状态、权益、持仓数、交易数、胜率等摘要数据，供外部程序读取。
