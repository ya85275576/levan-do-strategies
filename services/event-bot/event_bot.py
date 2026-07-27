#!/usr/bin/env python3
"""
OKX 事件合約（Event Contracts）自動交易機器人

獨立於 LE VAN DO 永續 Bot 運行，專注於事件合約的二元期權交易。

策略：短期價格動量（跟隨底層資產的短期趨勢）
標的：BTC 5分鐘事件合約、ETH 5分鐘事件合約、SOL 5分鐘事件合約
金額：動態比例（按帳戶餘額百分比）

事件合約特點：
  - 二元結果，每份 1 USDT，價格區間 0.001–0.999
  - 無槓桿、無爆倉、全額保證金
  - 到期按結果結算（漲/跌/觸達/區間）

運行模式:
  DRY_RUN=true  (預設) — 模擬模式，不下單僅記錄
  DRY_RUN=false        — 實盤模式

啟動:
  python event_bot.py
  或使用 PM2（見 ecosystem.config.cjs）
"""
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config
from market_data import EventMarketData
from strategy import MomentumStrategy, Signal, SignalResult
from executor import EventContractExecutor


# ---- 日誌配置 ----
def setup_logging(level: str = "INFO"):
    log_format = "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

logger = logging.getLogger("event-bot")


# ================================================================
# 事件合約系列映射（seriesId → 底層資產交易對）
# ================================================================

def get_underlying_for_series(series_id: str) -> Optional[str]:
    """獲取事件系列對應的底層資產"""
    mapping = {
        "BTC-UPDOWN-5MIN": "BTC-USDT",
        "BTC-UPDOWN-15MIN": "BTC-USDT",
        "BTC-BETWEEN-DAILY": "BTC-USDT",
        "BTC-ABOVE-DAILY": "BTC-USDT",
        "BTC-HIT-DAILY": "BTC-USDT",
        "BTC-HIT-MONTHLY": "BTC-USDT",
        "ETH-UPDOWN-5MIN": "ETH-USDT",
        "ETH-UPDOWN-15MIN": "ETH-USDT",
        "ETH-ABOVE-DAILY": "ETH-USDT",
        "ETH-BETWEEN-DAILY": "ETH-USDT",
        "ETH-HIT-DAILY": "ETH-USDT",
        "ETH-HIT-MONTHLY": "ETH-USDT",
        "SOL-UPDOWN-5MIN": "SOL-USDT",
        "SOL-UPDOWN-15MIN": "SOL-USDT",
        "SOL-ABOVE-DAILY": "SOL-USDT",
    }
    return mapping.get(series_id)


def get_series_method(series_id: str) -> str:
    """獲取事件系列的方法（price_up_down / between / hit / price_above）"""
    if "UPDOWN" in series_id:
        return "price_up_down"
    elif "HIT" in series_id:
        return "hit"
    elif "ABOVE" in series_id:
        return "price_above"
    elif "BETWEEN" in series_id:
        return "between"
    return "unknown"


# ================================================================
# 持倉管理
# ================================================================

class EventPosition:
    """事件合約持倉"""

    def __init__(self, inst_id: str, series_id: str, underlying: str,
                 side: str, outcome: str, size: int, entry_price: float,
                 floor_strike: float, exp_time: str):
        self.inst_id = inst_id
        self.series_id = series_id
        self.underlying = underlying
        self.side = side          # buy / sell
        self.outcome = outcome    # UP / DOWN
        self.size = size          # 合約數量
        self.entry_price = entry_price  # 買入均價
        self.floor_strike = floor_strike  # 參考價（定價時底層價格）
        self.exp_time = exp_time  # 到期時間
        self.entry_time = datetime.now(timezone.utc).isoformat()
        self.settled = False
        self.settle_outcome = None  # 結算結果
        self.pnl = 0.0           # 已實現盈虧
        self.exit_price = 0.0    # 結算價格（1 或 0）

    def to_dict(self) -> dict:
        return {
            "inst_id": self.inst_id,
            "series_id": self.series_id,
            "underlying": self.underlying,
            "side": self.side,
            "outcome": self.outcome,
            "size": self.size,
            "entry_price": self.entry_price,
            "cost": round(self.entry_price * self.size, 2),
            "floor_strike": self.floor_strike,
            "exp_time": self.exp_time,
            "entry_time": self.entry_time,
            "settled": self.settled,
            "settle_outcome": self.settle_outcome,
            "pnl": round(self.pnl, 2),
            "exit_price": self.exit_price,
            "roi_pct": round((self.pnl / (self.entry_price * self.size)) * 100, 2) if self.entry_price > 0 and self.size > 0 else 0.0,
        }


# ================================================================
# 風控模塊
# ================================================================

class RiskController:
    """風控模塊 — 每日虧損上限、連續虧損暫停"""

    def __init__(self, config: dict):
        self.daily_loss_limit = config["daily_loss_limit"]
        self.consecutive_loss_limit = config["consecutive_loss_limit"]
        self.max_concurrent = config["max_concurrent_positions"]

        # 每日統計
        self._today = datetime.now(timezone.utc).date()
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._consecutive_losses = 0
        self._last_trade_pnl = 0.0

    def check(self) -> tuple:
        """
        檢查風控狀態

        :returns: (allowed: bool, reason: str)
        """
        now = datetime.now(timezone.utc).date()
        if now != self._today:
            self._reset_daily()

        if self._daily_pnl <= -self.daily_loss_limit:
            return False, f"每日虧損已達上限 ({self._daily_pnl:.2f} / {self.daily_loss_limit:.2f})"

        if self._consecutive_losses >= self.consecutive_loss_limit:
            return False, f"連續虧損 {self._consecutive_losses} 次，已暫停"

        return True, ""

    def record_trade(self, pnl: float):
        """記錄交易結果"""
        now = datetime.now(timezone.utc).date()
        if now != self._today:
            self._reset_daily()

        self._daily_pnl += pnl
        self._daily_trades += 1
        self._last_trade_pnl = pnl

        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

    def _reset_daily(self):
        """重置每日統計"""
        logger.info("📅 新的一天，重置每日風控統計")
        self._today = datetime.now(timezone.utc).date()
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._consecutive_losses = 0
        self._last_trade_pnl = 0.0

    def get_summary(self) -> dict:
        """獲取風控摘要"""
        return {
            "today": str(self._today),
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_trades": self._daily_trades,
            "consecutive_losses": self._consecutive_losses,
            "daily_loss_limit": self.daily_loss_limit,
            "consecutive_loss_limit": self.consecutive_loss_limit,
        }


# ================================================================
# 事件合約機器人主類
# ================================================================

class EventBot:
    """
    OKX 事件合約自動交易機器人

    管理多個事件系列的策略執行、持倉跟蹤和風控。
    """

    def __init__(self, config: dict):
        self.config = config
        self.series_ids = config["trading_series"]  # 要交易的系列

        # 市場數據（共用）
        self.market_data = EventMarketData(profile=config["okx_profile"])

        # 執行器
        self.executor = EventContractExecutor(
            dry_run=config["dry_run"],
            profile=config["okx_profile"],
        )

        # 策略（每個系列獨立實例，但共用參數）
        self.strategy = MomentumStrategy(
            lookback=config["momentum_lookback"],
            threshold_pct=config["momentum_threshold_pct"],
        )

        # 風控
        self.risk = RiskController(config)

        # 持倉管理
        self.positions: Dict[str, EventPosition] = {}  # inst_id → EventPosition
        self.closed_positions: List[EventPosition] = []

        # 已交易過的事件集合（避免重複下單）
        self._traded_event_ids: set = set()

        # 統計
        self.total_trades = 0
        self.win_count = 0
        self.loss_count = 0
        self.total_pnl = 0.0
        self.start_time: Optional[float] = None

        # 運行控制
        self._running = False

        # 狀態檔案
        self._status_file = config["status_file"]
        self._closed_file = config["closed_trades_file"]

        # 帳戶餘額快取
        self._usdt_balance: float = config.get("initial_capital", 500.0)

    # ---- 核心邏輯 ----

    async def _process_series(self, series_id: str):
        """
        處理一個事件合約系列：
          1. 獲取活躍合約
          2. 找到最新可交易的合約
          3. 判定是否需要交易
          4. 執行交易
        """
        try:
            # 獲取底層資產
            underlying = get_underlying_for_series(series_id)
            if not underlying:
                logger.debug(f"系列 {series_id} 無對應底層資產，跳過")
                return

            # 獲取活躍市場
            markets = await self.market_data.get_markets(series_id, state="live")
            if not markets:
                logger.debug(f"系列 {series_id} 無活躍合約")
                return

            # 找最新的合約（最接近到期的「live」合約）
            # 過濾掉 floorStrike=0 的（還沒設定定價）
            valid = [
                m for m in markets
                if m.get("floorStrike") and float(m.get("floorStrike", 0)) > 0
                and m.get("state") == "live"
                and m.get("instId") not in self._traded_event_ids
            ]
            if not valid:
                # 試試 floorStrike=0 但已經 live 的合約
                pre_fix = [
                    m for m in markets
                    if m.get("state") == "live"
                    and m.get("instId") not in self._traded_event_ids
                ]
                if pre_fix:
                    logger.debug(f"系列 {series_id} 有 {len(pre_fix)} 個未定價合約等待")
                return

            # 按到期時間排序，取最新的
            # instId 格式: BTC-UPDOWN-5MIN-260727-1635-1640
            # 到期時間從 instId 或 expTime 取得
            valid.sort(key=lambda m: m.get("expTime", ""), reverse=True)
            target = valid[0]

            inst_id = target["instId"]
            floor_strike = float(target["floorStrike"])
            exp_time = target.get("expTime", "")

            logger.info(
                f"🎯 發現可交易合約: {inst_id} "
                f"(底層 {underlying}, 定價 {floor_strike:.2f})"
            )

            # 2. 獲取底層資產 K 線做動量分析
            candles = await self.market_data.get_underlying_candles(
                underlying, bar="1m", limit=self.config["momentum_lookback"] + 2
            )
            if not candles:
                logger.warning(f"無法獲取 {underlying} K 線數據，跳過")
                return

            # 3. 策略分析
            signal = self.strategy.analyze(candles)
            logger.info(
                f"📊 策略: {underlying} → {signal.signal.value} "
                f"(動量 {signal.momentum_pct:.3f}%, 信心 {signal.confidence:.2f})"
            )

            if signal.signal == Signal.NONE:
                return

            # 4. 獲取事件合約行情（買賣價）
            ticker = await self.market_data.get_ticker(inst_id)
            if not ticker:
                logger.warning(f"無法獲取 {inst_id} 行情，跳過")
                return

            ask_price = float(ticker.get("askPx", 0))
            bid_price = float(ticker.get("bidPx", 0))
            last_price = float(ticker.get("last", 0)) if ticker.get("last") else 0

            if ask_price <= 0:
                logger.warning(f"{inst_id} 無有效賣價，跳過")
                return

            # 決定交易方向
            # 信號 UP → 買入 UP outcome（支付 ask_price）
            # 信號 DOWN → 買入 DOWN outcome（買 DOWN 的價格 = 1 - bid_price，或直接 ask for down）
            if signal.signal == Signal.UP:
                outcome = "UP"
                side = "buy"
                buy_price = ask_price
                logger.info(
                    f"📈 信號 UP: {underlying} {signal.momentum_pct:.3f}% "
                    f"→ 買入 UP @ {buy_price:.3f}"
                )
            else:
                # 信號 DOWN：賣出 UP（等同於買入 DOWN）
                outcome = "UP"
                side = "sell"
                buy_price = bid_price
                logger.info(
                    f"📉 信號 DOWN: {underlying} {signal.momentum_pct:.3f}% "
                    f"→ 賣出 UP @ {buy_price:.3f}（等同買入 DOWN）"
                )

            # 5. 檢查是否可以交易
            if not self.strategy.should_trade(
                signal, buy_price, self.config["max_buy_price"]
            ):
                return

            # 6. 風控檢查
            allowed, reason = self.risk.check()
            if not allowed:
                logger.warning(f"⛔ 風控阻止交易: {reason}")
                return

            # 7. 計算數量
            balance = await self._get_balance()
            trade_value = balance * (self.config["trade_qty_pct"] / 100.0)
            size = int(trade_value / buy_price) if buy_price > 0 else 0
            size = max(self.config["min_position_size"], size)
            size = min(self.config["max_position_size"], size)

            if size < self.config["min_position_size"]:
                logger.info(f"跳過交易：計算數量 {size} < 最小 {self.config['min_position_size']}")
                return

            # 8. 下單
            logger.info(
                f"🚀 下單: {inst_id} {side} {outcome} {size}份 @ {buy_price:.3f} "
                f"(資金 ${trade_value:.2f}, 帳戶 ${balance:.2f})"
            )

            if not self.config["dry_run"]:
                result = await self.executor.place_order(
                    inst_id=inst_id,
                    side=side,
                    outcome=outcome,
                    size=size,
                    price=buy_price,
                )
                if result.get("status") != "success":
                    logger.error(f"❌ 下單失敗: {result}")
                    return
            else:
                logger.info(f"[DRY] 模擬下單成功: {inst_id} {side} {outcome} {size}份")

            # 9. 記錄持倉
            position = EventPosition(
                inst_id=inst_id,
                series_id=series_id,
                underlying=underlying,
                side=side,
                outcome=outcome,
                size=size,
                entry_price=buy_price,
                floor_strike=floor_strike,
                exp_time=exp_time,
            )
            self.positions[inst_id] = position
            self._traded_event_ids.add(inst_id)
            self.total_trades += 1

            logger.info(
                f"✅ 持倉已建立: {outcome} {size}份 @ {buy_price:.3f} "
                f"(成本 ${buy_price * size:.2f})"
            )

        except Exception as e:
            logger.error(f"處理系列 {series_id} 時發生異常: {e}", exc_info=True)

    async def _check_settlements(self):
        """
        檢查持倉結算情況

        遍歷當前持倉，查詢事件狀態，如果已結算則記錄盈虧。
        """
        if not self.positions:
            return

        to_remove = []
        for inst_id, pos in self.positions.items():
            try:
                # 查詢事件詳細資訊
                # 從 series 查 events 看該事件是否已結算
                events = await self.market_data.get_markets(pos.series_id, state="expired")
                expired = [e for e in events if e.get("instId") == inst_id]
                if expired:
                    event = expired[0]
                    outcome = event.get("outcome", "")
                    settle_value = event.get("settleValue", "")

                    if outcome and outcome != "pending":
                        # 已結算
                        pos.settled = True
                        pos.settle_outcome = outcome

                        # 計算盈虧（buy/sell 方向不同）
                        if pos.side == "buy":
                            # 買入 UP：UP 贏 → 賺
                            win = (outcome == pos.outcome)
                            if win:
                                pos.pnl = pos.size - (pos.entry_price * pos.size)
                                pos.exit_price = 1.0
                                self.win_count += 1
                            else:
                                pos.pnl = -(pos.entry_price * pos.size)
                                pos.exit_price = 0.0
                                self.loss_count += 1
                        else:
                            # 賣出 UP：UP 輸 → 賺（等於買入 DOWN）
                            win = (outcome != pos.outcome)
                            if win:
                                pos.pnl = pos.entry_price * pos.size
                                pos.exit_price = 1.0
                                self.win_count += 1
                            else:
                                pos.pnl = -(1.0 - pos.entry_price) * pos.size
                                pos.exit_price = 0.0
                                self.loss_count += 1

                        self.total_pnl += pos.pnl
                        self.risk.record_trade(pos.pnl)

                        logger.info(
                            f"💰 結算: {inst_id} "
                            f"結果={outcome} (我們={pos.outcome}) "
                            f"{'✅ 贏' if win else '❌ 輸'} "
                            f"盈虧=${pos.pnl:.2f}"
                        )

                        self.closed_positions.append(pos)
                        to_remove.append(inst_id)

                # 如果已過期很久但還沒結算，查詢 orders
                # 也可以透過 orders 或 fills 確認
                if not expired:
                    # 檢查是否已過期超過 1 小時
                    try:
                        exp_dt = datetime.fromisoformat(
                            pos.exp_time.replace(" UTC+8", "+08:00")
                            .replace(" UTC", "+00:00")
                        )
                        now = datetime.now(timezone.utc)
                        if now > exp_dt + timedelta(hours=1):
                            # 超過 1 小時未結算，嘗試查詢訂單狀態
                            logger.info(f"查詢 {inst_id} 訂單狀態...")
                            fills = await self.executor.get_fills()
                            related = [f for f in fills if f.get("instId") == inst_id]
                            if related:
                                # 有成交記錄但可能未結算，先保留
                                pass
                            else:
                                # 可能沒成交，移除
                                logger.warning(f"⚠️ {inst_id} 過期但無數據，移除")
                                to_remove.append(inst_id)
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"檢查結算 {inst_id} 時異常: {e}")

        # 清理已結算持倉
        for inst_id in to_remove:
            self.positions.pop(inst_id, None)

    async def _get_balance(self) -> float:
        """獲取 USDT 帳戶餘額（從 okx CLI）"""
        try:
            cmd = [
                "okx", "account", "balance", "USDT",
                "--json",
                "--profile", self.config["okx_profile"],
            ]
            import shlex
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            if stdout_str:
                data = json.loads(stdout_str)
                if isinstance(data, list) and len(data) > 0:
                    details = data[0].get("details", [])
                    for d in details:
                        if d.get("ccy") == "USDT":
                            eq = float(d.get("eq", 0))
                            if eq > 0:
                                self._usdt_balance = eq
                            return self._usdt_balance
        except Exception as e:
            logger.debug(f"獲取帳戶餘額失敗: {e}")

        return self._usdt_balance

    # ---- 狀態持久化 ----

    def _write_status_file(self):
        """寫入狀態檔案供儀表板讀取"""
        try:
            positions_list = [p.to_dict() for p in self.positions.values()]
            closed_list = [p.to_dict() for p in self.closed_positions[-50:]]

            status = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "running": self._running,
                "uptime_seconds": int(time.time() - (self.start_time or time.time())),
                "dry_run": self.config["dry_run"],
                "total_trades": self.total_trades,
                "win_count": self.win_count,
                "loss_count": self.loss_count,
                "total_pnl": round(self.total_pnl, 2),
                "win_rate": round(
                    (self.win_count / (self.win_count + self.loss_count) * 100), 1
                ) if (self.win_count + self.loss_count) > 0 else 0.0,
                "usdt_balance": round(self._usdt_balance, 2),
                "open_positions": len(self.positions),
                "positions": positions_list,
                "closed_positions": closed_list,
                "risk": self.risk.get_summary(),
                "config": {
                    "series": self.series_ids,
                    "dry_run": self.config["dry_run"],
                    "trade_qty_pct": self.config["trade_qty_pct"],
                    "max_position_size": self.config["max_position_size"],
                    "daily_loss_limit": self.config["daily_loss_limit"],
                    "momentum_lookback": self.config["momentum_lookback"],
                    "momentum_threshold_pct": self.config["momentum_threshold_pct"],
                },
            }

            tmp = self._status_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(status, f, ensure_ascii=False)
            os.replace(tmp, self._status_file)

            # 持久化已平倉記錄
            try:
                closed_trades = [p.to_dict() for p in self.closed_positions[-200:]]
                with open(self._closed_file + ".tmp", "w") as f:
                    json.dump(closed_trades, f, ensure_ascii=False)
                os.replace(self._closed_file + ".tmp", self._closed_file)
            except Exception as e:
                logger.error(f"寫入已平倉檔案失敗: {e}")

        except Exception as e:
            logger.error(f"寫入狀態檔案失敗: {e}")

    # ---- 主循環 ----

    async def run(self):
        """啟動事件合約機器人主循環"""
        self._running = True
        self.start_time = time.time()

        # 啟動橫幅
        logger.info("╔══════════════════════════════════════════════╗")
        logger.info("║  OKX 事件合約自動交易機器人                   ║")
        logger.info("╠══════════════════════════════════════════════╣")
        logger.info(f"║  系列: {', '.join(self.series_ids)}")
        logger.info(f"║  模擬模式: {'🟢 啟用' if self.config['dry_run'] else '🔴 關閉'}")
        logger.info(f"║  交易比例: {self.config['trade_qty_pct']}%")
        logger.info(f"║  最大數量: {self.config['max_position_size']} 份")
        logger.info(f"║  動量回看: {self.config['momentum_lookback']} 根 K 線")
        logger.info(f"║  動量閾值: {self.config['momentum_threshold_pct']}%")
        logger.info(f"║  輪詢間隔: {self.config['poll_interval']} 秒")
        logger.info("╚══════════════════════════════════════════════╝")

        # 立即寫入狀態
        self._write_status_file()

        # 負載統計
        loop_count = 0

        while self._running:
            try:
                loop_count += 1
                logger.debug(f"🔄 輪詢 #{loop_count}")

                # 1. 檢查結算
                await self._check_settlements()

                # 2. 處理每個系列
                for series_id in self.series_ids:
                    await self._process_series(series_id)

                # 3. 寫入狀態
                if loop_count % 3 == 0:
                    self._write_status_file()

                # 4. 等待下一次輪詢
                await asyncio.sleep(self.config["poll_interval"])

            except asyncio.CancelledError:
                logger.info("收到取消信號")
                break
            except Exception as e:
                logger.error(f"主循環異常: {e}", exc_info=True)
                await asyncio.sleep(self.config["poll_interval"])

        # 寫入最終狀態
        self._write_status_file()

        # 輸出統計
        elapsed = time.time() - (self.start_time or time.time())
        logger.info("╔══════════════════════════════════════════════╗")
        logger.info("║  運行統計                                    ║")
        logger.info("╠══════════════════════════════════════════════╣")
        logger.info(f"║  運行時間: {elapsed:.0f}s")
        logger.info(f"║  總交易數: {self.total_trades}")
        logger.info(f"║  勝/負:    {self.win_count}/{self.loss_count}")
        logger.info(f"║  勝率:     {self.risk.get_summary()['daily_pnl']:.1f}%")
        logger.info(f"║  總盈虧:   ${self.total_pnl:.2f}")
        logger.info("╚══════════════════════════════════════════════╝")

    async def stop(self):
        """停止機器人"""
        self._running = False
        self._write_status_file()

    def get_status(self) -> dict:
        """獲取機器人狀態摘要"""
        return {
            "running": self._running,
            "series": self.series_ids,
            "dry_run": self.config["dry_run"],
            "total_trades": self.total_trades,
            "open_positions": len(self.positions),
            "total_pnl": round(self.total_pnl, 2),
        }


# ================================================================
# 入口
# ================================================================

async def main():
    """主入口"""
    config = load_config()
    setup_logging(config["log_level"])

    logger.info(
        f"🚀 OKX 事件合約機器人啟動 "
        f"(dry_run={config['dry_run']}, "
        f"series={len(config['trading_series'])} 個系列)"
    )

    bot = EventBot(config)

    # 註冊信號處理
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(bot.stop()))
        except NotImplementedError:
            pass

    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
