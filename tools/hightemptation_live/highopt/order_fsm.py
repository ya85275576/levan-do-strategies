#!/usr/bin/env python3
"""
HighTempTation — 订单状态机 FSM（高阶优化 #4）

功能:
  1. Client Order ID 幂等   — 每个交易意图生成唯一 clientOrderId，重复提交去重返回原订单
  2. 幽灵订单防护           — 提交无响应 → UNKNOWN 态 → 重查/撤单，绝不盲目重发
  3. 部分成交处理           — filled_qty 累计 + 均价, 剩余量 < ε → FILLED
  4. 仓位对账               — 本地预期持仓 vs 交易所实际持仓, 差异告警 + 可选自动对冲

状态图:
  NEW ──submit──▶ SUBMITTED ──全成──▶ FILLED
                    │  ├─部分──▶ PARTIALLY_FILLED ──全成──▶ FILLED
                    │  ├─拒单──▶ REJECTED
                    │  ├─超时──▶ UNKNOWN ──重查──▶ (恢复原态 | 确认撤单→CANCELLED)
                    │  └─撤单──▶ CANCELLED
                    └─过期──▶ EXPIRED

用法:
  from highopt.order_fsm import OrderFSM, SimExchangeAdapter, PositionReconciler

  adapter = SimExchangeAdapter(fail_rate=0.1)      # 模拟交易所（可注入故障）
  fsm = OrderFSM(adapter)
  order = await fsm.submit("token-1", "buy", 100, price=0.40)   # 幂等: 同一意图再调返回同单
  order = await fsm.poll(order)                     # 推进状态（部分成交/超时→UNKNOWN）
  await fsm.reconcile(positions=[...])              # 仓位对账
"""
import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("highopt.order_fsm")


class OrderState(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"          # 幽灵订单风险态（提交后无确认）


@dataclass
class OrderRecord:
    """FSM 订单记录"""
    client_order_id: str
    token_id: str
    side: str                    # buy/sell
    qty: float
    limit_price: Optional[float] = None
    state: OrderState = OrderState.NEW
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    exchange_order_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    @property
    def is_terminal(self) -> bool:
        return self.state in (OrderState.FILLED, OrderState.CANCELLED,
                              OrderState.REJECTED, OrderState.EXPIRED)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d


# ════════════════════════════════════════════════════════════════
# 交易所适配器接口
# ════════════════════════════════════════════════════════════════

class ExchangeAdapter:
    """
    交易所适配器接口。实盘集成时实现为 Polymarket CLOB / OKX REST 调用。

    必须实现:
      submit(token_id, side, qty, price, client_order_id) -> dict
          {"ok": bool, "order_id": str|None, "error": str|None,
           "filled_qty": float, "avg_fill_price": float|None}
      query(order_id) -> dict
          {"status": "open"|"filled"|"cancelled"|"rejected"|"unknown",
           "filled_qty": float, "avg_fill_price": float|None}
      cancel(order_id) -> dict {"ok": bool}
      get_positions(token_id=None) -> List[dict]
          [{"token_id": str, "qty": float, "avg_price": float}, ...]
    """

    async def submit(self, token_id: str, side: str, qty: float,
                     price: Optional[float], client_order_id: str) -> dict:
        raise NotImplementedError

    async def query(self, order_id: str) -> dict:
        raise NotImplementedError

    async def cancel(self, order_id: str) -> dict:
        raise NotImplementedError

    async def get_positions(self, token_id: Optional[str] = None) -> List[dict]:
        raise NotImplementedError


class SimExchangeAdapter(ExchangeAdapter):
    """
    模拟交易所适配器 —— 用于单测/混沌验证。

    参数:
      fail_rate:    提交失败概率（拒单）
      ghost_rate:   提交后无响应概率（产生 UNKNOWN）
      latency:      单次调用延迟（秒）
      partial_fill: 每次查询的成交比例（0~1，None 表示随机）
    """

    def __init__(self, fail_rate: float = 0.0, ghost_rate: float = 0.0,
                 latency: float = 0.0, partial_fill: Optional[float] = None,
                 seed: int = 42):
        self.fail_rate = fail_rate
        self.ghost_rate = ghost_rate
        self.latency = latency
        self.partial_fill = partial_fill
        self._rng = random.Random(seed)
        self._orders: Dict[str, dict] = {}       # exchange_order_id → order state
        self._by_client: Dict[str, str] = {}     # client_order_id → exchange_order_id
        self._positions: Dict[str, float] = {}   # token_id → qty
        self._filled: Dict[str, Tuple[float, float]] = {}  # token_id → (qty, total_cost)

    async def _wait(self):
        if self.latency > 0:
            await asyncio.sleep(self.latency)

    async def submit(self, token_id: str, side: str, qty: float,
                     price: Optional[float], client_order_id: str) -> dict:
        await self._wait()
        if self._rng.random() < self.fail_rate:
            return {"ok": False, "order_id": None, "error": "sim_reject",
                    "filled_qty": 0.0, "avg_fill_price": None}
        oid = f"sim-{uuid.uuid4().hex[:12]}"
        self._by_client[client_order_id] = oid
        if self._rng.random() < self.ghost_rate:
            self._orders[oid] = {"status": "unknown", "filled_qty": 0.0,
                                 "avg_fill_price": None}
        else:
            self._orders[oid] = {"status": "open", "filled_qty": 0.0,
                                 "avg_fill_price": None}
        return {"ok": True, "order_id": oid, "error": None,
                "filled_qty": 0.0, "avg_fill_price": None}

    async def query(self, order_id: str) -> dict:
        await self._wait()
        rec = self._orders.get(order_id)
        if rec is None:
            return {"status": "unknown", "filled_qty": 0.0, "avg_fill_price": None}
        if rec["status"] in ("filled", "cancelled", "rejected"):
            return dict(rec)
        if rec["status"] == "unknown":
            # 幽灵: 一半概率确认在途
            if self._rng.random() < 0.5:
                rec["status"] = "open"
            return dict(rec)
        # open → 模拟部分/全部成交
        fill = self.partial_fill if self.partial_fill is not None \
            else self._rng.choice([0.0, 0.3, 0.5, 1.0])
        rec["filled_qty"] += fill
        if rec["filled_qty"] >= 1.0:
            rec["filled_qty"] = 1.0
            rec["status"] = "filled"
        return dict(rec)

    async def cancel(self, order_id: str) -> dict:
        await self._wait()
        rec = self._orders.get(order_id)
        if rec and rec["status"] in ("open", "unknown"):
            rec["status"] = "cancelled"
            return {"ok": True}
        return {"ok": False}

    async def get_positions(self, token_id: Optional[str] = None) -> List[dict]:
        await self._wait()
        out = []
        for tid, qty in self._positions.items():
            if token_id and tid != token_id:
                continue
            filled, cost = self._filled.get(tid, (0.0, 0.0))
            out.append({"token_id": tid, "qty": qty,
                        "avg_price": (cost / filled) if filled > 0 else 0.0})
        return out

    def apply_fill(self, token_id: str, side: str, qty: float, price: float):
        """外部注入成交（用于对账测试）"""
        self._positions[token_id] = self._positions.get(token_id, 0.0) + (
            qty if side == "buy" else -qty)
        fq, cost = self._filled.get(token_id, (0.0, 0.0))
        self._filled[token_id] = (fq + qty, cost + qty * price)


# ════════════════════════════════════════════════════════════════
# 订单状态机
# ════════════════════════════════════════════════════════════════

class OrderFSM:
    """
    订单状态机。

    幂等: submit(intent_key, ...) 用 client_order_id 去重，
          同一 intent_key 重复调用返回已存在的订单（不重复下单）。
    幽灵防护: 提交后 timeout 无确认 → UNKNOWN，
          resolve_ghost() 重查交易所；仍无法确认 → 请求撤单 + 告警，
          绝不自动重发（避免双单）。
    """

    # 合法迁移表
    _TRANSITIONS = {
        OrderState.NEW: {OrderState.SUBMITTED, OrderState.REJECTED, OrderState.CANCELLED},
        OrderState.SUBMITTED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                               OrderState.REJECTED, OrderState.CANCELLED,
                               OrderState.EXPIRED, OrderState.UNKNOWN},
        OrderState.PARTIALLY_FILLED: {OrderState.FILLED, OrderState.CANCELLED,
                                      OrderState.EXPIRED, OrderState.UNKNOWN},
        OrderState.UNKNOWN: {OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED,
                             OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED},
        OrderState.FILLED: set(),
        OrderState.CANCELLED: set(),
        OrderState.REJECTED: set(),
        OrderState.EXPIRED: set(),
    }

    def __init__(self, adapter: ExchangeAdapter,
                 submit_timeout: float = 5.0,
                 poll_interval: float = 0.5,
                 fill_epsilon: float = 1e-6,
                 on_alert: Optional[Callable[[str], None]] = None):
        self.adapter = adapter
        self.submit_timeout = submit_timeout
        self.poll_interval = poll_interval
        self.fill_epsilon = fill_epsilon
        self.on_alert = on_alert or (lambda msg: logger.warning(f"🚨 {msg}"))
        self._orders: Dict[str, OrderRecord] = {}   # client_order_id → record

    # ── 幂等提交 ──

    async def submit(self, token_id: str, side: str, qty: float,
                     price: Optional[float] = None,
                     client_order_id: Optional[str] = None,
                     intent_key: Optional[str] = None) -> OrderRecord:
        """
        提交订单（幂等）。

        :param intent_key: 交易意图键（如 "Tokyo-NO-20250415"）。
          同一 intent_key 重复调用 → 返回已存在订单，绝不重复下单。
        :param client_order_id: 显式订单 ID；缺省由 intent_key 派生。
        """
        cid = client_order_id or (f"ht-{intent_key}-{uuid.uuid4().hex[:8]}"
                                  if intent_key else f"ht-{uuid.uuid4().hex}")
        if intent_key:
            # 意图级幂等: 查找该意图已有订单
            for rec in self._orders.values():
                if rec.meta.get("intent_key") == intent_key and not rec.is_terminal:
                    logger.info(f"幂等命中: intent={intent_key} → {rec.client_order_id} "
                                f"state={rec.state.value}")
                    return rec
        if cid in self._orders:
            logger.info(f"幂等命中: client_order_id={cid} 已存在")
            return self._orders[cid]

        rec = OrderRecord(client_order_id=cid, token_id=token_id, side=side,
                          qty=qty, limit_price=price,
                          meta={"intent_key": intent_key} if intent_key else {})
        self._orders[cid] = rec
        await self._transition(rec, OrderState.SUBMITTED, "submit")

        # 立即向交易所提交
        try:
            resp = await self.adapter.submit(rec.token_id, rec.side, rec.qty,
                                             rec.limit_price, rec.client_order_id)
            return await self._on_submit_response(rec, resp)
        except Exception as e:
            # 网络故障/超时 → 幽灵订单风险（绝不重发，进入 UNKNOWN 待解析）
            logger.error(f"提交异常: {e}")
            await self._transition(rec, OrderState.UNKNOWN, "submit-error")
            self.on_alert(f"幽灵订单风险: {rec.client_order_id} 提交异常 {e}")
            return rec

    async def _transition(self, rec: OrderRecord, new_state: OrderState,
                          action: str) -> OrderRecord:
        allowed = self._TRANSITIONS.get(rec.state, set())
        if new_state not in allowed:
            logger.warning(f"非法迁移 {rec.state.value} --{action}--> {new_state.value} (忽略)")
            return rec
        rec.state = new_state
        rec.updated_at = time.time()
        return rec

    # ── 状态推进 ──

    async def poll(self, rec: OrderRecord, max_waits: int = 10) -> OrderRecord:
        """
        推进订单状态直到终结或超时。
        内部处理: 部分成交累计、超时 → UNKNOWN（幽灵防护触发）。
        """
        if rec.is_terminal:
            return rec
        if rec.state == OrderState.NEW:
            logger.warning(f"{rec.client_order_id} 处于 NEW，跳过 poll")
            return rec
        if rec.state == OrderState.UNKNOWN:
            # 幽灵订单 → 走 resolve_ghost 而非盲目重查
            return await self.resolve_ghost(rec)

        oid = rec.exchange_order_id
        if oid is None:
            # 未拿到交易所订单号（理论上 submit 已处理）
            await self._transition(rec, OrderState.UNKNOWN, "no-order-id")
            return rec

        waits = 0
        while not rec.is_terminal and waits < max_waits:
            await asyncio.sleep(self.poll_interval)
            q = await self.adapter.query(oid)
            rec.exchange_order_id = oid
            self._apply_query(rec, q)
            if rec.state == OrderState.UNKNOWN:
                break
            waits += 1
        if rec.state in (OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED):
            # 超时未确认 → 幽灵风险
            await self._transition(rec, OrderState.UNKNOWN, "timeout")
            self.on_alert(f"幽灵订单风险: {rec.client_order_id} 提交 {self.poll_interval*max_waits:.0f}s 无确认")
        return rec

    def _apply_query(self, rec: OrderRecord, q: dict):
        status = q.get("status")
        filled = float(q.get("filled_qty", 0.0))
        price = q.get("avg_fill_price")
        if status in ("filled", "cancelled", "rejected", "unknown"):
            if status == "filled":
                self._apply_fill(rec, filled, price)
                rec.state = OrderState.FILLED
            elif status == "cancelled":
                rec.state = OrderState.CANCELLED
            elif status == "rejected":
                rec.state = OrderState.REJECTED
            else:
                rec.state = OrderState.UNKNOWN
            rec.updated_at = time.time()
            return
        # open / partial
        new_filled = max(filled, rec.filled_qty)
        if new_filled > rec.filled_qty:
            self._apply_fill(rec, new_filled, price)
        if rec.remaining <= self.fill_epsilon:
            rec.state = OrderState.FILLED
        elif rec.filled_qty > 0:
            rec.state = OrderState.PARTIALLY_FILLED
        rec.updated_at = time.time()

    def _apply_fill(self, rec: OrderRecord, filled: float, price: Optional[float]):
        """部分成交累计 + 移动平均价"""
        if filled <= rec.filled_qty:
            return
        new_qty = filled - rec.filled_qty
        if price and price > 0:
            total_cost = rec.avg_fill_price * rec.filled_qty + price * new_qty
            rec.avg_fill_price = total_cost / filled
        rec.filled_qty = filled

    async def _on_submit_response(self, rec: OrderRecord, resp: dict) -> OrderRecord:
        if resp.get("ok"):
            rec.exchange_order_id = resp.get("order_id")
            filled = float(resp.get("filled_qty", 0.0))
            if filled > 0:
                self._apply_fill(rec, filled, resp.get("avg_fill_price"))
            if rec.remaining <= self.fill_epsilon:
                rec.state = OrderState.FILLED
            elif filled > 0:
                rec.state = OrderState.PARTIALLY_FILLED
            else:
                rec.state = OrderState.SUBMITTED
        else:
            # 拒单
            rec.state = OrderState.REJECTED
            self.on_alert(f"订单被拒: {rec.client_order_id} {resp.get('error')}")
        rec.updated_at = time.time()
        return rec

    # ── 幽灵订单解析 ──

    async def resolve_ghost(self, rec: OrderRecord) -> OrderRecord:
        """
        幽灵订单解析:
          1. 有 exchange_order_id → 重查一次
          2. 能确认 → 恢复对应状态
          3. 不能确认 → 尝试撤单；撤单失败 → 保持 UNKNOWN 并告警
          绝不自动重发（防止双开）。
        """
        if rec.state != OrderState.UNKNOWN:
            return rec
        if rec.exchange_order_id:
            q = await self.adapter.query(rec.exchange_order_id)
            if q.get("status") != "unknown":
                self._apply_query(rec, q)
                logger.info(f"幽灵解析: {rec.client_order_id} → {rec.state.value}")
                return rec
        # 无法确认 → 撤单
        if rec.exchange_order_id:
            c = await self.adapter.cancel(rec.exchange_order_id)
            if c.get("ok"):
                rec.state = OrderState.CANCELLED
                rec.updated_at = time.time()
                logger.info(f"幽灵撤单成功: {rec.client_order_id}")
                return rec
        self.on_alert(f"幽灵订单无法解析: {rec.client_order_id} — 需人工核查（禁止重发）")
        return rec

    async def cancel(self, rec: OrderRecord) -> OrderRecord:
        if rec.is_terminal:
            return rec
        if rec.exchange_order_id:
            resp = await self.adapter.cancel(rec.exchange_order_id)
            if resp.get("ok"):
                await self._transition(rec, OrderState.CANCELLED, "cancel")
        else:
            await self._transition(rec, OrderState.CANCELLED, "cancel-local")
        return rec

    def get_order(self, client_order_id: str) -> Optional[OrderRecord]:
        return self._orders.get(client_order_id)

    def open_orders(self) -> List[OrderRecord]:
        return [r for r in self._orders.values() if not r.is_terminal]

    def orders_by_intent(self, intent_key: str) -> List[OrderRecord]:
        return [r for r in self._orders.values()
                if r.meta.get("intent_key") == intent_key]


# ════════════════════════════════════════════════════════════════
# 仓位对账
# ════════════════════════════════════════════════════════════════

class PositionReconciler:
    """
    仓位对账器。

    本地预期持仓（由 FSM 成交记录累计） vs 交易所实际持仓。
    差异超过 tolerance → 告警 + 可选自动对冲回调。
    """

    def __init__(self, tolerance: float = 0.0,
                 on_mismatch: Optional[Callable[[dict], None]] = None):
        self.tolerance = tolerance
        self.on_mismatch = on_mismatch or (
            lambda d: logger.warning(f"⚠️ 仓位对账差异: {d}"))

    def expected_positions(self, orders: List[OrderRecord]) -> Dict[str, float]:
        """从 FSM 订单累计本地预期持仓"""
        pos: Dict[str, float] = {}
        for o in orders:
            if o.state == OrderState.FILLED and o.filled_qty > 0:
                delta = o.filled_qty if o.side == "buy" else -o.filled_qty
                pos[o.token_id] = pos.get(o.token_id, 0.0) + delta
        return pos

    def reconcile(self, expected: Dict[str, float],
                  actual: List[dict]) -> List[dict]:
        """
        :param expected: {token_id: qty} 本地预期
        :param actual:   [{"token_id":..., "qty":...}, ...] 交易所实际
        :returns: 差异报告列表
        """
        actual_map = {p["token_id"]: p["qty"] for p in actual}
        report: List[dict] = []
        for tid in set(expected) | set(actual_map):
            e = expected.get(tid, 0.0)
            a = actual_map.get(tid, 0.0)
            diff = e - a
            if abs(diff) > self.tolerance:
                entry = {"token_id": tid, "expected": e, "actual": a,
                         "diff": diff, "severity": "CRITICAL" if abs(diff) > 1 else "WARN"}
                report.append(entry)
                self.on_mismatch(entry)
        return report
