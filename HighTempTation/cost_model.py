#!/usr/bin/env python3
"""
HighTempTation — 交易成本模型 (Day1 关键项 #2)

核心公式:
    edge_net = |p_model - p_market| - taker_fee - gas - slippage - impact - theta_decay

设计原则:
  - gross edge 是模型价与市场价的绝对偏差；真正可落袋的是扣除全部摩擦后的净 edge
  - 每一项成本都建模为"每股 (share) 的期望成本"，与仓位大小/市场深度/时间相关
  - 成本项全部可配置 (环境变量)，默认值是 Polymarket 场景的保守估计:
      * Polymarket 名义 0 手续费，但 taker_fee 保留 0.2% 作为保守摩擦
      * gas: Polygon 链上结算/成交的摊薄成本 (每股)
      * slippage: 与订单簿深度相关 (size / depth)，深度不足时线性放大
      * impact: 大仓位对市场的冲击 (size / liquidity)
      * theta_decay: 时间衰减——距结算越近，edge 消散越快 (与 bot 的
        theta_mult 乘数互补: 那里是"门槛上浮"，这里是"edge 扣减")

用法:
  cm = CostModel()
  breakdown = cm.compute_net_edge(p_model=0.35, p_market=0.45, side="NO",
                                  size=50, liquidity=2000, end_date=iso)
  # → CostBreakdown(gross_edge=0.10, net_edge=0.067, is_profitable=True, ...)
  if breakdown.is_profitable: 开仓
"""
import logging
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("cost_model")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class CostBreakdown:
    """单笔信号的成本明细 (全部以概率/每股为单位, 0~1)"""
    p_model: float          # 模型概率 (已校准)
    p_market: float         # 市场价格 (YES)
    side: str               # "YES" / "NO"
    gross_edge: float       # |p_model - p_market| (按任务公式)
    directional_edge: float # 方向性 edge: 买入价 vs 模型概率差 (更严格的定义)
    taker_fee: float        # 手续费
    gas: float              # 链上 gas 摊薄
    slippage: float         # 滑点
    impact: float           # 市场冲击
    theta_decay: float      # 时间衰减
    total_cost: float       # 五项成本之和
    net_edge: float         # gross_edge - total_cost
    is_profitable: bool     # net_edge >= MIN_NET_EDGE
    detail: str = ""        # 人类可读明细 (日志用)

    def to_dict(self) -> dict:
        return asdict(self)


class CostModel:
    """
    交易成本模型。

    参数 (环境变量, 默认值为 Polymarket 保守估计):
      COST_TAKER_FEE        : 吃单手续费率 (默认 0.002 = 0.2%)
      COST_GAS_PER_SHARE    : 每股链上 gas 摊薄 (默认 0.001)
      COST_SLIPPAGE_COEF    : 滑点系数 (默认 0.02; 滑点 = coef × size/depth)
      COST_SLIPPAGE_CAP     : 滑点上限 (默认 0.05)
      COST_IMPACT_COEF      : 冲击系数 (默认 0.01; 冲击 = coef × size/liquidity)
      COST_IMPACT_CAP       : 冲击上限 (默认 0.03)
      COST_THETA_RATE       : 时间衰减速率 (默认 0.02; 满 24h 衰减上限)
      COST_THETA_CAP        : 时间衰减上限 (默认 0.05)
      MIN_NET_EDGE          : 最小净 edge 阈值 (默认 0.03, 取代/收紧 CALIB_THRESH)
    """

    def __init__(self):
        self.taker_fee = _env_float("COST_TAKER_FEE", 0.002)
        self.gas_per_share = _env_float("COST_GAS_PER_SHARE", 0.001)
        self.slippage_coef = _env_float("COST_SLIPPAGE_COEF", 0.02)
        self.slippage_cap = _env_float("COST_SLIPPAGE_CAP", 0.05)
        self.impact_coef = _env_float("COST_IMPACT_COEF", 0.01)
        self.impact_cap = _env_float("COST_IMPACT_CAP", 0.03)
        self.theta_rate = _env_float("COST_THETA_RATE", 0.02)
        self.theta_cap = _env_float("COST_THETA_CAP", 0.05)
        self.min_net_edge = _env_float("MIN_NET_EDGE", 0.03)

        # 统计 (供 /api/metrics)
        self.n_evaluated = 0
        self.n_passed = 0
        self.n_rejected = 0
        self.avg_net_edge = 0.0
        self._net_edge_sum = 0.0
        self.last_breakdown: Optional[CostBreakdown] = None

    # ════════════════════════════════════════════════════════════════
    # 核心接口
    # ════════════════════════════════════════════════════════════════

    def compute_net_edge(self, p_model: float, p_market: float, side: str,
                         size: float = 10.0, liquidity: float = 0.0,
                         end_date: str = "") -> CostBreakdown:
        """
        计算净 edge (每股)。

        Args:
            p_model: 模型概率 (建议传入校准后概率)
            p_market: 市场价格 (YES 概率)
            side: "YES" 或 "NO"
            size: 计划仓位 ($, 每股 $1 的 Polymarket 合约)
            liquidity: 市场流动性/深度 ($)。0 = 未知, 用保守默认深度
            end_date: ISO 结算时间, 用于 theta 衰减

        Returns:
            CostBreakdown
        """
        p_model = min(0.999, max(0.001, p_model))
        p_market = min(0.999, max(0.001, p_market))

        # 1. 毛 edge (任务公式): 模型与市场的绝对偏差
        gross_edge = abs(p_model - p_market)

        # 2. 方向性 edge (更严格): 买入价与模型概率的差
        #    买 NO 时买入价 = 1 - p_market, 我们的 NO 概率 = 1 - p_model
        if side == "NO":
            entry = 1.0 - p_market
            prob = 1.0 - p_model
        else:
            entry = p_market
            prob = p_model
        directional_edge = abs(prob - entry)

        # 3. 手续费 (每股)
        taker_fee = self.taker_fee

        # 4. gas (每股摊薄)
        gas = self.gas_per_share

        # 5. 滑点: 仓位 / 深度, 深度未知时按保守深度 $500 算
        depth = max(liquidity, 500.0)
        slippage = min(self.slippage_cap,
                       self.slippage_coef * size / depth)

        # 6. 市场冲击
        impact = min(self.impact_cap,
                     self.impact_coef * size / depth)

        # 7. theta 时间衰减: 距结算越近 edge 消散越快
        #    theta_decay = rate × (1 - hours_remaining/24), clamp [0, cap]
        theta_decay = 0.0
        if end_date:
            try:
                ed = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                hours_left = (ed - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left > 0:
                    theta_decay = min(self.theta_cap,
                                      self.theta_rate * (1.0 - min(hours_left, 24.0) / 24.0))
            except Exception:
                pass

        total_cost = taker_fee + gas + slippage + impact + theta_decay
        net_edge = gross_edge - total_cost

        breakdown = CostBreakdown(
            p_model=round(p_model, 4),
            p_market=round(p_market, 4),
            side=side,
            gross_edge=round(gross_edge, 4),
            directional_edge=round(directional_edge, 4),
            taker_fee=round(taker_fee, 4),
            gas=round(gas, 4),
            slippage=round(slippage, 4),
            impact=round(impact, 4),
            theta_decay=round(theta_decay, 4),
            total_cost=round(total_cost, 4),
            net_edge=round(net_edge, 4),
            is_profitable=net_edge >= self.min_net_edge,
        )
        breakdown.detail = (
            f"gross={gross_edge:.1%} - fee={taker_fee:.1%} - gas={gas:.1%} "
            f"- slip={slippage:.1%} - impact={impact:.1%} - θ={theta_decay:.1%} "
            f"= net {net_edge:+.1%} (需≥{self.min_net_edge:.1%})"
        )

        # 统计
        self.n_evaluated += 1
        if breakdown.is_profitable:
            self.n_passed += 1
        else:
            self.n_rejected += 1
        self._net_edge_sum += net_edge
        self.avg_net_edge = round(self._net_edge_sum / self.n_evaluated, 4)
        self.last_breakdown = breakdown
        return breakdown

    def evaluate_signal(self, sig: dict, size: float = 10.0) -> CostBreakdown:
        """
        直接评估信号 dict (bot 主循环集成入口)。

        从信号提取 p_model / p_market / side / liquidity / end_date，
        返回成本明细。is_profitable=False 的信号应被过滤。
        """
        return self.compute_net_edge(
            p_model=sig.get("p_model", 0.5),
            p_market=sig.get("p_market", 0.5),
            side=sig.get("side", "NO"),
            size=size,
            liquidity=float(sig.get("liquidity", 0) or 0),
            end_date=sig.get("end_date", ""),
        )

    # ════════════════════════════════════════════════════════════════
    # 统计 (供 /api/metrics)
    # ════════════════════════════════════════════════════════════════

    def get_stats(self) -> dict:
        return {
            "enabled": True,
            "min_net_edge": self.min_net_edge,
            "params": {
                "taker_fee": self.taker_fee,
                "gas_per_share": self.gas_per_share,
                "slippage_coef": self.slippage_coef,
                "slippage_cap": self.slippage_cap,
                "impact_coef": self.impact_coef,
                "impact_cap": self.impact_cap,
                "theta_rate": self.theta_rate,
                "theta_cap": self.theta_cap,
            },
            "n_evaluated": self.n_evaluated,
            "n_passed": self.n_passed,
            "n_rejected": self.n_rejected,
            "reject_rate": round(self.n_rejected / max(self.n_evaluated, 1), 4),
            "avg_net_edge": self.avg_net_edge,
            "last_breakdown": self.last_breakdown.to_dict() if self.last_breakdown else None,
        }
