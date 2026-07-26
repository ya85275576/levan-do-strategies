"""
OKX 订单执行器

通过 OKX V5 REST API 执行交易操作。
与现有 services/exchange/okx.js 使用相同的 API 模式和凭据源。

支持:
  - 模拟模式 (DRY_RUN=true)：仅记录日志，不实际下单
  - 市价单 / 限价单
  - 开仓 / 平仓
  - 设置杠杆
  - 查询持仓和余额
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("bot.order_manager")


class OkxOrderManager:
    """
    OKX 订单执行器

    API 文档: https://www.okx.com/docs-v5/zh/#rest-api
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        rest_url: str = "https://www.okx.com",
        dry_run: bool = True,
        symbol: str = "BTC-USDT",
        leverage: int = 1,
        position_mode: str = "isolated",
        simulated_trading: bool = False,
    ):
        """
        :param api_key: OKX API Key
        :param api_secret: OKX Secret Key
        :param api_passphrase: OKX Passphrase
        :param rest_url: REST API 基础 URL
        :param dry_run: 模拟模式
        :param symbol: 默认交易对
        :param leverage: 默认杠杆
        :param position_mode: 仓位模式 (isolated/cross)
        :param simulated_trading: 是否添加 x-simulated-trading 标头（OKX 模擬盤需要）
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.rest_url = rest_url.rstrip("/")
        self.dry_run = dry_run
        self.default_symbol = symbol
        self.default_leverage = leverage
        self.position_mode = position_mode
        self.simulated_trading = simulated_trading

        # 会话
        self._session: Optional[aiohttp.ClientSession] = None

        # 模拟状态
        self._simulated_positions: Dict[str, float] = {}
        self._simulated_orders: List[dict] = []
        self._simulated_balance: float = 0.0
        self._simulated_entry_price: Dict[str, float] = {}

        # 风控
        self._last_order_time: float = 0
        self._min_order_interval: float = 1.0  # 1 秒

        # 最高槓桿快取 {symbol: max_leverage}
        self._max_leverage_cache: Dict[str, int] = {}

        if self.dry_run:
            logger.info("🧪 模拟模式已启用 — 所有操作仅输出日志，不实际连接交易所")

    def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _timestamp(self) -> str:
        """生成 ISO 时间戳"""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """
        OKX V5 HMAC-SHA256 签名

        签名消息 = timestamp + method + requestPath + body
        """
        message = timestamp + method.upper() + request_path + body
        mac = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    async def _request(
        self,
        method: str,
        request_path: str,
        body: Optional[dict] = None,
    ) -> dict:
        """
        发送已签名的 REST 请求

        模拟模式下仅输出日志，不真实发送
        """
        if self.dry_run:
            body_str = json.dumps(body) if body else ""
            logger.debug(f"[模拟] {method} {request_path} body={body_str}")
            return {
                "code": "0",
                "msg": "模拟模式 — 请求已记录（未实际发送）",
                "data": [{}],
            }

        session = self._get_session()
        timestamp = self._timestamp()
        body_str = json.dumps(body) if body else ""
        sign = self._sign(timestamp, method, request_path, body_str)

        url = f"{self.rest_url}{request_path}"
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.api_passphrase,
            "Content-Type": "application/json",
        }

        # 模擬盤需要 x-simulated-trading 標頭
        if self.simulated_trading:
            headers["x-simulated-trading"] = "1"

        try:
            async with session.request(method, url, headers=headers,
                                       data=body_str if body else None) as resp:
                data = await resp.json()

                if data.get("code") != "0":
                    err_msg = f"[OKX] API 错误: code={data.get('code')}, msg={data.get('msg')}"
                    logger.error(err_msg)
                    raise RuntimeError(err_msg)

                return data
        except aiohttp.ClientError as e:
            logger.error(f"[OKX] 请求失败: {e}")
            raise

    async def get_server_time(self) -> dict:
        """获取服务器时间"""
        return await self._request("GET", "/api/v5/public/time")

    async def get_account_balance(self) -> List[dict]:
        """获取账户余额"""
        try:
            res = await self._request("GET", "/api/v5/account/balance")
            return res.get("data", [])
        except Exception as e:
            logger.error(f"获取账户余额失败: {e}")
            return []

    async def get_usdt_balance(self) -> float:
        """获取 USDT 余额"""
        if self.dry_run:
            return self._simulated_balance

        try:
            data = await self.get_account_balance()
            details = data[0].get("details", []) if data else []
            for d in details:
                if d.get("ccy") == "USDT":
                    return float(d.get("eq", 0))
            return 0.0
        except Exception as e:
            logger.error(f"获取 USDT 余额失败: {e}")
            return 0.0

    async def _public_request(self, request_path: str) -> dict:
        """發送公開 API 請求（無需認證）"""
        if self.dry_run:
            return {"code": "0", "msg": "模拟模式", "data": [{}]}

        session = self._get_session()
        url = f"{self.rest_url}{request_path}"

        try:
            async with session.request("GET", url) as resp:
                data = await resp.json()
                if data.get("code") != "0":
                    logger.warning(f"公開 API 錯誤: code={data.get('code')}, msg={data.get('msg')}")
                return data
        except aiohttp.ClientError as e:
            logger.error(f"公開 API 請求失敗: {e}")
            return {"code": "-1", "msg": str(e), "data": []}

    async def get_max_leverage(self, symbol: str) -> int:
        """
        查詢交易對的最高可用槓桿（帶快取）
        通過 /api/v5/public/instruments 公開 API
        """
        # 先查快取
        if symbol in self._max_leverage_cache:
            return self._max_leverage_cache[symbol]

        try:
            res = await self._public_request(
                f"/api/v5/public/instruments?instType=SWAP&instId={symbol}"
            )
            data = res.get("data", [])
            if data:
                # lever 字段是該幣種最高槓桿（如 "125"）
                max_lev = int(data[0].get("lever", "0"))
                if max_lev > 0:
                    self._max_leverage_cache[symbol] = max_lev
                    logger.info(f"📊 {symbol} 最高槓桿: {max_lev}x")
                    return max_lev
        except Exception as e:
            logger.warning(f"查詢 {symbol} 最高槓桿失敗: {e}")

        # 預設值
        logger.info(f"📊 {symbol} 無法查詢最高槓桿，使用預設 {self.default_leverage}x")
        return self.default_leverage

    async def set_leverage(self, symbol: str = None, leverage: int = None,
                           mode: str = None) -> Optional[dict]:
        """
        設置槓桿為該幣種最高可用的值。
        如果設置失敗（如該幣種不支持該槓桿），返回 None 而非拋出異常。
        """
        inst_id = symbol or self.default_symbol
        mgn_mode = mode or self.position_mode

        # 查詢最高槓桿
        max_lev = await self.get_max_leverage(inst_id)
        lever = max_lev  # 一律用最高槓桿

        logger.info(f"⚙️ 设置杠杆: {inst_id} {lever}x ({mgn_mode})")
        try:
            return await self._request("POST", "/api/v5/account/set-leverage", {
                "instId": inst_id,
                "lever": str(lever),
                "mgnMode": mgn_mode,
            })
        except Exception as e:
            logger.warning(f"⚠️ 设置杠杆失败 {inst_id} {lever}x: {e}（跳過該幣種）")
            return None

    async def place_order(
        self,
        symbol: str,
        side: str,  # "buy" or "sell"
        qty: float,
        order_type: str = "market",
        price: Optional[float] = None,
    ) -> dict:
        """
        执行订单

        :param symbol: 交易对 (如 BTC-USDT)
        :param side: 买卖方向 (buy/sell)
        :param qty: 数量
        :param order_type: 订单类型 (market/limit)
        :param price: 限价单价格
        :returns: API 响应
        """
        # ---- 风控检查 ----
        now = time.time()
        elapsed = now - self._last_order_time
        if elapsed < self._min_order_interval:
            wait = self._min_order_interval - elapsed
            logger.warning(f"⏳ 风控等待 {wait:.2f}s...")
            await asyncio.sleep(wait)

        # ---- 参数验证 ----
        if not symbol:
            raise ValueError("缺少 symbol")
        if side.lower() not in ("buy", "sell"):
            raise ValueError(f"无效 side: {side}")
        if qty <= 0:
            raise ValueError(f"无效数量: {qty}")

        order_type_lower = order_type.lower()
        if order_type_lower == "limit" and (price is None or price <= 0):
            raise ValueError("限价单必须指定有效的 price")

        symbol_info = f"[{symbol}] {side} {qty} @ {order_type_lower}"
        if price:
            symbol_info += f" ${price}"

        # ---- 构建订单参数 ----
        order_params = {
            "instId": symbol,
            "tdMode": self.position_mode,
            "side": side.lower(),
            "ordType": order_type_lower,
            "sz": str(qty),
        }
        if order_type_lower == "limit" and price is not None:
            order_params["px"] = str(price)

        logger.info(f"📤 下单: {symbol_info}")

        self._last_order_time = time.time()

        try:
            res = await self._request("POST", "/api/v5/trade/order", order_params)
            order_id = res.get("data", [{}])[0].get("ordId", f"sim-{int(now * 1000)}")
            logger.info(f"✅ 订单成功: orderId={order_id}")

            # 模拟模式：记录订单和持仓
            if self.dry_run:
                self._simulated_orders.append({
                    "id": order_id,
                    "symbol": symbol,
                    "side": side.lower(),
                    "qty": qty,
                    "type": order_type_lower,
                    "price": price,
                    "time": datetime.now(timezone.utc).isoformat(),
                })

                # 更新模拟持仓
                current = self._simulated_positions.get(symbol, 0.0)
                if side.lower() == "buy":
                    self._simulated_positions[symbol] = current + qty
                    self._simulated_entry_price[symbol] = price or 0
                else:
                    self._simulated_positions[symbol] = current - qty

                logger.info(f"📊 [模拟持仓] {symbol}: {self._simulated_positions[symbol]:.4f}")

            return res
        except Exception as e:
            logger.warning(f"⚠️ 下单失败 {symbol_info}: {e}（跳過）")
            return {"code": "-1", "msg": str(e), "data": [{}]}

    async def close_position(self, symbol: str = None) -> dict:
        """平仓"""
        inst_id = symbol or self.default_symbol
        logger.info(f"📤 平仓: {inst_id}")

        if self.dry_run:
            pos_size = self._simulated_positions.get(inst_id, 0.0)
            if pos_size == 0:
                logger.info(f"ℹ️ [模拟] {inst_id} 无持仓，无需平仓")
                return {"code": "0", "msg": "No position to close (simulated)"}

            pos_side = "long" if pos_size > 0 else "short"
            logger.info(f"[模拟] 平仓 {inst_id} ({pos_side}): {abs(pos_size):.4f}")
            self._simulated_positions[inst_id] = 0.0
            return {
                "code": "0",
                "msg": f"模拟平仓成功: {inst_id} {pos_side} {abs(pos_size):.4f}",
                "data": [{"ordId": f"sim-close-{int(time.time() * 1000)}"}],
            }

        try:
            # 获取当前持仓
            positions = await self.get_positions(inst_id)
            pos = positions[0] if positions else None

            if not pos or float(pos.get("pos", 0)) == 0:
                logger.info(f"ℹ️ {inst_id} 无持仓，无需平仓")
                return {"code": "0", "msg": "No position to close"}

            res = await self._request("POST", "/api/v5/trade/close-position", {
                "instId": inst_id,
                "mgnMode": pos.get("mgnMode", self.position_mode),
                "posSide": pos.get("posSide", "long"),
            })
            logger.info(f"✅ 平仓成功: {inst_id}")
            return res
        except Exception as e:
            logger.error(f"❌ 平仓失败 {inst_id}: {e}")
            raise

    async def get_positions(self, symbol: str = None) -> List[dict]:
        """获取持仓"""
        if self.dry_run:
            if symbol:
                pos_size = self._simulated_positions.get(symbol, 0.0)
                if pos_size == 0:
                    return []
                return [{
                    "instId": symbol,
                    "pos": str(pos_size),
                    "posSide": "long" if pos_size > 0 else "short",
                    "mgnMode": self.position_mode,
                    "uTime": str(int(time.time() * 1000)),
                }]
            return [
                {
                    "instId": sym,
                    "pos": str(pos),
                    "posSide": "long" if pos > 0 else "short",
                    "mgnMode": self.position_mode,
                    "uTime": str(int(time.time() * 1000)),
                }
                for sym, pos in self._simulated_positions.items()
                if pos != 0
            ]

        try:
            path = "/api/v5/account/positions"
            if symbol:
                path += f"?instId={symbol}"
            res = await self._request("GET", path)
            return res.get("data", [])
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            return []

    async def get_simulated_pnl(self, current_price: float, symbol: str = None) -> float:
        """计算模拟持仓的未实现盈亏"""
        inst_id = symbol or self.default_symbol
        pos = self._simulated_positions.get(inst_id, 0.0)
        entry = self._simulated_entry_price.get(inst_id, current_price)
        if pos > 0:
            return (current_price - entry) * pos
        elif pos < 0:
            return (entry - current_price) * abs(pos)
        return 0.0

    def get_simulated_position_size(self, symbol: str = None) -> float:
        """获取模拟持仓数量"""
        return self._simulated_positions.get(symbol or self.default_symbol, 0.0)

    def reset_simulation(self):
        """重置模拟状态"""
        self._simulated_positions.clear()
        self._simulated_orders.clear()
        self._simulated_balance = 0.0
        self._simulated_entry_price.clear()
        logger.info("🔄 模拟状态已重置")

    async def close(self):
        """关闭 HTTP 会话"""
        if self._session and not self._session.closed:
            await self._session.close()
