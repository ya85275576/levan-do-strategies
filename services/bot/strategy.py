"""
LE VAN DO® Swing Signals 策略引擎

从 Pine Script v5 移植的核心策略逻辑，包含：
  - Heikin Ashi K 线信号 (Open/Close 模式)
  - Renko 砖形图信号 (EMA 交叉模式)
  - Sideways 过滤器（7 种模式）
  - ATR 三级 TP/SL 管理
  - 状态机（condition 变量）
  - 交易信号生成

设计原则：
  - 纯逻辑层，与交易所解耦
  - 输入原始 OHLCV 数据，输出交易信号
  - 状态由 StrategyState 管理
"""
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from indicators import (
    RenkoBuilder,
    atr,
    ema,
    heikin_ashi,
    rsi,
    sideways_filter,
    truncate,
)

logger = logging.getLogger("bot.strategy")


class SignalType(Enum):
    """交易信号类型"""
    NONE = "none"
    LONG_ENTRY = "longE"        # 多头开仓
    SHORT_ENTRY = "shortE"      # 空头开仓
    LONG_EXIT = "longX"         # 多头平仓（手动）
    SHORT_EXIT = "shortX"       # 空头平仓（手动）
    LONG_TP1 = "longTP1"        # 多头 TP1
    LONG_TP2 = "longTP2"        # 多头 TP2
    LONG_TP3 = "longTP3"        # 多头 TP3
    LONG_SL = "longSL"          # 多头止损
    SHORT_TP1 = "shortTP1"      # 空头 TP1
    SHORT_TP2 = "shortTP2"      # 空头 TP2
    SHORT_TP3 = "shortTP3"      # 空头 TP3
    SHORT_SL = "shortSL"        # 空头止损


@dataclass
class StrategyParams:
    """策略参数（对应 Pine Script 的 input 和硬编码参数）"""
    # 交易模式
    tps_type: str = "Trailing"          # ATR | Trailing | Options
    setup_type: str = "Open/Close"      # Open/Close | Renko

    # 时间框架
    tf_mult: int = 18

    # Sideways 过滤器
    sideways_filter: str = "No Filtering"

    # RSI
    rsi_length: int = 7
    rsi_top_limit: int = 45
    rsi_bot_limit: int = 10

    # ATR 过滤
    atr_filter_len: int = 5
    atr_ma_len: int = 5

    # Renko
    renko_atr_len: int = 3
    renko_ema1_length: int = 2
    renko_ema2_length: int = 10

    # 风险管理
    atr_length: int = 20
    profit_factor: float = 2.5
    stop_factor: float = 1.0

    # 三级止盈百分比
    tp1_qty_pct: float = 50.0
    tp2_qty_pct: float = 30.0
    tp3_qty_pct: float = 20.0


@dataclass
class TpSlLevels:
    """TP/SL 水平"""
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    sl: float = 0.0
    entry: float = 0.0


@dataclass
class CandleData:
    """单根 K 线数据（高周期聚合）"""
    timestamp: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0


@dataclass
class StrategyState:
    """
    策略状态机

    对应 Pine Script 的:
      - condition: float  (0.0=无仓位, 1.0=多仓进场, 1.1=TP1完成, 1.2=TP2完成, 1.3=全部完成)
      - entryLine, slLine, tp1Line, tp2Line, tp3Line
      - wasLong, wasShort, leTrigger, seTrigger, lxTrigger, sxTrigger
    """
    # ---- 状态变量 ----
    condition: float = 0.0  # 0=无仓位, 1.0~1.3=多头, -1.0~(-1.3)=空头
    entry_line: float = 0.0
    sl_line: float = 0.0
    tp1_line: float = 0.0
    tp2_line: float = 0.0
    tp3_line: float = 0.0

    # 触发标志
    le_trigger: bool = False   # Long Entry 触发
    se_trigger: bool = False   # Short Entry 触发
    lx_trigger: bool = False   # Long Exit 触发
    sx_trigger: bool = False   # Short Exit 触发

    # 最新数据
    ha_open: float = 0.0
    ha_close: float = 0.0
    renko_open: float = 0.0
    renko_close: float = 0.0

    # ---- 数据缓冲区 ----
    closes: List[float] = field(default_factory=list)
    highs: List[float] = field(default_factory=list)
    lows: List[float] = field(default_factory=list)
    opens: List[float] = field(default_factory=list)

    # ---- 信号计数 ----
    last_signal: SignalType = SignalType.NONE
    signal_count: int = 0

    # ---- 性能统计（模拟用） ----
    trades: int = 0
    wins: int = 0
    losses: int = 0
    simulated_equity: float = 5000.0


class LeVanDoStrategy:
    """
    LE VAN DO® Swing Signals 策略引擎

    使用方式:
      1. 调用 update_higher_tf_candle(candle) 传入高周期 K 线
      2. 调用 analyze() 获取当前信号
      3. 信号产生后调用 on_signal 回调
    """

    def __init__(
        self,
        params: Optional[StrategyParams] = None,
        on_signal: Optional[Callable[[SignalType, TpSlLevels], None]] = None,
    ):
        self.params = params or StrategyParams()
        self.on_signal = on_signal

        self.state = StrategyState()
        self.tp_sl = TpSlLevels()
        self.renko_builder: Optional[RenkoBuilder] = None

        # 上次的 trigger 值（用于 detect crossover/crossunder）
        self._prev_ha_open: float = 0.0
        self._prev_ha_close: float = 0.0
        self._prev_renko_ema1: float = 0.0
        self._prev_renko_ema2: float = 0.0
        self._prev_condition: float = 0.0

        logger.info(
            f"📈 策略引擎初始化: setup={params.setup_type}, "
            f"tps={params.tps_type}, filter={params.sideways_filter}"
        )

    # ================================================================
    # 数据预处理
    # ================================================================

    def update_higher_tf_candle(self, candle: CandleData):
        """
        输入高周期 K 线数据（已聚合的 candles）

        维护内部数据缓冲区，用于指标计算。
        """
        self.state.opens.append(candle.open)
        self.state.highs.append(candle.high)
        self.state.lows.append(candle.low)
        self.state.closes.append(candle.close)

        # 限制缓冲区大小
        max_len = max(
            self.params.atr_length + 5,
            self.params.rsi_length + 5,
            self.params.atr_filter_len + self.params.atr_ma_len + 5,
            100,
        )
        for buf in (self.state.opens, self.state.highs,
                    self.state.lows, self.state.closes):
            while len(buf) > max_len:
                buf.pop(0)

    # ================================================================
    # Heikin Ashi 计算
    # ================================================================

    def _calc_heikin_ashi(self):
        """计算当前数据缓冲区的 Heikin Ashi 值"""
        n = len(self.state.closes)
        if n < 2:
            return

        ha_opens, _, _, ha_closes = heikin_ashi(
            self.state.opens, self.state.highs,
            self.state.lows, self.state.closes,
        )

        self.state.ha_open = ha_opens[-1] if ha_opens else 0.0
        self.state.ha_close = ha_closes[-1] if ha_closes else 0.0

    # ================================================================
    # Renko 计算
    # ================================================================

    def _update_renko(self, close_price: float, timestamp: int,
                         atr_values: Optional[List[float]] = None):
        """更新 Renko 砖形图"""
        if self.renko_builder is None:
            self.renko_builder = RenkoBuilder(
                use_atr=True,
                atr_values=atr_values or [self.params.renko_atr_len * 10],
            )
            # 初始添加
            self.renko_builder._last_price = close_price
        elif atr_values and len(atr_values) > 0:
            # 更新 ATR 值序列供动态砖大小
            self.renko_builder.atr_values = atr_values

        self.renko_builder.update(close_price, float(timestamp))

        # 更新 state 中的 renko 值
        if self.renko_builder.bricks:
            self.state.renko_close = self.renko_builder.last_close or close_price
            self.state.renko_open = self.renko_builder.last_open or close_price
        else:
            self.state.renko_close = close_price
            self.state.renko_open = close_price

    # ================================================================
    # 指标计算
    # ================================================================

    def _calc_atr(self) -> List[float]:
        """计算 ATR"""
        n = len(self.state.closes)
        if n < self.params.atr_length + 2:
            return [0.0] * n

        return atr(self.state.highs, self.state.lows,
                   self.state.closes, self.params.atr_length)

    def _calc_rsi(self) -> List[float]:
        """计算 RSI"""
        n = len(self.state.closes)
        if n < self.params.rsi_length + 2:
            return [50.0] * n

        return rsi(self.state.closes, self.params.rsi_length)

    def _calc_ema(self, values: List[float], length: int) -> List[float]:
        """计算 EMA"""
        return ema(values, length)

    # ================================================================
    # 信号检测
    # ================================================================

    def _detect_crossover(self, curr: float, prev: float, curr2: float, prev2: float) -> bool:
        """检测 crossover: a 上穿 b"""
        return prev <= prev2 and curr > curr2

    def _detect_crossunder(self, curr: float, prev: float, curr2: float, prev2: float) -> bool:
        """检测 crossunder: a 下穿 b"""
        return prev >= prev2 and curr < curr2

    def _detect_price_cross(self, price: float, prev_price: float,
                            level: float) -> Tuple[bool, bool]:
        """
        检测价格是否穿越某个水平线
        Returns: (cross_above, cross_below)
        """
        cross_above = prev_price <= level and price > level
        cross_below = prev_price >= level and price < level
        return cross_above, cross_below

    # ================================================================
    # 核心策略逻辑（对应 Pine Script 主逻辑）
    # ================================================================

    def analyze(self) -> SignalType:
        """
        执行完整的策略分析，返回当前信号。

        对应 Pine Script 的每一根 K 线收盘时的逻辑。
        """
        n = len(self.state.closes)
        if n < 5:
            return SignalType.NONE

        p = self.params
        state = self.state

        # ---- 1. 计算指标 ----
        self._calc_heikin_ashi()

        atr_values = self._calc_atr()
        rsi_values = self._calc_rsi()
        self._update_renko(state.closes[-1], 0, atr_values)

        # ATR 过滤计算
        atra = atr(
            state.highs, state.lows, state.closes, p.atr_filter_len
        )
        atr_ma = ema(atra, p.atr_ma_len)

        current_atr = atr_values[-1] if atr_values else 0.0
        current_rsi = rsi_values[-1] if rsi_values else 50.0

        # ---- 2. Sideways 过滤器 ----
        trend_allowed = sideways_filter(
            filter_type=p.sideways_filter,
            atr_values=atra,
            atr_ma_values=atr_ma,
            rsi_values=rsi_values,
            rsi_top_limit=p.rsi_top_limit,
            rsi_bot_limit=p.rsi_bot_limit,
        )

        # ---- 3. 信号触发条件 ----
        # Open/Close 模式：HA close 穿越 HA open
        if p.setup_type == "Open/Close":
            entry_buy = self._detect_crossover(
                state.ha_close, self._prev_ha_close,
                state.ha_open, self._prev_ha_open,
            )
            entry_sell = self._detect_crossunder(
                state.ha_close, self._prev_ha_close,
                state.ha_open, self._prev_ha_open,
            )
        else:
            entry_buy = False
            entry_sell = False

        # Renko 模式：EMA 交叉
        if p.setup_type == "Renko" and self.renko_builder and self.renko_builder.bricks:
            renko_closes, _ = self.renko_builder.get_renko_series()
            if len(renko_closes) >= max(p.renko_ema1_length, p.renko_ema2_length) + 2:
                ema1 = self._calc_ema(renko_closes, p.renko_ema1_length)
                ema2 = self._calc_ema(renko_closes, p.renko_ema2_length)

                if len(ema1) >= 2 and len(ema2) >= 2:
                    entry_buy = self._detect_crossover(
                        ema1[-1], ema1[-2], ema2[-1], ema2[-2]
                    )
                    entry_sell = self._detect_crossunder(
                        ema1[-1], ema1[-2], ema2[-1], ema2[-2]
                    )

        # 应用过滤器
        le_trigger = entry_buy and trend_allowed
        se_trigger = entry_sell and trend_allowed

        state.le_trigger = le_trigger
        state.se_trigger = se_trigger

        logger.debug(
            f"分析: HA_O={state.ha_open:.2f} HA_C={state.ha_close:.2f} "
            f"ATR={current_atr:.2f} RSI={current_rsi:.2f} "
            f"trend={trend_allowed} buy={entry_buy} sell={entry_sell}"
        )

        # ---- 4. TP/SL 计算 ----
        # Pine: tpatrValue = ta.atr(atrLength)
        # takeProfit1_buy = 1 * profitFactor * tpatrValue
        tp_distance = p.profit_factor * current_atr
        sl_distance = p.stop_factor * current_atr * p.profit_factor  # Pine: stopLoss_buy = close - takeProfit1_buy

        # Pine:
        # takeProfit1_buy = 1 * profitFactor * tpatrValue
        # takeProfit2_buy = 2 * profitFactor * tpatrValue
        # takeProfit3_buy = 3 * profitFactor * tpatrValue
        # stopLoss_buy = close - takeProfit1_buy
        # takeProfit1_sell = 1 * profitFactor * tpatrValue
        # stopLoss_sell = close + takeProfit1_sell

        current_price = state.closes[-1] if state.closes else 0.0

        # ---- 5. 状态机（对应 Pine Script 的 condition 变量） ----
        prev_condition = self._prev_condition
        new_condition = prev_condition

        # Pine Script 状态转换:
        # switch
        #   leTrigger and condition[1] <=  0.0 => condition :=  1.0
        #   seTrigger and condition[1] >=  0.0 => condition := -1.0
        #   tp3Long   and condition[1] ==  1.2 => condition :=  1.3
        #   tp3Short  and condition[1] == -1.2 => condition := -1.3
        #   tp2Long   and condition[1] ==  1.1 => condition :=  1.2
        #   tp2Short  and condition[1] == -1.1 => condition := -1.2
        #   tp1Long   and condition[1] ==  1.0 => condition :=  1.1
        #   tp1Short  and condition[1] == -1.0 => condition := -1.1
        #   slLong    and condition[1] >=  1.0 => condition :=  0.0
        #   slShort   and condition[1] <= -1.0 => condition :=  0.0
        #   lxTrigger and condition[1] >=  1.0 => condition :=  0.0
        #   sxTrigger and condition[1] <= -1.0 => condition :=  0.0

        # 检测 TP/SL 穿越
        tp1_hit_long = False
        tp2_hit_long = False
        tp3_hit_long = False
        sl_hit_long = False
        tp1_hit_short = False
        tp2_hit_short = False
        tp3_hit_short = False
        sl_hit_short = False

        if prev_condition >= 1.0:
            # 多头持仓中
            entry_price = state.entry_line
            tp1_level = entry_price + tp_distance
            tp2_level = entry_price + 2 * tp_distance
            tp3_level = entry_price + 3 * tp_distance
            sl_level = entry_price - sl_distance

            cross_above_1, _ = self._detect_price_cross(
                current_price, state.closes[-2] if len(state.closes) >= 2 else current_price,
                tp1_level
            )
            cross_above_2, _ = self._detect_price_cross(
                current_price, state.closes[-2] if len(state.closes) >= 2 else current_price,
                tp2_level
            )
            cross_above_3, _ = self._detect_price_cross(
                current_price, state.closes[-2] if len(state.closes) >= 2 else current_price,
                tp3_level
            )
            _, cross_below_sl = self._detect_price_cross(
                current_price, state.closes[-2] if len(state.closes) >= 2 else current_price,
                sl_level
            )

            tp1_hit_long = cross_above_1 and prev_condition == 1.0
            tp2_hit_long = cross_above_2 and prev_condition == 1.1
            tp3_hit_long = cross_above_3 and prev_condition == 1.2
            sl_hit_long = cross_below_sl and prev_condition >= 1.0

        elif prev_condition <= -1.0:
            # 空头持仓中
            entry_price = state.entry_line
            tp1_level = entry_price - tp_distance
            tp2_level = entry_price - 2 * tp_distance
            tp3_level = entry_price - 3 * tp_distance
            sl_level = entry_price + sl_distance

            _, cross_below_1 = self._detect_price_cross(
                current_price, state.closes[-2] if len(state.closes) >= 2 else current_price,
                tp1_level
            )
            _, cross_below_2 = self._detect_price_cross(
                current_price, state.closes[-2] if len(state.closes) >= 2 else current_price,
                tp2_level
            )
            _, cross_below_3 = self._detect_price_cross(
                current_price, state.closes[-2] if len(state.closes) >= 2 else current_price,
                tp3_level
            )
            cross_above_sl, _ = self._detect_price_cross(
                current_price, state.closes[-2] if len(state.closes) >= 2 else current_price,
                sl_level
            )

            tp1_hit_short = cross_below_1 and prev_condition == -1.0
            tp2_hit_short = cross_below_2 and prev_condition == -1.1
            tp3_hit_short = cross_below_3 and prev_condition == -1.2
            sl_hit_short = cross_above_sl and prev_condition <= -1.0

        # ---- 状态转换 ----
        if le_trigger and prev_condition <= 0.0:
            new_condition = 1.0
            state.entry_line = current_price
            state.tp1_line = current_price + tp_distance
            state.tp2_line = current_price + 2 * tp_distance
            state.tp3_line = current_price + 3 * tp_distance
            state.sl_line = current_price - sl_distance

        elif se_trigger and prev_condition >= 0.0:
            new_condition = -1.0
            state.entry_line = current_price
            state.tp1_line = current_price - tp_distance
            state.tp2_line = current_price - 2 * tp_distance
            state.tp3_line = current_price - 3 * tp_distance
            state.sl_line = current_price + sl_distance

        elif tp1_hit_long and prev_condition == 1.0:
            new_condition = 1.1
        elif tp1_hit_short and prev_condition == -1.0:
            new_condition = -1.1
        elif tp2_hit_long and prev_condition == 1.1:
            new_condition = 1.2
        elif tp2_hit_short and prev_condition == -1.1:
            new_condition = -1.2
        elif tp3_hit_long and prev_condition == 1.2:
            new_condition = 1.3
        elif tp3_hit_short and prev_condition == -1.2:
            new_condition = -1.3
        elif sl_hit_long and prev_condition >= 1.0:
            new_condition = 0.0
        elif sl_hit_short and prev_condition <= -1.0:
            new_condition = 0.0

        # ---- 更新状态 ----
        self._prev_condition = new_condition
        state.condition = new_condition

        # ---- 更新 prev 值 ----
        self._prev_ha_open = state.ha_open
        self._prev_ha_close = state.ha_close

        # ---- 确定信号 ----
        signal = SignalType.NONE

        # Pine: longE = leTrigger and condition[1] <= 0.0 and condition == 1.0
        if le_trigger and prev_condition <= 0.0 and new_condition == 1.0:
            signal = SignalType.LONG_ENTRY
        elif se_trigger and prev_condition >= 0.0 and new_condition == -1.0:
            signal = SignalType.SHORT_ENTRY
        elif tp1_hit_long and prev_condition == 1.0 and new_condition == 1.1:
            signal = SignalType.LONG_TP1
        elif tp1_hit_short and prev_condition == -1.0 and new_condition == -1.1:
            signal = SignalType.SHORT_TP1
        elif tp2_hit_long and prev_condition == 1.1 and new_condition == 1.2:
            signal = SignalType.LONG_TP2
        elif tp2_hit_short and prev_condition == -1.1 and new_condition == -1.2:
            signal = SignalType.SHORT_TP2
        elif tp3_hit_long and prev_condition == 1.2 and new_condition == 1.3:
            signal = SignalType.LONG_TP3
        elif tp3_hit_short and prev_condition == -1.2 and new_condition == -1.3:
            signal = SignalType.SHORT_TP3
        elif sl_hit_long and prev_condition >= 1.0 and new_condition == 0.0:
            signal = SignalType.LONG_SL
        elif sl_hit_short and prev_condition <= -1.0 and new_condition == 0.0:
            signal = SignalType.SHORT_SL

        # 记录信号
        if signal != SignalType.NONE:
            state.last_signal = signal
            state.signal_count += 1
            logger.info(
                f"🚨 信号产生: {signal.value} "
                f"(cond: {prev_condition:.1f}->{new_condition:.1f}, "
                f"price={current_price:.2f})"
            )

            # 更新 TP/SL 信息
            self.tp_sl.entry = state.entry_line
            self.tp_sl.tp1 = state.tp1_line
            self.tp_sl.tp2 = state.tp2_line
            self.tp_sl.tp3 = state.tp3_line
            self.tp_sl.sl = state.sl_line

            # 回调
            if self.on_signal:
                self.on_signal(signal, self.tp_sl)

        return signal

    # ================================================================
    # 重置
    # ================================================================

    def reset(self):
        """重置策略状态"""
        self.state = StrategyState()
        self.tp_sl = TpSlLevels()
        self.renko_builder = None
        self._prev_ha_open = 0.0
        self._prev_ha_close = 0.0
        self._prev_renko_ema1 = 0.0
        self._prev_renko_ema2 = 0.0
        self._prev_condition = 0.0
        logger.info("🔄 策略状态已重置")

    def get_status(self) -> dict:
        """获取策略状态摘要"""
        return {
            "condition": self.state.condition,
            "entry": round(self.state.entry_line, 2) if self.state.entry_line else None,
            "sl": round(self.state.sl_line, 2) if self.state.sl_line else None,
            "tp1": round(self.state.tp1_line, 2) if self.state.tp1_line else None,
            "tp2": round(self.state.tp2_line, 2) if self.state.tp2_line else None,
            "tp3": round(self.state.tp3_line, 2) if self.state.tp3_line else None,
            "setup": self.params.setup_type,
            "tps": self.params.tps_type,
            "filter": self.params.sideways_filter,
            "last_signal": self.state.last_signal.value,
            "signal_count": self.state.signal_count,
        }
