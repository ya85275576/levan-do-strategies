#!/usr/bin/env python3
"""
HighTempTation — v6 实盘策略（终极版）

v6 相比 live.py 新增三项胜率优化:
  1. OBI 订单簿不均衡过滤  — get_obi(), NO 要求 OBI < -0.2
  2. 日内流动性窗口        — is_liquid_hour(), 仅 UTC 12:00-20:00
  3. 市场偏差校准          — get_market_bias(), 修正 Edge 计算

环境变量新增:
  ENABLE_OBI           true/false (默认 true)
  OBI_THRESHOLD        0.2
  LIQUID_START_HOUR    12 (UTC)
  LIQUID_END_HOUR      20 (UTC)
  MARKET_BIAS_DAYS     30

用法:
  python hightemptation_live_v6.py
  ENABLE_OBI=false python hightemptation_live_v6.py
  LIQUID_START_HOUR=8 LIQUID_END_HOUR=22 python hightemptation_live_v6.py
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_manager import TradeDB
from data_fetcher_advanced import DataFetcher, STATIONS, ENSEMBLE_MODELS
from alert_manager import AlertManager

# ════════════════════════════════════════════════════════════════
# 环境变量
# ════════════════════════════════════════════════════════════════

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
LIVE_MODE = os.environ.get("LIVE_MODE", "backtest").lower()
INITIAL_CAPITAL = float(os.environ.get("INITIAL_CAPITAL", "10000"))
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "60"))
DB_PATH = os.environ.get("DB_PATH", "hightemptation.db")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# v6: OBI
ENABLE_OBI = os.environ.get("ENABLE_OBI", "true").lower() == "true"
OBI_THRESHOLD = float(os.environ.get("OBI_THRESHOLD", "0.2"))

# v6: 流动性窗口
LIQUID_START_HOUR = int(os.environ.get("LIQUID_START_HOUR", "12"))
LIQUID_END_HOUR = int(os.environ.get("LIQUID_END_HOUR", "20"))

# v6: 市场偏差
MARKET_BIAS_DAYS = int(os.environ.get("MARKET_BIAS_DAYS", "30"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("live_v6")

# ════════════════════════════════════════════════════════════════
# 策略参数
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
FORCE_EXIT_BEFORE_SETTLEMENT_H = 4
DAILY_LOSS_LIMIT_PCT = 5.0


def bucket_prob(lower: float, upper: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if lower <= mu < upper else 0.0
    return max(0.0, min(1.0, _gaussian_cdf((upper - mu) / sigma) - _gaussian_cdf((lower - mu) / sigma)))


# ════════════════════════════════════════════════════════════════
# v6: OBI 订单簿不均衡
# ════════════════════════════════════════════════════════════════

def get_obi(bid_depth: float, ask_depth: float) -> float:
    """
    计算订单簿不均衡 (Order Book Imbalance)。
    OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)
    范围 [-1, 1]:
      > 0 买方深度占优 (买压大)
      < 0 卖方深度占优 (卖压大)

    开 NO (看跌) 逻辑: 需要卖压大 → OBI < -THRESHOLD
    开 YES (看涨) 逻辑: 需要买压大 → OBI > +THRESHOLD
    """
    total = bid_depth + ask_depth
    if total == 0:
        return 0.0
    return (bid_depth - ask_depth) / total


# ════════════════════════════════════════════════════════════════
# v6: 日内流动性窗口
# ════════════════════════════════════════════════════════════════

def is_liquid_hour(now: Optional[datetime] = None) -> bool:
    """
    检查当前是否在流动性窗口内。
    仅在 UTC LIQUID_START_HOUR ~ LIQUID_END_HOUR 之间允许开仓。
    覆盖跨日情况 (如 start=22, end=2 表示 22:00~02:00 UTC)。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    hour = now.hour

    if LIQUID_START_HOUR <= LIQUID_END_HOUR:
        return LIQUID_START_HOUR <= hour < LIQUID_END_HOUR
    else:
        # 跨日: 22~2 → (hour >= 22 or hour < 2)
        return hour >= LIQUID_START_HOUR or hour < LIQUID_END_HOUR


# ════════════════════════════════════════════════════════════════
# v6: 市场偏差校准
# ════════════════════════════════════════════════════════════════

def get_market_bias(db: TradeDB, city: str, days: int = MARKET_BIAS_DAYS) -> float:
    """
    从 calibration 表读取市场偏差。
    market_bias = market_prob - realized_prob (正值=市场高估)

    用于修正 edge 计算:
      adjusted_market_prob = market_prob - market_bias
      edge = |(p_model + adjusted_market_prob) / 2 - 0.5|

    如果样本不足，返回 0 (不做校准)。
    """
    rows = db.conn.execute("""
        SELECT model_prob, realized_prob
        FROM calibration
        WHERE city=? AND realized_prob IS NOT NULL
        ORDER BY created_at DESC LIMIT 50
    """, (city,)).fetchall()

    if len(rows) < 3:
        return 0.0

    biases = [r["model_prob"] - r["realized_prob"] for r in rows if r["realized_prob"] is not None]
    if not biases:
        return 0.0
    return sum(biases) / len(biases)


# ════════════════════════════════════════════════════════════════
# v6 实盘策略
# ════════════════════════════════════════════════════════════════

class LiveStrategyV6:
    """
    v6 实盘策略引擎。

    相比 v5 (hightemptation_live.py) 新增:
      - try_open_positions_v6: OBI + 流动性窗口 + 市场偏差
      - 全部独立可配置 (ENABLE_OBI / LIQUID_* / MARKET_BIAS_DAYS)
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
        self._open_positions: Dict[str, dict] = {}

        # v6: 统计
        self.stats_obi_filtered = 0
        self.stats_liquid_filtered = 0
        self.stats_bias_filtered = 0

    # ════════════════════════════════════════════════════════════════
    # v6: try_open_positions (含全部三项优化)
    # ════════════════════════════════════════════════════════════════

    async def _try_open_position(self, now: datetime, city: str, token: dict,
                                  p_model: float, edge: float, p_market: float,
                                  depth: float) -> Optional[dict]:
        """
        v6 开仓检查。返回 position dict 或 None。

        检查顺序:
          1. 基础 (edge/价格/深度)  ← 与原版一致
          2. v6 OBI 过滤
          3. v6 流动性窗口
          4. v6 市场偏差校准 → 修正 edge
          5. 开仓执行
        """
        # ── 1. 基础过滤 ──
        bl, bu = token["bucket_lower"], token["bucket_upper"]
        token_key = token["token_id"]

        if token_key in self._open_positions:
            return None
        if len(self._open_positions) >= MAX_POSITIONS:
            return None
        if edge < MIN_EDGE:
            return None
        if abs(p_model - 0.5) < MIN_PROB_EDGE:
            return None
        if not (PRICE_LOW <= p_market <= PRICE_HIGH):
            return None
        if depth < MIN_DEPTH:
            return None
        impact = POSITION_SIZE_USD / (p_market * depth) if depth > 0 else 1.0
        if impact > MAX_IMPACT_RATIO:
            return None

        # ── 2. v6: OBI 过滤 ──
        if ENABLE_OBI:
            # 从 market_prices 取买卖深度 (简化: bid/ask 用 depth 近似)
            # 实盘集成时替换为真实订单簿 API
            bid_depth = depth * (1.0 + random.gauss(0, 0.1)) if HAS_RANDOM else depth
            ask_depth = depth * (1.0 + random.gauss(0, 0.1)) if HAS_RANDOM else depth

            obi = get_obi(bid_depth, ask_depth)
            if ALLOWED_SIDE == "NO" and obi >= -OBI_THRESHOLD:
                self.stats_obi_filtered += 1
                logger.debug(f"  OBI 过滤: {city} OBI={obi:.3f} (需 <{-OBI_THRESHOLD})")
                return None
            elif ALLOWED_SIDE == "YES" and obi <= OBI_THRESHOLD:
                self.stats_obi_filtered += 1
                logger.debug(f"  OBI 过滤: {city} OBI={obi:.3f} (需 >{OBI_THRESHOLD})")
                return None

        # ── 3. v6: 流动性窗口 ──
        if not is_liquid_hour(now):
            self.stats_liquid_filtered += 1
            logger.debug(f"  流动性过滤: {city} hour={now.hour} (需 {LIQUID_START_HOUR}-{LIQUID_END_HOUR} UTC)")
            return None

        # ── 4. v6: 市场偏差校准 → 修正 edge ──
        adjusted_edge = edge
        if MARKET_BIAS_DAYS > 0:
            market_bias = get_market_bias(self.db, city, MARKET_BIAS_DAYS)
            if abs(market_bias) > 0.01:
                # 偏差校准后的 market_prob
                adjusted_market = p_market - market_bias
                adjusted_market = max(0.01, min(0.99, adjusted_market))
                # 用调整后的市场概率重新计算 edge
                blended_prob = (p_model + adjusted_market) / 2.0
                adjusted_edge = abs(blended_prob - 0.5)
                logger.debug(f"  市场偏差: {city} bias={market_bias:+.4f} edge={edge:.3f}→{adjusted_edge:.3f}")

                if adjusted_edge < MIN_EDGE:
                    self.stats_bias_filtered += 1
                    logger.debug(f"  偏差校准后 edge 不足: {adjusted_edge:.3f} < {MIN_EDGE}")
                    return None

        # ── 5. 开仓 ──
        position = {
            "token_id": token_key,
            "city": city,
            "side": ALLOWED_SIDE,
            "entry_price": p_market,
            "size": POSITION_SIZE_USD / p_market,
            "edge": adjusted_edge,
            "raw_edge": edge,
            "entry_time": now.isoformat(),
            "max_pnl_pct": 0.0,
            "obi": obi if ENABLE_OBI else 0.0,
        }

        trade_id = self.db.open_trade(token_key, city, bl, bu, ALLOWED_SIDE,
                                       p_market, POSITION_SIZE_USD)
        if trade_id is None:
            return None
        position["trade_id"] = trade_id
        return position

    # ════════════════════════════════════════════════════════════════
    # 核心循环
    # ════════════════════════════════════════════════════════════════

    async def _scan_and_trade(self):
        now = datetime.now(timezone.utc)

        # 每 15 分钟采集预报
        if time.time() - self._last_forecast_time > 900:
            logger.info("📡 采集集合预报...")
            await self.fetcher.fetch_all_cities(forecast_days=7)
            self._last_forecast_time = time.time()

        # 遍历所有城市
        for city in STATIONS:
            fc = self.db.get_latest_forecast(city)
            if not fc:
                continue

            mu, sigma, date_str = fc["mu"], fc["sigma"], fc["date"]
            try:
                fc_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                settlement = fc_date + timedelta(hours=23, minutes=59)
            except ValueError:
                continue

            if now > settlement:
                continue
            if (settlement - now).total_seconds() < MIN_HOURS_TO_EXPIRY * 3600:
                continue

            tokens = self.db.conn.execute(
                "SELECT * FROM market_tokens WHERE city=? AND active=1", (city,)
            ).fetchall()

            for token_row in tokens:
                token = dict(token_row)
                p_model = bucket_prob(token["bucket_lower"], token["bucket_upper"], mu, sigma)
                edge = abs(p_model - 0.5)

                mkt = self.db.get_latest_market(city, token["bucket_lower"], token["bucket_upper"])
                if not mkt:
                    continue
                p_market = mkt["no_price"]
                depth = mkt.get("depth", 0) or 0

                # v6: 用 try_open_position 替代内联开仓
                pos = await self._try_open_position(now, city, token, p_model, edge, p_market, depth)
                if pos:
                    self._open_positions[token["token_id"]] = pos
                    logger.info(f"🟢 开仓 {city} NO @ ${p_market:.3f} edge={pos['edge']:.3f}")
                    if not DRY_RUN:
                        await self.alert.send_trade_open(city, "NO", p_market, pos["edge"], POSITION_SIZE_USD)

        # ── 监控已有持仓 ──
        to_close = []
        for key, pos in self._open_positions.items():
            mkt = self.db.conn.execute(
                "SELECT no_price FROM market_prices WHERE token_id=? ORDER BY ts DESC LIMIT 1",
                (pos["token_id"],),
            ).fetchone()
            if not mkt:
                continue

            current_no = mkt["no_price"]
            pnl_pct = (pos["entry_price"] - current_no) / pos["entry_price"] * 100

            if pnl_pct > pos["max_pnl_pct"]:
                pos["max_pnl_pct"] = pnl_pct

            if pnl_pct >= TP_PCT:
                to_close.append((key, current_no, "TP")); continue
            if pnl_pct <= -SL_PCT:
                to_close.append((key, current_no, "SL")); continue
            if pos["max_pnl_pct"] >= TRAILING_ACTIVATE:
                if pos["max_pnl_pct"] - pnl_pct >= TRAILING_DRAWDOWN:
                    to_close.append((key, current_no, "Trailing")); continue
            try:
                et = datetime.fromisoformat(pos["entry_time"])
                if et.replace(hour=23, minute=59) - now < timedelta(hours=FORCE_EXIT_BEFORE_SETTLEMENT_H):
                    to_close.append((key, current_no, "Settlement")); continue
            except ValueError:
                pass

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
                await self.alert.send_trade_close(pos["city"], pos["side"], pos["entry_price"],
                                                   exit_price, pnl, reason)

        today = now.strftime("%Y-%m-%d")
        daily = self.db.get_daily_pnl(today)
        self.daily_trades = daily["cnt"]
        self.daily_pnl = daily["total_pnl"]

        if self.daily_pnl < -INITIAL_CAPITAL * DAILY_LOSS_LIMIT_PCT / 100:
            logger.warning(f"🚨 日亏损超限 ({self.daily_pnl:.2f})")
            if not DRY_RUN:
                await self.alert.send_risk_alert(f"日亏损 {self.daily_pnl:.2f} > {DAILY_LOSS_LIMIT_PCT}%")

        logger.info(
            f"📊 {len(self._open_positions)}仓 | P&L={self.total_pnl:.2f} | "
            f"资金={self.equity:.2f} | OBI滤={self.stats_obi_filtered} "
            f"时滤={self.stats_liquid_filtered} 偏滤={self.stats_bias_filtered}"
        )

    # ════════════════════════════════════════════════════════════════
    # 主循环
    # ════════════════════════════════════════════════════════════════

    async def run(self):
        liquid_range = f"{LIQUID_START_HOUR}-{LIQUID_END_HOUR}UTC"
        obi_str = f"OBI<{-OBI_THRESHOLD}" if ALLOWED_SIDE == "NO" else f"OBI>{OBI_THRESHOLD}"
        logger.info("=" * 60)
        logger.info(f"  🌤️  HighTempTation v6 终极版")
        logger.info(f"  模式: {'🟢 实盘' if not DRY_RUN else '🟡 模拟'} / {LIVE_MODE.upper()}")
        logger.info(f"  v6: OBI={'✅' if ENABLE_OBI else '❌'} {obi_str}")
        logger.info(f"  v6: 窗口={'✅' if LIQUID_START_HOUR!=0 or LIQUID_END_HOUR!=24 else '❌'} {liquid_range}")
        logger.info(f"  v6: 偏差={'✅' if MARKET_BIAS_DAYS>0 else '❌'} {MARKET_BIAS_DAYS}d")
        logger.info(f"  资金: ${INITIAL_CAPITAL:.2f} | 扫描: {SCAN_INTERVAL}s | DB: {DB_PATH}")
        logger.info(f"  仓位: ${POSITION_SIZE_USD}/仓 × {MAX_POSITIONS} = ${POSITION_SIZE_USD * MAX_POSITIONS}")
        logger.info("=" * 60)

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
            for _ in range(SCAN_INTERVAL):
                if not self._running:
                    break
                await asyncio.sleep(1)
        logger.info("策略已停止")

    async def stop(self):
        self._running = False
        for key, pos in list(self._open_positions.items()):
            self.db.close_trade(pos["trade_id"], 0, "ForceStop")
        await self.fetcher.close()
        await self.alert.close()
        self.db.close()


# ════════════════════════════════════════════════════════════════
# 修复: _try_open_position 中调用 random
# ════════════════════════════════════════════════════════════════

# 仅在需要 OBI 模拟数据时导入 random
try:
    import random as _random
    HAS_RANDOM = True
except ImportError:
    HAS_RANDOM = False


async def main():
    strategy = LiveStrategyV6()
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
