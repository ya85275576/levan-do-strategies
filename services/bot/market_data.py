"""
OKX 行情数据模块

通过 OKX V5 公共 WebSocket API 订阅实时 K 线数据，并支持 REST API 回退。
管理多交易对的 K 线聚合（基础周期 → 高周期）。

OKX WebSocket 文档:
  https://www.okx.com/docs-v5/zh/#websocket-api-public-channel-candlesticks
"""
import asyncio
import json
import logging
import math
import random
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


class CandleAggregator:
    """
    K 线聚合器（单交易对）

    将基础周期的 K 线（如 15m）聚合成更高周期的 K 线（如 270m = 4.5h）。
    用于实现多时间框架策略（tfmult=18）。
    """

    def __init__(self, symbol: str, base_seconds: int = 900, target_seconds: int = 16200):
        """
        :param symbol: 交易对标识
        :param base_seconds: 基础周期秒数
        :param target_seconds: 目标周期秒数
        """
        self.symbol = symbol
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
        """
        self._candles.append(candle)
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
            confirm=True,
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

    @property
    def pending_count(self) -> int:
        """当前缓存中的基础 K 线数量"""
        return len(self._candles)

    @property
    def progress_pct(self) -> float:
        """聚合进度百分比"""
        return (len(self._candles) / self.ratio) * 100 if self.ratio > 0 else 0


class SimulatedDataFeed:
    """
    模拟数据源（DRY_RUN 模式）

    当 WebSocket 不可用时，生成合成 K 线数据用于测试策略逻辑。
    使用随机游走模拟价格变动，产生接近真实市场的技术指标信号。
    """

    def __init__(self, symbols: List[str], base_timeframe_sec: int = 900):
        """
        :param symbols: 交易对列表
        :param base_timeframe_sec: 基础周期秒数
        """
        self.symbols = symbols
        self.base_timeframe_sec = base_timeframe_sec

        # 每个交易对的价格状态: {symbol: {"price": float, "open": float, "high": float, "low": float}}
        self._prices: Dict[str, dict] = {}

        # 每个交易对的基础价格
        self._base_prices = {
            "BTC-USDT": 64000.0,
            "ETH-USDT": 3400.0,
            "SOL-USDT": 145.0,
            "XRP-USDT": 0.52,
            "DOGE-USDT": 0.125,
            "ADA-USDT": 0.45,
            "AVAX-USDT": 28.0,
            "DOT-USDT": 6.5,
            "LINK-USDT": 14.0,
            "MATIC-USDT": 0.55,
            "UNI-USDT": 8.0,
            "SHIB-USDT": 0.000018,
            "LTC-USDT": 72.0,
            "BCH-USDT": 380.0,
            "ATOM-USDT": 7.0,
            "ETC-USDT": 22.0,
            "XLM-USDT": 0.10,
            "TRX-USDT": 0.12,
            "FIL-USDT": 4.5,
            "APT-USDT": 7.5,
            "ARB-USDT": 0.85,
            "OP-USDT": 2.1,
            "SUI-USDT": 1.2,
            "PEPE-USDT": 0.000012,
            "INJ-USDT": 25.0,
            "TIA-USDT": 8.0,
            "SEI-USDT": 0.45,
            "RUNE-USDT": 5.5,
            "FET-USDT": 1.8,
            "GRT-USDT": 0.25,
            "NEAR-USDT": 5.0,
            "ICP-USDT": 9.0,
            "RENDER-USDT": 7.0,
            "IMX-USDT": 1.6,
            "MKR-USDT": 1800.0,
            "AAVE-USDT": 110.0,
            "CRV-USDT": 0.40,
            "SNX-USDT": 2.5,
            "COMP-USDT": 55.0,
            "EOS-USDT": 0.65,
            "ALGO-USDT": 0.15,
            "FLOW-USDT": 0.80,
            "SAND-USDT": 0.35,
            "MANA-USDT": 0.40,
            "AXS-USDT": 6.0,
            "THETA-USDT": 1.8,
            "FTM-USDT": 0.70,
            "CVX-USDT": 3.5,
            "1INCH-USDT": 0.40,
            "STX-USDT": 2.2,
        }

        self._running = False

    def _get_base_price(self, symbol: str) -> float:
        """获取交易对的基础价格"""
        for key, price in self._base_prices.items():
            if symbol.startswith(key.split("-")[0]):
                return price
        return 50.0

    def _initialize_symbol(self, symbol: str):
        """初始化交易对价格状态"""
        if symbol not in self._prices:
            base = self._get_base_price(symbol)
            self._prices[symbol] = {
                "price": base,
                "open": base,
                "high": base,
                "low": base,
            }

    def generate_candle(self, symbol: str) -> Candle:
        """
        为指定交易对生成一根模拟 K 线。
        使用随机游走 + 均值回归模拟价格行为。
        """
        self._initialize_symbol(symbol)
        state = self._prices[symbol]

        # 价格波动率（根据价格水平调整）
        vol = max(state["price"] * 0.002, 0.001)  # 0.2% 波动

        # 随机游走 + 均值回归
        drift = (self._get_base_price(symbol) - state["price"]) * 0.001
        change = random.gauss(drift, vol)

        new_close = state["price"] + change
        new_close = max(new_close, new_close * 0.001)  # 防止负价格

        # 生成 OHLC
        intra_vol = abs(change) * random.uniform(0.5, 2.0)
        new_high = max(state["open"], new_close) + intra_vol * random.random()
        new_low = min(state["open"], new_close) - intra_vol * random.random()

        candle = Candle(
            timestamp=int(time.time() * 1000),
            open_p=state["open"],
            high=new_high,
            low=new_low,
            close=new_close,
            volume=random.uniform(100, 10000),
            confirm=True,  # 模拟数据始终视为已确认
        )

        # 更新状态
        state["price"] = new_close
        state["open"] = new_close
        state["high"] = new_close
        state["low"] = new_close

        return candle

    async def run(self, on_candle: Callable[[str, Candle], None]):
        """
        运行模拟数据生成循环。

        生成 K 线的速度 = 每 (symbols * 0.05s) 完成一轮所有交易对，
        即每秒约生成 20 根 K 线。
        大幅加快模拟速度以快速触发策略信号。
        """
        self._running = True
        logger.info(f"🧪 模拟数据源已启动 ({len(self.symbols)} 个交易对, "
                    f"高速模式 ~{len(self.symbols) * 0.05:.0f}s/轮)")

        round_count = 0
        while self._running:
            round_count += 1
            tick_start = time.time()

            for symbol in self.symbols:
                if not self._running:
                    break
                candle = self.generate_candle(symbol)
                on_candle(symbol, candle)
                # 短暂间隔，避免 CPU 100%
                await asyncio.sleep(0.02)

            elapsed = time.time() - tick_start

            # 每 10 轮打印一次运行状态
            if round_count % 10 == 0:
                price_sample = self._prices.get(self.symbols[0], {}).get('price', 0)
                logger.info(f"🧪 模拟运行中: 第{round_count}轮, "
                            f"50个交易对已生成, "
                            f"{self.symbols[0]}价格={price_sample:.2f}")

            # 每轮间隔 0.3 秒
            sleep_time = max(0.05, 0.3 - elapsed)
            await asyncio.sleep(sleep_time)

    def stop(self):
        """停止模拟数据源"""
        self._running = False
        logger.info("🧪 模拟数据源已停止")


class RestApiDataFeed:
    """
    REST API 行情数据源

    通过轮询 OKX REST API 获取 K 线数据，替代 WebSocket 订阅。
    适用于 WebSocket 被 IP 封锁的场景（如东京服务器）。

    轮询策略:
      - 首次启动：获取每个交易对最近 100 根 K 线，按时间正序喂入
      - 定期轮询：每隔 base_timeframe_sec（默认 900s=15m）获取最新 K 线，自动去重
      - 并发控制：使用 asyncio.Semaphore 限制并发请求数

    用法:
        feed = RestApiDataFeed(
            symbols=["BTC-USDT", "ETH-USDT", ...],
            base_timeframe_sec=900,
            on_candle=lambda symbol, candle: print(symbol, candle),
        )
        await feed.run()
    """

    # OKX REST API 基础 URL（公开行情端点，无需认证）
    REST_URL = "https://www.okx.com"

    # OKX bar 参数映射
    BAR_MAP = {
        60: "1m",
        180: "3m",
        300: "5m",
        900: "15m",
        1800: "30m",
        3600: "1H",
        7200: "2H",
        14400: "4H",
        21600: "6H",
        28800: "8H",
        43200: "12H",
        86400: "1D",
        604800: "1W",
        2592000: "1M",
    }

    def __init__(
        self,
        symbols: List[str],
        base_timeframe_sec: int = 900,
        on_candle: Optional[Callable[[str, Candle], None]] = None,
        max_concurrent: int = 10,
        rest_url: Optional[str] = None,
    ):
        """
        :param symbols: 交易对列表
        :param base_timeframe_sec: 基础 K 线周期（秒），同时也是轮询间隔
        :param on_candle: K 线更新回调 (symbol, candle)
        :param max_concurrent: 最大并发请求数
        :param rest_url: REST API 基础 URL（默认 https://www.okx.com）
        """
        self.symbols = symbols
        self.base_timeframe_sec = base_timeframe_sec
        self.on_candle = on_candle
        self.rest_url = (rest_url or self.REST_URL).rstrip("/")

        # 并发控制
        self._semaphore = asyncio.Semaphore(max_concurrent)

        # HTTP 会话
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False

        # 去重跟踪：记录每个交易对已处理的最新时间戳
        self._last_ts: Dict[str, int] = {}

        # 初始加载标记
        self._initial_load_done = False

        # 最新 K 线缓存: {symbol: Candle}
        self.latest_candles: Dict[str, Candle] = {}

        logger.info(
            f"📡 [REST] 数据源初始化: {len(symbols)} 个交易对, "
            f"轮询间隔={base_timeframe_sec}s, bar={self._get_bar()}"
        )

    def _get_bar(self) -> str:
        """获取对应周期的 OKX bar 参数"""
        available = sorted(self.BAR_MAP.keys())
        best = available[0]
        for sec in available:
            if sec <= self.base_timeframe_sec:
                best = sec
            else:
                break
        return self.BAR_MAP[best]

    async def _ensure_session(self):
        """确保 HTTP 会话已创建"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Accept": "application/json"},
            )

    async def _fetch_candles(self, symbol: str, limit: int = 100) -> List[Candle]:
        """
        获取单个交易对的 K 线数据

        API: GET https://www.okx.com/api/v5/market/candles
        文档: https://www.okx.com/docs-v5/zh/#rest-api-market-data-get-candlesticks
        """
        await self._ensure_session()

        bar = self._get_bar()
        params = {"instId": symbol, "bar": bar, "limit": str(limit)}

        async with self._semaphore:
            # 速率限制：每个请求间延迟 100ms，避免 OKX 429 限流
            # OKX 公开 API 限制：20 次请求 / 2 秒（10 req/s）
            # 50 交易对 * 100ms = 5s 总耗时 ≈ 10 req/s，符合限制
            await asyncio.sleep(0.10)

            try:
                async with self._session.get(
                    f"{self.rest_url}/api/v5/market/candles",
                    params=params,
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"[REST] {symbol} HTTP {resp.status}: {text[:200]}")
                        return []

                    data = await resp.json()
                    if data.get("code") != "0":
                        logger.error(
                            f"[REST] {symbol} API 错误: "
                            f"code={data.get('code')}, msg={data.get('msg')}"
                        )
                        return []

                    candles = []
                    for raw in data.get("data", []):
                        candle = self._parse_candle(raw)
                        if candle:
                            candles.append(candle)

                    return candles

            except asyncio.TimeoutError:
                logger.warning(f"[REST] {symbol} 请求超时")
                return []
            except aiohttp.ClientError as e:
                logger.warning(f"[REST] {symbol} 请求异常: {e}")
                return []
            except Exception as e:
                logger.error(f"[REST] {symbol} 未知错误: {e}", exc_info=True)
                return []

    def _parse_candle(self, raw: list) -> Optional[Candle]:
        """解析 OKX K 线数据（与 WebSocket 相同的格式）"""
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
            logger.warning(f"[REST] K 线解析失败: {raw} - {e}")
            return None

    async def _poll_all(self) -> bool:
        """
        轮询所有交易对的最新 K 线

        Returns:
            bool: 是否至少有一个交易对获取到数据
        """
        limit = 100  # 每次获取 100 根，OKX 限制最大 100

        # 并发发起所有请求
        tasks = {symbol: self._fetch_candles(symbol, limit) for symbol in self.symbols}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        total_new = 0
        symbols_with_data = 0

        for symbol, result in zip(self.symbols, results):
            if isinstance(result, Exception):
                logger.error(f"[REST] {symbol} 轮询异常: {result}")
                continue

            candles = result  # OKX 返回倒序（最新在前）
            if not candles:
                continue

            symbols_with_data += 1

            # 反转成正序（最旧在前），以便聚合器正确累加
            candles.reverse()

            # 过滤已处理过的 K 线（按时间戳去重）
            last_ts = self._last_ts.get(symbol, 0)
            new_candles = [c for c in candles if c.timestamp > last_ts]

            if new_candles:
                for candle in new_candles:
                    self.latest_candles[symbol] = candle
                    if self.on_candle:
                        self.on_candle(symbol, candle)
                total_new += len(new_candles)

                # 更新最新时间戳
                self._last_ts[symbol] = max(c.timestamp for c in new_candles)

        if not self._initial_load_done:
            if symbols_with_data > 0:
                logger.info(
                    f"[REST] 初始加载完成: {symbols_with_data}/{len(self.symbols)} "
                    f"个交易对有数据, 共 {total_new} 根 K 线"
                )
            else:
                logger.error("[REST] 初始加载失败: 所有交易对均未获取到数据")
            self._initial_load_done = True
        else:
            if total_new > 0:
                logger.debug(
                    f"[REST] 轮询完成: {symbols_with_data} 个交易对, "
                    f"新增 {total_new} 根 K 线"
                )

        return symbols_with_data > 0

    async def run(self):
        """运行轮询循环"""
        self._running = True

        logger.info(
            f"📡 [REST] 行情数据源已启动 ({len(self.symbols)} 个交易对, "
            f"轮询间隔={self.base_timeframe_sec}s, bar={self._get_bar()})"
        )

        # 首次加载：获取所有历史 K 线
        logger.info("[REST] 首次加载历史 K 线 (limit=100)...")
        has_data = await self._poll_all()

        if not has_data:
            logger.warning("[REST] 首次加载未获取到数据，将在下一轮重试")

        # 定期轮询
        while self._running:
            await asyncio.sleep(self.base_timeframe_sec)
            if not self._running:
                break
            await self._poll_all()

    def stop(self):
        """停止轮询"""
        self._running = False
        logger.info("[REST] 行情数据源已停止")

    async def close(self):
        """关闭 HTTP 会话"""
        self.stop()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


class MarketDataSubscriber:
    """
    OKX WebSocket 行情订阅器

    支持订阅一个或多个交易对的 K 线数据。
    自动管理 WebSocket 连接、心跳、重连。
    如果 WebSocket 订阅失败，在 DRY_RUN 模式下自动回退到模拟数据源。
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
        base_timeframe_sec: int = 900,  # 基础周期（秒），默认 15m
        on_candle: Optional[Callable[[str, Candle], None]] = None,
        dry_run: bool = True,
    ):
        """
        :param ws_url: WebSocket URL (wss://...)
        :param symbols: 交易对列表
        :param base_timeframe_sec: 基础 K 线周期（秒）
        :param on_candle: 每根 K 线更新时的回调 (symbol, candle)
        :param dry_run: 模拟模式 — 启用时，WebSocket 失败则降级到模拟数据
        """
        self.ws_url = ws_url
        self.symbols = symbols
        self.base_timeframe_sec = base_timeframe_sec
        self.on_candle = on_candle
        self.dry_run = dry_run

        # WebSocket 运行时状态
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        self._reconnect_count = 0
        self._last_pong = time.time()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._ws_connected = False

        # 最新 K 线缓存: {symbol: Candle}
        self.latest_candles: Dict[str, Candle] = {}

        # 模拟数据源（备用）
        self._simulated_feed: Optional[SimulatedDataFeed] = None
        self._simulated_task: Optional[asyncio.Task] = None

    def _get_channel(self) -> str:
        """获取对应周期的 OKX 频道名"""
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

    async def _ws_connect_and_subscribe(self) -> bool:
        """建立 WebSocket 连接并订阅"""
        if self._session is None:
            self._session = aiohttp.ClientSession()

        try:
            logger.info(f"正在连接 OKX WebSocket: {self.ws_url}")
            self._ws = await self._session.ws_connect(
                self.ws_url,
                heartbeat=20.0,
                receive_timeout=30.0,
            )
            logger.info("WebSocket 连接成功")

            subscribe_msg = {
                "op": "subscribe",
                "args": self._build_subscribe_args(),
            }
            await self._ws.send_json(subscribe_msg)
            logger.info(f"已发送订阅请求 ({len(self.symbols)} 个交易对)")

            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._ws_connected = True
            return True
        except Exception as e:
            logger.error(f"WebSocket 连接失败: {e}")
            self._ws_connected = False
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
            event = message.get("event", "")

            if event == "subscribe":
                arg = message.get("arg", {})
                logger.info(f"订阅成功: {arg.get('channel', '')}/{arg.get('instId', '')}")
                return

            if event == "error":
                msg = message.get("msg", "")
                logger.error(f"订阅错误: {msg}")
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
                            if self.on_candle:
                                self.on_candle(inst_id, candle)

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

    async def _run_simulated_fallback(self):
        """运行模拟数据源作为备用"""
        logger.info("🔄 回退到模拟数据源...")
        self._simulated_feed = SimulatedDataFeed(
            symbols=self.symbols,
            base_timeframe_sec=self.base_timeframe_sec,
        )
        await self._simulated_feed.run(
            on_candle=lambda symbol, candle: self.on_candle(symbol, candle)
            if self.on_candle else None
        )

    async def run(self):
        """主运行循环：先尝试 WebSocket，失败则回退到模拟数据"""
        self._running = True

        # 先尝试 WebSocket 连接
        ws_success = await self._ws_connect_and_subscribe()

        if ws_success:
            # WebSocket 连接成功，进入消息循环
            self._reconnect_count = 0
            try:
                # 并行运行消息循环和模拟数据超时检测
                msg_task = asyncio.create_task(self._message_loop())
                timeout_task = asyncio.create_task(self._wait_for_data_timeout())

                done, pending = await asyncio.wait(
                    [msg_task, timeout_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # 取消尚未完成的任务
                for task in pending:
                    task.cancel()

                # 需要降级的条件：
                # 1) 超时任务先完成（没有数据到达）
                # 2) 消息循环结束了但从未收到数据（WebSocket 被服务器关闭）
                should_fallback = (
                    timeout_task in done or
                    (msg_task in done and len(self.latest_candles) == 0)
                )

                if should_fallback and self.dry_run and not self._simulated_feed:
                    logger.warning("WebSocket 未收到有效 K 线数据，降级到模拟数据源")
                    self._ws_connected = False
                    if self._ws and not self._ws.closed:
                        await self._ws.close()
                    await self._run_simulated_fallback()

            except Exception as e:
                logger.error(f"WebSocket 消息循环异常: {e}")
                if self.dry_run and not self._simulated_feed:
                    logger.info("WebSocket 中断，切换到模拟数据源")
                    await self._run_simulated_fallback()
        elif self.dry_run:
            # WebSocket 连接失败，回退到模拟数据
            await self._run_simulated_fallback()

    async def _wait_for_data_timeout(self):
        """等待数据超时检测：如果 15 秒内没有收到任何 K 线数据，则触发降级"""
        wait_interval = 1.0
        max_wait = 15.0
        waited = 0.0
        while self._running and waited < max_wait:
            if len(self.latest_candles) > 0:
                # 收到至少一根 K 线，认为 WebSocket 正常工作
                return
            await asyncio.sleep(wait_interval)
            waited += wait_interval
        # 超时，没有数据到达

    async def stop(self):
        """停止订阅器"""
        self._running = False

        # 停止模拟数据源
        if self._simulated_feed:
            self._simulated_feed.stop()
        if self._simulated_task and not self._simulated_task.done():
            self._simulated_task.cancel()

        # 停止 WebSocket
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self._ws and not self._ws.closed:
            try:
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
