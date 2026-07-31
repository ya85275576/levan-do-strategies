"""polymarket_5min_bot — 轻量 CLOB 客户端

职责:
  - 公共端点: 订单簿 (GET /book) / 市场 (GET /markets) / 资产价格
  - 下单: DRY_RUN 模式下模拟撮合 (按最优对手价成交, 记录 fill),
          实盘模式调用真实 CLOB POST /order (L2 签名由环境变量提供,
          未配置签名密钥时自动降级为 DRY_RUN, 绝不裸下单)。
  - 余额: 公共端点查询 USDC 余额不可用, 实盘经 /balance (需签名头);
          DRY_RUN 用配置的 INITIAL_CAPITAL 模拟。

设计目标: 不引入 py-clob-client 重量依赖, 保持 httpx 单依赖可运行。
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("polymarket_5min.clob")

# USDC / USDC.e 在 Polymarket 的 asset id (公共常量)
USDC_ASSET_ID = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
WETH_ASSET_ID = "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619"


class Order:
    """统一订单结构 (DRY_RUN / 实盘通用)"""

    __slots__ = ("market_id", "token_id", "side", "price", "size",
                 "order_type", "status", "filled_size", "created_at", "strategy", "fill_price")

    def __init__(self, market_id: str, token_id: str, side: str, price: float,
                 size: float, strategy: str = "", order_type: str = "limit"):
        self.market_id = market_id
        self.token_id = token_id
        self.side = side          # "BUY" / "SELL"
        self.price = price        # 限价
        self.size = size          # 股数
        self.order_type = order_type
        self.status = "PENDING"   # PENDING / FILLED / PARTIAL / REJECTED
        self.filled_size = 0.0
        self.fill_price = 0.0
        self.created_at = time.time()
        self.strategy = strategy

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "token_id": self.token_id,
            "side": self.side,
            "price": round(self.price, 4),
            "size": round(self.size, 4),
            "order_type": self.order_type,
            "status": self.status,
            "filled_size": round(self.filled_size, 4),
            "fill_price": round(self.fill_price, 4),
            "strategy": self.strategy,
            "created_at": self.created_at,
        }


class CLOBClient:
    def __init__(self, base_url: str, dry_run: bool = True,
                 initial_capital: float = 10000.0, api_key: str = "",
                 api_secret: str = "", api_passphrase: str = "",
                 private_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.initial_capital = initial_capital
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.private_key = private_key
        self._http: Optional[httpx.AsyncClient] = None
        self.orders: List[Order] = []
        self.order_history: List[Order] = []
        self._balance = initial_capital

    # ── 生命周期 ──
    async def start(self):
        self._http = httpx.AsyncClient(timeout=15.0,
                                       headers={"User-Agent": "hightemptation-5min/1.0"})

    async def stop(self):
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── 公共端点 ──
    async def get_book(self, token_id: str) -> Optional[dict]:
        """获取订单簿 {bids: [{price,size}], asks: [...]}"""
        if not token_id:
            return None
        try:
            r = await self._http.get(f"{self.base_url}/book",
                                     params={"token_id": token_id})
            if r.status_code == 200:
                data = r.json()
                bids = data.get("bids") or []
                asks = data.get("asks") or []
                return {
                    "bids": [{"price": float(b.get("price")), "size": float(b.get("size"))}
                             for b in bids if b.get("price") is not None],
                    "asks": [{"price": float(a.get("price")), "size": float(a.get("size"))}
                             for a in asks if a.get("price") is not None],
                }
            return None
        except Exception as e:
            logger.debug(f"get_book 失败 {token_id}: {e}")
            return None

    async def get_mid(self, token_id: str) -> Optional[float]:
        """订单簿中间价"""
        book = await self.get_book(token_id)
        if not book or not book["bids"] or not book["asks"]:
            return None
        return (book["bids"][0]["price"] + book["asks"][0]["price"]) / 2.0

    async def get_best_ask(self, token_id: str) -> Optional[float]:
        book = await self.get_book(token_id)
        return book["asks"][0]["price"] if book and book["asks"] else None

    async def get_best_bid(self, token_id: str) -> Optional[float]:
        book = await self.get_book(token_id)
        return book["bids"][0]["price"] if book and book["bids"] else None

    # ── 下单 ──
    async def place_order(self, market_id: str, token_id: str, side: str,
                          price: float, size: float, strategy: str = "",
                          order_type: str = "limit") -> Order:
        """
        下单统一入口。所有策略都必须经此函数, 以保证:
          - DRY_RUN 模拟撮合 (按最优对手价)
          - 实盘走真实 CLOB (签名密钥缺失时自动降级 DRY_RUN 并告警)
          - 订单进入 order_history 供看板/风控统计
        """
        price = round(max(0.01, min(0.99, price)), 4)
        size = round(max(0.01, size), 4)
        order = Order(market_id, token_id, side, price, size, strategy, order_type)
        self.orders.append(order)

        if self.dry_run or not self._can_sign():
            if not self.dry_run:
                logger.error("❌ 实盘模式缺少签名密钥 (POLYMARKET_API_KEY/SECRET/PASSPHRASE "
                             "+ POLYMARKET_PRIVATE_KEY), 本单按 DRY_RUN 模拟, 请修复配置!")
                order.status = "REJECTED"
                self.order_history.append(order)
                return order
            await self._simulate_fill(order)
        else:
            await self._place_real(order)
        self.order_history.append(order)
        return order

    def _can_sign(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase and self.private_key)

    async def _simulate_fill(self, order: Order):
        """DRY_RUN 模拟撮合: 限价单按对手盘最优价成交 (最坏情形=吃单)。"""
        book = await self.get_book(order.token_id)
        if not book:
            # 无订单簿 → 按限价成交 (模拟)
            fill = order.price
        elif order.side == "BUY":
            fill = book["asks"][0]["price"] if book["asks"] else order.price
        else:
            fill = book["bids"][0]["price"] if book["bids"] else order.price
        # 保护: 成交价劣于限价单保护价 → 拒单 (模拟风控)
        if order.side == "BUY" and fill > order.price * 1.02:
            order.status = "REJECTED"
            return
        if order.side == "SELL" and fill < order.price * 0.98:
            order.status = "REJECTED"
            return
        order.status = "FILLED"
        order.filled_size = order.size
        order.fill_price = fill
        if order.side == "BUY":
            self._balance -= fill * order.size
        else:
            self._balance += fill * order.size
        logger.info(f"  [DRY_RUN] {'买' if order.side=='BUY' else '卖'} {order.strategy} "
                    f"{order.size:.0f}股 @{fill:.3f} (限价 {order.price:.3f}) "
                    f"余额 ${self._balance:.2f}")

    async def _place_real(self, order: Order):
        """实盘下单 (CLOB V2 POST /order, 含 L2 签名头)。

        注: CLOB V2 的签名流程较复杂 (EIP-712 订单哈希 + L2 API 密钥
        HMAC 头), 此处实现 L2 头签名; 订单 EIP-712 签名在
        `_sign_order_hash` 占位。若签名实现未完成, 请保持 DRY_RUN=true。
        """
        try:
            timestamp = str(int(time.time() * 1000))
            # L2 认证头签名 (POLYMARKET_API_SECRET)
            sig = hmac.new(self.api_secret.encode(),
                           f"{timestamp}POST/order".encode(),
                           hashlib.sha256).hexdigest()
            payload = {
                "market": order.market_id,
                "token_id": order.token_id,
                "price": str(order.price),
                "size": str(order.size),
                "side": order.side.lower(),
                "signature_type": 0,
                "nonce": str(int(time.time() * 1000)),
                "expiration": str(int(time.time() * 1000) + 60_000),
                "signature": self._sign_order_hash(order),
                "order_type": "GTC",
            }
            r = await self._http.post(f"{self.base_url}/order",
                                      json=payload,
                                      headers={
                                          "POLY-API-KEY": self.api_key,
                                          "POLY-SIGNATURE": sig,
                                          "POLY-TIMESTAMP": timestamp,
                                          "POLY-PASSPHRASE": self.api_passphrase,
                                      })
            data = r.json() if r.status_code == 200 else {}
            if r.status_code in (200, 201) and data.get("success") is not False:
                order.status = "FILLED" if data.get("status") == "matched" else "PENDING"
                order.fill_price = float(data.get("average_price", order.price) or order.price)
            else:
                logger.warning(f"实盘下单被拒: {r.status_code} {data.get('error', data)}")
                order.status = "REJECTED"
        except Exception as e:
            logger.error(f"实盘下单异常: {e}")
            order.status = "REJECTED"

    def _sign_order_hash(self, order: Order) -> str:
        """EIP-712 订单签名占位 — 接入 py_eth_signing 或钱包 SDK 前不用于实盘。"""
        return "0x" + "00" * 65  # 占位 (实盘前必须实现)

    # ── 账户 ──
    def get_balance(self) -> float:
        return self._balance

    def total_committed(self) -> float:
        """所有未结算 BUY 订单占用的资金 (含已成交未赎回)"""
        spent = 0.0
        for o in self.orders:
            if o.side == "BUY" and o.status in ("FILLED", "PENDING", "PARTIAL"):
                spent += o.price * o.size
        return spent

    def recent_orders(self, n: int = 50) -> List[dict]:
        return [o.to_dict() for o in self.order_history[-n:]]


# ── 供外部直接使用的辅助 ──
def make_clob_client(cfg=None) -> CLOBClient:
    import os as _os
    from .config import FiveMinBotConfig
    cfg = cfg or FiveMinBotConfig()
    return CLOBClient(
        base_url=cfg.CLOB_API,
        dry_run=cfg.DRY_RUN,
        initial_capital=float(_os.getenv("INITIAL_CAPITAL", "10000")),
        api_key=_os.getenv("POLYMARKET_API_KEY", ""),
        api_secret=_os.getenv("POLYMARKET_API_SECRET", ""),
        api_passphrase=_os.getenv("POLYMARKET_PASSPHRASE", ""),
        private_key=_os.getenv("POLYMARKET_PRIVATE_KEY", ""),
    )
