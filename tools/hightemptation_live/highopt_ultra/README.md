# highopt_ultra — 第四波终极优化包

HighTempTation 天气预测市场的第四波生产级优化（链上安全 + 预言机 + 反博弈 + RL 元控制 + 人机协同）。

> 前置: [第二波 `highopt` 包](../highopt/__init__.py)（微观结构/套利/ML残差/订单FSM/回测/混沌）

## 一键自检

```bash
cd tools/hightemptation_live
python3 -m highopt_ultra.runner              # 全量自检（合成数据, 退出码 0/1）
python3 -m highopt_ultra.runner --db h.db    # 同时把结果写入 SQLite
```

## 模块总览

| # | 模块 | 文件 | 功能 |
|---|------|------|------|
| 1 | 链上执行层 | `onchain.py` | Gas 动态竞价(EIP-1559)、Nonce 分布式锁(SQLite 租约)、MEV 保护(sandwich 评分/私有 mempool 路由)、Multicall 批量、合约升级监听(Upgraded/AdminChanged)、USDC.e vs Native 校验 |
| 2 | 密钥安全 | `keysafe.py` | 冷热钱包分级(HOT/WARM/COLD 限额)、KMS/HSM 接口(私钥不落盘)、scrypt 加密密钥库、SHA-256 哈希链审计日志(可锚定/可校验)、双人控制(M-of-N 多签) |
| 3 | 预言机风险 | `oracle_risk.py` | UMA 结算延迟与争议期建模、合约措辞解析(阈值/单位/城市/日期)、6 维 Oracle Risk 矩阵(LOW→CRITICAL + 仓位系数)、同源数据校准(多模型一致性/偏差/漂移) |
| 4 | 延迟优化 | `latency.py` | Polygon 节点地理部署(延迟探测+就近排序+故障转移)、WebSocket 订单簿(快照+增量)、CPU 绑核/TCP BBR 调优 |
| 5 | 反博弈 | `antigame.py` | 地址轮换(时间/笔数/事件)、冰山订单(大单拆片)、噪声交易(随机小额混淆)、Dashboard 信号延迟(防抄作业) |
| 6 | 元控制器 | `metacontroller.py` | LinUCB Contextual Bandit、简化 PPO(策略梯度+clip)、Particle Filter 在线 BMA、三路融合投票调度子策略 |
| 7 | 人机协同 | `human_loop.py` | 分级自动化 AUTO/NOTIFY/CONFIRM/PAUSE、人工审批+超时策略、人工失联自动降级 PAUSE |

## 典型用法

```python
import sys; sys.path.insert(0, "tools/hightemptation_live")
from highopt_ultra.onchain import GasAuctioneer, NonceManager, MEVGuard, TokenValidator
from highopt_ultra.keysafe import WalletManager, SimKMSBackend, AuditLogger, DualControl
from highopt_ultra.oracle_risk import OracleRiskMatrix, ContractPhraseParser, UMASettlementModel
from highopt_ultra.antigame import IcebergOrder, NoiseTrader, DashboardDelay
from highopt_ultra.metacontroller import MetaController
from highopt_ultra.human_loop import HumanInTheLoop, AutomationLevel
```

### 链上执行链路（实盘接线示例）

```python
auction = GasAuctioneer()                    # 每新区块喂入
auction.observe(base_fee=45.2, gas_used_ratio=0.8)
fee = auction.suggest()                      # gas 参数

nonces = NonceManager("nonce_lock.db")       # 多进程共享
nonce = nonces.acquire("0xHOT", lease=30)    # 分布式锁分配
try:
    ok, _, mev = MEVGuard().check(qty=500, obi=0.2, depth=3000,
                                  spread=0.01, mid=0.40)
    route = "protected" if mev["route"] == "protected" else "public"
    tx = build_tx(nonce=nonce, max_fee=fee["max_fee"], ...)
    nonces.commit("0xHOT", nonce)            # 成功提交
except Exception:
    nonces.release("0xHOT", nonce)           # 失败归还
```

### 双人控制 + 审计（大额出金）

```python
audit = AuditLogger()
dc = DualControl(threshold=2, approvers=["alice", "bob"], audit=audit)
req = dc.request("WITHDRAW", {"amount": 50000, "to": "0xCOLD"})
dc.approve(req.id, "alice")                  # 第 1 签
dc.approve(req.id, "bob")                    # 第 2 签 → APPROVED → 放行
assert audit.verify_chain()                  # 全程可审计
```

### 元控制器调度子策略

```python
meta = MetaController(strategies=["edge_0.15", "edge_0.20", "depth_strict"])
ctx = [edge, 1-depth_norm, 时段, 波动率, 城市数, 近期收益]
decision = meta.decide(ctx, recent_rewards=[0.01, 0.05, -0.01])
# → {"strategy": "edge_0.20", "confidence": 0.38, "weights": {...}}
```

### 分级自动化

```python
htl = HumanInTheLoop(level=AutomationLevel.CONFIRM,   # 人工确认
                     notifier=alert.send,              # 接 AlertManager
                     timeout_policy="reject")          # 超时默认拒绝
r = await htl.execute("OPEN_POSITION", {"token": "Tokyo-NO"})
if r["reason"] == "AWAITING_APPROVAL":
    htl.approve(r["request_id"], "trader")             # 看板/Telegram 批准
```

## 与主程序集成

1. **持久化**: `db_manager.TradeDB` 已新增 7 张第四波表（`onchain_txs` / `gas_history` / `audit_logs` / `oracle_risk` / `antigame_actions` / `meta_decisions` / `human_approvals`），建库时自动创建。
2. **执行层**: 在 `order_manager` 或 `limit_order_executor` 外层包 `GasAuctioneer` + `NonceManager` + `MEVGuard`；大额/低频动作走 `MulticallBatcher` 合并。
3. **风控前置**: 开仓决策前调 `OracleRiskMatrix.assess` 与 `SameSourceCalibration.calibrate`, 风险等级 ≥ HIGH 时按 `position_factor` 缩减仓位。
4. **人机协同**: 把 `HumanInTheLoop` 挂在信号执行入口, 级别由环境变量 `AUTOMATION_LEVEL`(auto/notify/confirm/pause) 控制; 失联自动降级 PAUSE。
5. **元控制**: 每轮扫描后把各子策略的收益喂给 `MetaController.decide`, 决策写入 `meta_decisions` 表供回看。

## 生产注意

- `SimKMSBackend` / `EncryptedKeystore` 仅用于开发与自检; 生产密钥一律放云 KMS/HSM, 私钥不落盘。
- `NonceManager` 的 SQLite 实现适用于单机多进程; 跨机房部署请换 Redis/DB 后端（接口不变）。
- `ContractUpgradeWatcher` 的 topic0 为演示值, 接入真实合约前用 `cast keccak` 校准事件签名。
- PPO 为简化实现（数值梯度 + 均值基线）, 大状态空间请接 PyTorch 版并调参。
- 反博弈模块的噪声/延迟仅影响对外表现, 内部账目以 `is_noise` / `delayed` 标记隔离。
