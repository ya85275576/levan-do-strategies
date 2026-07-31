#!/usr/bin/env python3
"""
HighTempTation — 反博弈（第四波优化 #5）

功能:
  1. AddressRotation  — 地址轮换（时间/笔数/暴露度三种轮换策略, 防关联分析）
  2. IcebergOrder     — 冰山订单（大单拆分为小可见量, 隐藏真实意图）
  3. NoiseTrader      — 噪声交易（真实订单间隙穿插随机小额订单, 混淆对手）
  4. DashboardDelay   — Dashboard 信号延迟（对外信号加延迟+抖动, 防抄作业）

用法:
  from highopt_ultra.antigame import (
      AddressRotation, IcebergOrder, NoiseTrader, DashboardDelay,
  )

  rot = AddressRotation(pool=["0xA1", "0xA2", "0xA3"], rotate_every_n=5)
  addr = rot.pick()                     # 当前活跃地址
  rot.notify_trade()                    # 每笔交易计数

  ice = IcebergOrder(total=1000, visible=100)
  slices = ice.plan()                   # → [100, 100, ..., 100]

  noise = NoiseTrader(rate=0.3)
  order = noise.maybe_noise(price=0.40)  # 30% 概率生成噪声单

  dd = DashboardDelay(delay_minutes=30)
  shown = dd.expose(signal={"action": "buy", "size": 50})   # 延迟副本
"""
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("highopt_ultra.antigame")

# ════════════════════════════════════════════════════════════════
# 1. 地址轮换
# ════════════════════════════════════════════════════════════════

class AddressRotation:
    """
    地址轮换器。

    高频交易者若始终使用同一地址, 对手/链上分析可轻易识别并抢先交易。
    轮换策略:
      - time-based:   每 N 秒轮换
      - count-based:  每 N 笔交易轮换
      - event-based:  显式触发（检测到被盯梢时）

    用法:
      rot = AddressRotation(pool=["0xA1", "0xA2", "0xA3"],
                            rotate_every_n=5, rotate_every_sec=600)
      a = rot.pick()                    # 返回当前地址
      rot.notify_trade()                # 交易后调用（内部计数）
      rot.force_rotate("被盯梢")         # 主动轮换
    """

    def __init__(self, pool: List[str], rotate_every_n: int = 5,
                 rotate_every_sec: float = 600.0,
                 rng: Optional[random.Random] = None):
        if not pool:
            raise ValueError("地址池不能为空")
        self.pool = list(pool)
        self.rotate_every_n = max(1, rotate_every_n)
        self.rotate_every_sec = rotate_every_sec
        self._rng = rng or random.Random()
        self._idx = 0
        self._trades = 0
        self._since = time.time()
        self._rotations: List[dict] = []
        self._active_from = time.time()

    def pick(self) -> str:
        return self.pool[self._idx]

    def notify_trade(self):
        """每笔真实交易调用一次"""
        self._trades += 1
        if (self._trades >= self.rotate_every_n or
                time.time() - self._since >= self.rotate_every_sec):
            self.force_rotate("schedule")

    def force_rotate(self, reason: str = "manual"):
        """主动轮换（事件驱动）"""
        prev = self.pool[self._idx]
        candidates = [i for i in range(len(self.pool)) if i != self._idx]
        self._idx = self._rng.choice(candidates)
        self._trades = 0
        self._since = time.time()
        self._rotations.append({
            "ts": time.time(), "from": prev, "to": self.pool[self._idx],
            "reason": reason, "trades_before": self._trades,
        })

    def stats(self) -> dict:
        return {"active": self.pick(), "trades": self._trades,
                "rotations": len(self._rotations),
                "last": self._rotations[-1] if self._rotations else None}


# ════════════════════════════════════════════════════════════════
# 2. 冰山订单
# ════════════════════════════════════════════════════════════════

class IcebergOrder:
    """
    冰山订单调度器。

    大额订单一次暴露会推高冲击成本并暴露意图。冰山策略: 只暴露
    visible 量的子单, 成交后继续补单, 直到总成交量达成。

    用法:
      ice = IcebergOrder(total=1000, visible=100, min_slice=10)
      for qty in ice.plan():
          place_order(qty)             # 每次最多 100
      ice.remaining                      # 剩余量
    """

    def __init__(self, total: float, visible: float, min_slice: float = 0.0,
                 max_slices: int = 100):
        self.total = total
        self.visible = min(visible, total)
        self.min_slice = min_slice
        self.max_slices = max_slices
        self._filled = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.total - self._filled)

    def plan(self) -> List[float]:
        """一次性给出完整切片序列（调度用）"""
        out: List[float] = []
        left = self.total
        while left > 0 and len(out) < self.max_slices:
            q = min(self.visible, left)
            if self.min_slice > 0 and q < self.min_slice:
                break                    # 尾量过小, 保留给最后一口
            out.append(q)
            left -= q
        if left > 0:
            out.append(left)             # 最后一口
        return out

    def next_slice(self) -> Optional[float]:
        """流式取下一片（None = 完成）"""
        if self.remaining <= 0:
            return None
        q = min(self.visible, self.remaining)
        self._filled += q
        return q

    def stats(self) -> dict:
        return {"total": self.total, "filled": round(self._filled, 2),
                "remaining": round(self.remaining, 2),
                "slices_used": len(self.plan())}


# ════════════════════════════════════════════════════════════════
# 3. 噪声交易
# ════════════════════════════════════════════════════════════════

@dataclass
class NoiseOrder:
    id: str
    side: str
    price: float
    size: float
    is_noise: bool = True
    ts: float = field(default_factory=time.time)


class NoiseTrader:
    """
    噪声交易生成器。

    在真实订单间隙以一定概率插入随机小额订单, 干扰对手的
    order-flow 分析（如 VPIN、订单关联挖掘）。噪声单全部标记
    is_noise=True, 内部账目/风控自动忽略。

    用法:
      nt = NoiseTrader(rate=0.3, size_min=1, size_max=20, price_spread=0.02)
      for _ in range(100):
          o = nt.maybe_noise(mid=0.40)
          if o:
              exchange.place(o)          # 噪声单
      nt.noise_ratio()                   # 噪声占比统计
    """

    def __init__(self, rate: float = 0.3, size_min: float = 1.0,
                 size_max: float = 20.0, price_spread: float = 0.02,
                 rng: Optional[random.Random] = None):
        self.rate = min(1.0, max(0.0, rate))
        self.size_min = size_min
        self.size_max = size_max
        self.price_spread = price_spread
        self._rng = rng or random.Random()
        self._generated = 0
        self._noise_orders: List[NoiseOrder] = []

    def maybe_noise(self, mid: float) -> Optional[NoiseOrder]:
        """按概率生成一笔噪声单（None = 本次不生成）"""
        if self._rng.random() > self.rate:
            return None
        side = self._rng.choice(["buy", "sell"])
        price = mid + self._rng.uniform(-self.price_spread, self.price_spread)
        size = self._rng.uniform(self.size_min, self.size_max)
        order = NoiseOrder(id=f"noise-{uuid.uuid4().hex[:8]}",
                           side=side, price=round(max(0.01, price), 4),
                           size=round(size, 2))
        self._noise_orders.append(order)
        self._generated += 1
        return order

    def noise_ratio(self, total_orders: int = 0) -> float:
        denom = total_orders or self._generated
        return self._generated / denom if denom else 0.0

    def stats(self) -> dict:
        return {"generated": self._generated, "rate": self.rate,
                "sample": [o.__dict__ for o in self._noise_orders[-3:]]}


# ════════════════════════════════════════════════════════════════
# 4. Dashboard 信号延迟
# ════════════════════════════════════════════════════════════════

class DashboardDelay:
    """
    公开 Dashboard 信号延迟器。

    公开面板若实时暴露信号, 跟随者/对手可抢跑。策略: 对外展示
    延迟 + 抖动后的信号副本; 内部真实信号不受影响。抖动防止对手
    通过固定延迟反推真实时间。

    用法:
      dd = DashboardDelay(delay_minutes=30, jitter_minutes=5)
      internal = {"action": "buy", "size": 50, "ts": time.time()}
      public = dd.expose(internal)       # ts 被推到 30±5 分钟后, 其余字段保留
      public["reveal_at"]                # 真实可展示时间
    """

    def __init__(self, delay_minutes: float = 30.0, jitter_minutes: float = 5.0,
                 rng: Optional[random.Random] = None):
        self.delay_minutes = delay_minutes
        self.jitter_minutes = jitter_minutes
        self._rng = rng or random.Random()
        self._exposed: List[dict] = []

    def expose(self, signal: dict) -> dict:
        """返回延迟副本（不改动原信号）"""
        delay = self.delay_minutes + self._rng.uniform(-self.jitter_minutes,
                                                       self.jitter_minutes)
        out = dict(signal)
        out["ts"] = (signal.get("ts", time.time()) + delay * 60.0)
        out["reveal_at"] = out["ts"]
        out["delayed"] = True
        self._exposed.append(out)
        return out

    def internal(self, signal: dict) -> dict:
        """内部真实信号（不延迟）"""
        out = dict(signal)
        out["ts"] = signal.get("ts", time.time())
        out["delayed"] = False
        return out

    def stats(self) -> dict:
        return {"exposed_count": len(self._exposed),
                "delay_minutes": self.delay_minutes,
                "jitter_minutes": self.jitter_minutes}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    rot = AddressRotation(["0xA1", "0xA2", "0xA3"], rotate_every_n=2)
    for i in range(6):
        rot.notify_trade()
    print("rotation stats:", rot.stats())
    ice = IcebergOrder(total=1000, visible=150)
    print("iceberg slices:", ice.plan())
    nt = NoiseTrader(rate=0.5, seed=0)
    nt._rng = random.Random(0)
    n = sum(1 for _ in range(50) if nt.maybe_noise(0.40))
    print("noise count:", n)
    dd = DashboardDelay(delay_minutes=30, jitter_minutes=5)
    s = dd.expose({"action": "buy", "size": 50, "ts": 1000.0})
    print("dashboard delayed ts:", s["ts"])
