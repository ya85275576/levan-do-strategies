#!/usr/bin/env python3
"""
scripts/verify_5min_integration.py — 整合验证 (DRY_RUN)

在 DRY_RUN=true 下验证 Benjam1nCup 5min Bot 整合的 7 项要求:

  [1] 子模块引入      — polymarket_5min_bot 可导入, 四大策略注册
  [2] 统一适配层      — Polymarket5MinAdapter 构造/启动/状态桥接
  [3] 依赖合并        — requirements.txt 含 websockets/ccxt/polymarket-client
  [4] 共享风控        — SharedRiskGate 日亏损熔断/总仓位限制生效
  [5] 统一账户管理    — AccountManager 单例 nonce 递增 / 订单锁
  [6] Streamlit 看板  — /api/5min 数据通路 (dashboard 读取)
  [7] DRY_RUN 验证    — 本脚本全程不下真实订单

运行:
  python3 scripts/verify_5min_integration.py [--scans N]
退出码 0 = 全部通过; 非 0 = 存在失败项。
"""
import argparse
import asyncio
import os
import sys
import time

# 确保可从 HighTempTation 根目录导入
_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BOT_DIR)
os.environ.setdefault("DRY_RUN", "true")

import logging
logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")

from account_manager import get_account_manager, OrderGateBlocked
from shared_risk import get_shared_risk

RESULTS: list = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok))
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    return ok


def check_req_merge():
    """[3] 依赖合并: requirements.txt 含 websockets/ccxt/polymarket-client"""
    path = os.path.join(_BOT_DIR, "requirements.txt")
    if not os.path.exists(path):
        return check("[3] 依赖合并", False, "requirements.txt 缺失")
    text = open(path).read().lower()
    missing = [d for d in ("websockets", "ccxt", "polymarket-client")
               if d not in text]
    return check("[3] 依赖合并", not missing,
                 "缺失: " + ", ".join(missing) if missing else
                 "websockets/ccxt/polymarket-client 均已声明")


async def verify_submodule():
    """[1] 子模块 + 策略注册"""
    from polymarket_5min_bot import __version__
    from polymarket_5min_bot.config import FiveMinBotConfig
    from polymarket_5min_bot.engine import FiveMinEngine
    cfg = FiveMinBotConfig()
    cfg.SCAN_INTERVAL_SEC = 1
    ok = True
    ok &= check("[1] 子模块", __version__ == "1.0.0",
                f"polymarket_5min_bot v{__version__}")
    engine = FiveMinEngine(cfg)
    names = [s.name for s in engine._strategies]
    need = {"ARB", "SNIPER", "MOMENTUM", "LADDER", "STAIR"}
    ok &= check("[1] 四大策略注册", need.issubset(set(names)),
                "已注册: " + ", ".join(names))
    return ok, engine


async def verify_account():
    """[5] 统一账户管理: 单例 / nonce / 订单门控"""
    am1 = get_account_manager()
    am2 = get_account_manager()
    ok = check("[5] 账户单例", am1 is am2, f"initial={am1.initial_capital}")
    # nonce 递增
    n0 = am1._nonce
    try:
        async with am1.order_gate(strategy="TEST", amount_usd=5.0):
            pass
    except OrderGateBlocked as e:
        ok &= check("[5] 订单门控", False, str(e))
    ok &= check("[5] nonce 递增", am1._nonce == n0 + 1,
                f"{n0} → {am1._nonce}")
    # 余额不足拦截
    am1.balance = 1.0
    blocked = False
    try:
        async with am1.order_gate(strategy="TEST", amount_usd=5000.0):
            pass
    except OrderGateBlocked:
        blocked = True
    am1.balance = am1.initial_capital
    ok &= check("[5] 余额不足拦截", blocked, "amount>balance → OrderGateBlocked")
    return ok


def verify_shared_risk():
    """[4] 共享风控: 日亏损熔断 / 总仓位限制"""
    risk = get_shared_risk()
    # 重置
    risk._daily_pnl = 0.0
    risk._open_count = 0
    # 放行
    r = risk.check(strategy="TEST", amount_usd=10.0)
    ok = check("[4] 风控放行", r is None, "正常信号通过")
    # 总仓位超限
    risk._open_count = risk.max_concurrent
    r = risk.check(strategy="TEST", amount_usd=10.0)
    ok &= check("[4] 总仓位限制", r is not None and "总持仓" in r, r or "")
    risk._open_count = 0
    # 日亏损熔断
    risk._daily_pnl = -risk.daily_loss_limit - 1.0
    r = risk.check(strategy="TEST", amount_usd=10.0)
    ok &= check("[4] 日亏损熔断", r is not None and "日亏损" in r, r or "")
    risk._daily_pnl = 0.0
    return ok


async def verify_adapter():
    """[2] 适配层 + [6] 看板数据通路 + [7] DRY_RUN 全程模拟"""
    from adapters.polymarket_5min_adapter import Polymarket5MinAdapter
    adapter = Polymarket5MinAdapter()
    ok = True
    ok &= check("[2] 适配器构造", adapter.engine is not None, "引擎已挂载")
    ok &= check("[2] 门控执行器", type(adapter.engine.executor).__name__
                == "GuardedExecutor", "风控+账户门控已注入")
    ok &= check("[2] DRY_RUN", adapter.cfg.DRY_RUN is True,
                "未连接真实下单路径")

    await adapter.engine.start()
    # 跑 3 次扫描, 触发信号
    for i in range(3):
        await adapter.engine.scan_once()
        await asyncio.sleep(0.3)
    st = adapter.engine.status()
    stats = st["stats"]
    ok &= check("[7] 扫描执行", stats["scan_count"] >= 3,
                f"扫描 {stats['scan_count']} 次, 信号 {stats['signals']}, "
                f"执行 {stats['executed']}, 拒绝 {stats['rejected']}")
    counts = st["signal_counts"]
    ok &= check("[7] 信号链路", stats["signals"] > 0,
                f"策略触发分布: {counts}")
    # 狙击窗口验证: 构造结算前 30s + 价格 0.96 的模拟市场 → 应触发 SNIPER
    from polymarket_5min_bot.markets import FiveMinMarket as _FM
    from polymarket_5min_bot.strategies import SniperStrategy, ArbitrageStrategy
    sniper_before = adapter.engine.status()["signal_counts"].get("SNIPER", 0)
    m_sniper = _FM(
        event_id="sim-sniper-test", event_title="BTC Up or Down - SNIPER TEST",
        asset="BTC", strike_price=60000.0,
        start_time=time.time(), end_time=time.time() + 30,
        yes_market_id="sim-sniper-test-yes", no_market_id="sim-sniper-test-no",
        yes_token_id="tok-sniper-yes", no_token_id="tok-sniper-no",
        simulated=True, spot_price=60000.0,
    )
    m_sniper.prices = {m_sniper.yes_token_id: 0.96, m_sniper.no_token_id: 0.03}
    sigs = await SniperStrategy(adapter.cfg).scan(adapter.engine._ctx, [m_sniper])
    for s in sigs:
        await adapter.engine.executor.execute(s, adapter.engine)
    ok &= check("[7] 狙击窗口",
                adapter.engine.status()["signal_counts"].get("SNIPER", 0) > sniper_before,
                f"结算前 30s + 价格 0.96 → 触发 SNIPER: {sniper_before} → "
                f"{adapter.engine.status()['signal_counts'].get('SNIPER', 0)}")
    # 套利验证: 组合成本 0.94 ≤ 0.95 → 应触发 ARB (Buy1 + Buy2 双腿)
    arb_before = adapter.engine.status()["signal_counts"].get("ARB", 0)
    m_arb = _FM(
        event_id="sim-arb-test", event_title="BTC Up or Down - ARB TEST",
        asset="BTC", strike_price=60000.0,
        start_time=time.time(), end_time=time.time() + 300,
        yes_market_id="sim-arb-test-yes", no_market_id="sim-arb-test-no",
        yes_token_id="tok-arb-yes", no_token_id="tok-arb-no",
        simulated=True, spot_price=60000.0,
    )
    m_arb.prices = {m_arb.yes_token_id: 0.62, m_arb.no_token_id: 0.32}  # 组合 0.94
    sigs = await ArbitrageStrategy(adapter.cfg).scan(adapter.engine._ctx, [m_arb])
    for s in sigs:
        await adapter.engine.executor.execute(s, adapter.engine)
    ok &= check("[7] 套利触发",
                adapter.engine.status()["signal_counts"].get("ARB", 0) > arb_before,
                f"组合成本 0.94 → 触发 ARB (含 LEG2 第二腿): {arb_before} → "
                f"{adapter.engine.status()['signal_counts'].get('ARB', 0)}")
    ok &= check("[6] 状态桥接", "shared_risk" in adapter.status() and
                "account" in adapter.status(),
                "/api/5min 数据源就绪 (shared_risk+account)")
    # 结算链路: 手动触发一次结算 (模拟 5min 周期结束)
    risk_open_before = get_shared_risk()._open_count
    for p in adapter.engine._positions:
        p.market.end_time = time.time() - 1
    await adapter.engine._settle_finished()
    ok &= check("[7] 结算链路", adapter.engine.stats["settled"] > 0,
                f"已结算 {adapter.engine.stats['settled']}, "
                f"胜 {adapter.engine.stats['wins']} 负 {adapter.engine.stats['losses']}")
    # 结算回调必须释放共享总仓位 (report_open(-1)), 否则仓位计数只增不减
    risk_open_after = get_shared_risk()._open_count
    ok &= check("[4] 结算释放仓位", risk_open_after < risk_open_before,
                f"共享总仓位 {risk_open_before} → {risk_open_after} "
                f"(结算回调释放, 防只增不减)")

    # 风控联动: 设置日亏损熔断后新信号应被拦截
    risk = get_shared_risk()
    risk._daily_pnl = -risk.daily_loss_limit - 1.0
    before = adapter.engine.stats["rejected"]
    await adapter.engine.scan_once()
    await asyncio.sleep(0.3)
    risk._daily_pnl = 0.0
    ok &= check("[4] 共享风控联动", adapter.engine.stats["rejected"] > before,
                "日亏损熔断后 5min 信号被拦截")

    await adapter.engine.stop()
    return ok


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scans", type=int, default=3)
    args = parser.parse_args()

    print("=" * 62)
    print("🧪 HighTempTation × Benjam1nCup 5min Bot 整合验证 (DRY_RUN)")
    print("=" * 62)

    ok1, engine = await verify_submodule()
    ok2 = await verify_account()
    ok3 = verify_shared_risk()
    ok4 = await verify_adapter()
    ok5 = check_req_merge()

    print("\n" + "=" * 62)
    failed = [n for n, ok in RESULTS if not ok]
    if not failed:
        print(f"🎉 全部 {len(RESULTS)} 项检查通过 — 整合就绪!")
    else:
        print(f"⚠️ {len(failed)} 项检查失败: {failed}")
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
