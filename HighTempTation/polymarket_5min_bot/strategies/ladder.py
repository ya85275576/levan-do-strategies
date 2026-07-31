"""polymarket_5min_bot — Ladder 阶梯做市 + Stair 阶梯出场 (Benjam1nCup #10/#11)

Ladder (做市腿):
  当 YES+NO 两侧组合卖价 > 1.00 + LADDER_MIN_SPREAD 时,
  双向同时挂限价单 (买 YES @ bid, 买 NO @ bid),
  组合成本 < 1.00 → 结算锁价差。周期结束前 LADDER_STOP_BEFORE_END 秒停止。

Stair (出场腿):
  持有双腿仓位时, 结算前按 STAIR_STEPS 批、每批价格偏移
  STAIR_STEP_OFFSET 依次挂卖单 (先出盘口最优侧, 再逐批出对面),
  实现流动性感知的分阶段退场, 减少市场冲击。
"""
import logging
from typing import List

from ..markets import FiveMinMarket
from .base import BaseStrategy, Signal, StrategyContext

logger = logging.getLogger("polymarket_5min.ladder")


class LadderStrategy(BaseStrategy):
    name = "LADDER"

    async def scan(self, ctx: StrategyContext, markets: List[FiveMinMarket]) -> List[Signal]:
        if not self.cfg.LADDER_ENABLED:
            return []
        out: List[Signal] = []
        for m in markets:
            if not m.is_live:
                continue
            if m.seconds_left <= self.cfg.LADDER_STOP_BEFORE_END:
                continue  # 临近结算停止做市
            try:
                if m.simulated:
                    yes_bid = m.prices.get(m.yes_token_id, 0.5) - 0.01
                    no_bid = m.prices.get(m.no_token_id, 0.5) - 0.01
                else:
                    yes_bid = await ctx.clob.get_best_bid(m.yes_token_id)
                    no_bid = await ctx.clob.get_best_bid(m.no_token_id)
                if yes_bid is None or no_bid is None:
                    continue
                combined = yes_bid + no_bid
                if combined > 1.00 - self.cfg.LADDER_MIN_SPREAD:
                    continue  # 组合价差不足 (买不到 <1 的组合)
                edge = 1.0 - combined
                out.append(Signal(
                    strategy=self.name, market=m, side="YES",
                    token_id=m.yes_token_id, price=yes_bid,
                    size_usd=self.cfg.LADDER_SIZE_USD,
                    reason=f"Ladder 做市: YES bid {yes_bid:.3f} + NO bid {no_bid:.3f} "
                           f"= {combined:.3f} (锁 {edge*100:.1f}%)",
                    legs=[{"side": "NO", "token_id": m.no_token_id,
                           "price": no_bid, "size_usd": self.cfg.LADDER_SIZE_USD}],
                ))
                logger.info(f"🪜 Ladder 信号 {m.asset} {m.event_id}: "
                            f"组合 {combined:.3f} → 锁 {edge*100:.1f}%")
            except Exception as e:
                logger.debug(f"Ladder 扫描 {m.event_id} 异常: {e}")
        return out


class StairStrategy(BaseStrategy):
    name = "STAIR"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._last_exit: dict = {}

    async def scan(self, ctx: StrategyContext, markets: List[FiveMinMarket]) -> List[Signal]:
        if not self.cfg.STAIR_ENABLED:
            return []
        # Stair 是出场策略: 扫描引擎持有的双腿仓位, 生成分批卖单
        from ..engine import get_engine
        eng = get_engine()
        if eng is None:
            return []
        out: List[Signal] = []
        for pos in eng.open_positions():
            m = pos["market"]
            if not m.is_live:
                continue
            if m.seconds_left > 120:
                continue  # 只在最后 2 分钟分批出场
            key = pos["market"].event_id
            now = __import__("time").time()
            if now - self._last_exit.get(key, 0.0) < 20:
                continue
            self._last_exit[key] = now
            # 生成 STAIR_STEPS 批的卖单 (价格逐批让步 0.2%)
            mid = pos["mid_price"] or 0.5
            for i in range(self.cfg.STAIR_STEPS):
                px = max(0.01, min(0.99, mid + i * self.cfg.STAIR_STEP_OFFSET))
                out.append(Signal(
                    strategy=self.name, market=m,
                    side="SELL", token_id=pos["token_id"], price=px,
                    size_usd=pos["size_usd"] / self.cfg.STAIR_STEPS,
                    reason=f"Stair 出场 第{i+1}/{self.cfg.STAIR_STEPS}批 @ {px:.3f}",
                ))
        return out
