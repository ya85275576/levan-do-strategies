#!/usr/bin/env python3
"""
HighTempTation 天气校准套利 - 完整可运行版本

高胜率回测框架，包含：
  - 模拟数据生成 (generate_mock_data)
  - 高胜率开平仓逻辑 (MIN_EDGE=0.20, TP=9%, SL=6.5%, 移动止盈)
  - force_close_all 强制平仓
  - 多周期对比 (按周/月切分)
  - 资金曲线+回撤可视化 (matplotlib)
  - MODE / 单周期 / 多周期 三种入口

用法:
  python tools/backtest/hightemptation_full.py              # MODE 模式（默认使用模拟数据）
  python tools/backtest/hightemptation_full.py --mode single # 单周期模式
  python tools/backtest/hightemptation_full.py --mode multi  # 多周期对比模式
  python tools/backtest/hightemptation_full.py --symbol BTC-USDT  # 指定交易对（从OKX取真实数据）
  python tools/backtest/hightemptation_full.py --mock-only   # 只使用模拟数据
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
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

# ── matplotlib 导入（延迟加载避免无头环境报错） ──
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
logger = logging.getLogger("hightemptation")


# ================================================================
# 模拟数据生成
# ================================================================

def generate_mock_data(
    days: int = 90,
    timeframe_min: int = 15,
    base_price: float = 50000.0,
    volatility: float = 0.015,
    trend: float = 0.0002,
    seed: int = 42,
) -> List["CandleData"]:
    """
    生成模拟 K 线数据（布朗运动 + 趋势 + 随机波动）。

    :param days: 生成天数
    :param timeframe_min: K 线周期（分钟）
    :param base_price: 起始价格
    :param volatility: 每日波动率
    :param trend: 每日趋势偏移
    :param seed: 随机种子
    :returns: List[CandleData]
    """
    random.seed(seed)
    np.random.seed(seed)

    # 从 dataclass 导入
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
        # 随机游走
        ret = np.random.normal(trend / bars_per_day, volatility / math.sqrt(bars_per_day))
        open_price = price
        close_price = price * (1 + ret)
        high_price = max(open_price, close_price) * (1 + abs(np.random.normal(0, volatility * 0.3)))
        low_price = min(open_price, close_price) * (1 - abs(np.random.normal(0, volatility * 0.3)))
        volume = abs(np.random.normal(1000, 300))

        candles.append(MockCandle(
            timestamp=ts,
            open=round(open_price, 2),
            high=round(high_price, 2),
            low=round(low_price, 2),
            close=round(close_price, 2),
            volume=round(volume, 2),
        ))

        price = close_price

    logger.info(f"模拟数据生成: {len(candles)} 根 K 线, 起始 ${base_price:.2f}, 结束 ${price:.2f}, {days} 天")
    return candles


# ================================================================
# 回测数据结构
# ================================================================

@dataclass
class CandleData:
    """单根 K 线数据"""
    timestamp: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0


@dataclass
class BacktestTrade:
    """一笔完整的交易记录"""
    symbol: str
    side: str                     # "long" | "short" | "NO" | "YES"
    entry_time: str               # ISO timestamp
    exit_time: str                # ISO timestamp
    entry_price: float
    exit_price: float
    size: float                   # 交易数量
    pnl: float                    # 盈亏（美元）
    pnl_pct: float                # 盈亏百分比
    leverage: int = 1
    exit_reason: str = ""         # "TP" | "SL" | "Trailing" | "TimeStop" | "Manual" | "ForceClose"
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    tp3_price: float = 0.0
    sl_price: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    bars_held: int = 0            # 持仓 K 线数
    max_pnl_pct: float = 0.0      # 持仓期间最大浮盈百分比


@dataclass
class BacktestResult:
    """回测结果汇总"""
    symbol: str
    params: dict
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

    def to_dict(self):
        d = asdict(self)
        if len(d["trades"]) > 100:
            d["trades"] = d["trades"][-100:]
        return d


# ================================================================
# 高胜率版开平仓引擎
# ================================================================

class HighWinRateEngine:
    """
    高胜率版开平仓逻辑，核心参数：
      - MIN_EDGE = 0.20
      - TP = 9%, SL = 6.5%
      - 移动止盈: 浮盈 ≥5% 启动，回撤 3% 平仓
      - 价格过滤 0.28–0.72（价格在区间中间位置）
      - 模型概率 |p-0.5| ≥ 0.12（由 RSI 映射）
      - ALLOWED_SIDES = ["NO"]
      - 时间止损 24h
      - 每仓 $1, 最大 50 仓
    """

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
    RSI_LENGTH = 7
    RSI_TOP = 45
    RSI_BOT = 10

    def __init__(self, symbol: str = "MOCK", initial_capital: float = 5000.0, leverage: int = 1):
        self.symbol = symbol
        self.initial_capital = initial_capital
        self.leverage = leverage
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = [initial_capital]
        self.equity = initial_capital
        self._min_price: float = float("inf")
        self._max_price: float = 0.0
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
        self.total_signals: int = 0

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
        # 简化 RSI 计算
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

    def try_open_positions(self, candle: CandleData, edge: float, model_prob: float, price_in_range: bool) -> bool:
        """
        尝试开仓。条件:
          - price_in_range (0.28-0.72)
          - model_prob 通过 |p-0.5| >= 0.12
          - edge >= MIN_EDGE
          - 只能开 ALLOWED_SIDES
          - 已有持仓不开
        """
        if self._position_side:
            return False
        if not price_in_range:
            return False
        if not self._check_prob_filter(model_prob):
            return False
        if edge < self.MIN_EDGE:
            return False

        # ALLOWED_SIDES = ["NO"] → 只开空头
        self._position_side = "NO"
        self._entry_price = candle.close
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
            f"  🟢 [{self.symbol}] 开仓 NO @ {candle.close:.4f}, "
            f"edge={edge:.3f}, prob={model_prob:.3f}"
        )
        return True

    def check_and_close_positions(self, candle: CandleData) -> Optional[str]:
        """
        检查并平仓。返回平仓原因或 None。

        优先级: TP > SL > Trailing > TimeStop
        """
        if not self._position_side:
            return None

        self._bars_held += 1
        current_price = candle.close

        # 空头: 价格下跌才盈利，peak_price 跟踪最低价
        if current_price < self._peak_price:
            self._peak_price = current_price

        # 计算当前浮盈 %
        pnl_pct = (self._entry_price - current_price) / self._entry_price * 100

        # 更新最大浮盈
        if pnl_pct > self._max_pnl_pct:
            self._max_pnl_pct = pnl_pct

        # ---- 止盈 9% ----
        if pnl_pct >= self.TP_PCT:
            self._close_trade(current_price, candle.timestamp, "TP")
            return "TP"

        # ---- 止损 6.5% ----
        if pnl_pct <= -self.SL_PCT:
            self._close_trade(current_price, candle.timestamp, "SL")
            return "SL"

        # ---- 移动止盈 ----
        if self._max_pnl_pct >= self.TRAILING_ACTIVATE:
            drawdown_from_peak = self._max_pnl_pct - pnl_pct
            if drawdown_from_peak >= self.TRAILING_DRAWDOWN:
                self._close_trade(current_price, candle.timestamp, "Trailing")
                return "Trailing"

        # ---- 时间止损 24h ----
        elapsed_hours = (candle.timestamp - self._entry_ms) / 3600000
        if elapsed_hours >= self.TIME_STOP_HOURS:
            self._close_trade(current_price, candle.timestamp, "TimeStop")
            return "TimeStop"

        return None

    def force_close_all(self, candle: Optional[CandleData] = None) -> int:
        """
        强制平仓所有持仓（用于收盘/异常终止）。

        :param candle: 用此 K 线收盘价平仓，None 则用最新价
        :returns: 平仓数量 (0 或 1)
        """
        if not self._position_side:
            return 0

        exit_price = candle.close if candle else self._entry_price
        exit_ts = candle.timestamp if candle else int(time.time() * 1000)
        self._close_trade(exit_price, exit_ts, "ForceClose")
        return 1

    def _close_trade(self, exit_price: float, timestamp: int, reason: str):
        """平仓并记录交易"""
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
        )
        self.trades.append(trade)

        logger.debug(
            f"  🔴 [{self.symbol}] 平仓 {reason}: "
            f"入场={self._entry_price:.4f} → 出场={exit_price:.4f}, "
            f"PnL={pnl:.2f} ({pnl_pct:+.2f}%), "
            f"maxPct={self._max_pnl_pct:.2f}%"
        )

        self._position_side = None
        self._position_size = 0.0
        self._max_pnl_pct = 0.0

    # ================================================================
    # 运行回测
    # ================================================================

    def run(self, candles: List[CandleData]) -> BacktestResult:
        """在 K 线数据上运行回测"""
        if len(candles) < 20:
            logger.error(f"K 线不足: {len(candles)}")
            return self._build_result()

        self._update_price_range(candles)

        tp_count = 0
        sl_count = 0
        manual_count = 0
        trailing_count = 0
        time_stop_count = 0
        force_close_count = 0

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
            prob_ok = self._check_prob_filter(model_prob)
            edge = abs(model_prob - 0.5)

            # 开仓
            self.try_open_positions(candle, edge, model_prob, price_in_range)

            # 平仓检查
            if self._position_side:
                reason = self.check_and_close_positions(candle)
                if reason == "TP":
                    tp_count += 1
                elif reason == "SL":
                    sl_count += 1
                elif reason == "Trailing":
                    trailing_count += 1
                elif reason == "TimeStop":
                    time_stop_count += 1

            # 更新权益
            if self._position_side:
                if self._position_side == "NO":
                    upnl = (self._entry_price - current_price) * self._position_size
                else:
                    upnl = (current_price - self._entry_price) * self._position_size
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

        # 收盘强制平仓
        if self._position_side:
            self.force_close_all(candles[-1])
            force_close_count += 1

        # 汇总
        result = self._build_result()
        short_trades = 0
        for t in self.trades:
            if t.side == "NO" or t.side == "short":
                short_trades += 1
            if t.exit_reason == "ForceClose":
                force_close_count += 1
            elif t.exit_reason == "Manual":
                manual_count += 1

        result.long_trades = len(self.trades) - short_trades
        result.short_trades = short_trades
        result.tp1_count = tp_count
        result.sl_count = sl_count
        result.manual_count = manual_count + force_close_count
        result.tp2_count = trailing_count
        result.tp3_count = time_stop_count
        result.equity_curve = self.equity_curve
        result.max_drawdown = round(max_drawdown, 2)
        result.max_drawdown_pct = round(
            (max_drawdown / peak_equity * 100) if peak_equity > 0 else 0, 2
        )

        # Sharpe Ratio
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
                std = math.sqrt(var) if var > 0 else 1e-10
                result.sharpe_ratio = round(avg_r / std * math.sqrt(365 * 24 * 4), 2)

        logger.info(
            f"[{self.symbol}] 回测完成: "
            f"{result.total_trades} 笔, "
            f"PnL=${result.total_pnl:.2f}, "
            f"胜率={result.win_rate:.1f}%, "
            f"回撤={result.max_drawdown_pct:.1f}%"
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
                "mode": "high_win",
                "min_edge": self.MIN_EDGE,
                "tp_pct": self.TP_PCT,
                "sl_pct": self.SL_PCT,
                "trailing_activate": self.TRAILING_ACTIVATE,
                "trailing_drawdown": self.TRAILING_DRAWDOWN,
                "price_low": self.PRICE_LOW,
                "price_high": self.PRICE_HIGH,
                "min_prob_edge": self.MIN_PROB_EDGE,
                "allowed_sides": list(self.ALLOWED_SIDES),
                "time_stop_hours": self.TIME_STOP_HOURS,
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
    """按周/月切分 K 线"""
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
    symbol: str,
    periods: List[Tuple[str, List[CandleData]]],
    initial_capital: float = 5000.0,
    leverage: int = 1,
) -> List[Tuple[str, BacktestResult]]:
    """逐周期独立回测"""
    results = []
    for label, period_candles in periods:
        if len(period_candles) < 10:
            continue
        engine = HighWinRateEngine(symbol=symbol, initial_capital=initial_capital, leverage=leverage)
        result = engine.run(period_candles)
        results.append((label, result))
        logger.info(f"  [{label}] 交易={result.total_trades}, PnL=${result.total_pnl:.2f}, 胜率={result.win_rate:.1f}%, PF={result.profit_factor}")
    return results


def compare_periods(period_results: List[Tuple[str, BacktestResult]]):
    """打印多周期对比表"""
    if not period_results:
        logger.warning("无周期结果")
        return
    sep = "=" * 90
    print()
    print(sep)
    print("  📅 多周期对比表")
    print(sep)
    print(f"| {'周期':<12s} | {'交易':>5s} | {'PnL':>10s} | {'胜率':>6s} | {'PF':>6s} | {'均盈':>8s} | {'均亏':>8s} | {'回撤':>6s} |")
    print("|" + "-" * 12 + "|" + "-" * 7 + "|" + "-" * 12 + "|" + "-" * 8 + "|" + "-" * 8 + "|" + "-" * 10 + "|" + "-" * 10 + "|" + "-" * 8 + "|")
    total_trades = 0
    total_pnl = 0.0
    for label, r in period_results:
        pnl_str = f"{r.total_pnl:+.2f}"
        icon = "🟢" if r.total_pnl >= 0 else "🔴"
        print(f"| {label:<12s} | {r.total_trades:>5d} | {icon} {pnl_str:>8s} | {r.win_rate:>5.1f}% | {r.profit_factor:>5.2f} | ${r.avg_win:>6.2f} | ${r.avg_loss:>6.2f} | {r.max_drawdown_pct:>5.1f}% |")
        total_trades += r.total_trades
        total_pnl += r.total_pnl
    print("|" + "-" * 12 + "|" + "-" * 7 + "|" + "-" * 12 + "|" + "-" * 8 + "|" + "-" * 8 + "|" + "-" * 10 + "|" + "-" * 10 + "|" + "-" * 8 + "|")
    print(f"| {'合计':<12s} | {total_trades:>5d} | {'$' + f'{total_pnl:+.2f}':>12s} | {'':>8s} | {'':>8s} | {'':>10s} | {'':>10s} | {'':>8s} |")
    print(sep)


# ================================================================
# 可视化
# ================================================================

def plot_equity_and_drawdown(
    result: BacktestResult,
    symbol: str = "",
    save_path: str = "",
):
    """
    绘制资金曲线 + 下方回撤图（双轴）。

    :param result: BacktestResult
    :param symbol: 交易对名称
    :param save_path: 保存路径，空则自动生成
    """
    if not HAS_MPL:
        logger.warning("matplotlib 未安装，跳过图表")
        return
    if not result.equity_curve or len(result.equity_curve) < 2:
        logger.warning("权益曲线数据不足，跳过图表")
        return

    curve = result.equity_curve
    init_val = curve[0] if curve else 1.0
    x = list(range(len(curve)))

    # 计算回撤
    peak = np.maximum.accumulate(curve)
    drawdown = (peak - np.array(curve)) / peak * 100

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})
    fig.patch.set_facecolor("#0d1117")

    # ── 上图：资金曲线 ──
    ax1.fill_between(x, curve, alpha=0.1, color="#58a6ff")
    ax1.plot(x, curve, color="#58a6ff", linewidth=1.5, label="Equity")
    ax1.axhline(y=init_val, color="#484f58", linewidth=0.8, linestyle="--", alpha=0.7)
    ax1.set_facecolor("#161b22")
    ax1.tick_params(colors="#8b949e", labelsize=9)
    ax1.set_ylabel("Equity ($)", fontsize=11, color="#8b949e")
    ax1.set_title(f"Equity Curve — {symbol or result.symbol}", fontsize=13, fontweight="bold", color="#f0f6fc")
    ax1.legend(loc="best", fontsize=9, facecolor="#161b22", edgecolor="#30363d", labelcolor="#f0f6fc")
    ax1.grid(True, alpha=0.12, color="#30363d")

    # 标注最终 PnL
    final_pnl = curve[-1] - init_val
    pnl_color = "#0ecb81" if final_pnl >= 0 else "#f6465d"
    ax1.text(
        0.98, 0.95,
        f"PnL: {'+' if final_pnl >= 0 else ''}${final_pnl:.2f} ({result.total_pnl_pct:+.2f}%)",
        transform=ax1.transAxes, fontsize=11, fontweight="bold",
        color=pnl_color, ha="right", va="top",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#1c2128", edgecolor="#30363d"),
    )

    # ── 下图：回撤 ──
    ax2.fill_between(x, 0, drawdown, color="#f6465d", alpha=0.3)
    ax2.plot(x, drawdown, color="#f6465d", linewidth=1.0)
    ax2.set_facecolor("#161b22")
    ax2.tick_params(colors="#8b949e", labelsize=9)
    ax2.set_ylabel("Drawdown (%)", fontsize=11, color="#8b949e")
    ax2.set_xlabel("Bar #", fontsize=11, color="#8b949e")
    ax2.invert_yaxis()
    ax2.grid(True, alpha=0.12, color="#30363d")

    # 标注最大回撤
    max_dd_idx = np.argmax(drawdown)
    max_dd_val = drawdown[max_dd_idx]
    ax2.annotate(
        f"Max DD: {max_dd_val:.1f}%",
        xy=(max_dd_idx, max_dd_val),
        xytext=(max_dd_idx + 20, max_dd_val + 5),
        fontsize=9, color="#f6465d", fontweight="bold",
        arrowprops=dict(arrowstyle="->", color="#f6465d", lw=1.2),
        bbox=dict(boxstyle="round,pad=0.2", facecolor="#1c2128", edgecolor="#f6465d"),
    )

    plt.tight_layout()

    if not save_path:
        save_path = f"tools/backtest/equity_drawdown_{symbol.replace('-', '_') if symbol else 'result'}.png"
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    logger.info(f"📈 Equity+Drawdown chart: {save_path}")
    plt.close(fig)


def plot_multi_period_equity(
    period_results: List[Tuple[str, BacktestResult]],
    symbol: str = "",
    save_path: str = "",
):
    """多周期归一化资金曲线叠加图"""
    if not HAS_MPL:
        return
    if not period_results:
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
    ax.set_title(f"Normalized Equity — {symbol or 'Multi-Period'}", fontsize=13, fontweight="bold", color="#f0f6fc")
    ax.set_xlabel("Bar #", fontsize=10, color="#8b949e")
    ax.set_ylabel("Normalized Equity (start=1.0)", fontsize=10, color="#8b949e")
    ax.axhline(y=1.0, color="#484f58", linewidth=0.8, linestyle="--")
    ax.legend(loc="best", fontsize=8, ncol=2, facecolor="#161b22", edgecolor="#30363d", labelcolor="#f0f6fc")
    ax.tick_params(colors="#8b949e", labelsize=8)
    ax.grid(True, alpha=0.15, color="#30363d")

    plt.tight_layout()
    if not save_path:
        save_path = f"tools/backtest/multi_period_equity_{symbol.replace('-', '_') if symbol else 'result'}.png"
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    logger.info(f"📈 Multi-period equity chart: {save_path}")
    plt.close(fig)


# ================================================================
# 报告输出
# ================================================================

def print_report(result: BacktestResult, verbose: bool = False):
    sep = "=" * 60
    sub = "-" * 60
    print()
    print(sep)
    print(f"  HighTempTation 回测报告 — {result.symbol}")
    print(sep)
    print(f"  参数:")
    for k, v in result.params.items():
        print(f"    {k}: {v}")
    print(sub)
    print(f"  📊 核心绩效")
    print(f"    总交易:    {result.total_trades}")
    print(f"    盈利交易:  {result.winning_trades} ({result.win_rate:.1f}%)")
    print(f"    亏损交易:  {result.losing_trades} ({100 - result.win_rate:.1f}%)")
    print(f"    总盈亏:    {'+' if result.total_pnl >= 0 else ''}${result.total_pnl:.2f} ({result.total_pnl_pct:+.2f}%)")
    print(f"    平均盈利:  ${result.avg_win:.2f}")
    print(f"    平均亏损:  ${result.avg_loss:.2f}")
    print(f"    盈亏比:    {result.profit_factor}")
    print(f"    夏普比率:  {result.sharpe_ratio}")
    print(sub)
    print(f"  📉 风控指标")
    print(f"    最大回撤:  ${result.max_drawdown:.2f} ({result.max_drawdown_pct:.1f}%)")
    print(f"    平均持仓:  {result.avg_bars_held:.1f} 根 K 线")
    print(sub)
    print(f"  📋 交易明细")
    print(f"    空头交易 (NO): {result.short_trades}")
    print(f"    多头交易 (YES): {result.long_trades}")
    print(f"    TP:       {result.tp1_count}")
    print(f"    SL:       {result.sl_count}")
    print(f"    Trailing: {result.tp2_count}")
    print(f"    TimeStop: {result.tp3_count}")
    print(f"    ForceClose/Manual: {result.manual_count}")
    print(sep)

    if verbose and result.trades:
        print(f"\n  最近交易 (最多 10 笔):")
        print(f"  {'时间':>20s} {'方向':>6s} {'入场':>10s} {'出场':>10s} {'盈亏':>8s} {'原因':>12s}")
        print(f"  {'─'*20} {'─'*6} {'─'*10} {'─'*10} {'─'*8} {'─'*12}")
        for t in result.trades[-10:]:
            if isinstance(t, dict):
                et = t.get('exit_time', '')[11:19] if t.get('exit_time') else ''
                side = t.get('side', '')
                ep = t.get('entry_price', 0)
                xp = t.get('exit_price', 0)
                pnl = t.get('pnl', 0)
                reason = t.get('exit_reason', '')
            else:
                et = t.exit_time[11:19] if t.exit_time else ''
                side = t.side
                ep = t.entry_price
                xp = t.exit_price
                pnl = t.pnl
                reason = t.exit_reason
            print(f"  {et:>20s} {side:>6s} {ep:>10.2f} {xp:>10.2f} {pnl:>+8.2f} {reason:>12s}")


# ================================================================
# CLI 入口
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="HighTempTation 天气校准套利 - 完整回测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mode", type=str, default="single", choices=["single", "multi", "mock"],
                        help="运行模式: single=单周期, multi=多周期对比, mock=纯模拟")
    parser.add_argument("--symbol", type=str, default="MOCK", help="交易对 (默认 MOCK)")
    parser.add_argument("--days", type=int, default=90, help="回测天数 (默认 90)")
    parser.add_argument("--timeframe", type=str, default="15m", help="K 线周期 (默认 15m)")
    parser.add_argument("--mock-only", action="store_true", help="只使用模拟数据")
    parser.add_argument("--seed", type=int, default=42, help="模拟数据随机种子")
    parser.add_argument("--initial-capital", type=float, default=5000.0, help="初始资金")
    parser.add_argument("--leverage", type=int, default=1, help="杠杆倍数")
    parser.add_argument("--output", type=str, default="", help="输出文件路径")
    parser.add_argument("--verbose", "-v", action="store_true", help="显示交易明细")
    parser.add_argument("--no-plot", action="store_true", help="跳过图表生成")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("  🌤️  HighTempTation 天气校准套利 — 完整回测")
    logger.info(f"  模式: {args.mode}, 交易对: {args.symbol}, 天数: {args.days}")
    logger.info("=" * 60)

    # ── 获取数据 ──
    candles = []

    if not args.mock_only and args.symbol != "MOCK":
        # 从 OKX 获取真实数据
        try:
            import requests
            logger.info(f"📡 尝试从 OKX 获取 {args.symbol} 数据...")
            sym = args.symbol.replace("-", "").upper()
            params = {"instId": sym, "bar": args.timeframe, "limit": "300"}
            resp = requests.get(
                "https://www.okx.com/api/v5/market/history-candles",
                params=params, timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == "0":
                    for c in data.get("data", []):
                        candles.append(CandleData(
                            timestamp=int(c[0]), open=float(c[1]),
                            high=float(c[2]), low=float(c[3]),
                            close=float(c[4]), volume=float(c[5]),
                        ))
                    candles.sort(key=lambda x: x.timestamp)
                    logger.info(f"  OKX: {len(candles)} 根 K 线")
                else:
                    logger.warning(f"  OKX API 错误: {data.get('msg')}")
        except Exception as e:
            logger.warning(f"  OKX 获取失败: {e}")

    # 不足则使用模拟数据
    if len(candles) < 50:
        logger.info("🎲 使用模拟数据...")
        candles = generate_mock_data(
            days=args.days,
            seed=args.seed,
            base_price=50000.0 if args.symbol in ("MOCK", "BTC-USDT") else 100.0,
        )

    # ── MODE 模式选择 ──
    # 先尝试 mock 模式（默认用模拟数据跑高胜率 + 多周期）
    if args.mode == "mock":
        logger.info("\n🧪 MODE 模式: 模拟数据全部回测")
        logger.info(f"{'=' * 60}")

        # 1. 单周期回测
        logger.info("\n📊 单周期回测")
        engine = HighWinRateEngine(symbol=args.symbol, initial_capital=args.initial_capital, leverage=args.leverage)
        result = engine.run(candles)
        print_report(result, verbose=args.verbose)

        # 2. 资金曲线图
        if not args.no_plot and HAS_MPL:
            plot_equity_and_drawdown(result, symbol=args.symbol)

        # 3. 多周期对比
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

        # 4. JSON 导出
        if args.output:
            with open(args.output, "w") as f:
                json.dump({
                    "single": result.to_dict(),
                    "multi_period": [(label, r.to_dict()) for label, r in period_results],
                }, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 结果已导出: {args.output}")

        print("\n全部完成。")
        return

    # ── 单周期模式 ──
    if args.mode == "single":
        logger.info("\n📊 单周期回测")
        engine = HighWinRateEngine(symbol=args.symbol, initial_capital=args.initial_capital, leverage=args.leverage)
        result = engine.run(candles)
        print_report(result, verbose=args.verbose)

        if not args.no_plot and HAS_MPL:
            plot_equity_and_drawdown(result, symbol=args.symbol)

        if args.output:
            with open(args.output, "w") as f:
                json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)

        print("\n全部完成。")
        return result

    # ── 多周期对比模式 ──
    if args.mode == "multi":
        logger.info("\n📅 多周期对比")
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
                json.dump(
                    [(label, r.to_dict()) for label, r in period_results],
                    f, ensure_ascii=False, indent=2,
                )

        print("\n全部完成。")
        return


if __name__ == "__main__":
    main()
