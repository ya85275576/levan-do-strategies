#!/usr/bin/env python3
"""
LE VAN DO® OKX 原生交易机器人 — 主程序

直接通过 OKX REST API 轮询行情数据驱动交易，无需 WebSocket，无需 TradingView。
将 LE VAN DO® Swing Signals 策略从 Pine Script 移植到 Python。
支援多交易对并行运行（50 个主流币种 USDT 交易对）。

运行模式:
  DRY_RUN=true  (默认) — 模拟模式，REST API 获取真实行情，但仅记录日志不下单
  DRY_RUN=false        — 实盘模式，实际连接交易所并发送 API 请求

数据源优先级:
  1. REST API 轮询（默认，避免东京服务器 IP 被 WebSocket 封锁的问题）
  2. 模拟数据源（REST API 不可用时回退）

架构:
  market_data.py  ←  OKX REST API / 模拟数据源 (15m K 线，轮询)
       ↓ 聚合 (tfmult=18)
  strategy.py     ←  策略引擎 x 50 (每交易对独立实例)
       ↓ 信号
  order_manager.py  →  OKX REST API (下单)

启动:
  python bot.py                    # 模拟模式（50 个交易对）
  DRY_RUN=false python bot.py      # 实盘模式
  或使用 PM2 (见 ecosystem.config.js)
"""
import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Dict, Optional

# 确保可以导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config
from market_data import (
    Candle,
    CandleAggregator,
    MarketDataSubscriber,
    RestApiDataFeed,
    SimulatedDataFeed,
)
from strategy import (
    CandleData,
    LeVanDoStrategy,
    SignalType,
    StrategyParams,
    TpSlLevels,
)
from order_manager import OkxOrderManager

# ---- 日志配置 ----
def setup_logging(level: str = "INFO"):
    log_format = (
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s"
    )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )

logger = logging.getLogger("bot")


# ================================================================
# 信号到操作映射
# ================================================================

SIGNAL_ACTION_MAP = {
    SignalType.LONG_ENTRY: ("enter", "buy"),
    SignalType.SHORT_ENTRY: ("enter", "sell"),
    SignalType.LONG_EXIT: ("exit", "buy"),
    SignalType.SHORT_EXIT: ("exit", "sell"),
    SignalType.LONG_TP1: ("exit_partial", "sell"),
    SignalType.LONG_TP2: ("exit_partial", "sell"),
    SignalType.LONG_TP3: ("exit_partial", "sell"),
    SignalType.LONG_SL: ("exit", "sell"),
    SignalType.SHORT_TP1: ("exit_partial", "buy"),
    SignalType.SHORT_TP2: ("exit_partial", "buy"),
    SignalType.SHORT_TP3: ("exit_partial", "buy"),
    SignalType.SHORT_SL: ("exit", "buy"),
}


# ================================================================
# 交易信号处理（每交易对独立实例）
# ================================================================

class SignalHandler:
    """
    信号处理器 — 每交易对一个实例

    根据策略产生的信号，决定如何执行交易。
    支持三种止盈止损模式（ATR / Trailing / Options）。
    """

    def __init__(self, order_manager: OkxOrderManager, config: dict, symbol: str):
        self.om = order_manager
        self.config = config
        self.tps_type = config.get("tps_type", "Trailing")
        self.symbol = symbol
        self.trade_qty_pct = config.get("trade_qty_pct", 50.0)
        self.initial_capital = config.get("initial_capital", 5000.0)

        # 仓位管理
        self._position_side: Optional[str] = None  # "long" | "short" | None
        self._position_qty: float = 0.0
        self._entry_price: float = 0.0
        self._tp1_pct = config.get("tp1_qty_pct", 50.0)
        self._tp2_pct = config.get("tp2_qty_pct", 30.0)
        self._tp3_pct = config.get("tp3_qty_pct", 20.0)
        self._tp_hit_level: int = 0

        # 统计
        self.total_signals: int = 0
        self.last_signal: Optional[SignalType] = None

    def _calculate_qty(self, current_price: float) -> float:
        """根据账户余额和仓位百分比计算交易数量"""
        if self.om.dry_run:
            equity = self.initial_capital
        else:
            # TODO: 实盘余额获取, 暂时使用初始资金
            equity = self.initial_capital

        trade_value = equity * (self.trade_qty_pct / 100.0)
        qty = trade_value / current_price if current_price > 0 else 0.001

        # 最小交易量处理
        if self.symbol.startswith("BTC"):
            qty = max(qty, 0.001)
        elif self.symbol.startswith("ETH"):
            qty = max(qty, 0.01)
        elif self.symbol.startswith("SHIB") or self.symbol.startswith("PEPE"):
            qty = max(qty, 100000.0)
        else:
            qty = max(qty, 0.1)

        return round(qty, 4)

    async def handle_signal(self, signal: SignalType, tp_sl: TpSlLevels):
        """处理策略信号，执行交易"""
        self.total_signals += 1
        self.last_signal = signal

        action, side = SIGNAL_ACTION_MAP.get(signal, ("none", "none"))
        if action == "none":
            logger.warning(f"[{self.symbol}] 未识别的信号: {signal}")
            return

        current_price = tp_sl.entry or 0.0
        if current_price == 0.0:
            logger.error(f"[{self.symbol}] 信号中缺少入场价格")
            return

        base_msg = f"[{self.symbol}] 信号: {signal.value}"
        if self.tps_type == "Trailing":
            await self._handle_trailing(signal, action, side, current_price, tp_sl)
        elif self.tps_type == "ATR":
            await self._handle_atr(signal, action, side, current_price, tp_sl)
        elif self.tps_type == "Options":
            await self._handle_options(signal, action, side, current_price, tp_sl)

    async def _handle_trailing(self, signal, action, side, price, tp_sl):
        if signal == SignalType.LONG_ENTRY:
            pos = self.om.get_simulated_position_size(self.symbol)
            if pos < 0:
                logger.info(f"[{self.symbol}] Trailing: 平空仓")
                await self.om.close_position(self.symbol)
            qty = self._calculate_qty(price)
            logger.info(f"[{self.symbol}] Trailing: 开多仓 {qty} @ {price:.2f}")
            await self.om.set_leverage(self.symbol)
            await self.om.place_order(self.symbol, "buy", qty, "market")

        elif signal == SignalType.SHORT_ENTRY:
            pos = self.om.get_simulated_position_size(self.symbol)
            if pos > 0:
                logger.info(f"[{self.symbol}] Trailing: 平多仓")
                await self.om.close_position(self.symbol)
            qty = self._calculate_qty(price)
            logger.info(f"[{self.symbol}] Trailing: 开空仓 {qty} @ {price:.2f}")
            await self.om.set_leverage(self.symbol)
            await self.om.place_order(self.symbol, "sell", qty, "market")

    async def _handle_atr(self, signal, action, side, price, tp_sl):
        if signal == SignalType.LONG_ENTRY:
            if self._position_side == "short":
                await self.om.close_position(self.symbol)
            qty = self._calculate_qty(price)
            logger.info(f"[{self.symbol}] ATR: 开多仓 {qty} @ {price:.2f}, "
                        f"TP1={tp_sl.tp1:.2f}, SL={tp_sl.sl:.2f}")
            await self.om.set_leverage(self.symbol)
            await self.om.place_order(self.symbol, "buy", qty, "market")
            self._position_side = "long"
            self._entry_price = price

        elif signal == SignalType.SHORT_ENTRY:
            if self._position_side == "long":
                await self.om.close_position(self.symbol)
            qty = self._calculate_qty(price)
            logger.info(f"[{self.symbol}] ATR: 开空仓 {qty} @ {price:.2f}, "
                        f"TP1={tp_sl.tp1:.2f}, SL={tp_sl.sl:.2f}")
            await self.om.set_leverage(self.symbol)
            await self.om.place_order(self.symbol, "sell", qty, "market")
            self._position_side = "short"
            self._entry_price = price

        elif signal in (SignalType.LONG_TP1, SignalType.SHORT_TP1):
            qty_pct = self._tp1_pct
            pos = self.om.get_simulated_position_size(self.symbol)
            qty = abs(pos) * (qty_pct / 100.0)
            close_side = "sell" if self._position_side == "long" else "buy"
            logger.info(f"[{self.symbol}] ATR: TP1 {qty_pct}%")
            await self.om.place_order(self.symbol, close_side, round(qty, 4), "market")

        elif signal in (SignalType.LONG_TP2, SignalType.SHORT_TP2):
            qty_pct = self._tp2_pct
            pos = self.om.get_simulated_position_size(self.symbol)
            qty = abs(pos) * (qty_pct / 100.0)
            close_side = "sell" if self._position_side == "long" else "buy"
            logger.info(f"[{self.symbol}] ATR: TP2 {qty_pct}%")
            await self.om.place_order(self.symbol, close_side, round(qty, 4), "market")

        elif signal in (SignalType.LONG_TP3, SignalType.SHORT_TP3):
            qty_pct = self._tp3_pct
            pos = self.om.get_simulated_position_size(self.symbol)
            qty = abs(pos) * (qty_pct / 100.0)
            close_side = "sell" if self._position_side == "long" else "buy"
            logger.info(f"[{self.symbol}] ATR: TP3 {qty_pct}%")
            await self.om.place_order(self.symbol, close_side, round(qty, 4), "market")

        elif signal in (SignalType.LONG_SL, SignalType.SHORT_SL):
            logger.warning(f"[{self.symbol}] ATR: SL 触发")
            await self.om.close_position(self.symbol)
            self._position_side = None

    async def _handle_options(self, signal, action, side, price, tp_sl):
        if signal == SignalType.LONG_ENTRY:
            qty = self._calculate_qty(price)
            logger.info(f"[{self.symbol}] Options: 开多仓 {qty} @ {price:.2f}")
            await self.om.set_leverage(self.symbol)
            await self.om.place_order(self.symbol, "buy", qty, "market")
            self._position_side = "long"

        elif signal == SignalType.SHORT_ENTRY:
            logger.info(f"[{self.symbol}] Options: 平多仓")
            await self.om.close_position(self.symbol)
            self._position_side = None

        elif signal == SignalType.LONG_EXIT:
            logger.info(f"[{self.symbol}] Options: 手动平多仓")
            await self.om.close_position(self.symbol)
            self._position_side = None

        elif signal == SignalType.SHORT_EXIT:
            logger.info(f"[{self.symbol}] Options: 手动平空仓")
            await self.om.close_position(self.symbol)
            self._position_side = None

    def reset(self):
        """重置仓位状态"""
        self._position_side = None
        self._position_qty = 0.0
        self._entry_price = 0.0
        self._tp_hit_level = 0

    def get_summary(self) -> dict:
        """获取信号处理器状态摘要"""
        return {
            "symbol": self.symbol,
            "position_side": self._position_side,
            "entry_price": round(self._entry_price, 2) if self._entry_price else None,
            "total_signals": self.total_signals,
            "last_signal": self.last_signal.value if self.last_signal else None,
        }


# ================================================================
# 单交易对运行单元
# ================================================================

class SymbolTradingUnit:
    """
    单个交易对的交易单元。

    包含：
      - CandleAggregator（k 线聚合）
      - LeVanDoStrategy（策略引擎）
      - SignalHandler（信号处理）
    """

    def __init__(self, symbol: str, config: dict, order_manager: OkxOrderManager,
                 on_signal_callback):
        self.symbol = symbol
        self.config = config

        self.base_tf_sec = config.get("base_timeframe_min", 15) * 60
        self.higher_tf_sec = self.base_tf_sec * config.get("tf_mult", 18)

        # K 线聚合器
        self.aggregator = CandleAggregator(
            symbol=symbol,
            base_seconds=self.base_tf_sec,
            target_seconds=self.higher_tf_sec,
        )

        # 策略参数
        strategy_params = StrategyParams(
            tps_type=config.get("tps_type", "Trailing"),
            setup_type=config.get("setup_type", "Open/Close"),
            tf_mult=config.get("tf_mult", 18),
            sideways_filter=config.get("sideways_filter", "No Filtering"),
            rsi_length=config.get("rsi_length", 7),
            rsi_top_limit=config.get("rsi_top_limit", 45),
            rsi_bot_limit=config.get("rsi_bot_limit", 10),
            atr_filter_len=config.get("atr_filter_len", 5),
            atr_ma_len=config.get("atr_ma_len", 5),
            renko_atr_len=config.get("renko_atr_len", 3),
            renko_ema1_length=config.get("renko_ema1_length", 2),
            renko_ema2_length=config.get("renko_ema2_length", 10),
            atr_length=config.get("atr_length", 20),
            profit_factor=config.get("profit_factor", 2.5),
            stop_factor=config.get("stop_factor", 1.0),
            tp1_qty_pct=config.get("tp1_qty_pct", 50.0),
            tp2_qty_pct=config.get("tp2_qty_pct", 30.0),
            tp3_qty_pct=config.get("tp3_qty_pct", 20.0),
        )

        # 策略引擎
        self.strategy = LeVanDoStrategy(
            params=strategy_params,
            on_signal=lambda sig, tp: on_signal_callback(self.symbol, sig, tp),
        )

        # 信号处理器
        self.signal_handler = SignalHandler(order_manager, config, symbol)

        # 最新價格（從 base candle 更新，供 PnL 計算）
        self.latest_price: float = 0.0

        # 统计
        self.higher_tf_candle_count: int = 0

    def on_base_candle(self, candle: Candle):
        """基础周期 K 线更新"""
        self.latest_price = candle.close
        completed = self.aggregator.add_candle(CandleData(
            timestamp=candle.timestamp,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
        ))

        if completed:
            self._on_higher_tf_candle(completed)

    def _on_higher_tf_candle(self, candle: CandleData):
        """高周期 K 线完成回调"""
        self.higher_tf_candle_count += 1
        logger.debug(
            f"[{self.symbol}] 高周期 K 线 #{self.higher_tf_candle_count}: "
            f"O={candle.open:.2f} H={candle.high:.2f} "
            f"L={candle.low:.2f} C={candle.close:.2f}"
        )

        self.strategy.update_higher_tf_candle(candle)
        signal = self.strategy.analyze()

        if signal != SignalType.NONE:
            asyncio.ensure_future(
                self.signal_handler.handle_signal(signal, self.strategy.tp_sl)
            )

    def get_summary(self) -> dict:
        """获取交易单元状态摘要"""
        return {
            "symbol": self.symbol,
            "aggregator_progress": f"{self.aggregator.progress_pct:.0f}%",
            "higher_tf_candles": self.higher_tf_candle_count,
            "strategy": self.strategy.get_status(),
            "signal_handler": self.signal_handler.get_summary(),
        }


# ================================================================
# 机器人主类
# ================================================================

class OkxTradingBot:
    """
    LE VAN DO® OKX 原生交易机器人

    管理 50 个交易对的并行策略实例。
    """

    def __init__(self, config: dict):
        self.config = config
        self.symbols = config["symbols"]
        self.base_tf_sec = config.get("base_timeframe_min", 15) * 60
        self.higher_tf_sec = self.base_tf_sec * config.get("tf_mult", 18)

        # 订单管理器（共享）
        self.order_manager = OkxOrderManager(
            api_key=config["api_key"],
            api_secret=config["api_secret"],
            api_passphrase=config["api_passphrase"],
            rest_url=config["rest_url"],
            dry_run=config["dry_run"],
            symbol=self.symbols[0] if self.symbols else "BTC-USDT",
            leverage=config.get("default_leverage", 1),
            position_mode=config.get("position_mode", "isolated"),
        )

        # 交易单元: {symbol: SymbolTradingUnit}
        self.trading_units: Dict[str, SymbolTradingUnit] = OrderedDict()

        for symbol in self.symbols:
            unit = SymbolTradingUnit(
                symbol=symbol,
                config=config,
                order_manager=self.order_manager,
                on_signal_callback=self._on_strategy_signal,
            )
            self.trading_units[symbol] = unit

        # 市场数据（REST API 轮询，失败时回退到模拟数据）
        self.subscriber: Optional[RestApiDataFeed] = None
        self._simulated_feed: Optional[SimulatedDataFeed] = None

        # 运行控制
        self._running = False
        self._tasks: list = []

        # 全局统计
        self.start_time: Optional[float] = None
        self.total_signals: int = 0

        # 状态文件路径（供 Webhook 仪表板读取）
        self._status_file = os.environ.get(
            "BOT_STATUS_FILE",
            "/tmp/le-van-do-bot-status.json",
        )
        self._flush_task: Optional[asyncio.Task] = None
        self._signal_queue: list = []

        # 歷史平倉記錄（供 Webhook 儀表板讀取）
        self.closed_trades: list = []
        self._last_positions_snapshot: Dict[str, dict] = {}  # {symbol: {"qty": float, "entry_price": float}}

    # ---- 策略信号回调 ----

    def _on_strategy_signal(self, symbol: str, signal: SignalType, tp_sl: TpSlLevels):
        """策略引擎产生信号时的回调"""
        self.total_signals += 1
        unit = self.trading_units.get(symbol)
        sh = unit.signal_handler if unit else None

        logger.info(
            f"📨 [{symbol}] 策略信号: {signal.value} "
            f"(累计: {self.total_signals}, "
            f"该交易对: {sh.total_signals if sh else 0})"
        )

        # 加入訊號佇列，供 _flush_status_file 寫入狀態檔案
        self._signal_queue.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "signal": signal.value,
            "price": round(tp_sl.entry, 2) if tp_sl.entry else None,
        })

    # ---- 市场数据回调 ----

    def _on_base_candle(self, symbol: str, candle: Candle):
        """基础周期 K 线更新回调"""
        unit = self.trading_units.get(symbol)
        if unit is None:
            return
        unit.on_base_candle(candle)

    # ---- 启动与停止 ----

    async def start(self):
        """启动机器人"""
        self._running = True
        self.start_time = time.time()

        # 打印启动横幅
        logger.info("╔══════════════════════════════════════════════════════════╗")
        logger.info("║  LE VAN DO® OKX 原生交易机器人（多交易对）             ║")
        logger.info("╠══════════════════════════════════════════════════════════╣")
        logger.info(f"║  交易对数量: {len(self.symbols):<45d}║")
        logger.info(f"║  网络:       {'🟡 测试网' if self.config['is_testnet'] else '🔴 实盘':<45s}║")
        logger.info(f"║  模拟模式:   {'🟢 启用' if self.config['dry_run'] else '🔴 关闭':<45s}║")
        logger.info(f"║  交易模式:   {self.config.get('setup_type', 'Open/Close'):<45s}║")
        logger.info(f"║  TP/SL 模式: {self.config.get('tps_type', 'Trailing'):<45s}║")
        logger.info(f"║  基础周期:   {self.config['base_timeframe_min']}m -> 高周期 {self.higher_tf_sec // 60}m       ║")
        logger.info(f"║  Sideways:   {self.config.get('sideways_filter', 'No Filtering'):<45s}║")
        logger.info("╠══════════════════════════════════════════════════════════╣")
        logger.info("║  交易对列表:                                          ║")

        # 分列打印交易对
        cols = 4
        for i in range(0, len(self.symbols), cols):
            chunk = self.symbols[i:i + cols]
            padded = [f"{s:<16s}" for s in chunk]
            logger.info(f"║  {' '.join(padded)} ║")

        logger.info("╚══════════════════════════════════════════════════════════╝")

        # ---- 啟動狀態寫入任務（供 Webhook 儀表板讀取） ----
        self._flush_task = asyncio.create_task(self._flush_status_file())

        # ---- 数据源：优先使用 REST API，失败则回退到模拟数据 ----
        rest_feed = RestApiDataFeed(
            symbols=self.symbols,
            base_timeframe_sec=self.base_tf_sec,
            on_candle=self._on_base_candle,
            rest_url=self.config["rest_url"],
        )
        self.subscriber = rest_feed

        # 尝试 REST API 数据源
        rest_success = False
        try:
            logger.info("📡 正在通过 REST API 获取初始 K 线数据...")
            # 在后台运行轮询循环，先进行一次初始加载
            feed_task = asyncio.create_task(rest_feed.run())

            # 等待初始加载完成（通过检测 _initial_load_done 标记）
            # RestApiDataFeed.run() 内部会立即执行 _poll_all() 作为初始加载
            rest_success = await self._wait_for_rest_init(rest_feed, timeout=30)

            if rest_success:
                logger.info("✅ REST API 行情数据源正常运行")
                # 等待 feed_task 完成（它会持续轮询直到停止）
                await feed_task
            else:
                # REST API 失败，取消轮询任务
                feed_task.cancel()
                try:
                    await feed_task
                except asyncio.CancelledError:
                    pass
                await rest_feed.close()
                raise ConnectionError("REST API 初始加载未获取到数据")

        except (ConnectionError, Exception) as e:
            logger.warning(f"⚠️ REST API 行情不可用: {e}")

            if self.config["dry_run"]:
                logger.warning("🔄 回退到模拟数据源...")
                self.subscriber = None

                sim_feed = SimulatedDataFeed(
                    symbols=self.symbols,
                    base_timeframe_sec=self.base_tf_sec,
                )
                self._simulated_feed = sim_feed

                try:
                    await sim_feed.run(on_candle=self._on_base_candle)
                except asyncio.CancelledError:
                    logger.info("模拟数据源收到停止信号")
            else:
                logger.error("❌ 非模拟模式下 REST API 行情不可用，机器人停止")
                raise
        except asyncio.CancelledError:
            logger.info("机器人收到停止信号")
        finally:
            await self.stop()

    async def _wait_for_rest_init(
        self, feed: RestApiDataFeed, timeout: float = 30
    ) -> bool:
        """
        等待 REST API 数据源完成初始加载。

        首次调用后先 yield 控制权，让 feed_task 有机会开始执行。
        检查策略：每 0.5 秒检测一次：
          - 初始加载已完成（_initial_load_done），凭 latest_candles 判定成败
          - 已有 K 线数据到达（无需等待初始化标记）
        """
        # 先 yield 一次，让 create_task 调度的 run() 有机会启动
        await asyncio.sleep(0)

        waited = 0.0
        check_interval = 0.5

        while waited < timeout:
            # 初始加载已完成
            if feed._initial_load_done:
                return len(feed.latest_candles) > 0

            # 已有数据到达（无需等待完整加载）
            if len(feed.latest_candles) > 0:
                return True

            await asyncio.sleep(check_interval)
            waited += check_interval

        # 超时
        logger.warning(f"[REST] 初始加载超时 ({timeout}s)")
        return len(feed.latest_candles) > 0

    async def stop(self):
        """停止机器人"""
        self._running = False

        # 最後一次寫入狀態檔案
        await self._flush_status_file_now()

        # 取消定時寫入任務
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        if self.subscriber:
            await self.subscriber.close()
            self.subscriber = None

        if self._simulated_feed:
            self._simulated_feed.stop()
            self._simulated_feed = None

        await self.order_manager.close()

        elapsed = time.time() - (self.start_time or time.time())
        logger.info("╔════════════════════════════════════════════╗")
        logger.info("║  运行统计                                 ║")
        logger.info("╠════════════════════════════════════════════╣")
        logger.info(f"║  运行时间: {elapsed:.0f}s                    ║")
        logger.info(f"║  交易对数: {len(self.symbols)}                       ║")
        logger.info(f"║  总信号数: {self.total_signals}                       ║")
        logger.info("╠════════════════════════════════════════════╣")
        logger.info("║  各交易对信号统计:                        ║")

        for symbol, unit in self.trading_units.items():
            sh = unit.signal_handler
            strategy_status = unit.strategy.get_status()
            cond = strategy_status.get("condition", 0)
            logger.info(
                f"║  {symbol:<12s} | 信号={sh.total_signals:<3d} | "
                f"持仓={sh.get_summary()['position_side'] or '空':>5s} | "
                f"条件={cond:<+4.1f} ║"
            )

        logger.info("╚════════════════════════════════════════════╝")
        logger.info("👋 机器人已停止")

    def _detect_closed_trades(self):
        """
        比對當前持倉與上一次快照，檢測平倉並記錄到 closed_trades。
        """
        for sym, last_state in list(self._last_positions_snapshot.items()):
            last_qty = last_state.get("qty", 0)
            if last_qty == 0:
                continue
            curr_qty = self.order_manager.get_simulated_position_size(sym)

            # 只處理數量減少的情況（部分或全部平倉）
            if abs(curr_qty) >= abs(last_qty):
                continue

            closed_qty = abs(last_qty) - abs(curr_qty)
            if closed_qty <= 0:
                continue

            side = "long" if last_qty > 0 else "short"
            entry_price = last_state.get("entry_price", 0)

            unit = self.trading_units.get(sym)
            exit_price = unit.latest_price if unit and unit.latest_price > 0 else entry_price

            pnl = 0.0
            if entry_price and entry_price > 0 and exit_price > 0:
                if side == "long":
                    pnl = (exit_price - entry_price) * closed_qty
                else:
                    pnl = (entry_price - exit_price) * closed_qty

            pnl_pct = 0.0
            if entry_price and entry_price > 0 and closed_qty > 0:
                pnl_pct = (pnl / (entry_price * closed_qty)) * 100

            self.closed_trades.append({
                "symbol": sym,
                "side": side,
                "entry_price": round(entry_price, 8) if entry_price else None,
                "exit_price": round(exit_price, 8) if exit_price else None,
                "size": round(closed_qty, 4),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "close_time": datetime.now(timezone.utc).isoformat(),
            })

            logger.info(
                f"📊 [平倉] {sym} {side} {closed_qty:.4f} @ "
                f"{exit_price:.2f} → PnL={pnl:.2f} ({pnl_pct:+.2f}%)"
            )

        # 更新快照供下次比對
        for sym in self.trading_units:
            qty = self.order_manager.get_simulated_position_size(sym)
            entry = self.order_manager._simulated_entry_price.get(sym, 0)
            self._last_positions_snapshot[sym] = {
                "qty": qty,
                "entry_price": entry,
            }

        # 保留最近 100 筆
        if len(self.closed_trades) > 100:
            self.closed_trades = self.closed_trades[-100:]

    async def _flush_status_file(self):
        """
        定期將 Bot 狀態寫入 JSON 檔案，供 Webhook 儀表板讀取。
        每 3 秒寫入一次。
        """
        while self._running:
            try:
                await asyncio.sleep(3)
                if not self._running:
                    break

                # 檢測已平倉交易
                self._detect_closed_trades()

                # 彙整各交易對狀態
                symbols_state = {}
                positions_list = []
                for sym, unit in self.trading_units.items():
                    sh = unit.signal_handler.get_summary()
                    st = unit.strategy.get_status()
                    
                    # 從 Order Manager 取得實際持倉數量（不限於 SignalHandler 內部狀態）
                    pos_qty = self.order_manager.get_simulated_position_size(sym)
                    entry_price = self.order_manager._simulated_entry_price.get(sym, sh["entry_price"])
                    
                    symbols_state[sym] = {
                        "total_signals": sh["total_signals"],
                        "last_signal": sh["last_signal"],
                        "position_side": sh["position_side"],
                        "position_qty": pos_qty,
                        "entry_price": entry_price,
                        "condition": st.get("condition", 0),
                        "higher_tf_candles": unit.higher_tf_candle_count,
                        "aggregator_progress": round(unit.aggregator.progress_pct, 1),
                    }

                    # 收集活躍持倉（含盈虧計算）
                    if pos_qty != 0:
                        current_price = unit.latest_price if unit.latest_price > 0 else entry_price
                        side = "long" if pos_qty > 0 else "short"

                        # 計算浮盈
                        pnl = 0.0
                        if entry_price and entry_price > 0 and current_price > 0:
                            if side == "long":
                                pnl = (current_price - entry_price) * abs(pos_qty)
                            else:
                                pnl = (entry_price - current_price) * abs(pos_qty)

                        # 盈虧百分比（相對入場價值）
                        pnl_pct = 0.0
                        if entry_price and entry_price > 0 and abs(pos_qty) > 0:
                            pnl_pct = (pnl / (entry_price * abs(pos_qty))) * 100

                        positions_list.append({
                            "symbol": sym,
                            "side": side,
                            "size": abs(pos_qty),
                            "entry_price": entry_price,
                            "current_price": round(current_price, 8),
                            "pnl": round(pnl, 2),
                            "pnl_pct": round(pnl_pct, 2),
                        })

                # 取出佇列中最近的訊號（最多 50 條）
                recent_signals = self._signal_queue[-50:]
                self._signal_queue.clear()

                # 計算總權益與總盈虧
                total_pnl = sum(p.get("pnl", 0) for p in positions_list)
                equity = self.config.get("initial_capital", 10000.0) + total_pnl

                # 歷史平倉統計
                closed_count = len(self.closed_trades)
                total_closed_pnl = sum(t.get("pnl", 0) or 0 for t in self.closed_trades)

                status = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "running": self._running,
                    "uptime_seconds": int(time.time() - (self.start_time or time.time())),
                    "total_signals": self.total_signals,
                    "symbol_count": len(self.trading_units),
                    "dry_run": self.config.get("dry_run", True),
                    "network": self.config.get("network", "testnet"),
                    "initial_capital": self.config.get("initial_capital", 10000.0),
                    "equity": round(equity, 2),
                    "total_pnl": round(total_pnl, 2),
                    "closed_trades_summary": {
                        "count": closed_count,
                        "total_closed_pnl": round(total_closed_pnl, 2),
                    },
                    "closed_trades": self.closed_trades[-20:],
                    "signal_queue": recent_signals,
                    "symbols": symbols_state,
                    "positions": positions_list,
                }

                tmp = self._status_file + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(status, f, ensure_ascii=False)
                os.replace(tmp, self._status_file)

            except Exception as e:
                logger.error(f"寫入狀態檔案失敗: {e}")

    async def _flush_status_file_now(self):
        """立即寫入一次狀態檔案（在 stop 時調用）"""
        try:
            # 檢測已平倉交易
            self._detect_closed_trades()

            symbols_state = {}
            positions_list = []
            for sym, unit in self.trading_units.items():
                sh = unit.signal_handler.get_summary()
                st = unit.strategy.get_status()
                
                pos_qty = self.order_manager.get_simulated_position_size(sym)
                entry_price = self.order_manager._simulated_entry_price.get(sym, sh["entry_price"])
                
                symbols_state[sym] = {
                    "total_signals": sh["total_signals"],
                    "last_signal": sh["last_signal"],
                    "position_side": sh["position_side"],
                    "position_qty": pos_qty,
                    "entry_price": entry_price,
                    "condition": st.get("condition", 0),
                    "higher_tf_candles": unit.higher_tf_candle_count,
                }

                if pos_qty != 0:
                    current_price = unit.latest_price if unit.latest_price > 0 else entry_price
                    side = "long" if pos_qty > 0 else "short"

                    pnl = 0.0
                    if entry_price and entry_price > 0 and current_price > 0:
                        if side == "long":
                            pnl = (current_price - entry_price) * abs(pos_qty)
                        else:
                            pnl = (entry_price - current_price) * abs(pos_qty)

                    pnl_pct = 0.0
                    if entry_price and entry_price > 0 and abs(pos_qty) > 0:
                        pnl_pct = (pnl / (entry_price * abs(pos_qty))) * 100

                    positions_list.append({
                        "symbol": sym,
                        "side": side,
                        "size": abs(pos_qty),
                        "entry_price": entry_price,
                        "current_price": round(current_price, 8),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                    })

            recent = self._signal_queue[-50:]
            total_pnl = sum(p.get("pnl", 0) for p in positions_list)
            equity = self.config.get("initial_capital", 10000.0) + total_pnl

            # 歷史平倉統計
            closed_count = len(self.closed_trades)
            total_closed_pnl = sum(t.get("pnl", 0) or 0 for t in self.closed_trades)

            status = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "running": False,
                "uptime_seconds": int(time.time() - (self.start_time or time.time())),
                "total_signals": self.total_signals,
                "signal_queue": recent,
                "initial_capital": self.config.get("initial_capital", 10000.0),
                "equity": round(equity, 2),
                "total_pnl": round(total_pnl, 2),
                "closed_trades_summary": {
                    "count": closed_count,
                    "total_closed_pnl": round(total_closed_pnl, 2),
                },
                "closed_trades": self.closed_trades[-20:],
                "symbols": symbols_state,
                "positions": positions_list,
            }

            with open(self._status_file, "w") as f:
                json.dump(status, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"寫入最終狀態檔案失敗: {e}")


# ================================================================
# 入口
# ================================================================

async def main():
    """主入口"""
    config = load_config()

    setup_logging(config["log_level"])

    logger.info(
        f"🚀 LE VAN DO® OKX 机器人启动 "
        f"(network={config['network']}, dry_run={config['dry_run']}, "
        f"symbols={len(config['symbols'])} 个交易对)"
    )

    bot = OkxTradingBot(config)

    # 注册信号处理
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(bot.stop()))
        except NotImplementedError:
            pass

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
