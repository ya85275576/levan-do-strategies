#!/usr/bin/env python3
"""
HighTempTation — 跨市场/跨期套利（高阶优化 #2）

功能:
  1. 桶分割套利 BucketPartitionArb  — 同一 (城市,日期) 所有桶 YES 价格之和应 ≈ 1
  2. 相邻桶平价 AdjacentBucketParity — 相邻温度桶的 Put-Call Parity / 单调性约束
  3. 期限结构 TermStructureMonitor   — 远期 vs 近期价格曲线, 检测倒挂/升水
  4. 多平台价差 MultiPlatformSpread  — 同一标的跨平台价差, 价差>成本 → 套利信号

用法:
  from highopt.arbitrage import ArbitrageScanner, BucketPartitionArb

  scanner = ArbitrageScanner(fee_pct=0.02, gas_usd=0.05)
  signals = scanner.scan_bucket_group(city="Tokyo", date="2025-04-15", buckets=[...])
  for sig in signals:
      print(sig)

理论依据:
  - 若桶 [a,b), [b,c), ... 覆盖整个温度空间, 则 Σ P(YES_i) = 1（无套利）
  - CDF 单调: P(YES[a,b]) ≤ P(YES[b,c])（相邻桶, 上界更高）
  - 期限结构: 远期温度概率价格 vs 到期时间的关系
"""
import logging
import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("highopt.arbitrage")

# ── 默认参数 ──
DEFAULT_FEE_PCT = 0.02        # 单边手续费 %（模拟市场 taker）
DEFAULT_GAS_USD = 0.05        # 单笔 Gas（$）
PARTITION_TOL = 0.01          # 桶和偏离 1 的容忍带
MONOTONE_TOL = 0.02           # 相邻桶单调性容忍带


@dataclass
class ArbitrageSignal:
    """套利信号"""
    arb_type: str               # bucket_partition / adjacent_parity / term_structure / cross_platform
    description: str
    expected_pnl: float         # 期望套利收益（$，含成本后净值）
    gross_edge: float           # 毛价差（概率单位）
    cost: float                 # 总成本（概率单位）
    instruments: List[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ════════════════════════════════════════════════════════════════
# 1. 桶分割套利（Σ YES = 1）
# ════════════════════════════════════════════════════════════════

class BucketPartitionArb:
    """
    桶分割套利。

    若一组桶完整覆盖温度空间（如 [<15],[15,20),[20,25),[25,30),[30,35),[35,40),[≥40]），
    则无论实际温度多少，恰好一个桶结算为 YES：
      Σ P(YES_i) = 1

    检查:
      - Σ YES > 1 + tol + cost → 全卖 YES（买所有 NO）锁定利润
      - Σ YES < 1 - tol - cost → 全买 YES（卖所有 NO）锁定利润

    注意: 需确认桶确实完整覆盖（用户提供 covers_all 标志或检查相邻性）。
    """

    def __init__(self, fee_pct: float = DEFAULT_FEE_PCT,
                 gas_usd: float = DEFAULT_GAS_USD,
                 tol: float = PARTITION_TOL):
        self.fee_pct = fee_pct
        self.gas_usd = gas_usd
        self.tol = tol

    def check(self, buckets: List[dict], min_qty: float = 1.0,
              covers_all: bool = True) -> List[ArbitrageSignal]:
        """
        :param buckets: [{"key": "T<15", "yes": 0.05, "no": 0.95, "qty": 100}, ...]
        :param covers_all: 桶是否完整覆盖空间（不完整时套利不成立）
        :param min_qty: 每个桶最小可交易量
        """
        if not buckets or not covers_all:
            return []
        sum_yes = sum(b["yes"] for b in buckets)
        n = len(buckets)
        # 成本（概率单位）: 每桶双边手续费 + 每桶 Gas 折算
        cost_per_bucket = self.fee_pct / 100.0 * 2 + self.gas_usd / max(min_qty, 1e-9)
        total_cost = cost_per_bucket * n

        signals: List[ArbitrageSignal] = []
        if sum_yes > 1 + self.tol + total_cost:
            pnl = (sum_yes - 1 - total_cost) * min_qty
            signals.append(ArbitrageSignal(
                arb_type="bucket_partition",
                description=(f"ΣYES={sum_yes:.3f} > 1: 卖所有桶 YES（买全部 NO）"
                             f" 锁定 {(sum_yes-1)*100:.1f}¢/组"),
                expected_pnl=round(pnl, 4),
                gross_edge=round(sum_yes - 1, 4),
                cost=round(total_cost, 4),
                instruments=[b["key"] for b in buckets],
                meta={"sum_yes": sum_yes, "direction": "sell_yes_all",
                      "n_buckets": n, "cost_per_bucket": cost_per_bucket},
            ))
        elif sum_yes < 1 - self.tol - total_cost:
            pnl = (1 - sum_yes - total_cost) * min_qty
            signals.append(ArbitrageSignal(
                arb_type="bucket_partition",
                description=(f"ΣYES={sum_yes:.3f} < 1: 买所有桶 YES"
                             f" 锁定 {(1-sum_yes)*100:.1f}¢/组"),
                expected_pnl=round(pnl, 4),
                gross_edge=round(1 - sum_yes, 4),
                cost=round(total_cost, 4),
                instruments=[b["key"] for b in buckets],
                meta={"sum_yes": sum_yes, "direction": "buy_yes_all",
                      "n_buckets": n, "cost_per_bucket": cost_per_bucket},
            ))
        return signals


# ════════════════════════════════════════════════════════════════
# 2. 相邻桶 Put-Call Parity / 单调性
# ════════════════════════════════════════════════════════════════

class AdjacentBucketParity:
    """
    相邻桶 Put-Call Parity。

    概率公理约束（无需模型）:
      1. 每桶自洽: YES + NO ≈ 1 —— 超过成本带即可「卖贵边买便宜边」
      2. 合并桶可加性: 若 [a,b]、[b,c]、[a,c] 三个市场同时存在,
         YES[a,b] + YES[b,c] ≈ YES[a,c]（违反 → 三腿套利）

    注意: 相邻桶的 YES 价格本身不满足单调性（分布可在中间桶取峰），
    故不检查“低桶 yes > 高桶 yes”这类伪约束。
    """

    def __init__(self, fee_pct: float = DEFAULT_FEE_PCT,
                 gas_usd: float = DEFAULT_GAS_USD,
                 tol: float = MONOTONE_TOL):
        self.fee_pct = fee_pct
        self.gas_usd = gas_usd
        self.tol = tol

    def check(self, buckets: List[dict],
              min_qty: float = 1.0) -> List[ArbitrageSignal]:
        """
        检查每桶 put-call parity（YES + NO ≈ 1）。
        :param buckets: [{"key", "yes", "no", ...}, ...]
        """
        cost_each = self.fee_pct / 100.0 * 2 + self.gas_usd / max(min_qty, 1e-9)
        signals: List[ArbitrageSignal] = []
        for b in buckets:
            s = b["yes"] + b["no"]
            if abs(s - 1.0) > cost_each + self.tol:
                edge = abs(s - 1.0) - cost_each - self.tol
                signals.append(ArbitrageSignal(
                    arb_type="put_call_parity",
                    description=(f"{b['key']} YES+NO={s:.3f} ≠ 1"
                                 f"（净 {edge * 100:.1f}¢）"),
                    expected_pnl=round(edge * min_qty, 4),
                    gross_edge=round(abs(s - 1.0), 4),
                    cost=round(cost_each + self.tol, 4),
                    instruments=[b["key"]],
                    meta={"direction": "sell" if s > 1 else "buy",
                          "yes": b["yes"], "no": b["no"]},
                ))
        return signals

    def check_additive(self, bucket_lo: dict, bucket_hi: dict,
                       combined: dict,
                       min_qty: float = 1.0) -> List[ArbitrageSignal]:
        """
        合并桶可加性: YES[a,b] + YES[b,c] ≈ YES[a,c]。
        :param bucket_lo: 低桶 [a,b] 报价
        :param bucket_hi: 高桶 [b,c] 报价
        :param combined:  合并桶 [a,c] 报价
        """
        lhs = bucket_lo["yes"] + bucket_hi["yes"]
        rhs = combined["yes"]
        cost = self.fee_pct / 100.0 * 3 + self.gas_usd / max(min_qty, 1e-9)
        if abs(lhs - rhs) <= cost + self.tol:
            return []
        edge = abs(lhs - rhs) - cost - self.tol
        return [ArbitrageSignal(
            arb_type="additive_parity",
            description=(f"合并桶可加性: YES[{bucket_lo['key']}]+YES[{bucket_hi['key']}]="
                         f"{lhs:.3f} vs 合并 YES={rhs:.3f}（净 {edge * 100:.1f}¢）"),
            expected_pnl=round(edge * min_qty, 4),
            gross_edge=round(abs(lhs - rhs), 4),
            cost=round(cost + self.tol, 4),
            instruments=[bucket_lo["key"], bucket_hi["key"], combined["key"]],
            meta={"lhs": lhs, "rhs": rhs, "direction": "sell" if lhs > rhs else "buy"},
        )]


# ════════════════════════════════════════════════════════════════
# 3. 期限结构
# ════════════════════════════════════════════════════════════════

class TermStructureMonitor:
    """
    期限结构监控。

    对同一标的（如 "Tokyo 高温≥30°C"）的不同到期日（D+0..D+7）:
      - 计算期限曲线斜率: (p_long - p_short) / (T_long - T_short)
      - 正向期限结构（远期更贵）→ 天气模型预期升温 / 风险溢价
      - 倒挂（远期更便宜超过带）→ 反向信号: 远期市场被低估或近期被高估

    输出:
      - backwardation: 倒挂（近贵远贱）
      - contango:      升水（近贱远贵）
    """

    def __init__(self, band: float = 0.03,
                 fee_pct: float = DEFAULT_FEE_PCT):
        self.band = band
        self.fee_pct = fee_pct

    def check(self, quotes: List[dict]) -> List[ArbitrageSignal]:
        """
        :param quotes: [{"maturity": "2025-04-15", "days": 1, "yes": 0.30, "qty": 100}, ...]
          days = 距到期天数（越大越远期）
        """
        if len(quotes) < 2:
            return []
        q = sorted(quotes, key=lambda x: x["days"])
        shortest, longest = q[0], q[-1]
        dt = longest["days"] - shortest["days"]
        if dt <= 0:
            return []
        slope = (longest["yes"] - shortest["yes"]) / dt   # 每天概率变化
        signals: List[ArbitrageSignal] = []
        cost = self.fee_pct / 100.0 * 2

        if slope < -self.band:
            # 倒挂: 远期比近期便宜
            edge = -slope * dt - cost
            if edge > 0:
                signals.append(ArbitrageSignal(
                    arb_type="term_structure",
                    description=(f"期限倒挂: {shortest['maturity']} YES={shortest['yes']:.2f} "
                                 f"> {longest['maturity']} YES={longest['yes']:.2f} "
                                 f"斜率={slope:.4f}/d"),
                    expected_pnl=round(edge * min(shortest.get("qty", 1), longest.get("qty", 1)), 4),
                    gross_edge=round(-slope * dt, 4),
                    cost=round(cost, 4),
                    instruments=[shortest["maturity"], longest["maturity"]],
                    meta={"mode": "backwardation", "slope": slope,
                          "short_yes": shortest["yes"], "long_yes": longest["yes"]},
                ))
        elif slope > self.band:
            edge = slope * dt - cost
            if edge > 0:
                signals.append(ArbitrageSignal(
                    arb_type="term_structure",
                    description=(f"期限升水: 远期 {longest['maturity']} YES={longest['yes']:.2f} "
                                 f"> 近期 {shortest['maturity']} YES={shortest['yes']:.2f} "
                                 f"斜率={slope:.4f}/d"),
                    expected_pnl=round(edge * min(shortest.get("qty", 1), longest.get("qty", 1)), 4),
                    gross_edge=round(slope * dt, 4),
                    cost=round(cost, 4),
                    instruments=[shortest["maturity"], longest["maturity"]],
                    meta={"mode": "contango", "slope": slope,
                          "short_yes": shortest["yes"], "long_yes": longest["yes"]},
                ))
        return signals


# ════════════════════════════════════════════════════════════════
# 4. 多平台价差
# ════════════════════════════════════════════════════════════════

class MultiPlatformSpread:
    """
    多平台价差套利。

    同一标的（同一事件/同一桶）在多个平台报价:
      - 跨平台最优买价 = max(bid)，最优卖价 = min(ask)
      - 价差 = 最优卖价 - 最优买价
      - 价差 > 双边成本 → 在低价平台买入 + 高价平台卖出

    深度感知: 每个平台报价带深度，套利量受最小深度限制。
    """

    def __init__(self, fee_pct: float = DEFAULT_FEE_PCT,
                 gas_usd: float = DEFAULT_GAS_USD):
        self.fee_pct = fee_pct
        self.gas_usd = gas_usd

    def check(self, platform_quotes: Dict[str, dict]) -> List[ArbitrageSignal]:
        """
        :param platform_quotes: {"polymarket": {"bid": 0.40, "ask": 0.42, "depth": 500},
                                  "okx_events": {"bid": 0.38, "ask": 0.39, "depth": 300}, ...}
        """
        if len(platform_quotes) < 2:
            return []
        best_bid_platform = max(platform_quotes.items(),
                                key=lambda kv: kv[1]["bid"])[0]
        best_ask_platform = min(platform_quotes.items(),
                                key=lambda kv: kv[1]["ask"])[0]
        best_bid = platform_quotes[best_bid_platform]["bid"]
        best_ask = platform_quotes[best_ask_platform]["ask"]
        if best_bid <= best_ask:
            return []  # 无跨平台价差（需 卖价 > 买价 才有利可图）
        spread = best_bid - best_ask
        cost = self.fee_pct / 100.0 * 2 + self.gas_usd / max(
            min(platform_quotes[best_ask_platform]["depth"],
                platform_quotes[best_bid_platform]["depth"]), 1e-9)
        edge = spread - cost
        if edge <= 0:
            return []

        max_qty = min(platform_quotes[best_ask_platform]["depth"],
                      platform_quotes[best_bid_platform]["depth"])
        return [ArbitrageSignal(
            arb_type="cross_platform",
            description=(f"跨平台: {best_ask_platform} ask={best_ask:.3f} 买 → "
                         f"{best_bid_platform} bid={best_bid:.3f} 卖, "
                         f"价差 {spread*100:.1f}¢ > 成本 {cost*100:.1f}¢"),
            expected_pnl=round(edge * max_qty, 4),
            gross_edge=round(spread, 4),
            cost=round(cost, 4),
            instruments=[best_ask_platform, best_bid_platform],
            meta={"spread": spread, "max_qty": max_qty,
                  "buy_at": best_ask_platform, "sell_at": best_bid_platform},
        )]


# ════════════════════════════════════════════════════════════════
# 汇总扫描器
# ════════════════════════════════════════════════════════════════

class ArbitrageScanner:
    """
    套利扫描器 —— 一键运行全部套利检查。

    scan_bucket_group:  对一组桶运行 桶分割 + 相邻桶平价
    scan_term_structure: 对多到期日报价运行期限结构检查
    scan_cross_platform: 对多平台报价运行价差检查
    """

    def __init__(self, fee_pct: float = DEFAULT_FEE_PCT,
                 gas_usd: float = DEFAULT_GAS_USD):
        self.partition = BucketPartitionArb(fee_pct, gas_usd)
        self.parity = AdjacentBucketParity(fee_pct, gas_usd)
        self.term = TermStructureMonitor(fee_pct=fee_pct)
        self.platform = MultiPlatformSpread(fee_pct, gas_usd)

    def scan_bucket_group(self, buckets: List[dict],
                          sigma: Optional[float] = None,
                          covers_all: bool = True,
                          min_qty: float = 1.0,
                          combined: Optional[dict] = None) -> List[ArbitrageSignal]:
        """
        对一组桶运行 桶分割 + 每桶 put-call parity 检查。
        :param combined: 可选的合并桶报价，用于可加性检查
        """
        signals = self.partition.check(buckets, min_qty=min_qty,
                                       covers_all=covers_all)
        signals += self.parity.check(buckets, min_qty=min_qty)
        if combined:
            sorted_b = sorted(buckets, key=lambda b: b.get("lower", 0))
            for i in range(len(sorted_b) - 1):
                signals += self.parity.check_additive(
                    sorted_b[i], sorted_b[i + 1], combined, min_qty=min_qty)
        return signals

    def scan_term_structure(self, quotes: List[dict]) -> List[ArbitrageSignal]:
        return self.term.check(quotes)

    def scan_cross_platform(self, quotes: Dict[str, dict]) -> List[ArbitrageSignal]:
        return self.platform.check(quotes)

    def scan_all(self, bucket_group: Optional[List[dict]] = None,
                 sigma: Optional[float] = None,
                 term_quotes: Optional[List[dict]] = None,
                 platform_quotes: Optional[Dict[str, dict]] = None) -> List[ArbitrageSignal]:
        signals: List[ArbitrageSignal] = []
        if bucket_group:
            signals += self.scan_bucket_group(bucket_group, sigma=sigma)
        if term_quotes:
            signals += self.scan_term_structure(term_quotes)
        if platform_quotes:
            signals += self.scan_cross_platform(platform_quotes)
        return signals
