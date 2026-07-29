#!/usr/bin/env python3
"""
HighTempTation — 限价单执行器

功能:
  1. 挂限价单 (优于市价 offset 0.2%)
  2. 后台线程监控成交状态
  3. 最多等待 timeout=60s
  4. 超时撤单，回退到市价单
  5. 记录挂单/成交/撤单到 DB limit_orders 表

用法:
  executor = LimitOrderExecutor(db=TradeDB("hightemptation.db"))
  result = await executor.execute_limit_order(
      token_id="12345",
      side="NO",
      price=0.35,
      size=10.0,
  )
  # → {"status": "filled"|"timeout"|"cancelled", "fill_price": 0.3493, "filled_size": 10.0}
"""
import asyncio
import logging
import math
import random
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from db_manager import TradeDB

logger = logging.getLogger("limit_executor")

# ── 默认参数 ──
DEFAULT_OFFSET = 0.002       # 优于市价 0.2%
DEFAULT_TIMEOUT_SEC = 60     # 最长等待 60s
POLL_INTERVAL = 0.5          # 轮询间隔 (秒)
MAX_RETRY_PRICE_LEVELS = 3   # 撤单后最多重试 3 个价位


class LimitOrderExecutor:
    """
    限价单执行器。

    流程:
      1. 读取当前市价 NO 深度
      2. 计算限价 = 市价 * (1 - offset) 买入 NO (offset=0.2%)
      3. 挂单
      4. 后台轮询成交状态 (每 0.5s)
      5. 超时 → 撤单 → 可选回退市价
    """

    def __init__(self, db: TradeDB, offset: float = DEFAULT_OFFSET,
                 timeout_sec: float = DEFAULT_TIMEOUT_SEC):
        self.db = db
        self.offset = offset
        self.timeout_sec = timeout_sec
        self._pending_orders: Dict[str, dict] = {}  # order_id → order_info

    # ════════════════════════════════════════════════════════════════
    # 模拟成交检测
    # ════════════════════════════════════════════════════════════════

    async def _poll_fill_status(self, order_id: str, limit_price: float,
                                 side: str, size: float) -> Tuple[bool, float, float]:
        """
        轮询成交状态（模拟版）。

        实盘集成时替换为 Polymarket CLOB API 查询:
          GET /orders/{order_id} → 检查 status / filled_size / avg_fill_price

        模拟逻辑: 价格在 timeout 内随机波动，
          如果市价触及或优于限价 → 成交概率增加。
        """
        start_time = time.time()
        current_price = limit_price / (1 - self.offset) if side == "NO" else limit_price * (1 - self.offset)

        while time.time() - start_time < self.timeout_sec:
            elapsed = time.time() - start_time

            # 模拟市价随机波动 (实盘替换为真实价格查询)
            price_move = random.gauss(0, 0.001) * math.sqrt(elapsed)
            current_price *= (1 + price_move)

            if side == "NO":
                # 买入 NO: 市价 ≤ 限价时成交 (NO 价格上涨意味着 YES 下跌)
                if current_price <= limit_price:
                    fill_prob = min(1.0, (limit_price - current_price) / limit_price * 100 + 0.3)
                    if random.random() < fill_prob:
                        fill_price = min(limit_price, current_price)
                        logger.info(f"  ✅ 限价单成交: {order_id} @ ${fill_price:.4f} (限价 ${limit_price:.4f})")
                        return True, fill_price, size
            else:
                if current_price >= limit_price:
                    fill_prob = min(1.0, (current_price - limit_price) / limit_price * 100 + 0.3)
                    if random.random() < fill_prob:
                        fill_price = max(limit_price, current_price)
                        logger.info(f"  ✅ 限价单成交: {order_id} @ ${fill_price:.4f}")
                        return True, fill_price, size

            await asyncio.sleep(POLL_INTERVAL)

        logger.info(f"  ⏰ 限价单超时: {order_id} ({self.timeout_sec}s)")
        return False, 0.0, 0.0

    # ════════════════════════════════════════════════════════════════
    # 执行限价单
    # ════════════════════════════════════════════════════════════════

    async def execute_limit_order(self, token_id: str, side: str,
                                   market_price: float, size: float,
                                   timeout_sec: Optional[float] = None,
                                   fallback_to_market: bool = True) -> dict:
        """
        执行限价单。

        :param token_id: Polymarket token ID
        :param side: "YES" 或 "NO"
        :param market_price: 当前市价
        :param size: 买入金额 ($)
        :param timeout_sec: 超时秒数 (默认 60)
        :param fallback_to_market: 超时后是否回退到市价单
        :returns: {"status": str, "fill_price": float, "filled_size": float, "order_id": str}
        """
        timeout = timeout_sec or self.timeout_sec

        if side == "NO":
            limit_price = market_price * (1 - self.offset)
        else:
            limit_price = market_price * (1 + self.offset)

        limit_price = max(0.01, min(0.99, limit_price))

        order_id = f"limit_{token_id[-8:]}_{int(time.time() * 1000)}"
        logger.info(f"📋 限价单: {order_id} {side} ${limit_price:.4f} (市价 ${market_price:.4f}, offset {self.offset*100:.2f}%)")

        self._pending_orders[order_id] = {
            "token_id": token_id,
            "side": side,
            "limit_price": limit_price,
            "size": size,
            "created_at": time.time(),
        }

        # 记录到 DB limit_orders 表
        try:
            self.db.conn.execute("""
                CREATE TABLE IF NOT EXISTS limit_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT UNIQUE,
                    token_id TEXT,
                    side TEXT,
                    limit_price REAL,
                    market_price REAL,
                    size REAL,
                    status TEXT DEFAULT 'pending',
                    filled_price REAL,
                    filled_size REAL,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            self.db.conn.execute(
                "INSERT OR IGNORE INTO limit_orders(order_id, token_id, side, limit_price, market_price, size) "
                "VALUES(?,?,?,?,?,?)",
                (order_id, token_id, side, limit_price, market_price, size),
            )
            self.db.conn.commit()
        except Exception as e:
            logger.warning(f"记录限价单失败: {e}")

        # 模拟成交检测
        filled, fill_price, filled_size = await self._poll_fill_status(
            order_id, limit_price, side, size,
        )

        if filled:
            self.db.conn.execute(
                "UPDATE limit_orders SET status='filled', filled_price=?, filled_size=?, "
                "updated_at=datetime('now') WHERE order_id=?",
                (round(fill_price, 6), round(filled_size, 4), order_id),
            )
            self.db.conn.commit()
            self._pending_orders.pop(order_id, None)
            return {
                "status": "filled",
                "fill_price": round(fill_price, 6),
                "filled_size": round(filled_size, 4),
                "order_id": order_id,
            }

        # 超时撤单
        self.db.conn.execute(
            "UPDATE limit_orders SET status='cancelled', updated_at=datetime('now') WHERE order_id=?",
            (order_id,),
        )
        self.db.conn.commit()
        self._pending_orders.pop(order_id, None)

        # 回退到市价
        if fallback_to_market:
            logger.info(f"  ↪ 回退市价单: {token_id}")
            return {
                "status": "market_fallback",
                "fill_price": round(market_price, 6),
                "filled_size": round(size, 4),
                "order_id": f"market_{token_id[-8:]}_{int(time.time()*1000)}",
            }

        return {
            "status": "timeout",
            "fill_price": 0.0,
            "filled_size": 0.0,
            "order_id": order_id,
        }

    # ════════════════════════════════════════════════════════════════
    # 批量执行 + 集成辅助
    # ════════════════════════════════════════════════════════════════

    async def execute_with_calibrated_price(self, token_id: str, side: str,
                                              market_price: float, size: float,
                                              calibrated_prob: Optional[float] = None) -> dict:
        """
        带概率校准的限价单执行。

        如果校准概率比市价更优，调整限价以确保成交概率。
        """
        offset = self.offset
        if calibrated_prob is not None:
            # 校准概率 vs 市价: 如果校准概率更低(更看空)，
            # 可接受更高限价
            prob_diff = calibrated_prob - market_price
            if side == "NO" and prob_diff < -0.02:
                offset = max(0.0005, offset * 0.5)  # 更激进
            elif side == "NO" and prob_diff > 0.02:
                offset = min(0.01, offset * 2.0)  # 更保守

        executor = LimitOrderExecutor(db=self.db, offset=offset)
        return await executor.execute_limit_order(token_id, side, market_price, size)

    def get_pending_count(self) -> int:
        return len(self._pending_orders)

    def cancel_all(self):
        """取消所有挂单"""
        for oid in list(self._pending_orders.keys()):
            self.db.conn.execute(
                "UPDATE limit_orders SET status='cancelled', updated_at=datetime('now') WHERE order_id=?",
                (oid,),
            )
        self.db.conn.commit()
        self._pending_orders.clear()
        logger.info(f"已取消 {len(self._pending_orders)} 个挂单")
