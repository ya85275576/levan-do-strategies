"""polymarket_5min_bot — 流动性动量策略 (Benjam1nCup #2/#12)

核心逻辑 (Liquidity Momentum Arbitrage):
  1. 实时订单簿 → OBI (order-book influence): 买卖压力突变
  2. OBI 方向 + 现货价 vs 行权价偏离方向双确认
  3. 确认后买入压力指向的一侧 (Buy1), 同时买对面互补 (Buy2)
     组合成本 ≈ 0.95, 结算 $1.00 → 高胜率 + 低风险
  4. 动量冷却: 同一市场两次入场间隔 MOMENTUM_COOLDOWN 秒
"""
import logging
import time
from typing import List

from ..markets import FiveMinMarket
from ..obi import compute_obi, compute_imbalance_signal, price_deviation, confirm_direction
from .base import BaseStrategy, Signal, StrategyContext

logger = logging.getLogger("polymarket_5min.momentum")


class MomentumStrategy(BaseStrategy):
    name = "MOMENTUM"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._last_fire: dict = {}

    async def scan(self, ctx: StrategyContext, markets: List[FiveMinMarket]) -> List[Signal]:
        if not self.cfg.MOMENTUM_ENABLED:
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
                logger.debug(f"动量扫描 {m.event_id} 异常: {e}")
        return out

    async def _scan_market(self, ctx: StrategyContext, m: FiveMinMarket) -> Signal | None:
        now = time.time()
        if now - self._last_fire.get(m.event_id, 0.0) < self.cfg.MOMENTUM_COOLDOWN:
            return None

        if m.simulated:
            # 模拟市场: 用预生成价格 + 随机 OBI 近似 (演示信号链路)
            yes_p = m.prices.get(m.yes_token_id, 0.5)
            no_p = 1.0 - yes_p
            obi = (yes_p - 0.5) * 2.0
        else:
            yes_book = await ctx.get_book(m.yes_token_id)
            no_book = await ctx.get_book(m.no_token_id)
            if not yes_book or not no_book:
                return None
            obi_yes = compute_obi(yes_book["bids"], yes_book["asks"])
            obi_no = compute_obi(no_book["bids"], no_book["asks"])
            # YES 侧 OBI 为正 = 买 YES 压力; NO 侧 OBI 为正 = 买 NO 压力
            obi = (obi_yes if obi_yes is not None else 0.0) - \
                  (obi_no if obi_no is not None else 0.0)
            yes_p = await ctx.get_mid(m.yes_token_id)
            no_p = await ctx.get_mid(m.no_token_id)

        sig_dir = compute_imbalance_signal(obi, self.cfg.MOMENTUM_OBI_THRESH)
        if not sig_dir:
            return None

        # 现货确认: 现价相对行权价的方向需与 OBI 一致
        spot = m.spot_price or ctx.spot.get(m.asset)
        dev = price_deviation(spot, m.strike_price)
        if m.strike_price <= 0:
            dev = (yes_p - 0.5) * 2 * self.cfg.MOMENTUM_PRICE_DEV * 10  # 模拟盘近似
        if not confirm_direction(sig_dir, dev):
            # 方向不一致 → 无确认, 放弃 (防假突破)
            return None

        side = sig_dir
        price = yes_p if side == "YES" else no_p
        if price is None or price <= 0:
            return None
        tok = m.yes_token_id if side == "YES" else m.no_token_id
        other_tok = m.no_token_id if side == "YES" else m.yes_token_id
        other_price = no_p if side == "YES" else yes_p

        self._last_fire[m.event_id] = now
        logger.info(f"⚡ 动量信号 {m.asset} {m.event_id}: OBI={obi:+.2f} "
                    f"方向={side} spot={spot:.0f} strike={m.strike_price:.0f} dev={dev:+.3%}")
        return Signal(
            strategy=self.name, market=m, side=side, token_id=tok,
            price=price, size_usd=self.cfg.MOMENTUM_SIZE_USD,
            reason=f"流动性动量: OBI={obi:+.2f}, 现价偏离 {dev:+.3%}",
            # 动量也做互补对冲 (Buy2 对面, 控制尾部风险)
            legs=[{"side": "NO" if side == "YES" else "YES",
                   "token_id": other_tok, "price": other_price,
                   "size_usd": self.cfg.MOMENTUM_SIZE_USD * 0.5}],
        )
