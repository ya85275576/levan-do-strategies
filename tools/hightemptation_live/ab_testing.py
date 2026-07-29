#!/usr/bin/env python3
"""
HighTempTation — 多策略 A/B 测试框架

功能:
  1. 多实例并行: 同时跑多个 variant，各自独立参数
  2. 优胜劣汰: 定期比较各 variant 表现，停用落后、推广领先
  3. 参数配置: 每个 variant 可独立配置 edge/TP/SL/仓位等参数
  4. 结果记录: 每笔交易关联到 variant，自动汇总统计

配置格式 (JSON):
  {
    "test_name": "edge_threshold_test",
    "variants": [
      {
        "name": "control",
        "params": {"MIN_EDGE": 0.20, "TP_PCT": 9.0, "SL_PCT": 6.5, ...}
      },
      {
        "name": "v2_tight_edge",
        "params": {"MIN_EDGE": 0.25, "TP_PCT": 8.0, "SL_PCT": 5.5, ...}
      }
    ],
    "eval_interval_hours": 24,
    "elimination_rule": "worst_by_pnl"       // 淘汰规则
  }

用法:
  # 命令行
  python ab_testing.py start --config ab_config.json
  python ab_testing.py status                          # 查看运行状态
  python ab_testing.py eval                            # 手动评估
  python ab_testing.py stop --variant v2_tight_edge    # 停用某个 variant

  # 编程接口
  from ab_testing import ABTestingFramework
  framework = ABTestingFramework(db, alert, config_path="ab_config.json")
  await framework.run_all()

流程:
  1. 启动时注册所有 variant 到 DB
  2. 对每个 variant 创建独立的 LiveStrategyV6 实例（参数覆盖）
  3. 所有实例并行扫描，共享同一 DB
  4. 每 eval_interval_hours 比较各 variant 的 PnL/胜率
  5. 表现最差的 variant 自动停用
"""
import argparse
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_manager import TradeDB
from data_fetcher_advanced import DataFetcher, STATIONS
from alert_manager import AlertManager
from hightemptation_live_v6 import LiveStrategyV6, bucket_prob, _gaussian_cdf
from health_check import HealthChecker

logger = logging.getLogger("ab_testing")

# ── 默认配置 ──
DEFAULT_EVAL_INTERVAL_HOURS = 24
DEFAULT_SCAN_INTERVAL = 120  # AB 测试扫描间隔稍长（2 分钟）
AB_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ab_configs")

# ── 默认 variant 模板参数（与 v6 一致）──
V6_DEFAULT_PARAMS = {
    "MIN_EDGE": 0.20,
    "TP_PCT": 9.0,
    "SL_PCT": 6.5,
    "TRAILING_ACTIVATE": 5.0,
    "TRAILING_DRAWDOWN": 3.0,
    "PRICE_LOW": 0.28,
    "PRICE_HIGH": 0.72,
    "MIN_PROB_EDGE": 0.12,
    "ALLOWED_SIDE": "NO",
    "POSITION_SIZE_USD": 1.0,
    "MAX_POSITIONS": 50,
    "COMMISSION_PCT": 0.02,
    "MIN_DEPTH": 200,
    "MAX_IMPACT_RATIO": 0.2,
    "MIN_HOURS_TO_EXPIRY": 6,
    "FORCE_EXIT_BEFORE_SETTLEMENT_H": 4,
    "DAILY_LOSS_LIMIT_PCT": 5.0,
    "ENABLE_OBI": True,
    "OBI_THRESHOLD": 0.2,
    "LIQUID_START_HOUR": 12,
    "LIQUID_END_HOUR": 20,
    "MARKET_BIAS_DAYS": 30,
}


class ABVariantRunner:
    """
    单个 A/B variant 的运行器。

    包装 LiveStrategyV6，用 variant 的 params 覆盖默认参数。
    """

    def __init__(self, test_name: str, variant_name: str, params: dict,
                 db: TradeDB, alert: AlertManager,
                 scan_interval: int = DEFAULT_SCAN_INTERVAL):
        self.test_name = test_name
        self.variant_name = variant_name
        self.params = {**V6_DEFAULT_PARAMS, **params}  # 合并参数
        self.db = db
        self.alert = alert
        self.scan_interval = scan_interval

        # 创建策略实例
        self.strategy = LiveStrategyV6()
        # 覆盖参数
        self._apply_params()

        self._running = False
        self._trade_ids: List[int] = []  # 该 variant 开出的交易 ID

    def _apply_params(self):
        """将 params 应用到策略实例的类变量"""
        for k, v in self.params.items():
            if hasattr(self.strategy.__class__, k):
                setattr(self.strategy.__class__, k, v)
            elif hasattr(self.strategy, k):
                setattr(self.strategy, k, v)

        # 特殊处理: POSITION_SIZE_USD 影响开仓
        self._position_size = self.params.get("POSITION_SIZE_USD", 1.0)

    async def _custom_scan_and_trade(self):
        """
        改造版 _scan_and_trade，在开仓时记录 variant 关联。
        """
        # 复用原版逻辑，但在开仓后记录 AB trade mapping
        orig_open_trade = self.db.open_trade

        def patched_open_trade(token_id, city, bl, bu, side, price, size):
            trade_id = orig_open_trade(token_id, city, bl, bu, side, price, size)
            if trade_id:
                self._trade_ids.append(trade_id)
                self.db.record_ab_trade(self.test_name, self.variant_name, trade_id)
            return trade_id

        # 临时替换
        self.db.open_trade = patched_open_trade

        try:
            # 调用原版扫描逻辑
            await self.strategy._scan_and_trade()
        finally:
            self.db.open_trade = orig_open_trade

    async def run_one_cycle(self):
        """执行一次扫描 + 交易"""
        try:
            await self._custom_scan_and_trade()
        except Exception as e:
            logger.error(f"[{self.variant_name}] 扫描异常: {e}", exc_info=True)

    def get_stats(self) -> dict:
        """获取该 variant 的当前统计数据"""
        from_db = self.db.conn.execute("""
            SELECT total_pnl, total_trades, win_count, loss_count
            FROM ab_tests
            WHERE test_name=? AND variant_name=?
        """, (self.test_name, self.variant_name)).fetchone()

        if not from_db:
            return {
                "variant_name": self.variant_name,
                "total_pnl": 0.0,
                "total_trades": 0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate": 0.0,
            }

        trades = from_db["total_trades"] or 0
        wins = from_db["win_count"] or 0
        return {
            "variant_name": self.variant_name,
            "total_pnl": round(from_db["total_pnl"] or 0.0, 2),
            "total_trades": trades,
            "win_count": wins,
            "loss_count": from_db["loss_count"] or 0,
            "win_rate": round(wins / trades * 100, 1) if trades > 0 else 0.0,
        }

    def is_active(self) -> bool:
        """检查该 variant 是否仍在运行"""
        row = self.db.conn.execute(
            "SELECT is_active FROM ab_tests WHERE test_name=? AND variant_name=?",
            (self.test_name, self.variant_name),
        ).fetchone()
        return row and row["is_active"] == 1


class ABTestingFramework:
    """
    A/B 测试主框架。

    流程:
      1. 初始化: 从配置文件加载所有 variant
      2. 注册: 将 variant 注册到 DB
      3. 运行: 并行执行所有 active variant
      4. 评估: 定期比较各 variant 表现
      5. 淘汰: 停用表现最差的 variant
    """

    def __init__(self, db: TradeDB, alert: AlertManager,
                 config_path: Optional[str] = None,
                 config: Optional[dict] = None):
        self.db = db
        self.alert = alert
        self.config = config or {}
        self.config_path = config_path

        # 从文件加载配置
        if config_path and not config:
            self._load_config(config_path)

        self.test_name = self.config.get("test_name", "unnamed_test")
        self.eval_interval_hours = self.config.get("eval_interval_hours",
                                                     DEFAULT_EVAL_INTERVAL_HOURS)
        self.elimination_rule = self.config.get("elimination_rule", "worst_by_pnl")

        # variant 运行器
        self._runners: Dict[str, ABVariantRunner] = {}
        self._last_eval_time = 0.0
        self._running = False

        # 健康检查器
        self._health_checker = HealthChecker(db, alert)

    def _load_config(self, config_path: str):
        """从 JSON 文件加载配置"""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"AB 测试配置不存在: {config_path}")

        with open(config_path) as f:
            self.config = json.load(f)

        logger.info(f"📋 AB 测试配置加载: {config_path}")
        logger.info(f"  测试: {self.config.get('test_name', '?')}")
        logger.info(f"  Variants: {len(self.config.get('variants', []))} 个")
        logger.info(f"  评估间隔: {self.config.get('eval_interval_hours', 24)}h")

    # ════════════════════════════════════════════════════════════════
    # 注册/初始化
    # ════════════════════════════════════════════════════════════════

    def initialize(self):
        """注册所有 variant 到 DB"""
        variants = self.config.get("variants", [])
        if not variants:
            logger.warning("无 variant 配置")
            return

        for v in variants:
            name = v.get("name", "unnamed")
            params = v.get("params", {})
            self.db.register_ab_test(self.test_name, name, params)
            logger.info(f"  📌 注册 variant: {name} ({len(params)} 参数)")

        # 创建运行器
        self._runners = {}
        for v in variants:
            name = v.get("name", "unnamed")
            params = v.get("params", {})
            runner = ABVariantRunner(
                test_name=self.test_name,
                variant_name=name,
                params=params,
                db=self.db,
                alert=self.alert,
                scan_interval=self.config.get("scan_interval", DEFAULT_SCAN_INTERVAL),
            )
            self._runners[name] = runner

        logger.info(f"✅ {len(self._runners)} 个 variant 就绪")

    # ════════════════════════════════════════════════════════════════
    # 运行循环
    # ════════════════════════════════════════════════════════════════

    async def run_all(self):
        """并行运行所有 active variant"""
        logger.info("=" * 60)
        logger.info(f"  🧪 A/B 测试开始: {self.test_name}")
        logger.info(f"  Variants: {list(self._runners.keys())}")
        logger.info(f"  评估间隔: {self.eval_interval_hours}h")
        logger.info(f"  淘汰规则: {self.elimination_rule}")
        logger.info("=" * 60)

        # 初始采集预报
        fetcher = DataFetcher(self.db)
        try:
            await fetcher.fetch_all_cities(forecast_days=7)
        except Exception as e:
            logger.warning(f"预报采集失败: {e}")
        await fetcher.close()

        self._running = True
        cycle_count = 0

        while self._running:
            cycle_count += 1
            now = datetime.now(timezone.utc)
            active_variants = [n for n, r in self._runners.items()
                               if r.is_active()]

            if not active_variants:
                logger.warning("⚠️ 所有 variant 已停用，停止 AB 测试")
                break

            logger.info(f"🔄 循环 #{cycle_count} | "
                        f"{len(active_variants)}/{len(self._runners)} active")

            # 并行执行所有 active variant
            tasks = []
            for name, runner in self._runners.items():
                if runner.is_active():
                    tasks.append(runner.run_one_cycle())

            if tasks:
                await asyncio.gather(*tasks)

            # 定期评估
            elapsed = time.time() - self._last_eval_time
            if elapsed > self.eval_interval_hours * 3600:
                await self._evaluate()
                self._last_eval_time = time.time()

            # 健康检查（每 10 个循环）
            if cycle_count % 10 == 0:
                await self._health_checker.run_all(send_alerts=True)

            # 状态日志
            self._log_status()

            # 等待
            await asyncio.sleep(self.config.get("scan_interval", DEFAULT_SCAN_INTERVAL))

        logger.info("AB 测试已停止")

    # ════════════════════════════════════════════════════════════════
    # 评估 + 淘汰
    # ════════════════════════════════════════════════════════════════

    async def _evaluate(self):
        """
        评估所有 variant，进行优胜劣汰。

        淘汰规则:
          - worst_by_pnl: 停用累计 PnL 最低的 variant
          - worst_by_win_rate: 停用胜率最低的 variant
          - worst_by_sharpe: 停用夏普率最低的 variant (暂用 PnL 替代)
        """
        logger.info("📊 评估所有 variant...")

        stats = []
        for name, runner in self._runners.items():
            s = runner.get_stats()
            stats.append(s)

        if len(stats) < 2:
            logger.info("  variant 不足 2 个，跳过评估")
            return

        # 排序
        if self.elimination_rule == "worst_by_pnl":
            stats.sort(key=lambda s: s["total_pnl"])
        elif self.elimination_rule == "worst_by_win_rate":
            stats.sort(key=lambda s: s["win_rate"])
        else:
            stats.sort(key=lambda s: s["total_pnl"])

        worst = stats[0]
        best = stats[-1]

        logger.info(f"  🥇 最佳: {best['variant_name']} (PnL={best['total_pnl']:+.2f}, "
                    f"{best['total_trades']}笔, 胜率{best['win_rate']:.1f}%)")
        logger.info(f"  🥉 最差: {worst['variant_name']} (PnL={worst['total_pnl']:+.2f}, "
                    f"{worst['total_trades']}笔, 胜率{worst['win_rate']:.1f}%)")

        # 发送评估报告
        await self.alert.send_ab_test_result(self.test_name, stats)

        # 淘汰最差的（但有最少交易数保护）
        min_trades_for_elimination = self.config.get("min_trades_for_elimination", 5)
        if worst["total_trades"] >= min_trades_for_elimination:
            logger.warning(f"  🗑️ 淘汰 {worst['variant_name']}")
            self.db.deactivate_ab_test(self.test_name, worst["variant_name"])
            await self.alert.send(
                f"🗑️ AB 测试淘汰\n"
                f"测试: {self.test_name}\n"
                f"淘汰: {worst['variant_name']}\n"
                f"原因: {self.elimination_rule} (PnL={worst['total_pnl']:+.2f})"
            )

        # 如果只剩 1 个 variant，推广为胜者
        active = [s for s in stats if self._runners[s["variant_name"]].is_active()]
        if len(active) == 1:
            winner = active[0]
            logger.info(f"🏆 唯一胜者: {winner['variant_name']}, AB 测试完成!")
            await self.alert.send(
                f"🏆 AB 测试完成!\n"
                f"测试: {self.test_name}\n"
                f"胜者: {winner['variant_name']}\n"
                f"PnL: {winner['total_pnl']:+.2f} | 胜率: {winner['win_rate']:.1f}%"
            )

    def _log_status(self):
        """记录当前状态"""
        for name, runner in self._runners.items():
            if runner.is_active():
                s = runner.get_stats()
                logger.info(f"  [{name}] PnL={s['total_pnl']:+.2f} "
                            f"交易={s['total_trades']} 胜率={s['win_rate']:.1f}%")

    async def stop(self):
        self._running = False
        for runner in self._runners.values():
            await runner.strategy.stop()


# ════════════════════════════════════════════════════════════════
# 命令行入口
# ════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="HighTempTation A/B 测试框架")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # start
    start_parser = subparsers.add_parser("start", help="启动 AB 测试")
    start_parser.add_argument("--config", type=str, required=True,
                              help="AB 测试配置文件路径")
    start_parser.add_argument("--db", type=str, default="hightemptation.db")

    # status
    status_parser = subparsers.add_parser("status", help="查看 AB 测试状态")
    status_parser.add_argument("--test", type=str, default="",
                               help="测试名称（可选）")
    status_parser.add_argument("--db", type=str, default="hightemptation.db")

    # eval
    eval_parser = subparsers.add_parser("eval", help="手动评估 AB 测试")
    eval_parser.add_argument("--test", type=str, default="",
                             help="测试名称（可选）")
    eval_parser.add_argument("--db", type=str, default="hightemptation.db")

    # stop
    stop_parser = subparsers.add_parser("stop", help="停用 variant")
    stop_parser.add_argument("--test", type=str, required=True, help="测试名称")
    stop_parser.add_argument("--variant", type=str, required=True, help="variant 名称")
    stop_parser.add_argument("--db", type=str, default="hightemptation.db")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    db = TradeDB(args.db)
    alert = AlertManager()

    if args.command == "start":
        if not os.path.exists(args.config):
            print(f"❌ 配置文件不存在: {args.config}")
            return

        # 创建 AB configs 目录
        os.makedirs(AB_CONFIG_DIR, exist_ok=True)

        framework = ABTestingFramework(db, alert, config_path=args.config)
        framework.initialize()
        await framework.run_all()

    elif args.command == "status":
        if args.test:
            results = db.get_ab_test_results(test_name=args.test)
        else:
            results = db.get_ab_test_results()

        if not results:
            print("📭 无 AB 测试记录")
            return

        print(f"\n{'='*70}")
        print(f"  AB 测试状态 ({len(results)} records)")
        print(f"{'='*70}")
        print(f"  {'测试':20s} {'Variant':20s} {'PnL':>10s} {'交易':>6s} "
              f"{'胜率':>8s} {'状态':>8s}")
        print(f"  {'─'*72}")
        for r in results:
            active = "🟢" if r.get("is_active") else "⏹️"
            trades = r.get("total_trades", 0)
            wins = r.get("win_count", 0)
            wr = f"{wins/trades*100:.1f}%" if trades > 0 else "-"
            print(f"  {r['test_name'][:18]:20s} {r['variant_name'][:18]:20s} "
                  f"{r['total_pnl']:>+9.2f} {trades:>5d} {wr:>8s} {active:>8s}")

    elif args.command == "eval":
        # 手动评估: 重新计算所有 variant 的统计
        if args.test:
            results = db.get_ab_test_results(test_name=args.test)
        else:
            results = db.get_ab_test_results()

        if not results:
            print("📭 无 AB 测试记录")
            return

        results.sort(key=lambda r: r.get("total_pnl", 0), reverse=True)
        print(f"\n{'='*70}")
        print(f"  AB 测试评估 ({len(results)} variants)")
        print(f"{'='*70}")
        for i, r in enumerate(results):
            trades = r.get("total_trades", 0)
            wins = r.get("win_count", 0)
            losses = r.get("loss_count", 0)
            wr = (wins / trades * 100) if trades > 0 else 0
            medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"  {i+1}."
            print(f"  {medal} {r['variant_name']:20s} "
                  f"PnL={r['total_pnl']:+.2f} | "
                  f"{trades}笔 | 胜率{wr:.1f}% | "
                  f"{wins}胜 {losses}负")

    elif args.command == "stop":
        print(f"⏹️ 停用 {args.test}/{args.variant}...")
        ok = db.deactivate_ab_test(args.test, args.variant)
        if ok:
            msg = f"✅ 已停用 {args.test}/{args.variant}"
            print(msg)
            await alert.send(f"⏹️ AB 测试停用\n测试: {args.test}\nVariant: {args.variant}")
        else:
            print(f"❌ 停用失败，检查测试名/variant 名是否正确")

    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
