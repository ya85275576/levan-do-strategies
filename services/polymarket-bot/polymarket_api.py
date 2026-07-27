"""
Polymarket CLOB API 客户端

封装 Polymarket 的 Central Limit Order Book API：
  - GET /markets           — 列出所有活跃市场
  - GET /book?token_id=X   — 查询某个代币的订单簿

API 文档: https://docs.polymarket.com/api/rest
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("polymarket.api")


# ================================================================
# 数据结构
# ================================================================

@dataclass
class TokenInfo:
    """Polymarket 代币信息"""
    token_id: str          # Polygon 上的 ERC1155 token ID
    outcome: str           # "Yes" 或 "No"
    price: str             # 当前最佳买入/卖出价格（字符串，保留精度）


@dataclass
class MarketInfo:
    """Polymarket 二元预测市场信息"""
    condition_id: str      # 条件 ID（链上标识）
    question: str          # 市场问题，例如 "Will BTC exceed $100k by Dec 31 2025?"
    description: str       # 详细描述
    slug: str              # URL 友好的标识
    closed: bool           # 是否已关闭
    archived: bool         # 是否已归档
    accepting_orders: bool # 是否接受订单
    rewards: dict          # 流动性激励信息

    # YES 和 NO 代币信息
    yes_token_id: str      # YES 代币 ID
    no_token_id: str       # NO 代币 ID

    # 缓存的价格数据
    yes_best_ask: float = 0.0    # YES 最优卖价
    yes_best_bid: float = 0.0    # YES 最优买价
    no_best_ask: float = 0.0     # NO 最优卖价
    no_best_bid: float = 0.0     # NO 最优买价
    yes_ask_depth: float = 0.0   # YES 卖一深度（USDC）
    no_ask_depth: float = 0.0    # NO 卖一深度（USDC）
    spread_yes: float = 0.0      # YES 买卖价差
    spread_no: float = 0.0       # NO 买卖价差

    last_updated: float = 0.0    # 最后更新时间戳


@dataclass
class ArbitrageOpportunity:
    """套利机会"""
    market: MarketInfo
    yes_ask: float           # 买入 YES 的价格
    no_ask: float            # 买入 NO 的价格
    cost: float              # 总成本 = yes_ask + no_ask
    profit_per_share: float  # 每股利润 = 1 - cost
    profit_pct: float        # 利润率 = profit_per_share / cost * 100
    yes_depth: float         # YES 卖一深度
    no_depth: float          # NO 卖一深度
    max_trade_size: float    # 最大可交易规模（受深度限制）
    timestamp: float         # 发现时间


# ================================================================
# API 客户端
# ================================================================

class PolymarketClient:
    """
    Polymarket CLOB API 客户端

    提供市场查询和订单簿查询方法。
    所有方法都是只读的，无需 API 密钥。
    """

    def __init__(self, clob_api_url: str = "https://clob.polymarket.com"):
        """
        :param clob_api_url: CLOB API 基础 URL
        """
        self._base_url = clob_api_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_count: int = 0

    async def _ensure_session(self):
        """确保 HTTP 会话已创建"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={
                    "Accept": "application/json",
                    "User-Agent": "LE-VAN-DO-Polymarket-Bot/1.0",
                },
            )

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """
        执行 GET 请求

        Args:
            path: API 路径，例如 "/markets"
            params: 查询参数

        Returns:
            dict: 解析后的 JSON 响应

        Raises:
            ConnectionError: 网络或 HTTP 错误
            ValueError: 响应格式错误
        """
        await self._ensure_session()
        self._request_count += 1

        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 429:
                    text = await resp.text()
                    logger.warning(f"[API] 限流(429): {url}")
                    raise ConnectionError(f"API rate limited: {text[:200]}")

                if resp.status == 404:
                    logger.warning(f"[API] 404: {url}")
                    return {}

                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"[API] HTTP {resp.status}: {url} -> {text[:200]}")
                    raise ConnectionError(f"HTTP {resp.status}: {text[:200]}")

                # Polymarket API 返回 JSON 或纯文本
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return await resp.json()
                else:
                    # 某些端返回纯文本 JSON
                    text = await resp.text()
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        logger.error(f"[API] 非 JSON 响应: {text[:200]}")
                        return {}

        except asyncio.TimeoutError:
            logger.warning(f"[API] 请求超时: {url}")
            raise ConnectionError(f"Timeout: {url}")
        except aiohttp.ClientError as e:
            logger.warning(f"[API] 请求异常: {url} -> {e}")
            raise ConnectionError(f"Request failed: {e}")

    async def fetch_markets(
        self,
        closed: bool = False,
        limit: int = 100,
        max_pages: int = 5,
    ) -> List[MarketInfo]:
        """
        获取活跃的二元预测市场列表。

        Args:
            closed: 是否包含已关闭的市场
            limit: 每页数量（最大 100）
            max_pages: 最多获取的页数

        Returns:
            List[MarketInfo]: 市场信息列表（仅二元预测市场）
        """
        all_markets: List[MarketInfo] = []
        next_cursor: Optional[str] = None

        for page in range(max_pages):
            params: Dict[str, object] = {
                "closed": str(closed).lower(),
                "limit": str(limit),
            }
            if next_cursor:
                params["next_cursor"] = next_cursor

            try:
                data = await self._get("/markets", params)
            except ConnectionError:
                logger.error(f"[API] 获取第 {page+1} 页市场失败，停止翻页")
                break

            # 解析响应
            # Polymarket API 可能返回列表或 {"data": [...], "next_cursor": "..."}
            if isinstance(data, list):
                markets_data = data
                next_cursor = None
            elif isinstance(data, dict):
                markets_data = data.get("data") or data.get("markets", [])
                next_cursor = data.get("next_cursor")
                if not isinstance(markets_data, list):
                    markets_data = []
            else:
                markets_data = []

            for raw in markets_data:
                market = self._parse_market(raw)
                if market:
                    all_markets.append(market)

            logger.debug(
                f"[API] 第 {page+1} 页: 解析到 {len(markets_data)} 个市场, "
                f"其中 {len([m for m in all_markets])} 个二元市场"
            )

            # 没有下一页了
            if not next_cursor:
                break

            # 翻页间隔，避免限流
            await asyncio.sleep(0.2)

        # 最终过滤：只保留二元市场
        return all_markets

    def _parse_market(self, raw: dict) -> Optional[MarketInfo]:
        """
        解析单个市场原始数据为 MarketInfo。

        Polymarket API 返回格式示例：
        {
            "condition_id": "0x...",
            "question": "Will BTC exceed $100k...",
            "description": "...",
            "slug": "btc-100k-2025",
            "closed": false,
            "archived": false,
            "accepting_orders": true,
            "rewards": {...},
            "tokens": [
                {"token_id": "123", "outcome": "Yes", "price": "0.45"},
                {"token_id": "456", "outcome": "No", "price": "0.50"},
            ],
            "outcomes": ["Yes", "No"]
        }

        Returns:
            Optional[MarketInfo]: 如果是二元市场则返回，否则返回 None
        """
        try:
            # 检查是否是二元市场
            outcomes = raw.get("outcomes") or []
            if len(outcomes) != 2:
                return None

            # 必须同时包含 "Yes" 和 "No"
            outcome_set = {o.lower() for o in outcomes}
            if "yes" not in outcome_set or "no" not in outcome_set:
                return None

            # 解析代币
            tokens_raw = raw.get("tokens") or []
            yes_token_id = ""
            no_token_id = ""

            for token in tokens_raw:
                outcome = (token.get("outcome") or "").lower()
                token_id = token.get("token_id", "")
                if outcome == "yes":
                    yes_token_id = token_id
                elif outcome == "no":
                    no_token_id = token_id

            if not yes_token_id or not no_token_id:
                return None

            return MarketInfo(
                condition_id=raw.get("condition_id", ""),
                question=raw.get("question", ""),
                description=raw.get("description", ""),
                slug=raw.get("slug", ""),
                closed=raw.get("closed", False),
                archived=raw.get("archived", False),
                accepting_orders=raw.get("accepting_orders", True),
                rewards=raw.get("rewards", {}),
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
            )

        except Exception as e:
            logger.warning(f"[API] 市场解析失败: {e}")
            return None

    async def fetch_order_book(
        self, token_id: str, side: str = "SELL"
    ) -> List[dict]:
        """
        获取某个代币的订单簿。

        Args:
            token_id: 代币 ID
            side: "SELL"（卖出盘=买入代币的卖价）或 "BUY"（买入盘）

        Returns:
            List[dict]: 订单列表，每笔订单包含 price, size 等字段
        """
        params = {"token_id": token_id, "side": side.upper()}
        try:
            data = await self._get("/book", params)

            # 响应可能是 {"asks": [...], "bids": [...]} 或直接是数组
            if isinstance(data, dict):
                orders = data.get("asks" if side.upper() == "SELL" else "bids", [])
                if isinstance(orders, list):
                    return orders
            elif isinstance(data, list):
                return data

            return []

        except ConnectionError as e:
            logger.warning(f"[API] 获取订单簿失败: token={token_id[:16]}... side={side}: {e}")
            return []

    async def fetch_market_prices(self, market: MarketInfo) -> MarketInfo:
        """
        获取某个市场的 YES 和 NO 最新报价。

        从订单簿中提取最优买卖价格和深度。

        Args:
            market: 市场信息（会原地更新价格字段）

        Returns:
            MarketInfo: 更新了价格信息的市场
        """
        # 获取 YES 卖出盘（我们要买入 YES 的价格）
        yes_asks = await self.fetch_order_book(market.yes_token_id, "SELL")
        # 获取 YES 买入盘
        yes_bids = await self.fetch_order_book(market.yes_token_id, "BUY")

        # 获取 NO 卖出盘
        no_asks = await self.fetch_order_book(market.no_token_id, "SELL")
        # 获取 NO 买入盘
        no_bids = await self.fetch_order_book(market.no_token_id, "BUY")

        # ---- 解析 YES 价格 ----
        if yes_asks:
            best = yes_asks[0]
            market.yes_best_ask = float(best.get("price", 0))
            market.yes_ask_depth = float(best.get("size", 0))

        if yes_bids:
            best = yes_bids[0]
            market.yes_best_bid = float(best.get("price", 0))

        market.spread_yes = market.yes_best_ask - market.yes_best_bid

        # ---- 解析 NO 价格 ----
        if no_asks:
            best = no_asks[0]
            market.no_best_ask = float(best.get("price", 0))
            market.no_ask_depth = float(best.get("size", 0))

        if no_bids:
            best = no_bids[0]
            market.no_best_bid = float(best.get("price", 0))

        market.spread_no = market.no_best_ask - market.no_best_bid

        market.last_updated = asyncio.get_event_loop().time()
        return market

    async def scan_arbitrage_opportunities(
        self,
        threshold: float = 0.98,
        min_liquidity: float = 100.0,
        max_pages: int = 5,
        price_filters: Optional[dict] = None,
    ) -> List[ArbitrageOpportunity]:
        """
        扫描全市场，寻找 YES+NO < threshold 的套利机会。

        Args:
            threshold: 触发阈值，默认 0.98（2% 折价）
            min_liquidity: 最低盘口深度（USDC）
            max_pages: 最多扫描页数
            price_filters: 价格过滤字典，可选键：
                - min_yes_price, max_yes_price
                - min_no_price, max_no_price

        Returns:
            List[ArbitrageOpportunity]: 套利机会列表，按利润率降序排列
        """
        price_filters = price_filters or {}
        min_yes = price_filters.get("min_yes_price", 0.02)
        max_yes = price_filters.get("max_yes_price", 0.98)
        min_no = price_filters.get("min_no_price", 0.02)
        max_no = price_filters.get("max_no_price", 0.98)

        # 1. 获取所有活跃市场
        logger.info(f"[扫描] 获取活跃市场 (最多 {max_pages} 页)...")
        markets = await self.fetch_markets(
            closed=False, limit=100, max_pages=max_pages
        )
        logger.info(f"[扫描] 共获取 {len(markets)} 个二元活跃市场")

        # 2. 逐个获取报价
        opportunities: List[ArbitrageOpportunity] = []
        scanned = 0
        skipped_no_liquidity = 0
        skipped_not_accepting = 0

        for market in markets:
            scanned += 1

            # 跳过不接收订单的市场
            if not market.accepting_orders:
                skipped_not_accepting += 1
                continue

            # 获取价格
            market = await self.fetch_market_prices(market)

            # 跳过没有报价的市场
            if market.yes_best_ask <= 0 or market.no_best_ask <= 0:
                skipped_no_liquidity += 1
                continue

            # 应用价格过滤器
            if not (min_yes <= market.yes_best_ask <= max_yes):
                continue
            if not (min_no <= market.no_best_ask <= max_no):
                continue

            # 跳过流动性不足的
            if market.yes_ask_depth < min_liquidity:
                continue
            if market.no_ask_depth < min_liquidity:
                continue

            # 计算套利条件
            yes_price = market.yes_best_ask
            no_price = market.no_best_ask
            total_cost = yes_price + no_price

            if total_cost < threshold:
                profit_per_share = 1.0 - total_cost
                profit_pct = (profit_per_share / total_cost) * 100
                max_trade = min(market.yes_ask_depth, market.no_ask_depth)

                opp = ArbitrageOpportunity(
                    market=market,
                    yes_ask=yes_price,
                    no_ask=no_price,
                    cost=total_cost,
                    profit_per_share=profit_per_share,
                    profit_pct=profit_pct,
                    yes_depth=market.yes_ask_depth,
                    no_depth=market.no_ask_depth,
                    max_trade_size=max_trade,
                    timestamp=asyncio.get_event_loop().time(),
                )
                opportunities.append(opp)

            if scanned % 20 == 0:
                logger.info(f"[扫描] 进度: {scanned}/{len(markets)}, "
                            f"已发现 {len(opportunities)} 个机会")

        # 3. 按利润率降序排列
        opportunities.sort(key=lambda o: o.profit_pct, reverse=True)

        logger.info(
            f"[扫描] 完成: 扫描 {scanned} 个市场, "
            f"跳过未激活={skipped_not_accepting}, "
            f"跳过无流动性={skipped_no_liquidity}, "
            f"发现 {len(opportunities)} 个套利机会"
        )

        return opportunities

    async def close(self):
        """关闭 HTTP 会话"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @property
    def request_count(self) -> int:
        return self._request_count
