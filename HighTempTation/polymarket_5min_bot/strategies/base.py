"""polymarket_5min_bot — 策略基类与信号结构"""
import abc
import time
from dataclasses import dataclass, field
from typing import List, Optional

from ..markets import FiveMinMarket


@dataclass
class Signal:
    """统一策略信号 (adapter 与 engine 共用)"""
    strategy: str                # ARB / SNIPER / MOMENTUM / LADDER / STAIR
    market: FiveMinMarket
    side: str                    # "YES" / "NO"
    token_id: str
    price: float                 # 期望成交价 (股单价)
    size_usd: float              # 投入金额 ($)
    reason: str = ""
    legs: list = field(default_factory=list)   # 套利第二腿: [{side, price, size_usd}]
    ts: float = field(default_factory=time.time)

    @property
    def shares(self) -> float:
        return self.size_usd / self.price if self.price > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "market": self.market.event_id,
            "asset": self.market.asset,
            "side": self.side,
            "token_id": self.token_id,
            "price": round(self.price, 4),
            "size_usd": round(self.size_usd, 2),
            "shares": round(self.shares, 2),
            "reason": self.reason,
            "legs": self.legs,
            "ts": self.ts,
        }


class StrategyContext:
    """策略运行上下文 (engine 注入, 屏蔽底层执行差异)"""

    def __init__(self, clob, cfg, spot_feed):
        self.clob = clob          # 订单执行接口 (place_order/get_book)
        self.cfg = cfg
        self.spot = spot_feed     # 现货价格

    async def get_book(self, token_id: str) -> Optional[dict]:
        return await self.clob.get_book(token_id)

    async def get_mid(self, token_id: str) -> Optional[float]:
        return await self.clob.get_mid(token_id)


class BaseStrategy(abc.ABC):
    """策略基类 — 只负责生成信号, 不直接下单 (下单经 context.clob)"""

    name: str = "base"

    def __init__(self, cfg):
        self.cfg = cfg

    @abc.abstractmethod
    async def scan(self, ctx: StrategyContext, markets: List[FiveMinMarket]) -> List[Signal]:
        """输入当前活跃市场, 输出待执行信号"""

    def _signals(self, s: List[Signal]) -> List[Signal]:
        return s

    def to_dict(self) -> dict:
        return {"name": self.name, "enabled": True}


class SignalHistory:
    """策略信号/成交历史 (供看板与 DRY_RUN 验证)"""

    def __init__(self, maxlen: int = 500):
        self._items: List[dict] = []
        self._maxlen = maxlen
        self._by_strategy: dict = {}

    def record(self, item: dict):
        self._items.append(item)
        if len(self._items) > self._maxlen:
            self._items = self._items[-self._maxlen:]
        st = item.get("strategy", "?")
        self._by_strategy.setdefault(st, []).append(item)
        if len(self._by_strategy[st]) > self._maxlen:
            self._by_strategy[st] = self._by_strategy[st][-self._maxlen:]

    def recent(self, n: int = 50) -> List[dict]:
        return self._items[-n:]

    def count_by_strategy(self) -> dict:
        return {k: len(v) for k, v in self._by_strategy.items()}
