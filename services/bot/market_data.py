"""
OKX WebSocket 行情数据订阅器

通过 OKX V5 公共 WebSocket API 订阅实时 K 线数据。
支持多时间框架（基础周期 + 高周期聚合）。

OKX WebSocket 文档:
  https://www.okx.com/docs-v5/zh/#websocket-api-public-channel-candlesticks
"""
import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("bot.market_data")


class Candle:
    """统一 K 线数据结构"""
    __slots__ = ("timestamp", "open", "high", "low", "close", "volume", "confirm")

    def __init__(self, timestamp: int, open_p: float, high: float,
                 low: float, close: float, volume: float, confirm: bool = False):
        self.timestamp = timestamp  # ms
        self.open = open_p
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.confirm = confirm  # True 表示该 K 线已确认（收盘）

    def __repr__(self) -> str:
        return (f"Candle(ts={self.timestamp}, O={self.open:.2f}, "
                f"H={self.high:.2f}, L={self.low:.2f}, C={self.close:.2f}, "
                f"V={self.volume:.4f}, confirm={self.confirm})")

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "confirm": self.confirm,
        }


class MarketDataSubscriber:
    """
    OKX WebSocket 行情订阅器

    支持订阅一个或多个交易对的 K 线数据。
    自动管理 WebSocket 连接、心跳、重连。
    """

    # OKX 频道名称映射：基础周期 -> OKX 频道
    CHANNEL_MAP = {
        60: "candle1m",       # 1 分钟
        180: "candle3m",      # 3 分钟
        300: "candle5m",      # 5 分钟
        900: "candle15m",     # 15 分钟
        1800: "candle30m",    # 30 分钟
        3600: "candle1H",     # 1 小时
        7200: "candle2H",     # 2 小时
        14400: "candle4H",    # 4 小时
        21600: "candle6H",    # 6 小时
        28800: "candle8H",    # 8 小时
        43200: "candle12H",   # 12 小时
        86400: "candle1D",    # 1 天
        604800: "candle1W",   # 1 周
        2592000: "candle1M",  # 1 月
    }

    def __init__(
        self,
        ws_url: str,
        symbols: List[str],
        base_timeframe_sec: int = 60,  # 基础周期（秒），默认 1m
        on_candle: Optional[Callable[[str, Candle], None]] = None,
        on_candle_closed: Optional[Callable[[str, Candle], None]] = None,
        reconnect_delay: float = 5.0,
        max_reconnect_attempts: int = 0,  # 0 = 无限重连
    ):
        """
        :param ws_url: WebSocket URL (wss://...)
        :param symbols: 交易对列表，如 ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        :param base_timeframe_sec: 基础 K 线周期（秒）
        :param on_candle: 每根 K 线更新时的回调 (symbol, candle)
        :param on_candle_closed: K 线收盘确认时的回调 (symbol, candle)
        :param reconnect_delay: 重连延迟（秒）
        :param max_reconnect_attempts: 最大重连次数，0=无限
        """
        self.ws_url = ws_url
        self.symbols = symbols
        self.base_timeframe_sec = base_timeframe_sec
        self.on_candle = on_candle
        self.on_candle_closed = on_candle_closed
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts

        # 运行时状态
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        self._reconnect_count = 0
        self._last_pong = time.time()
        self._heartbeat_task: Optional[asyncio.Task] = None

        # 最新 K 线缓存: {symbol: Candle}
        self.latest_candles: Dict[str, Candle] = {}

    def _get_channel(self) -> str:
        """获取对应周期的 OKX 频道名"""
        # 找到最接近的频道
        available = sorted(self.CHANNEL_MAP.keys())
        best = available[0]
        for sec in available:
            if sec <= self.base_timeframe_sec:
                best = sec
            else:
                break
        return self.CHANNEL_MAP[best]

    def _build_subscribe_args(self) -> List[dict]:
        """构建订阅参数列表"""
        channel = self._get_channel()
        return [
            {"channel": channel, "instId": symbol}
            for symbol in self.symbols
        ]

    async def connect(self):
        """建立 WebSocket 连接并订阅"""
        if self._session is None:
            self._session = aiohttp.ClientSession()

        try:
            logger.info(f"正在连接 OKX WebSocket: {self.ws_url}")
            self._ws = await self._session.ws_connect(
                self.ws_url,
                heartbeat=20.0,  # 20 秒心跳
                receive_timeout=30.0,
            )
            logger.info("WebSocket 连接成功")

            # 订阅行情
            subscribe_msg = {
                "op": "subscribe",
                "args": self._build_subscribe_args(),
            }
            await self._ws.send_json(subscribe_msg)
            logger.info(f"已发送订阅请求: {json.dumps(subscribe_msg)}")

            # 启动心跳监控
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            return True
        except Exception as e:
            logger.error(f"WebSocket 连接失败: {e}")
            return False

    async def _heartbeat_loop(self):
        """心跳监控：定期发送 Ping"""
        while self._running and self._ws and not self._ws.closed:
            try:
                await self._ws.send_str("ping")
                await asyncio.sleep(15)
            except Exception as e:
                logger.warning(f"心跳发送失败: {e}")
                break

    async def _handle_message(self, message: dict):
        """处理收到的 WebSocket 消息"""
        try:
            # OKX 事件类型
            event = message.get("event", "")

            if event == "subscribe":
                logger.info(f"订阅成功: {message.get('arg', {})}")
                return

            if event == "error":
                logger.error(f"订阅错误: {message.get('msg', '')}")
                return

            # K 线数据
            if "arg" in message and "data" in message:
                arg = message["arg"]
                channel = arg.get("channel", "")
                inst_id = arg.get("instId", "")

                if channel.startswith("candle") and inst_id:
                    for raw in message["data"]:
                        candle = self._parse_candle(raw)
                        if candle:
                            self.latest_candles[inst_id] = candle

                            # 回调：实时更新
                            if self.on_candle:
                                self.on_candle(inst_id, candle)

                            # 回调：K 线收盘确认
                            if candle.confirm and self.on_candle_closed:
                                self.on_candle_closed(inst_id, candle)

        except Exception as e:
            logger.error(f"消息处理异常: {e}", exc_info=True)

    def _parse_candle(self, raw: list) -> Optional[Candle]:
        """
        解析 OKX K 线数据

        OKX 返回格式:
        ["ts", "o", "h", "l", "c", "vol", "volCcy", "volCcyQuote", "confirm"]
        """
        try:
            if isinstance(raw, list) and len(raw) >= 9:
                return Candle(
                    timestamp=int(raw[0]),
                    open_p=float(raw[1]),
                    high=float(raw[2]),
                    low=float(raw[3]),
                    close=float(raw[4]),
                    volume=float(raw[5]),
                    confirm=raw[8] == "1",
                )
            return None
        except (ValueError, IndexError) as e:
            logger.warning(f"K 线解析失败: {raw} - {e}")
            return None

    async def _message_loop(self):
        """消息接收循环"""
        async for msg in self._ws:
            if not self._running:
                break

            if msg.type == aiohttp.WSMsgType.TEXT:
                # 心跳响应
                if msg.data == "pong":
                    self._last_pong = time.time()
                    continue

                try:
                    data = json.loads(msg.data)
                    await self._handle_message(data)
                except json.JSONDecodeError as e:
                    logger.warning(f"JSON 解析失败: {e}")

            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"WebSocket 错误: {self._ws.exception()}")
                break

    async def run(self):
        """主运行循环（带自动重连）"""
        self._running = True

        while self._running:
            connected = await self.connect()
            if connected:
                self._reconnect_count = 0
                try:
                    await self._message_loop()
                except Exception as e:
                    logger.error(f"消息循环异常: {e}")

            if not self._running:
                break

            # 重连逻辑
            self._reconnect_count += 1
            if (self.max_reconnect_attempts > 0 and
                    self._reconnect_count > self.max_reconnect_attempts):
                logger.error("超过最大重连次数，停止")
                break

            delay = min(self.reconnect_delay * (1.5 ** (self._reconnect_count - 1)), 60.0)
            logger.info(f"将在 {delay:.1f} 秒后重连 (第 {self._reconnect_count} 次)...")
            await asyncio.sleep(delay)

    async def stop(self):
        """停止订阅器"""
        self._running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self._ws and not self._ws.closed:
            try:
                # 发送取消订阅
                unsubscribe_msg = {
                    "op": "unsubscribe",
                    "args": self._build_subscribe_args(),
                }
                await self._ws.send_json(unsubscribe_msg)
                await self._ws.close()
            except Exception:
                pass

        if self._session:
            await self._session.close()
            self._session = None

        logger.info("市场数据订阅器已停止")


class CandleAggregator:
    """
    K 线聚合器

    将基础周期的 K 线（如 1m）聚合成更高周期的 K 线（如 18m）。
    用于实现多时间框架策略（tfmult=18）。
    """

    def __init__(self, base_seconds: int = 60, target_seconds: int = 1080):
        """
        :param base_seconds: 基础周期秒数
        :param target_seconds: 目标周期秒数
        """
        self.base_seconds = base_seconds
        self.target_seconds = target_seconds
        self.ratio = target_seconds // base_seconds if base_seconds > 0 else 1

        # 当前聚合中的 K 线
        self._candles: List[Candle] = []

        # 已完成的聚合 K 线
        self.completed: List[Candle] = []

    def add_candle(self, candle: Candle) -> Optional[Candle]:
        """
        添加一根基础 K 线。如果聚合完成，返回聚合后的 K 线。

        聚合规则：当基础 K 线数量达到 ratio 或被确认收盘时完成聚合。
        """
        self._candles.append(candle)

        # 检查是否完成聚合
        completed = None

        if len(self._candles) >= self.ratio:
            completed = self._aggregate()
            self.completed.append(completed)
            self._candles = []

        return completed

    def _aggregate(self) -> Candle:
        """聚合当前缓存中的 K 线"""
        if not self._candles:
            raise ValueError("没有 K 线可聚合")

        first = self._candles[0]
        last = self._candles[-1]

        return Candle(
            timestamp=first.timestamp,
            open_p=first.open,
            high=max(c.high for c in self._candles),
            low=min(c.low for c in self._candles),
            close=last.close,
            volume=sum(c.volume for c in self._candles),
            confirm=True,  # 聚合完成后视为已确认
        )

    @property
    def current(self) -> Optional[Candle]:
        """当前正在聚合的 K 线（未完成）"""
        if self._candles:
            return self._candles[-1]
        return None

    def reset(self):
        """重置聚合器"""
        self._candles = []
        self.completed = []
