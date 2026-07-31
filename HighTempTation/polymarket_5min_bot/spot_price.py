"""polymarket_5min_bot — 现货价格轮询

从 Coinbase Public API 拉取 BTC/ETH 现货价 (无认证), 失败时回退
到模拟随机游走, 保证 DRY_RUN 与离线环境可运行。
"""
import asyncio
import logging
import random
import time
from typing import Dict, Optional

import httpx

logger = logging.getLogger("polymarket_5min.spot")


class SpotPriceFeed:
    def __init__(self, assets: list, interval_sec: int = 5,
                 http: Optional[httpx.AsyncClient] = None):
        self.assets = [a.upper() for a in assets]
        self.interval = interval_sec
        self._http = http
        self._own_http = http is None
        self.prices: Dict[str, float] = {}
        self.last_update = 0.0
        self.using_sim = True
        self._sim_price: Dict[str, float] = {}

    async def start(self):
        if self._own_http:
            self._http = httpx.AsyncClient(timeout=10.0)
        base = {"BTC": 61_500.0, "ETH": 3_000.0}
        for a in self.assets:
            self._sim_price[a] = base.get(a, 100.0)
        # 首次拉取
        await self.refresh()

    async def stop(self):
        if self._own_http and self._http:
            await self._http.aclose()

    async def refresh(self):
        """拉取一次全部资产现货价; 全部失败则使用模拟随机游走"""
        ok = 0
        for a in self.assets:
            try:
                r = await self._http.get(
                    f"https://api.coinbase.com/v2/prices/{a}-USD/spot")
                if r.status_code == 200:
                    price = float(r.json()["data"]["amount"])
                    self.prices[a] = price
                    self._sim_price[a] = price
                    ok += 1
            except Exception as e:
                logger.debug(f"现货 {a} 获取失败: {e}")
        if ok > 0:
            self.using_sim = False
        else:
            self.using_sim = True
            # 模拟随机游走
            for a in self.assets:
                self._sim_price[a] *= 1.0 + random.uniform(-0.0008, 0.0008)
                self.prices[a] = self._sim_price[a]
        self.last_update = time.time()
        return self.prices

    def get(self, asset: str) -> float:
        return self.prices.get(asset.upper(), self._sim_price.get(asset.upper(), 0.0))

    async def loop(self):
        """后台轮询循环"""
        while True:
            try:
                await self.refresh()
            except Exception as e:
                logger.warning(f"现货轮询异常: {e}")
            await asyncio.sleep(self.interval)
