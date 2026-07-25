"""
LE VAN DO® 技术指标计算器

从 Pine Script v5 移植的核心指标：
  - Heikin Ashi K 线
  - ATR Renko 砖形图（及传统 Renko）
  - EMA（指数移动平均）
  - ATR（平均真实波幅，RMA 平滑）
  - RSI（相对强弱指数，Wilder 平滑）
  - Sideways 过滤器（7 种模式）

所有函数均为纯 Python，输入为 NumPy 数组或列表。
"""
import math
from collections import deque
from typing import List, Tuple, Optional


# ================================================================
# 辅助函数
# ================================================================

def truncate(value: float, decimals: int = 2) -> float:
    """Pine Script 的 truncate 函数：直接截断，不四舍五入"""
    factor = 10.0 ** decimals
    return math.floor(value * factor) / factor


def round_nearest(value: float, step: float) -> float:
    """按指定步长舍入"""
    if step == 0:
        return value
    return round(value / step) * step


# ================================================================
# EMA（指数移动平均）
# ================================================================

def ema(values: List[float], length: int) -> List[float]:
    """
    计算 EMA（指数移动平均）
    Pine Script 公式: alpha = 2 / (length + 1)
    EMA = alpha * price + (1 - alpha) * EMA[1]
    """
    if not values or length <= 0:
        return [0.0] * len(values) if values else []

    alpha = 2.0 / (length + 1)
    result = [0.0] * len(values)

    # SMA 作为初始值
    if len(values) >= length:
        init_sum = sum(values[:length])
        result[length - 1] = init_sum / length

        for i in range(length, len(values)):
            result[i] = alpha * values[i] + (1 - alpha) * result[i - 1]

        # 前缀填充 NaN/Pine-style: 前 length-1 个元素保持为 0
        for i in range(length - 1):
            result[i] = 0.0

    return result


# ================================================================
# RMA（移动平均，Wilder 风格）
# ================================================================

def rma(values: List[float], length: int) -> List[float]:
    """
    RMA（移动平均，Wilder 平滑）
    Pine Script 的 ta.rma()，alpha = 1 / length
    用于 ATR 和 RSI 的内部计算
    """
    if not values or length <= 0:
        return [0.0] * len(values) if values else []

    alpha = 1.0 / length
    result = [0.0] * len(values)

    if len(values) >= length:
        init_sum = sum(values[:length])
        result[length - 1] = init_sum / length

        for i in range(length, len(values)):
            result[i] = alpha * values[i] + (1 - alpha) * result[i - 1]

        for i in range(length - 1):
            result[i] = 0.0

    return result


# ================================================================
# True Range 与 ATR
# ================================================================

def true_range(high: List[float], low: List[float], close: List[float]) -> List[float]:
    """
    计算 True Range
    TR = max(high - low, abs(high - prev_close), abs(low - prev_close))
    """
    n = len(high)
    tr = [0.0] * n
    for i in range(n):
        if i == 0:
            tr[i] = high[i] - low[i]
        else:
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1])
            )
    return tr


def atr(high: List[float], low: List[float], close: List[float], length: int = 20) -> List[float]:
    """
    ATR（平均真实波幅）
    Pine Script 的 ta.atr(length) = RMA(TR, length)
    """
    tr = true_range(high, low, close)
    return rma(tr, length)


# ================================================================
# RSI（相对强弱指数）
# ================================================================

def rsi(close: List[float], length: int = 7) -> List[float]:
    """
    RSI 计算（Wilder 平滑）
    Pine Script 的 ta.rsi(close, length)
    """
    n = len(close)
    if n < 2:
        return [0.0] * n

    # 计算价格变化
    changes = [0.0] * n
    for i in range(1, n):
        changes[i] = close[i] - close[i - 1]

    # 分离上涨和下跌
    gains = [max(ch, 0.0) for ch in changes]
    losses = [max(-ch, 0.0) for ch in changes]

    # Wilder 平滑
    avg_gain = rma(gains, length)
    avg_loss = rma(losses, length)

    # 计算 RSI
    result = [0.0] * n
    for i in range(n):
        if avg_loss[i] == 0:
            result[i] = 100.0 if avg_gain[i] > 0 else 50.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            result[i] = 100.0 - (100.0 / (1.0 + rs))

    return result


# ================================================================
# Heikin Ashi
# ================================================================

def heikin_ashi(
    open_p: List[float],
    high: List[float],
    low: List[float],
    close: List[float]
) -> Tuple[List[float], List[float], List[float], List[float]]:
    """
    Heikin Ashi K 线计算
    HA_Close  = (Open + High + Low + Close) / 4
    HA_Open   = (prev_HA_Open + prev_HA_Close) / 2
    HA_High   = max(High, HA_Open, HA_Close)
    HA_Low    = min(Low, HA_Open, HA_Close)
    """
    n = len(open_p)
    ha_open = [0.0] * n
    ha_high = [0.0] * n
    ha_low = [0.0] * n
    ha_close = [0.0] * n

    for i in range(n):
        ha_close[i] = (open_p[i] + high[i] + low[i] + close[i]) / 4.0

        if i == 0:
            ha_open[i] = (open_p[i] + close[i]) / 2.0
        else:
            ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0

        ha_high[i] = max(high[i], ha_open[i], ha_close[i])
        ha_low[i] = min(low[i], ha_open[i], ha_close[i])

    return ha_open, ha_high, ha_low, ha_close


# ================================================================
# Renko 砖形图
# ================================================================

class RenkoBrick:
    """Renko 砖块"""
    def __init__(self, open_p: float, close_p: float, high: float, low: float,
                 direction: int, timestamp: float):
        self.open = open_p
        self.close = close_p
        self.high = high
        self.low = low
        self.direction = direction  # 1 = up, -1 = down
        self.timestamp = timestamp


class RenkoBuilder:
    """
    ATR-based Renko 砖形图构建器
    对应 Pine Script 的 ticker.renko(syminfo.tickerid, "ATR", atrLen)
    """

    def __init__(self, brick_size: float = 0.0, atr_values: List[float] = None,
                 use_atr: bool = True, trad_len: float = 1000.0):
        """
        :param brick_size: 固定砖大小（use_atr=False 时使用）
        :param atr_values: ATR 值序列（use_atr=True 时使用）
        :param use_atr: 是否使用 ATR 动态砖大小
        :param trad_len: 传统 Renko 砖大小
        """
        self.use_atr = use_atr
        self.trad_len = trad_len
        self.fixed_brick_size = brick_size
        self.atr_values = atr_values or []

        self.bricks: List[RenkoBrick] = []
        self._last_price = None
        self._brick_open = None
        self._brick_high = None
        self._brick_low = None
        self._brick_direction = 0  # 1=up, -1=down, 0=first
        self._atr_index = 0

    @property
    def current_brick_size(self) -> float:
        """获取当前砖大小（ATR 模式下动态变化）"""
        if self.use_atr and self.atr_values:
            idx = min(self._atr_index, len(self.atr_values) - 1)
            return max(self.atr_values[idx], 0.001)
        return max(self.fixed_brick_size, 0.001)

    def update(self, price: float, timestamp: float = 0.0) -> List[RenkoBrick]:
        """
        输入最新价格，返回新生成的砖块列表（可能为空）

        Pine Script Renko 逻辑：
        - 当价格超过当前砖边界时，产生新砖
        - ATR 模式下砖大小 = ATR(atrLen)
        """
        new_bricks = []

        if self._last_price is None:
            self._last_price = price
            self._brick_open = price
            self._brick_high = price
            self._brick_low = price
            return new_bricks

        brick_size = self.current_brick_size
        price_move = price - self._last_price

        # 更新当前砖的极值
        if self._brick_high is None:
            self._brick_high = price
        else:
            self._brick_high = max(self._brick_high, price)
        if self._brick_low is None:
            self._brick_low = price
        else:
            self._brick_low = min(self._brick_low, price)

        if abs(price_move) >= brick_size:
            # 确定方向
            direction = 1 if price_move > 0 else -1

            # 计算砖的 close
            if direction == 1:
                brick_close = self._last_price + brick_size
            else:
                brick_close = self._last_price - brick_size

            brick = RenkoBrick(
                open_p=self._brick_open,
                close_p=brick_close,
                high=self._brick_high,
                low=self._brick_low,
                direction=direction,
                timestamp=timestamp,
            )
            self.bricks.append(brick)
            new_bricks.append(brick)

            # 重置下一砖的起点
            self._brick_open = brick_close
            self._brick_high = brick_close
            self._brick_low = brick_close
            self._last_price = brick_close
            self._brick_direction = direction
            self._atr_index += 1

            # 检查是否有多砖移动（大行情）
            remaining = abs(price_move) - brick_size
            while remaining >= brick_size:
                brick_size = self.current_brick_size
                if direction == 1:
                    brick_close = self._last_price + brick_size
                else:
                    brick_close = self._last_price - brick_size

                brick = RenkoBrick(
                    open_p=self._brick_open,
                    close_p=brick_close,
                    high=self._brick_high,
                    low=self._brick_low,
                    direction=direction,
                    timestamp=timestamp,
                )
                self.bricks.append(brick)
                new_bricks.append(brick)

                self._brick_open = brick_close
                self._brick_high = brick_close
                self._brick_low = brick_close
                self._last_price = brick_close
                remaining -= brick_size
                self._atr_index += 1

        return new_bricks

    @property
    def last_close(self) -> Optional[float]:
        """最后一个砖的收盘价"""
        if self.bricks:
            return self.bricks[-1].close
        return self._last_price

    @property
    def last_open(self) -> Optional[float]:
        """最后一个砖的开盘价"""
        if self.bricks:
            return self.bricks[-1].open
        return self._last_price

    def get_renko_series(self) -> Tuple[List[float], List[float]]:
        """返回 Renko 的 close 和 open 序列（用于 EMA 计算）"""
        closes = [b.close for b in self.bricks]
        opens = [b.open for b in self.bricks]
        return closes, opens


# ================================================================
# Sideways 过滤器（7 种模式）
# ================================================================

def sideways_filter(
    filter_type: str,
    atr_values: List[float],
    atr_ma_values: List[float],
    rsi_values: List[float],
    rsi_top_limit: float = 45,
    rsi_bot_limit: float = 10,
) -> bool:
    """
    Sideways 过滤器，返回 True 表示允许交易

    7 种模式对应 Pine Script:
      filter1 = 'Filter with Atr'
      filter2 = 'Filter with RSI'
      filter3 = 'Atr or RSI'
      filter4 = 'Atr and RSI'
      filter5 = 'No Filtering'
      filter6 = 'Entry Only in sideways market(By ATR or RSI)'
      filter7 = 'Entry Only in sideways market(By ATR and RSI)'
    """
    if not atr_values or not rsi_values:
        return True

    current_atr = atr_values[-1] if atr_values else 0
    current_atr_ma = atr_ma_values[-1] if atr_ma_values else 0
    current_rsi = rsi_values[-1] if rsi_values else 50

    # Pine Script 条件：
    # cndSidwayss1 = atra >= atrMa       (ATR >= ATR_MA → 趋势)
    # cndSidwayss2 = RSI > toplimitrsi or RSI < botlimitrsi  (RSI 超卖/超买 → 趋势)
    # cndSidways = cndSidwayss1 or cndSidwayss2
    # cndSidways1 = cndSidwayss1 and cndSidwayss2
    # Sidwayss1 = atra <= atrMa          (ATR <= ATR_MA → 横盘)
    # Sidwayss2 = RSI < toplimitrsi and RSI > botlimitrsi  (RSI 在范围内 → 横盘)
    # Sidways = Sidwayss1 or Sidwayss2
    # Sidways1 = Sidwayss1 and Sidwayss2

    cnd_sidwayss1 = current_atr >= current_atr_ma
    cnd_sidwayss2 = current_rsi > rsi_top_limit or current_rsi < rsi_bot_limit
    cnd_sidways = cnd_sidwayss1 or cnd_sidwayss2
    cnd_sidways1 = cnd_sidwayss1 and cnd_sidwayss2

    sidwayss1 = current_atr <= current_atr_ma
    sidwayss2 = rsi_bot_limit <= current_rsi <= rsi_top_limit
    sidways = sidwayss1 or sidwayss2
    sidways1 = sidwayss1 and sidwayss2

    # Pine Script: trendType = ...
    filter_map = {
        "Filter with Atr": cnd_sidwayss1,
        "Filter with RSI": cnd_sidwayss2,
        "Atr or RSI": cnd_sidways,
        "Atr and RSI": cnd_sidways1,
        "No Filtering": True,  # RSI > 0 实际上永远为 True
        "Entry Only in sideways market(By ATR or RSI)": sidways,
        "Entry Only in sideways market(By ATR and RSI)": sidways1,
    }

    return filter_map.get(filter_type, True)


# ================================================================
# 时间框架工具
# ================================================================

def timeframe_to_minutes(timeframe: str) -> float:
    """转换 Pine Script 时间框架字符串到分钟数"""
    tf = timeframe.strip().lower()
    if tf.endswith("s"):
        return float(tf[:-1]) / 60.0
    elif tf.endswith("h"):
        return float(tf[:-1]) * 60.0
    elif tf.endswith("d"):
        return float(tf[:-1]) * 1440.0
    elif tf.endswith("w"):
        return float(tf[:-1]) * 10080.0
    elif tf.endswith("m"):
        return float(tf[:-1])
    elif tf == "1":
        return 1.0
    else:
        try:
            return float(tf)
        except ValueError:
            return 1.0
