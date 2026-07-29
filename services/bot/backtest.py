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
            max_pnl_pct=0.0,
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
    # 多周期对比
    parser.add_argument("--multi-period", action="store_true", help="多周期对比模式")
    parser.add_argument("--multi-period-freq", type=str, default="W", choices=["W", "M"], help="周期频率 W=周 M=月")
    
    # 高胜率模式
    parser.add_argument("--high-win", action="store_true", help="高胜率版开平仓逻辑")

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

        # ---- 参数优化模式 ----
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

        # ---- 多周期对比模式 ----
        if args.multi_period:
            logger.info(f"\n📅 多周期对比 ({args.multi_period_freq})")
            periods = split_periods(candles, freq=args.multi_period_freq)
            logger.info(f"  共 {len(periods)} 个周期")
            period_results = run_multi_period_backtest(
                symbol=sym, periods=periods, params=base_params,
                initial_capital=args.initial_capital, leverage=args.leverage,
            )
            compare_periods(period_results)
            try:
                plot_multi_period_equity(period_results, symbol=sym)
            except ImportError as e:
                logger.warning(f"matplotlib 不可用，跳过图表: {e}")
            all_results.extend([r for _, r in period_results])
            continue

        # ---- 高胜率模式 ----
        if args.high_win:
            logger.info(f"\n🎯 高胜率版回测: {sym}")
            hw_engine = HighWinRateEngine(
                symbol=sym,
                initial_capital=args.initial_capital,
                leverage=args.leverage,
            )
            hw_result = hw_engine.run(candles)
            print_report(hw_result, verbose=args.verbose)
            all_results.append(hw_result)
            if len(symbols) == 1:
                single_result = hw_result
            continue

        # ---- 单次回测（默认） ----
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


# ================================================================
# 多周期对比
# ================================================================

def split_periods(candles: List[CandleData], freq: str = "W") -> List[Tuple[str, List[CandleData]]]:
    """
    将 K 线按周/月切分为多个周期。

    :param candles: 升序排列的 K 线列表
    :param freq: 'W'=周, 'M'=月
    :returns: [(period_label, candles_in_period), ...] 按时间升序
    """
    from collections import OrderedDict

    period_map: Dict[str, List[CandleData]] = OrderedDict()

    for c in candles:
        dt = datetime.fromtimestamp(c.timestamp / 1000, tz=timezone.utc)
        if freq == "M":
            key = dt.strftime("%Y-%m")
        else:
            # ISO 周: 2025-W14
            iso_year, iso_week, _ = dt.isocalendar()
            key = f"{iso_year}-W{iso_week:02d}"

        if key not in period_map:
            period_map[key] = []
        period_map[key].append(c)

    return list(period_map.items())


def run_multi_period_backtest(
    symbol: str,
    periods: List[Tuple[str, List[CandleData]]],
    params: Optional[StrategyParams] = None,
    initial_capital: float = 5000.0,
    leverage: int = 1,
) -> List[Tuple[str, BacktestResult]]:
    """
    对每个周期独立运行回测。

    :returns: [(period_label, result), ...] 按时间升序
    """
    results = []
    for label, period_candles in periods:
        if len(period_candles) < 10:
            logger.debug(f"  跳过 {label}: K 线不足 ({len(period_candles)})")
            continue

        engine = BacktestEngine(
            symbol=symbol,
            params=params,
            initial_capital=initial_capital,
            leverage=leverage,
        )
        result = engine.run(period_candles)
        results.append((label, result))

        logger.info(
            f"  [{label}] 交易={result.total_trades}, "
            f"PnL=${result.total_pnl:.2f}, "
            f"胜率={result.win_rate:.1f}%, "
            f"PF={result.profit_factor}"
        )

    return results


def compare_periods(period_results: List[Tuple[str, BacktestResult]]):
    """
    打印多周期对比表。
    """
    if not period_results:
        logger.warning("无周期结果可对比")
        return

    sep = "=" * 90
    header = f"| {'周期':<12s} | {'交易':>5s} | {'PnL':>10s} | {'胜率':>6s} | {'PF':>6s} | {'均盈':>8s} | {'均亏':>8s} | {'回撤':>6s} |"
    divider = "|" + "-" * 12 + "|" + "-" * 7 + "|" + "-" * 12 + "|" + "-" * 8 + "|" + "-" * 8 + "|" + "-" * 10 + "|" + "-" * 10 + "|" + "-" * 8 + "|"

    lines = ["", sep, "  📅 多周期对比表", sep, header, divider]

    total_trades = 0
    total_pnl = 0.0

    for label, r in period_results:
        pnl_str = f"{r.total_pnl:+.2f}"
        pnl_color = "🟢" if r.total_pnl >= 0 else "🔴"
        lines.append(
            f"| {label:<12s} | {r.total_trades:>5d} | {pnl_color} {pnl_str:>8s} | "
            f"{r.win_rate:>5.1f}% | {r.profit_factor:>5.2f} | "
            f"${r.avg_win:>6.2f} | ${r.avg_loss:>6.2f} | {r.max_drawdown_pct:>5.1f}% |"
        )
        total_trades += r.total_trades
        total_pnl += r.total_pnl

    # 汇总行
    lines.append(divider)
    lines.append(
        f"| {'合计':<12s} | {total_trades:>5d} | {'$' + f'{total_pnl:+.2f}':>12s} | {'':>8s} | {'':>8s} | "
        f"{'':>10s} | {'':>10s} | {'':>8s} |"
    )
    lines.append(sep)

    print("\n".join(lines))


def plot_multi_period_equity(
    period_results: List[Tuple[str, BacktestResult]],
    symbol: str = "",
    figsize: Tuple[int, int] = (14, 6),
):
    """
    绘制多周期归一化资金曲线叠加图。

    每个周期以初始资金 = 1.0 归一化，叠加显示。
    需安装 matplotlib。

    :param period_results: [(label, result), ...]
    :param symbol: 交易对名称（标题用）
    :param figsize: 图表尺寸
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, ax = plt.subplots(figsize=figsize)
    colors = plt.cm.viridis_r([i / max(len(period_results), 1) for i in range(len(period_results))])

    for i, (label, r) in enumerate(period_results):
        curve = r.equity_curve
        if not curve or len(curve) < 2:
            continue
        # 归一化: 起始值 = 1.0
        norm = [v / curve[0] for v in curve]
        ax.plot(norm, label=label, color=colors[i], linewidth=1.2, alpha=0.85)

    ax.set_title(f"归一化资金曲线叠加 — {symbol}", fontsize=13, fontweight="bold", color="#f0f6fc")
    ax.set_xlabel("K 线序号", fontsize=10, color="#8b949e")
    ax.set_ylabel("归一化权益 (起始=1.0)", fontsize=10, color="#8b949e")
    ax.axhline(y=1.0, color="#484f58", linewidth=0.8, linestyle="--")
    ax.legend(loc="best", fontsize=8, ncol=2)
    ax.set_facecolor("#161b22")
    fig.patch.set_facecolor("#0d1117")
    ax.tick_params(colors="#8b949e", labelsize=8)
    ax.grid(True, alpha=0.15, color="#30363d")

    plt.tight_layout()
    out_path = f"multi_period_equity_{symbol.replace('-', '_')}.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    logger.info(f"📈 多周期资金曲线图已保存: {out_path}")
    plt.close(fig)


# ================================================================
# 高胜率版开平仓引擎
# ================================================================

class HighWinRateEngine:
    """
    高胜率版开平仓逻辑。

    核心参数:
      - MIN_EDGE = 0.20
      - 止盈 9%、止损 6.5%
      - 移动止盈：浮盈 >= 5% 启动，回撤 3% 平仓
      - 价格过滤 0.28–0.72（价格在 [minP, maxP] 区间的中间位置）
      - 模型概率 |p-0.5| >= 0.12
      - ALLOWED_SIDES = ["NO"]
      - 时间止损 24h
      - Position 增加 max_pnl_pct 字段

    注意：此引擎不与原有 BacktestEngine 共享，独立运行。
    """

    # 高胜率参数
    MIN_EDGE = 0.20          # 最小边缘阈值
    TP_PCT = 9.0             # 止盈 9%
    SL_PCT = 6.5             # 止损 6.5%
    TRAILING_ACTIVATE = 5.0  # 浮盈 >= 5% 启动移动止盈
    TRAILING_DRAWDOWN = 3.0  # 从最高浮盈回撤 3% 平仓
    PRICE_LOW = 0.28         # 价格过滤下限
    PRICE_HIGH = 0.72        # 价格过滤上限
    MIN_PROB_EDGE = 0.12     # |p-0.5| >= 0.12
    ALLOWED_SIDES = ["NO"]   # 只允许 NO（空头）
    TIME_STOP_HOURS = 24     # 时间止损 24h

    # 资金管理
    POSITION_SIZE_USD = 1.0  # 每仓 $1
    MAX_POSITIONS = 50
    INITIAL_CAPITAL = 5000.0
    COMMISSION_PCT = 0.02

    # 用于计算模型概率的 RSI 映射
    # 高胜率版用 RSI 作为模型概率代理
    RSI_LENGTH = 7
    RSI_TOP = 45
    RSI_BOT = 10

    def __init__(
        self,
        symbol: str,
        initial_capital: float = 5000.0,
        leverage: int = 1,
    ):
        self.symbol = symbol
        self.initial_capital = initial_capital
        self.leverage = leverage

        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = [initial_capital]
        self.equity = initial_capital

        # 位置价格范围（从 K 线数据推断）
        self._min_price: float = float("inf")
        self._max_price: float = 0.0

        # 当前持仓
        self._position_side: Optional[str] = None  # "NO" only
        self._entry_price: float = 0.0
        self._entry_time: Optional[str] = None
        self._entry_bar: int = 0
        self._entry_ms: int = 0
        self._position_size: float = 0.0
        self._max_pnl_pct: float = 0.0  # 持仓期间最大浮盈 %
        self._bars_held: int = 0
        self._current_bar: int = 0
        self._peak_price: float = 0.0  # 持仓期间最高价（用于空头 = 最低价）

        # RSI 缓存
        self._closes: List[float] = []

        # 统计
        self.total_signals: int = 0

    def _update_price_range(self, candles: List[CandleData]):
        """从历史数据推断价格范围"""
        for c in candles:
            if c.high > self._max_price:
                self._max_price = c.high
            if c.low < self._min_price:
                self._min_price = c.low

    def _is_price_in_range(self, price: float) -> bool:
        """检查价格是否在 0.28–0.72 中间区域"""
        if self._max_price <= self._min_price:
            return True
        norm = (price - self._min_price) / (self._max_price - self._min_price)
        return self.PRICE_LOW <= norm <= self.PRICE_HIGH

    def _calc_model_prob(self, close: float) -> float:
        """
        用 RSI 映射为模型概率 (0~1)。
        RSI <= RSI_BOT → p ≈ 0.9 (高置信度 NO)
        RSI >= RSI_TOP → p ≈ 0.1 (高置信度 YES)
        RSI = 50 → p = 0.5
        """
        if len(self._closes) < self.RSI_LENGTH + 2:
            return 0.5

        # 从 indicators 导入 rsi
        from indicators import rsi as calc_rsi
        rsi_vals = calc_rsi(self._closes, self.RSI_LENGTH)
        current_rsi = rsi_vals[-1] if rsi_vals else 50.0

        # RSI 0~100 → 概率 0~1
        # RSI=0 → p=1.0 (极端NO), RSI=100 → p=0.0 (极端YES)
        prob = 1.0 - (current_rsi / 100.0)
        return max(0.0, min(1.0, prob))

    def _check_prob_filter(self, prob: float) -> bool:
        """检查 |p-0.5| >= MIN_PROB_EDGE"""
        return abs(prob - 0.5) >= self.MIN_PROB_EDGE

    def _calculate_position_size(self, price: float) -> float:
        """每仓 $1"""
        if price <= 0:
            return 0.001
        return round(self.POSITION_SIZE_USD / price, 8)

    def run(self, candles: List[CandleData]) -> BacktestResult:
        """运行高胜率版回测"""
        if len(candles) < 20:
            logger.error(f"K 线不足: {len(candles)}")
            return self._build_result()

        self._update_price_range(candles)

        long_trades = 0
        short_trades = 0
        tp_count = 0
        sl_count = 0
        manual_count = 0
        trailing_count = 0
        time_stop_count = 0

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

            # 边缘值: |p-0.5| 作为 edge
            edge = abs(model_prob - 0.5)

            # ===== try_open_positions =====
            if not self._position_side and price_in_range and prob_ok and edge >= self.MIN_EDGE:
                # ALLOWED_SIDES = ["NO"] → 只开空头
                self._position_side = "NO"
                self._entry_price = current_price
                self._entry_time = datetime.fromtimestamp(
                    candle.timestamp / 1000, tz=timezone.utc
                ).isoformat()
                self._entry_ms = candle.timestamp
                self._entry_bar = i
                self._position_size = self._calculate_position_size(current_price)
                self._max_pnl_pct = 0.0
                self._bars_held = 0
                self._peak_price = current_price
                self.total_signals += 1

                logger.debug(
                    f"  🟢 [{self.symbol}] 开仓 NO @ {current_price:.4f}, "
                    f"edge={edge:.3f}, prob={model_prob:.3f}"
                )

            # ===== check_and_close_positions =====
            if self._position_side:
                self._bars_held += 1

                # 空头: 价格下跌才盈利，所以 peak_price 是最低
                if current_price < self._peak_price:
                    self._peak_price = current_price

                # 计算当前浮盈 %
                if self._position_side == "NO":
                    pnl_pct = (self._entry_price - current_price) / self._entry_price * 100
                else:
                    pnl_pct = (current_price - self._entry_price) / self._entry_price * 100

                # 更新最大浮盈
                if pnl_pct > self._max_pnl_pct:
                    self._max_pnl_pct = pnl_pct

                # 更新权益
                if self._position_side == "NO":
                    unrealized_pnl = (self._entry_price - current_price) * self._position_size
                else:
                    unrealized_pnl = (current_price - self._entry_price) * self._position_size

                current_equity = self.initial_capital + sum(
                    t.pnl for t in self.trades
                ) + unrealized_pnl
                self.equity = current_equity

                if current_equity > peak_equity:
                    peak_equity = current_equity
                dd = peak_equity - current_equity
                if dd > max_drawdown:
                    max_drawdown = dd

                # ---- 止盈 9% ----
                if pnl_pct >= self.TP_PCT:
                    self._close_trade(current_price, candle.timestamp, "TP")
                    tp_count += 1
                    continue

                # ---- 止损 6.5% ----
                if pnl_pct <= -self.SL_PCT:
                    self._close_trade(current_price, candle.timestamp, "SL")
                    sl_count += 1
                    continue

                # ---- 移动止盈 ----
                if self._max_pnl_pct >= self.TRAILING_ACTIVATE:
                    drawdown_from_peak = self._max_pnl_pct - pnl_pct
                    if drawdown_from_peak >= self.TRAILING_DRAWDOWN:
                        self._close_trade(current_price, candle.timestamp, "Trailing")
                        trailing_count += 1
                        continue

                # ---- 时间止损 24h ----
                elapsed_hours = (candle.timestamp - self._entry_ms) / 3600000
                if elapsed_hours >= self.TIME_STOP_HOURS:
                    self._close_trade(current_price, candle.timestamp, "TimeStop")
                    time_stop_count += 1
                    continue
            else:
                # 无持仓时记录权益
                self.equity = self.initial_capital + sum(t.pnl for t in self.trades)

            # 记录权益曲线
            self.equity_curve.append(round(self.equity, 2))

        # ---- 收盘强制平仓 ----
        if self._position_side:
            self._close_trade(candles[-1].close, candles[-1].timestamp, "Manual")
            manual_count += 1

        # ---- 汇总 ----
        result = self._build_result()

        for t in self.trades:
            if t.side == "NO" or t.side == "short":
                short_trades += 1
            else:
                long_trades += 1
            if t.exit_reason == "TP": tp_count += 1
            elif t.exit_reason == "SL": sl_count += 1
            elif t.exit_reason == "Trailing": trailing_count += 1
            elif t.exit_reason == "TimeStop": time_stop_count += 1
            elif t.exit_reason == "Manual": manual_count += 1

        result.long_trades = long_trades
        result.short_trades = short_trades
        result.tp1_count = tp_count
        result.sl_count = sl_count
        result.manual_count = manual_count
        result.tp2_count = trailing_count
        result.tp3_count = time_stop_count
        result.equity_curve = self.equity_curve
        result.max_drawdown = round(max_drawdown, 2)
        result.max_drawdown_pct = round(
            (max_drawdown / peak_equity * 100) if peak_equity > 0 else 0, 2
        )

        # Sharpe Ratio
        if len(self.equity_curve) > 1:
            returns = [
                (self.equity_curve[i] - self.equity_curve[i-1]) / self.equity_curve[i-1]
                for i in range(1, len(self.equity_curve))
                if self.equity_curve[i-1] > 0
            ]
            if returns:
                avg_r = sum(returns) / len(returns)
                var = sum((r - avg_r) ** 2 for r in returns) / len(returns)
                std = math.sqrt(var) if var > 0 else 1e-10
                result.sharpe_ratio = round(
                    avg_r / std * math.sqrt(365 * 24 * 4), 2
                )

        logger.info(
            f"[{self.symbol}] 高胜率版回测完成: "
            f"{result.total_trades} 笔, "
            f"PnL=${result.total_pnl:.2f}, "
            f"胜率={result.win_rate:.1f}%, "
            f"回撤={result.max_drawdown_pct:.1f}%"
        )

        return result

    def _close_trade(self, exit_price: float, timestamp: int, reason: str):
        """平仓"""
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

    def _build_result(self) -> BacktestResult:
        """汇总结果"""
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


if __name__ == "__main__":
    main()
