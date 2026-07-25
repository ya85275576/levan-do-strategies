# 策略参数配置文件规范 (Schema)

本文档定义多策略参数配置 JSON 文件的规范格式，方便配置管理员维护和创建新策略的配置文件。

## 顶层结构

```json
{
  "meta":            { /* 策略元数据 */ },
  "tradingMode":     { /* 交易模式 */ },
  "filterSettings":  { /* 过滤设置 */ },
  "renkoSettings":   { /* Renko 设置 */ },
  "riskManagement":  { /* 风险管理 */ },
  "webhookMessages": { /* Webhook 消息 */ },
  "displayOptions":  { /* 显示选项 */ },
  "reservedParams":  { /* 预留参数 */ }
}
```

每个策略的配置必须包含 `meta` 字段，其余分类按策略实际参数情况增减。

## meta 字段规范

```json
{
  "strategyId":   "唯一标识符，建议大写+短横线",
  "strategyName": "策略全名",
  "version":      "版本号",
  "date":         "发布日期",
  "platform":     "运行平台（如 TradingView）",
  "language":     "编程语言",
  "sourceFile":   "源码文件路径",
  "description":  "策略概述（200字以内）",
  "author":       "作者",
  "license":      "许可证",
  "tags":         ["标签1", "标签2"],
  "exchange":     "默认交易所",
  "symbols":      ["默认交易对"]
}
```

## 参数对象规范

每个参数必须是一个包含以下字段的对象：

### 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `value` | 任意 | 参数当前设定值 |
| `type` | string | 数据类型标识，可选值见下方 |

### 可选字段

| 字段 | 类型 | 适用 type | 说明 |
|------|------|-----------|------|
| `options` | array | enum | 可选枚举值列表 |
| `min` | number | integer, float | 最小值 |
| `max` | number | integer, float | 最大值 |
| `step` | number | float | 步进值（如 0.1, 0.5, 1） |
| `group` | string | 所有 | Pine Script 中的 UI 分组 |
| `pineInput` | string | 所有 | 参数定义方式（见下方） |
| `description` | string | 所有 | 参数说明 |

### type 允许值

- `boolean` — 布尔值（true/false）
- `integer` — 整数
- `float` — 浮点数
- `string` — 字符串
- `enum` — 枚举（需提供 options 数组）
- `timestamp` — 时间戳字符串

### pineInput 允许值

| 值 | 含义 |
|----|------|
| `input.string` | Pine Script `input.string()` 暴露的参数，可在 UI 中修改 |
| `input.int` | Pine Script `input.int()` 暴露的参数 |
| `input.float` | Pine Script `input.float()` 暴露的参数 |
| `input.bool` | Pine Script `input.bool()` 暴露的参数 |
| `input.time` | Pine Script `input.time()` 暴露的参数 |
| `strategy setting` | `strategy()` 函数参数（如 initial_capital） |
| `hardcoded` | 源码中硬编码的常量，修改需编辑 `.pine` 文件 |
| `not_implemented` | 当前版本尚未实现的预留参数 |

## 创建新策略配置的步骤

1. 新建目录 `configs/<策略标识>/`
2. 创建 `config.json`，填充 `meta` 部分
3. 逐一分析 Pine Script 源码中的所有 `input.*()` 和 `strategy()` 参数
4. 提取硬编码常量，记录到对应分类
5. 按参数分组整理到合适的顶层分类中
6. 为每个参数填写 value / type / pineInput / description
7. 如有需要，参考 `LE_VAN_DO_Swing_Signals_7.9-X/config.json` 的格式
