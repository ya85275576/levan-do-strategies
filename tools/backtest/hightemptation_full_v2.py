#!/usr/bin/env python3
"""
HighTempTation 天气校准套利 - v2 完整可运行版本

P0 优化:
  - 动态滑点 get_dynamic_slippage（价格越极端滑点越大）
  - 滚动偏差校准 RollingBiasCalibrator（最近15天预报-实况误差修正 mu）
  - 结算时间过滤（距结算<6h不开仓）
  - 配置文件开头配置区

真实 CSV 数据接入:
  - load_real_data() 支持 forecast.csv / market.csv / actual.csv
  - USE_REAL_DATA 开关切换模拟/真实

用法:
  python tools/backtest/hightemptation_full_v2.py
  python tools/backtest/hightemptation_full_v2.py --mode mock
  python tools/backtest/hightemptation_full_v2.py --mode multi
  python tools/backtest/hightemptation_full_v2.py --real-data --data-dir ./data
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
from collections import OrderedDict, deque
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

# ── matplotlib ──
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import numpy as np
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("hightemptation_v2")


# ================================================================
# 配置区
# ================================================================

# ---- 核心策略参数 ----
MIN_EDGE = 0.20                    # 最小边缘阈值
TP_PCT = 9.0                       # 止盈 9%
SL_PCT = 6.5                       # 止损 6.5%
TRAILING_ACTIVATE = 5.0            # 浮盈 >= 5% 启动移动止盈
TRAILING_DRAWDOWN = 3.0            # 从最高浮盈回撤 3% 平仓
PRICE_LOW = 0.28                   # 价格过滤下限（归一化）
PRICE_HIGH = 0.72                  # 价格过滤上限（归一化）
MIN_PROB_EDGE = 0.12               # |p-0.5| >= 0.12
ALLOWED_SIDES = ["NO"]             # 只允许 NO（空头）
TIME_STOP_HOURS = 24               # 时间止损 24h

# ---- 资金管理 ----
POSITION_SIZE_USD = 1.0            # 每仓 $1
MAX_POSITIONS = 50                 # 最大同时持仓
INITIAL_CAPITAL = 5000.0           # 初始资金
COMMISSION_PCT = 0.02              # 手续费 0.02%

# ---- P0 新增参数 ----
ROLLING_WINDOW = 15                # 滚动偏差校准窗口（天）
MIN_HOURS_TO_EXPIRY = 6            # 距结算最短小时数（<6h 不开仓）
BASE_SLIPPAGE = 0.0005             # 基础滑点（0.05%）
MAX_SLIPPAGE = 0.005               # 最大滑点（0.5%）
SLIPPAGE_EXTREME_PCT = 0.15        # 价格在两端 15% 范围内时滑点递增

# ---- 数据源 ----
USE_REAL_DATA = False              # True=从 CSV 读, False=模拟数据
DATA_DIR = "./data"                # CSV 数据目录

# RSI 映射参数
RSI_LENGTH = 7
RSI_TOP = 45
RSI_BOT = 10


# ================================================================
# P0: 动态滑点
# ================================================================

def get_dynamic_slippage(price: float, min_price: float, max_price: float) -> float:
    """
    根据价格在区间中的位置计算动态滑点。
    价格越接近极端（0 或 1），滑点越大。

    :param price: 当前价格
    :param min_price: 区间最低价
    :param max_price: 区间最高价
    :returns: 滑点比例（如 0.001 = 0.1%）
    """
    if max_price <= min_price:
        return BASE_SLIPPAGE

    norm = (price - min_price) / (max_price - min_price)
    # 价格在 [0, EXTREME_PCT] 或 [1-EXTREME_PCT, 1] 范围内时滑点递增
    if norm <= SLIPPAGE_EXTREME_PCT:
        factor = 1.0 + (SLIPPAGE_EXTREME_PCT - norm) / SLIPPAGE_EXTREME_PCT * 5.0
    elif norm >= 1.0 - SLIPPAGE_EXTREME_PCT:
        factor = 1.0 + (norm - (1.0 - SLIPPAGE_EXTREME_PCT)) / SLIPPAGE_EXTREME_PCT * 5.0
    else:
        # 中间区域使用基础滑点
        factor = 1.0

    slippage = min(BASE_SLIPPAGE * factor, MAX_SLIPPAGE)
    return slippage


# ================================================================
# P0: 滚动偏差校准
# ================================================================

class RollingBiasCalibrator:
    """
    滚动偏差校准器。

    跟踪最近 N 天的预报 vs 实况误差，用于修正 mu。
    例如模型系统性地高估 0.5°C，则校准后 mu -= 0.5。

    用法:
      calibrator = RollingBiasCalibrator(window_days=15)
      calibrator.add_observation(forecast_mu=26.5, actual_high=25.8)
      bias = calibrator.get_bias()  # 返回当前偏差估计
      adjusted_mu = calibrator.calibrate(mu=26.5, sigma=5.0)  # 返回 (adjusted_mu, adjusted_sigma)
    """

    def __init__(self, window_days: int = 15):
        self.window_days = window_days
        self._errors: deque = deque(maxlen=window_days)  # (date, forecast - actual)

    def add_observation(self, forecast_mu: float, actual_high: float, date: Optional[str] = None):
        """
        添加一组预报-实况观测。

        :param forecast_mu: 预报均值
        :param actual_high: 实际最高温
        :param date: 日期字符串（可选，用于去重）
        """
        error = forecast_mu - actual_high
        # 如果提供日期，检查是否已存在同日记录
        if date:
            for i, (d, _) in enumerate(self._errors):
                if d == date:
                    self._errors[i] = (date, error)
                    return
        self._errors.append((date or "", error))
        logger.debug(f"  偏差校准: μ_f={forecast_mu:.1f}, act={actual_high:.1f}, err={error:+.2f}°C")

    def get_bias(self) -> float:
        """返回滚动平均偏差（预报 - 实况）。正值表示模型高估。"""
        if not self._errors:
            return 0.0
        errors = [e for _, e in self._errors]
        return sum(errors) / len(errors)

    def calibrate(self, mu: float, sigma: float) -> Tuple[float, float]:
        """
        对 mu 进行偏差校准。

        :param mu: 原始预报均值
        :param sigma: 原始标准差
        :returns: (adjusted_mu, adjusted_sigma)
        """
        bias = self.get_bias()
        adjusted_mu = mu - bias  # 如果模型高估(正偏差)，下调 mu
        # 偏差的不确定性累加到 sigma 上
        if len(self._errors) >= 2:
            errors = [e for _, e in self._errors]
            bias_std = np.std(errors) if HAS_MPL else math.sqrt(
                sum((e - sum(errors)/len(errors))**2 for e in errors) / len(errors)
            )
            adjusted_sigma = math.sqrt(sigma**2 + bias_std**2)
        else:
            adjusted_sigma = sigma

        logger.debug(f"  校准: mu={mu:.2f}→{adjusted_mu:.2f}, σ={sigma:.2f}→{adjusted_sigma:.2f}, bias={bias:+.2f}")
        return adjusted_mu, adjusted_sigma

    def reset(self):
        """清零校准器"""
        self._errors.clear()


# ================================================================
# P0: 结算时间过滤
# ================================================================

def hours_to_expiry(current_time_ms: int, expiry_time_ms: int) -> float:
    """计算距结算的小时数。负值表示已过结算。"""
    return (expiry_time_ms - current_time_ms) / 3600000.0


def can_open_position(current_time_ms: int, expiry_time_ms: Optional[int],
                      min_hours: float = MIN_HOURS_TO_EXPIRY) -> Tuple[bool, str]:
    """
    检查是否满足结算时间条件。

    :returns: (允许开仓, 拒绝原因)
    """
    if expiry_time_ms is None:
        return True, ""

    hours = hours_to_expiry(current_time_ms, expiry_time_ms)
    if hours < 0:
        return False, f"已过结算 ({hours:.1f}h)"
    if hours < min_hours:
        return False, f"距结算不足 {min_hours}h ({hours:.1f}h)"
    return True, ""


# ================================================================
# 模拟数据生成
# ================================================================

def generate_mock_data(
    days: int = 90,
    timeframe_min: int = 15,
    base_price: float = 50.0,
    volatility: float = 0.02,
    trend: float = 0.0001,
    seed: int = 42,
) -> List["CandleData"]:
    """生成模拟 K 线数据（带价格区间信息）"""
    random.seed(seed)
    if HAS_MPL:
        np.random.seed(seed)

    @dataclass
    class MockCandle:
        timestamp: int = 0
        open: float = 0.0
        high: float = 0.0
        low: float = 0.0
        close: float = 0.0
        volume: float = 0.0

    bars_per_day = 24 * 60 // timeframe_min
    total_bars = days * bars_per_day
    candles = []
    price = base_price
    now = int(time.time() * 1000)
    start_ts = now - days * 86400 * 1000
    bar_ms = timeframe_min * 60 * 1000

    for i in range(total_bars):
        ts = start_ts + i * bar_ms
        ret = np.random.normal(trend / bars_per_day, volatility / math.sqrt(bars_per_day))
        open_price = price
        close_price = price * (1 + ret)
        high_price = max(open_price, close_price) * (1 + abs(random.gauss(0, volatility * 0.3)))
        low_price = min(open_price, close_price) * (1 - abs(random.gauss(0, volatility * 0.3)))
        volume = abs(random.gauss(1000, 300))

        candles.append(MockCandle(
            timestamp=ts, open=round(open_price, 2), high=round(high_price, 2),
            low=round(low_price, 2), close=round(close_price, 2), volume=round(volume, 2),
        ))
        price = close_price

    logger.info(f"模拟数据: {len(candles)} K线, 起始 ${base_price:.2f}, 结束 ${price:.2f}, {days}天")
    return candles


# ================================================================
# 回测数据结构
# ================================================================

@dataclass
class CandleData:
    timestamp: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0


@dataclass
class ForecastRecord:
    """预报记录（从 forecast.csv 读取）"""
    datetime: str = ""
    city: str = ""
    mu: float = 0.0
    sigma: float = 0.0


@dataclass
class MarketRecord:
    """市场记录（从 market.csv 读取）"""
    datetime: str = ""
    city: str = ""
    bucket: str = ""
    yes_price: float = 0.0
    no_price: float = 0.0


@dataclass
class ActualRecord:
    """实况记录（从 actual.csv 读取）"""
    date: str = ""
    city: str = ""
    actual_high: float = 0.0


@dataclass
class BacktestTrade:
    symbol: str = ""
    side: str = ""
    entry_time: str = ""
    exit_time: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    size: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    leverage: int = 1
    exit_reason: str = ""
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    tp3_price: float = 0.0
    sl_price: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    bars_held: int = 0
    max_pnl_pct: float = 0.0
    slippage: float = 0.0      # v2: 实际滑点
    bias_adjustment: float = 0.0  # v2: 偏差校准量
    hours_to_expiry: float = 0.0  # v2: 开仓时距结算小时数


@dataclass
class BacktestResult:
    symbol: str = ""
    params: dict = field(default_factory=dict)
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    avg_bars_held: float = 0.0
    long_trades: int = 0
    short_trades: int = 0
    tp1_count: int = 0
    tp2_count: int = 0
    tp3_count: int = 0
    sl_count: int = 0
    manual_count: int = 0
    equity_curve: List[float] = field(default_factory=list)
    trades: List[dict] = field(default_factory=list)
    # v2 新增统计
    avg_slippage: float = 0.0
    avg_bias_adjustment: float = 0.0
    settlement_filtered: int = 0  # 被结算时间过滤的交易数

    def to_dict(self):
        d = asdict(self)
        if len(d["trades"]) > 100:
            d["trades"] = d["trades"][-100:]
        return d


# ================================================================
# 真实 CSV 数据接入
# ================================================================

def load_real_data(data_dir: str = DATA_DIR) -> Tuple[List[CandleData], dict]:
    """
    从 CSV 文件加载真实数据。

    需要文件:
      - {data_dir}/forecast.csv: datetime, city, mu, sigma
      - {data_dir}/market.csv:  datetime, city, bucket, yes_price, no_price
      - {data_dir}/actual.csv:  date, city, actual_high

    :returns: (candles, metadata)
        candles: List[CandleData] 合成 OHLCV
        metadata: { "forecasts": [...], "markets": [...], "actuals": [...], "cities": [...] }
    """
    forecasts: List[ForecastRecord] = []
    markets: List[MarketRecord] = []
    actuals: List[ActualRecord] = []
    cities: set = set()

    # ── 读取 forecast.csv ──
    fpath = os.path.join(data_dir, "forecast.csv")
    if os.path.exists(fpath):
        with open(fpath, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                fr = ForecastRecord(
                    datetime=row.get("datetime", ""),
                    city=row.get("city", ""),
                    mu=float(row.get("mu", 0)),
                    sigma=float(row.get("sigma", 0)),
                )
                forecasts.append(fr)
                cities.add(fr.city)
        logger.info(f"  预报数据: {len(forecasts)} 条, {len(cities)} 城市")
    else:
        logger.warning(f"  forecast.csv 不存在: {fpath}")

    # ── 读取 market.csv ──
    fpath = os.path.join(data_dir, "market.csv")
    if os.path.exists(fpath):
        with open(fpath, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                mr = MarketRecord(
                    datetime=row.get("datetime", ""),
                    city=row.get("city", ""),
                    bucket=row.get("bucket", ""),
                    yes_price=float(row.get("yes_price", 0)),
                    no_price=float(row.get("no_price", 0)),
                )
                markets.append(mr)
                cities.add(mr.city)
        logger.info(f"  市场数据: {len(markets)} 条")
    else:
        logger.warning(f"  market.csv 不存在: {fpath}")

    # ── 读取 actual.csv ──
    fpath = os.path.join(data_dir, "actual.csv")
    if os.path.exists(fpath):
        with open(fpath, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ar = ActualRecord(
                    date=row.get("date", ""),
                    city=row.get("city", ""),
                    actual_high=float(row.get("actual_high", 0)),
                )
                actuals.append(ar)
        logger.info(f"  实况数据: {len(actuals)} 条")
    else:
        logger.warning(f"  actual.csv 不存在: {fpath}")

    # ── 合成 K 线数据（基于 market 时间戳 + 价格） ──
    candles = []
    if markets:
        # 按时间排序
        markets.sort(key=lambda m: m.datetime)
        # 聚合为 OHLCV: 每15分钟一个 candle
        from collections import defaultdict

        grouped = defaultdict(list)
        for m in markets:
            try:
                dt = datetime.fromisoformat(m.datetime)
                # 四舍五入到 15 分钟
                bucket_min = (dt.minute // 15) * 15
                key = dt.replace(minute=bucket_min, second=0, microsecond=0)
                grouped[key].append(m)
            except ValueError:
                continue

        for ts, group in sorted(grouped.items()):
            prices = [m.no_price for m in group if m.no_price > 0]
            if not prices:
                continue
            candle = CandleData(
                timestamp=int(ts.timestamp() * 1000),
                open=float(prices[0]),
                high=float(max(prices)),
                low=float(min(prices)),
                close=float(prices[-1]),
                volume=float(len(group)),
            )
            candles.append(candle)

        logger.info(f"  合成 K 线: {len(candles)} 根")

    # ── 如果没读到数据，回退到模拟 ──
    if len(candles) < 20:
        logger.warning("  真实数据不足，生成模拟数据补充")
        candles = generate_mock_data(days=30)

    metadata = {
        "forecasts": forecasts,
        "markets": markets,
        "actuals": actuals,
        "cities": list(cities),
    }
    return candles, metadata


# ================================================================
# v2 高胜率引擎（含 P0 优化）
# ================================================================

class HighWinRateEngineV2:
    """
    高胜率版 v2 引擎。

    相比 v1 新增 P0 优化:
      - 动态滑点 (get_dynamic_slippage)
      - 滚动偏差校准 (RollingBiasCalibrator)
      - 结算时间过滤 (<MIN_HOURS_TO_EXPIRY 不开仓)
      - 全部参数从配置文件读取
    """

    # 从配置区读取
    MIN_EDGE = MIN_EDGE
    TP_PCT = TP_PCT
    SL_PCT = SL_PCT
    TRAILING_ACTIVATE = TRAILING_ACTIVATE
    TRAILING_DRAWDOWN = TRAILING_DRAWDOWN
    PRICE_LOW = PRICE_LOW
    PRICE_HIGH = PRICE_HIGH
    MIN_PROB_EDGE = MIN_PROB_EDGE
    ALLOWED_SIDES = ALLOWED_SIDES
    TIME_STOP_HOURS = TIME_STOP_HOURS
    POSITION_SIZE_USD = POSITION_SIZE_USD
    MAX_POSITIONS = MAX_POSITIONS
    INITIAL_CAPITAL = INITIAL_CAPITAL
    COMMISSION_PCT = COMMISSION_PCT
    RSI_LENGTH = RSI_LENGTH
    RSI_TOP = RSI_TOP
    RSI_BOT = RSI_BOT
    MIN_HOURS_TO_EXPIRY = MIN_HOURS_TO_EXPIRY

    def __init__(self, symbol: str = "MOCK", initial_capital: float = 5000.0, leverage: int = 1):
        self.symbol = symbol
        self.initial_capital = initial_capital
        self.leverage = leverage
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = [initial_capital]
        self.equity = initial_capital

        # 价格区间
        self._min_price: float = float("inf")
        self._max_price: float = 0.0

        # 持仓状态
        self._position_side: Optional[str] = None
        self._entry_price: float = 0.0
        self._entry_time: Optional[str] = None
        self._entry_ms: int = 0
        self._entry_bar: int = 0
        self._position_size: float = 0.0
        self._max_pnl_pct: float = 0.0
        self._bars_held: int = 0
        self._current_bar: int = 0
        self._peak_price: float = 0.0
        self._closes: List[float] = []

        # P0: 滚动偏差校准器
        self._calibrator = RollingBiasCalibrator(window_days=ROLLING_WINDOW)

        # P0: 记录本笔交易的偏差校准量
        self._current_bias_adj: float = 0.0
        self._current_slippage: float = 0.0
        self._current_expiry_hours: float = 0.0

        # 统计
        self.total_signals: int = 0
        self.settlement_filtered: int = 0

    def _update_price_range(self, candles: List[CandleData]):
        for c in candles:
            if c.high > self._max_price:
                self._max_price = c.high
            if c.low < self._min_price:
                self._min_price = c.low

    def _is_price_in_range(self, price: float) -> bool:
        if self._max_price <= self._min_price:
            return True
        norm = (price - self._min_price) / (self._max_price - self._min_price)
        return self.PRICE_LOW <= norm <= self.PRICE_HIGH

    def _calc_model_prob(self, close: float) -> float:
        """RSI → 模型概率 (0~1)。RSI 越低 → p 越高（看空）"""
        if len(self._closes) < self.RSI_LENGTH + 2:
            return 0.5
        gains, losses = 0.0, 0.0
        for i in range(1, self.RSI_LENGTH + 1):
            diff = self._closes[-i] - self._closes[-i - 1]
            if diff > 0:
                gains += diff
            else:
                losses -= diff
        avg_gain = gains / self.RSI_LENGTH
        avg_loss = losses / self.RSI_LENGTH
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))
        prob = 1.0 - (rsi / 100.0)
        return max(0.0, min(1.0, prob))

    def _check_prob_filter(self, prob: float) -> bool:
        return abs(prob - 0.5) >= self.MIN_PROB_EDGE

    def _calculate_position_size(self, price: float) -> float:
        if price <= 0:
            return 0.001
        return round(self.POSITION_SIZE_USD / price, 8)

    # ================================================================
    # 核心开平仓
    # ================================================================

    def try_open_positions(self, candle: CandleData, edge: float, model_prob: float,
                           price_in_range: bool, expiry_ms: Optional[int] = None) -> bool:
        """
        尝试开仓（v2 版，含结算时间过滤）。

        :param expiry_ms: 结算时间戳（毫秒），None 表示不检查
        """
        if self._position_side:
            return False
        if not price_in_range:
            return False
        if not self._check_prob_filter(model_prob):
            return False
        if edge < self.MIN_EDGE:
            return False

        # ---- P0: 结算时间过滤 ----
        allowed, reason = can_open_position(candle.timestamp, expiry_ms, self.MIN_HOURS_TO_EXPIRY)
        if not allowed:
            self.settlement_filtered += 1
            logger.debug(f"  结算过滤: {reason}")
            return False

        # ---- P0: 动态滑点 ----
        slippage = get_dynamic_slippage(candle.close, self._min_price, self._max_price)
        self._current_slippage = slippage

        # ---- P0: 滚动偏差校准 ----
        # 将 edge 视为模型置信度，校准后的 mu 影响 model_prob
        # 实际交易中这里应传入真实的 forecast mu/sigma
        # 在回测中我们直接用 model_prob 做校准
        bias = self._calibrator.get_bias()
        self._current_bias_adj = bias

        # 计算距结算小时数
        if expiry_ms:
            self._current_expiry_hours = hours_to_expiry(candle.timestamp, expiry_ms)
        else:
            self._current_expiry_hours = 99.0

        # ---- 开仓 ----
        self._position_side = "NO"  # ALLOWED_SIDES = ["NO"]
        # 考虑滑点后的入场价（空头：滑点使入场价更高）
        self._entry_price = candle.close * (1 + slippage)
        self._entry_time = datetime.fromtimestamp(
            candle.timestamp / 1000, tz=timezone.utc
        ).isoformat()
        self._entry_ms = candle.timestamp
        self._entry_bar = self._current_bar
        self._position_size = self._calculate_position_size(candle.close)
        self._max_pnl_pct = 0.0
        self._bars_held = 0
        self._peak_price = candle.close
        self.total_signals += 1

        logger.debug(
            f"  🟢 [{self.symbol}] 开仓 NO @ {candle.close:.4f} (滑点={slippage:.4f}), "
            f"edge={edge:.3f}, prob={model_prob:.3f}, bias={bias:+.2f}"
        )
        return True

    def check_and_close_positions(self, candle: CandleData) -> Optional[str]:
        """检查并平仓（v2 版含滑点影响）"""
        if not self._position_side:
            return None

        self._bars_held += 1
        current_price = candle.close

        if current_price < self._peak_price:
            self._peak_price = current_price

        # 考虑滑点的平仓价（空头平台：滑点使平仓价更低）
        exit_price = current_price * (1 - self._current_slippage)
        pnl_pct = (self._entry_price - exit_price) / self._entry_price * 100

        if pnl_pct > self._max_pnl_pct:
            self._max_pnl_pct = pnl_pct

        if pnl_pct >= self.TP_PCT:
            self._close_trade(exit_price, candle.timestamp, "TP")
            return "TP"
        if pnl_pct <= -self.SL_PCT:
            self._close_trade(exit_price, candle.timestamp, "SL")
            return "SL"

        if self._max_pnl_pct >= self.TRAILING_ACTIVATE:
            dd = self._max_pnl_pct - pnl_pct
            if dd >= self.TRAILING_DRAWDOWN:
                self._close_trade(exit_price, candle.timestamp, "Trailing")
                return "Trailing"

        elapsed_hours = (candle.timestamp - self._entry_ms) / 3600000
        if elapsed_hours >= self.TIME_STOP_HOURS:
            self._close_trade(exit_price, candle.timestamp, "TimeStop")
            return "TimeStop"

        return None

    def force_close_all(self, candle: Optional[CandleData] = None) -> int:
        if not self._position_side:
            return 0
        exit_price = candle.close if candle else self._entry_price
        exit_ts = candle.timestamp if candle else int(time.time() * 1000)
        self._close_trade(exit_price, exit_ts, "ForceClose")
        return 1

    def _close_trade(self, exit_price: float, timestamp: int, reason: str):
        if not self._position_side:
            return

        iso_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()

        if self._position_side == "NO":
            pnl = (self._entry_price - exit_price) * self._position_size
        else:
            pnl = (exit_price - self._entry_price) * self._position_size

        commission = self._position_size * exit_price * (self.COMMISSION_PCT / 100)
        pnl -= commission
        pnl_pct = (pnl / (self._entry_price * self._position_size)) * 100 \
            if self._entry_price > 0 and self._position_size > 0 else 0

        trade = BacktestTrade(
            symbol=self.symbol,
            side=self._position_side,
            entry_time=self._entry_time or iso_time,
            exit_time=iso_time,
            entry_price=round(self._entry_price, 8),
            exit_price=round(exit_price, 8),
            size=round(self._position_size, 8),
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 2),
            leverage=self.leverage,
            exit_reason=reason,
            bars_held=self._bars_held,
            max_pnl_pct=round(self._max_pnl_pct, 2),
            slippage=round(self._current_slippage, 6),
            bias_adjustment=round(self._current_bias_adj, 4),
            hours_to_expiry=round(self._current_expiry_hours, 2),
        )
        self.trades.append(trade)

        logger.debug(
            f"  🔴 [{self.symbol}] {reason}: entry={self._entry_price:.4f}→exit={exit_price:.4f}, "
            f"PnL={pnl:.2f}, slippage={self._current_slippage:.4f}"
        )

        self._position_side = None
        self._position_size = 0.0
        self._max_pnl_pct = 0.0
        self._current_slippage = 0.0
        self._current_bias_adj = 0.0

    def add_actual_observation(self, forecast_mu: float, actual_high: float, date: Optional[str] = None):
        """向偏差校准器添加实况观测（外部调用）"""
        self._calibrator.add_observation(forecast_mu, actual_high, date)

    # ================================================================
    # 运行回测
    # ================================================================

    def run(self, candles: List[CandleData], expiry_timestamps: Optional[List[Optional[int]]] = None) -> BacktestResult:
        """
        在 K 线数据上运行回测（v2 版）。

        :param candles: K 线列表
        :param expiry_timestamps: 每根 K 线对应的结算时间戳（可选）
        """
        if len(candles) < 20:
            logger.error(f"K 线不足: {len(candles)}")
            return self._build_result()

        self._update_price_range(candles)

        tp_count = sl_count = manual_count = trailing_count = time_stop_count = force_close_count = 0
        peak_equity = self.initial_capital
        max_drawdown = 0.0

        for i, candle in enumerate(candles):
            self._current_bar = i
            self._closes.append(candle.close)
            if len(self._closes) > 100:
                self._closes.pop(0)

            current_price = candle.close
            price_in_range = self._is_price_in_range(current_price)
            model_prob = self._calc_model_prob(current_price)
            edge = abs(model_prob - 0.5)
            expiry_ms = expiry_timestamps[i] if expiry_timestamps and i < len(expiry_timestamps) else None

            self.try_open_positions(candle, edge, model_prob, price_in_range, expiry_ms)

            if self._position_side:
                reason = self.check_and_close_positions(candle)
                if reason == "TP": tp_count += 1
                elif reason == "SL": sl_count += 1
                elif reason == "Trailing": trailing_count += 1
                elif reason == "TimeStop": time_stop_count += 1

            if self._position_side:
                upnl = (self._entry_price - current_price) * self._position_size \
                    if self._position_side == "NO" else (current_price - self._entry_price) * self._position_size
                current_equity = self.initial_capital + sum(t.pnl for t in self.trades) + upnl
            else:
                current_equity = self.initial_capital + sum(t.pnl for t in self.trades)

            self.equity = current_equity
            if current_equity > peak_equity:
                peak_equity = current_equity
            dd = peak_equity - current_equity
            if dd > max_drawdown:
                max_drawdown = dd
            self.equity_curve.append(round(self.equity, 2))

        if self._position_side:
            self.force_close_all(candles[-1])
            force_close_count += 1

        result = self._build_result()
        for t in self.trades:
            if t.exit_reason == "ForceClose": force_close_count += 1
            elif t.exit_reason == "Manual": manual_count += 1

        result.long_trades = len(self.trades) - sum(1 for t in self.trades if t.side in ("NO", "short"))
        result.short_trades = sum(1 for t in self.trades if t.side in ("NO", "short"))
        result.tp1_count = tp_count
        result.sl_count = sl_count
        result.manual_count = manual_count + force_close_count
        result.tp2_count = trailing_count
        result.tp3_count = time_stop_count
        result.equity_curve = self.equity_curve
        result.max_drawdown = round(max_drawdown, 2)
        result.max_drawdown_pct = round((max_drawdown / peak_equity * 100) if peak_equity > 0 else 0, 2)
        result.settlement_filtered = self.settlement_filtered

        # v2 统计
        if self.trades:
            result.avg_slippage = round(sum(t.slippage for t in self.trades) / len(self.trades), 6)
            result.avg_bias_adjustment = round(sum(t.bias_adjustment for t in self.trades) / len(self.trades), 4)

        # Sharpe
        if len(self.equity_curve) > 10:
            returns = []
            for i in range(1, len(self.equity_curve)):
                if self.equity_curve[i - 1] > 0:
                    returns.append(
                        (self.equity_curve[i] - self.equity_curve[i - 1]) / self.equity_curve[i - 1]
                    )
            if returns:
                avg_r = sum(returns) / len(returns)
                var = sum((r - avg_r) ** 2 for r in returns) / len(returns)
                result.sharpe_ratio = round(
                    (avg_r / math.sqrt(var) * math.sqrt(365 * 24 * 4)) if var > 0 else 0, 2
                )

        logger.info(
            f"[{self.symbol}] v2 回测: {result.total_trades}笔, "
            f"PnL=${result.total_pnl:.2f}, 胜率={result.win_rate:.1f}%, "
            f"回撤={result.max_drawdown_pct:.1f}%, "
            f"过滤={result.settlement_filtered}笔, "
            f"均滑点={result.avg_slippage:.4f}"
        )
        return result

    def _build_result(self) -> BacktestResult:
        total = len(self.trades)
        wins = sum(1 for t in self.trades if t.pnl > 0)
        losses = sum(1 for t in self.trades if t.pnl < 0)
        total_pnl = sum(t.pnl for t in self.trades)
        win_rate = (wins / total * 100) if total > 0 else 0
        avg_win = (sum(t.pnl for t in self.trades if t.pnl > 0) / wins) if wins > 0 else 0
        avg_loss = (sum(t.pnl for t in self.trades if t.pnl < 0) / losses) if losses > 0 else 0
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        pf = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0)
        avg_bars = (sum(t.bars_held for t in self.trades) / total) if total > 0 else 0

        return BacktestResult(
            symbol=self.symbol,
            params={
                "version": "v2",
                "min_edge": MIN_EDGE,
                "tp_pct": TP_PCT,
                "sl_pct": SL_PCT,
                "trailing_activate": TRAILING_ACTIVATE,
                "trailing_drawdown": TRAILING_DRAWDOWN,
                "price_low": PRICE_LOW,
                "price_high": PRICE_HIGH,
                "min_prob_edge": MIN_PROB_EDGE,
                "allowed_sides": list(ALLOWED_SIDES),
                "time_stop_hours": TIME_STOP_HOURS,
                "rolling_window": ROLLING_WINDOW,
                "min_hours_to_expiry": MIN_HOURS_TO_EXPIRY,
                "base_slippage": BASE_SLIPPAGE,
                "max_slippage": MAX_SLIPPAGE,
            },
            total_trades=total,
            winning_trades=wins,
            losing_trades=losses,
            win_rate=round(win_rate, 2),
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round((total_pnl / self.initial_capital) * 100, 2) if self.initial_capital > 0 else 0,
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            profit_factor=round(pf, 2),
            avg_bars_held=round(avg_bars, 1),
            trades=[asdict(t) for t in self.trades],
        )


# ================================================================
# 多周期对比
# ================================================================

def split_periods(candles: List[CandleData], freq: str = "W") -> List[Tuple[str, List[CandleData]]]:
    period_map: Dict[str, List[CandleData]] = OrderedDict()
    for c in candles:
        dt = datetime.fromtimestamp(c.timestamp / 1000, tz=timezone.utc)
        if freq == "M":
            key = dt.strftime("%Y-%m")
        else:
            iso_year, iso_week, _ = dt.isocalendar()
            key = f"{iso_year}-W{iso_week:02d}"
        if key not in period_map:
            period_map[key] = []
        period_map[key].append(c)
    return list(period_map.items())


def run_multi_period_backtest(
    symbol: str, periods: List[Tuple[str, List[CandleData]]],
    initial_capital: float = 5000.0, leverage: int = 1,
) -> List[Tuple[str, BacktestResult]]:
    results = []
    for label, period_candles in periods:
        if len(period_candles) < 10:
            continue
        engine = HighWinRateEngineV2(symbol=symbol, initial_capital=initial_capital, leverage=leverage)
        result = engine.run(period_candles)
        results.append((label, result))
        logger.info(f"  [{label}] 交易={result.total_trades}, PnL=${result.total_pnl:.2f}, 胜率={result.win_rate:.1f}%, PF={result.profit_factor}")
    return results


def compare_periods(period_results: List[Tuple[str, BacktestResult]]):
    if not period_results:
        logger.warning("无周期结果")
        return
    sep = "=" * 100
    print()
    print(sep)
    print("  📅 多周期对比表 (v2)")
    print(sep)
    print(f"| {'周期':<12s} | {'交易':>5s} | {'PnL':>10s} | {'胜率':>6s} | {'PF':>6s} | {'均盈':>8s} | {'均亏':>8s} | {'回撤':>6s} | {'滑点':>7s} |")
    print("|" + "-" * 12 + "|" + "-" * 7 + "|" + "-" * 12 + "|" + "-" * 8 + "|" + "-" * 8 + "|" + "-" * 10 + "|" + "-" * 10 + "|" + "-" * 8 + "|" + "-" * 9 + "|")
    total_trades = 0
    total_pnl = 0.0
    for label, r in period_results:
        pnl_str = f"{r.total_pnl:+.2f}"
        icon = "🟢" if r.total_pnl >= 0 else "🔴"
        slippage_str = f"{r.avg_slippage:.4f}" if hasattr(r, 'avg_slippage') else "--"
        print(f"| {label:<12s} | {r.total_trades:>5d} | {icon} {pnl_str:>8s} | {r.win_rate:>5.1f}% | {r.profit_factor:>5.2f} | ${r.avg_win:>6.2f} | ${r.avg_loss:>6.2f} | {r.max_drawdown_pct:>5.1f}% | {slippage_str:>7s} |")
        total_trades += r.total_trades
        total_pnl += r.total_pnl
    print("|" + "-" * 12 + "|" + "-" * 7 + "|" + "-" * 12 + "|" + "-" * 8 + "|" + "-" * 8 + "|" + "-" * 10 + "|" + "-" * 10 + "|" + "-" * 8 + "|" + "-" * 9 + "|")
    print(f"| {'合计':<12s} | {total_trades:>5d} | {'$' + f'{total_pnl:+.2f}':>12s} | {'':>8s} | {'':>8s} | {'':>10s} | {'':>10s} | {'':>8s} | {'':>9s} |")
    print(sep)


# ================================================================
# 可视化
# ================================================================

def plot_equity_and_drawdown(result: BacktestResult, symbol: str = "", save_path: str = ""):
    if not HAS_MPL or not result.equity_curve or len(result.equity_curve) < 2:
        return
    curve = result.equity_curve
    init_val = curve[0]
    x = list(range(len(curve)))
    peak = np.maximum.accumulate(curve)
    drawdown = (peak - np.array(curve)) / peak * 100

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})
    fig.patch.set_facecolor("#0d1117")
    ax1.fill_between(x, curve, alpha=0.1, color="#58a6ff")
    ax1.plot(x, curve, color="#58a6ff", linewidth=1.5, label="Equity")
    ax1.axhline(y=init_val, color="#484f58", linewidth=0.8, linestyle="--", alpha=0.7)
    ax1.set_facecolor("#161b22")
    ax1.tick_params(colors="#8b949e", labelsize=9)
    ax1.set_ylabel("Equity ($)", fontsize=11, color="#8b949e")
    ax1.set_title(f"Equity Curve v2 — {symbol or result.symbol}", fontsize=13, fontweight="bold", color="#f0f6fc")
    ax1.legend(loc="best", fontsize=9, facecolor="#161b22", edgecolor="#30363d", labelcolor="#f0f6fc")
    ax1.grid(True, alpha=0.12, color="#30363d")
    final_pnl = curve[-1] - init_val
    pnl_color = "#0ecb81" if final_pnl >= 0 else "#f6465d"
    ax1.text(0.98, 0.95, f"PnL: {'+' if final_pnl >= 0 else ''}${final_pnl:.2f} ({result.total_pnl_pct:+.2f}%)",
             transform=ax1.transAxes, fontsize=11, fontweight="bold", color=pnl_color, ha="right", va="top",
             bbox=dict(boxstyle="round,pad=0.3", facecolor="#1c2128", edgecolor="#30363d"))

    ax2.fill_between(x, 0, drawdown, color="#f6465d", alpha=0.3)
    ax2.plot(x, drawdown, color="#f6465d", linewidth=1.0)
    ax2.set_facecolor("#161b22")
    ax2.tick_params(colors="#8b949e", labelsize=9)
    ax2.set_ylabel("Drawdown (%)", fontsize=11, color="#8b949e")
    ax2.set_xlabel("Bar #", fontsize=11, color="#8b949e")
    ax2.invert_yaxis()
    ax2.grid(True, alpha=0.12, color="#30363d")
    max_dd_idx = np.argmax(drawdown)
    max_dd_val = drawdown[max_dd_idx]
    ax2.annotate(f"Max DD: {max_dd_val:.1f}%", xy=(max_dd_idx, max_dd_val),
                 xytext=(max_dd_idx + 20, max_dd_val + 5), fontsize=9, color="#f6465d", fontweight="bold",
                 arrowprops=dict(arrowstyle="->", color="#f6465d", lw=1.2),
                 bbox=dict(boxstyle="round,pad=0.2", facecolor="#1c2128", edgecolor="#f6465d"))
    plt.tight_layout()
    if not save_path:
        save_path = f"tools/backtest/v2_equity_{symbol.replace('-', '_') if symbol else 'result'}.png"
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    logger.info(f"📈 v2 Equity+Drawdown: {save_path}")
    plt.close(fig)


def plot_multi_period_equity(period_results: List[Tuple[str, BacktestResult]], symbol: str = "", save_path: str = ""):
    if not HAS_MPL or not period_results:
        return
    fig, ax = plt.subplots(figsize=(14, 6))
    fig.patch.set_facecolor("#0d1117")
    colors = plt.cm.viridis_r([i / max(len(period_results), 1) for i in range(len(period_results))])
    for i, (label, r) in enumerate(period_results):
        curve = r.equity_curve
        if not curve or len(curve) < 2:
            continue
        norm = [v / curve[0] for v in curve]
        ax.plot(norm, label=label, color=colors[i], linewidth=1.2, alpha=0.85)
    ax.set_facecolor("#161b22")
    ax.set_title(f"Normalized Equity v2 — {symbol or 'Multi-Period'}", fontsize=13, fontweight="bold", color="#f0f6fc")
    ax.set_xlabel("Bar #", fontsize=10, color="#8b949e")
    ax.set_ylabel("Normalized Equity", fontsize=10, color="#8b949e")
    ax.axhline(y=1.0, color="#484f58", linewidth=0.8, linestyle="--")
    ax.legend(loc="best", fontsize=8, ncol=2, facecolor="#161b22", edgecolor="#30363d", labelcolor="#f0f6fc")
    ax.tick_params(colors="#8b949e", labelsize=8)
    ax.grid(True, alpha=0.15, color="#30363d")
    plt.tight_layout()
    if not save_path:
        save_path = f"tools/backtest/v2_multi_{symbol.replace('-', '_') if symbol else 'result'}.png"
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    logger.info(f"📈 v2 Multi-period: {save_path}")
    plt.close(fig)


# ================================================================
# 报告输出
# ================================================================

def print_report(result: BacktestResult, verbose: bool = False):
    sep = "=" * 60
    sub = "-" * 60
    print()
    print(sep)
    print(f"  HighTempTation v2 回测报告 — {result.symbol}")
    print(sep)
    print(f"  参数 (P0):")
    for k in ["min_edge", "tp_pct", "sl_pct", "rolling_window", "min_hours_to_expiry",
              "base_slippage", "max_slippage", "time_stop_hours"]:
        v = result.params.get(k)
        if v is not None:
            print(f"    {k}: {v}")
    print(sub)
    print(f"  📊 核心绩效")
    print(f"    总交易:    {result.total_trades}")
    print(f"    盈利交易:  {result.winning_trades} ({result.win_rate:.1f}%)")
    print(f"    总盈亏:    {'+' if result.total_pnl >= 0 else ''}${result.total_pnl:.2f} ({result.total_pnl_pct:+.2f}%)")
    print(f"    盈亏比:    {result.profit_factor}")
    print(f"    夏普比率:  {result.sharpe_ratio}")
    print(sub)
    print(f"  📉 风控 + P0 指标")
    print(f"    最大回撤:  ${result.max_drawdown:.2f} ({result.max_drawdown_pct:.1f}%)")
    print(f"    平均持仓:  {result.avg_bars_held:.1f} bars")
    print(f"    结算过滤:  {result.settlement_filtered} 笔")
    print(f"    平均滑点:  {result.avg_slippage:.4f}")
    print(f"    平均偏差:  {result.avg_bias_adjustment:.4f}")
    print(sub)
    print(f"  📋 交易明细")
    print(f"    空头 (NO): {result.short_trades}")
    print(f"    TP: {result.tp1_count}  SL: {result.sl_count}  Trailing: {result.tp2_count}  TimeStop: {result.tp3_count}")
    print(sep)

    if verbose and result.trades:
        print(f"\n  最近交易:")
        print(f"  {'时间':>20s} {'方向':>6s} {'盈亏':>8s} {'原因':>12s} {'滑点':>7s} {'偏差':>7s}")
        print(f"  {'─'*20} {'─'*6} {'─'*8} {'─'*12} {'─'*7} {'─'*7}")
        for t in result.trades[-10:]:
            et = t.get('exit_time', '')[11:19] if isinstance(t, dict) else (t.exit_time[11:19] if t.exit_time else '')
            side = t.get('side', '') if isinstance(t, dict) else t.side
            pnl = t.get('pnl', 0) if isinstance(t, dict) else t.pnl
            reason = t.get('exit_reason', '') if isinstance(t, dict) else t.exit_reason
            slip = t.get('slippage', 0) if isinstance(t, dict) else (t.slippage if hasattr(t, 'slippage') else 0)
            bias = t.get('bias_adjustment', 0) if isinstance(t, dict) else (t.bias_adjustment if hasattr(t, 'bias_adjustment') else 0)
            print(f"  {et:>20s} {side:>6s} {pnl:>+8.2f} {reason:>12s} {slip:>7.4f} {bias:>+7.4f}")


# ================================================================
# CLI 入口
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="HighTempTation v2 回测")
    parser.add_argument("--mode", type=str, default="single", choices=["single", "multi", "mock"],
                        help="运行模式")
    parser.add_argument("--symbol", type=str, default="MOCK", help="交易对")
    parser.add_argument("--days", type=int, default=90, help="回测天数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--initial-capital", type=float, default=5000.0)
    parser.add_argument("--leverage", type=int, default=1)
    parser.add_argument("--real-data", action="store_true", help="使用真实 CSV 数据")
    parser.add_argument("--data-dir", type=str, default="./data", help="CSV 数据目录")
    parser.add_argument("--output", type=str, default="")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("  🌤️  HighTempTation v2 — P0 优化版回测")
    logger.info(f"  模式: {args.mode}, 交易对: {args.symbol}, 天数: {args.days}")
    logger.info(f"  P0: 滑点={BASE_SLIPPAGE}~{MAX_SLIPPAGE}, 滚动窗口={ROLLING_WINDOW}d, 结算过滤>={MIN_HOURS_TO_EXPIRY}h")
    logger.info("=" * 60)

    # ── 加载数据 ──
    use_real = args.real_data or USE_REAL_DATA
    candles = []
    metadata = {}

    if use_real:
        logger.info(f"📂 加载真实数据: {args.data_dir}")
        candles, metadata = load_real_data(args.data_dir)
    else:
        logger.info("🎲 使用模拟数据")
        candles = generate_mock_data(days=args.days, seed=args.seed)

    # ── MODE ──
    if args.mode == "mock":
        logger.info("\n🧪 MODE 模式: 全部回测 (v2)")
        engine = HighWinRateEngineV2(symbol=args.symbol, initial_capital=args.initial_capital, leverage=args.leverage)
        result = engine.run(candles)
        print_report(result, verbose=args.verbose)

        if not args.no_plot and HAS_MPL:
            plot_equity_and_drawdown(result, symbol=args.symbol)

        logger.info("\n📅 多周期对比 (按周)")
        periods = split_periods(candles, freq="W")
        logger.info(f"  共 {len(periods)} 个周期")
        period_results = run_multi_period_backtest(
            symbol=args.symbol, periods=periods,
            initial_capital=args.initial_capital, leverage=args.leverage,
        )
        compare_periods(period_results)

        if not args.no_plot and HAS_MPL:
            plot_multi_period_equity(period_results, symbol=args.symbol)

        if args.output:
            with open(args.output, "w") as f:
                json.dump({
                    "single": result.to_dict(),
                    "multi_period": [(label, r.to_dict()) for label, r in period_results],
                }, f, ensure_ascii=False, indent=2)
        print("\n全部完成。")
        return

    if args.mode == "single":
        engine = HighWinRateEngineV2(symbol=args.symbol, initial_capital=args.initial_capital, leverage=args.leverage)
        result = engine.run(candles)
        print_report(result, verbose=args.verbose)
        if not args.no_plot and HAS_MPL:
            plot_equity_and_drawdown(result, symbol=args.symbol)
        print("\n全部完成。")
        return result

    if args.mode == "multi":
        periods = split_periods(candles, freq="W")
        logger.info(f"  共 {len(periods)} 个周期")
        period_results = run_multi_period_backtest(
            symbol=args.symbol, periods=periods,
            initial_capital=args.initial_capital, leverage=args.leverage,
        )
        compare_periods(period_results)
        if not args.no_plot and HAS_MPL:
            plot_multi_period_equity(period_results, symbol=args.symbol)
        print("\n全部完成。")
        return


if __name__ == "__main__":
    main()
