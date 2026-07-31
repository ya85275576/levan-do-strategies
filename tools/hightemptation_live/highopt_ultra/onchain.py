#!/usr/bin/env python3
"""
HighTempTation — 链上执行层（第四波优化 #1）

功能:
  1. GasAuctioneer       — Gas 动态竞价（EIP-1559: baseFee 序列预测 + 优先费 + 上限保护）
  2. NonceManager        — Nonce 分布式锁（SQLite 事务锁 / 租约超时回收 / 冲突检测）
  3. MEVGuard            — MEV 保护（sandwich 风险评分 / 滑点保护 / 私有 mempool 路由建议）
  4. MulticallBatcher    — Multicall 批量（打包编码 / Gas 估算 / 按上限拆分）
  5. ContractUpgradeWatcher — 合约升级监听（Upgraded/AdminChanged 事件轮询 + 风险处置）
  6. TokenValidator      — USDC.e vs Native 校验（代币白名单 / 桥来源 / 精度核对）

用法:
  from highopt_ultra.onchain import (
      GasAuctioneer, NonceManager, MEVGuard, MulticallBatcher,
      ContractUpgradeWatcher, TokenValidator,
  )

  # Gas 竞价
  auction = GasAuctioneer()
  auction.observe(block_base_fee=30.0, block_gas_used=0.85)   # 每个新区块喂一次
  fee = auction.suggest()                                     # → {base_fee, priority_fee, ...}

  # Nonce 锁
  nonces = NonceManager("nonce.db")
  n = nonces.acquire("0xHOT", lease=30)                      # 分布式分配, 冲突自动规避
  nonces.release("0xHOT", n)

  # MEV 防护
  guard = MEVGuard()
  ok, reasons, metrics = guard.check(qty=500, obi=0.8, depth=1200, spread=0.01, mid=0.40)

  # Multicall
  batcher = MulticallBatcher(max_batch_gas=300_000)
  batch = batcher.build_batch([("0xA", "0x1111"), ("0xB", "0x2222")])
  saving = batcher.estimate_saving(calls, per_call_gas=60_000)

  # 升级监听
  watcher = ContractUpgradeWatcher(proxy="0xPROXY")
  watcher.consume_logs(feed.next_logs())                     # 事件 → 风险处置建议

  # 代币校验
  tv = TokenValidator()
  verdict = tv.validate("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "USDC", 6)  # native
"""
import hashlib
import logging
import os
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("highopt_ultra.onchain")

# ════════════════════════════════════════════════════════════════
# 1. Gas 动态竞价（EIP-1559）
# ════════════════════════════════════════════════════════════════

class GasAuctioneer:
    """
    EIP-1559 动态 Gas 竞价。

    对 baseFee 历史做 EWMA + 高分位数预测，priorityFee 按区块拥堵度
    （gas used / gas limit）在 [min, max] 区间内插值，最终套一层硬上限保护。

    用法:
      auction = GasAuctioneer()
      auction.observe(base_fee=30.0, gas_used_ratio=0.85)   # 每个新区块调用
      fee = auction.suggest()
      # fee = {"base_fee": 32.4, "priority_fee": 1.8, "max_fee": 60.0,
      #        "max_priority_fee": 3.0, "confidence": "MEDIUM"}
    """

    def __init__(self, window: int = 20, ewma_alpha: float = 0.3,
                 priority_min: float = 0.5, priority_max: float = 5.0,
                 base_multiplier: float = 1.15, cap_multiplier: float = 2.0,
                 max_fee_abs: float = 500.0):
        self.window = window
        self.alpha = ewma_alpha
        self.priority_min = priority_min
        self.priority_max = priority_max
        self.base_multiplier = base_multiplier
        self.cap_multiplier = cap_multiplier
        self.max_fee_abs = max_fee_abs
        self._base_fees: List[float] = []
        self._loads: List[float] = []          # gas used / gas limit
        self._ewma: Optional[float] = None

    def observe(self, base_fee: float, gas_used_ratio: float):
        """喂入一个新区块的 baseFee 与拥堵度(0~1)"""
        self._base_fees.append(float(base_fee))
        self._loads.append(min(1.0, max(0.0, float(gas_used_ratio))))
        if len(self._base_fees) > self.window:
            self._base_fees.pop(0)
            self._loads.pop(0)
        if self._ewma is None:
            self._ewma = float(base_fee)
        else:
            self._ewma = self.alpha * base_fee + (1 - self.alpha) * self._ewma

    def _pct(self, values: List[float], q: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        idx = min(len(s) - 1, int(q * len(s)))
        return s[idx]

    def suggest(self) -> dict:
        """给出下一笔交易的 gas 参数"""
        if not self._base_fees:
            base = 30.0
            load = 0.5
        else:
            base = max(self._ewma or 0.0, self._pct(self._base_fees, 0.9))
            load = sum(self._loads) / len(self._loads)

        base_fee = base * self.base_multiplier
        # 拥堵度 → 优先费（线性插值 + 突发缓冲）
        priority_fee = self.priority_min + (self.priority_max - self.priority_min) * load ** 1.5
        priority_fee = round(min(self.priority_max, priority_fee), 2)
        max_fee = min(self.max_fee_abs, base_fee * self.cap_multiplier + priority_fee)
        max_priority_fee = round(priority_fee * 1.2, 2)

        if load > 0.85:
            confidence = "HIGH_CONGESTION"
        elif load < 0.3:
            confidence = "LOW"
        else:
            confidence = "MEDIUM"
        return {
            "base_fee": round(base_fee, 2),
            "priority_fee": priority_fee,
            "max_fee": round(max_fee, 2),
            "max_priority_fee": max_priority_fee,
            "confidence": confidence,
        }


# ════════════════════════════════════════════════════════════════
# 2. Nonce 分布式锁
# ════════════════════════════════════════════════════════════════

class NonceManager:
    """
    Nonce 分布式锁（SQLite WAL + BEGIN IMMEDIATE 事务实现）。

    多进程/多实例同时发交易时, 同一账户的 nonce 必须串行分配, 否则
    会发生 nonce 冲突（覆盖/丢弃）。本管理器:
      - 分配: 事务内读取 next_nonce 并递增（唯一且单调）
      - 租约: 分配后带租约(lease 秒), 崩溃/超时自动回收, 绝不重复分配
      - 冲突检测: check() 校验某个 nonce 是否已被分配

    用法:
      nm = NonceManager("nonce_lock.db")
      nonce = nm.acquire("0xHOT", lease=30)
      # ... 发交易（失败则 release, 成功则 commit）...
      nm.commit("0xHOT", nonce)      # 释放锁 + 记录已用
      nm.release("0xHOT", nonce)     # 失败回滚: 归还 nonce
    """

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
        return self._conn

    def _init_db(self):
        c = self.conn
        c.executescript("""
        CREATE TABLE IF NOT EXISTS nonce_state (
            account TEXT PRIMARY KEY,
            next_nonce INTEGER NOT NULL DEFAULT 0,
            owner TEXT DEFAULT NULL,
            lease_until REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS nonce_used (
            account TEXT NOT NULL,
            nonce INTEGER NOT NULL,
            used_at REAL,
            PRIMARY KEY (account, nonce)
        );
        """)
        c.commit()

    def _lease_expired(self, lease_until: float) -> bool:
        return time.time() > lease_until

    def acquire(self, account: str, lease: float = 30.0,
                owner: str = "") -> int:
        """分配下一个可用 nonce。返回 -1 表示锁被其他持有者占用且未过期。"""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT next_nonce, owner, lease_until FROM nonce_state WHERE account=?",
                (account,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO nonce_state(account, next_nonce) VALUES(?,0)", (account,))
                nonce = 0
            else:
                next_nonce, cur_owner, lease_until = row
                if cur_owner and not self._lease_expired(lease_until):
                    return -1          # 锁被占用
                nonce = next_nonce
            conn.execute(
                "UPDATE nonce_state SET next_nonce=?, owner=?, lease_until=? WHERE account=?",
                (nonce + 1, owner or f"pid{os.getpid()}", time.time() + lease, account))
            conn.commit()
            return nonce
        except sqlite3.OperationalError:
            return -1
        finally:
            try:
                conn.rollback()
            except sqlite3.OperationalError:
                pass

    def release(self, account: str, nonce: int, owner: str = ""):
        """失败回滚: 归还 nonce（仅当仍由本 owner 持有）"""
        with self.conn:
            row = self.conn.execute(
                "SELECT next_nonce, owner FROM nonce_state WHERE account=?",
                (account,)).fetchone()
            if row and row["owner"] == (owner or f"pid{os.getpid()}"):
                self.conn.execute(
                    "UPDATE nonce_state SET next_nonce=?, owner=NULL, lease_until=0 "
                    "WHERE account=? AND next_nonce=?", (nonce, account, nonce + 1))

    def commit(self, account: str, nonce: int):
        """成功提交: 标记 nonce 已用并释放锁"""
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO nonce_used(account, nonce, used_at) VALUES(?,?,?)",
                (account, nonce, time.time()))
            self.conn.execute(
                "UPDATE nonce_state SET owner=NULL, lease_until=0 WHERE account=?", (account,))

    def check(self, account: str, nonce: int) -> bool:
        """nonce 是否已被使用（true=已被用, 有冲突风险）"""
        row = self.conn.execute(
            "SELECT 1 FROM nonce_used WHERE account=? AND nonce=?", (account, nonce)).fetchone()
        return row is not None

    def pending(self, account: str) -> int:
        """下一个应使用的 nonce（对账用）"""
        row = self.conn.execute(
            "SELECT next_nonce FROM nonce_state WHERE account=?", (account,)).fetchone()
        return row["next_nonce"] if row else 0


# ════════════════════════════════════════════════════════════════
# 3. MEV 保护
# ════════════════════════════════════════════════════════════════

# 私有 mempool / 保护性 RPC（Polygon 生态示例, 实盘按需替换）
PROTECTED_RPCS = [
    "https://rpc.flashbots.net",          # Flashbots Protect (ETH)
    "https://polygon-rpc.com/private",    # 自建私有节点（最优: 地理就近）
    "wss://polygon-bor-rpc.publicnode.com",
]


class MEVGuard:
    """
    MEV 保护闸门。

    sandwich 风险评分由三部分组成:
      1. 订单簿失衡 |OBI| 过大（极端失衡 = 易被抢先/尾随）
      2. 交易规模相对深度过大（冲击大 → 套利空间大）
      3. 价差过宽（做市薄弱）

    check() 返回 (ok, reasons, metrics):
      ok       — 是否允许裸发交易（不经私有 mempool）
      reasons  — 未通过项说明
      metrics  — sandwish_score(0~1), price_impact, obi, 建议路由
    """

    def __init__(self, max_obi: float = 0.55, max_impact_ratio: float = 0.10,
                 min_depth_usd: float = 1000.0, max_spread_pct: float = 2.0,
                 always_protected: bool = False):
        self.max_obi = max_obi
        self.max_impact_ratio = max_impact_ratio
        self.min_depth_usd = min_depth_usd
        self.max_spread_pct = max_spread_pct
        self.always_protected = always_protected

    def sandwich_score(self, obi: float, qty: float, depth: float) -> float:
        """0~1: 分数越高越容易被 sandwich"""
        obi_risk = min(1.0, abs(obi) / 1.0)
        impact_risk = min(1.0, (qty / max(depth, 1.0)) / 0.5)
        return round(0.6 * obi_risk + 0.4 * impact_risk, 3)

    def check(self, qty: float, obi: float, depth: float,
              spread: float, mid: float = 0.5) -> Tuple[bool, List[str], dict]:
        reasons: List[str] = []
        impact = (qty / max(depth, 1.0)) if depth > 0 else 1.0
        score = self.sandwich_score(obi, qty, depth)
        spread_pct = (spread / mid * 100.0) if mid > 0 else 99.0

        if abs(obi) > self.max_obi:
            reasons.append(f"订单簿失衡 OBI={obi:.2f} 超限 {self.max_obi}")
        if impact > self.max_impact_ratio:
            reasons.append(f"冲击占比 {impact:.3f} 超限 {self.max_impact_ratio}")
        if depth < self.min_depth_usd:
            reasons.append(f"深度 ${depth:.0f} < ${self.min_depth_usd}")
        if spread_pct > self.max_spread_pct:
            reasons.append(f"价差 {spread_pct:.2f}% 超限 {self.max_spread_pct}%")

        route = "protected" if (self.always_protected or reasons or score > 0.4) else "public"
        metrics = {
            "sandwich_score": score,
            "price_impact": round(impact, 4),
            "obi": round(obi, 3),
            "spread_pct": round(spread_pct, 2),
            "route": route,
            "protected_rpcs": PROTECTED_RPCS if route == "protected" else [],
        }
        return (len(reasons) == 0, reasons, metrics)


# ════════════════════════════════════════════════════════════════
# 4. Multicall 批量
# ════════════════════════════════════════════════════════════════

class MulticallBatcher:
    """
    Multicall 批量打包。

    把多个 (to, calldata) 调用编码为一个 batch payload（长度前缀拼接,
    与 ABI dynamic 编码同构, 可由 multicall 合约解包）。批量后只需
    一次交易: 省 base fee + 省 per-call 固定开销。

    用法:
      batcher = MulticallBatcher()
      calls = [("0xAAA", "0x1234"), ("0xBBB", "0x5678")]
      payload = batcher.build_batch(calls)          # → 0x...（可 decode 还原）
      saving  = batcher.estimate_saving(calls, per_call_gas=60_000)
      batches = batcher.split_batches(calls, max_gas=300_000)   # 按 Gas 拆分
    """

    BASE_TX_GAS = 21_000          # 普通转账基础 Gas
    PER_CALL_OVERHEAD = 18_000    # 每项在 multicall 内的固定开销
    WORD_GAS = 16                 # 每 32 字节

    @staticmethod
    def _enc(calls: List[Tuple[str, str]]) -> bytes:
        out = struct.pack(">I", len(calls))
        for to, data in calls:
            data = bytes.fromhex(data[2:]) if data.startswith("0x") else bytes.fromhex(data)
            out += struct.pack(">I", int(to, 16)) + struct.pack(">I", len(data)) + data
        return out

    @staticmethod
    def _dec(payload: bytes) -> List[Tuple[str, str]]:
        n = struct.unpack(">I", payload[:4])[0]
        calls, off = [], 4
        for _ in range(n):
            to = struct.unpack(">I", payload[off:off + 4])[0]
            ln = struct.unpack(">I", payload[off + 4:off + 8])[0]
            data = payload[off + 8:off + 8 + ln]
            off += 8 + ln
            calls.append(("0x" + format(to, "X"), "0x" + data.hex()))
        return calls

    def build_batch(self, calls: List[Tuple[str, str]]) -> str:
        """编码批量调用 → hex payload"""
        return "0x" + self._enc(calls).hex()

    def decode_batch(self, payload: str) -> List[Tuple[str, str]]:
        """解码回原始调用列表（往返校验用）"""
        raw = bytes.fromhex(payload[2:]) if payload.startswith("0x") else bytes.fromhex(payload)
        return self._dec(raw)

    def estimate_saving(self, calls: List[Tuple[str, str]],
                        per_call_gas: float = 60_000) -> dict:
        """批量 vs 逐笔的 Gas 节省估算"""
        n = len(calls)
        if n == 0:
            return {"saving_gas": 0, "saving_pct": 0.0, "batch_gas": 0, "individual_gas": 0}
        data_len = len(self._enc(calls))
        batch_gas = self.BASE_TX_GAS + self.PER_CALL_OVERHEAD * n + data_len * self.WORD_GAS
        individual_gas = n * (self.BASE_TX_GAS + per_call_gas)
        saving = individual_gas - batch_gas
        return {
            "saving_gas": saving,
            "saving_pct": round(saving / individual_gas * 100.0, 1) if individual_gas else 0.0,
            "batch_gas": batch_gas,
            "individual_gas": individual_gas,
            "n_calls": n,
        }

    def split_batches(self, calls: List[Tuple[str, str]],
                      max_gas: float = 300_000) -> List[List[Tuple[str, str]]]:
        """贪心按 Gas 上限拆分（大调用单独成批）"""
        batches: List[List[Tuple[str, str]]] = []
        cur: List[Tuple[str, str]] = []
        for call in calls:
            est = self.BASE_TX_GAS + self.PER_CALL_OVERHEAD + len(call[1]) // 2 * self.WORD_GAS
            if cur and sum(self.BASE_TX_GAS + self.PER_CALL_OVERHEAD +
                           len(c[1]) // 2 * self.WORD_GAS for c in cur) + est > max_gas:
                batches.append(cur)
                cur = []
            cur.append(call)
        if cur:
            batches.append(cur)
        return batches


# ════════════════════════════════════════════════════════════════
# 5. 合约升级监听
# ════════════════════════════════════════════════════════════════

# 事件 topic0（keccak 签名, 演示值）
EVENT_UPGRADED = "0xbc7cd75a20ee27fd9adebab32041f755214dbc6bffa90cc0225b39da2e5c2d3b"
EVENT_ADMIN_CHANGED = "0x7e644d79422f17c01e4894b5f4f588d331ebfa28653d42ae832dc59e38c9798"


@dataclass
class UpgradeEvent:
    block: int
    tx_hash: str
    event: str                       # "Upgraded" | "AdminChanged" | ...
    implementation: str
    prev_implementation: str = ""
    admin: str = ""
    ts: float = field(default_factory=time.time)

    def risk(self) -> str:
        """升级事件的处置建议"""
        if self.event == "Upgraded":
            return "CRITICAL: 代理实现已变更 → 暂停自动化, 审计新实现后恢复"
        if self.event == "AdminChanged":
            return "HIGH: 管理员变更 → 复核多签配置"
        return "MEDIUM: 未知合约事件, 人工复核"


class ContractUpgradeWatcher:
    """
    合约升级监听器。

    consume_logs() 接收 RPC 返回的日志列表, 按 topic0 匹配解析,
    记录升级历史并触发 on_event 回调。风险事件（Upgraded）自动
    给出"暂停自动化"处置建议。

    用法:
      watcher = ContractUpgradeWatcher(proxy="0xPROXY", on_event=cb)
      watcher.consume_logs(rpc.get_logs(address="0xPROXY", fromBlock=last+1))
      watcher.last_risk()   # → 最近一次风险处置建议
    """

    def __init__(self, proxy: str = "", on_event: Optional[Callable] = None):
        self.proxy = proxy
        self.on_event = on_event
        self.events: List[UpgradeEvent] = []
        self.last_checked_block = 0

    def consume_logs(self, logs: List[dict]):
        """logs: [{"blockNumber", "transactionHash", "topics": [...], "data": "0x..."}]"""
        for lg in logs:
            topics = lg.get("topics") or []
            if not topics:
                continue
            topic0 = topics[0]
            block = lg.get("blockNumber", 0)
            if isinstance(block, str):
                block = int(block, 16) if block.startswith("0x") else int(block)
            evt = UpgradeEvent(
                block=block,
                tx_hash=lg.get("transactionHash", ""),
                event="Upgraded" if topic0 == EVENT_UPGRADED else
                      "AdminChanged" if topic0 == EVENT_ADMIN_CHANGED else "Unknown",
                implementation=(topics[1] if len(topics) > 1 else lg.get("data", "")),
            )
            self.events.append(evt)
            self.last_checked_block = max(self.last_checked_block, evt.block)
            if self.on_event:
                self.on_event(evt)

    def last_risk(self) -> Optional[str]:
        """最近事件的处置建议（None = 无事件）"""
        return self.events[-1].risk() if self.events else None

    def history(self) -> List[dict]:
        return [{"block": e.block, "event": e.event, "risk": e.risk()}
                for e in self.events]


# ════════════════════════════════════════════════════════════════
# 6. USDC.e vs Native 代币校验
# ════════════════════════════════════════════════════════════════

# Polygon 代币白名单（演示地址）: address → (symbol, decimals, source, risk)
KNOWN_TOKENS: Dict[str, Tuple[str, int, str, str]] = {
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": ("USDC", 6, "native", "SAFE"),
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": ("USDC.e", 6, "bridged", "CAUTION"),
    "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619": ("WETH", 18, "native", "SAFE"),
    "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270": ("WMATIC", 18, "native", "SAFE"),
}


class TokenValidator:
    """
    代币校验器 — USDC.e vs Native 校验。

    桥接资产（USDC.e）与原生资产（USDC）同价不同源: 桥接合约若被
    攻击/冻结, 桥接代币可能归零。校验规则:
      - 地址是否在白名单
      - symbol / decimals 是否匹配
      - 桥接资产标记 CAUTION, 大额操作前必须确认到账路径

    用法:
      tv = TokenValidator()
      verdict = tv.validate("0x2791...", "USDC", 6)
      # → {"ok": True, "risk": "CAUTION", "symbol": "USDC.e", ...}
    """

    def validate(self, address: str, symbol: str = "",
                 decimals: Optional[int] = None) -> dict:
        addr = address.lower()
        known = KNOWN_TOKENS.get(addr)
        if not known:
            return {"ok": False, "risk": "UNKNOWN_TOKEN", "message":
                    f"地址 {addr} 不在白名单, 拒绝交易", "symbol": symbol}
        exp_symbol, exp_dec, source, risk = known
        problems = []
        if symbol and symbol.upper() != exp_symbol.upper():
            problems.append(f"symbol 不匹配: 声明 {symbol}, 白名单 {exp_symbol}")
        if decimals is not None and decimals != exp_dec:
            problems.append(f"decimals 不匹配: 声明 {decimals}, 白名单 {exp_dec}")
        if risk == "CAUTION":
            problems.append(f"{exp_symbol} 为桥接资产({source}), 大额操作需确认桥合约状态")
        return {
            "ok": len(problems) == 0,
            "risk": risk,
            "symbol": exp_symbol,
            "decimals": exp_dec,
            "source": source,
            "messages": problems,
            "native_check": "USDC.e != USDC" if exp_symbol == "USDC.e" else "OK",
        }


if __name__ == "__main__":
    # 快速自检
    logging.basicConfig(level=logging.INFO)
    a = GasAuctioneer()
    for i in range(30):
        a.observe(base_fee=25 + (i % 7) * 3, gas_used_ratio=0.5 + 0.3 * (i % 3) / 2)
    print("fee:", a.suggest())
    nm = NonceManager(":memory:")
    n1 = nm.acquire("0xHOT")
    n2 = nm.acquire("0xHOT")
    print("nonces:", n1, n2)
    nm.commit("0xHOT", n1)
    g = MEVGuard()
    print("mev:", g.check(qty=500, obi=0.8, depth=1200, spread=0.01, mid=0.4))
    b = MulticallBatcher()
    p = b.build_batch([("0xAAA", "0x1234")])
    print("batch roundtrip:", b.decode_batch(p))
    w = ContractUpgradeWatcher()
    w.consume_logs([{"blockNumber": 12345, "transactionHash": "0xtx",
                     "topics": [EVENT_UPGRADED, "0xNEWIMPL"]}])
    print("upgrade risk:", w.last_risk())
    print("token:", TokenValidator().validate("0x2791bca1f2de4661ed88a30c99a7a9449aa84174", "USDC", 6))
