"""
Polymarket YES+NO=$1 互补套利扫描器

核心业务逻辑：
  1. 定时调用 API 扫描所有活跃市场
  2. 计算 YES + NO 买入成本
  3. 当成本低于阈值时记录套利机会
  4. 维护机会状态（新出现 / 已消失 / 已确认）
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

logger = logging.getLogger("polymarket.scanner")


@dataclass
class ScannerState:
    """
    扫描器状态

    维护扫描历史，用于增量检测和去重。
    """
    # 已发现的机会集合（按 condition_id 索引）
    known_opportunities: Dict[str, dict] = field(default_factory=dict)

    # 总扫描轮次
    scan_rounds: int = 0

    # 累计发现的机会数（去重）
    total_opportunities_found: int = 0

    # 上次扫描时间
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
    YES+NO=$1 互补套利扫描器

    定时扫描 Polymarket 市场，发现套利机会并记录。

    用法:
        scanner = ArbitrageScanner()
        await scanner.run_once()          # 执行一次扫描
        await scanner.run_forever()       # 持续循环扫描
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config or load_config()

        # API 客户端
        self.client = PolymarketClient(
            clob_api_url=self.config["clob_api_url"],
        )

        # 状态
        self.state = ScannerState()
        self._load_state()

        # 机会记录
        self.opportunities: List[ArbitrageOpportunity] = []

        # 运行标志
        self._running = False

    # ---- 状态持久化 ----

    def _load_state(self):
        """从文件加载运行状态"""
        state_file = self.config["state_file"]
        try:
            if os.path.exists(state_file):
                with open(state_file, "r") as f:
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
        state_file = self.config["state_file"]
        try:
            tmp = state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state.to_dict(), f, ensure_ascii=False)
            os.replace(tmp, state_file)
        except Exception as e:
            logger.error(f"[状态] 保存失败: {e}")

    def _save_opportunities(self, new_opps: List[ArbitrageOpportunity]):
        """追加新的套利机会记录"""
        try:
            # 读取已有记录
            existing = []
            if os.path.exists(self.config["opportunities_file"]):
                with open(self.config["opportunities_file"], "r") as f:
                    existing = json.load(f)

            # 追加新机会
            for opp in new_opps:
                existing.append(self._opportunity_to_dict(opp))

            # 只保留最近 1000 条
            if len(existing) > 1000:
                existing = existing[-1000:]

            tmp = self.config["opportunities_file"] + ".tmp"
            with open(tmp, "w") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.config["opportunities_file"])

        except Exception as e:
            logger.error(f"[记录] 保存机会失败: {e}")

    def _opportunity_to_dict(self, opp: ArbitrageOpportunity) -> dict:
        """将套利机会转为可序列化字典"""
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

    # ---- 核心扫描 ----

    async def scan_once(self) -> List[ArbitrageOpportunity]:
        """
        执行一次完整的市场扫描。

        流程：
          1. 获取所有活跃二元市场
          2. 查询每个市场的 YES 和 NO 报价
          3. 计算 YES+NO 成本，找出低于阈值的
          4. 去重：跳过已发现的机会
          5. 更新状态

        Returns:
            List[ArbitrageOpportunity]: 本轮新发现的套利机会
        """
        logger.info("=" * 60)
        logger.info(f"🔍 扫描轮次 #{self.state.scan_rounds + 1}")
        logger.info("=" * 60)

        # 执行扫描
        price_filters = {
            "min_yes_price": self.config["min_yes_price"],
            "max_yes_price": self.config["max_yes_price"],
            "min_no_price": self.config["min_no_price"],
            "max_no_price": self.config["max_no_price"],
        }

        all_opportunities = await self.client.scan_arbitrage_opportunities(
            threshold=self.config["arbitrage_threshold"],
            min_liquidity=self.config["min_liquidity_usdc"],
            max_pages=self.config["max_pages"],
            price_filters=price_filters,
        )

        # 去重：只保留新出现的机会
        new_opportunities = []
        for opp in all_opportunities:
            cid = opp.market.condition_id
            if cid not in self.state.known_opportunities:
                new_opportunities.append(opp)
                self.state.known_opportunities[cid] = {
                    "first_seen": time.time(),
                    "slug": opp.market.slug,
                    "question": opp.market.question,
                }

        # 更新状态
        self.state.scan_rounds += 1
        self.state.total_opportunities_found += len(new_opportunities)
        self.state.last_scan_time = time.time()

        # 保存状态和记录
        self._save_state()
        if new_opportunities:
            self._save_opportunities(new_opportunities)

        # 保存到内存
        self.opportunities = all_opportunities

        # 输出报告
        self._print_report(all_opportunities, new_opportunities)

        return new_opportunities

    def _print_report(
        self,
        all_opps: List[ArbitrageOpportunity],
        new_opps: List[ArbitrageOpportunity],
    ):
        """打印扫描报告到控制台"""
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
        """
        持续循环扫描。

        每次扫描后等待 config['scan_interval_sec'] 秒。
        """
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

            # 等待下一次扫描
            if self._running:
                logger.info(f"⏰ 等待 {interval} 秒后下次扫描...")
                await asyncio.sleep(interval)

        logger.info("🛑 套利扫描器已停止")

    def stop(self):
        """停止扫描"""
        self._running = False

    async def close(self):
        """释放资源"""
        self._save_state()
        await self.client.close()
        logger.info("[扫描器] 资源已释放")



