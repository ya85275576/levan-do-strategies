"""
HighTempTation 高阶优化包（第二波）

包含 6 个模块:
  1. microstructure — 订单簿微观结构（LOB 形状/深度斜率/Square-Root Law/VPIN）
  2. arbitrage      — 跨市场/跨期套利（桶分割/相邻桶平价/期限结构/多平台价差）
  3. ml_residual    — ML 残差学习（LightGBM/XGBoost 修正物理模型残差 + 在线学习 + NLP）
  4. order_fsm      — 订单状态机（Client Order ID 幂等/幽灵订单防护/部分成交/仓位对账）
  5. walk_forward   — Walk-Forward 回测（Point-in-Time/成本敏感性/夏普-Calmar 稳定性）
  6. chaos          — 混沌工程（故障注入 + 熔断器验证）

用法:
  from highopt.microstructure import MicrostructureGate
  from highopt.arbitrage import ArbitrageScanner
  from highopt.ml_residual import MLResidualLearner
  from highopt.order_fsm import OrderFSM, SimExchangeAdapter
  from highopt.walk_forward import WalkForwardBacktester
  from highopt.chaos import CircuitBreaker, ChaosVerifier

一键自检:
  python -m highopt.runner
"""
from .microstructure import (OrderBookSnapshot, OrderBookShape,
                             SquareRootLawImpact, VPINFilter, MicrostructureGate)
from .arbitrage import (BucketPartitionArb, AdjacentBucketParity,
                        TermStructureMonitor, MultiPlatformSpread, ArbitrageScanner)
from .ml_residual import MLResidualLearner, NLPEnhancement, ResidualFeatureBuilder
from .order_fsm import (OrderFSM, OrderState, ExchangeAdapter,
                        SimExchangeAdapter, PositionReconciler)
from .walk_forward import (PointInTimeLoader, WalkForwardBacktester,
                           CostSensitivityAnalyzer, StabilityMetrics)
from .chaos import FaultInjector, CircuitBreaker, ChaosVerifier

__version__ = "1.0.0"
__all__ = [
    "microstructure", "arbitrage", "ml_residual", "order_fsm", "walk_forward", "chaos",
    "OrderBookSnapshot", "OrderBookShape", "SquareRootLawImpact", "VPINFilter", "MicrostructureGate",
    "BucketPartitionArb", "AdjacentBucketParity", "TermStructureMonitor",
    "MultiPlatformSpread", "ArbitrageScanner",
    "MLResidualLearner", "NLPEnhancement", "ResidualFeatureBuilder",
    "OrderFSM", "OrderState", "ExchangeAdapter", "SimExchangeAdapter", "PositionReconciler",
    "PointInTimeLoader", "WalkForwardBacktester", "CostSensitivityAnalyzer", "StabilityMetrics",
    "FaultInjector", "CircuitBreaker", "ChaosVerifier",
]
