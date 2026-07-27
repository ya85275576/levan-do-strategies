#!/usr/bin/env python3
"""
OKX 事件合約策略模塊 — 短期價格動量策略

策略邏輯：
  1. 獲取底層資產（如 BTC-USDT）最近 N 根 1 分鐘 K 線
  2. 比較最近 1 根 K 線收盤價 vs 前 N-1 根平均收盤價
  3. 價格上漲 → 信號 UP（看漲）
  4. 價格下跌 → 信號 DOWN（看跌）
  5. 變動幅度低於閾值 → 橫盤不交易
"""
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("event-bot.strategy")


class Signal(Enum):
    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"


@dataclass
class SignalResult:
    """策略信號結果"""
    signal: Signal
    confidence: float  # 0.0 ~ 1.0
    momentum_pct: float  # 價格變動百分比
    current_price: float  # 當前底層資產價格
    lookback_avg: float  # 回看平均價格
    reason: str = ""


class MomentumStrategy:
    """
    短期價格動量策略

    基於 1 分鐘 K 線的簡短趨勢跟蹤。
    """

    def __init__(self, lookback: int = 3, threshold_pct: float = 0.05):
        """
        :param lookback: 回看 K 線數量（含最近一根）
        :param threshold_pct: 價格變動最小百分比（低於此值視為橫盤）
        """
        self.lookback = lookback
        self.threshold_pct = threshold_pct

    def analyze(self, candles: list) -> SignalResult:
        """
        分析 K 線數據，產生交易信號

        :param candles: K 線數據列表，每條為 [ts, o, h, l, c, vol, ...]
                        （來自 okx market candles 的標準格式）
        :returns: SignalResult
        """
        if not candles or len(candles) < 2:
            return SignalResult(
                signal=Signal.NONE,
                confidence=0.0,
                momentum_pct=0.0,
                current_price=0.0,
                lookback_avg=0.0,
                reason="K線數據不足",
            )

        # 解析收盤價
        closes = []
        for c in candles:
            try:
                close = float(c[4])  # 第 5 個欄位是收盤價
                closes.append(close)
            except (IndexError, ValueError, TypeError):
                continue

        if len(closes) < 2:
            return SignalResult(
                signal=Signal.NONE,
                confidence=0.0,
                momentum_pct=0.0,
                current_price=0.0,
                lookback_avg=0.0,
                reason="收盤價數據不足",
            )

        current_price = closes[-1]
        # 前 N-1 根收盤價平均值（不含最新一根）
        prev_closes = closes[:-1]
        lookback_count = min(self.lookback - 1, len(prev_closes))
        if lookback_count < 1:
            lookback_count = 1
        lookback_closes = prev_closes[-lookback_count:]
        lookback_avg = sum(lookback_closes) / len(lookback_closes)

        # 計算變動百分比
        if lookback_avg > 0:
            momentum_pct = ((current_price - lookback_avg) / lookback_avg) * 100
        else:
            momentum_pct = 0.0

        # 判斷方向
        abs_momentum = abs(momentum_pct)

        if abs_momentum < self.threshold_pct:
            return SignalResult(
                signal=Signal.NONE,
                confidence=0.0,
                momentum_pct=momentum_pct,
                current_price=current_price,
                lookback_avg=lookback_avg,
                reason=f"橫盤（變動 {momentum_pct:.3f}% < 閾值 {self.threshold_pct}%）",
            )

        if momentum_pct > 0:
            # 上漲趨勢
            confidence = min(abs_momentum / (self.threshold_pct * 5), 1.0)
            return SignalResult(
                signal=Signal.UP,
                confidence=confidence,
                momentum_pct=momentum_pct,
                current_price=current_price,
                lookback_avg=lookback_avg,
                reason=f"上漲動量 {momentum_pct:.3f}%（信心 {confidence:.2f}）",
            )
        else:
            # 下跌趨勢
            confidence = min(abs_momentum / (self.threshold_pct * 5), 1.0)
            return SignalResult(
                signal=Signal.DOWN,
                confidence=confidence,
                momentum_pct=momentum_pct,
                current_price=current_price,
                lookback_avg=lookback_avg,
                reason=f"下跌動量 {momentum_pct:.3f}%（信心 {confidence:.2f}）",
            )

    def should_trade(self, result: SignalResult, ask_price: float, max_buy_price: float) -> bool:
        """
        判斷是否應該交易

        :param result: 策略分析結果
        :param ask_price: 事件合約的賣一價（買入 UP 的價格）
        :param max_buy_price: 最大願意支付的價格
        :returns: 是否交易
        """
        if result.signal == Signal.NONE:
            return False

        if result.confidence < 0.1:
            return False

        # 價格檢查：買入價格不能過高
        if ask_price > max_buy_price:
            logger.info(
                f"跳過交易：賣價 {ask_price:.3f} > 最大買入價 {max_buy_price:.3f}"
            )
            return False

        # 如果價格接近 0.5 表示市場不確定性高，可以交易（賠率合理）
        # 如果價格接近 0.9+，潛在收益很低
        # 如果價格接近 0.1-，雖然收益高但勝率可能很低
        if ask_price > 0.85:
            logger.info(f"跳過交易：賣價 {ask_price:.3f} 過高，潛在收益不足")
            return False

        return True
