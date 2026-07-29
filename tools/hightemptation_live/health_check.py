#!/usr/bin/env python3
"""
HighTempTation — 策略健康度检查

三项例行检查:
  1. 数据新鲜度检测 (1h)      — market_prices / forecasts 表最近数据是否超过 1h
  2. 开平仓活跃度检测 (3h)    — 最近 3h 是否有开仓或平仓记录
  3. 数据库连接健康检测        — SQLite 连接是否正常

每次检查结果写入 health_checks 表，失败时通过 AlertManager 推送告警。

用法:
  from health_check import HealthChecker
  checker = HealthChecker(db, alert)
  results = await checker.run_all()
  # [{"check_name": "data_freshness", "status": "pass", ...}, ...]

独立运行:
  python health_check.py                      # 完整检查一次
  python health_check.py --watch              # 持续监控（默认 15 分钟间隔）
  python health_check.py --interval 300       # 自定义间隔（秒）
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_manager import TradeDB
from alert_manager import AlertManager

logger = logging.getLogger("health_check")

# 默认阈值（可被环境变量覆盖）
DATA_FRESHNESS_MAX_MINUTES = int(os.environ.get("HC_FRESHNESS_MINUTES", "60"))
TRADE_ACTIVITY_MIN_TRADES = int(os.environ.get("HC_MIN_TRADES_3H", "1"))
HEALTH_CHECK_INTERVAL = int(os.environ.get("HC_INTERVAL", "900"))  # 15 分钟

CHECK_NAMES = {
    "data_freshness": "数据新鲜度检测",
    "trade_activity": "开平仓活跃度检测",
    "db_connection": "数据库连接检测",
}


class HealthChecker:
    """
    策略健康度检查器。

    按周期执行三项例行检查，结果写入 DB 并按需告警。
    """

    def __init__(self, db: TradeDB, alert: AlertManager):
        self.db = db
        self.alert = alert
        self._last_alert_state: Dict[str, str] = {}  # check_name → last status

    # ════════════════════════════════════════════════════════════════
    # 1. 数据新鲜度检测 (1h)
    # ════════════════════════════════════════════════════════════════

    def _check_data_freshness(self) -> dict:
        """
        检查 market_prices 和 forecasts 表最近数据是否超过阈值。

        :returns: {"check_name": "data_freshness", "status": "pass"|"warn"|"fail",
                    "message": str, "detail": str}
        """
        now = datetime.now(timezone.utc)
        issues = []

        # 检查 market_prices 最新时间戳
        row = self.db.conn.execute(
            "SELECT MAX(ts) as max_ts FROM market_prices"
        ).fetchone()
        if row and row["max_ts"]:
            last_market_ts = row["max_ts"]
            # ts 是毫秒时间戳
            last_market_dt = datetime.fromtimestamp(last_market_ts / 1000, tz=timezone.utc)
            minutes_since = (now - last_market_dt).total_seconds() / 60
            if minutes_since > DATA_FRESHNESS_MAX_MINUTES:
                issues.append(
                    f"market_prices 最后更新于 {minutes_since:.0f} 分钟前 "
                    f"(阈值: {DATA_FRESHNESS_MAX_MINUTES}min)"
                )

        # 检查 forecasts 最新时间戳
        row = self.db.conn.execute(
            "SELECT MAX(created_at) as max_ts FROM forecasts"
        ).fetchone()
        if row and row["max_ts"]:
            last_fc_str = row["max_ts"]
            try:
                last_fc_dt = datetime.strptime(last_fc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                minutes_since = (now - last_fc_dt).total_seconds() / 60
                if minutes_since > DATA_FRESHNESS_MAX_MINUTES:
                    issues.append(
                        f"forecasts 最后更新于 {minutes_since:.0f} 分钟前 "
                        f"(阈值: {DATA_FRESHNESS_MAX_MINUTES}min)"
                    )
            except ValueError:
                issues.append(f"forecasts 日期解析失败: {last_fc_str}")

        if not issues:
            return {
                "check_name": "data_freshness",
                "status": "pass",
                "message": f"所有数据在 {DATA_FRESHNESS_MAX_MINUTES}min 内更新",
                "detail": "",
            }
        elif len(issues) <= 1:
            return {
                "check_name": "data_freshness",
                "status": "warn",
                "message": "; ".join(issues),
                "detail": json.dumps(issues),
            }
        else:
            return {
                "check_name": "data_freshness",
                "status": "fail",
                "message": "; ".join(issues),
                "detail": json.dumps(issues),
            }

    # ════════════════════════════════════════════════════════════════
    # 2. 开平仓活跃度检测 (3h)
    # ════════════════════════════════════════════════════════════════

    def _check_trade_activity(self) -> dict:
        """
        检查最近 3 小时内是否有开仓或平仓记录。

        :returns: {"check_name": "trade_activity", "status": ..., "message": ...}
        """
        now = datetime.now(timezone.utc)
        three_hours_ago = now - timedelta(hours=3)
        time_str = three_hours_ago.strftime("%Y-%m-%d %H:%M:%S")

        # 检查开仓
        open_count = self.db.conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE entry_time >= ?",
            (time_str,),
        ).fetchone()["cnt"] or 0

        # 检查平仓
        close_count = self.db.conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE exit_time >= ? AND status='closed'",
            (time_str,),
        ).fetchone()["cnt"] or 0

        total = open_count + close_count
        detail = json.dumps({"open_3h": open_count, "close_3h": close_count})

        if total >= TRADE_ACTIVITY_MIN_TRADES:
            return {
                "check_name": "trade_activity",
                "status": "pass",
                "message": f"最近3h: {open_count}开仓, {close_count}平仓 (需≥{TRADE_ACTIVITY_MIN_TRADES})",
                "detail": detail,
            }
        elif total > 0:
            return {
                "check_name": "trade_activity",
                "status": "warn",
                "message": f"最近3h仅{total}笔交易 (阈值:{TRADE_ACTIVITY_MIN_TRADES})",
                "detail": detail,
            }
        else:
            return {
                "check_name": "trade_activity",
                "status": "fail",
                "message": f"最近3h无任何开平仓，策略可能停滞",
                "detail": detail,
            }

    # ════════════════════════════════════════════════════════════════
    # 3. 数据库连接检测
    # ════════════════════════════════════════════════════════════════

    def _check_db_connection(self) -> dict:
        """
        检查 SQLite 连接和基本 I/O。

        :returns: {"check_name": "db_connection", "status": ..., "message": ...}
        """
        try:
            # 检查连接对象
            conn = self.db.conn
            if conn is None:
                return {
                    "check_name": "db_connection",
                    "status": "fail",
                    "message": "数据库连接对象为空",
                    "detail": "",
                }

            # 执行简单查询验证
            result = conn.execute("SELECT 1 as alive").fetchone()
            if result and result["alive"] == 1:
                # 检查 WAL 模式是否正常
                try:
                    conn.execute("PRAGMA integrity_check").fetchone()
                    return {
                        "check_name": "db_connection",
                        "status": "pass",
                        "message": f"DB连接正常 ({self.db.db_path})",
                        "detail": "",
                    }
                except Exception as e:
                    return {
                        "check_name": "db_connection",
                        "status": "warn",
                        "message": f"DB连接基本正常，但完整性检查异常: {e}",
                        "detail": str(e),
                    }
            else:
                return {
                    "check_name": "db_connection",
                    "status": "fail",
                    "message": "DB连接异常: 查询无返回",
                    "detail": "",
                }
        except Exception as e:
            return {
                "check_name": "db_connection",
                "status": "fail",
                "message": f"DB连接失败: {e}",
                "detail": str(e),
            }

    # ════════════════════════════════════════════════════════════════
    # 运行全部检查
    # ════════════════════════════════════════════════════════════════

    async def run_all(self, send_alerts: bool = True) -> List[dict]:
        """
        运行全部三项健康检查。

        :param send_alerts: 状态变化时是否发送告警
        :returns: [{"check_name": ..., "status": ..., "message": ...}, ...]
        """
        checks = [
            self._check_data_freshness(),
            self._check_trade_activity(),
            self._check_db_connection(),
        ]

        for check in checks:
            # 写入 DB
            self.db.store_health_check(
                check_name=check["check_name"],
                status=check["status"],
                message=check["message"],
                detail=check.get("detail", ""),
            )

            # 状态变化时告警
            if send_alerts:
                last_status = self._last_alert_state.get(check["check_name"], "")
                if check["status"] != last_status and check["status"] != "pass":
                    await self.alert.send_health_alert(
                        check_name=CHECK_NAMES.get(check["check_name"], check["check_name"]),
                        status=check["status"],
                        message=check["message"],
                    )
                self._last_alert_state[check["check_name"]] = check["status"]

        # 如果有任何 fail，发送汇总
        if send_alerts:
            fails = [c for c in checks if c["status"] == "fail"]
            if fails:
                await self.alert.send_health_summary(checks)

        # 日志
        for c in checks:
            emoji = {"pass": "✅", "warn": "⚠️", "fail": "🚨"}.get(c["status"], "ℹ️")
            logger.info(f"{emoji} {c['check_name']}: {c['status'].upper()} — {c['message']}")

        return checks

    async def run_loop(self, interval: int = HEALTH_CHECK_INTERVAL):
        """
        持续运行健康检查循环。

        :param interval: 检查间隔（秒）
        """
        logger.info(f"🔍 健康检查守护启动 (间隔 {interval}s)")
        while True:
            await self.run_all(send_alerts=True)
            await asyncio.sleep(interval)


# ════════════════════════════════════════════════════════════════
# 独立运行
# ════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="HighTempTation 策略健康检查")
    parser.add_argument("--watch", action="store_true", help="持续监控模式")
    parser.add_argument("--interval", type=int, default=HEALTH_CHECK_INTERVAL,
                        help=f"检查间隔（秒，默认 {HEALTH_CHECK_INTERVAL}）")
    parser.add_argument("--db", type=str, default="hightemptation.db",
                        help="DB 路径")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    db_path = args.db
    logger.info(f"📂 使用数据库: {db_path}")

    db = TradeDB(db_path)
    alert = AlertManager()
    checker = HealthChecker(db, alert)

    if args.watch:
        await checker.run_loop(interval=args.interval)
    else:
        await checker.run_all(send_alerts=True)
        logger.info("✅ 单次检查完成")


if __name__ == "__main__":
    asyncio.run(main())
