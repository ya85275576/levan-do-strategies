#!/usr/bin/env python3
"""
HighTempTation 高阶优化包 — 一键自检/演示

对 6 个模块用合成数据跑通全部功能并输出报告:

  1. microstructure — LOB 形状/深度斜率/Square-Root Law/VPIN/开仓闸门
  2. arbitrage      — 桶分割/相邻桶平价/期限结构/多平台价差
  3. ml_residual    — 残差学习（自动探测后端）/在线学习/NLP 增强
  4. order_fsm      — 幂等提交/幽灵防护/部分成交/仓位对账
  5. walk_forward   — Point-in-Time/Walk-Forward/成本敏感性/稳定性
  6. chaos          — 5 种故障注入 + 熔断验证

用法:
  python -m highopt.runner                 # 全量自检
  python -m highopt.runner --db path.db    # 同时把结果写入 SQLite
  python highopt/runner.py                 # 直接从目录运行

退出码: 0 = 全部通过, 1 = 有失败项
"""
import argparse
import asyncio
import json
import logging
import math
import os
import random
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from highopt.microstructure import (OrderBookSnapshot, OrderBookShape,
                                    SquareRootLawImpact, VPINFilter,
                                    MicrostructureGate)
from highopt.arbitrage import ArbitrageScanner
from highopt.ml_residual import MLResidualLearner, NLPEnhancement
from highopt.order_fsm import (OrderFSM, OrderState, SimExchangeAdapter,
                               PositionReconciler)
from highopt.walk_forward import (PointInTimeLoader, WalkForwardBacktester,
                                  CostSensitivityAnalyzer, StabilityMetrics,
                                  SimpleBucketStrategy)
from highopt.chaos import ChaosVerifier, CircuitBreaker, FaultInjector

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("highopt.runner")

RESULTS: dict = {}


# ════════════════════════════════════════════════════════════════
# 合成数据
# ════════════════════════════════════════════════════════════════

def make_lob(mid: float, depth: float = 500.0, levels: int = 5) -> OrderBookSnapshot:
    """生成一个厚度可调的 LOB 快照"""
    rng = random.Random(7)
    bids, asks = [], []
    for i in range(levels):
        step = 0.01 * (i + 1)
        bids.append((mid - step, depth * (0.8 ** i) * rng.uniform(0.9, 1.1)))
        asks.append((mid + step, depth * (0.8 ** i) * rng.uniform(0.9, 1.1)))
    return OrderBookSnapshot(bids=bids, asks=asks)


def make_records(n: int = 400, seed: int = 1) -> list:
    """合成 Point-in-Time 记录: mu/sigma/bucket/市场价/结算/实况"""
    rng = random.Random(seed)
    records = []
    base_ts = 1700000000
    for i in range(n):
        mu = 28.0 + 6.0 * math.sin(i / 40.0) + rng.gauss(0, 1.0)
        sigma = max(1.0, 2.0 + rng.gauss(0, 0.3))
        # 模型残差: 正弦漂移（可被 ML 学习）
        resid = 1.2 * math.sin(i / 60.0) + rng.gauss(0, 0.5)
        actual = mu + resid
        lo = 25.0; hi = 35.0
        p_model = _bucket_prob(mu, sigma, lo, hi)
        p_market = max(0.05, min(0.95, p_model + rng.gauss(0, 0.03) + 0.02))
        records.append({
            "ts": base_ts + i * 3600,
            "date": datetime.fromtimestamp(base_ts + i * 3600, tz=timezone.utc).strftime("%Y-%m-%d"),
            "city": rng.choice(["Tokyo", "Seoul", "Singapore"]),
            "mu": mu, "sigma": sigma, "n_models": 5,
            "spread": rng.uniform(0.5, 3.0),
            "models": {m: mu + rng.gauss(0, sigma * 0.3) for m in
                       ["best_match", "icon_seamless", "gfs_seamless", "gem_seamless", "jma_seamless"]},
            "bucket_lower": lo, "bucket_upper": hi,
            "market_price": p_market,
            "actual": actual,
            "settle": 1.0 if actual >= lo and actual < hi else 0.0,  # NO 结算值(0=结算YES,1=结算NO)
        })
    return records


def _bucket_prob(mu, sigma, lo, hi):
    from scipy.stats import norm
    return max(0.0, min(1.0, norm.cdf((hi - mu) / sigma) - norm.cdf((lo - mu) / sigma)))


# ════════════════════════════════════════════════════════════════
# 模块自检
# ════════════════════════════════════════════════════════════════

def test_microstructure() -> bool:
    """1. 订单簿微观结构"""
    ok = True
    thick = make_lob(0.40, depth=1000.0)
    thin = make_lob(0.40, depth=30.0)

    shape_t = OrderBookShape(thick)
    shape_n = OrderBookShape(thin)
    logger.info(f"  厚簿: shape={shape_t.lob_shape} slope={shape_t.depth_slope():.1f} "
                f"总深度={thick.total_depth:.0f}")
    logger.info(f"  薄簿: shape={shape_n.lob_shape} slope={shape_n.depth_slope():.1f} "
                f"总深度={thin.total_depth:.0f}")
    assert shape_t.lob_shape in ("THICK", "NORMAL"), "厚簿被误判"
    assert shape_n.lob_shape == "THIN", "薄簿未被识别"
    assert thick.depth_bid > thin.depth_bid

    # Square-Root Law
    imp = SquareRootLawImpact(k=1.0)
    i1 = imp.impact(qty=100, sigma=0.02, volume=5000)
    i2 = imp.impact(qty=400, sigma=0.02, volume=5000)
    assert i2 > i1 * 1.9, "Square-Root Law 单调性失败"
    assert i2 < i1 * 2.1, "Square-Root Law 根号比例失败"
    max_q = imp.max_qty_for_budget(budget=0.01, sigma=0.02, volume=5000, mid=0.4)
    logger.info(f"  SR-Law: impact(100)={i1:.5f} impact(400)={i2:.5f} max_qty(1¢)={max_q:.0f}")
    assert 0 < max_q < 5000

    # VPIN
    vpin = VPINFilter(bucket_volume=500, window=10)
    rng = random.Random(3)
    price = 0.40
    for _ in range(200):
        drift = rng.choice([-1, 0, 1]) * 0.005
        price = max(0.1, min(0.9, price + drift))
        vpin.update(price=price, volume=float(rng.randint(10, 100)))
    # 高知情: 单边 tick
    vpin_high = VPINFilter(bucket_volume=500, window=10)
    for _ in range(100):
        vpin_high.update(price=0.45, volume=100.0)   # 全买
    logger.info(f"  VPIN 随机流={vpin.vpin:.3f} 单边流={vpin_high.vpin:.3f}")
    assert vpin_high.vpin >= 0.9, "单边流 VPIN 应接近 1"
    assert vpin_high.is_high()

    # 开仓闸门
    gate = MicrostructureGate(vpin=vpin_high)
    ok_g, reasons, metrics = gate.check(thick, qty=50, sigma=0.02, volume=5000)
    logger.info(f"  闸门(高VPIN): ok={ok_g} reasons={reasons} metrics={metrics}")
    assert not ok_g and any("VPIN" in r for r in reasons), "高 VPIN 应拒绝开仓"
    gate2 = MicrostructureGate(vpin=VPINFilter())
    ok_g2, _, m2 = gate2.check(thick, qty=50, sigma=0.02, volume=5000)
    assert ok_g2, f"健康簿应放行: {m2}"

    RESULTS["microstructure"] = {
        "ok": ok, "thick_shape": shape_t.lob_shape, "thin_shape": shape_n.lob_shape,
        "srlaw_impact_100": round(i1, 5), "vpin_random": round(vpin.vpin, 3),
        "vpin_oneway": round(vpin_high.vpin, 3),
        "gate_metrics": m2,
    }
    return ok


def test_arbitrage() -> bool:
    """2. 跨市场/跨期套利"""
    # 桶分割: ΣYES = 0.95（被低估 → 买全部 YES）
    buckets_cheap = [
        {"key": "T<20", "lower": -99, "yes": 0.05, "no": 0.95},
        {"key": "20-25", "lower": 20, "yes": 0.15, "no": 0.85},
        {"key": "25-30", "lower": 25, "yes": 0.30, "no": 0.70},
        {"key": "30-35", "lower": 30, "yes": 0.28, "no": 0.72},
        {"key": "35-40", "lower": 35, "yes": 0.12, "no": 0.88},
        {"key": "T>=40", "lower": 40, "yes": 0.05, "no": 0.95},
    ]
    # 桶分割: ΣYES = 1.06（被高估 → 卖全部 YES）
    buckets_rich = [dict(b, yes=b["yes"] + 0.018) for b in buckets_cheap]
    # 单调性违反: 低桶 YES 高于高桶 YES
    buckets_bad = [dict(b) for b in buckets_cheap]
    buckets_bad[1]["yes"] = 0.42  # 20-25 高于 25-30

    scanner = ArbitrageScanner(fee_pct=0.02, gas_usd=0.01)
    sigs_cheap = scanner.scan_bucket_group(buckets_cheap, sigma=2.0)
    sigs_rich = scanner.scan_bucket_group(buckets_rich, sigma=2.0)
    sigs_bad = scanner.scan_bucket_group(buckets_bad, sigma=1.0)
    for s in sigs_cheap + sigs_rich + sigs_bad:
        logger.info(f"  套利: [{s.arb_type}] {s.description} pnl=${s.expected_pnl:.3f}")
    assert any(s.arb_type == "bucket_partition" for s in sigs_cheap), "低估组应触发桶分割买"
    assert any(s.arb_type == "bucket_partition" for s in sigs_rich), "高估组应触发桶分割卖"
    assert any(s.arb_type == "adjacent_monotone" for s in sigs_bad), "单调违反应被检出"

    # 期限结构
    term_sigs = scanner.scan_term_structure([
        {"maturity": "D0", "days": 1, "yes": 0.45, "qty": 100},
        {"maturity": "D2", "days": 3, "yes": 0.25, "qty": 100},
        {"maturity": "D5", "days": 6, "yes": 0.20, "qty": 100},
    ])
    for s in term_sigs:
        logger.info(f"  期限: [{s.arb_type}] {s.description}")
    assert any(s.arb_type == "term_structure" for s in term_sigs), "期限倒挂应被检出"

    # 多平台价差
    plat_sigs = scanner.scan_cross_platform({
        "polymarket": {"bid": 0.40, "ask": 0.42, "depth": 500},
        "okx_events": {"bid": 0.36, "ask": 0.37, "depth": 300},
    })
    for s in plat_sigs:
        logger.info(f"  平台: [{s.arb_type}] {s.description}")
    assert any(s.arb_type == "cross_platform" for s in plat_sigs), "跨平台价差应被检出"

    RESULTS["arbitrage"] = {
        "ok": True, "signals": len(sigs_cheap + sigs_rich + sigs_bad + term_sigs + plat_sigs),
        "bucket_partition_cheap": len(sigs_cheap), "bucket_partition_rich": len(sigs_rich),
        "monotone_violations": len(sigs_bad), "term": len(term_sigs), "platform": len(plat_sigs),
    }
    return True


def test_ml_residual() -> bool:
    """3. ML 残差学习"""
    rows = make_records(300, seed=5)
    train = rows[:200]
    test = rows[200:]

    learner = MLResidualLearner(backend="auto", min_samples=30)
    learner.fit(train)
    logger.info(f"  后端: {learner.backend} stats={learner.stats()}")

    # 预测质量: 残差预测应比零修正更接近实际
    err_zero, err_ml = 0.0, 0.0
    for r in test:
        err_zero += abs(r["actual"] - r["mu"])
        pred = learner.predict(r)
        err_ml += abs(r["actual"] - (r["mu"] + pred))
    err_zero /= len(test); err_ml /= len(test)
    logger.info(f"  MAE: 零修正={err_zero:.3f}°C ML修正={err_ml:.3f}°C 改善={(1-err_ml/err_zero)*100:.1f}%")
    assert err_ml <= err_zero * 1.05, "ML 残差修正不应显著劣于零修正"

    # 在线学习
    learner.online_update(rows[200:240])
    assert learner.stats()["n_train"] >= 240

    # 概率修正
    corr_prob, raw_prob, res = learner.correct_prob(
        _bucket_prob, {"mu": 28.0, "sigma": 2.0}, 25.0, 35.0)
    logger.info(f"  概率修正: raw={raw_prob:.3f} corr={corr_prob:.3f} residual={res:+.2f}")

    # NLP
    nlp = NLPEnhancement()
    s_hot = nlp.score_texts(["Heatwave warning for Tokyo", "record high 38C expected"])
    s_cold = nlp.score_texts(["Cold snap with frost expected"])
    s_neutral = nlp.score_texts(["Partly cloudy"])
    logger.info(f"  NLP: 热={s_hot:+.3f} 冷={s_cold:+.3f} 中性={s_neutral:+.3f}")
    assert s_hot > 0.3 and s_cold < -0.3 and abs(s_neutral) < 0.2
    mu_adj = nlp.adjust_forecast(28.0, s_hot, max_shift=1.0)
    assert mu_adj > 28.0 and mu_adj <= 29.0

    RESULTS["ml_residual"] = {
        "ok": True, "backend": learner.backend,
        "mae_zero": round(err_zero, 3), "mae_ml": round(err_ml, 3),
        "improvement_pct": round((1 - err_ml / err_zero) * 100, 1),
        "nlp_hot": round(s_hot, 3), "nlp_cold": round(s_cold, 3),
    }
    return True


async def test_order_fsm() -> bool:
    """4. 订单状态机 FSM"""
    # 4a. 幂等 + 正常成交
    adapter = SimExchangeAdapter(seed=1)
    fsm = OrderFSM(adapter, submit_timeout=2.0, poll_interval=0.1)
    o1 = await fsm.submit("tok-1", "buy", 100, price=0.40, intent_key="Tokyo-NO-20250415")
    o2 = await fsm.submit("tok-1", "buy", 100, price=0.40, intent_key="Tokyo-NO-20250415")
    assert o1.client_order_id == o2.client_order_id, "幂等失败: 重复意图应返回同一订单"
    logger.info(f"  幂等: intent 重复提交 → 同一订单 {o1.client_order_id}")
    o1 = await fsm.poll(o1, max_waits=5)
    logger.info(f"  订单推进: state={o1.state.value} filled={o1.filled_qty} avg={o1.avg_fill_price:.4f}")
    assert o1.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED), \
        f"订单未成交: {o1.state.value}"

    # 4b. 幽灵订单防护（ghost_rate=1.0 → 提交无响应）
    adapter_g = SimExchangeAdapter(ghost_rate=1.0, seed=2)
    fsm_g = OrderFSM(adapter_g, submit_timeout=0.5, poll_interval=0.1)
    g = await fsm_g.submit("tok-2", "buy", 50, price=0.30, intent_key="ghost-test")
    if g.state != OrderState.UNKNOWN:
        g = await fsm_g.poll(g, max_waits=3)
    logger.info(f"  幽灵: state={g.state.value}")
    resolved = await fsm_g.resolve_ghost(g)
    logger.info(f"  幽灵解析: state={resolved.state.value}")
    assert resolved.state in (OrderState.UNKNOWN, OrderState.CANCELLED,
                              OrderState.SUBMITTED, OrderState.FILLED), \
        f"幽灵解析异常: {resolved.state.value}"
    # 绝不允许自动重发: 同一 intent 无第二个订单
    assert len(fsm_g.orders_by_intent("ghost-test")) == 1, "幽灵订单被重复提交!"

    # 4c. 部分成交 + 仓位对账
    adapter_p = SimExchangeAdapter(partial_fill=0.4, seed=3)
    fsm_p = OrderFSM(adapter_p)
    p = await fsm_p.submit("tok-3", "buy", 100, price=0.35, intent_key="partial-test")
    p = await fsm_p.poll(p, max_waits=8)
    logger.info(f"  部分成交: state={p.state.value} filled={p.filled_qty:.1f} avg={p.avg_fill_price:.4f}")
    assert p.filled_qty > 0

    reconciler = PositionReconciler(tolerance=0.0)
    expected = reconciler.expected_positions(fsm_p.open_orders() + [p])
    actual = await adapter_p.get_positions()
    report = reconciler.reconcile(expected, actual)
    logger.info(f"  对账: expected={expected} actual={actual} 差异={report}")
    # 注入外部成交模拟丢失 → 应检出差异
    adapter_p.apply_fill("tok-3", "buy", 30, 0.36)
    actual2 = await adapter_p.get_positions()
    report2 = reconciler.reconcile(expected, actual2)
    assert len(report2) > 0, "注入的外部成交应产生对账差异"
    logger.info(f"  对账(注入差异): {report2}")

    RESULTS["order_fsm"] = {
        "ok": True,
        "idempotent": o1.client_order_id == o2.client_order_id,
        "final_state": o1.state.value,
        "ghost_resolved_state": resolved.state.value,
        "ghost_no_dup": len(fsm_g.orders_by_intent("ghost-test")) == 1,
        "partial_fill": round(p.filled_qty, 1),
        "reconcile_diff": len(report2),
    }
    return True


def test_walk_forward() -> bool:
    """5. Walk-Forward 回测"""
    records = make_records(500, seed=9)

    # Point-in-Time: 前视偏差断言
    loader = PointInTimeLoader(records)
    snap = loader.state_at(records[100]["ts"])
    assert all(r["ts"] <= records[100]["ts"] for r in snap), "Point-in-Time 违反!"
    assert loader.assert_no_lookahead(records[50]["ts"], future_keys=["actual"])

    # Walk-Forward 回测
    def make_model(train_events):
        """用训练事件拟合残差修正器（fold 内 Point-in-Time 拟合）"""
        lr = MLResidualLearner(backend="auto", min_samples=10)
        lr.fit(train_events)
        return lr.predict

    bt = WalkForwardBacktester(n_folds=6, model_factory=make_model)
    report = bt.run(records)
    logger.info(f"  Walk-Forward: folds={len(report.folds)} stability={report.stability}")
    for f in report.folds:
        logger.info(f"    fold{f.fold}: trades={f.trades} pnl={f.total_pnl} "
                    f"sharpe={f.sharpe} calmar={f.calmar}")
    assert len(report.folds) >= 2, "折叠数不足"
    assert report.stability, "稳定性指标缺失"

    # 成本敏感性
    csa = CostSensitivityAnalyzer()
    cost = csa.analyze(records, fee_pcts=[0.0, 0.02, 0.05, 0.1, 0.2],
                       slippage_pcts=[0.0, 0.1], gas_usd=[0.0, 0.02])
    breakeven = cost["breakeven_fee_pct"]
    logger.info(f"  成本敏感性: 网格={len(cost['grid'])} 盈亏平衡手续费={breakeven}")
    assert len(cost["grid"]) == 5 * 2 * 2

    # 稳定性统计
    rets = [r["pnl"] / 100.0 for r in SimpleBucketStrategy().run(records)]
    stab = StabilityMetrics.summarize(rets)
    logger.info(f"  稳定性: {stab}")
    assert stab["n"] > 0

    RESULTS["walk_forward"] = {
        "ok": True,
        "folds": len(report.folds),
        "stability": report.stability,
        "cost_grid": len(cost["grid"]),
        "breakeven_fee_pct": breakeven,
    }
    return True


async def test_chaos() -> bool:
    """6. 混沌工程"""
    verifier = ChaosVerifier(failure_threshold=3, cooldown=1.5, success_threshold=2)
    report = await verifier.run_all()

    for name, s in report.items():
        if name.startswith("_"):
            continue
        status = "✅" if s["ok"] else "❌"
        logger.info(f"  混沌[{name}]: {status} "
                    f"stats={s['circuit_stats']} 断言={[(a['desc'], a['ok']) for a in s['assertions']]}")

    summary = report["_summary"]
    logger.info(f"  混沌汇总: {summary['passed']}/{summary['total_scenarios']} 场景通过")
    assert summary["passed"] == summary["total_scenarios"], "混沌场景未全部通过"

    RESULTS["chaos"] = {
        "ok": True,
        "summary": summary,
        "scenarios": {k: {"stats": v["circuit_stats"],
                          "assertions": [a["ok"] for a in v["assertions"]]}
                      for k, v in report.items() if not k.startswith("_")},
    }
    return True


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════

def persist(db_path: str):
    """把自检结果写入 SQLite（可选）"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from db_manager import TradeDB
        db = TradeDB(db_path)
        now = int(time.time())
        ms = RESULTS.get("microstructure", {})
        if ms:
            db.store_microstructure_snapshot(now, "Tokyo", "demo", 0.40, 5000, 5000,
                                             ms.get("thick_shape", ""),
                                             ms.get("gate_metrics", {}).get("depth_slope"),
                                             ms.get("srlaw_impact_100"),
                                             ms.get("vpin_random"))
        arb = RESULTS.get("arbitrage", {})
        if arb:
            db.store_arbitrage_signal(now, "demo_bucket_partition",
                                      f"自检检出 {arb['signals']} 个套利信号",
                                      arb["signals"] * 0.01)
        chaos = RESULTS.get("chaos", {})
        if chaos:
            for name, s in chaos["scenarios"].items():
                db.store_chaos_event(now, name, name, "verified",
                                     "pass" if all(s["assertions"]) else "fail")
        db.close()
        logger.info(f"✅ 结果已写入 {db_path}")
    except Exception as e:
        logger.warning(f"写库失败（不影响自检结论）: {e}")


async def main():
    parser = argparse.ArgumentParser(description="HighTempTation 高阶优化自检")
    parser.add_argument("--db", default=None, help="可选: 写入结果的 SQLite 路径")
    args = parser.parse_args()

    logger.info("=" * 66)
    logger.info("  HighTempTation 高阶优化包 — 一键自检")
    logger.info("=" * 66)

    checks = [
        ("microstructure", test_microstructure, "订单簿微观结构"),
        ("arbitrage", test_arbitrage, "跨市场/跨期套利"),
        ("ml_residual", test_ml_residual, "ML 残差学习"),
        ("order_fsm", lambda: asyncio.run(test_order_fsm()), "订单状态机 FSM"),
        ("walk_forward", test_walk_forward, "Walk-Forward 回测"),
        ("chaos", lambda: asyncio.run(test_chaos()), "混沌工程"),
    ]

    passed = 0
    for key, fn, label in checks:
        try:
            ok = fn()
            passed += 1 if ok else 0
            logger.info(f"  ✅ [{label}] 通过")
        except Exception as e:
            logger.error(f"  ❌ [{label}] 失败: {e}", exc_info=True)
            RESULTS[key] = {"ok": False, "error": str(e)}

    logger.info("=" * 66)
    logger.info(f"  自检结果: {passed}/{len(checks)} 模块通过")
    for key, label in [(k, l) for k, _, l in checks]:
        r = RESULTS.get(key, {})
        logger.info(f"    - {label}: {'✅' if r.get('ok') else '❌'}")
    logger.info("=" * 66)

    if args.db:
        persist(args.db)

    sys.exit(0 if passed == len(checks) else 1)


if __name__ == "__main__":
    asyncio.run(main())
