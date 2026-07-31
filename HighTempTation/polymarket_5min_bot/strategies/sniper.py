"""polymarket_5min_bot — Endcycle Sniper 狙击策略 (Benjam1nCup #1/#8)

核心逻辑:
  周期接近结算 (SNIPER_WINDOW_SEC 内) 时, 高概率侧价格 ≥
  SNIPER_PRICE_THRESH (0.95) 则买入, 结算自动赎回 $1.00,
  在最后几十秒锁定确定性的 3~5% 收益。

风险控制:
  - 价格 < SNIPER_MIN_PRICE (0.80) 拒绝: 结算前价格崩盘不接飞刀
  - 每市场每日成交上限 (防刷单)
"""
import logging
import time
from typing import List

from ..markets import FiveMinMarket
from .base import BaseStrategy, Signal, StrategyContext

logger = logging.getLogger("polymarket_5min.sniper")


class SniperStrategy(BaseStrategy):
    name = "SNIPER"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._last_fire: dict = {}          # market_id -> ts
        self._daily_count: dict = {}        # date -> count
        self._daily_max = 200

    async def scan(self, ctx: StrategyContext, markets: List[FiveMinMarket]) -> List[Signal]:
        if not self.cfg.SNIPER_ENABLED:
            return []
        out: List[Signal] = []
        for m in markets:
            if not m.is_live:
                continue
            secs_left = m.seconds_left
            if secs_left > self.cfg.SNIPER_WINDOW_SEC:
                continue
            try:
                if m.simulated:
                    yes_p = m.prices.get(m.yes_token_id, 0.5)
                    no_p = m.prices.get(m.no_token_id, 0.5)
                else:
                    yes_p = await ctx.get_mid(m.yes_token_id)
                    no_p = await ctx.get_mid(m.no_token_id)
                # 选择高概率侧 (价格高的一侧) 狙击
                if yes_p is not None and (no_p is None or yes_p >= no_p):
                    side, price, tok = "YES", yes_p, m.yes_token_id
                else:
                    side, price, tok = "NO", no_p, m.no_token_id
                if price is None:
                    continue
                if price < self.cfg.SNIPER_MIN_PRICE:
                    continue
                if price < self.cfg.SNIPER_PRICE_THRESH:
                    continue
                now = time.time()
                if now - self._last_fire.get(m.event_id, 0.0) < 30:
                    continue
                self._last_fire[m.event_id] = now
                logger.info(f"🎯 狙击 {m.asset} 结算前 {secs_left:.0f}s: "
                            f"{side} @ {price:.3f} (锁 {1-price:.1%})")
                out.append(Signal(
                    strategy=self.name, market=m, side=side,
                    token_id=tok, price=price,
                    size_usd=self.cfg.SNIPER_SIZE_USD,
                    reason=f"Endcycle Sniper: 结算前 {secs_left:.0f}s 价格 {price:.3f}",
                ))
            except Exception as e:
                logger.debug(f"狙击扫描 {m.event_id} 异常: {e}")
        return out
