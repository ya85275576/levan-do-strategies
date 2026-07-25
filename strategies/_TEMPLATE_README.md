# Pine Script 策略模板使用指南

本模板定义了一套标准化的 Pine Script v5 策略开发规范，涵盖代码结构、参数命名、
信号输出格式和注释风格。所有新策略应基于此模板开发。

---

## 快速开始

```bash
cp strategies/_TEMPLATE.pine strategies/我的策略名称_1.0.0.pine
```

然后编辑新文件：

1. 修改 `strategy()` 声明中的策略名称
2. 更新 `VERSION` 常量
3. 替换「信号计算区」的示例逻辑（MA 交叉）为实际策略逻辑
4. 按需调整风险管理参数
5. 运行回测验证

---

## 文件结构

```
strategies/
├── README.md                        ← 策略清单总表
├── _TEMPLATE.pine                   ← 策略模板（本仓库核心）
├── _TEMPLATE_README.md              ← 本文档
├── LE_VAN_DO_Swing_Signals_7.9-X.pine
├── LE_VAN_DO_Swing_Signals_7.9-X/
│   ├── params.json
│   └── version.json
└── ...
```

---

## 模板代码结构

每段代码按以下顺序组织，用 `=== 标题 ===` 注释块分隔：

| 分区 | 说明 | 开发者必改？ |
|------|------|--------------|
| 版本号 | `VERSION` 常量 | ✓ 改版本号 |
| 策略声明 | `strategy()` 参数 | ✓ 改名称 |
| 工具函数 | 通用函数（`truncate`, `rp_security`, `f_cross` 等） | 可选添加 |
| 颜色常量 | 标准颜色定义 `c_xxx` | 可选添加 |
| 输入参数区 | 所有 `input.*()` 集中在此 | ✓ 按需添加 |
| 信号计算区 | **核心** — 定义 `leTrigger` / `seTrigger` / `lxTrigger` / `sxTrigger` | ✓ **必须实现** |
| 仓位管理区 | 多级 TP/SL 状态机 `condition` | 可选调整参数 |
| 信号输出区 | 统一信号变量 `longE` / `shortE` / `longSL` / `shortTP1` 等 | 通常不改 |
| 订单执行区 | `strategy.entry()` / `strategy.exit()` / `strategy.close()` | 可选调整 |
| 可视化区 | `plotshape` 信号标签、K线着色 | 可选调整 |
| 调试数据区 | `plot()` 到数据窗口 | 可选 |
| 警报区 | `alert()` Webhook 通知 | 可选调整消息内容 |
| 绩效仪表盘 | 回测指标表格 | 可选启用 |
| 扩展线绘制 | ATR 模式的延长线 | 可选 |

---

## 命名规范

### 前缀约定

| 前缀 | 用途 | 示例 |
|------|------|------|
| `i_` | input 参数（用户可调） | `i_atrLength`, `i_profitFactor` |
| `G_` | input 分组标签 | `G_RISK`, `G_DISPLAY` |
| `c_` | 颜色常量 | `c_GREEN`, `c_RED`, `c_SL` |
| `f_` | 自定义函数 | `f_cross`, `f_tfInMinutes` |
| `p_` | plot 对象 | `p_entry`, `p_sl`, `p_tp1` |

### 信号变量规范

| 变量名 | 类型 | 含义 |
|--------|------|------|
| `leTrigger` | bool | 多头触发条件（自定义逻辑的入口） |
| `seTrigger` | bool | 空头触发条件 |
| `lxTrigger` | bool | 多头手动离场条件 |
| `sxTrigger` | bool | 空头手动离场条件 |
| `longE` | bool | 多头入场执行 |
| `shortE` | bool | 空头入场执行 |
| `longX` | bool | 多头离场执行 |
| `shortX` | bool | 空头离场执行 |
| `longSL` / `shortSL` | bool | 止损触发 |
| `longTP1` / `shortTP1` | bool | 第一层止盈 |
| `longTP2` / `shortTP2` | bool | 第二层止盈 |
| `longTP3` / `shortTP3` | bool | 第三层止盈 |

> **规则**：信号变量统一为 `long` / `short` 前缀 + `E`(Entry)、`X`(Exit)、`SL`(Stop Loss)、`TPn`(Take Profit n) 后缀。

---

## 信号输出格式

所有策略必须输出以下 **9 类标准信号**，供外部系统（Webhook、订单执行、图表显示）统一消费：

```
信号矩阵：
                   Entry    TP1     TP2     TP3     StopLoss    Exit
   多头（Long）    longE    longTP1 longTP2 longTP3 longSL      longX
   空头（Short）   shortE   shortTP1 shortTP2 shortTP3 shortSL   shortX
```

### 外部系统集成

信号通过以下方式传递给外部系统：

1. **`strategy.entry/exit/close` 的 `alert_message` 参数** — 发送到 TradingView 警报的 payload
2. **`alert()` 函数** — 在警报面板中配置触发
3. **`plotshape` 标签** — 在图表中可视化展示

### Webhook JSON 格式建议

外部服务解析的推荐 JSON 格式（由 `alert_message` 送出）：

```json
{
  "action": "longE",
  "symbol": "BTCUSDT",
  "price": 45000.0,
  "tp1": 45600.0,
  "tp2": 46200.0,
  "tp3": 46800.0,
  "sl": 44400.0,
  "timestamp": "2024-01-01T00:00:00Z"
}
```

---

## 注释风格

### 区块注释

```
// —————————————————————————————————————————————————————————————————————————————
// [区块标题] — 区块说明
// —————————————————————————————————————————————————————————————————————————————
```

### 函数注释

```
// 函数功能说明
// @param  _paramName  参数说明
// @returns           返回值说明
```

### 代码内注释

```
变量赋值  // 简短说明
```

### 占位注释

```
// ★ 开发者在此区域实现自定义入场/离场条件
```

---

## 策略开发步骤

1. **复制模板**
   ```bash
   cp strategies/_TEMPLATE.pine strategies/MyStrategy_1.0.0.pine
   ```

2. **填写元信息**
   - 修改 `strategy()` 中的名称和 `shorttitle`
   - 设置 `VERSION`
   - 调整 `strategy()` 参数（初始资金、手续费等）

3. **实现信号逻辑**（核心工作）
   - 在「信号计算区」删除示例的 MA 交叉代码
   - 实现你的策略逻辑，输出 `leTrigger` 和 `seTrigger`

4. **调整风险参数**
   - 修改 `i_atrLength`、`i_profitFactor`、`i_stopFactor` 等
   - 或改为你自定义的风险模型

5. **调整可视化**
   - 修改颜色、标签文字等以匹配策略风格

6. **验证并回测**
   - 在 TradingView 上加载运行
   - 检查信号是否正确触发

7. **注册参数文档**
   - 在策略同名目录下创建 `params.json` 和 `version.json`
   - 更新 `strategies/README.md` 清单

---

## 常见问题

### Q: 我的策略不需要多级 TP，只需要单层 TP + SL，如何修改？

将仓位管理区的 `condition` 状态机简化为二级即可：去掉 `1.2`、`1.3` 分支，只保留 `1.0`（已入场）和 `0.0`（无持仓）。订单执行区也只保留一个 `strategy.exit()`。

### Q: 我的策略使用不同的风险管理方式（如固定点数、百分比），如何适配？

修改「仓位管理区」的 `tp1Distance`、`slDistance` 等计算公式，替换 ATR 计算为你自己的方式。信号输出区的变量名保持不变。

### Q: 如何添加新的输入参数？

在「输入参数区」添加 `i_xxx = input.type(...)`，并在对应的计算逻辑中使用。命名必须以 `i_` 开头。

### Q: 如何集成外部交易系统？

通过 `alert_message` 参数发送 JSON 字符串。外部 Webhook 接收后解析信号变量名（`longE`、`shortE` 等）执行对应操作。参见 `services/` 目录下的信号解析器。
