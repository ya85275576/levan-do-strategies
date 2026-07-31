"""polymarket_5min_bot — 市场发现

真实 5 分钟 up/down 市场扫描 (Gamma API) + 模拟市场回退。

5 分钟加密市场的典型结构:
  - 一个 event (如 "Bitcoin Up or Down - 5 Minute"), 内含两个 market:
    YES 市场 (价格接近 P(涨)), NO 市场 (价格接近 P(跌)), 互为互补
  - 每个 market 有 clobTokenIds[0]=YES token, [1]=NO token
  - 周期极短 (5 分钟), 结算后立即开启下一周期

当 Gamma 无活跃 5min 市场时 (该品类周期性开放), 生成模拟市场,
保证 DRY_RUN 验证 / 策略开发始终可运行。
"""
import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx

logger = logging.getLogger("polymarket_5min.markets")


@dataclass
class FiveMinMarket:
    """5 分钟市场对 (YES + NO 互为互补)"""
    event_id: str
    event_title: str
    asset: str                     # BTC / ETH / ...
    strike_price: float            # 周期开始时资产价格 (行权价)
    start_time: float              # 周期开始 (epoch s)
    end_time: float                # 周期结束 (epoch s)
    yes_market_id: str
    no_market_id: str
    yes_token_id: str
    no_token_id: str
    simulated: bool = False
    # 现货参考价 (周期内实时更新)
    spot_price: float = field(default=0.0)
    # 实时价格缓存 {token_id: price}
    prices: dict = field(default_factory=dict)

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.end_time - time.time())

    @property
    def is_live(self) -> bool:
        return time.time() < self.end_time

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_title": self.event_title,
            "asset": self.asset,
            "strike_price": self.strike_price,
            "start_time": datetime.fromtimestamp(self.start_time, timezone.utc).isoformat(),
            "end_time": datetime.fromtimestamp(self.end_time, timezone.utc).isoformat(),
            "seconds_left": round(self.seconds_left, 1),
            "yes_market_id": self.yes_market_id,
            "no_market_id": self.no_market_id,
            "simulated": self.simulated,
            "spot_price": self.spot_price,
            "prices": {k: round(v, 4) for k, v in self.prices.items()},
        }


class MarketScanner:
    def __init__(self, cfg, http: Optional[httpx.AsyncClient] = None):
        self.cfg = cfg
        self._http = http
        self._own_http = http is None
        self._sim_counter = 0
        self._last_sim_spots: dict = {}

    async def start(self):
        if self._own_http:
            self._http = httpx.AsyncClient(timeout=15.0)

    async def stop(self):
        if self._own_http and self._http:
            await self._http.aclose()

    # ────────────────────────────────────────────────────────────────
    # 真实市场扫描
    # ────────────────────────────────────────────────────────────────
    async def scan_real(self) -> List[FiveMinMarket]:
        """从 Gamma API 扫描活跃的短周期 up/down 市场。"""
        found: List[FiveMinMarket] = []
        for term in self.cfg.SEARCH_TERMS:
            try:
                r = await self._http.get(f"{self.cfg.GAMMA_API}/events",
                                         params={"text": term, "active": "true",
                                                 "limit": 50})
                if r.status_code != 200:
                    continue
                for ev in (r.json() or []):
                    m = self._parse_event(ev)
                    if m:
                        found.append(m)
            except Exception as e:
                logger.debug(f"扫描 '{term}' 失败: {e}")
        # 去重
        seen = set()
        uniq = []
        for m in found:
            if m.yes_market_id in seen:
                continue
            seen.add(m.yes_market_id)
            uniq.append(m)
        return uniq

    def _parse_event(self, ev: dict) -> Optional[FiveMinMarket]:
        try:
            title = (ev.get("title") or ev.get("slug") or "")
            if not any(k in title.lower() for k in ("up or down", "up/down", "5 minute", "5-min")):
                return None
            end_str = ev.get("endDate") or ""
            start_str = ev.get("startDate") or ""
            try:
                end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except Exception:
                return None
            duration = (end - start).total_seconds() / 60
            if not (0 < duration <= 60):
                return None  # 只接受 ≤60 分钟的短周期
            markets = ev.get("markets") or []
            if len(markets) < 2:
                return None
            # 找出 YES/NO 两个市场 (question 含 up/yes 或 down/no)
            yes_m = no_m = None
            for mk in markets:
                q = (mk.get("question") or "").lower()
                if yes_m is None and ("up" in q or "yes" in q):
                    yes_m = mk
                elif no_m is None and ("down" in q or "no" in q):
                    no_m = mk
            if not yes_m or not no_m:
                return None
            # 资产识别
            asset = "BTC" if "btc" in title.lower() or "bitcoin" in title.lower() else \
                "ETH" if "eth" in title.lower() or "ethereum" in title.lower() else \
                title.split()[0].upper() if title.split() else "BTC"
            tokens = (yes_m.get("clobTokenIds") or "")
            tokens = tokens.split(",") if tokens else []
            tokens_n = (no_m.get("clobTokenIds") or "")
            tokens_n = tokens_n.split(",") if tokens_n else []
            if not tokens or not tokens_n:
                return None
            spot = self._last_sim_spots.get(asset, 0.0)
            return FiveMinMarket(
                event_id=str(ev.get("id", "")),
                event_title=title,
                asset=asset,
                strike_price=self._last_sim_spots.get(asset, 100.0),
                start_time=start.timestamp(),
                end_time=end.timestamp(),
                yes_market_id=str(yes_m.get("id", "")),
                no_market_id=str(no_m.get("id", "")),
                yes_token_id=tokens[0],
                no_token_id=tokens_n[0],
                simulated=False,
                spot_price=spot,
            )
        except Exception as e:
            logger.debug(f"事件解析失败: {e}")
            return None

    # ────────────────────────────────────────────────────────────────
    # 模拟市场 (真实市场不可用时的回退, 供 DRY_RUN 验证)
    # ────────────────────────────────────────────────────────────────
    def scan_sim(self) -> List[FiveMinMarket]:
        """生成模拟 5 分钟周期市场。

        模拟随机游走: 每周期从当前"现价"出发, 生成 0~1 方向的漂移,
        反向生成 YES/NO 概率, 使模拟订单簿可被策略消费。
        """
        if not self.cfg.SIM_MARKETS_ENABLED:
            return []
        out: List[FiveMinMarket] = []
        for _ in range(self.cfg.SIM_CYCLES_PER_SCAN):
            for asset in self.cfg.TARGET_ASSETS:
                self._sim_counter += 1
                base = self._last_sim_spots.get(asset, 60_000.0 if asset == "BTC" else 3_000.0)
                # 模拟该周期内的漂移方向
                drift = random.uniform(-0.002, 0.002)
                p_up = 0.5 + drift * 200  # 使 p_up 在 0.1~0.9 附近波动
                p_up = max(0.05, min(0.95, p_up))
                # 模拟盘口价差: YES+NO 组合成本 < 1 (0.94~0.99),
                # 使套利/Ladder 也能在模拟盘触发
                gap = random.uniform(0.01, 0.06)
                p_yes = p_up * (1 - gap / 2)
                p_no = (1.0 - p_up) * (1 - gap / 2)
                # 偶尔制造极端 (结算前狙击窗口的形态)
                if random.random() < 0.15:
                    p_yes = max(0.05, min(0.98, p_yes))
                    p_no = 1.0 - p_yes
                now = time.time()
                end = now + self.cfg.MARKET_WINDOW_MIN * 60
                eid = f"sim-{asset}-{self._sim_counter}"
                m = FiveMinMarket(
                    event_id=eid,
                    event_title=f"{asset} Up or Down - 5 Minute (SIM)",
                    asset=asset,
                    strike_price=base,
                    start_time=now,
                    end_time=end,
                    yes_market_id=f"{eid}-yes",
                    no_market_id=f"{eid}-no",
                    yes_token_id=f"tok-{eid}-yes",
                    no_token_id=f"tok-{eid}-no",
                    simulated=True,
                    spot_price=base,
                )
                # 为模拟市场预生成价格 (策略可直接读取)
                m.prices = {
                    m.yes_token_id: round(p_yes, 4),
                    m.no_token_id: round(p_no, 4),
                }
                out.append(m)
        return out

    # ────────────────────────────────────────────────────────────────
    # 统一入口
    # ────────────────────────────────────────────────────────────────
    async def scan(self) -> List[FiveMinMarket]:
        real = await self.scan_real()
        if real:
            logger.info(f"📡 真实 5min 市场: {len(real)} 个")
            return real
        sim = self.scan_sim()
        if sim:
            logger.info(f"🧪 无活跃真实 5min 市场, 回退模拟市场: {len(sim)} 个")
        return sim

    def update_spot(self, asset: str, price: float):
        """由 spot_price 轮询回调更新模拟盘行权价与现价"""
        self._last_sim_spots[asset] = price
