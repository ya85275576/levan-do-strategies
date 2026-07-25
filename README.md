# LE VAN DO® — Swing Signals & Overlays Private™

此仓库托管 LE VAN DO® 交易策略的 Pine Script 源代码，用于 TradingView 平台的回测与实盘交易。

## 策略清單總表

詳細清單請參見 [`strategies/README.md`](strategies/README.md)。

| # | 策略名稱 | 版本 | 檔案 | 結構化參數 | 狀態 |
|---|----------|------|------|-----------|------|
| 1 | LE VAN DO® - Swing Signals & Overlays Private™ | 7.9-X (2024.3.20) | `strategies/LE_VAN_DO_Swing_Signals_7.9-X.pine` | [`params.json`](strategies/LE_VAN_DO_Swing_Signals_7.9-X/params.json) / [`version.json`](strategies/LE_VAN_DO_Swing_Signals_7.9-X/version.json) | 回測中 |

### 目錄結構

```
.
├── README.md                                ← 本文档
├── .gitignore
├── configs/                                 ← 策略参数配置文件
│   ├── README.md                            ← 配置管理规范
│   ├── CONFIG_SCHEMA.md                     ← JSON 配置格式规范
│   └── LE_VAN_DO_Swing_Signals_7.9-X/
│       └── config.json                      ← 策略参数配置
├── strategies/                              ← Pine Script 策略源码
│   ├── README.md                            ← 策略清單總表
│   ├── LE_VAN_DO_Swing_Signals_7.9-X.pine   ← 原始碼（Pine Script v5）
│   └── LE_VAN_DO_Swing_Signals_7.9-X/
│       ├── params.json                      ← 結構化參數定義
│       └── version.json                     ← 版本變更記錄
└── services/                                ← 交易所连接与信号执行服务
    ├── README.md                            ← 交易所连接配置文档
    ├── package.json                         ← Node.js 依赖
    ├── .env.example                         ← 环境变量模板
    ├── config/                              ← 交易所配置
    ├── exchange/                            ← 交易所 API 封装（OKX）
    ├── signals/                             ← TradingView 信号解析
    └── webhook/                             ← Webhook 接收端点
```

## 策略概述

LE VAN DO® - Swing Signals & Overlays Private™ 是一个基于 Pine Script v5 的多时间框架交易策略，支持以下功能：

- **交易方式**：Open/Close 蜡烛图信号 或 Renko 砖形图信号
- **止盈止损模式**：ATR 追踪、Trailing 追踪、Options 模式
- **震荡过滤**：支持 ATR / RSI 多种过滤组合
- **风险控制**：三级止盈（TP1/TP2/TP3）+ 止损
- **仪表盘**：策略表现、周度表现、月度表现可视化
- **Webhook 警报**：支持自动化交易通知

## 自动化交易链路

```
TradingView 策略警报
        │
        ▼  HTTP POST (alert_message)
Webhook 接收端点 (POST /webhook)
        │
        ▼  信号解析
信号解析器 (longE/shortE/longX/shortX)
        │
        ▼  OKX V5 REST API
交易所执行 (测试网/实盘)
        │
        ▼  订单结果
开仓 / 平仓 / TP / SL
```

详细配置说明请参阅 [`services/README.md`](services/README.md)。

## 使用方式

1. 在 TradingView Pine Editor 中打开 `.pine` 文件
2. 将代码粘贴到新的 Pine Script 指标/策略中
3. 调整参数后添加到图表运行
4. 配置 Webhook 警报，指向 [`services/`](services/) 提供的接收端点
5. 启动信号执行服务，开始自动化交易

## 许可证

此源代码基于 Mozilla Public License 2.0 发布。  
© TraderHalai
