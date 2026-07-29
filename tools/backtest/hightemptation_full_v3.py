#!/usr/bin/env python3
"""
HighTempTation 天气校准套利 - v3 完整版（P0+P1 全部优化）

P0:
  - 动态滑点 get_dynamic_slippage
  - 滚动偏差校准 RollingBiasCalibrator
  - 结算时间过滤（距结算<6h不开仓）

P1 新增:
  1. 流动性深度过滤 — MIN_DEPTH=200, MAX_IMPACT_RATIO=0.2
  2. 城市相关性风控 — compute_correlation_matrix + get_portfolio_risk
  3. 动态边缘阈值 — get_dynamic_edge（基于最近波动率，VOLATILITY_WINDOW=20）

用法:
  python tools/backtest/hightemptation_full_v3.py
  python tools/backtest/hightemptation_full_v3.py --mode multi
  python tools/backtest/hightemptation_full_v3.py --real-data --data-dir ./data
"""

import argparse
import csv
import json
import logging
import math
import os
import random
import sys
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

try:
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("hightemptation_v3")


# ════════════════════════════════════════════════════════════════
# 配置区
# ════════════════════════════════════════════════════════════════

MIN_EDGE = 0.20
TP_PCT = 9.0
SL_PCT = 6.5
TRAILING_ACTIVATE = 5.0
TRAILING_DRAWDOWN = 3.0
PRICE_LOW = 0.28
PRICE_HIGH = 0.72
MIN_PROB_EDGE = 0.12
ALLOWED_SIDES = ["NO"]
TIME_STOP_HOURS = 24

POSITION_SIZE_USD = 1.0
MAX_POSITIONS = 50
INITIAL_CAPITAL = 5000.0
COMMISSION_PCT = 0.02

# P0
ROLLING_WINDOW = 15
MIN_HOURS_TO_EXPIRY = 6
BASE_SLIPPAGE = 0.0005
MAX_SLIPPAGE = 0.005
SLIPPAGE_EXTREME_PCT = 0.15

# P1: 流动性深度
MIN_DEPTH = 200               # 最小订单簿深度（张数）
MAX_IMPACT_RATIO = 0.2        # 最大冲击比（仓位/深度）

# P1: 组合风控
MAX_PORTFOLIO_RISK = 1500     # 相关性调整后最大总敞口

# P1: 动态边缘
VOLATILITY_WINDOW = 20        # 计算波动率的窗口（K线数）

USE_REAL_DATA = False
DATA_DIR = "./data"

RSI_LENGTH = 7
RSI_TOP = 45
RSI_BOT = 10


# ════════════════════════════════════════════════════════════════
# P0 函数
# ════════════════════════════════════════════════════════════════

def get_dynamic_slippage(price: float, min_price: float, max_price: float) -> float:
    if max_price <= min_price:
        return BASE_SLIPPAGE
    norm = (price - min_price) / (max_price - min_price)
    if norm <= SLIPPAGE_EXTREME_PCT:
        factor = 1.0 + (SLIPPAGE_EXTREME_PCT - norm) / SLIPPAGE_EXTREME_PCT * 5.0
    elif norm >= 1.0 - SLIPPAGE_EXTREME_PCT:
        factor = 1.0 + (norm - (1.0 - SLIPPAGE_EXTREME_PCT)) / SLIPPAGE_EXTREME_PCT * 5.0
    else:
        factor = 1.0
    return min(BASE_SLIPPAGE * factor, MAX_SLIPPAGE)


class RollingBiasCalibrator:
    def __init__(self, window_days: int = 15):
        self.window_days = window_days
        self._errors: deque = deque(maxlen=window_days)

    def add_observation(self, forecast_mu: float, actual_high: float, date: Optional[str] = None):
        error = forecast_mu - actual_high
        if date:
            for i, (d, _) in enumerate(self._errors):
                if d == date:
                    self._errors[i] = (date, error); return
        self._errors.append((date or "", error))

    def get_bias(self) -> float:
        if not self._errors:
            return 0.0
        return sum(e for _, e in self._errors) / len(self._errors)

    def calibrate(self, mu: float, sigma: float) -> Tuple[float, float]:
        bias = self.get_bias()
        adj_mu = mu - bias
        if len(self._errors) >= 2:
            errs = [e for _, e in self._errors]
            m = sum(errs) / len(errs)
            b_std = math.sqrt(sum((e-m)**2 for e in errs) / len(errs))
            adj_sigma = math.sqrt(sigma**2 + b_std**2)
        else:
            adj_sigma = sigma
        return adj_mu, adj_sigma

    def reset(self):
        self._errors.clear()


def hours_to_expiry(ct_ms: int, et_ms: int) -> float:
    return (et_ms - ct_ms) / 3600000.0


def can_open_position(ct_ms: int, et_ms: Optional[int], min_h: float = MIN_HOURS_TO_EXPIRY) -> Tuple[bool, str]:
    if et_ms is None:
        return True, ""
    h = hours_to_expiry(ct_ms, et_ms)
    if h < 0:
        return False, f"已过结算 ({h:.1f}h)"
    if h < min_h:
        return False, f"距结算<{min_h}h ({h:.1f}h)"
    return True, ""


# ════════════════════════════════════════════════════════════════
# P1 函数
# ════════════════════════════════════════════════════════════════

def get_dynamic_edge(prices: List[float], window: int = VOLATILITY_WINDOW) -> float:
    """
    基于最近价格波动率动态调整 MIN_EDGE。
    波动率大 → 要求更高 edge（噪音多），波动率小 → 可接受更低 edge。

    :param prices: 最近价格列表（按时间升序）
    :param window: 计算波动率的窗口
    :returns: 动态 edge 阈值 (0.0~1.0)
    """
    if len(prices) < window + 1:
        return MIN_EDGE

    recent = prices[-window:]
    # 计算收益率的标准差作为波动率
    returns = [(recent[i] - recent[i-1]) / recent[i-1] for i in range(1, len(recent))]
    if not returns:
        return MIN_EDGE

    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r)**2 for r in returns) / len(returns)
    vol = math.sqrt(variance) if variance > 0 else 0.001

    # 基准波动率 0.02 → edge=0.20；vol 翻倍 → edge 提高 50%
    BASE_VOL = 0.02
    dynamic_edge = MIN_EDGE * (1.0 + max(0, (vol - BASE_VOL) / BASE_VOL * 0.5))
    return min(dynamic_edge, 0.50)  # 上限 0.50


def compute_correlation_matrix(price_series_by_city: Dict[str, List[float]]) -> Tuple[List[str], np.ndarray]:
    """
    计算城市间价格相关性矩阵。

    :param price_series_by_city: {city: [price1, price2, ...]}
    :returns: (city_names, corr_matrix) 其中 corr_matrix 为 NxN numpy 数组
    """
    if not HAS_MPL:
        # 无 numpy 时，返回单位矩阵（假设全部独立）
        cities = list(price_series_by_city.keys())
        n = len(cities)
        return cities, np.eye(n) if HAS_MPL else [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

    cities = list(price_series_by_city.keys())
    n = len(cities)
    if n == 0:
        return [], np.array([])
    if n == 1:
        return cities, np.array([[1.0]])

    # 对齐长度（取最短序列）
    min_len = min(len(series) for series in price_series_by_city.values())
    if min_len < 5:
        return cities, np.eye(n)

    aligned = [np.array(price_series_by_city[c][-min_len:]) for c in cities]
    corr = np.corrcoef(aligned)
    # 处理 NaN（全零序列导致）
    corr = np.nan_to_num(corr, nan=0.0)
    return cities, corr


def get_portfolio_risk(positions_value: Dict[str, float], corr_matrix: np.ndarray,
                       cities: List[str]) -> Tuple[float, float]:
    """
    计算组合风险（相关性调整后总敞口 + 集中度分数）。

    :param positions_value: {city: total_dollar_exposure}
    :param corr_matrix: NxN 相关性矩阵
    :param cities: 城市列表（与 corr_matrix 顺序一致）
    :returns: (adjusted_exposure, concentration_score)
        adjusted_exposure = sum_i sum_j w_i * w_j * corr_ij * total_capital
        concentration_score = Herfindahl 指数 (0~1)
    """
    n = len(cities)
    if n == 0 or corr_matrix.size == 0:
        return 0.0, 0.0

    weights = np.array([positions_value.get(c, 0.0) for c in cities]) if HAS_MPL else [
        positions_value.get(c, 0.0) for c in cities
    ]
    total = sum(weights) if not HAS_MPL else float(np.sum(weights))
    if total == 0:
        return 0.0, 0.0

    if HAS_MPL:
        w = weights / total if total > 0 else weights
        # adjusted_exposure = sqrt(w^T * Corr * w) * total
        risk = float(np.sqrt(np.dot(w, np.dot(corr_matrix, w))) * total)
        # Herfindahl
        conc = float(np.sum((w / np.sum(w))**2)) if np.sum(w) > 0 else 0.0
    else:
        w = [v / total for v in weights]
        risk_val = 0.0
        for i in range(n):
            for j in range(n):
                risk_val += w[i] * w[j] * corr_matrix[i][j]
        risk = math.sqrt(max(risk_val, 0)) * total if risk_val > 0 else total
        w_sum = sum(w)
        conc = sum((v / w_sum)**2 for v in w) if w_sum > 0 else 0.0

    return risk, conc


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════

@dataclass
class CandleData:
    timestamp: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    depth: float = 1000.0   # v3: 订单簿深度（默认充足）
    city: str = ""           # v3: 城市标识（用于相关性计算）


@dataclass
class ForecastRecord:
    datetime: str = ""; city: str = ""; mu: float = 0.0; sigma: float = 0.0

@dataclass
class MarketRecord:
    datetime: str = ""; city: str = ""; bucket: str = ""
    yes_price: float = 0.0; no_price: float = 0.0; depth: float = 1000.0

@dataclass
class ActualRecord:
    date: str = ""; city: str = ""; actual_high: float = 0.0


@dataclass
class BacktestTrade:
    symbol: str = ""; side: str = ""; entry_time: str = ""; exit_time: str = ""
    entry_price: float = 0.0; exit_price: float = 0.0; size: float = 0.0
    pnl: float = 0.0; pnl_pct: float = 0.0; leverage: int = 1
    exit_reason: str = ""
    slippage: float = 0.0; bias_adjustment: float = 0.0
    hours_to_expiry: float = 0.0; bars_held: int = 0; max_pnl_pct: float = 0.0
    # v3 新增
    dynamic_edge: float = 0.0; depth_at_entry: float = 0.0
    impact_ratio: float = 0.0; portfolio_risk: float = 0.0


@dataclass
class BacktestResult:
    symbol: str = ""; params: dict = field(default_factory=dict)
    total_trades: int = 0; winning_trades: int = 0; losing_trades: int = 0
    win_rate: float = 0.0; total_pnl: float = 0.0; total_pnl_pct: float = 0.0
    avg_win: float = 0.0; avg_loss: float = 0.0; max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0; profit_factor: float = 0.0; sharpe_ratio: float = 0.0
    avg_bars_held: float = 0.0; long_trades: int = 0; short_trades: int = 0
    tp1_count: int = 0; tp2_count: int = 0; tp3_count: int = 0
    sl_count: int = 0; manual_count: int = 0
    equity_curve: List[float] = field(default_factory=list)
    trades: List[dict] = field(default_factory=list)
    # P0 统计
    avg_slippage: float = 0.0; avg_bias_adjustment: float = 0.0
    settlement_filtered: int = 0
    # P1 统计
    depth_filtered: int = 0; risk_filtered: int = 0; avg_dynamic_edge: float = 0.0
    avg_portfolio_risk: float = 0.0; max_portfolio_risk: float = 0.0

    def to_dict(self):
        d = asdict(self)
        if len(d["trades"]) > 100: d["trades"] = d["trades"][-100:]
        return d


# ════════════════════════════════════════════════════════════════
# 模拟数据（v3 含 depth 列和 city 字段）
# ════════════════════════════════════════════════════════════════

def generate_mock_data(
    days: int = 90, timeframe_min: int = 15,
    base_price: float = 50.0, volatility: float = 0.02,
    trend: float = 0.0001, seed: int = 42,
    cities: Optional[List[str]] = None,
    multi_city: bool = False,
) -> List[CandleData]:
    random.seed(seed)
    if HAS_MPL: np.random.seed(seed)
    if cities is None:
        cities = ["NYC", "LON", "TKY"] if multi_city else ["DFT"]
    bars_per_day = 24 * 60 // timeframe_min
    total_bars = days * bars_per_day
    now = int(time.time() * 1000)
    start_ts = now - days * 86400 * 1000
    bar_ms = timeframe_min * 60 * 1000
    candles = []
    # 每个城市一条独立的价格序列
    prices = {c: base_price * (1 + i * 0.1) for i, c in enumerate(cities)}
    for i in range(total_bars):
        ts = start_ts + i * bar_ms
        for city in cities:
            p = prices[city]
            ret = random.gauss(trend / bars_per_day, volatility / math.sqrt(bars_per_day))
            c = p * (1 + ret)
            depth = max(50, random.gauss(500, 200))  # 模拟深度
            candles.append(CandleData(
                timestamp=ts, open=round(p, 2), high=round(max(p, c) * 1.01, 2),
                low=round(min(p, c) * 0.99, 2), close=round(c, 2),
                volume=round(abs(random.gauss(1000, 300)), 2),
                depth=round(depth, 2), city=city,
            ))
            prices[city] = c
    logger.info(f"模拟: {len(candles)} K线, {len(cities)}城市, {days}天, 起${base_price:.0f}")
    return candles


# ════════════════════════════════════════════════════════════════
# 真实 CSV 加载（v3 含可选 depth 列）
# ════════════════════════════════════════════════════════════════

def load_real_data(data_dir: str = DATA_DIR) -> Tuple[List[CandleData], dict]:
    forecasts, markets, actuals, cities = [], [], [], set()
    fpath = os.path.join(data_dir, "forecast.csv")
    if os.path.exists(fpath):
        with open(fpath) as f:
            for row in csv.DictReader(f):
                forecasts.append(ForecastRecord(row.get("datetime",""), row.get("city",""),
                                                 float(row.get("mu",0)), float(row.get("sigma",0))))
                cities.add(forecasts[-1].city)

    fpath = os.path.join(data_dir, "market.csv")
    if os.path.exists(fpath):
        with open(fpath) as f:
            for row in csv.DictReader(f):
                mr = MarketRecord(datetime=row.get("datetime",""), city=row.get("city",""),
                                  bucket=row.get("bucket",""),
                                  yes_price=float(row.get("yes_price",0)),
                                  no_price=float(row.get("no_price",0)),
                                  depth=float(row.get("depth", 1000)))
                markets.append(mr); cities.add(mr.city)

    fpath = os.path.join(data_dir, "actual.csv")
    if os.path.exists(fpath):
        with open(fpath) as f:
            for row in csv.DictReader(f):
                actuals.append(ActualRecord(row.get("date",""), row.get("city",""),
                                            float(row.get("actual_high",0))))

    candles = []
    if markets:
        markets.sort(key=lambda m: m.datetime)
        grp = defaultdict(list)
        for m in markets:
            try:
                dt = datetime.fromisoformat(m.datetime)
                key = dt.replace(minute=(dt.minute//15)*15, second=0, microsecond=0)
                grp[key].append(m)
            except ValueError:
                continue
        for ts, g in sorted(grp.items()):
            prices = [m.no_price for m in g if m.no_price > 0]
            depths = [m.depth for m in g if m.depth > 0]
            if not prices: continue
            candles.append(CandleData(
                timestamp=int(ts.timestamp() * 1000),
                open=float(prices[0]), high=float(max(prices)), low=float(min(prices)),
                close=float(prices[-1]), volume=float(len(g)),
                depth=float(sum(depths)/len(depths)) if depths else 1000.0,
                city=g[0].city,
            ))
    if len(candles) < 20:
        logger.warning("真实数据不足，补充模拟"); candles = generate_mock_data(days=30)
    return candles, {"forecasts": forecasts, "markets": markets, "actuals": actuals, "cities": list(cities)}


# ════════════════════════════════════════════════════════════════
# v3 高胜率引擎（P0+P1）
# ════════════════════════════════════════════════════════════════

class HighWinRateEngineV3:
    """
    v3 引擎 = v2 (P0) + P1 三项新增:
      1. 流动性深度过滤 (MIN_DEPTH, MAX_IMPACT_RATIO)
      2. 城市相关性风控 (compute_correlation_matrix → get_portfolio_risk)
      3. 动态边缘阈值 (get_dynamic_edge)
    """

    MIN_EDGE = MIN_EDGE; TP_PCT = TP_PCT; SL_PCT = SL_PCT
    TRAILING_ACTIVATE = TRAILING_ACTIVATE; TRAILING_DRAWDOWN = TRAILING_DRAWDOWN
    PRICE_LOW = PRICE_LOW; PRICE_HIGH = PRICE_HIGH; MIN_PROB_EDGE = MIN_PROB_EDGE
    ALLOWED_SIDES = ALLOWED_SIDES; TIME_STOP_HOURS = TIME_STOP_HOURS
    POSITION_SIZE_USD = POSITION_SIZE_USD; MAX_POSITIONS = MAX_POSITIONS
    INITIAL_CAPITAL = INITIAL_CAPITAL; COMMISSION_PCT = COMMISSION_PCT
    RSI_LENGTH = RSI_LENGTH; RSI_TOP = RSI_TOP; RSI_BOT = RSI_BOT
    MIN_HOURS_TO_EXPIRY = MIN_HOURS_TO_EXPIRY
    # P1
    MIN_DEPTH = MIN_DEPTH; MAX_IMPACT_RATIO = MAX_IMPACT_RATIO
    MAX_PORTFOLIO_RISK = MAX_PORTFOLIO_RISK; VOLATILITY_WINDOW = VOLATILITY_WINDOW

    def __init__(self, symbol: str = "MOCK", initial_capital: float = 5000.0, leverage: int = 1):
        self.symbol = symbol; self.initial_capital = initial_capital; self.leverage = leverage
        self.trades: List[BacktestTrade] = []; self.equity_curve = [initial_capital]
        self.equity = initial_capital
        self._min_price = float("inf"); self._max_price = 0.0
        self._position_side: Optional[str] = None; self._entry_price = 0.0
        self._entry_time: Optional[str] = None; self._entry_ms = 0; self._entry_bar = 0
        self._position_size = 0.0; self._max_pnl_pct = 0.0; self._bars_held = 0
        self._current_bar = 0; self._peak_price = 0.0
        self._closes: List[float] = []
        self._calibrator = RollingBiasCalibrator(window_days=ROLLING_WINDOW)
        self._current_bias_adj = 0.0; self._current_slippage = 0.0
        self._current_expiry_hours = 0.0; self._current_dynamic_edge = MIN_EDGE
        self._current_depth = 0.0; self._current_impact = 0.0; self._current_portfolio_risk = 0.0
        self.total_signals = 0; self.settlement_filtered = 0
        # P1 统计
        self.depth_filtered = 0; self.risk_filtered = 0
        # 城市仓位映射（用于组合风控）
        self._city_positions: Dict[str, float] = defaultdict(float)
        # 所有城市的 K 线（按城市分组，用于相关性矩阵）
        self._cities_prices: Dict[str, List[float]] = defaultdict(list)

    def _update_price_range(self, candles: List[CandleData]):
        for c in candles:
            if c.high > self._max_price: self._max_price = c.high
            if c.low < self._min_price: self._min_price = c.low

    def _is_price_in_range(self, price: float) -> bool:
        if self._max_price <= self._min_price: return True
        n = (price - self._min_price) / (self._max_price - self._min_price)
        return self.PRICE_LOW <= n <= self.PRICE_HIGH

    def _calc_model_prob(self, close: float) -> float:
        if len(self._closes) < self.RSI_LENGTH + 2: return 0.5
        gains = losses = 0.0
        for i in range(1, self.RSI_LENGTH + 1):
            d = self._closes[-i] - self._closes[-i-1]
            if d > 0: gains += d
            else: losses -= d
        ag = gains / self.RSI_LENGTH; al = losses / self.RSI_LENGTH
        rsi = 100.0 if al == 0 else 100.0 - (100.0 / (1.0 + ag/al))
        return max(0.0, min(1.0, 1.0 - rsi/100.0))

    def _check_prob_filter(self, prob: float) -> bool:
        return abs(prob - 0.5) >= self.MIN_PROB_EDGE

    def _calc_pos_size(self, price: float) -> float:
        return round(self.POSITION_SIZE_USD / price, 8) if price > 0 else 0.001

    # ════════════════════════════════════════════════════════════
    # P1: 深度检查
    # ════════════════════════════════════════════════════════════

    def _check_depth(self, candle: CandleData, pos_size: float) -> Tuple[bool, str]:
        depth = candle.depth
        if depth < self.MIN_DEPTH:
            return False, f"深度不足 ({depth:.0f} < {self.MIN_DEPTH})"
        impact = pos_size / depth if depth > 0 else 1.0
        if impact > self.MAX_IMPACT_RATIO:
            return False, f"冲击比过高 ({impact:.3f} > {self.MAX_IMPACT_RATIO})"
        self._current_depth = depth
        self._current_impact = impact
        return True, ""

    # ════════════════════════════════════════════════════════════
    # P1: 组合风控
    # ════════════════════════════════════════════════════════════

    def _compute_correlation_matrix(self) -> Tuple[List[str], np.ndarray]:
        if not self._cities_prices:
            return [], np.array([]) if HAS_MPL else []
        return compute_correlation_matrix(dict(self._cities_prices))

    def _check_portfolio_risk(self, candle: CandleData, pos_value: float) -> Tuple[bool, str]:
        cities, corr = self._compute_correlation_matrix()
        if not cities or corr.size == 0:
            return True, ""
        # 当前仓位价值 + 新仓估值
        pos_copy = dict(self._city_positions)
        city = candle.city or "default"
        pos_copy[city] = pos_copy.get(city, 0.0) + pos_value
        risk, conc = get_portfolio_risk(pos_copy, corr, cities)
        self._current_portfolio_risk = risk
        if risk > self.MAX_PORTFOLIO_RISK:
            return False, f"组合风险超限 ({risk:.0f} > {self.MAX_PORTFOLIO_RISK})"
        return True, ""

    # ════════════════════════════════════════════════════════════
    # 开平仓
    # ════════════════════════════════════════════════════════════

    def try_open_positions(self, candle: CandleData, model_prob: float,
                           expiry_ms: Optional[int] = None) -> bool:
        if self._position_side:
            return False

        # P1: 动态边缘
        dynamic_edge = get_dynamic_edge(self._closes, self.VOLATILITY_WINDOW)
        self._current_dynamic_edge = dynamic_edge
        edge = abs(model_prob - 0.5)

        # 基础过滤
        if not self._is_price_in_range(candle.close):
            return False
        if not self._check_prob_filter(model_prob):
            return False
        if edge < dynamic_edge:
            return False

        # P0: 结算过滤
        allowed, reason = can_open_position(candle.timestamp, expiry_ms, self.MIN_HOURS_TO_EXPIRY)
        if not allowed:
            self.settlement_filtered += 1; return False

        # P1: 深度检查
        pos_size = self._calc_pos_size(candle.close)
        depth_ok, depth_reason = self._check_depth(candle, pos_size)
        if not depth_ok:
            self.depth_filtered += 1; logger.debug(f"  深度过滤: {depth_reason}"); return False

        # P1: 组合风控
        pos_value = pos_size * candle.close
        risk_ok, risk_reason = self._check_portfolio_risk(candle, pos_value)
        if not risk_ok:
            self.risk_filtered += 1; logger.debug(f"  风控过滤: {risk_reason}"); return False

        # 滑点 + 偏差
        slippage = get_dynamic_slippage(candle.close, self._min_price, self._max_price)
        self._current_slippage = slippage
        bias = self._calibrator.get_bias()
        self._current_bias_adj = bias
        self._current_expiry_hours = hours_to_expiry(candle.timestamp, expiry_ms) if expiry_ms else 99.0

        # 开仓
        self._position_side = "NO"
        self._entry_price = candle.close * (1 + slippage)
        self._entry_time = datetime.fromtimestamp(candle.timestamp/1000, tz=timezone.utc).isoformat()
        self._entry_ms = candle.timestamp; self._entry_bar = self._current_bar
        self._position_size = pos_size; self._max_pnl_pct = 0.0; self._bars_held = 0
        self._peak_price = candle.close
        self.total_signals += 1
        # 更新城市仓位
        city = candle.city or "default"
        self._city_positions[city] += pos_value

        logger.debug(f"  🟢 [{self.symbol}] NO @ {candle.close:.2f} edge={edge:.3f}>={dynamic_edge:.3f} "
                     f"depth={candle.depth:.0f} impact={self._current_impact:.3f} risk={self._current_portfolio_risk:.0f}")
        return True

    def check_and_close_positions(self, candle: CandleData) -> Optional[str]:
        if not self._position_side: return None
        self._bars_held += 1; cp = candle.close
        if cp < self._peak_price: self._peak_price = cp
        ep = cp * (1 - self._current_slippage)
        pnl_pct = (self._entry_price - ep) / self._entry_price * 100
        if pnl_pct > self._max_pnl_pct: self._max_pnl_pct = pnl_pct
        if pnl_pct >= self.TP_PCT: self._close_trade(ep, candle.timestamp, "TP"); return "TP"
        if pnl_pct <= -self.SL_PCT: self._close_trade(ep, candle.timestamp, "SL"); return "SL"
        if self._max_pnl_pct >= self.TRAILING_ACTIVATE:
            if self._max_pnl_pct - pnl_pct >= self.TRAILING_DRAWDOWN:
                self._close_trade(ep, candle.timestamp, "Trailing"); return "Trailing"
        if (candle.timestamp - self._entry_ms) / 3600000 >= self.TIME_STOP_HOURS:
            self._close_trade(ep, candle.timestamp, "TimeStop"); return "TimeStop"
        return None

    def force_close_all(self, candle=None) -> int:
        if not self._position_side: return 0
        ep = candle.close if candle else self._entry_price
        self._close_trade(ep, candle.timestamp if candle else int(time.time()*1000), "ForceClose")
        return 1

    def _close_trade(self, exit_price: float, ts: int, reason: str):
        if not self._position_side: return
        iso = datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat()
        pnl = (self._entry_price - exit_price) * self._position_size
        comm = self._position_size * exit_price * (self.COMMISSION_PCT/100)
        pnl -= comm
        pnl_pct = (pnl/(self._entry_price*self._position_size))*100 if self._entry_price>0 and self._position_size>0 else 0
        self.trades.append(BacktestTrade(
            symbol=self.symbol, side=self._position_side,
            entry_time=self._entry_time or iso, exit_time=iso,
            entry_price=round(self._entry_price,8), exit_price=round(exit_price,8),
            size=round(self._position_size,8), pnl=round(pnl,2), pnl_pct=round(pnl_pct,2),
            leverage=self.leverage, exit_reason=reason, bars_held=self._bars_held,
            max_pnl_pct=round(self._max_pnl_pct,2), slippage=round(self._current_slippage,6),
            bias_adjustment=round(self._current_bias_adj,4),
            hours_to_expiry=round(self._current_expiry_hours,2),
            dynamic_edge=round(self._current_dynamic_edge,4),
            depth_at_entry=round(self._current_depth,2),
            impact_ratio=round(self._current_impact,4),
            portfolio_risk=round(self._current_portfolio_risk,2),
        ))
        self._position_side = None; self._position_size = 0.0
        self._max_pnl_pct = 0.0; self._current_slippage = 0.0; self._current_bias_adj = 0.0
        # 从城市仓位扣除
        # (简化：不精确追踪每个仓位所属城市，仅用于风控估算)

    # ════════════════════════════════════════════════════════════
    # 运行回测
    # ════════════════════════════════════════════════════════════

    def run(self, candles: List[CandleData],
            expiry_timestamps: Optional[List[Optional[int]]] = None) -> BacktestResult:
        if len(candles) < 20:
            return self._build_result()

        self._update_price_range(candles)

        # 预先按城市收集价格序列（用于相关性矩阵）
        for c in candles:
            city = c.city or "default"
            self._cities_prices[city].append(c.close)

        tp_c = sl_c = man_c = trail_c = ts_c = fc_c = 0
        peak_eq = self.initial_capital; max_dd = 0.0

        for i, candle in enumerate(candles):
            self._current_bar = i
            self._closes.append(candle.close)
            if len(self._closes) > 100: self._closes.pop(0)

            prob = self._calc_model_prob(candle.close)
            exp_ms = expiry_timestamps[i] if expiry_timestamps and i < len(expiry_timestamps) else None

            self.try_open_positions(candle, prob, exp_ms)

            if self._position_side:
                r = self.check_and_close_positions(candle)
                if r == "TP":
                    tp_c += 1
                elif r == "SL":
                    sl_c += 1
                elif r == "Trailing":
                    trail_c += 1
                elif r == "TimeStop":
                    ts_c += 1

            if self._position_side == "NO":
                upnl = (self._entry_price - candle.close) * self._position_size
            elif self._position_side == "YES":
                upnl = (candle.close - self._entry_price) * self._position_size
            else:
                upnl = 0
            ceq = self.initial_capital + sum(t.pnl for t in self.trades) + upnl
            self.equity = ceq
            if ceq > peak_eq: peak_eq = ceq
            dd = peak_eq - ceq
            if dd > max_dd: max_dd = dd
            self.equity_curve.append(round(ceq, 2))

        if self._position_side:
            self.force_close_all(candles[-1]); fc_c += 1

        result = self._build_result()
        for t in self.trades:
            if t.exit_reason == "ForceClose": fc_c += 1
            elif t.exit_reason == "Manual": man_c += 1
        result.long_trades = len(self.trades) - sum(1 for t in self.trades if t.side in ("NO","short"))
        result.short_trades = sum(1 for t in self.trades if t.side in ("NO","short"))
        result.tp1_count = tp_c; result.sl_count = sl_c; result.manual_count = man_c + fc_c
        result.tp2_count = trail_c; result.tp3_count = ts_c
        result.equity_curve = self.equity_curve
        result.max_drawdown = round(max_dd, 2); result.max_drawdown_pct = round((max_dd/peak_eq*100) if peak_eq>0 else 0, 2)
        result.settlement_filtered = self.settlement_filtered
        result.depth_filtered = self.depth_filtered; result.risk_filtered = self.risk_filtered
        if self.trades:
            result.avg_slippage = round(sum(t.slippage for t in self.trades)/len(self.trades), 6)
            result.avg_bias_adjustment = round(sum(t.bias_adjustment for t in self.trades)/len(self.trades), 4)
            result.avg_dynamic_edge = round(sum(t.dynamic_edge for t in self.trades)/len(self.trades), 4)
            result.avg_portfolio_risk = round(sum(t.portfolio_risk for t in self.trades)/len(self.trades), 2)
            result.max_portfolio_risk = round(max(t.portfolio_risk for t in self.trades), 2)
        if len(self.equity_curve) > 10:
            rets = [(self.equity_curve[i]-self.equity_curve[i-1])/self.equity_curve[i-1]
                    for i in range(1,len(self.equity_curve)) if self.equity_curve[i-1]>0]
            if rets:
                ar = sum(rets)/len(rets); v = sum((r-ar)**2 for r in rets)/len(rets)
                result.sharpe_ratio = round(ar/math.sqrt(v)*math.sqrt(365*24*4), 2) if v>0 else 0
        logger.info(f"[{self.symbol}] v3: {result.total_trades}笔, PnL=${result.total_pnl:.2f}, "
                    f"胜率={result.win_rate:.1f}%, 深度过滤={result.depth_filtered}, "
                    f"风控过滤={result.risk_filtered}, 动态edge={result.avg_dynamic_edge:.3f}")
        return result

    def _build_result(self) -> BacktestResult:
        t = len(self.trades); w = sum(1 for x in self.trades if x.pnl>0); l = t - w
        tp = sum(x.pnl for x in self.trades); wr = (w/t*100) if t>0 else 0
        aw = (sum(x.pnl for x in self.trades if x.pnl>0)/w) if w>0 else 0
        al = (sum(x.pnl for x in self.trades if x.pnl<0)/l) if l>0 else 0
        gp = sum(x.pnl for x in self.trades if x.pnl>0)
        gl = abs(sum(x.pnl for x in self.trades if x.pnl<0))
        pf = (gp/gl) if gl>0 else (gp if gp>0 else 0)
        ab = (sum(x.bars_held for x in self.trades)/t) if t>0 else 0
        return BacktestResult(symbol=self.symbol, params={
            "version":"v3","min_edge":MIN_EDGE,"tp_pct":TP_PCT,"sl_pct":SL_PCT,
            "rolling_window":ROLLING_WINDOW,"min_hours_to_expiry":MIN_HOURS_TO_EXPIRY,
            "base_slippage":BASE_SLIPPAGE,"max_slippage":MAX_SLIPPAGE,
            "min_depth":MIN_DEPTH,"max_impact_ratio":MAX_IMPACT_RATIO,
            "max_portfolio_risk":MAX_PORTFOLIO_RISK,"volatility_window":VOLATILITY_WINDOW,
        }, total_trades=t, winning_trades=w, losing_trades=l, win_rate=round(wr,2),
        total_pnl=round(tp,2), total_pnl_pct=round((tp/self.initial_capital)*100,2) if self.initial_capital>0 else 0,
        avg_win=round(aw,2), avg_loss=round(al,2), profit_factor=round(pf,2), avg_bars_held=round(ab,1),
        trades=[asdict(x) for x in self.trades])


# ════════════════════════════════════════════════════════════════
# 多周期对比
# ════════════════════════════════════════════════════════════════

def split_periods(candles, freq="W"):
    pm = OrderedDict()
    for c in candles:
        dt = datetime.fromtimestamp(c.timestamp/1000, tz=timezone.utc)
        k = dt.strftime("%Y-%m") if freq == "M" else f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
        if k not in pm: pm[k] = []
        pm[k].append(c)
    return list(pm.items())

def run_multi(symbol, periods, ic=5000.0, lev=1):
    res = []
    for label, pc in periods:
        if len(pc) < 10: continue
        e = HighWinRateEngineV3(symbol=symbol, initial_capital=ic, leverage=lev)
        r = e.run(pc)
        res.append((label, r))
        logger.info(f"  [{label}] 交易={r.total_trades}, PnL=${r.total_pnl:.2f}, 胜率={r.win_rate:.1f}%, PF={r.profit_factor}")
    return res

def compare_periods(res):
    if not res: return
    print(f"\n{'='*110}\n  📅 多周期对比 (v3)\n{'='*110}")
    print(f"| {'周期':<12s} | {'交易':>5s} | {'PnL':>10s} | {'胜率':>6s} | {'PF':>6s} | {'回撤':>6s} | {'滑点':>7s} | {'深滤':>5s} | {'风滤':>5s} | {'dEdge':>6s} |")
    print("|"+'-'*12+'|'+'-'*7+'|'+'-'*12+'|'+'-'*8+'|'+'-'*8+'|'+'-'*8+'|'+'-'*9+'|'+'-'*7+'|'+'-'*7+'|'+'-'*8+'|')
    tt = 0; tp = 0.0
    for l, r in res:
        i = "🟢" if r.total_pnl>=0 else "🔴"
        print(f"| {l:<12s} | {r.total_trades:>5d} | {i} {r.total_pnl:>+8.2f} | {r.win_rate:>5.1f}% | {r.profit_factor:>5.2f} | {r.max_drawdown_pct:>5.1f}% | {r.avg_slippage:>7.4f} | {r.depth_filtered:>5d} | {r.risk_filtered:>5d} | {r.avg_dynamic_edge:>6.3f} |")
        tt += r.total_trades; tp += r.total_pnl
    print("|"+'-'*12+'|'+'-'*7+'|'+'-'*12+'|'+'-'*8+'|'+'-'*8+'|'+'-'*8+'|'+'-'*9+'|'+'-'*7+'|'+'-'*7+'|'+'-'*8+'|')
    print(f"| {'合计':<12s} | {tt:>5d} | {'$'+f'{tp:+.2f}':>12s} | {'':>8s} | {'':>8s} | {'':>8s} | {'':>9s} | {'':>7s} | {'':>7s} | {'':>8s} |")
    print('='*110)


# ════════════════════════════════════════════════════════════════
# 可视化
# ════════════════════════════════════════════════════════════════

def plot_eq_dd(r, sym="", sp=""):
    if not HAS_MPL or not r.equity_curve or len(r.equity_curve)<2: return
    cv = r.equity_curve; x = list(range(len(cv)))
    pk = np.maximum.accumulate(cv); dd = (pk - np.array(cv))/pk*100
    fig, (a1,a2) = plt.subplots(2,1,figsize=(14,8),gridspec_kw={"height_ratios":[3,1]})
    fig.patch.set_facecolor("#0d1117")
    a1.fill_between(x,cv,alpha=0.1,color="#58a6ff"); a1.plot(x,cv,color="#58a6ff",lw=1.5)
    a1.axhline(y=cv[0],color="#484f58",lw=0.8,ls="--",alpha=0.7)
    a1.set_facecolor("#161b22"); a1.tick_params(colors="#8b949e",labelsize=9)
    a1.set_ylabel("Equity ($)",fontsize=11,color="#8b949e")
    a1.set_title(f"Equity Curve v3 — {sym or r.symbol}",fontsize=13,fontweight="bold",color="#f0f6fc")
    a1.grid(True,alpha=0.12,color="#30363d")
    fp = cv[-1]-cv[0]; pc = "#0ecb81" if fp>=0 else "#f6465d"
    a1.text(0.98,0.95,f"PnL: {'+' if fp>=0 else ''}${fp:.2f} ({r.total_pnl_pct:+.2f}%)",
            transform=a1.transAxes,fontsize=11,fontweight="bold",color=pc,ha="right",va="top",
            bbox=dict(boxstyle="round,pad=0.3",facecolor="#1c2128",edgecolor="#30363d"))
    a2.fill_between(x,0,dd,color="#f6465d",alpha=0.3); a2.plot(x,dd,color="#f6465d",lw=1)
    a2.set_facecolor("#161b22"); a2.tick_params(colors="#8b949e",labelsize=9)
    a2.set_ylabel("Drawdown (%)",fontsize=11,color="#8b949e"); a2.set_xlabel("Bar #",fontsize=11,color="#8b949e")
    a2.invert_yaxis(); a2.grid(True,alpha=0.12,color="#30363d")
    mi = np.argmax(dd); mv = dd[mi]
    a2.annotate(f"Max DD: {mv:.1f}%",xy=(mi,mv),xytext=(mi+20,mv+5),fontsize=9,color="#f6465d",fontweight="bold",
                arrowprops=dict(arrowstyle="->",color="#f6465d",lw=1.2),
                bbox=dict(boxstyle="round,pad=0.2",facecolor="#1c2128",edgecolor="#f6465d"))
    plt.tight_layout()
    if not sp: sp = f"tools/backtest/v3_eq_{sym.replace('-','_') if sym else 'result'}.png"
    plt.savefig(sp,dpi=150,bbox_inches="tight"); logger.info(f"📈 v3: {sp}"); plt.close(fig)


# ════════════════════════════════════════════════════════════════
# 报告
# ════════════════════════════════════════════════════════════════

def print_report(r, verbose=False):
    s = "="*60; d = "-"*60
    print(f"\n{s}\n  HighTempTation v3 回测报告 — {r.symbol}\n{s}")
    print(f"  参数 (P0+P1):")
    for k in ["min_edge","tp_pct","sl_pct","rolling_window","min_hours_to_expiry",
              "min_depth","max_impact_ratio","max_portfolio_risk","volatility_window"]:
        if k in r.params: print(f"    {k}: {r.params[k]}")
    print(d)
    print(f"  📊 核心: {r.total_trades}笔, PnL={'+' if r.total_pnl>=0 else ''}${r.total_pnl:.2f}, 胜率={r.win_rate:.1f}%, PF={r.profit_factor}, Sharpe={r.sharpe_ratio}")
    print(f"  📉 风控: 回撤={r.max_drawdown_pct:.1f}%, 均bars={r.avg_bars_held:.1f}")
    print(f"  🎯 P1: 深度过滤={r.depth_filtered}, 风控过滤={r.risk_filtered}, 动态edge={r.avg_dynamic_edge:.3f}, 组合风险={r.avg_portfolio_risk:.0f}/{r.max_portfolio_risk:.0f}")
    print(f"  📋 空头={r.short_trades} TP={r.tp1_count} SL={r.sl_count} Trail={r.tp2_count} TS={r.tp3_count}")
    print(s)
    if verbose and r.trades:
        print(f"\n  最近交易:")
        print(f"  {'时间':>20s} {'盈亏':>8s} {'原因':>12s} {'滑点':>7s} {'dEdge':>6s} {'深度':>7s} {'冲击':>7s} {'风控':>7s}")
        print(f"  {'─'*20} {'─'*8} {'─'*12} {'─'*7} {'─'*6} {'─'*7} {'─'*7} {'─'*7}")
        for t in r.trades[-8:]:
            td = t if isinstance(t,dict) else t.__dict__
            print(f"  {(td.get('exit_time','') or '')[11:19]:>20s} {td.get('pnl',0):>+8.2f} {td.get('exit_reason',''):>12s} "
                  f"{td.get('slippage',0):>7.4f} {td.get('dynamic_edge',0):>6.3f} {td.get('depth_at_entry',0):>7.0f} "
                  f"{td.get('impact_ratio',0):>7.4f} {td.get('portfolio_risk',0):>7.0f}")


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="HighTempTation v3 (P0+P1)")
    p.add_argument("--mode",default="mock",choices=["single","multi","mock"])
    p.add_argument("--symbol",default="MOCK"); p.add_argument("--days",type=int,default=90)
    p.add_argument("--seed",type=int,default=42); p.add_argument("--initial-capital",type=float,default=5000.0)
    p.add_argument("--leverage",type=int,default=1); p.add_argument("--real-data",action="store_true")
    p.add_argument("--data-dir",default="./data"); p.add_argument("--output",default="")
    p.add_argument("--verbose","-v",action="store_true"); p.add_argument("--no-plot",action="store_true")
    a = p.parse_args()

    logger.info("="*60)
    logger.info(f"  🌤️  HighTempTation v3 — P0+P1 全部优化")
    logger.info(f"  模式={a.mode}, P1: 深度>={MIN_DEPTH} 冲击<={MAX_IMPACT_RATIO} 组合风险<={MAX_PORTFOLIO_RISK} 动态窗口={VOLATILITY_WINDOW}")
    logger.info("="*60)

    use_real = a.real_data or USE_REAL_DATA
    candles = []
    if use_real:
        candles, _ = load_real_data(a.data_dir)
    else:
        candles = generate_mock_data(days=a.days, seed=a.seed)

    if a.mode == "mock":
        e = HighWinRateEngineV3(symbol=a.symbol, initial_capital=a.initial_capital, leverage=a.leverage)
        r = e.run(candles); print_report(r, verbose=a.verbose)
        if not a.no_plot and HAS_MPL: plot_eq_dd(r, sym=a.symbol)

        logger.info("\n📅 多周期对比 (按周)")
        periods = split_periods(candles); logger.info(f"  共 {len(periods)} 个周期")
        pr = run_multi(a.symbol, periods, a.initial_capital, a.leverage)
        compare_periods(pr)
        if a.output:
            with open(a.output,"w") as f: json.dump({"single":r.to_dict(),"multi":[(l,x.to_dict()) for l,x in pr]},f,ensure_ascii=False,indent=2)
        print("\n全部完成。")
    elif a.mode == "single":
        e = HighWinRateEngineV3(symbol=a.symbol, initial_capital=a.initial_capital, leverage=a.leverage)
        r = e.run(candles); print_report(r, verbose=a.verbose)
        if not a.no_plot and HAS_MPL: plot_eq_dd(r, sym=a.symbol)
        print("\n全部完成。")
    elif a.mode == "multi":
        periods = split_periods(candles); logger.info(f"  共 {len(periods)} 个周期")
        pr = run_multi(a.symbol, periods, a.initial_capital, a.leverage)
        compare_periods(pr); print("\n全部完成。")

if __name__ == "__main__":
    main()
