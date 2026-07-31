#!/usr/bin/env python3
"""
HighTempTation — 订单簿微观结构分析（高阶优化 #1）

功能:
  1. LOB 形状建模        — 对订单簿价格档位拟合指数/线性形状, 输出簿的厚度
  2. 深度斜率检测        — 累计深度随价格距离的斜率, 判断簿的薄厚与支撑质量
  3. Square-Root Law 冲击预估 — ΔP = k·σ·√(Q/V), 预估吃单冲击成本
  4. VPIN 逆向选择过滤   — 成交量同步知情交易概率, 高 VPIN 时过滤开仓

用法:
  from highopt.microstructure import (
      OrderBookSnapshot, OrderBookShape, SquareRootLawImpact, VPINFilter,
      MicrostructureGate,
  )
  # 构造快照: bids/asks 为 [(price, size), ...]
  snap = OrderBookSnapshot(bids=[(0.40, 500), (0.39, 800)], asks=[(0.41, 600), (0.42, 700)])
  shape = OrderBookShape(snap)
  print(shape.depth_slope, shape.lob_shape)          # 深度斜率 + 形状分类
  impact = SquareRootLawImpact().estimate(qty=100, sigma=0.02, volume=5000)
  vpin = VPINFilter(window=50)
  vpin.update(price=0.41, volume=300)                # 每笔 tick 更新
  if vpin.vpin > 0.3:                                # 高 VPIN → 不开仓
      ...

开仓前一站式检查（模拟 v6 的过滤风格）:
  gate = MicrostructureGate()
  ok, reasons, metrics = gate.check(snapshot, qty=50, sigma=0.02, volume=5000)
"""
import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("highopt.microstructure")

# ── 默认阈值（可被环境变量覆盖）──
VPIN_HIGH = 0.30                 # VPIN 高于此值视为知情交易密集
DEPTH_SLOPE_THIN = 0.5           # 累计深度斜率低于此值 → 簿薄
MIN_TOTAL_DEPTH = 200            # 总深度下限
MAX_IMPACT_PCT = 0.20            # 冲击成本占价格比例上限 (%)
SQRT_LAW_K_DEFAULT = 1.0         # Square-Root Law 常数 k（按市场校准）


# ════════════════════════════════════════════════════════════════
# LOB 快照与形状建模
# ════════════════════════════════════════════════════════════════

@dataclass
class OrderBookSnapshot:
    """
    订单簿快照。

    :param bids: 买盘档位 [(price, size), ...]，按价格降序
    :param asks: 卖盘档位 [(price, size), ...]，按价格升序
    :param mid:  中间价（缺省取最佳买卖价均值）
    """
    bids: List[Tuple[float, float]] = field(default_factory=list)
    asks: List[Tuple[float, float]] = field(default_factory=list)
    mid: float = 0.0

    def __post_init__(self):
        self.bids = sorted(self.bids, key=lambda x: x[0], reverse=True)
        self.asks = sorted(self.asks, key=lambda x: x[0])
        if not self.mid and self.bids and self.asks:
            self.mid = (self.bids[0][0] + self.asks[0][0]) / 2.0

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def depth_bid(self) -> float:
        return sum(s for _, s in self.bids)

    @property
    def depth_ask(self) -> float:
        return sum(s for _, s in self.asks)

    @property
    def total_depth(self) -> float:
        return self.depth_bid + self.depth_ask

    @property
    def obi(self) -> float:
        """订单簿不均衡 (Order Book Imbalance), 与 v6 get_obi 一致"""
        total = self.depth_bid + self.depth_ask
        if total == 0:
            return 0.0
        return (self.depth_bid - self.depth_ask) / total

    def to_dict(self) -> dict:
        return {
            "mid": self.mid,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "depth_bid": self.depth_bid,
            "depth_ask": self.depth_ask,
            "total_depth": self.total_depth,
            "obi": self.obi,
            "n_bid_levels": len(self.bids),
            "n_ask_levels": len(self.asks),
        }


class OrderBookShape:
    """
    LOB 形状建模 + 深度斜率检测。

    方法:
      - cumulative_depth(side, max_dist): 距中间价 max_dist 内的累计深度
      - depth_slope(side):                累计深度 vs 距离的线性斜率（每价格单位张数）
      - exp_decay(side):                  档位深度指数衰减率 b（size ≈ a·e^(-b·d)）
      - lob_shape:                        按斜率/厚度分类 THICK / NORMAL / THIN
    """

    def __init__(self, snap: OrderBookSnapshot,
                 thin_slope: float = DEPTH_SLOPE_THIN,
                 min_depth: float = MIN_TOTAL_DEPTH):
        self.snap = snap
        self.thin_slope = thin_slope
        self.min_depth = min_depth

    # ── 累计深度 ──

    def cumulative_depth(self, side: str = "both",
                         max_dist: Optional[float] = None) -> float:
        """距中间价 max_dist 内的累计深度（side: bid/ask/both）"""
        levels = []
        if side in ("bid", "both"):
            levels += [(self.snap.mid - p, s) for p, s in self.snap.bids if p < self.snap.mid]
        if side in ("ask", "both"):
            levels += [(p - self.snap.mid, s) for p, s in self.snap.asks if p > self.snap.mid]
        total = 0.0
        for dist, size in levels:
            if max_dist is None or dist <= max_dist:
                total += size
        return total

    # ── 深度斜率（累计深度 vs 距离）──

    def depth_slope(self, side: str = "both") -> float:
        """
        深度斜率: 对 (distance, cumulative_depth) 做最小二乘线性拟合,
        斜率 = 每移动 1 个价格单位新增多少张深度。
        斜率小 → 簿薄（价格易被推动）; 斜率大 → 簿厚。
        """
        pts: List[Tuple[float, float]] = []
        if side in ("bid", "both"):
            for p, s in sorted(self.snap.bids, key=lambda x: x[0], reverse=True):
                d = self.snap.mid - p
                if d > 0:
                    pts.append((d, s))
        if side in ("ask", "both"):
            for p, s in sorted(self.snap.asks, key=lambda x: x[0]):
                d = p - self.snap.mid
                if d > 0:
                    pts.append((d, s))
        if not pts:
            return 0.0
        # 按距离累计
        pts.sort(key=lambda x: x[0])
        cum = 0.0
        cum_pts: List[Tuple[float, float]] = []
        for d, s in pts:
            cum += s
            cum_pts.append((d, cum))
        return _linreg_slope(cum_pts)

    # ── 指数衰减形状 ──

    def exp_decay(self, side: str = "bid") -> float:
        """
        档位深度指数衰减率 b: size(d) ≈ a·exp(-b·d)。
        b 大 → 深度集中在盘口（簿薄）; b 小 → 深度均匀分布（簿厚）。
        """
        levels = self.snap.bids if side == "bid" else self.snap.asks
        pts = []
        for p, s in levels:
            d = abs(p - self.snap.mid)
            if d > 0 and s > 0:
                pts.append((d, math.log(s)))
        if len(pts) < 2:
            return 0.0
        return -_linreg_slope(pts)  # 斜率为负 → b = -slope

    # ── 形状分类 ──

    @property
    def lob_shape(self) -> str:
        """
        按近端流动性分类: THICK / NORMAL / THIN。
        判据 = 距中间价 ±0.05 内的累计深度（近端流动性是吃单冲击的直接缓冲）:
          - < min_depth          → THIN（近端无支撑）
          - ≥ min_depth * 3      → THICK
          - 其余                → NORMAL
        """
        near = self.cumulative_depth("both", max_dist=0.05)
        if near < self.min_depth:
            return "THIN"
        if near >= self.min_depth * 3:
            return "THICK"
        return "NORMAL"

    def to_dict(self) -> dict:
        d = self.snap.to_dict()
        d.update({
            "depth_slope": round(self.depth_slope(), 4),
            "exp_decay_bid": round(self.exp_decay("bid"), 4),
            "exp_decay_ask": round(self.exp_decay("ask"), 4),
            "lob_shape": self.lob_shape,
        })
        return d


def _linreg_slope(pts: List[Tuple[float, float]]) -> float:
    """最小二乘斜率（纯 Python，避免 numpy 依赖）"""
    n = len(pts)
    if n < 2:
        return 0.0
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return 0.0
    return (n * sxy - sx * sy) / denom


# ════════════════════════════════════════════════════════════════
# Square-Root Law 冲击预估
# ════════════════════════════════════════════════════════════════

class SquareRootLawImpact:
    """
    Square-Root Law 冲击成本预估。

    公式: ΔP = k · σ · √(Q / V)
      Q = 订单数量（张）
      V = 市场成交量（如 24h 成交量或近端累计深度）
      σ = 价格波动率（与 V 同周期）
      k = 市场常数（默认 1.0，可按市场历史成交校准）

    适用性: 该定律源自 Almgren-Chriss 执行模型, 对簿薄的市场
    （如预测市场长尾桶）尤其重要 —— 深度不足时市价单冲击巨大。
    """

    def __init__(self, k: float = SQRT_LAW_K_DEFAULT):
        self.k = k

    def impact(self, qty: float, sigma: float, volume: float) -> float:
        """冲击价格变动（与 sigma 同单位）"""
        if qty <= 0 or volume <= 0 or sigma <= 0:
            return 0.0
        return self.k * sigma * math.sqrt(qty / volume)

    def impact_pct(self, qty: float, sigma: float, volume: float,
                   mid: float) -> float:
        """冲击成本占中间价百分比（%）"""
        if mid <= 0:
            return 0.0
        return self.impact(qty, sigma, volume) / mid * 100.0

    def total_cost(self, qty: float, sigma: float, volume: float,
                   mid: float, fee_pct: float = 0.0) -> float:
        """总成本 = 冲击 + 手续费（价格单位）"""
        imp = self.impact(qty, sigma, volume)
        fee = mid * fee_pct / 100.0
        return imp + fee

    def max_qty_for_budget(self, budget: float, sigma: float, volume: float,
                           mid: float, fee_pct: float = 0.0) -> float:
        """
        在预算（价格单位）内可承受的最大订单量。
        由 budget = k·σ·√(Q/V) + mid·fee_pct 反解 Q。
        """
        if sigma <= 0 or volume <= 0 or budget <= 0:
            return 0.0
        fee = mid * fee_pct / 100.0
        if budget <= fee:
            return 0.0
        x = (budget - fee) / (self.k * sigma)
        return min(volume, volume * x * x)

    def calibrate_k(self, observed: List[Tuple[float, float, float, float]]) -> float:
        """
        用历史 (qty, sigma, volume, realized_impact) 校准 k。
        返回 k = median(impact / (σ·√(Q/V)))，样本不足时保持默认。
        """
        ks = []
        for qty, sigma, volume, imp in observed:
            if qty > 0 and sigma > 0 and volume > 0:
                ks.append(imp / (sigma * math.sqrt(qty / volume)))
        if not ks:
            return self.k
        ks.sort()
        self.k = ks[len(ks) // 2]
        return self.k


# ════════════════════════════════════════════════════════════════
# VPIN 逆向选择过滤
# ════════════════════════════════════════════════════════════════

class VPINFilter:
    """
    Volume-Synchronized Probability of Informed Trading（成交量同步知情交易概率）。

    原理 (Easley-López de Prado-O'Hara 2012):
      - 把成交量切成等量桶（bucket）
      - 每桶用 tick 规则 / 批量量分类估计买入量 V_buy、卖出量 V_sell
      - VPIN = Σ|V_buy - V_sell| / (n · V_bucket)
      - 高 VPIN → 知情交易占比高 → 逆向选择风险大 → 应过滤或缩小仓位

    用法:
      vpin = VPINFilter(bucket_volume=1000, window=50)
      for price, vol in stream:
          vpin.update(price=price, volume=vol)
      print(vpin.vpin, vpin.is_high())
    """

    def __init__(self, bucket_volume: float = 1000.0, window: int = 50,
                 high_threshold: float = VPIN_HIGH):
        self.bucket_volume = bucket_volume
        self.window = window
        self.high_threshold = high_threshold
        self._last_price: Optional[float] = None
        self._bucket_buy = 0.0
        self._bucket_sell = 0.0
        self._imbalances: List[float] = []   # 每桶 |V_buy - V_sell| / V

    def _classify(self, price: float, volume: float) -> Tuple[float, float]:
        """tick 规则分类: 价格上涨→买, 下跌→卖, 平盘→均分"""
        if self._last_price is None or abs(price - self._last_price) < 1e-12:
            return volume / 2.0, volume / 2.0
        if price > self._last_price:
            return volume, 0.0
        return 0.0, volume

    def update(self, price: float, volume: float) -> float:
        """
        更新一笔记账（tick）。
        :returns: 当前 VPIN
        """
        buy, sell = self._classify(price, volume)
        self._bucket_buy += buy
        self._bucket_sell += sell
        self._last_price = price

        while self._bucket_buy + self._bucket_sell >= self.bucket_volume:
            # 满桶结算
            total = self._bucket_buy + self._bucket_sell
            # 按比例归一到桶容量，剩余滚入下一桶
            scale = self.bucket_volume / total
            buy_norm = self._bucket_buy * scale
            sell_norm = self._bucket_sell * scale
            self._imbalances.append(abs(buy_norm - sell_norm) / self.bucket_volume)
            if len(self._imbalances) > self.window:
                self._imbalances.pop(0)
            # 残留（< 1 桶的部分）保留
            residual = total - self.bucket_volume
            if residual > 0 and total > 0:
                self._bucket_buy = self._bucket_buy * (residual / total)
                self._bucket_sell = self._bucket_sell * (residual / total)
            else:
                self._bucket_buy = 0.0
                self._bucket_sell = 0.0

        return self.vpin

    def update_bucket(self, buy_volume: float, sell_volume: float) -> float:
        """直接喂一个已完成桶的买卖量（外部分类器可用）"""
        total = buy_volume + sell_volume
        if total <= 0:
            return self.vpin
        self._imbalances.append(abs(buy_volume - sell_volume) / total)
        if len(self._imbalances) > self.window:
            self._imbalances.pop(0)
        return self.vpin

    @property
    def vpin(self) -> float:
        if not self._imbalances:
            return 0.0
        return sum(self._imbalances) / len(self._imbalances)

    def is_high(self) -> bool:
        return self.vpin >= self.high_threshold

    def reset(self):
        self._last_price = None
        self._bucket_buy = 0.0
        self._bucket_sell = 0.0
        self._imbalances.clear()


# ════════════════════════════════════════════════════════════════
# 开仓前一站式检查（对齐 v6 过滤风格）
# ════════════════════════════════════════════════════════════════

class MicrostructureGate:
    """
    微观结构开仓闸门 —— 把上述四项分析打包成 v6 风格的过滤链。

    check() 返回 (ok, reasons, metrics):
      ok      — 是否允许开仓
      reasons — 拒绝原因列表（空 = 通过）
      metrics — 各项微观结构指标
    """

    def __init__(self, vpin: Optional[VPINFilter] = None,
                 min_depth: float = MIN_TOTAL_DEPTH,
                 max_impact_pct: float = MAX_IMPACT_PCT,
                 high_vpin: float = VPIN_HIGH):
        self.vpin = vpin or VPINFilter()
        self.min_depth = min_depth
        self.max_impact_pct = max_impact_pct
        self.high_vpin = high_vpin

    def check(self, snapshot: OrderBookSnapshot, qty: float,
              sigma: float, volume: float,
              fee_pct: float = 0.0) -> Tuple[bool, List[str], dict]:
        """
        开仓前微观结构检查。

        :param snapshot: LOB 快照
        :param qty: 计划开仓数量（张）
        :param sigma: 波动率（与 volume 同周期）
        :param volume: 参考成交量（24h 或近端深度）
        :param fee_pct: 手续费率（%）
        :returns: (ok, reasons, metrics)
        """
        shape = OrderBookShape(snapshot, min_depth=self.min_depth)
        impact_model = SquareRootLawImpact()
        impact = impact_model.impact(qty, sigma, volume)
        impact_pct = impact_model.impact_pct(qty, sigma, volume, snapshot.mid or 1.0)
        total_cost = impact_model.total_cost(qty, sigma, volume,
                                             snapshot.mid or 1.0, fee_pct)

        metrics = {
            "mid": snapshot.mid,
            "obi": snapshot.obi,
            "total_depth": snapshot.total_depth,
            "depth_slope": round(shape.depth_slope(), 4),
            "lob_shape": shape.lob_shape,
            "impact": round(impact, 6),
            "impact_pct": round(impact_pct, 4),
            "total_cost": round(total_cost, 6),
            "vpin": round(self.vpin.vpin, 4),
        }

        reasons: List[str] = []
        if snapshot.total_depth < self.min_depth:
            reasons.append(f"深度不足: {snapshot.total_depth:.0f} < {self.min_depth:.0f}")
        if shape.lob_shape == "THIN":
            reasons.append(f"簿过薄: shape={shape.lob_shape} slope={metrics['depth_slope']:.3f}")
        if impact_pct > self.max_impact_pct:
            reasons.append(f"冲击过大: {impact_pct:.2f}% > {self.max_impact_pct:.2f}%")
        if self.vpin.vpin >= self.high_vpin:
            reasons.append(f"VPIN 过高(逆向选择): {self.vpin.vpin:.3f} >= {self.high_vpin:.3f}")

        return (len(reasons) == 0), reasons, metrics
