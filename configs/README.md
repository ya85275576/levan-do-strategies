# 多策略参数配置文件

此目录管理所有交易策略的独立 JSON 配置文件，每个子目录对应一个策略版本。

## 目录结构

```
configs/
├── README.md                          # 本文档
├── CONFIG_SCHEMA.md                   # JSON 配置格式规范
├── LE_VAN_DO_Swing_Signals_7.9-X/     # 策略：LE VAN DO® Swing Signals 7.9-X
│   └── config.json                    # 参数配置文件
└── ...
```

## 配置管理规范

### 文件格式

每个策略的配置文件为标准的 JSON 格式，建议遵循以下顶层结构：

| 字段 | 类型 | 说明 |
|------|------|------|
| `meta` | object | 策略元信息（名称、版本、作者、交易所、标签等） |
| `tradingMode` | object | 交易模式配置 |
| `filterSettings` | object | 信号过滤设置 |
| `renkoSettings` | object | Renko 砖形图设置 |
| `riskManagement` | object | 风险参数 |
| `webhookMessages` | object | Webhook 消息 |
| `displayOptions` | object | 图表显示选项 |
| `reservedParams` | object | 预留扩展参数 |

### 参数字段规范

每个参数对象应包含以下字段：

| 字段 | 必填 | 说明 |
|------|------|------|
| `value` | ✓ | 当前设定值 |
| `type` | ✓ | 数据类型：boolean / integer / float / string / enum / timestamp |
| `options` | 仅 enum | 可选值列表 |
| `min` | 数值 | 最小值约束 |
| `max` | 数值 | 最大值约束 |
| `step` | 浮点 | 步进值 |
| `group` | 推荐 | Pine Script 中的分组名称 |
| `pineInput` | ✓ | 在 Pine Script 中的定义方式：input.string / input.int / input.float / input.bool / input.time / hardcoded / strategy setting / not_implemented |
| `description` | 推荐 | 参数含义说明 |

### 多版本管理

- 每次策略升级时，将旧版本配置目录完整保留（可加 `.bak` 后缀或存档）
- 新版本创建独立目录，如 `LE_VAN_DO_Swing_Signals_8.0-X/`
- 同一策略的不同变体（如同一策略的不同参数模板）可额外使用 `config_<variant>.json` 命名

### 切换策略配置

1. 在 TradingView Pine Editor 中打开对应的 `.pine` 文件
2. 根据 `config.json` 中每个参数的 `value` 手动设置输入框参数
3. 对于标记为 `hardcoded` 的参数，需在 Pine Script 源码中直接修改
4. 对于标记为 `not_implemented` 的预留参数，当前版本尚不支持

## 交易所连接配置

策略的交易所连接、API Key 管理、测试网/实盘切换等配置由 [`services/`](../services/) 目录管理。

- 当前预设交易所：**OKX**（见 `config.json` → `meta.exchange`）
- 交易所连接配置文档：[`services/README.md`](../services/README.md)
- API 凭据通过团队 Secrets 页面配置（`OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE`）
- 支持市价单/限价单、模拟盘/实盘切换
