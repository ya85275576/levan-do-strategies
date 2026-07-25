# LE VAN DO® — Swing Signals & Overlays Private™

此仓库托管 LE VAN DO® 交易策略的 Pine Script 源代码，用于 TradingView 平台的回测与实盘交易。

## 策略列表

| 策略 | 版本 | 文件 |
|------|------|------|
| LE VAN DO® - Swing Signals & Overlays Private™ | 7.9-X | `strategies/LE_VAN_DO_Swing_Signals_7.9-X.pine` |

## 策略概述

LE VAN DO® - Swing Signals & Overlays Private™ 是一个基于 Pine Script v5 的多时间框架交易策略，支持以下功能：

- **交易方式**：Open/Close 蜡烛图信号 或 Renko 砖形图信号
- **止盈止损模式**：ATR 追踪、Trailing 追踪、Options 模式
- **震荡过滤**：支持 ATR / RSI 多种过滤组合
- **风险控制**：三级止盈（TP1/TP2/TP3）+ 止损
- **仪表盘**：策略表现、周度表现、月度表现可视化
- **Webhook 警报**：支持自动化交易通知

## 使用方式

1. 在 TradingView Pine Editor 中打开 `.pine` 文件
2. 将代码粘贴到新的 Pine Script 指标/策略中
3. 调整参数后添加到图表运行

## 许可证

此源代码基于 Mozilla Public License 2.0 发布。  
© TraderHalai
