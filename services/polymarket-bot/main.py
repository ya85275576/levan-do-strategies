#!/usr/bin/env python3
"""
Polymarket YES+NO=$1 互补套利机器人 — 主程序

扫描 Polymarket 上所有活跃二元预测市场，
寻找 YES 和 NO 买入价之和低于 $1 的无风险套利机会。

运行模式:
  DRY_RUN=true   (默认) — 模拟模式，仅扫描和记录，不下单
  DRY_RUN=false         — 实盘模式，发现机会时执行套利交易

用法:
  python main.py                    # 一次扫描（默认）
  python main.py --once             # 一次扫描
  python main.py --loop             # 持续循环扫描
  python main.py --scan-and-notify  # 扫描 + Webhook 通知

环境变量:
  ARBITRAGE_THRESHOLD=0.98    # YES+NO < 0.98 触发
  SCAN_INTERVAL_SEC=60        # 扫描间隔（秒）
  MIN_LIQUIDITY_USDC=100      # 最低流动性（USDC）
  DRY_RUN=true                # 模拟模式
  ARBITRAGE_WEBHOOK_URL=...   # 通知 Webhook
"""
import argparse
import asyncio
import logging
import os
import signal
import sys

import aiohttp

# 确保可以导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config
from polymarket_api import ArbitrageOpportunity, PolymarketClient
from arbitrage_scanner import ArbitrageScanner
from reporter import (
    format_opportunity_text,
    format_summary_text,
    send_webhook_notification,
)


# ---- 日志配置 ----
def setup_logging(level: str = "INFO"):
    log_format = (
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s"
    )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )


logger = logging.getLogger("polymarket.main")


# ================================================================
# 信号处理
# ================================================================

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("⚠️ 强制退出...")
        sys.exit(1)
    _shutdown_requested = True
    logger.info(f"⏹️  收到信号 {signum}，正在优雅关闭...")


def register_signal_handlers():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


# ================================================================
# 子命令
# ================================================================

async def cmd_scan_once(config: dict):
    """执行一次扫描"""
    logger.info("🔍 执行一次扫描...")
    scanner = ArbitrageScanner(config)

    try:
        new_opps = await scanner.scan_once()

        # Webhook 通知（如有配置）
        webhook_url = config.get("webhook_url", "")
        if webhook_url and new_opps:
            logger.info(f"📨 发送 Webhook 通知 ({len(new_opps)} 个新机会)...")
            await send_webhook_notification(webhook_url, new_opps)

        return new_opps
    finally:
        await scanner.close()


async def cmd_scan_loop(config: dict):
    """持续循环扫描"""
    logger.info("🔄 持续扫描模式")
    register_signal_handlers()

    scanner = ArbitrageScanner(config)

    try:
        scan_task = asyncio.create_task(scanner.run_forever())

        # 等待直到收到关闭信号
        while not _shutdown_requested:
            await asyncio.sleep(1)

        scanner.stop()
        await scan_task
    finally:
        await scanner.close()


async def cmd_scan_and_notify(config: dict):
    """扫描并发送通知"""
    logger.info("📡 扫描 + 通知模式")
    scanner = ArbitrageScanner(config)

    try:
        new_opps = await scanner.scan_once()
        all_opps = scanner.opportunities

        # 通知
        webhook_url = config.get("webhook_url", "")
        if webhook_url:
            if new_opps:
                logger.info(f"📨 发送 {len(new_opps)} 条通知...")
                await send_webhook_notification(webhook_url, new_opps)
            else:
                # 发送摘要报告
                summary = format_summary_text(
                    total_rounds=scanner.state.scan_rounds,
                    total_opps=scanner.state.total_opportunities_found,
                    known_markets=len(scanner.state.known_opportunities),
                    new_opps_count=0,
                )
                logger.info(f"📨 发送摘要通知（无新机会）")
                async with aiohttp.ClientSession() as session:
                    try:
                        await session.post(
                            webhook_url,
                            json={"text": summary},
                            timeout=aiohttp.ClientTimeout(total=10),
                        )
                    except Exception as e:
                        logger.error(f"[Webhook] 发送摘要异常: {e}")
        else:
            logger.info("ℹ️ 未配置 Webhook URL，跳过通知")

        return new_opps
    finally:
        await scanner.close()


async def cmd_list_markets(config: dict):
    """列出所有活跃市场（不使用扫描器，直接调用 API）"""
    logger.info("📋 列出活跃市场...")
    client = PolymarketClient(clob_api_url=config["clob_api_url"])

    try:
        markets = await client.fetch_markets(
            closed=False, limit=100, max_pages=config["max_pages"]
        )
        logger.info(f"共 {len(markets)} 个活跃二元市场:")
        for i, m in enumerate(markets, 1):
            logger.info(f"  {i:>4}. {m.question[:70]}")
    finally:
        await client.close()


async def cmd_show_config(config: dict):
    """显示当前配置"""
    print("=" * 60)
    print("📋 Polymarket 互补套利机器人 — 配置")
    print("=" * 60)
    for key, value in sorted(config.items()):
        # 隐藏长的 token ID
        if isinstance(value, str) and len(value) > 40:
            value = value[:20] + "..." + value[-8:]
        print(f"  {key:30s} = {value}")
    print("=" * 60)


# ================================================================
# 入口
# ================================================================

def main():
    """主入口"""
    parser = argparse.ArgumentParser(
        description="Polymarket YES+NO=$1 互补套利机器人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                    # 一次扫描（默认）
  python main.py --once             # 一次扫描
  python main.py --loop             # 持续循环扫描
  python main.py --scan-and-notify  # 扫描 + Webhook 通知
  python main.py --list-markets     # 列出所有活跃市场
  python main.py --show-config      # 显示当前配置

环境变量:
  ARBITRAGE_THRESHOLD=0.98          # YES+NO 触发阈值
  SCAN_INTERVAL_SEC=60              # 扫描间隔（秒）
  MIN_LIQUIDITY_USDC=100            # 最低盘口流动性
  DRY_RUN=true                      # 模拟模式（默认）
  ARBITRAGE_WEBHOOK_URL=...         # Slack/Discord 通知 URL
  LOG_LEVEL=INFO                    # 日志级别
""",
    )

    parser.add_argument(
        "--once", action="store_true",
        help="执行一次扫描后退出",
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="持续循环扫描",
    )
    parser.add_argument(
        "--scan-and-notify", action="store_true",
        help="扫描并通过 Webhook 发送通知",
    )
    parser.add_argument(
        "--list-markets", action="store_true",
        help="列出所有活跃二元市场",
    )
    parser.add_argument(
        "--show-config", action="store_true",
        help="显示当前配置",
    )

    args = parser.parse_args()

    # 加载配置
    config = load_config()

    # 配置日志
    setup_logging(config["log_level"])

    # 显示横幅
    logger.info("╔══════════════════════════════════════════════════════════╗")
    logger.info("║  Polymarket YES+NO=$1 互补套利机器人                     ║")
    logger.info("╠══════════════════════════════════════════════════════════╣")
    logger.info(f"║  模式:       {'🟢 模拟 (DRY_RUN)' if config['dry_run'] else '🔴 实盘':<47s}║")
    logger.info(f"║  阈值:       YES+NO < {config['arbitrage_threshold']:<45.2f}║")
    logger.info(f"║  流动性:     ≥ {config['min_liquidity_usdc']:<10.2f} USDC{'':>27s}║")
    logger.info(f"║  API:        {config['clob_api_url']:<46s}║")
    logger.info("╚══════════════════════════════════════════════════════════╝")

    # 确定命令
    if args.show_config:
        asyncio.run(cmd_show_config(config))
    elif args.list_markets:
        asyncio.run(cmd_list_markets(config))
    elif args.loop:
        asyncio.run(cmd_scan_loop(config))
    elif args.scan_and_notify:
        asyncio.run(cmd_scan_and_notify(config))
    else:  # --once 或默认
        asyncio.run(cmd_scan_once(config))


if __name__ == "__main__":
    main()
