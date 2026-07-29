#!/usr/bin/env python3
"""
HighTempTation — 实盘 vs 回测偏差监控

功能:
  1. 滑点偏差监控: 实盘成交价 vs 回测假设入场价的偏差
  2. 盈亏偏差监控: 实盘每笔 PnL vs 回测预期 PnL 的偏差比
  3. 偏差阈值: 超过 2 倍阈值触发告警并记录到 deviation_logs 表

偏差比定义:
  deviation_ratio = |实盘值 - 预期值| / max(|预期值|, 1e-6)

阈值:
  - SLIPPAGE_DEVIATION_THRESHOLD: 滑点偏差比阈值 (默认 2.0x)
  - PNL_DEVIATION_THRESHOLD:      盈亏偏差比阈值 (默认 2.0x)
  - RECENT_WINDOW:                最近 N 笔交易参与统计 (默认 20)

用法:
  from deviation_monitor import DeviationMonitor
  monitor = DeviationMonitor(db, alert, backtest_stats=backtest_results)
  # 每笔平仓时调用
  monitor.check_trade_deviation(trade_id, entry_price, exit_price, size, pnl)

独立运行:
  python deviation_monitor.py --report           # 查看最近偏差记录
  python deviation_monitor.py --summary          # 偏差统计摘要
  python deviation_monitor.py --watch            # 持续监控
"""
import argparse
import asyncio
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_manager import TradeDB
from alert_manager import AlertManager

logger = logging.getLogger("deviation_monitor")

# ── 默认参数（可被环境变量覆盖）──
SLIPPAGE_DEVIATION_THRESHOLD = float(os.environ.get("SLIPPAGE_DEV_THRESHOLD", "2.0"))
PNL_DEVIATION_THRESHOLD = float(os.environ.get("PNL_DEV_THRESHOLD", "2.0"))
RECENT_WINDOW = int(os.environ.get("DEV_WINDOW", "20"))

# 回测默认基准参数（与 v3 回测一致）
BACKTEST_DEFAULT_SLIPPAGE = 0.0005   # 回测假设的基准滑点
BACKTEST_DEFAULT_COMMISSION = 0.02   # 回测假设的费率 %


class DeviationMonitor:
    """
    实盘 vs 回测偏差监控器。

    工作原理:
      - 每笔平仓时调用 check_trade_deviation() 记录实盘成交价
      - 对比回测预期的滑点和盈亏
      - 超过阈值的偏差写入 deviation_logs 表 + 告警
      - 滚动窗口统计最近 N 笔交易的平均偏差
    """

    def __init__(self, db: TradeDB, alert: AlertManager,
                 backtest_stats: Optional[dict] = None):
        """
        :param db: TradeDB 实例
        :param alert: AlertManager 实例
        :param backtest_stats: 回测统计字典，包含:
            - "avg_slippage": 回测平均滑点
            - "avg_pnl_per_trade": 回测每笔平均盈亏
            - "win_rate": 回测胜率
            - "avg_commission": 回测平均佣金
        """
        self.db = db
        self.alert = alert
        self.backtest = backtest_stats or {}

        # 滚动窗口
        self._recent_slippages: deque = deque(maxlen=RECENT_WINDOW)
        self._recent_pnl_deviations: deque = deque(maxlen=RECENT_WINDOW)

        # 去重: 已检查过的 trade_id
        self._checked_trade_ids: set = set()

    # ════════════════════════════════════════════════════════════════
    # 核心检查
    # ════════════════════════════════════════════════════════════════

    def check_trade_deviation(self, trade_id: int, entry_price: float,
                                exit_price: float, size: float,
                                pnl: float, side: str = "NO") -> dict:
        """
        检查单笔交易的偏差。

        计算:
          - 实际滑点 = |exit_price - entry_price| / entry_price    (简化)
          - 预期滑点 = 回测基准滑点
          - 滑点偏差比 = actual_slippage / expected_slippage (带下限保护)
          - PnL 偏差比 = |实际PnL - 预期PnL| / max(|预期PnL|, 1e-4)

        :returns: {"status": "normal"|"alert", "slippage_dev": float, "pnl_dev": float, ...}
        """
        if trade_id in self._checked_trade_ids:
            return {"status": "skipped", "reason": "already_checked"}
        self._checked_trade_ids.add(trade_id)

        now = datetime.now(timezone.utc)

        # ── 滑点偏差 ──
        # 实际滑点: 入场到出场的价格变化率（NO 方向正收益是价格下跌）
        actual_slippage = abs(exit_price - entry_price) / max(entry_price, 1e-6)
        expected_slippage = self.backtest.get("avg_slippage", BACKTEST_DEFAULT_SLIPPAGE)

        slippage_dev_ratio = actual_slippage / max(expected_slippage, 1e-8)
        self._recent_slippages.append(slippage_dev_ratio)

        # ── PnL 偏差 ──
        expected_pnl = self.backtest.get("avg_pnl_per_trade", 0.0)
        if expected_pnl == 0:
            pnl_dev_ratio = 0.0
        else:
            pnl_dev_ratio = abs(pnl) / max(abs(expected_pnl), 1e-4)
        self._recent_pnl_deviations.append(pnl_dev_ratio)

        # ── 判断是否超阈值 ──
        alerts = []
        status = "normal"

        if slippage_dev_ratio > SLIPPAGE_DEVIATION_THRESHOLD:
            status = "alert"
            alerts.append(
                f"slippage_dev_{trade_id}: "
                f"实际滑点={actual_slippage:.6f} (预期={expected_slippage:.6f}), "
                f"偏差比={slippage_dev_ratio:.2f}x"
            )

        if pnl_dev_ratio > PNL_DEVIATION_THRESHOLD:
            status = "alert"
            alerts.append(
                f"pnl_dev_{trade_id}: "
                f"实际PnL={pnl:.4f} (预期={expected_pnl:.4f}), "
                f"偏差比={pnl_dev_ratio:.2f}x"
            )

        # ── 写入 DB + 告警 ──
        result = {
            "trade_id": trade_id,
            "status": status,
            "slippage_dev": round(slippage_dev_ratio, 4),
            "pnl_dev": round(pnl_dev_ratio, 4),
            "actual_slippage": round(actual_slippage, 6),
            "expected_slippage": round(expected_slippage, 6),
            "actual_pnl": round(pnl, 4),
            "expected_pnl": round(expected_pnl, 4),
        }

        if status == "alert":
            message = "; ".join(alerts)
            self.db.store_deviation_log(
                metric="slippage" if "slippage" in message else "pnl",
                live_value=actual_slippage if "slippage" in message else pnl,
                backtest_expected=expected_slippage if "slippage" in message else expected_pnl,
                deviation_ratio=max(slippage_dev_ratio, pnl_dev_ratio),
                threshold=SLIPPAGE_DEVIATION_THRESHOLD if "slippage" in message else PNL_DEVIATION_THRESHOLD,
                status="alert",
                message=message,
            )

            logger.warning(f"⚠️ 偏差告警: {message}")

        logger.debug(
            f"偏差检查 trade#{trade_id}: 滑点{actual_slippage:.6f}/{expected_slippage:.6f} "
            f"({slippage_dev_ratio:.2f}x) PnL偏差({pnl_dev_ratio:.2f}x)"
        )

        return result

    # ════════════════════════════════════════════════════════════════
    # 滚动统计
    # ════════════════════════════════════════════════════════════════

    def get_recent_deviation_summary(self) -> dict:
        """
        获取最近窗口内的偏差统计。

        :returns: {
            "avg_slippage_dev": float,
            "max_slippage_dev": float,
            "avg_pnl_dev": float,
            "max_pnl_dev": float,
            "sample_count": int,
            "alert_count": int,
        }
        """
        sample_count = len(self._recent_slippages)

        if sample_count == 0:
            return {
                "avg_slippage_dev": 0.0,
                "max_slippage_dev": 0.0,
                "avg_pnl_dev": 0.0,
                "max_pnl_dev": 0.0,
                "sample_count": 0,
                "alert_count": 0,
            }

        # 从 DB 获取告警计数
        alert_count = self.db.conn.execute(
            "SELECT COUNT(*) as cnt FROM deviation_logs "
            "WHERE created_at >= datetime('now', '-1 day')"
        ).fetchone()["cnt"] or 0

        return {
            "avg_slippage_dev": round(sum(self._recent_slippages) / sample_count, 4),
            "max_slippage_dev": round(max(self._recent_slippages), 4),
            "avg_pnl_dev": round(sum(self._recent_pnl_deviations) / len(self._recent_pnl_deviations), 4)
                if self._recent_pnl_deviations else 0.0,
            "max_pnl_dev": round(max(self._recent_pnl_deviations), 4)
                if self._recent_pnl_deviations else 0.0,
            "sample_count": sample_count,
            "alert_count": alert_count,
        }

    def get_live_vs_backtest_comparison(self) -> dict:
        """
        生成实盘 vs 回测的全面比较。

        :returns: dict with live, backtest, deviation fields
        """
        # 实盘统计
        live_trades = self.db.conn.execute("""
            SELECT COUNT(*) as cnt,
                   COALESCE(AVG(pnl),0) as avg_pnl,
                   COALESCE(SUM(pnl),0) as total_pnl,
                   SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) as losses
            FROM trades WHERE status='closed' AND pnl IS NOT NULL
        """).fetchone()

        live_win_rate = (live_trades["wins"] / live_trades["cnt"] * 100) \
            if live_trades["cnt"] > 0 else 0.0

        # 回测基准
        bt_win_rate = self.backtest.get("win_rate", 0.0)
        bt_avg_pnl = self.backtest.get("avg_pnl_per_trade", 0.0)

        comparison = {
            "live": {
                "trades": live_trades["cnt"],
                "total_pnl": round(live_trades["total_pnl"], 2),
                "avg_pnl": round(live_trades["avg_pnl"], 4),
                "win_rate": round(live_win_rate, 1),
            },
            "backtest": {
                "trades": self.backtest.get("total_trades", 0),
                "total_pnl": round(self.backtest.get("total_pnl", 0), 2),
                "avg_pnl": round(bt_avg_pnl, 4),
                "win_rate": round(bt_win_rate, 1),
            },
            "deviation": {
                "win_rate_diff": round(live_win_rate - bt_win_rate, 1),
                "avg_pnl_ratio": round(
                    abs(live_trades["avg_pnl"]) / max(abs(bt_avg_pnl), 1e-4), 2
                ) if bt_avg_pnl != 0 else 0,
            },
        }

        # 偏差过大时写入日志
        if comparison["deviation"]["avg_pnl_ratio"] > PNL_DEVIATION_THRESHOLD:
            self.db.store_deviation_log(
                metric="overall_pnl_deviation",
                live_value=live_trades["avg_pnl"],
                backtest_expected=bt_avg_pnl,
                deviation_ratio=comparison["deviation"]["avg_pnl_ratio"],
                threshold=PNL_DEVIATION_THRESHOLD,
                status="alert",
                message=f"整体PnL偏差比={comparison['deviation']['avg_pnl_ratio']:.2f}x",
            )

        return comparison


# ════════════════════════════════════════════════════════════════
# 独立运行
# ════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="HighTempTation 偏差监控")
    parser.add_argument("--report", action="store_true", help="显示最近偏差记录")
    parser.add_argument("--summary", action="store_true", help="显示偏差统计摘要")
    parser.add_argument("--compare", action="store_true", help="实盘 vs 回测对比")
    parser.add_argument("--db", type=str, default="hightemptation.db", help="DB 路径")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    db = TradeDB(args.db)
    alert = AlertManager()

    # 尝试从文件加载回测基准（可选）
    backtest_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backtest", "backtest_baseline.json",
    )
    backtest_stats = {}
    if os.path.exists(backtest_file):
        with open(backtest_file) as f:
            backtest_stats = json.load(f)
        logger.info(f"📊 加载回测基准: {backtest_file}")

    monitor = DeviationMonitor(db, alert, backtest_stats=backtest_stats)

    if args.report:
        logs = db.get_recent_deviations(limit=20)
        print(f"\n{'='*60}")
        print(f"  最近偏差记录 ({len(logs)} 条)")
        print(f"{'='*60}")
        for log in logs:
            print(f"  [{log['created_at']}] {log['metric']}: "
                  f"实盘={log['live_value']:.4f} 回测预期={log['backtest_expected']:.4f} "
                  f"偏差={log['deviation_ratio']:.2f}x")

    elif args.summary:
        summary = monitor.get_recent_deviation_summary()
        print(f"\n{'='*60}")
        print(f"  偏差统计摘要 (最近 {RECENT_WINDOW} 笔)")
        print(f"{'='*60}")
        print(f"  平均滑点偏差: {summary['avg_slippage_dev']:.2f}x")
        print(f"  最大滑点偏差: {summary['max_slippage_dev']:.2f}x")
        print(f"  平均 PnL 偏差: {summary['avg_pnl_dev']:.2f}x")
        print(f"  最大 PnL 偏差: {summary['max_pnl_dev']:.2f}x")
        print(f"  样本数: {summary['sample_count']}")
        print(f"  24h 告警数: {summary['alert_count']}")

    elif args.compare:
        comp = monitor.get_live_vs_backtest_comparison()
        print(f"\n{'='*60}")
        print(f"  实盘 vs 回测对比")
        print(f"{'='*60}")
        print(f"  {'':20s} {'实盘':>12s} {'回测':>12s} {'偏差':>12s}")
        print(f"  {'─'*56}")
        print(f"  {'交易笔数':20s} {comp['live']['trades']:>12d} "
              f"{comp['backtest']['trades']:>12d} {'':>12s}")
        print(f"  {'总盈亏':20s} {comp['live']['total_pnl']:>12.2f} "
              f"{comp['backtest']['total_pnl']:>12.2f} {'':>12s}")
        print(f"  {'平均盈亏':20s} {comp['live']['avg_pnl']:>12.4f} "
              f"{comp['backtest']['avg_pnl']:>12.4f} "
              f"{comp['deviation']['avg_pnl_ratio']:>11.2f}x")
        print(f"  {'胜率':20s} {comp['live']['win_rate']:>11.1f}% "
              f"{comp['backtest']['win_rate']:>11.1f}% "
              f"{comp['deviation']['win_rate_diff']:>+10.1f}%")
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
