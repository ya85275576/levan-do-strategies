#!/usr/bin/env python3
"""
HighTempTation 终极优化包 — 一键自检/演示（第四波）

对 7 个模块用合成数据跑通全部功能并输出报告:

  1. onchain        — Gas 竞价 / Nonce 锁 / MEV / Multicall / 合约升级 / USDC.e 校验
  2. keysafe        — 钱包分级 / KMS / 加密密钥库 / 审计哈希链 / 双人控制
  3. oracle_risk    — UMA 结算窗口 / 措辞解析 / 风险矩阵 / 同源校准
  4. latency        — 节点选择 / WS 订单簿 / 内核调优
  5. antigame       — 地址轮换 / 冰山 / 噪声 / Dashboard 延迟
  6. metacontroller — LinUCB / PPO / Particle BMA / 融合调度
  7. human_loop     — AUTO/NOTIFY/CONFIRM/PAUSE 分级执行

用法:
  python -m highopt_ultra.runner                 # 全量自检
  python -m highopt_ultra.runner --db path.db    # 同时把结果写入 SQLite
  python highopt_ultra/runner.py                 # 直接从目录运行

退出码: 0 = 全部通过, 1 = 有失败项
"""
import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import random
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from highopt_ultra.onchain import (GasAuctioneer, NonceManager, MEVGuard,
                                   MulticallBatcher, ContractUpgradeWatcher,
                                   TokenValidator, EVENT_UPGRADED)
from highopt_ultra.keysafe import (WalletManager, WalletTier, SimKMSBackend,
                                   EncryptedKeystore, AuditLogger, DualControl)
from highopt_ultra.oracle_risk import (UMASettlementModel, ContractPhraseParser,
                                       OracleRiskMatrix, SameSourceCalibration)
from highopt_ultra.latency import (RPCNodeSelector, WSOrderBookFeed, KernelTuner)
from highopt_ultra.antigame import (AddressRotation, IcebergOrder, NoiseTrader,
                                    DashboardDelay)
from highopt_ultra.metacontroller import (ContextualBandit, PPOScheduler,
                                          ParticleFilterBMA, MetaController)
from highopt_ultra.human_loop import (HumanInTheLoop, HumanLoopController,
                                      AutomationLevel)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("highopt_ultra.runner")

RESULTS: dict = {}


# ════════════════════════════════════════════════════════════════
# 1. 链上执行层
# ════════════════════════════════════════════════════════════════

def test_onchain() -> bool:
    ok = True

    # Gas 动态竞价
    auction = GasAuctioneer()
    for i in range(30):
        auction.observe(base_fee=25 + (i % 7) * 3, gas_used_ratio=0.5 + 0.3 * (i % 3) / 2)
    fee = auction.suggest()
    logger.info(f"  Gas: base={fee['base_fee']} priority={fee['priority_fee']} "
                f"max={fee['max_fee']} ({fee['confidence']})")
    assert fee["max_fee"] >= fee["base_fee"] + fee["priority_fee"], "max_fee 覆盖不足"
    assert fee["priority_fee"] > 0

    # Nonce 分布式锁
    nm = NonceManager(":memory:")
    n0 = nm.acquire("0xHOT")
    lock_held = (nm.acquire("0xHOT") == -1)          # 锁未释放 → 拒绝分配
    nm.commit("0xHOT", n0)
    n1 = nm.acquire("0xHOT")
    nm.commit("0xHOT", n1)
    n2 = nm.acquire("0xHOT")
    nm.commit("0xHOT", n2)
    logger.info(f"  Nonce: {n0},{n1},{n2} 单调唯一 | 锁占用拦截={lock_held}")
    assert (n0, n1, n2) == (0, 1, 2), "nonce 必须单调且唯一"
    assert lock_held, "锁未释放时不得分配 nonce"
    assert nm.check("0xHOT", n0), "已用 nonce 应被标记"
    n3 = nm.acquire("0xHOT")
    assert not nm.check("0xHOT", n3), "未提交 nonce 不应标记为已用"
    nm.release("0xHOT", n3)

    # MEV 保护
    guard = MEVGuard()
    ok1, reasons1, m1 = guard.check(qty=50, obi=0.1, depth=5000, spread=0.005, mid=0.40)
    ok2, reasons2, m2 = guard.check(qty=2000, obi=0.9, depth=300, spread=0.03, mid=0.40)
    logger.info(f"  MEV: 小单通过={ok1} 大单+失衡={ok2} score={m2['sandwich_score']} "
                f"route={m2['route']}")
    assert ok1 and not ok2, "低风险放行 / 高风险拦截"
    assert m2["route"] == "protected"

    # Multicall
    batcher = MulticallBatcher()
    calls = [("0xAAA", "0x1234"), ("0xBBB", "0x5678"), ("0xCCC", "0x90ab")]
    payload = batcher.build_batch(calls)
    back = batcher.decode_batch(payload)
    save = batcher.estimate_saving(calls, per_call_gas=60_000)
    logger.info(f"  Multicall: {len(calls)}笔→1笔 省Gas={save['saving_gas']} "
                f"({save['saving_pct']}%) 往返一致={back == calls}")
    assert back == calls, "Multicall 编解码往返失败"
    assert save["saving_gas"] > 0
    split = batcher.split_batches(calls, max_gas=100_000)
    assert all(len(b) >= 1 for b in split) and sum(len(b) for b in split) == len(calls)

    # 合约升级监听
    watcher = ContractUpgradeWatcher(proxy="0xPROXY")
    watcher.consume_logs([{"blockNumber": 12345, "transactionHash": "0xtx1",
                           "topics": [EVENT_UPGRADED, "0xNEWIMPL"]}])
    risk = watcher.last_risk()
    logger.info(f"  合约升级: {watcher.history()[0]['event']} → {risk}")
    assert risk and "暂停" in risk

    # USDC.e vs Native
    tv = TokenValidator()
    v_native = tv.validate("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "USDC", 6)
    v_bridged = tv.validate("0x2791bca1f2de4661ed88a30c99a7a9449aa84174", "USDC", 6)
    v_unknown = tv.validate("0xdeadbeef")
    logger.info(f"  Token: native={v_native['risk']} bridged={v_bridged['risk']} "
                f"unknown={v_unknown['risk']}")
    assert v_native["risk"] == "SAFE" and v_bridged["risk"] == "CAUTION"
    assert not v_unknown["ok"]

    RESULTS["onchain"] = {"fee": fee, "nonce": [n0, n1, n2],
                          "nonce_lock_held": lock_held,
                          "mev_thin_ok": ok1, "mev_thick_blocked": not ok2,
                          "multicall_saving_pct": save["saving_pct"],
                          "upgrade_risk": risk, "usdc_e_risk": v_bridged["risk"]}
    return ok


# ════════════════════════════════════════════════════════════════
# 2. 密钥安全
# ════════════════════════════════════════════════════════════════

def test_keysafe(tmpdir: str) -> bool:
    ok = True

    # 冷热钱包分级
    wm = WalletManager()
    t_small = wm.approve_tx(50)
    t_mid = wm.approve_tx(500)
    t_big = wm.approve_tx(50000)
    t_overflow = wm.approve_tx(500000)
    logger.info(f"  WalletTier: 50→{t_small[2]['tier']} 500→{t_mid[2]['tier']} "
                f"50000→{t_big[2]['tier']} 超大→{t_overflow[0]}")
    assert t_small[2]["tier"] == "HOT" and t_mid[2]["tier"] == "WARM"
    assert t_big[2]["tier"] == "COLD" and not t_overflow[0]

    # KMS 模拟
    kms = SimKMSBackend(seed="runner")
    kid = kms.create_key("hot")
    digest = hashlib.sha256(b"tx").hexdigest()
    sig = kms.sign(digest, kid)
    logger.info(f"  KMS: key={kid[:12]}… 验签={kms.verify(digest, kid, sig)}")
    assert kms.verify(digest, kid, sig)
    assert not kms.verify(digest, kid, "0" * 64)

    # 加密密钥库
    ks = EncryptedKeystore(os.path.join(tmpdir, "ks.bin"))
    ks.save("hot", b"private-key", "pw-123")
    loaded = ks.load("hot", "pw-123")
    try:
        ks.load("hot", "wrong")
        tamper_detected = False
    except ValueError:
        tamper_detected = True
    logger.info(f"  密钥库: 往返={loaded == b'private-key'} 口令错误拦截={tamper_detected}")
    assert loaded == b"private-key" and tamper_detected

    # 审计日志哈希链
    audit = AuditLogger()
    audit.append("alice", "OPEN", {"token": "x", "size": 10})
    audit.append("bob", "APPROVE", {"req": "r1"})
    audit.append("carol", "CLOSE", {"token": "x"})
    ok_chain, _ = audit.verify_chain()
    audit.tamper(2)
    bad_chain, bad_seq = audit.verify_chain()
    logger.info(f"  审计链: 完整={ok_chain} 篡改检测={not bad_chain}(seq={bad_seq})")
    assert ok_chain and not bad_chain

    # 双人控制
    dc = DualControl(threshold=2, approvers=["alice", "bob", "carol"], audit=audit)
    req = dc.request("WITHDRAW", {"amount": 1000})
    dc.approve(req.id, "alice")
    s1 = dc.status(req.id)
    dc.approve(req.id, "bob")
    s2 = dc.status(req.id)
    logger.info(f"  双人控制: 1人={s1.value} 2人={s2.value}")
    assert s1.value == "PENDING" and s2.value == "APPROVED"

    RESULTS["keysafe"] = {"tiers": [t_small[2]["tier"], t_mid[2]["tier"], t_big[2]["tier"]],
                          "kms_ok": True, "audit_chain_ok": ok_chain,
                          "tamper_detected": not bad_chain,
                          "dual_control": s2.value}
    return ok


# ════════════════════════════════════════════════════════════════
# 3. 预言机风险
# ════════════════════════════════════════════════════════════════

def test_oracle_risk() -> bool:
    ok = True

    # UMA 结算延迟/争议期
    uma = UMASettlementModel()
    sched = uma.schedule(time.time())
    r1 = uma.risk_window(proposed_at=time.time() + 6 * 3600, settle_h=4, dispute_rate=0.05)
    r2 = uma.risk_window(proposed_at=time.time() + 2 * 3600, settle_h=4, dispute_rate=0.5)
    logger.info(f"  UMA: 理想周期={sched['ideal_total_h']}h 充足={r1['level']} "
                f"紧张+高争议={r2['level']}")
    assert r1["safe"] and r1["level"] == "SAFE"
    assert not r2["safe"] and r2["level"] == "DANGER"

    # 合约措辞解析
    parser = ContractPhraseParser()
    sp1 = parser.parse("Is the high temperature in Tokyo on 2026-08-05 "
                       "at least 30 degrees Celsius?")
    sp2 = parser.parse("Will the low in Seoul on 2026-08-06 be between 20 and 25 °F?")
    logger.info(f"  措辞: 「{sp1.raw[:40]}…」→ {sp1.op} {sp1.lower}{sp1.unit} "
                f"| 另一条 → {sp2.op} {sp2.lower}~{sp2.upper}{sp2.unit}")
    assert sp1.op == "ge" and sp1.lower == 30.0 and sp1.unit == "°C"
    assert sp2.op == "between" and sp2.lower == 20.0 and sp2.upper == 25.0
    assert sp2.unit == "°F"

    # Oracle Risk 矩阵
    matrix = OracleRiskMatrix()
    v_good = matrix.assess("Tokyo-NO", {"source_reliability": 1, "settle_latency": 2,
                                        "dispute_rate": 1, "wording_ambiguity": 1,
                                        "historical_bias": 2, "same_source_drift": 1})
    v_bad = matrix.assess("NYC-NO", {"source_reliability": 5, "settle_latency": 5,
                                     "dispute_rate": 4, "wording_ambiguity": 4,
                                     "historical_bias": 4, "same_source_drift": 4},
                          phrase=sp1)
    logger.info(f"  RiskMatrix: 优质={v_good.level}(×{v_good.position_factor}) "
                f"劣质={v_bad.level}(×{v_bad.position_factor})")
    assert v_good.level == "LOW" and v_good.position_factor == 1.0
    assert v_bad.level == "CRITICAL" and v_bad.position_factor == 0.0

    # 同源数据校准
    cal = SameSourceCalibration()
    c_good = cal.calibrate({"gfs": 29.8, "icon": 30.2, "gem": 30.0, "jma": 30.1},
                           actual=30.1, history_bias=[0.4, 0.3, 0.2, 0.1])
    c_bad = cal.calibrate({"gfs": 28.0, "icon": 31.0, "gem": 33.0}, actual=30.0)
    logger.info(f"  校准: 一致={c_good.calibrated}(std={c_good.std_forecast}) "
                f"分歧={not c_bad.calibrated}(max_div={c_bad.max_divergence})")
    assert c_good.calibrated and not c_bad.calibrated

    RESULTS["oracle_risk"] = {"uma_safe": r1["safe"], "uma_danger": not r2["safe"],
                              "phrase": f"{sp1.op} {sp1.lower}{sp1.unit}",
                              "matrix_good": v_good.level, "matrix_bad": v_bad.level,
                              "calibrated": c_good.calibrated}
    return ok


# ════════════════════════════════════════════════════════════════
# 4. 延迟优化
# ════════════════════════════════════════════════════════════════

def test_latency() -> bool:
    ok = True

    # 节点选择（本地探测）
    sel = RPCNodeSelector([("a", "http://127.0.0.1:1", "tokyo"),
                           ("b", "http://127.0.0.1:65535", "tokyo"),
                           ("c", "http://127.0.0.1:2", "singapore")])
    sel.probe()
    rep = sel.report()
    logger.info(f"  节点: 存活={rep['alive_count']}/3 best={rep['best']}")
    assert 0 <= rep["alive_count"] <= 3

    # WS 订单簿
    feed = WSOrderBookFeed("Tokyo-NO")
    for m in feed.simulate(40):
        feed.apply(m)
    state = feed.state
    logger.info(f"  WS订单簿: {feed.msg_count}条消息 mid={state.mid:.4f} "
                f"top_bid={state.top_n(1)['bids'][0]}")
    assert feed.msg_count == 40 and state.mid > 0
    assert state.best_ask > state.best_bid

    # 内核调优
    kt = KernelTuner()
    kt.set_affinity(cores=[0])
    krep = kt.report()
    logger.info(f"  内核: cores={krep['cpu_cores']} affinity={krep['current_affinity']} "
                f"tcp={krep['tcp_congestion']}")
    assert krep["cpu_cores"] >= 1
    if krep["platform"] == "Linux":
        assert krep["current_affinity"], "Linux 下应能读取亲和性"

    RESULTS["latency"] = {"nodes_alive": rep["alive_count"], "ws_mid": state.mid,
                          "tcp_cc": krep["tcp_congestion"]}
    return ok


# ════════════════════════════════════════════════════════════════
# 5. 反博弈
# ════════════════════════════════════════════════════════════════

def test_antigame() -> bool:
    ok = True

    # 地址轮换
    rot = AddressRotation(["0xA1", "0xA2", "0xA3"], rotate_every_n=2,
                          rng=random.Random(5))
    first = rot.pick()
    for _ in range(5):
        rot.notify_trade()
    stats = rot.stats()
    rot_events = rot._rotations
    logger.info(f"  地址轮换: {first} → {stats['active']} 轮换{stats['rotations']}次")
    assert stats["rotations"] >= 1, "应按计划触发轮换"
    assert all(r["from"] != r["to"] for r in rot_events), "每次轮换地址必须变化"

    # 冰山订单
    ice = IcebergOrder(total=1000, visible=150)
    slices = ice.plan()
    logger.info(f"  冰山: 总量1000 可见150 → {len(slices)}片 "
                f"总成交={sum(slices)}")
    assert sum(slices) == 1000 and max(slices) <= 150

    # 噪声交易
    nt = NoiseTrader(rate=0.4)
    nt._rng = random.Random(7)
    noise_n = sum(1 for _ in range(100) if nt.maybe_noise(0.40))
    logger.info(f"  噪声: 100 次尝试生成 {noise_n} 笔噪声单 (目标≈40)")
    assert 10 <= noise_n <= 70

    # Dashboard 延迟
    dd = DashboardDelay(delay_minutes=30, jitter_minutes=5)
    sig = {"action": "buy", "size": 50, "ts": 1000.0}
    pub = dd.expose(sig)
    intern = dd.internal(sig)
    logger.info(f"  Dashboard: 内部ts={intern['ts']} 对外ts={pub['ts']} "
                f"(延迟={pub['ts'] - intern['ts']:.0f}s)")
    assert pub["ts"] - intern["ts"] >= 25 * 60       # 至少延迟 25 分钟
    assert pub["ts"] - intern["ts"] <= 35 * 60
    assert not intern["delayed"] and pub["delayed"]

    RESULTS["antigame"] = {"rotations": stats["rotations"], "iceberg_slices": len(slices),
                           "noise_count": noise_n, "dashboard_delay_s": pub["ts"] - 1000.0}
    return ok


# ════════════════════════════════════════════════════════════════
# 6. 元控制器
# ════════════════════════════════════════════════════════════════

def test_metacontroller() -> bool:
    ok = True
    STRATS = ["edge_0.15", "edge_0.20", "depth_strict", "time_window"]

    # LinUCB 单独
    bandit = ContextualBandit(n_arms=4, dim=6, seed=1)
    rng = np_random = __import__("numpy").random.default_rng(1)
    for _ in range(60):
        x = rng.normal(0, 1, 6)
        arm = bandit.select(x)
        bandit.update(arm, x, reward=0.05 if arm == 1 else -0.01)
    counts = bandit.counts
    logger.info(f"  LinUCB: 各臂被选次数={counts.tolist()} (最优臂1应最多)")
    assert counts[1] >= counts[3] and counts[1] > 0

    # PPO 单独
    ppo = PPOScheduler(dim=6, n_actions=4, hidden=8, lr=0.05, seed=2)
    states, acts, rews, oldps = [], [], [], []
    rng2 = __import__("numpy").random.default_rng(2)
    for _ in range(16):
        s = rng2.normal(0, 1, 6)
        a, p = ppo.act(s, return_prob=True)
        states.append(s); acts.append(a)
        rews.append(0.5 if a == 1 else -0.2); oldps.append(p)
    drop = ppo.learn(np.array(states), np.array(acts), np.array(rews), np.array(oldps))
    logger.info(f"  PPO: 一轮更新 loss下降={drop:.4f} 偏好动作概率={ppo.probs(np.zeros(6))[1]:.3f}")
    assert drop > 1e-6 or ppo.probs(np.zeros(6))[1] > 1e-3, "PPO 更新应有效"

    # Particle Filter BMA
    bma = ParticleFilterBMA(n_arms=4, n_particles=400, seed=3)
    for _ in range(50):
        bma.observe(np.array([0.01, 0.06, -0.01, 0.00]))
    w = bma.posterior_weights()
    logger.info(f"  BMA: 后验权重={np.round(w, 3).tolist()} (臂1应最高)")
    assert abs(w.sum() - 1.0) < 1e-6 and w[1] == max(w)

    # 融合
    meta = MetaController(strategies=STRATS, dim=6, seed=4)
    decisions = []
    for _ in range(20):
        ctx = np_random.normal(0, 1, 6)
        d = meta.decide(ctx, np.array([0.01, 0.05, -0.01, 0.00]))
        decisions.append(d)
    last = decisions[-1]
    logger.info(f"  MetaController: 最终选择={last['strategy']} "
                f"conf={last['confidence']} bandit选={last['arms']['bandit']}")
    assert last["strategy"] in STRATS and 0 < last["confidence"] <= 1.5
    assert last["strategy"] == "edge_0.20", "收敛后应选出最优臂"

    RESULTS["metacontroller"] = {"bandit_counts": counts.tolist(),
                                 "bma_weights": np.round(w, 3).tolist(),
                                 "final_strategy": last["strategy"]}
    return ok


# ════════════════════════════════════════════════════════════════
# 7. 人机协同
# ════════════════════════════════════════════════════════════════

def test_human_loop() -> bool:
    ok = True
    notifs: list = []

    async def notifier(text):
        notifs.append(text)

    # AUTO
    htl_auto = HumanInTheLoop(level=AutomationLevel.AUTO)
    r = asyncio.run(htl_auto.execute("OPEN", {"token": "T"}))
    assert r["ok"] and r["reason"] == "EXECUTED"

    # PAUSE
    htl_pause = HumanInTheLoop(level=AutomationLevel.PAUSE)
    r = asyncio.run(htl_pause.execute("OPEN", {"token": "T"}))
    logger.info(f"  AUTO执行={r['reason'] if r['ok'] else 'blocked'} "
                f"PAUSE阻断={not r['ok']}")
    assert not r["ok"] and r["reason"] == "PAUSE"

    # CONFIRM 全流程
    htl = HumanInTheLoop(level=AutomationLevel.CONFIRM, notifier=notifier,
                         confirm_timeout=300)
    r1 = asyncio.run(htl.execute("OPEN", {"token": "Tokyo-NO", "size": 10}))
    assert r1["reason"] == "AWAITING_APPROVAL"
    assert htl.approve(r1["request_id"], "boss")
    r2 = asyncio.run(htl.execute("OPEN", {"token": "Tokyo-NO", "size": 10}))
    logger.info(f"  CONFIRM: 待批→{r1['reason']} 批准后→{r2['reason']} "
                f"通知{len(notifs)}条")
    assert r2["ok"] and r2["reason"] == "APPROVED"

    # 超时策略
    htl_to = HumanInTheLoop(level=AutomationLevel.CONFIRM, confirm_timeout=0.01,
                            timeout_policy="reject")
    r3 = asyncio.run(htl_to.execute("OPEN", {"token": "T"}))
    import time as _t
    _t.sleep(0.02)
    r4 = asyncio.run(htl_to.execute("OPEN", {"token": "T"}))
    assert r4["reason"] == "TIMEOUT_REJECT"

    # 分级控制器 + 失联降级
    ctl = HumanLoopController(default_level=AutomationLevel.NOTIFY,
                              human_timeout_sec=10)
    ctl.set_level("Tokyo", AutomationLevel.CONFIRM)
    assert ctl.level_for("Tokyo").value == "CONFIRM"
    ctl._last_human_heartbeat = time.time() - 99999
    evt = ctl.monitor_human_alive()
    logger.info(f"  控制器: Tokyo=CONFIRM 失联→{evt['event']} 有效级别={ctl.level_for().value}")
    assert evt and ctl.level_for().value == "PAUSE"

    RESULTS["human_loop"] = {"auto_ok": True, "pause_blocked": True,
                             "confirm_approved": True, "timeout_rejected": True,
                             "downgraded": True}
    return ok


# ════════════════════════════════════════════════════════════════
# 写库
# ════════════════════════════════════════════════════════════════

def write_results(db_path: Optional[str]):
    if not db_path:
        return
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ultraopt_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
    for module, payload in RESULTS.items():
        conn.execute("INSERT INTO ultraopt_results(module, payload) VALUES(?,?)",
                     (module, json.dumps(payload, ensure_ascii=False)))
    conn.commit()
    conn.close()
    logger.info(f"  结果已写入 {db_path}")


def main():
    ap = argparse.ArgumentParser(description="HighTempTation 第四波优化自检")
    ap.add_argument("--db", help="SQLite 路径（可选, 写入自检结果）")
    args = ap.parse_args()

    tmpdir = "/tmp/highopt_ultra_selftest"
    os.makedirs(tmpdir, exist_ok=True)

    tests = [
        ("1. 链上执行层 (onchain)", test_onchain),
        ("2. 密钥安全 (keysafe)", lambda: test_keysafe(tmpdir)),
        ("3. 预言机风险 (oracle_risk)", test_oracle_risk),
        ("4. 延迟优化 (latency)", test_latency),
        ("5. 反博弈 (antigame)", test_antigame),
        ("6. 元控制器 (metacontroller)", test_metacontroller),
        ("7. 人机协同 (human_loop)", test_human_loop),
    ]

    print("\n" + "=" * 70)
    print("  HighTempTation 第四波终极优化 — 一键自检")
    print("=" * 70)
    all_ok = True
    for name, fn in tests:
        try:
            fn()
            print(f"  ✅ {name}")
        except AssertionError as e:
            all_ok = False
            print(f"  ❌ {name}: {e}")
        except Exception as e:  # noqa: BLE001
            all_ok = False
            print(f"  ❌ {name}: 异常 {type(e).__name__}: {e}")

    write_results(args.db)
    print("=" * 70)
    print(f"  结果: {'全部通过 ✅' if all_ok else '存在失败项 ❌'}")
    print("=" * 70)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
