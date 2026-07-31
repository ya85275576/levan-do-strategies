#!/usr/bin/env python3
"""
HighTempTation — Walk-Forward 回测（高阶优化 #5）

功能:
  1. PointInTimeLoader   — 严格 Point-in-Time 数据切片: 时刻 t 只用 ts ≤ t 的数据（无前视偏差）
  2. WalkForwardBacktester — 滚动训练/验证/测试折叠, 每折独立拟合+评估, 输出夏普/Calmar 稳定性
  3. CostSensitivityAnalyzer — 交易成本敏感性: 扫描手续费×滑点×Gas, 找盈亏平衡成本
  4. StabilityMetrics    — 滚动夏普/Calmar + 稳定性统计（均值/标准差/最差折叠）

用法:
  from highopt.walk_forward import (
      PointInTimeLoader, WalkForwardBacktester, CostSensitivityAnalyzer, StabilityMetrics,
  )

  loader = PointInTimeLoader(events)          # events: [{ts, ...}, ...]
  snap = loader.state_at(t)                   # 只用 ts <= t 的数据
  bt = WalkForwardBacktester(model_factory=make_model, folds=6)
  report = bt.run(records)                    # 每条 record 带 ts 与标签
  # → report.stability: {sharpe_mean, sharpe_std, calmar_mean, ..., pos_fold_ratio}

说明:
  - 本模块自带一个极简「桶边缘」策略跑分（复用 v6 的高斯概率思想），
    也可传入自定义 model_factory 与 strategy 做完整复现。
"""
import logging
import math
import random
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("highopt.walk_forward")


# ════════════════════════════════════════════════════════════════
# 1. Point-in-Time 数据加载
# ════════════════════════════════════════════════════════════════

class PointInTimeLoader:
    """
    Point-in-Time 数据切片器。

    保证: 任何时刻 t 的视图只包含 ts ≤ t 的事件 —— 杜绝前视偏差
    （如用未来实况温度校准今天的预报、用未来价格回填深度等）。

    events: [{ts: float, ...}, ...]（ts 单位统一，如 epoch 秒）
    """

    def __init__(self, events: List[dict], ts_key: str = "ts"):
        self.events = sorted(events, key=lambda e: e.get(ts_key, 0))
        self.ts_key = ts_key

    def state_at(self, t: float) -> List[dict]:
        """返回 ts ≤ t 的全部事件（Point-in-Time 视图）"""
        # 二分查找边界（events 已按 ts 升序）
        lo, hi = 0, len(self.events)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.events[mid].get(self.ts_key, 0) <= t:
                lo = mid + 1
            else:
                hi = mid
        return self.events[:lo]

    def window(self, start: float, end: float) -> List[dict]:
        """返回 [start, end) 区间内事件"""
        return [e for e in self.events
                if start <= e.get(self.ts_key, 0) < end]

    def split_folds(self, n_folds: int,
                    train_frac: float = 0.6, val_frac: float = 0.2,
                    gap: float = 0.0) -> List[Tuple[List[dict], List[dict], List[dict]]]:
        """
        滚动折叠切分（训练/验证/测试）。

        每个折叠 k: 训练 = [t0, t_k), 验证 = [t_k, t_k+val), 测试 = [t_k+val+gap, ...)
        gap 用于消除相邻样本自相关（预测市场常用）。
        :returns: [(train_events, val_events, test_events), ...]
        """
        total = len(self.events)
        if total < n_folds * 3:
            logger.warning(f"样本不足切 {n_folds} 折 ({total})")
            n_folds = max(1, total // 3)

        fold_size = total // n_folds
        folds = []
        for k in range(1, n_folds):
            train_end = k * fold_size
            if train_end <= 0 or train_end >= total - 2:
                continue
            train = self.events[:train_end]
            val = self.events[train_end: train_end + int(fold_size * val_frac / 0.4)]
            test_start = train_end + int(fold_size * val_frac / 0.4) + int(gap)
            test = self.events[test_start:] if test_start < total else []
            if train and val and test:
                folds.append((train, val, test))
        if not folds:
            # 退化: 单折叠
            half = total // 2
            folds.append((self.events[:half], self.events[half:], self.events[half:]))
        return folds

    def assert_no_lookahead(self, t: float, future_keys: List[str]) -> bool:
        """
        前视偏差断言: state_at(t) 中任何事件不得携带 future_keys 未来信息。
        用于测试数据管线。
        """
        snap = self.state_at(t)
        for e in snap:
            for k in future_keys:
                if e.get(k) is not None:
                    logger.error(f"前视偏差: 事件 ts={e.get(self.ts_key)} 携带未来字段 {k}")
                    return False
        return True


# ════════════════════════════════════════════════════════════════
# 2. 极简策略引擎（Walk-Forward 演示用）
# ════════════════════════════════════════════════════════════════

def _gaussian_cdf(x: float) -> float:
    try:
        from scipy.stats import norm
        return float(norm.cdf(x))
    except ImportError:
        if x < -8: return 0.0
        if x > 8: return 1.0
        a = [0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429]
        p = 0.3275911
        s = 1.0 if x >= 0 else -1.0
        t = 1.0 / (1.0 + p * abs(x))
        y = 1.0 - (((((a[4] * t + a[3]) * t + a[2]) * t + a[1]) * t + a[0]) * t) * math.exp(-x * x / 2)
        return y if s > 0 else 1.0 - y


def bucket_prob(mu: float, sigma: float, lower: float, upper: float) -> float:
    if sigma <= 0:
        return 1.0 if lower <= mu < upper else 0.0
    return max(0.0, min(1.0, _gaussian_cdf((upper - mu) / sigma)
                        - _gaussian_cdf((lower - mu) / sigma)))


class SimpleBucketStrategy:
    """
    极简桶边缘策略（与 v6 同思想, 简化）:
      p_model = bucket_prob(mu, sigma, lo, hi) （mu 可被残差模型修正）
      edge = |p_model - 0.5|
      若 edge > threshold 且市场价在 [lo_p, hi_p] → 开仓 NO（1 张）
      结算: 收盘价 0/1 结算（0.02 手续费）
    """

    def __init__(self, min_edge: float = 0.15, price_lo: float = 0.25,
                 price_hi: float = 0.75, fee_pct: float = 0.02,
                 mu_corrector: Optional[Callable[[dict], float]] = None):
        self.min_edge = min_edge
        self.price_lo = price_lo
        self.price_hi = price_hi
        self.fee_pct = fee_pct
        self.mu_corrector = mu_corrector

    def run(self, records: List[dict]) -> List[dict]:
        """对记录序列跑策略, 返回成交列表 [{pnl, ...}]"""
        trades = []
        for r in records:
            mu = r.get("mu", 0.0)
            if self.mu_corrector:
                mu += self.mu_corrector(r)
            p_model = bucket_prob(mu, r.get("sigma", 2.0),
                                  r.get("bucket_lower", 0), r.get("bucket_upper", 30))
            edge = abs(p_model - 0.5)
            p_market = r.get("market_price", 0.5)
            if edge < self.min_edge:
                continue
            if not (self.price_lo <= p_market <= self.price_hi):
                continue
            # 买 NO
            entry = p_market
            settled = r.get("settle", 1.0)  # NO 结算价值
            gross = (entry - (1.0 - settled))  # NO 盈亏
            # 简化: settled=1 → NO 归零损失 entry; settled=0 → NO 归 1 盈利 1-entry
            if settled >= 0.5:
                pnl = -entry
            else:
                pnl = (1.0 - entry)
            pnl -= self.fee_pct / 100.0
            trades.append({"pnl": pnl, "entry": entry, "edge": edge,
                           "mu": mu, "sigma": r.get("sigma", 2.0)})
        return trades


# ════════════════════════════════════════════════════════════════
# 3. 稳定性指标
# ════════════════════════════════════════════════════════════════

class StabilityMetrics:
    """
    夏普/Calmar 稳定性指标。

    rolling_sharpe(returns, window): 滚动年化夏普
    rolling_calmar(returns, window): 滚动年化收益/最大回撤
    summarize(returns): 稳定性摘要 {mean, std, min, worst_window, pos_ratio, ann_return, max_dd}
    """

    TRADING_DAYS = 365

    @staticmethod
    def _returns_from_equity(equity: List[float]) -> List[float]:
        rets = []
        for i in range(1, len(equity)):
            prev = equity[i - 1]
            if prev > 0:
                rets.append(equity[i] / prev - 1.0)
        return rets

    @staticmethod
    def sharpe(returns: List[float], periods_per_year: float = TRADING_DAYS) -> float:
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        if var <= 0:
            return 0.0
        return mean / math.sqrt(var) * math.sqrt(periods_per_year)

    @staticmethod
    def max_drawdown(equity: List[float]) -> float:
        if not equity:
            return 0.0
        peak = equity[0]
        max_dd = 0.0
        for v in equity:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    @staticmethod
    def calmar(equity: List[float], periods_per_year: float = TRADING_DAYS) -> float:
        if len(equity) < 2:
            return 0.0
        rets = StabilityMetrics._returns_from_equity(equity)
        ann_ret = (equity[-1] / equity[0]) ** (periods_per_year / max(len(rets), 1)) - 1.0 \
            if equity[0] > 0 else 0.0
        mdd = StabilityMetrics.max_drawdown(equity)
        return ann_ret / mdd if mdd > 0 else (ann_ret if ann_ret > 0 else 0.0)

    @staticmethod
    def rolling_sharpe(returns: List[float], window: int = 30) -> List[float]:
        out = []
        for i in range(window, len(returns) + 1):
            out.append(StabilityMetrics.sharpe(returns[i - window:i]))
        return out

    @staticmethod
    def rolling_calmar(equity: List[float], window: int = 30) -> List[float]:
        out = []
        for i in range(window, len(equity) + 1):
            out.append(StabilityMetrics.calmar(equity[i - window:i]))
        return out

    @staticmethod
    def summarize(returns: List[float]) -> dict:
        if not returns:
            return {"n": 0}
        n = len(returns)
        mean = sum(returns) / n
        var = sum((r - mean) ** 2 for r in returns) / n
        std = math.sqrt(var) if var > 0 else 0.0
        pos = sum(1 for r in returns if r > 0)
        equity = [1.0]
        for r in returns:
            equity.append(equity[-1] * (1.0 + r))
        mdd = StabilityMetrics.max_drawdown(equity)
        ann = mean * StabilityMetrics.TRADING_DAYS
        return {
            "n": n,
            "mean_daily": round(mean, 6),
            "std_daily": round(std, 6),
            "ann_return": round(ann, 4),
            "sharpe": round(StabilityMetrics.sharpe(returns), 3),
            "calmar": round(StabilityMetrics.calmar(equity), 3),
            "max_drawdown": round(mdd, 4),
            "win_days_ratio": round(pos / n, 3),
            "worst_day": round(min(returns), 6),
        }


# ════════════════════════════════════════════════════════════════
# 4. Walk-Forward 回测器
# ════════════════════════════════════════════════════════════════

@dataclass
class FoldReport:
    fold: int
    train_n: int
    test_n: int
    trades: int
    total_pnl: float
    win_rate: float
    sharpe: float
    calmar: float
    max_drawdown: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WalkForwardReport:
    folds: List[FoldReport] = field(default_factory=list)
    stability: dict = field(default_factory=dict)
    cost_sensitivity: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"folds": [f.to_dict() for f in self.folds],
                "stability": self.stability,
                "cost_sensitivity": self.cost_sensitivity,
                "params": self.params}


class WalkForwardBacktester:
    """
    Walk-Forward 回测器。

    每折流程:
      1. 训练事件 → model_factory(train) 拟合模型（如 ML 残差学习器）
      2. 验证事件 → 调参/早停（可选）
      3. 测试事件 → 跑策略, 计算该折指标
    最终输出稳定性: 各折夏普/Calmar 的均值/标准差/最差/正收益折占比。
    """

    def __init__(self, n_folds: int = 6, train_frac: float = 0.6,
                 val_frac: float = 0.2, gap: float = 0.0,
                 model_factory: Optional[Callable[[List[dict]], Callable[[dict], float]]] = None,
                 strategy_factory: Optional[Callable[[], SimpleBucketStrategy]] = None,
                 seed: int = 42):
        """
        :param model_factory: train_events → mu_corrector(row)->float（残差修正函数）
        :param strategy_factory: () → 策略实例（需有 run(records)->trades）
        """
        self.n_folds = n_folds
        self.train_frac = train_frac
        self.val_frac = val_frac
        self.gap = gap
        self.model_factory = model_factory
        self.strategy_factory = strategy_factory or (lambda: SimpleBucketStrategy())

    def run(self, records: List[dict]) -> WalkForwardReport:
        """records: 按 ts 升序的含标签记录（mu/sigma/bucket/market_price/settle/actual）"""
        if len(records) < 20:
            logger.warning("记录太少, 跳过回测")
            return WalkForwardReport()

        loader = PointInTimeLoader(records)
        folds_raw = loader.split_folds(self.n_folds, self.train_frac, self.val_frac, self.gap)
        report = WalkForwardReport()
        report.params = {"n_folds": len(folds_raw), "train_frac": self.train_frac,
                         "val_frac": self.val_frac, "gap": self.gap}

        for k, (train_ev, val_ev, test_ev) in enumerate(folds_raw):
            # 训练模型（残差学习器等）
            mu_corrector = None
            if self.model_factory is not None:
                try:
                    mu_corrector = self.model_factory(train_ev)
                except Exception as e:
                    logger.warning(f"fold {k} 模型训练失败: {e}")

            strategy = self.strategy_factory()
            if isinstance(strategy, SimpleBucketStrategy) and mu_corrector:
                strategy.mu_corrector = mu_corrector

            trades = strategy.run(test_ev)
            pnls = [t["pnl"] for t in trades]
            n = len(pnls)
            if n == 0:
                report.folds.append(FoldReport(k, len(train_ev), len(test_ev),
                                               0, 0.0, 0.0, 0.0, 0.0, 0.0))
                continue
            equity = [1.0]
            for p in pnls:
                equity.append(equity[-1] * (1.0 + p / 100.0))
            total_pnl = sum(pnls)
            win_rate = sum(1 for p in pnls if p > 0) / n * 100.0
            sharpe = StabilityMetrics.sharpe(pnls)
            calmar = StabilityMetrics.calmar(equity)
            mdd = StabilityMetrics.max_drawdown(equity)
            report.folds.append(FoldReport(k, len(train_ev), len(test_ev), n,
                                           round(total_pnl, 2), round(win_rate, 1),
                                           round(sharpe, 3), round(calmar, 3),
                                           round(mdd, 4)))

        report.stability = self._stability(report.folds)
        return report

    @staticmethod
    def _stability(folds: List[FoldReport]) -> dict:
        if not folds:
            return {}
        sharpes = [f.sharpe for f in folds]
        calmars = [f.calmar for f in folds]
        pnls = [f.total_pnl for f in folds]

        def stats(vals):
            mean = sum(vals) / len(vals)
            var = sum((x - mean) ** 2 for x in vals) / len(vals)
            return {"mean": round(mean, 3), "std": round(math.sqrt(var), 3),
                    "min": round(min(vals), 3), "max": round(max(vals), 3)}

        return {
            "sharpe": stats(sharpes),
            "calmar": stats(calmars),
            "pnl": stats(pnls),
            "pos_fold_ratio": round(sum(1 for p in pnls if p > 0) / len(pnls), 3),
            "sharpe_positive_ratio": round(sum(1 for s in sharpes if s > 0) / len(sharpes), 3),
            "best_fold": int(max(range(len(pnls)), key=lambda i: pnls[i])),
            "worst_fold": int(min(range(len(pnls)), key=lambda i: pnls[i])),
            "verdict": ("STABLE" if (stats(sharpes)["mean"] > 0
                                     and stats(sharpes)["std"] < abs(stats(sharpes)["mean"]) + 0.5
                                     and sum(1 for p in pnls if p > 0) >= len(pnls) // 2)
                        else "UNSTABLE"),
        }


# ════════════════════════════════════════════════════════════════
# 5. 交易成本敏感性分析
# ════════════════════════════════════════════════════════════════

class CostSensitivityAnalyzer:
    """
    交易成本敏感性分析。

    扫描 (手续费, 滑点, Gas) 组合, 输出净 PnL / 夏普网格,
    并求盈亏平衡成本（净 PnL 归零点）。

    用法:
      analyzer = CostSensitivityAnalyzer()
      grid = analyzer.analyze(records, fee_pcts=[0.0,0.02,0.1,0.3],
                              slippage_pcts=[0.0,0.05,0.2], gas_usd=[0.0,0.05])
      # grid["breakeven_fee_pct"]  → 盈亏平衡手续费
    """

    def __init__(self, strategy_factory: Optional[Callable[[], SimpleBucketStrategy]] = None):
        self.strategy_factory = strategy_factory or (lambda: SimpleBucketStrategy())

    def analyze(self, records: List[dict],
                fee_pcts: Optional[List[float]] = None,
                slippage_pcts: Optional[List[float]] = None,
                gas_usd: Optional[List[float]] = None) -> dict:
        fee_pcts = fee_pcts or [0.0, 0.02, 0.05, 0.1, 0.2, 0.3]
        slippage_pcts = slippage_pcts or [0.0, 0.05, 0.1, 0.2]
        gas_usd = gas_usd or [0.0, 0.05, 0.1]

        grid = []
        for fee in fee_pcts:
            for slip in slippage_pcts:
                for gas in gas_usd:
                    strat = self.strategy_factory()
                    strat.fee_pct = fee
                    trades = strat.run([{**r, "market_price": r["market_price"] * (1 + slip / 100.0)}
                                        for r in records])
                    pnls = [t["pnl"] for t in trades]
                    total = sum(pnls)
                    n = len(pnls)
                    # 折算 Gas（假设每笔 $1 仓位）
                    total -= n * gas
                    sharpe = StabilityMetrics.sharpe(pnls) if n else 0.0
                    grid.append({"fee_pct": fee, "slippage_pct": slip, "gas_usd": gas,
                                 "trades": n, "total_pnl": round(total, 2),
                                 "sharpe": round(sharpe, 3)})

        # 盈亏平衡: 固定滑点/Gas 为 0 时，PnL 归零的手续费（线性插值）
        breakeven = self._breakeven(grid)
        return {"grid": grid, "breakeven_fee_pct": breakeven}

    @staticmethod
    def _breakeven(grid: List[dict]) -> Optional[float]:
        base = [g for g in grid if g["slippage_pct"] == 0 and g["gas_usd"] == 0]
        base.sort(key=lambda g: g["fee_pct"])
        if not base:
            return None
        if base[0]["total_pnl"] <= 0:
            return 0.0  # 零手续费已亏损 → 盈亏平衡成本为 0
        prev = base[0]
        for g in base[1:]:
            if g["total_pnl"] <= 0:
                # 线性插值
                if prev["total_pnl"] == g["total_pnl"]:
                    return g["fee_pct"]
                frac = prev["total_pnl"] / (prev["total_pnl"] - g["total_pnl"])
                return round(prev["fee_pct"] + (g["fee_pct"] - prev["fee_pct"]) * frac, 4)
            prev = g
        return None  # 全为正 → 无盈亏平衡点
