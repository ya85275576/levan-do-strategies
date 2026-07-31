"""polymarket_5min_bot — 互补套利策略 (Benjam1nCup Arbitrage: Buy1 + Buy2)

核心逻辑 (README #2/#8/#9):
  1. 从订单簿读取 YES/NO 两侧最优价, 计算组合成本 YES_price + NO_price
  2. 组合成本 ≤ ARB_COMBINED_TARGET (0.95) 时:
       Buy1 = 买高概率侧 (价格更高的一侧, 通常 ≥0.55)
       Buy2 = 立即买对面, 组合锁定 (1.00 - combined) 的确定性收益
  3. 结算自动赎回 $1.00 → 每周期稳定吃 3~5 分价差

风险控制:
  - 组合成本上限 ARB_MAX_COMBINED (0.97): 成本过高不接
  - 最小 edge ARB_MIN_EDGE (1.5%): 收益不达阈值不交易
  - 单腿价格崩盘 (买不到互补侧) → 放弃整个配对, 避免单腿裸奔
"""
import logging
from typing import List

from ..markets import FiveMinMarket
from .base import BaseStrategy, Signal, StrategyContext

logger = logging.getLogger("polymarket_5min.arb")


class ArbitrageStrategy(BaseStrategy):
    name = "ARB"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._open_legs = {}  # market_id -> ts (Buy1 后等待 Buy2)

    async def scan(self, ctx: StrategyContext, markets: List[FiveMinMarket]) -> List[Signal]:
        if not self.cfg.ARB_ENABLED:
            return []
        out: List[Signal] = []
        for m in markets:
            if not m.is_live:
                continue
            try:
                sig = await self._scan_market(ctx, m)
                if sig:
                    out.append(sig)
            except Exception as e:
                logger.debug(f"套利扫描 {m.event_id} 异常: {e}")
        return out

    async def _scan_market(self, ctx: StrategyContext, m: FiveMinMarket) -> Signal | None:
        # 真实市场: 从订单簿取价; 模拟市场: 直接用预生成价格
        if m.simulated:
            yes_p = m.prices.get(m.yes_token_id, 0.5)
            no_p = m.prices.get(m.no_token_id, 0.5)
        else:
            yes_p = await ctx.get_mid(m.yes_token_id)
            no_p = await ctx.get_mid(m.no_token_id)
            if yes_p is None or no_p is None:
                return None

        combined = yes_p + no_p
        if combined <= 0 or combined > self.cfg.ARB_COMBINED_TARGET:
            return None
        edge = 1.0 - combined
        if edge < self.cfg.ARB_MIN_EDGE:
            return None

        # Buy1 = 高概率侧 (价格高的一侧)
        if yes_p >= no_p:
            leg1 = {"side": "YES", "price": yes_p, "token": m.yes_token_id}
            leg2 = {"side": "NO", "price": no_p, "token": m.no_token_id}
        else:
            leg1 = {"side": "NO", "price": no_p, "token": m.no_token_id}
            leg2 = {"side": "YES", "price": yes_p, "token": m.yes_token_id}

        # 冷却: 同一市场配对进行中则跳过
        now_ts = __import__("time").time()
        last = self._open_legs.get(m.event_id, 0.0)
        if now_ts - last < 60:
            return None
        self._open_legs[m.event_id] = now_ts

        size_usd = self.cfg.ARB_SIZE_USD
        logger.info(f"🧲 套利信号 {m.asset} {m.event_id}: "
                    f"组合成本 ${combined:.3f} → 锁定 {edge*100:.1f}%")
        return Signal(
            strategy=self.name,
            market=m,
            side=leg1["side"],
            token_id=leg1["token"],
            price=leg1["price"],
            size_usd=size_usd,
            reason=f"互补套利: YES+NO=${combined:.3f}, 锁定 {edge*100:.1f}%",
            legs=[{"side": leg2["side"], "token_id": leg2["token"],
                   "price": leg2["price"], "size_usd": size_usd}],
        )
