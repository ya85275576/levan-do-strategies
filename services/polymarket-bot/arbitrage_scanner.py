"""
Polymarket YES+NO=$1 互补套利扫描器（兼容包装层）

此模块是 strategy.py 的薄包装层，保持向后兼容。
核心逻辑已迁移到 ArbitrageStrategy（strategy.py）。
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from config import load_config
from polymarket_api import (
    ArbitrageOpportunity,
    MarketInfo,
    PolymarketClient,
)
from strategy import (
    ArbitrageSignalType,
    ArbitrageStrategy,
    ArbitrageStrategyParams,
)

logger = logging.getLogger("polymarket.scanner")


@dataclass
class ScannerState:
    """
    扫描器状态（兼容层）
    内部委托给 ArbitrageStrategy.state
    """
    known_opportunities: Dict[str, dict] = field(default_factory=dict)
    scan_rounds: int = 0
    total_opportunities_found: int = 0
    last_scan_time: float = 0.0

    def to_dict(self) -> dict:
        return {
            "known_opportunities": self.known_opportunities,
            "scan_rounds": self.scan_rounds,
            "total_opportunities_found": self.total_opportunities_found,
            "last_scan_time": self.last_scan_time,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScannerState":
        return cls(
            known_opportunities=data.get("known_opportunities", {}),
            scan_rounds=data.get("scan_rounds", 0),
            total_opportunities_found=data.get("total_opportunities_found", 0),
            last_scan_time=data.get("last_scan_time", 0.0),
        )


class ArbitrageScanner:
    """
    YES+NO=$1 互补套利扫描器（兼容包装层）

    内部使用 ArbitrageStrategy（strategy.py）执行核心逻辑。
    保持对旧代码的向后兼容。

    用法:
        scanner = ArbitrageScanner()
        await scanner.run_once()          # 执行一次扫描
        await scanner.run_forever()       # 持续循环扫描
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config or load_config()

        # 创建策略参数
        params = ArbitrageStrategyParams(
            threshold=self.config["arbitrage_threshold"],
            trade_size=self.config["trade_size"],
            min_liquidity_usdc=self.config["min_liquidity_usdc"],
            max_pages=self.config["max_pages"],
            min_yes_price=self.config["min_yes_price"],
            max_yes_price=self.config["max_yes_price"],
            min_no_price=self.config["min_no_price"],
            max_no_price=self.config["max_no_price"],
            clob_api_url=self.config["clob_api_url"],
            scan_interval_sec=self.config["scan_interval_sec"],
        )

        # 核心策略引擎
        self.strategy = ArbitrageStrategy(params=params)

        # 状态文件
        self._state_file = self.config["state_file"]
        self._opportunities_file = self.config["opportunities_file"]

        # 兼容层状态
        self.state = ScannerState()
        self._load_state()

        # 运行标志
        self._running = False

    # ---- 状态持久化（兼容层） ----

    def _load_state(self):
        """从文件加载运行状态"""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r") as f:
                    data = json.load(f)
                self.state = ScannerState.from_dict(data)
                logger.info(
                    f"[状态] 已加载: {self.state.scan_rounds} 轮, "
                    f"{self.state.total_opportunities_found} 个历史机会"
                )
        except Exception as e:
            logger.warning(f"[状态] 加载失败: {e}")

    def _save_state(self):
        """保存运行状态到文件"""
        try:
            tmp = self._state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state.to_dict(), f, ensure_ascii=False)
            os.replace(tmp, self._state_file)
        except Exception as e:
            logger.error(f"[状态] 保存失败: {e}")

    def _save_opportunities(self, new_opps: List[ArbitrageOpportunity]):
        """追加新的套利机会记录"""
        try:
            existing = []
            if os.path.exists(self._opportunities_file):
                with open(self._opportunities_file, "r") as f:
                    existing = json.load(f)

            for opp in new_opps:
                existing.append(self._opportunity_to_dict(opp))

            if len(existing) > 1000:
                existing = existing[-1000:]

            tmp = self._opportunities_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._opportunities_file)
        except Exception as e:
            logger.error(f"[记录] 保存机会失败: {e}")

    def _opportunity_to_dict(self, opp: ArbitrageOpportunity) -> dict:
        market = opp.market
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "condition_id": market.condition_id,
            "question": market.question,
            "slug": market.slug,
            "yes_price": round(opp.yes_ask, 4),
            "no_price": round(opp.no_ask, 4),
            "total_cost": round(opp.cost, 4),
            "profit_per_share": round(opp.profit_per_share, 4),
            "profit_pct": round(opp.profit_pct, 2),
            "yes_depth": round(opp.yes_depth, 2),
            "no_depth": round(opp.no_depth, 2),
            "max_trade_size": round(opp.max_trade_size, 2),
            "yes_token_id": market.yes_token_id,
            "no_token_id": market.no_token_id,
        }

    # ---- 核心扫描（委托给 strategy.py） ----

    async def scan_once(self) -> List[ArbitrageOpportunity]:
        """
        执行一次完整的市场扫描。

        委托给 ArbitrageStrategy.scan_markets()。
        """
        logger.info("=" * 60)
        logger.info(f"🔍 扫描轮次 #{self.state.scan_rounds + 1}")
        logger.info("=" * 60)

        # 委托给策略引擎
        new_opps = await self.strategy.scan_markets(
            dry_run=self.config["dry_run"],
        )

        # 更新兼容层状态
        self.state.scan_rounds = self.strategy.state.scan_rounds
        self.state.total_opportunities_found = self.strategy.state.total_opportunities_found
        self.state.last_scan_time = time.time()

        # 更新 known_opportunities（兼容层）
        for opp in new_opps:
            cid = opp.market.condition_id
            if cid not in self.state.known_opportunities:
                self.state.known_opportunities[cid] = {
                    "first_seen": time.time(),
                    "slug": opp.market.slug,
                    "question": opp.market.question,
                }

        # 持久化
        self._save_state()
        if new_opps:
            self._save_opportunities(new_opps)

        # 输出报告
        self._print_report(self.strategy.state.current_opportunities, new_opps)

        return new_opps

    def _print_report(
        self,
        all_opps: List[ArbitrageOpportunity],
        new_opps: List[ArbitrageOpportunity],
    ):
        """打印扫描报告"""
        logger.info("-" * 60)
        logger.info(f"📊 扫描报告")
        logger.info(f"   本轮发现: {len(all_opps)} 个机会 (新增: {len(new_opps)} 个)")
        logger.info(f"   累计机会: {self.state.total_opportunities_found}")
        logger.info(f"   已知市场: {len(self.state.known_opportunities)}")
        logger.info("-" * 60)

        if all_opps:
            logger.info(f"{'排名':>4} | {'问题':<50} | {'YES买入':>8} | {'NO买入':>8} | {'成本':>6} | {'利润%':>6}")
            logger.info("-" * 95)
            for i, opp in enumerate(all_opps[:10], 1):
                question = opp.market.question[:48]
                logger.info(
                    f"{i:>4} | {question:<50} "
                    f"| {opp.yes_ask:>8.4f} "
                    f"| {opp.no_ask:>8.4f} "
                    f"| {opp.cost:>6.4f} "
                    f"| {opp.profit_pct:>6.2f}%"
                )
            if len(all_opps) > 10:
                logger.info(f"   ... 还有 {len(all_opps) - 10} 个机会未显示")
        else:
            logger.info("   ❌ 当前无套利机会")
        logger.info("=" * 60)

    # ---- 持续运行 ----

    async def run_forever(self):
        """持续循环扫描"""
        self._running = True
        interval = self.config["scan_interval_sec"]

        logger.info(
            f"🟢 套利扫描器已启动 (间隔={interval}s, "
            f"阈值={self.config['arbitrage_threshold']}, "
            f"最低流动性={self.config['min_liquidity_usdc']} USDC)"
        )

        while self._running:
            try:
                await self.scan_once()
            except Exception as e:
                logger.error(f"[扫描] 执行异常: {e}", exc_info=True)

            if self._running:
                logger.info(f"⏰ 等待 {interval} 秒后下次扫描...")
                await asyncio.sleep(interval)

        logger.info("🛑 套利扫描器已停止")

    def stop(self):
        """停止扫描"""
        self._running = False
        self.strategy.state.last_signal = ArbitrageSignalType.NONE

    async def close(self):
        """释放资源"""
        self._save_state()
        await self.strategy.close()
        logger.info("[扫描器] 资源已释放")

    @property
    def opportunities(self) -> List[ArbitrageOpportunity]:
        """当前轮次的所有机会（兼容属性）"""
        return self.strategy.state.current_opportunities
