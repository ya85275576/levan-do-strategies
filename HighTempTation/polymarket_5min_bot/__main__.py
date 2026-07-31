#!/usr/bin/env python3
"""polymarket_5min_bot — 独立运行入口

用法:
  python -m polymarket_5min_bot            # DRY_RUN 模式 (默认)
  DRY_RUN=true python -m polymarket_5min_bot --scans 5   # 跑 N 次扫描后退出

与 HighTempTation 集成时请使用 adapters/polymarket_5min_adapter.py
(共享风控 + 统一账户 + 看板)。
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polymarket_5min_bot.config import FiveMinBotConfig
from polymarket_5min_bot.engine import FiveMinEngine


async def main():
    parser = argparse.ArgumentParser(description="Polymarket 5min bot (standalone)")
    parser.add_argument("--scans", type=int, default=0,
                        help="执行 N 次扫描后退出 (0=无限循环)")
    parser.add_argument("--loop", action="store_true", help="无限主循环 (默认)")
    args = parser.parse_args()

    cfg = FiveMinBotConfig()
    logging.basicConfig(level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    engine = FiveMinEngine(cfg)
    await engine.start()
    print("=" * 64)
    for line in cfg.summarize():
        print(line)
    print("=" * 64)

    n = args.scans if args.scans > 0 else None
    count = 0
    try:
        while n is None or count < n:
            count += 1
            print(f"\n🔄 扫描 #{count} {asyncio.get_event_loop().time():.0f}s")
            await engine.scan_once()
            st = engine.status()
            print(f"   执行 {st['stats']['executed']} / 拒绝 {st['stats']['rejected']} | "
                  f"持仓 {len([p for p in st['open_positions']])} | "
                  f"余额 ${st['balance']:.2f}")
            await asyncio.sleep(cfg.SCAN_INTERVAL_SEC)
    finally:
        await engine.stop()
    print(f"\n✅ 完成: {engine.status()['stats']}")


if __name__ == "__main__":
    asyncio.run(main())
