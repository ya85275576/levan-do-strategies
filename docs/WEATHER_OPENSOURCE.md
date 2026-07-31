# 🌤️ HighTempTation 天气系统开源说明

> 本仓库（levan-do-strategies）仅保留天气系统开源交付的说明与索引。
> **实际开源代码存放于独立仓库**：https://github.com/ya85275576/HighTempTation

## 背景

用户要求：**只开源天气交易系统，不含任何加密策略**。因此从服务器提取纯天气相关文件，
创建独立开源仓库，与加密策略（polymarket-arb / polymarket-bot / webhook 信号网关等）完全隔离。

## 开源仓库信息

| 项目 | 值 |
|------|-----|
| GitHub 仓库 | https://github.com/ya85275576/HighTempTation |
| 可见性 | public（公开） |
| 默认分支 | main |
| 许可证 | MIT |

## 提取内容清单

| # | 来源（服务器） | 开源仓库内文件 | 说明 |
|---|----------------|----------------|------|
| 1 | `archived_old_bots/hightemptation-bot/bot.py`（v8，190KB） | `bot.py` | 天气 Bot 主程序：多源气象数据、概率模型、温度阶梯套利、Kelly 仓位、风控 |
| 2 | `archived_old_bots/okx-webhook/dashboard.html`（提取天气部分） | `dashboard_forecast.html` | 天气预报面板（城市预报网格 / DB 状态） |
| 3 | `tools/hightemptation_live/dashboard.html` | `dashboard.html` | 交易分析面板（持仓 / 平仓 / 资金曲线 / 策略归因） |
| 4 | `services/hightemptation/requirements.txt` | `requirements.txt` | Python 依赖 |
| 5 | 新建 | `README_WEATHER.md` | 中文说明文档（快速开始 / 配置 / 算法 / API） |
| 6 | — | GitHub 仓库 | 已创建并推送，仅含天气相关代码 |

附带文件：`dashboard/`（dashboard_server.py / dashboard.py / db_manager.py / calibrator.py / chart.umd.min.js）、
`ecosystem.config.cjs`（PM2 配置）、`.env.example`（配置模板）、`.gitignore`、`LICENSE`。

## 合规性验证（已通过）

- ✅ **无加密策略**：bot.py / dashboard.html / dashboard/ 中无 BTC/ETH/OKX/合约/加密信号等代码
- ✅ **无密钥泄露**：`.env.example` 仅含占位符与注释，无真实 API Key
- ✅ **无内部引用**：已清理 Caddy 域名（shtdjf.indevs.in）、内部路径（levan-do-strategies/.venv）、Cloudflare Tunnel 等
- ✅ **路径自包含**：`dashboard_server.py` 已改为自包含路径，克隆即可运行
- ✅ **源文件一致性**：`bot.py`、`dashboard.html` 与服务器源文件 md5 校验一致

## 开源仓库提交历史

```
2ac90db 📝 Use real repo URL in clone instructions
b85a3c1 ✨ Add weather forecast dashboard panel + update README
45b03fe 🧹 Clean internal domain references for open source
6a717aa 🎉 Initial commit: HighTempTation weather forecast arbitrage system
```

## 后续维护（如需同步更新）

- 本地克隆：`/root/levan-do-strategies/HighTempTation`（main 分支，远端已配置）
- 推送方式：`cd /root/levan-do-strategies/HighTempTation && git push origin main`
- **推送前必须复查**：无密钥 / 无内部域名（shtdjf.indevs.in）/ 无加密策略代码
