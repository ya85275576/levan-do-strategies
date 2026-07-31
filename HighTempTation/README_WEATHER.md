# 🌤️ HighTempTation — 天气预报套利系统

> 基于天气预报与预测市场的自动化概率套利系统。
> 实时采集多源气象数据，通过概率模型计算温度桶定价偏差，自动执行套利交易。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)](https://www.python.org/)

---

## 📋 项目简介

HighTempTation 是一套 **纯天气数据驱动的概率套利系统**，不依赖任何加密策略。核心思路：

```
天气预报 (Open-Meteo) ──→ 高斯 CDF 概率模型 ──→ 模型概率 p_model
                                                ↓
METAR 实时观测 ──────────→  偏差对比  ←───────── 市场价格 p_market
HKO 天文台数据 ─────────→    ↓
                           如果 |p_model - p_market| ≥ 阈值 → 触发信号
                            ↓
                      Fractional Kelly 仓位管理 → 执行套利
```

### 核心特性

| 特性 | 说明 |
|------|------|
| 🌡️ **多源气象数据** | Open-Meteo (ECMWF/GFS/ICON) + METAR 实时观测 + HKO 香港天文台 |
| 🧮 **概率模型** | 高斯 CDF、40-member 集合预报、贝叶斯实时更新、多模型加权集成 |
| 🌐 **40 成员 ICON-EPS** | 从 Open-Meteo Ensemble API 获取 39+1 个成员，非参数概率计算 |
| 🧠 **贝叶斯更新引擎** | 微观因子（辐射/云量/风速/湿度）→ 似然函数 → 后验概率 |
| 🪜 **温度阶梯套利** | 主信号触发时自动扫描相邻温度桶，批量挂单 |
| 📊 **多模型聚合** | ECMWF/GFS/ICON 加权平均 + 一致性评分 + 离散度惩罚 |
| 🌍 **38+ 城市覆盖** | Tokyo/New York/London/Hong Kong/Singapore/Dubai 等全球主要城市 |
| 🎯 **偏差套利** | 模型概率 vs 市场价格偏差 ≥ 阈值时触发信号 |
| 💰 **Fractional Kelly** | 标准 Kelly 公式 + 信号类型动态分数 (METAR 0.50/MODEL 0.25/LADDER 0.15) |
| 🔒 **多重风控** | 固定止盈(+9%) / 止损(-6.5%) / 移动止盈 / 时间止损(24h) / Theta 惩罚 |
| 📉 **订单簿过滤 (OBI)** | CLOB 订单簿不均衡过滤，避免不良挂单环境 |
| 📊 **实时看板** | HTML 看板 + Streamlit 高级看板，零配置开箱即用 |
| 📝 **信号历史** | 信号胜率追踪，<40% 自动拒绝新开仓 |
| 🧪 **模拟模式** | 不配置 API Key 即可运行，自动生成模拟交易数据 |

---

## 🏗️ 项目结构

```
HighTempTation/
├── README_WEATHER.md           ← 本文档
├── LICENSE                     ← MIT 许可证
├── .gitignore
├── .env.example                ← 配置模板（所有可调参数）
├── requirements.txt            ← Python 依赖
├── bot.py                      ← 🌤 主程序（含天气预报/市场扫描/交易引擎/看板服务器）
├── dashboard.html              ← 📊 HTML 看板前端（交易分析：持仓/平仓/资金曲线/策略归因）
├── dashboard_forecast.html     ← 🌡️ 天气预报面板（城市预报网格/DB 状态，需后端返回 weather 字段）
├── dashboard/
│   ├── dashboard_server.py     ← HTTP 服务器（端口 3002，提供 /api/status + 前端）
│   ├── dashboard.py            ← Streamlit 看板（端口 8501，高级可视化）
│   ├── db_manager.py           ← SQLite 数据库管理器（持久化交易/预报/信号记录）
│   ├── calibrator.py           ← 概率校准器
│   └── chart.umd.min.js        ← Chart.js 图表库（用于 HTML 看板）
```

---

## 🚀 快速开始

### 环境要求

- **Python 3.12+**
- pip 依赖（见 requirements.txt）

### 安装

```bash
# 1. 克隆仓库
git clone https://github.com/ya85275576/HighTempTation.git
cd HighTempTation

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate  # Linux/Mac
# 或 .venv\Scripts\activate  # Windows

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env 文件调整参数（默认即为模拟模式，无需 API Key）
```

### 运行看板（无需 API Key，即开即用）

```bash
# 启动 HTML 看板（端口 3002）
python3 dashboard/dashboard_server.py

# 浏览器打开 http://localhost:3002 即可查看
```

看板自动生成模拟交易数据，展示：
- 实时持仓（城市/温度桶/入场价/当前价/PnL）
- 平仓记录（盈亏/出场原因）
- 资金曲线
- 策略归因分析

> 💡 **第二面板**：`dashboard_forecast.html` 是天气预报专用面板（城市预报网格、DB 健康状态），
> 需要后端 `/api/status` 额外返回 `weather` 字段（含 `forecasts`/`summary`/`db_exists`），
> 适合部署在已有 Bot 数据服务（如 webhook 网关）旁边作为天气视图。

### 运行完整 Bot（需 Polymarket API）

```bash
# 编辑 .env 配置实际参数
# 设置 DRY_RUN=true 以模拟模式运行（不实际下单）
python3 bot.py

# DRY_RUN=false + 配置 Polymarket API Key → 实盘模式
```

---

## ⚙️ 配置详解

所有配置通过环境变量控制（.env 文件或 export），以下是核心配置项：

### 运行模式

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DRY_RUN` | `true` | `true`=模拟模式，`false`=实盘模式 |
| `LOG_LEVEL` | `INFO` | 日志级别 (DEBUG/INFO/WARNING/ERROR) |

### 天气预报

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FORECAST_DAYS` | `7` | 预报天数 |
| `WEATHER_MODELS` | `best_match,ecmwf_ifs,gfs_global,icon_global` | 使用的预报模型 |
| `DEFAULT_SIGMA` | `2.0` | 预报默认标准差 (°C) |

### 40 成员 ICON Ensemble

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ENSEMBLE_40_ENABLED` | `true` | 启用 40 成员 ICON-EPS |
| `ENSEMBLE_40_MODE` | `hybrid` | 模式: hybrid/ensemble_only/deterministic_only |
| `ENSEMBLE_40_EDGE` | `0.06` | 集合概率 vs 市场价格偏差阈值 |

### METAR 实时观测

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ENABLE_METAR` | `true` | 启用 METAR 实时温度采集 |
| `METAR_DEVIATION_THRESH` | `3.0` | METAR vs 预报偏差阈值 (°C) |
| `EXTREME_BUY_THRESH` | `0.05` | 极端低估合约阈值 (YES 价格 < 5¢) |

### HKO 香港天文台

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HKO_ENABLED` | `true` | 启用 HKO 香港天文台数据 |
| `HKO_STATION` | `Hong Kong Observatory` | 首选观测站 |

### 仓位与 Kelly

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KELLY_ENABLED` | `true` | 启用 Fractional Kelly |
| `KELLY_FRACTION_METAR` | `0.50` | METAR 信号的 Kelly 分数 |
| `KELLY_FRACTION_MODEL` | `0.25` | 模型信号的 Kelly 分数 |
| `KELLY_FRACTION_LADDER` | `0.15` | 阶梯信号的 Kelly 分数 |
| `KELLY_MIN_SIZE` | `1.0` | 最小仓位 ($) |
| `KELLY_MAX_SIZE` | `100.0` | 最大仓位 ($) |

### 风控

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FIXED_TAKE_PROFIT_PCT` | `0.09` | 固定止盈 (+9%) |
| `STOP_LOSS_PCT` | `0.065` | 止损 (-6.5%) |
| `TRAILING_ACTIVATE_PCT` | `0.05` | 移动止盈激活 (浮盈 5%) |
| `TRAILING_RETRACE_PCT` | `0.03` | 移动止盈回撤 (3% 平仓) |
| `TIME_STOP_HOURS` | `24` | 时间止损 (24h) |
| `MAX_CONCURRENT` | `10` | 最大并发持仓 |

### 温度阶梯

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LADDER_ENABLED` | `true` | 启用温度阶梯套利 |
| `LADDER_SPREAD` | `1` | 相邻桶扩散数量 |
| `LADDER_EDGE_BOOST` | `1.3` | 阶梯桶 edge 门槛乘数 |
| `LADDER_SIZE_PCT` | `0.5` | 阶梯桶仓位比例 |

### 贝叶斯引擎

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BAYESIAN_ENABLED` | `true` | 启用贝叶斯实时概率更新 |
| `BAYESIAN_SCAN_INTERVAL` | `600` | 扫描间隔 (秒) |
| `BAYESIAN_EDGE` | `0.08` | 后验概率偏差阈值 |
| `BAYESIAN_LIKELIHOOD_STRENGTH` | `0.40` | 似然修正强度 [0~1] |

---

## 📡 数据源说明

### Open-Meteo (免费，无需 API Key)

- **预报**: `api.open-meteo.com/v1/forecast` — 多模型日最高温预报
- **历史**: `archive-api.open-meteo.com/v1/archive` — 历史温度用于偏差校正
- **地理编码**: `geocoding-api.open-meteo.com/v1/search` — 城市名→经纬度
- **集合预报**: `ensemble-api.open-meteo.com/v1/ensemble` — 40 成员 ICON-EPS

### METAR (免费，无需 API Key)

- **来源**: `aviationweather.gov/metar` — 全球机场实时气象观测
- **频率**: 每 30-60 分钟更新，5 分钟本地缓存

### HKO 香港天文台 (免费，无需 API Key)

- **来源**: `data.weather.gov.hk/weatherAPI/opendata/` — 香港官方实时气象数据
- **站点**: 天文台总站 / 香港国际机场 / 京士柏

---

## 📊 API 端点

看板服务器提供以下 REST API：

| 路径 | 说明 |
|------|------|
| `GET /` | 天气交易面板 HTML |
| `GET /api/status` | 交易状态 JSON（持仓/盈亏/资金曲线） |
| `GET /api/strategies` | 各策略归因数据 |
| `GET /api/trades` | 历史交易记录 |
| `GET /chart.umd.min.js` | Chart.js 库 |

> 所有 API 在无 Bot 后端时自动返回模拟数据，每 30s 更新。

---

## 🧠 核心算法

### 高斯 CDF 概率模型

对每个温度桶 [lower, upper)，计算：

```
P(temp ∈ [lower, upper)) = Φ((upper - μ) / σ) - Φ((lower - μ) / σ)
```

其中 μ = 预报平均温度，σ = 模型间标准差（至少 2°C 兜底）。

### 多模型集成 (Ensemble)

对 ECMWF / GFS / ICON 三种模型独立计算桶概率，加权平均：

```
p_ensemble = Σ(w_i × p_i) / Σ(w_i)
```

默认权重: ECMWF=0.4, GFS=0.3, ICON=0.3

### 40 成员非参数概率

对 40 个 ICON-EPS 成员直接计数：

```
p_40 = (满足 lower ≤ t < upper 的成员数) / 40
```

完全不依赖高斯假设，适合极端温度分布。

### Fractional Kelly 仓位

```
f* = (bp - q) / b     ← 标准 Kelly
size = f* × kelly_fraction × capital     ← 按信号类型打折
size = min(size, capital × 2%)           ← 单笔最大 2% 风险
```

### 贝叶斯实时更新

```
posterior ∝ prior × L(radiation) × L(cloud) × L(wind) × L(humidity)
```

微观因子通过物理意义似然函数修正先验概率：
- 高辐射 → 升温 → 高温桶概率增加
- 多云 → 降温 → 低温桶概率增加
- 大风 → 收敛到均值 → 极端桶概率降低

---

## 🗺️ 路线图

- [x] Open-Meteo 多模型预报
- [x] METAR 实时观测
- [x] HKO 香港天文台数据
- [x] 40-member ICON Ensemble
- [x] 多模型加权集成
- [x] 温度阶梯套利
- [x] Fractional Kelly 仓位
- [x] 贝叶斯实时更新引擎
- [x] 信号历史验证闭环
- [x] 订单簿不均衡过滤 (OBI)
- [x] 移动止盈 / 止损 / 时间止损
- [x] HTML + Streamlit 双看板
- [ ] 更多数据源 (JMA, DWD, NOAA)
- [ ] 机器学习概率校准
- [ ] 策略可视化编辑器
- [ ] 压力测试框架
- [ ] Telegram/Webhook 通知

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

### 开发指南

```bash
# 安装开发依赖
pip install -r requirements.txt

# 运行测试
python3 -m pytest tests/

# 代码风格
pip install black ruff
black bot.py dashboard/*.py
ruff check bot.py
```

---

## 📄 许可证

MIT License — 详见 [LICENSE](LICENSE)

## 🙏 致谢

- [Open-Meteo](https://open-meteo.com/) — 免费开源天气 API
- [aviationweather.gov](https://www.aviationweather.gov/) — 全球 METAR 数据
- [香港天文台](https://www.hko.gov.hk/) — 香港官方气象数据
- [scipy](https://scipy.org/) — 科学计算基础库

---

*让天气预报的价值被市场发现 🌤️*
