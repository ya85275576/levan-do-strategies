#!/usr/bin/env python3
"""
LE VAN DO® Swing Signals — 回测框架

基于历史 K 线数据运行策略引擎，输出全面的绩效报告。
支持 50 个交易对、多参数优化、CSV/JSON 导出。

用法:
  # 回测单个交易对（默认参数）
  python backtest.py --symbol BTC-USDT

  # 回测多个交易对
  python backtest.py --symbols BTC-USDT,ETH-USDT,SOL-USDT

  # 自定义参数
  python backtest.py --symbol BTC-USDT --profit-factor 3.0 --stop-factor 1.5 --atr-length 14

  # 参数扫描优化
  python backtest.py --symbol BTC-USDT --optimize profit_factor:2.0,2.5,3.0 --optimize atr_length:14,20,26

  # 导出结果
  python backtest.py --symbol BTC-USDT --output backtest_results.json

  # 完整模式（全部交易对 + CSV 导出）
  python backtest.py --all-symbols --output results/ --csv

依赖:
  pip install requests pandas numpy
"""
import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

# 确保可以导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy import (
    CandleData,
    LeVanDoStrategy,
    SignalType,
    StrategyParams,
    TpSlLevels,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backtest")


# ================================================================
# 回测交易记录
# ================================================================

@dataclass
class BacktestTrade:
    """一笔完整的交易记录"""
    symbol: str
    side: str                     # "long" | "short"
    entry_time: str               # ISO timestamp
    exit_time: str                # ISO timestamp
    entry_price: float
    exit_price: float
    size: float                   # 交易数量
    pnl: float                    # 盈亏（美元）
    pnl_pct: float                # 盈亏百分比
    leverage: int = 1
    exit_reason: str = ""         # "TP1" | "TP2" | "TP3" | "SL" | "Manual"
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    tp3_price: float = 0.0
    sl_price: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    bars_held: int = 0            # 持仓 K 线数


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
        """序列化为字典"""
        d = asdict(self)
        # 只保留最近的 100 笔交易详情
        if len(d["trades"]) > 100:
            d["trades"] = d["trades"][-100:]
        return d


# ================================================================
# OKX 历史数据获取
# ================================================================

class OkxHistoryFetcher:
    """
    从 OKX REST API 获取历史 K 线数据（无需 API Key，公开数据）
    API: GET /api/v5/market/history-candles
    """

    REST_URL = "https://www.okx.com"

    # OKX 支持的 K 线周期（秒）
    TIMEFRAMES = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1H": 3600,
        "2H": 7200,
        "4H": 14400,
        "6H": 21600,
        "12H": 43200,
        "1D": 86400,
    }

    def __init__(self, symbol: str, timeframe: str = "15m", limit: int = 300):
        """
        :param symbol: 交易对，如 "BTC-USDT"
        :param timeframe: K 线周期，默认 "15m"
        :param limit: 获取的 K 线数量（最大 300）
        """
        self.symbol = symbol.replace("-", "").upper()  # OKX API 格式: BTCUSDT
        self.display_symbol = symbol
        self.timeframe = timeframe
        self.bar_sec = self.TIMEFRAMES.get(timeframe, 900)
        self.limit = min(limit, 300)

        # 缓存
        self._candles: List[CandleData] = []

    def fetch(self, before: Optional[int] = None, after: Optional[int] = None) -> List[CandleData]:
        """
        获取历史 K 线数据

        :param before: 获取此时间戳之前的 K 线（毫秒）
        :param after: 获取此时间戳之后的 K 线（毫秒）
        :returns: CandleData 列表，按时间升序排列
        """
        import requests

        params = {
            "instId": self.symbol,
            "bar": self.timeframe,
            "limit": str(self.limit),
        }
        if before:
            params["before"] = str(before)
        if after:
            params["after"] = str(after)

        url = f"{self.REST_URL}/api/v5/market/history-candles"
        headers = {"Accept": "application/json"}

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != "0":
                logger.error(f"[{self.display_symbol}] OKX API 错误: {data.get('msg')}")
                return self._candles

            raw_candles = data.get("data", [])
            if not raw_candles:
                logger.warning(f"[{self.display_symbol}] 无历史数据")
                return self._candles

            # OKX 返回: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
            candles = []
            for c in raw_candles:
                try:
                    candle = CandleData(
                        timestamp=int(c[0]),
                        open=float(c[1]),
                        high=float(c[2]),
                        low=float(c[3]),
                        close=float(c[4]),
                        volume=float(c[5]),
                    )
                    candles.append(candle)
                except (IndexError, ValueError) as e:
                    logger.warning(f"解析 K 线失败: {c} — {e}")

            # 按时间升序排列
            candles.sort(key=lambda x: x.timestamp)
            self._candles = candles
            logger.info(
                f"[{self.display_symbol}] 获取 {len(candles)} 根 K 线 "
                f"({self.timeframe}, {candles[0].timestamp} ~ {candles[-1].timestamp})"
            )
            return candles

        except Exception as e:
            logger.error(f"[{self.display_symbol}] 获取历史数据失败: {e}")
            return self._candles

    def fetch_range(self, days: int = 30) -> List[CandleData]:
        """
        获取最近指定天数的 K 线数据

        :param days: 天数
        """
        now_ms = int(time.time() * 1000)
        # 每次获取 300 条，分批获取
        all_candles = []
        cursor = now_ms
        seen_ts = set()

        max_batches = math.ceil(days * 86400 / (self.bar_sec * 300)) + 1
        for batch in range(max_batches):
            candles = self.fetch(before=cursor)
            if not candles:
                break

            # 去重
            new_count = 0
            for c in candles:
                if c.timestamp not in seen_ts:
                    seen_ts.add(c.timestamp)
                    all_candles.append(c)
                    new_count += 1

            logger.debug(f"  批次 {batch+1}: 新增 {new_count}/{len(candles)} 根 K 线")

            if new_count == 0:
                break

            # 更新游标到最早的时间戳
            cursor = candles[0].timestamp

            # 检查是否已覆盖足够的天数
            if all_candles and (now_ms - all_candles[0].timestamp) / 1000 > days * 86400:
                break

            time.sleep(0.2)  # 限速

        all_candles.sort(key=lambda x: x.timestamp)
        self._candles = all_candles
        logger.info(
            f"[{self.display_symbol}] 共获取 {len(all_candles)} 根 K 线 "
            f"(过去 {days} 天, {self.timeframe})"
        )
        return all_candles


# ================================================================
# 回测引擎
# ================================================================

class BacktestEngine:
    """
    回测引擎 — 在历史数据上运行策略，记录交易和权益曲线。
    """

    # 模拟参数（保持与 bot 一致）
    INITIAL_CAPITAL = 5000.0
    TRADE_QTY_PCT = 50.0  # 每次交易使用 50% 资金
    COMMISSION_PCT = 0.02  # 手续费 0.02%
    MAX_POSITIONS = 50  # 最大同时持仓数
    POSITION_SIZE_USD = 1.0  # 每仓 $1（硬性要求）

    def __init__(
        self,
        symbol: str,
        params: Optional[StrategyParams] = None,
        initial_capital: float = 5000.0,
        leverage: int = 1,
    ):
        self.symbol = symbol
        self.params = params or StrategyParams()
        self.initial_capital = initial_capital
        self.leverage = leverage
        self.strategy = LeVanDoStrategy(params=self.params)

        # 交易记录
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = [initial_capital]
        self.equity = initial_capital

        # 当前持仓状态（模拟）
        self._position_side: Optional[str] = None
        self._entry_price: float = 0.0
        self._entry_time: Optional[str] = None
        self._entry_bar: int = 0
        self._tp_prices: List[float] = [0.0, 0.0, 0.0]
        self._sl_price: float = 0.0
        self._tp_hit_level: int = 0
        self._max_price: float = 0.0
        self._min_price: float = 0.0
        self._bars_held: int = 0
        self._current_bar: int = 0
        self._position_size: float = 0.0

        # 统计
        self.total_signals: int = 0

    def _calculate_position_size(self, price: float) -> float:
        """
        计算仓位大小（保持每仓 $1，最大 50 仓）
        
        使用硬性要求：每仓 $1，最大 50 仓
        """
        if price <= 0:
            return 0.001
        qty = self.POSITION_SIZE_USD / price
        return round(qty, 8)

    def run(self, candles: List[CandleData]) -> BacktestResult:
        """
        在历史 K 线数据上运行回测

        :param candles: 升序排列的 K 线列表
        :returns: BacktestResult
        """
        if len(candles) < 10:
            logger.error(f"K 线数量不足: {len(candles)}")
            return self._build_result()

        # 统计用
        long_trades = 0
        short_trades = 0
        tp1_count = 0
        tp2_count = 0
        tp3_count = 0
        sl_count = 0
        manual_count = 0

        peak_equity = self.initial_capital
        max_drawdown = 0.0

        # ---- 逐根 K 线处理 ----
        for i, candle in enumerate(candles):
            self._current_bar = i

            # 更新策略引擎
            self.strategy.update_higher_tf_candle(candle)
            signal = self.strategy.analyze()
            current_price = candle.close

            if signal != SignalType.NONE:
                self.total_signals += 1

                # 根据信号类型处理
                self._handle_signal(signal, current_price, candle.timestamp)

            # 更新持仓中的最高/最低价（用于 MAE/MFE）
            if self._position_side:
                self._bars_held += 1
                
                if candle.high > self._max_price:
                    self._max_price = candle.high
                if candle.low < self._min_price or self._min_price == 0:
                    self._min_price = candle.low

                # 检查 TP/SL（策略引擎已处理，这里仅更新权益）
                # 权益随市价波动
                if self._position_side == "long":
                    unrealized_pnl = (current_price - self._entry_price) * self._position_size
                else:
                    unrealized_pnl = (self._entry_price - current_price) * self._position_size

                current_equity = self.initial_capital + sum(
                    t.pnl for t in self.trades
                ) + unrealized_pnl
                self.equity = current_equity

                # 跟踪回撤
                if current_equity > peak_equity:
                    peak_equity = current_equity
                dd = peak_equity - current_equity
                if dd > max_drawdown:
                    max_drawdown = dd
            else:
                self.equity = self.initial_capital + sum(t.pnl for t in self.trades)

            # 记录权益曲线（每根 K 线）
            self.equity_curve.append(round(self.equity, 2))

        # ---- 收盘时强制平仓 ----
        if self._position_side:
            last_candle = candles[-1]
            self._close_trade(
                exit_price=last_candle.close,
                exit_time=last_candle.timestamp,
                exit_reason="Manual",
            )
            manual_count += 1

        # ---- 统计数据 ----
        result = self._build_result()
        
        # 补充统计
        for t in self.trades:
            if t.side == "long":
                long_trades += 1
            else:
                short_trades += 1
            if t.exit_reason == "TP1": tp1_count += 1
            elif t.exit_reason == "TP2": tp2_count += 1
            elif t.exit_reason == "TP3": tp3_count += 1
            elif t.exit_reason == "SL": sl_count += 1
            elif t.exit_reason == "Manual": manual_count += 1

        result.long_trades = long_trades
        result.short_trades = short_trades
        result.tp1_count = tp1_count
        result.tp2_count = tp2_count
        result.tp3_count = tp3_count
        result.sl_count = sl_count
        result.manual_count = manual_count
        result.equity_curve = self.equity_curve
        result.max_drawdown = round(max_drawdown, 2)
        result.max_drawdown_pct = round(
            (max_drawdown / peak_equity * 100) if peak_equity > 0 else 0, 2
        )

        # Sharpe Ratio（简化版，以 K 线为周期）
        if len(self.equity_curve) > 1:
            returns = [
                (self.equity_curve[i] - self.equity_curve[i-1]) / self.equity_curve[i-1]
                for i in range(1, len(self.equity_curve))
                if self.equity_curve[i-1] > 0
            ]
            if returns:
                avg_return = sum(returns) / len(returns)
                variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
                std_dev = math.sqrt(variance)
                result.sharpe_ratio = round(
                    (avg_return / std_dev * math.sqrt(365 * 24 * 4)) if std_dev > 0 else 0,
                    2,
                )

        logger.info(
            f"[{self.symbol}] 回测完成: "
            f"{result.total_trades} 笔交易, "
            f"PnL=${result.total_pnl:.2f}, "
            f"胜率={result.win_rate:.1f}%, "
            f"最大回撤={result.max_drawdown_pct:.1f}%"
        )

        return result

    def _handle_signal(self, signal: SignalType, price: float, timestamp: int):
        """处理策略信号，执行模拟交易"""
        iso_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()

        if signal == SignalType.LONG_ENTRY:
            # 先平反向仓
            if self._position_side == "short":
                self._close_trade(price, timestamp, "Manual")
            # 开多仓
            self._position_side = "long"
            self._entry_price = price
            self._entry_time = iso_time
            self._entry_bar = self._current_bar
            self._position_size = self._calculate_position_size(price)
            self._tp_prices = [
                self.strategy.tp_sl.tp1,
                self.strategy.tp_sl.tp2,
                self.strategy.tp_sl.tp3,
            ]
            self._sl_price = self.strategy.tp_sl.sl
            self._tp_hit_level = 0
            self._max_price = price
            self._min_price = price
            self._bars_held = 0

        elif signal == SignalType.SHORT_ENTRY:
            if self._position_side == "long":
                self._close_trade(price, timestamp, "Manual")
            self._position_side = "short"
            self._entry_price = price
            self._entry_time = iso_time
            self._entry_bar = self._current_bar
            self._position_size = self._calculate_position_size(price)
            self._tp_prices = [
                self.strategy.tp_sl.tp1,
                self.strategy.tp_sl.tp2,
                self.strategy.tp_sl.tp3,
            ]
            self._sl_price = self.strategy.tp_sl.sl
            self._tp_hit_level = 0
            self._max_price = price
            self._min_price = price
            self._bars_held = 0

        elif signal in (SignalType.LONG_TP1, SignalType.SHORT_TP1):
            self._close_trade_partial(price, timestamp, "TP1")

        elif signal in (SignalType.LONG_TP2, SignalType.SHORT_TP2):
            self._close_trade_partial(price, timestamp, "TP2")

        elif signal in (SignalType.LONG_TP3, SignalType.SHORT_TP3):
            self._close_trade_partial(price, timestamp, "TP3")

        elif signal in (SignalType.LONG_SL, SignalType.SHORT_SL):
            self._close_trade(price, timestamp, "SL")

    def _close_trade(self, exit_price: float, timestamp: int, reason: str):
        """平仓（完整平仓）"""
        if not self._position_side:
            return

        iso_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()

        if self._position_side == "long":
            pnl = (exit_price - self._entry_price) * self._position_size
        else:
            pnl = (self._entry_price - exit_price) * self._position_size

        # 扣除手续费
        commission = self._position_size * exit_price * (self.COMMISSION_PCT / 100)
        pnl -= commission

        pnl_pct = (pnl / (self._entry_price * self._position_size)) * 100 if self._entry_price > 0 and self._position_size > 0 else 0

        # 计算 MAE/MFE
        if self._position_side == "long":
            mfe = (self._max_price - self._entry_price) * self._position_size
            mae = (self._entry_price - self._min_price) * self._position_size
        else:
            mfe = (self._entry_price - self._min_price) * self._position_size
            mae = (self._max_price - self._entry_price) * self._position_size

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
            tp1_price=round(self._tp_prices[0], 8) if self._tp_prices[0] else 0,
            tp2_price=round(self._tp_prices[1], 8) if self._tp_prices[1] else 0,
            tp3_price=round(self._tp_prices[2], 8) if self._tp_prices[2] else 0,
            sl_price=round(self._sl_price, 8) if self._sl_price else 0,
            max_favorable_excursion=round(mfe, 2),
            max_adverse_excursion=round(mae, 2),
            bars_held=self._bars_held,
        )
        self.trades.append(trade)
        self._position_side = None
        self._position_size = 0.0

    def _close_trade_partial(self, exit_price: float, timestamp: int, reason: str):
        """
        分批平仓（TP1/TP2/TP3）。
        
        根据 TP 批次平仓对应比例。
        """
        if not self._position_side:
            return

        # 确定平仓比例
        if reason == "TP1":
            pct = self.params.tp1_qty_pct / 100.0
            self._tp_hit_level = 1
        elif reason == "TP2":
            pct = self.params.tp2_qty_pct / 100.0
            self._tp_hit_level = 2
        elif reason == "TP3":
            pct = self.params.tp3_qty_pct / 100.0
            self._tp_hit_level = 3
        else:
            pct = 1.0

        # TP1 到达时，按比例平部分仓位
        close_qty = self._position_size * pct
        if close_qty <= 0:
            return

        iso_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()

        if self._position_side == "long":
            pnl = (exit_price - self._entry_price) * close_qty
        else:
            pnl = (self._entry_price - exit_price) * close_qty

        # 扣除手续费
        commission = close_qty * exit_price * (self.COMMISSION_PCT / 100)
        pnl -= commission

        pnl_pct = (pnl / (self._entry_price * close_qty)) * 100 if self._entry_price > 0 and close_qty > 0 else 0

        # 选择 TP 价格
        tp_price = 0.0
        if reason == "TP1":
            tp_price = self._tp_prices[0] if self._tp_prices[0] else exit_price
        elif reason == "TP2":
            tp_price = self._tp_prices[1] if self._tp_prices[1] else exit_price
        elif reason == "TP3":
            tp_price = self._tp_prices[2] if self._tp_prices[2] else exit_price

        trade = BacktestTrade(
            symbol=self.symbol,
            side=self._position_side,
            entry_time=self._entry_time or iso_time,
            exit_time=iso_time,
            entry_price=round(self._entry_price, 8),
            exit_price=round(tp_price, 8),
            size=round(close_qty, 8),
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 2),
            leverage=self.leverage,
            exit_reason=reason,
            tp1_price=round(self._tp_prices[0], 8) if self._tp_prices[0] else 0,
            tp2_price=round(self._tp_prices[1], 8) if self._tp_prices[1] else 0,
            tp3_price=round(self._tp_prices[2], 8) if self._tp_prices[2] else 0,
            sl_price=round(self._sl_price, 8) if self._sl_price else 0,
            max_favorable_excursion=0,
            max_adverse_excursion=0,
            bars_held=self._bars_held,
        )
        self.trades.append(trade)

        # 更新剩余仓位大小
        self._position_size -= close_qty

        # TP3 是最后一批，平仓完毕
        if reason == "TP3":
            self._position_side = None
            self._position_size = 0.0

    def _build_result(self) -> BacktestResult:
        """汇总回测结果"""
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
            params=asdict(self.params),
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
# 参数优化
# ================================================================

def optimize_params(
    symbol: str,
    candles: List[CandleData],
    param_grid: Dict[str, List],
    initial_capital: float = 5000.0,
    leverage: int = 1,
) -> List[Tuple[dict, BacktestResult]]:
    """
    参数扫描优化
    
    :param symbol: 交易对
    :param candles: 历史 K 线
    :param param_grid: 参数字典 {param_name: [value1, value2, ...]}
    :param initial_capital: 初始资金
    :param leverage: 杠杆倍数
    :returns: [(params, result), ...] 按 PnL 降序排列
    """
    from itertools import product

    # 构建参数组合
    param_names = list(param_grid.keys())
    param_values = list(param_grid.values())
    combinations = list(product(*param_values))

    logger.info(f"参数扫描: {len(combinations)} 种组合")
    results = []

    for i, combo in enumerate(combinations):
        combo_dict = dict(zip(param_names, combo))

        # 从默认参数开始，覆盖优化值
        params = StrategyParams()
        for k, v in combo_dict.items():
            if hasattr(params, k):
                setattr(params, k, v)

        engine = BacktestEngine(
            symbol=symbol,
            params=params,
            initial_capital=initial_capital,
            leverage=leverage,
        )
        result = engine.run(candles)

        results.append((combo_dict, result))
        logger.info(
            f"  [{i+1}/{len(combinations)}] {combo_dict} → "
            f"PnL=${result.total_pnl:.2f}, 胜率={result.win_rate:.1f}%, "
            f"交易={result.total_trades}, PF={result.profit_factor}"
        )

    # 按 PnL 降序排列
    results.sort(key=lambda x: x[1].total_pnl, reverse=True)
    return results


# ================================================================
# 报告输出
# ================================================================

def print_report(result: BacktestResult, verbose: bool = False):
    """打印回测报告"""
    sep = "=" * 60
    sub = "-" * 60

    lines = []
    lines.append("")
    lines.append(sep)
    lines.append(f"  LE VAN DO® 回测报告 — {result.symbol}")
    lines.append(sep)
    lines.append(f"  参数:")
    for k, v in result.params.items():
        if k in ("tps_type", "setup_type", "sideways_filter"):
            lines.append(f"    {k}: {v}")
    lines.append(f"    profit_factor: {result.params.get('profit_factor', 2.5)}")
    lines.append(f"    stop_factor: {result.params.get('stop_factor', 1.0)}")
    lines.append(f"    atr_length: {result.params.get('atr_length', 20)}")
    lines.append(sub)
    lines.append(f"  📊 核心绩效")
    lines.append(f"    总交易:    {result.total_trades}")
    lines.append(f"    盈利交易:  {result.winning_trades} ({result.win_rate:.1f}%)")
    lines.append(f"    亏损交易:  {result.losing_trades} ({100 - result.win_rate:.1f}%)")
    lines.append(f"    总盈亏:    {'+' if result.total_pnl >= 0 else ''}${result.total_pnl:.2f} ({result.total_pnl_pct:+.2f}%)")
    lines.append(f"    平均盈利:  ${result.avg_win:.2f}")
    lines.append(f"    平均亏损:  ${result.avg_loss:.2f}")
    lines.append(f"    盈亏比:    {result.profit_factor}")
    lines.append(f"    夏普比率:  {result.sharpe_ratio}")
    lines.append(sub)
    lines.append(f"  📉 风控指标")
    lines.append(f"    最大回撤:  ${result.max_drawdown:.2f} ({result.max_drawdown_pct:.1f}%)")
    lines.append(f"    平均持仓:  {result.avg_bars_held:.1f} 根 K 线")
    lines.append(sub)
    lines.append(f"  📋 交易明细")
    lines.append(f"    多头交易:  {result.long_trades}")
    lines.append(f"    空头交易:  {result.short_trades}")
    lines.append(f"    TP1:       {result.tp1_count}")
    lines.append(f"    TP2:       {result.tp2_count}")
    lines.append(f"    TP3:       {result.tp3_count}")
    lines.append(f"    SL:        {result.sl_count}")
    lines.append(f"    手动:      {result.manual_count}")
    lines.append(sep)

    print("\n".join(lines))

    if verbose and result.trades:
        print(f"\n  最近交易 (最多 10 笔):")
        print(f"  {'时间':>20s} {'方向':>6s} {'入场':>10s} {'出场':>10s} {'盈亏':>8s} {'原因':>6s}")
        print(f"  {'─'*20} {'─'*6} {'─'*10} {'─'*10} {'─'*8} {'─'*6}")
        for t in result.trades[-10:]:
            et = t.exit_time[11:19] if t.exit_time else ""
            lines = f"  {et:>20s} {t.side:>6s} {t.entry_price:>10.2f} {t.exit_price:>10.2f} {t.pnl:>+8.2f} {t.exit_reason:>6s}"
            print(lines)


def export_csv(results: List[BacktestResult], filepath: str):
    """导出为 CSV"""
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Symbol", "TotalTrades", "Wins", "Losses", "WinRate%",
            "TotalPnL", "TotalPnL%", "AvgWin", "AvgLoss",
            "ProfitFactor", "Sharpe", "MaxDD%", "AvgBars",
            "LongTrades", "ShortTrades", "TP1", "TP2", "TP3", "SL",
        ])
        for r in results:
            writer.writerow([
                r.symbol, r.total_trades, r.winning_trades, r.losing_trades,
                r.win_rate, r.total_pnl, r.total_pnl_pct, r.avg_win, r.avg_loss,
                r.profit_factor, r.sharpe_ratio, r.max_drawdown_pct, r.avg_bars_held,
                r.long_trades, r.short_trades, r.tp1_count, r.tp2_count,
                r.tp3_count, r.sl_count,
            ])
    logger.info(f"CSV 导出完成: {filepath}")


def export_json(results: List[BacktestResult], filepath: str):
    """导出为 JSON"""
    data = [r.to_dict() for r in results]
    with open(filepath, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"JSON 导出完成: {filepath}")


# ================================================================
# CLI
# ================================================================

def parse_optimize_args(args: List[str]) -> Dict[str, List]:
    """
    解析 --optimize 参数
    
    --optimize profit_factor:2.0,2.5,3.0
    --optimize atr_length:14,20,26
    """
    param_grid = {}
    for arg in args:
        parts = arg.split(":")
        if len(parts) != 2:
            logger.warning(f"忽略无效优化参数: {arg}")
            continue
        param_name = parts[0].strip()
        values = []
        for v in parts[1].split(","):
            v = v.strip()
            try:
                if "." in v:
                    values.append(float(v))
                else:
                    values.append(int(v))
            except ValueError:
                logger.warning(f"忽略无效值: {v}")
        if values:
            param_grid[param_name] = values
    return param_grid


def main():
    parser = argparse.ArgumentParser(
        description="LE VAN DO® 回测框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python backtest.py --symbol BTC-USDT
  python backtest.py --symbols BTC-USDT,ETH-USDT --days 60
  python backtest.py --symbol BTC-USDT --optimize profit_factor:2.0,2.5,3.0
  python backtest.py --symbol BTC-USDT --output results/btc.json --verbose
  python backtest.py --all-symbols --csv --output results/all.csv
        """,
    )
    parser.add_argument("--symbol", type=str, help="交易对 (如 BTC-USDT)")
    parser.add_argument("--symbols", type=str, help="逗号分隔的交易对列表")
    parser.add_argument("--all-symbols", action="store_true", help="所有交易对")
    parser.add_argument("--timeframe", type=str, default="15m", help="K 线周期 (默认 15m)")
    parser.add_argument("--days", type=int, default=30, help="回测天数 (默认 30)")
    parser.add_argument("--initial-capital", type=float, default=5000.0, help="初始资金 (默认 5000)")
    parser.add_argument("--leverage", type=int, default=1, help="杠杆倍数 (默认 1)")

    # 策略参数
    parser.add_argument("--setup-type", type=str, choices=["Open/Close", "Renko"], help="交易模式")
    parser.add_argument("--tps-type", type=str, choices=["Trailing", "ATR", "Options"], help="TP/SL 模式")
    parser.add_argument("--profit-factor", type=float, help="盈利倍数")
    parser.add_argument("--stop-factor", type=float, help="止损倍数")
    parser.add_argument("--atr-length", type=int, help="ATR 周期")
    parser.add_argument("--rsi-length", type=int, help="RSI 周期")
    parser.add_argument("--sideways-filter", type=str, help="横盘过滤器模式")

    # 优化
    parser.add_argument("--optimize", type=str, action="append", help="参数优化 (格式 name:val1,val2,...)")

    # 输出
    parser.add_argument("--output", type=str, help="输出文件路径")
    parser.add_argument("--csv", action="store_true", help="导出 CSV")
    parser.add_argument("--json", action="store_true", help="导出 JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="显示详细交易记录")
    parser.add_argument("--quiet", "-q", action="store_true", help="静默模式")

    args = parser.parse_args()

    # ---- 日志级别 ----
    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    # ---- 确定交易对列表 ----
    symbols = []
    if args.all_symbols:
        # 使用默认的 50 个交易对
        symbols = [
            "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "DOGE-USDT",
            "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT", "MATIC-USDT",
            "UNI-USDT", "SHIB-USDT", "LTC-USDT", "BCH-USDT", "ATOM-USDT",
            "ETC-USDT", "XLM-USDT", "TRX-USDT", "FIL-USDT", "APT-USDT",
            "ARB-USDT", "OP-USDT", "SUI-USDT", "PEPE-USDT", "INJ-USDT",
            "TIA-USDT", "SEI-USDT", "RUNE-USDT", "FET-USDT", "GRT-USDT",
            "NEAR-USDT", "ICP-USDT", "RENDER-USDT", "IMX-USDT", "MKR-USDT",
            "AAVE-USDT", "CRV-USDT", "SNX-USDT", "COMP-USDT", "EOS-USDT",
            "ALGO-USDT", "FLOW-USDT", "SAND-USDT", "MANA-USDT", "AXS-USDT",
            "THETA-USDT", "FTM-USDT", "CVX-USDT", "1INCH-USDT", "STX-USDT",
        ]
    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.symbol:
        symbols = [args.symbol]
    else:
        symbols = ["BTC-USDT"]

    logger.info(f"交易对: {len(symbols)} 个 — {', '.join(symbols[:5])}{'...' if len(symbols) > 5 else ''}")

    # ---- 构建策略参数 ----
    base_params = StrategyParams()
    if args.setup_type:
        base_params.setup_type = args.setup_type
    if args.tps_type:
        base_params.tps_type = args.tps_type
    if args.profit_factor:
        base_params.profit_factor = args.profit_factor
    if args.stop_factor:
        base_params.stop_factor = args.stop_factor
    if args.atr_length:
        base_params.atr_length = args.atr_length
    if args.rsi_length:
        base_params.rsi_length = args.rsi_length
    if args.sideways_filter:
        base_params.sideways_filter = args.sideways_filter

    # ---- 运行回测 ----
    all_results = []
    single_result = None

    for sym in symbols:
        logger.info(f"\n{'='*60}")
        logger.info(f"  📊 回测: {sym}")
        logger.info(f"{'='*60}")

        # 获取历史数据
        fetcher = OkxHistoryFetcher(symbol=sym, timeframe=args.timeframe)
        candles = fetcher.fetch_range(days=args.days)

        if len(candles) < 50:
            logger.warning(f"[{sym}] K 线数据不足 ({len(candles)}), 跳过")
            continue

        # 参数优化模式
        if args.optimize:
            param_grid = parse_optimize_args(args.optimize)
            if param_grid:
                logger.info(f"参数优化模式: {param_grid}")
                opt_results = optimize_params(
                    symbol=sym,
                    candles=candles,
                    param_grid=param_grid,
                    initial_capital=args.initial_capital,
                    leverage=args.leverage,
                )
                logger.info(f"\n  最佳参数:")
                best_params, best_result = opt_results[0]
                for k, v in best_params.items():
                    logger.info(f"    {k}: {v}")
                print_report(best_result, verbose=args.verbose)
                all_results.append(best_result)
                continue

        # 单次回测
        engine = BacktestEngine(
            symbol=sym,
            params=base_params,
            initial_capital=args.initial_capital,
            leverage=args.leverage,
        )
        result = engine.run(candles)
        print_report(result, verbose=args.verbose)

        if len(symbols) == 1:
            single_result = result
        all_results.append(result)

    # ---- 输出 ----
    if args.output:
        output_path = args.output
        if args.csv or output_path.endswith(".csv"):
            export_csv(all_results, output_path)
        if args.json or output_path.endswith(".json"):
            export_json(all_results, output_path)

    # 单交易对模式下返回 result 对象（可用于交互式环境）
    if single_result:
        return single_result


if __name__ == "__main__":
    main()
