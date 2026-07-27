"""
Polymarket YES+NO=$1 互补套利策略引擎

遵循 LE VAN DO® 策略引擎设计模式：
  - ArbitrageStrategyParams  → 策略参数（对应 StrategyParams）
  - ArbitrageSignalType      → 信号类型枚举（对应 SignalType）
  - ArbitrageStrategy        → 策略引擎（对应 LeVanDoStrategy）

设计原则：
  - 纯逻辑层，与输出方式解耦
  - 输入市场数据，输出套利信号
  - 状态由 StrategyState 管理
"""
import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from config import load_config
from polymarket_api import (
    ArbitrageOpportunity,
    MarketInfo,
    PolymarketClient,
)

logger = logging.getLogger("polymarket.strategy")


# ================================================================
# 信号类型
# ================================================================

class ArbitrageSignalType(Enum):
    """套利信号类型（对应 SignalType）"""
    NONE = "none"                     # 无套利机会
    ARBITRAGE_BUY = "arbitrage_buy"   # 买入 YES + NO
    MONITORING = "monitoring"         # 监控中


# ================================================================
# 策略参数（对应 StrategyParams）
# ================================================================

@dataclass
class ArbitrageStrategyParams:
    """互补套利策略参数"""
    # 套利触发阈值：YES+NO < threshold 时触发信号
    threshold: float = 0.98

    # 每次模拟交易数量（USDC 面值）
    trade_size: float = 100.0

    # 最低流动性过滤（USDC）
    min_liquidity_usdc: float = 100.0

    # API 翻页数（每页 100 个市场）
    max_pages: int = 5

    # 价格过滤
    min_yes_price: float = 0.02
    max_yes_price: float = 0.98
    min_no_price: float = 0.02
    max_no_price: float = 0.98

    # CLOB API URL
    clob_api_url: str = "https://clob.polymarket.com"

    # 扫描间隔（秒）
    scan_interval_sec: int = 60


# ================================================================
# 策略状态（对应 StrategyState）
# ================================================================

@dataclass
class ArbitrageStrategyState:
    """策略状态机"""
    # 扫描轮次
    scan_rounds: int = 0

    # 累计发现机会数
    total_opportunities_found: int = 0

    # 当前轮次的机会
    current_opportunities: List[ArbitrageOpportunity] = field(default_factory=list)

    # 最后信号
    last_signal: ArbitrageSignalType = ArbitrageSignalType.NONE

    # 已知市场集合（去重用）
    known_markets: Dict[str, float] = field(default_factory=dict)

    # 模拟交易记录
    simulated_trades: List[dict] = field(default_factory=list)

    # 最新价格快照
    latest_snapshot: dict = field(default_factory=dict)


# ================================================================
# 策略引擎（对应 LeVanDoStrategy）
# ================================================================

class ArbitrageStrategy:
    """
    YES+NO=$1 互补套利策略引擎

    使用方式:
      1. 创建 ArbitrageStrategy 实例
      2. 调用 scan_markets() 执行一次扫描
      3. 通过 on_signal 回调接收套利信号
      4. 调用 get_status() 获取状态
    """

    def __init__(
        self,
        params: Optional[ArbitrageStrategyParams] = None,
        on_signal: Optional[Callable[[ArbitrageSignalType, ArbitrageOpportunity], None]] = None,
    ):
        """
        :param params: 策略参数
        :param on_signal: 套利信号回调 (signal, opportunity)
        """
        self.params = params or ArbitrageStrategyParams()
        self.on_signal = on_signal

        # API 客户端
        self.client = PolymarketClient(
            clob_api_url=self.params.clob_api_url,
        )

        # 状态
        self.state = ArbitrageStrategyState()

        logger.info(
            f"📈 套利策略引擎初始化: "
            f"threshold={self.params.threshold}, "
            f"min_liquidity={self.params.min_liquidity_usdc}, "
            f"max_pages={self.params.max_pages}"
        )

    # ================================================================
    # 核心扫描逻辑
    # ================================================================

    async def scan_markets(
        self,
        dry_run: bool = True,
    ) -> List[ArbitrageOpportunity]:
        """
        执行一次完整的市场扫描。

        流程:
          1. 通过 API 获取所有活跃二元市场
          2. 查询每个市场的 YES 和 NO 报价
          3. 计算 YES+NO 总成本
          4. 当成本低于阈值时生成套利信号
          5. 去重并记录

        Args:
            dry_run: 模拟模式（仅记录，不下单）

        Returns:
            List[ArbitrageOpportunity]: 本轮发现的套利机会
        """
        self.state.scan_rounds += 1
        round_num = self.state.scan_rounds

        logger.info(f"🔍 [{round_num}] 开始扫描活跃市场...")

        # 构建价格过滤器
        price_filters = {
            "min_yes_price": self.params.min_yes_price,
            "max_yes_price": self.params.max_yes_price,
            "min_no_price": self.params.min_no_price,
            "max_no_price": self.params.max_no_price,
        }

        # 执行扫描
        all_opportunities = await self.client.scan_arbitrage_opportunities(
            threshold=self.params.threshold,
            min_liquidity=self.params.min_liquidity_usdc,
            max_pages=self.params.max_pages,
            price_filters=price_filters,
        )

        # 去重：只保留新出现的机会
        new_opportunities = []
        for opp in all_opportunities:
            cid = opp.market.condition_id
            if cid not in self.state.known_markets:
                new_opportunities.append(opp)
                self.state.known_markets[cid] = opp.cost

        # 更新状态
        self.state.total_opportunities_found += len(new_opportunities)
        self.state.current_opportunities = all_opportunities

        # 模拟交易
        if dry_run and new_opportunities:
            self._simulate_trades(new_opportunities)

        # 生成信号
        if new_opportunities:
            self.state.last_signal = ArbitrageSignalType.ARBITRAGE_BUY

            # 回调
            if self.on_signal:
                for opp in new_opportunities:
                    self.on_signal(ArbitrageSignalType.ARBITRAGE_BUY, opp)

            logger.info(
                f"🚨 [{round_num}] 套利信号: {len(new_opportunities)} 个机会, "
                f"最佳利润: {max(o.profit_pct for o in new_opportunities):.2f}%"
            )
        else:
            self.state.last_signal = ArbitrageSignalType.NONE
            logger.info(f"✅ [{round_num}] 无套利机会")

        return new_opportunities

    # ================================================================
    # 模拟交易
    # ================================================================

    def _simulate_trades(self, opportunities: List[ArbitrageOpportunity]):
        """
        模拟套利交易。

        计算：
          - 买入 YES 数量 = trade_size / yes_price
          - 买入 NO 数量 = trade_size / no_price
          - 到期收入 = min(trade_size, min(yes_depth, no_depth))
          - 利润 = 到期收入 - 成本

        遵循 Pine Script 模拟模式设计。
        """
        for opp in opportunities:
            trade_size = self.params.trade_size
            max_possible = min(opp.yes_depth, opp.no_depth)
            actual_size = min(trade_size, max_possible)

            # 买入 YES 和 NO 各 actual_size 股
            cost_yes = actual_size * opp.yes_ask
            cost_no = actual_size * opp.no_ask
            total_cost = cost_yes + cost_no
            settlement = actual_size * 1.0  # 到期拿到 $1/股
            profit = settlement - total_cost

            trade_record = {
                "round": self.state.scan_rounds,
                "condition_id": opp.market.condition_id,
                "question": opp.market.question,
                "slug": opp.market.slug,
                "yes_price": round(opp.yes_ask, 4),
                "no_price": round(opp.no_ask, 4),
                "total_cost_per_share": round(opp.cost, 4),
                "profit_per_share_pct": round(opp.profit_pct, 2),
                "trade_size_shares": round(actual_size, 2),
                "cost_yes_usdc": round(cost_yes, 2),
                "cost_no_usdc": round(cost_no, 2),
                "total_cost_usdc": round(total_cost, 2),
                "settlement_usdc": round(settlement, 2),
                "profit_usdc": round(profit, 2),
                "yes_depth": round(opp.yes_depth, 2),
                "no_depth": round(opp.no_depth, 2),
                "simulated": True,
            }
            self.state.simulated_trades.append(trade_record)

            logger.info(
                f"💵 模拟交易: '{opp.market.question[:40]}...' "
                f"买入 {actual_size:.2f} 股 "
                f"(YES=$${cost_yes:.2f} + NO=$${cost_no:.2f} = ${total_cost:.2f}) "
                f"→ 到期 ${settlement:.2f}, 利润 ${profit:.2f} ({opp.profit_pct:.2f}%)"
            )

    # ================================================================
    # 状态查询
    # ================================================================

    def get_status(self) -> dict:
        """获取策略状态摘要"""
        all_opps = self.state.current_opportunities
        if all_opps:
            best = max(all_opps, key=lambda o: o.profit_pct)
            best_profit = round(best.profit_pct, 2)
            best_question = best.market.question[:60]
        else:
            best_profit = 0.0
            best_question = ""

        return {
            "scan_rounds": self.state.scan_rounds,
            "total_opportunities_found": self.state.total_opportunities_found,
            "current_opportunities": len(all_opps),
            "known_markets": len(self.state.known_markets),
            "last_signal": self.state.last_signal.value,
            "best_opportunity": {
                "profit_pct": best_profit,
                "question": best_question,
            },
            "simulated_trades_count": len(self.state.simulated_trades),
            "total_simulated_profit": round(
                sum(t["profit_usdc"] for t in self.state.simulated_trades), 2
            ),
            "params": {
                "threshold": self.params.threshold,
                "trade_size": self.params.trade_size,
                "min_liquidity_usdc": self.params.min_liquidity_usdc,
                "max_pages": self.params.max_pages,
            },
        }

    # ================================================================
    # 重置
    # ================================================================

    def reset(self):
        """重置策略状态"""
        self.state = ArbitrageStrategyState()
        logger.info("🔄 套利策略状态已重置")

    # ================================================================
    # 资源释放
    # ================================================================

    async def close(self):
        """释放 API 客户端资源"""
        await self.client.close()
        logger.info("[策略] 资源已释放")
