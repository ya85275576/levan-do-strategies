#!/usr/bin/env python3
"""
LE VAN DO® OKX 原生交易机器人 — 主程序

直接通过 OKX WebSocket 行情数据驱动交易，无需 TradingView。
将 LE VAN DO® Swing Signals 策略从 Pine Script 移植到 Python。

运行模式:
  DRY_RUN=true  (默认) — 模拟模式，仅记录日志
  DRY_RUN=false        — 实盘模式，实际发送 API 请求

架构:
  market_data.py  ←  OKX WebSocket (实时 K 线)
       ↓ 聚合 (tfmult=18)
  strategy.py     ←  策略引擎 (状态机)
       ↓ 信号
  order_manager.py  →  OKX REST API (下单)

启动:
  python bot.py                    # 模拟模式
  DRY_RUN=false python bot.py      # 实盘模式
  或使用 PM2 (见 ecosystem.config.js)
"""
import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

# 确保可以导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config
from market_data import Candle, CandleAggregator, MarketDataSubscriber
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
# 交易信号处理
# ================================================================

class SignalHandler:
    """
    信号处理器

    根据策略产生的信号，决定如何执行交易。
    支持三种止盈止损模式（ATR / Trailing / Options）。
    """

    def __init__(self, order_manager: OkxOrderManager, config: dict):
        self.om = order_manager
        self.config = config
        self.tps_type = config.get("tps_type", "Trailing")
        self.symbol = config.get("symbol", "BTC-USDT")
        self.trade_qty_pct = config.get("trade_qty_pct", 50.0)
        self.initial_capital = config.get("initial_capital", 5000.0)

        # 仓位管理
        self._position_side: Optional[str] = None  # "long" | "short" | None
        self._position_qty: float = 0.0
        self._entry_price: float = 0.0
        self._tp1_pct = config.get("tp1_qty_pct", 50.0)
        self._tp2_pct = config.get("tp2_qty_pct", 30.0)
        self._tp3_pct = config.get("tp3_qty_pct", 20.0)
        self._tp_hit_level: int = 0  # 0=未触发, 1=TP1, 2=TP2, 3=TP3

    def _calculate_qty(self, current_price: float) -> float:
        """根据账户余额和仓位百分比计算交易数量"""
        if self.om.dry_run:
            equity = self.initial_capital
        else:
            # 实盘时获取实际余额
            pass  # TODO: 实现实盘余额获取

        # 简单计算：equity * 百分比 / 价格
        trade_value = equity * (self.trade_qty_pct / 100.0)
        qty = trade_value / current_price if current_price > 0 else 0.001

        # 最小交易量处理
        if self.symbol.startswith("BTC"):
            qty = max(qty, 0.001)
        elif self.symbol.startswith("ETH"):
            qty = max(qty, 0.01)
        else:
            qty = max(qty, 0.1)

        return round(qty, 4)

    async def handle_signal(self, signal: SignalType, tp_sl: TpSlLevels):
        """
        处理策略信号，执行交易

        根据 TPSType 决定执行逻辑：
          - ATR: 三级 TP/SL 自动管理
          - Trailing: 反向开仓时自动关闭反向持仓
          - Options: 仅开仓/手动平仓
        """
        logger.info(f"🚀 处理信号: {signal.value} (TPSType={self.tps_type})")

        action, side = SIGNAL_ACTION_MAP.get(signal, ("none", "none"))

        if action == "none":
            logger.warning(f"未识别的信号: {signal}")
            return

        current_price = tp_sl.entry or 0.0
        if current_price == 0.0:
            logger.error("信号中缺少入场价格")
            return

        if self.tps_type == "Trailing":
            await self._handle_trailing(signal, action, side, current_price, tp_sl)
        elif self.tps_type == "ATR":
            await self._handle_atr(signal, action, side, current_price, tp_sl)
        elif self.tps_type == "Options":
            await self._handle_options(signal, action, side, current_price, tp_sl)
        else:
            logger.warning(f"不支持的 TPSType: {self.tps_type}")

    async def _handle_trailing(self, signal: SignalType, action: str,
                                side: str, price: float, tp_sl: TpSlLevels):
        """
        Trailing 模式

        Pine Script 逻辑:
          if buy and TPSType == "Trailing":
            strategy.close("Short")
            strategy.entry("Long")
          if sell and TPSType == "Trailing":
            strategy.close("Long")
            strategy.entry("Short")
        """
        if signal == SignalType.LONG_ENTRY:
            # 先平空仓，再开多仓
            pos = self.om.get_simulated_position_size(self.symbol)
            if pos < 0:
                logger.info("Trailing: 平空仓")
                await self.om.close_position(self.symbol)
            qty = self._calculate_qty(price)
            logger.info(f"Trailing: 开多仓 {qty} @ {price:.2f}")
            await self.om.set_leverage(self.symbol)
            await self.om.place_order(self.symbol, "buy", qty, "market")

        elif signal == SignalType.SHORT_ENTRY:
            # 先平多仓，再开空仓
            pos = self.om.get_simulated_position_size(self.symbol)
            if pos > 0:
                logger.info("Trailing: 平多仓")
                await self.om.close_position(self.symbol)
            qty = self._calculate_qty(price)
            logger.info(f"Trailing: 开空仓 {qty} @ {price:.2f}")
            await self.om.set_leverage(self.symbol)
            await self.om.place_order(self.symbol, "sell", qty, "market")

    async def _handle_atr(self, signal: SignalType, action: str,
                           side: str, price: float, tp_sl: TpSlLevels):
        """
        ATR 模式 — 三级 TP/SL 自动管理

        Pine Script 逻辑:
          if strategy.position_size <= 0 and longE:
            strategy.entry("Long")
          if position > 0 and condition == 1.0:
            strategy.exit(..., limit=tp1Line, stop=slLine, qty_percent=50)
          if position > 0 and condition == 1.1:
            strategy.exit(..., limit=tp2Line, stop=slLine, qty_percent=30)
          if position > 0 and condition == 1.2:
            strategy.exit(..., limit=tp3Line, stop=slLine, qty_percent=20)
        """
        # 根据信号类型执行操作
        if signal == SignalType.LONG_ENTRY:
            # 确保无持仓或平空仓
            if self._position_side == "short":
                await self.om.close_position(self.symbol)
            qty = self._calculate_qty(price)
            logger.info(f"ATR: 开多仓 {qty} @ {price:.2f}, TP1={tp_sl.tp1:.2f}, "
                        f"SL={tp_sl.sl:.2f}")
            await self.om.set_leverage(self.symbol)
            await self.om.place_order(self.symbol, "buy", qty, "market")
            self._position_side = "long"
            self._entry_price = price

        elif signal == SignalType.SHORT_ENTRY:
            if self._position_side == "long":
                await self.om.close_position(self.symbol)
            qty = self._calculate_qty(price)
            logger.info(f"ATR: 开空仓 {qty} @ {price:.2f}, TP1={tp_sl.tp1:.2f}, "
                        f"SL={tp_sl.sl:.2f}")
            await self.om.set_leverage(self.symbol)
            await self.om.place_order(self.symbol, "sell", qty, "market")
            self._position_side = "short"
            self._entry_price = price

        elif signal in (SignalType.LONG_TP1, SignalType.SHORT_TP1):
            qty_pct = self._tp1_pct
            logger.info(f"ATR: TP1 触发, 平仓 {qty_pct}%")
            # 获取当前持仓计算平仓数量
            pos = self.om.get_simulated_position_size(self.symbol)
            qty = abs(pos) * (qty_pct / 100.0)
            close_side = "sell" if self._position_side == "long" else "buy"
            await self.om.place_order(self.symbol, close_side, round(qty, 4), "market")

        elif signal in (SignalType.LONG_TP2, SignalType.SHORT_TP2):
            qty_pct = self._tp2_pct
            logger.info(f"ATR: TP2 触发, 平仓 {qty_pct}%")
            pos = self.om.get_simulated_position_size(self.symbol)
            qty = abs(pos) * (qty_pct / 100.0)
            close_side = "sell" if self._position_side == "long" else "buy"
            await self.om.place_order(self.symbol, close_side, round(qty, 4), "market")

        elif signal in (SignalType.LONG_TP3, SignalType.SHORT_TP3):
            qty_pct = self._tp3_pct
            logger.info(f"ATR: TP3 触发, 平仓 {qty_pct}%")
            pos = self.om.get_simulated_position_size(self.symbol)
            qty = abs(pos) * (qty_pct / 100.0)
            close_side = "sell" if self._position_side == "long" else "buy"
            await self.om.place_order(self.symbol, close_side, round(qty, 4), "market")

        elif signal in (SignalType.LONG_SL, SignalType.SHORT_SL):
            logger.warning(f"ATR: SL 触发, 全仓平仓")
            await self.om.close_position(self.symbol)
            self._position_side = None

    async def _handle_options(self, signal: SignalType, action: str,
                               side: str, price: float, tp_sl: TpSlLevels):
        """
        Options 模式

        Pine Script 逻辑:
          if buy and TPSType == "Options":
            strategy.entry("Long")
          if sell and TPSType == "Options":
            strategy.close("Long")
        """
        if signal == SignalType.LONG_ENTRY:
            qty = self._calculate_qty(price)
            logger.info(f"Options: 开多仓 {qty} @ {price:.2f}")
            await self.om.set_leverage(self.symbol)
            await self.om.place_order(self.symbol, "buy", qty, "market")
            self._position_side = "long"

        elif signal == SignalType.SHORT_ENTRY:
            logger.info("Options: 平多仓（收到空头信号）")
            await self.om.close_position(self.symbol)
            self._position_side = None

        elif signal == SignalType.LONG_EXIT:
            logger.info("Options: 手动平多仓")
            await self.om.close_position(self.symbol)
            self._position_side = None

        elif signal == SignalType.SHORT_EXIT:
            logger.info("Options: 手动平空仓")
            await self.om.close_position(self.symbol)
            self._position_side = None

    def reset(self):
        """重置仓位状态"""
        self._position_side = None
        self._position_qty = 0.0
        self._entry_price = 0.0
        self._tp_hit_level = 0


# ================================================================
# 机器人主类
# ================================================================

class OkxTradingBot:
    """
    LE VAN DO® OKX 原生交易机器人

    整合市场数据、策略引擎、订单执行三大模块。
    """

    def __init__(self, config: dict):
        self.config = config
        self.symbol = config["symbol"]
        self.base_tf_sec = config.get("base_timeframe_min", 1) * 60
        self.higher_tf_sec = self.base_tf_sec * config.get("tf_mult", 18)

        # 模块
        self.order_manager = OkxOrderManager(
            api_key=config["api_key"],
            api_secret=config["api_secret"],
            api_passphrase=config["api_passphrase"],
            rest_url=config["rest_url"],
            dry_run=config["dry_run"],
            symbol=self.symbol,
            leverage=config.get("default_leverage", 1),
            position_mode=config.get("position_mode", "isolated"),
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

        self.signal_handler = SignalHandler(self.order_manager, config)
        self.strategy = LeVanDoStrategy(
            params=strategy_params,
            on_signal=self._on_strategy_signal,
        )

        # 市场数据
        self.subscriber: Optional[MarketDataSubscriber] = None
        self.aggregator: Optional[CandleAggregator] = None

        # 运行控制
        self._running = False
        self._tasks: list = []

        # 统计
        self.start_time: Optional[float] = None
        self.higher_tf_candle_count: int = 0
        self.total_signals: int = 0

    # ---- 策略信号回调 ----

    def _on_strategy_signal(self, signal: SignalType, tp_sl: TpSlLevels):
        """策略引擎产生信号时的回调（同步）"""
        self.total_signals += 1
        logger.info(f"📨 策略信号: {signal.value} (累计: {self.total_signals})")

    # ---- 市场数据回调 ----

    def _on_base_candle(self, symbol: str, candle: Candle):
        """基础周期 K 线更新回调"""
        if self.aggregator is None:
            return

        # 将基础 K 线送入聚合器
        completed = self.aggregator.add_candle(CandleData(
            timestamp=candle.timestamp,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
        ))

        # 如果高周期 K 线完成，送入策略引擎
        if completed:
            self._on_higher_tf_candle(completed)

    def _on_higher_tf_candle(self, candle: CandleData):
        """高周期 K 线完成回调"""
        self.higher_tf_candle_count += 1

        logger.debug(
            f"📊 高周期 K 线 #{self.higher_tf_candle_count}: "
            f"O={candle.open:.2f} H={candle.high:.2f} "
            f"L={candle.low:.2f} C={candle.close:.2f}"
        )

        # 更新策略引擎
        self.strategy.update_higher_tf_candle(candle)

        # 执行策略分析
        signal = self.strategy.analyze()

        # 如果有信号，异步执行交易
        if signal != SignalType.NONE:
            asyncio.ensure_future(
                self.signal_handler.handle_signal(
                    signal, self.strategy.tp_sl
                )
            )

    # ---- 启动与停止 ----

    async def start(self):
        """启动机器人"""
        self._running = True
        self.start_time = time.time()

        logger.info("╔══════════════════════════════════════════╗")
        logger.info("║  LE VAN DO® OKX 原生交易机器人          ║")
        logger.info("╠══════════════════════════════════════════╣")
        logger.info(f"║  交易对:     {self.symbol:<32s}║")
        logger.info(f"║  网络:       {'🟡 测试网' if self.config['is_testnet'] else '🔴 实盘':<32s}║")
        logger.info(f"║  模拟模式:   {'🟢 启用' if self.config['dry_run'] else '🔴 关闭':<32s}║")
        logger.info(f"║  交易模式:   {self.config.get('setup_type', 'Open/Close'):<32s}║")
        logger.info(f"║  TP/SL 模式: {self.config.get('tps_type', 'Trailing'):<32s}║")
        logger.info(f"║  基础周期:   {self.config['base_timeframe_min']}m -> 高周期 {self.higher_tf_sec // 60}m     ║")
        logger.info(f"║  Sideways:   {self.config.get('sideways_filter', 'No Filtering'):<32s}║")
        logger.info("╚══════════════════════════════════════════╝")

        # 初始化 K 线聚合器
        self.aggregator = CandleAggregator(
            base_seconds=self.base_tf_sec,
            target_seconds=self.higher_tf_sec,
        )

        # 初始化市场数据订阅器
        self.subscriber = MarketDataSubscriber(
            ws_url=self.config["ws_url"],
            symbols=[self.symbol],
            base_timeframe_sec=self.base_tf_sec,
            on_candle=self._on_base_candle,
            on_candle_closed=None,  # 使用实时更新，不等待收盘
        )

        # 异步运行
        try:
            await self.subscriber.run()
        except asyncio.CancelledError:
            logger.info("机器人收到停止信号")
        finally:
            await self.stop()

    async def stop(self):
        """停止机器人"""
        self._running = False

        if self.subscriber:
            await self.subscriber.stop()

        await self.order_manager.close()

        elapsed = time.time() - (self.start_time or time.time())
        logger.info(
            f"📊 运行统计: "
            f"运行时间={elapsed:.0f}s, "
            f"高周期K线={self.higher_tf_candle_count}, "
            f"总信号={self.total_signals}"
        )

        if self.config["dry_run"]:
            positions = self.order_manager.get_simulated_position_size(self.symbol)
            logger.info(f"📊 模拟持仓: {self.symbol} = {positions:.4f}")

            pnl = await self.order_manager.get_simulated_pnl(
                self.strategy.state.closes[-1] if self.strategy.state.closes else 0,
                self.symbol,
            )
            logger.info(f"📊 模拟未实现盈亏: ${pnl:.2f}")

        logger.info("👋 机器人已停止")


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
        f"symbol={config['symbol']})"
    )

    bot = OkxTradingBot(config)

    # 注册信号处理
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(bot.stop()))
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            pass

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
