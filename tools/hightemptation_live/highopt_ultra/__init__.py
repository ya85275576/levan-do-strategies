"""
HighTempTation 终极优化包（第四波）

包含 7 个模块:
  1. onchain        — 链上执行层（Gas 动态竞价/Nonce 分布式锁/MEV 保护/Multicall/合约升级监听/USDC.e 校验）
  2. keysafe        — 密钥安全（冷热钱包分级/KMS-HSM 集成/不可篡改审计日志/双人控制）
  3. oracle_risk    — 预言机风险（UMA 结算延迟与争议期/合约措辞解析/Oracle Risk 矩阵/同源数据校准）
  4. latency        — 延迟优化（Polygon 节点地理部署/WebSocket 订单簿/CPU 绑核/TCP BBR）
  5. antigame       — 反博弈（地址轮换/冰山订单/噪声交易/Dashboard 信号延迟）
  6. metacontroller — 元控制器（Contextual Bandit/PPO 调度/Particle Filter 在线 BMA）
  7. human_loop     — 人机协同（分级自动化: 全自动/通知/人工确认/暂停）

用法:
  from highopt_ultra.onchain import GasAuctioneer, NonceManager, MEVGuard, MulticallBatcher, \
      ContractUpgradeWatcher, TokenValidator
  from highopt_ultra.keysafe import WalletManager, SimKMSBackend, EncryptedKeystore, \
      AuditLogger, DualControl
  from highopt_ultra.oracle_risk import UMASettlementModel, ContractPhraseParser, \
      OracleRiskMatrix, SameSourceCalibration
  from highopt_ultra.latency import RPCNodeSelector, WSOrderBookFeed, KernelTuner
  from highopt_ultra.antigame import AddressRotation, IcebergOrder, NoiseTrader, DashboardDelay
  from highopt_ultra.metacontroller import ContextualBandit, PPOScheduler, ParticleFilterBMA, \
      MetaController
  from highopt_ultra.human_loop import HumanInTheLoop, HumanLoopController, AutomationLevel

一键自检:
  python -m highopt_ultra.runner
"""
from .onchain import (GasAuctioneer, NonceManager, MEVGuard, MulticallBatcher,
                      ContractUpgradeWatcher, TokenValidator,
                      UpgradeEvent, EVENT_UPGRADED, EVENT_ADMIN_CHANGED)
from .keysafe import (WalletTier, TierPolicy, WalletManager, KMSBackend, SimKMSBackend,
                      EncryptedKeystore, AuditLogger, DualControl,
                      ApprovalRequest, ApprovalStatus)
from .oracle_risk import (UMASettlementModel, ContractPhraseParser, ConditionSpec,
                          OracleRiskMatrix, OracleRiskVerdict, SameSourceCalibration,
                          CalibrationReport)
from .latency import (RPCNodeSelector, NodeStats, WSOrderBookFeed, OrderBookState,
                      KernelTuner, DEFAULT_NODES)
from .antigame import (AddressRotation, IcebergOrder, NoiseOrder, NoiseTrader,
                       DashboardDelay)
from .metacontroller import (ContextualBandit, PPOScheduler, ParticleFilterBMA,
                             MetaController)
from .human_loop import (AutomationLevel, LEVEL_DESC, ApprovalRequest as HtlApprovalRequest,
                         HumanInTheLoop, HumanLoopController)

__version__ = "1.0.0"
__all__ = [
    "onchain", "keysafe", "oracle_risk", "latency", "antigame", "metacontroller", "human_loop",
    "GasAuctioneer", "NonceManager", "MEVGuard", "MulticallBatcher",
    "ContractUpgradeWatcher", "TokenValidator", "UpgradeEvent", "EVENT_UPGRADED", "EVENT_ADMIN_CHANGED",
    "WalletTier", "TierPolicy", "WalletManager", "KMSBackend", "SimKMSBackend",
    "EncryptedKeystore", "AuditLogger", "DualControl", "ApprovalRequest", "ApprovalStatus",
    "UMASettlementModel", "ContractPhraseParser", "ConditionSpec",
    "OracleRiskMatrix", "OracleRiskVerdict", "SameSourceCalibration", "CalibrationReport",
    "RPCNodeSelector", "NodeStats", "WSOrderBookFeed", "OrderBookState", "KernelTuner", "DEFAULT_NODES",
    "AddressRotation", "IcebergOrder", "NoiseOrder", "NoiseTrader", "DashboardDelay",
    "ContextualBandit", "PPOScheduler", "ParticleFilterBMA", "MetaController",
    "AutomationLevel", "LEVEL_DESC", "HtlApprovalRequest", "HumanInTheLoop", "HumanLoopController",
]
