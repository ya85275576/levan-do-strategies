#!/usr/bin/env python3
"""
HighTempTation — 实盘策略主循环

集成:
  - db_manager:    SQLite 持久化
  - data_fetcher:  集合预报采集
  - alert_manager: Telegram+飞书告警

流程:
  1. 启动时初始化 DB + 发现市场
  2. 主循环: 采集预报 → 查询价格 → 检查开仓 → 监控持仓 → 平仓
  3. 每分钟扫描一次，每 15 分钟采集一次预报
  4. 所有操作记录到 DB + 告警推送

用法:
  python hightemptation_live.py                          # dry-run 模式
  DRY_RUN=false python hightemptation_live.py            # 实盘模式
  DRY_RUN=false LIVE_MODE=full python hightemptation_live.py

环境变量:
  DRY_RUN          true/false (默认 true)
  LIVE_MODE        backtest/observe/small/full (默认 backtest)
  INITIAL_CAPITAL  初始资金 (默认 10000)
  SCAN_INTERVAL    扫描间隔秒 (默认 60)
  DB_PATH          数据库路径 (默认 hightemptation.db)
"""
import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

# 尝试导入 scipy（回退到近似函数）
try:
    from scipy.stats import norm as _norm
    def _gaussian_cdf(x): return float(_norm.cdf(x))
except ImportError:
    def _gaussian_cdf(x):
        if x < -8: return 0.0
        if x > 8: return 1.0
        a = [0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429]
        p = 0.3275911
        s = 1.0 if x >= 0 else -1.0
        t = 1.0 / (1.0 + p * abs(x))
        y = 1.0 - (((((a[4]*t+a[3])*t+a[2])*t+a[1])*t+a[0])*t) * math.exp(-x*x/2)
        return y if s > 0 else 1.0 - y

import sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_manager import TradeDB
from data_fetcher_advanced import DataFetcher, STATIONS, ENSEMBLE_MODELS
from alert_manager import AlertManager

# ── 从环境变量读配置 ──
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
LIVE_MODE = os.environ.get("LIVE_MODE", "backtest").lower()
INITIAL_CAPITAL = float(os.environ.get("INITIAL_CAPITAL", "10000"))
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "60"))
DB_PATH = os.environ.get("DB_PATH", "hightemptation.db")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("live")


# ════════════════════════════════════════════════════════════════
# 策略参数（与 v3 回测一致）
# ════════════════════════════════════════════════════════════════

MIN_EDGE = 0.20
TP_PCT = 9.0
SL_PCT = 6.5
TRAILING_ACTIVATE = 5.0
TRAILING_DRAWDOWN = 3.0
PRICE_LOW = 0.28
PRICE_HIGH = 0.72
MIN_PROB_EDGE = 0.12
ALLOWED_SIDE = "NO"
POSITION_SIZE_USD = 1.0
MAX_POSITIONS = 50
COMMISSION_PCT = 0.02
MIN_DEPTH = 200
MAX_IMPACT_RATIO = 0.2
MIN_HOURS_TO_EXPIRY = 6

# 实盘特有
FORCE_EXIT_BEFORE_SETTLEMENT_H = 4
DAILY_LOSS_LIMIT_PCT = 5.0


def bucket_prob(lower: float, upper: float, mu: float, sigma: float) -> float:
    """P(lower < X < upper) 高斯 CDF"""
    if sigma <= 0:
        return 1.0 if lower <= mu < upper else 0.0
    return max(0.0, min(1.0, _gaussian_cdf((upper - mu) / sigma) - _gaussian_cdf((lower - mu) / sigma)))


class LiveStrategy:
    """
    实盘策略引擎。

    与回测引擎 HighWinRateEngineV3 逻辑一致，但:
      - 从 DB 读取预报与市场数据（vs 回测遍历 K 线）
      - 通过 AlertManager 推送告警
      - 记录实时持仓到 DB
    """

    def __init__(self):
        self.db = TradeDB(DB_PATH)
        self.fetcher = DataFetcher(self.db)
        self.alert = AlertManager()

        self.equity = INITIAL_CAPITAL
        self.total_pnl = 0.0
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self._running = False
        self._last_forecast_time = 0
        self._open_positions: Dict[str, dict] = {}  # city_token → position info

    # ════════════════════════════════════════════════════════════════
    # 核心逻辑
    # ════════════════════════════════════════════════════════════════

    async def _scan_and_trade(self):
        """单次扫描 + 交易决策"""
        now = datetime.now(timezone.utc)

        # 每 15 分钟采集一次预报
        if time.time() - self._last_forecast_time > 900:
            logger.info("📡 采集集合预报...")
            forecasts = await self.fetcher.fetch_all_cities(forecast_days=7)
            self._last_forecast_time = time.time()
        else:
            forecasts = None

        # 遍历所有城市
        for city in STATIONS:
            # 获取最新预报
            fc = self.db.get_latest_forecast(city)
            if not fc:
                continue

            mu = fc["mu"]
            sigma = fc["sigma"]
            date_str = fc["date"]

            # 计算日期 → 结算时间
            try:
                fc_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                settlement = fc_date + timedelta(hours=23, minutes=59)
            except ValueError:
                continue

            # 结算时间过滤
            if now > settlement:
                continue
            if (settlement - now).total_seconds() < MIN_HOURS_TO_EXPIRY * 3600:
                continue

            # 检查该城市的市场 token
            tokens = self.db.conn.execute(
                "SELECT * FROM market_tokens WHERE city=? AND active=1",
                (city,),
            ).fetchall()

            for token_row in tokens:
                token = dict(token_row)
                bl, bu = token["bucket_lower"], token["bucket_upper"]
                p_model = bucket_prob(bl, bu, mu, sigma)
                edge = abs(p_model - 0.5)

                # 边缘过滤
                if edge < MIN_EDGE:
                    continue
                if abs(p_model - 0.5) < MIN_PROB_EDGE:
                    continue

                # 获取市场最新价格
                mkt = self.db.get_latest_market(city, bl, bu)
                if not mkt:
                    continue

                p_market = mkt["no_price"]
                depth = mkt.get("depth", 0) or 0

                # 价格范围过滤
                if not (PRICE_LOW <= p_market <= PRICE_HIGH):
                    continue

                # 深度过滤
                if depth < MIN_DEPTH:
                    continue
                impact = POSITION_SIZE_USD / (p_market * depth) if depth > 0 else 1.0
                if impact > MAX_IMPACT_RATIO:
                    continue

                # 开仓信号
                token_key = token["token_id"]
                if token_key not in self._open_positions:
                    if len(self._open_positions) >= MAX_POSITIONS:
                        continue

                    position = {
                        "token_id": token_key,
                        "city": city,
                        "side": ALLOWED_SIDE,
                        "entry_price": p_market,
                        "size": POSITION_SIZE_USD / p_market,
                        "edge": edge,
                        "entry_time": now.isoformat(),
                        "max_pnl_pct": 0.0,
                    }

                    # 写入 DB
                    trade_id = self.db.open_trade(
                        token_key, city, bl, bu, ALLOWED_SIDE,
                        p_market, POSITION_SIZE_USD,
                    )
                    if trade_id is None:
                        continue
                    position["trade_id"] = trade_id
                    self._open_positions[token_key] = position

                    logger.info(f"🟢 开仓 {city} NO @ ${p_market:.3f} edge={edge:.3f}")
                    if not DRY_RUN:
                        await self.alert.send_trade_open(city, "NO", p_market, edge, POSITION_SIZE_USD)

        # 监控已有持仓
        to_close = []
        for key, pos in self._open_positions.items():
            # 获取当前价格
            mkt = self.db.conn.execute("""
                SELECT no_price FROM market_prices
                WHERE token_id=? ORDER BY ts DESC LIMIT 1
            """, (pos["token_id"],)).fetchone()
            if not mkt:
                continue

            current_no = mkt["no_price"]
            pnl_pct = (pos["entry_price"] - current_no) / pos["entry_price"] * 100

            if pnl_pct > pos["max_pnl_pct"]:
                pos["max_pnl_pct"] = pnl_pct

            # 止盈
            if pnl_pct >= TP_PCT:
                to_close.append((key, current_no, "TP"))
                continue
            # 止损
            if pnl_pct <= -SL_PCT:
                to_close.append((key, current_no, "SL"))
                continue
            # 移动止盈
            if pos["max_pnl_pct"] >= TRAILING_ACTIVATE:
                if pos["max_pnl_pct"] - pnl_pct >= TRAILING_DRAWDOWN:
                    to_close.append((key, current_no, "Trailing"))
                    continue
            # 结算前强平
            try:
                et = datetime.fromisoformat(pos["entry_time"])
                settlement = et.replace(hour=23, minute=59)
                if settlement - now < timedelta(hours=FORCE_EXIT_BEFORE_SETTLEMENT_H):
                    to_close.append((key, current_no, "Settlement"))
                    continue
            except ValueError:
                pass

        # 执行平仓
        for key, exit_price, reason in to_close:
            pos = self._open_positions.pop(key, None)
            if not pos:
                continue

            pnl = (pos["entry_price"] - exit_price) * pos["size"]
            pnl -= pos["size"] * exit_price * (COMMISSION_PCT / 100)
            self.total_pnl += pnl
            self.daily_pnl += pnl
            self.equity += pnl

            self.db.close_trade(pos["trade_id"], exit_price, reason)
            logger.info(f"🔴 平仓 {pos['city']} {reason}: PnL={pnl:.2f}")
            if not DRY_RUN:
                await self.alert.send_trade_close(
                    pos["city"], pos["side"], pos["entry_price"],
                    exit_price, pnl, reason,
                )

        # 每日统计
        today = now.strftime("%Y-%m-%d")
        daily = self.db.get_daily_pnl(today)
        self.daily_trades = daily["cnt"]
        self.daily_pnl = daily["total_pnl"]

        # 日亏损风控
        if self.daily_pnl < -INITIAL_CAPITAL * DAILY_LOSS_LIMIT_PCT / 100:
            logger.warning(f"🚨 日亏损超限 ({self.daily_pnl:.2f}), 停止交易")
            if not DRY_RUN:
                await self.alert.send_risk_alert(
                    f"日亏损 {self.daily_pnl:.2f} 超过限额 {DAILY_LOSS_LIMIT_PCT}%"
                )

        # 状态日志
        logger.info(
            f"📊 {len(self._open_positions)}持仓 | "
            f"P&L={self.total_pnl:.2f} | "
            f"资金={self.equity:.2f} | "
            f"日交易={self.daily_trades}"
        )

    # ════════════════════════════════════════════════════════════════
    # 主循环
    # ════════════════════════════════════════════════════════════════

    async def run(self):
        logger.info("=" * 60)
        logger.info(f"  🌤️  HighTempTation 实盘策略")
        logger.info(f"  模式: {'🟢 实盘' if not DRY_RUN else '🟡 模拟'} / {LIVE_MODE.upper()}")
        logger.info(f"  资金: ${INITIAL_CAPITAL:.2f} | 扫描: {SCAN_INTERVAL}s | DB: {DB_PATH}")
        logger.info(f"  仓位: ${POSITION_SIZE_USD}/仓 × {MAX_POSITIONS} = ${POSITION_SIZE_USD * MAX_POSITIONS}")
        logger.info("=" * 60)

        # 初始采集
        logger.info("📡 首次采集预报...")
        try:
            await self.fetcher.fetch_all_cities(forecast_days=7)
            self._last_forecast_time = time.time()
        except Exception as e:
            logger.warning(f"预报采集失败: {e}")

        self._running = True
        while self._running:
            try:
                await self._scan_and_trade()
            except Exception as e:
                logger.error(f"扫描异常: {e}", exc_info=True)

            # 等待
            for _ in range(SCAN_INTERVAL):
                if not self._running:
                    break
                await asyncio.sleep(1)

        logger.info("策略已停止")

    async def stop(self):
        self._running = False
        # 强制平仓所有持仓
        for key, pos in list(self._open_positions.items()):
            self.db.close_trade(pos["trade_id"], 0, "ForceStop")
        await self.fetcher.close()
        await self.alert.close()
        self.db.close()


async def main():
    strategy = LiveStrategy()
    loop = asyncio.get_event_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(strategy.stop()))
        except NotImplementedError:
            pass

    try:
        await strategy.run()
    except KeyboardInterrupt:
        await strategy.stop()


if __name__ == "__main__":
    asyncio.run(main())
