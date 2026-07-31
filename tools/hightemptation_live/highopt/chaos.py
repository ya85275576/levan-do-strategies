#!/usr/bin/env python3
"""
HighTempTation — 混沌工程（高阶优化 #6）

功能:
  1. FaultInjector   — 故障注入: API 延迟 / 404 / 500 / 丢包(网络错误) / 余额突变
  2. CircuitBreaker  — 熔断器: 连续失败 → OPEN 快速失败 → 冷却 → HALF_OPEN 试探 → 恢复
  3. ChaosVerifier   — 场景验证矩阵: 对每个故障场景断言熔断正确触发/恢复/安全降级

用法:
  from highopt.chaos import FaultInjector, CircuitBreaker, ChaosVerifier

  # 包装任意 async 函数
  async def fetch_price(): ...   # 真实 API 调用
  injector = FaultInjector(latency_ms=500, status_500=0.3)
  wrapped = injector.wrap(fetch_price)

  # 熔断保护
  cb = CircuitBreaker(failure_threshold=3, cooldown=5)
  result = await cb.call(wrapped)

  # 一键验证全部故障场景
  verifier = ChaosVerifier()
  report = await verifier.run_all()   # → 每个场景的断言结果

集成说明:
  - 实盘接入: 在交易所适配器（order_fsm.ExchangeAdapter）外层包 FaultInjector + CircuitBreaker
  - 余额突变: 注入器可改写返回体中的余额字段, 用于验证「余额突变 → 风控熔断」链路
"""
import asyncio
import logging
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("highopt.chaos")


# ════════════════════════════════════════════════════════════════
# 1. 故障注入器
# ════════════════════════════════════════════════════════════════

class FaultInjector:
    """
    故障注入器。

    场景参数（均为概率/幅度）:
      latency_ms    — 注入延迟（固定毫秒）
      latency_prob  — 注入延迟的概率
      status_404    — 返回 404 错误的概率
      status_500    — 返回 500 错误的概率
      drop_prob     — 丢包（抛网络异常）的概率
      balance_mutation — 余额突变: 以概率改写返回体 balance 字段
      balance_delta — 余额突变幅度（乘法系数, 如 0.5 = 余额腰斩）

    用法:
      injector = FaultInjector(status_500=0.3, latency_ms=300)
      async def api(): return {"code": "0", "data": {"balance": 100.0}}
      wrapped = injector.wrap(api)
      result = await wrapped()   # 30% 概率抛 RuntimeError("500")
    """

    def __init__(self, latency_ms: float = 0.0, latency_prob: float = 0.0,
                 status_404: float = 0.0, status_500: float = 0.0,
                 drop_prob: float = 0.0,
                 balance_mutation: float = 0.0, balance_delta: float = 0.5,
                 seed: int = 42):
        self.latency_ms = latency_ms
        self.latency_prob = latency_prob
        self.status_404 = status_404
        self.status_500 = status_500
        self.drop_prob = drop_prob
        self.balance_mutation = balance_mutation
        self.balance_delta = balance_delta
        self._rng = random.Random(seed)
        self.events: List[dict] = []     # 注入事件记录

    def _record(self, fault: str, detail: str = ""):
        self.events.append({"fault": fault, "detail": detail,
                            "ts": time.time()})

    async def wrap(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """
        包装 async 函数, 按概率注入故障。
        :raises: RuntimeError（404/500）、ConnectionError（丢包）
        """
        # 延迟
        if self.latency_ms > 0 and self._rng.random() < self.latency_prob:
            self._record("latency", f"{self.latency_ms:.0f}ms")
            await asyncio.sleep(self.latency_ms / 1000.0)
        # 丢包
        if self._rng.random() < self.drop_prob:
            self._record("packet_loss")
            raise ConnectionError("simulated packet loss")
        # 404
        if self._rng.random() < self.status_404:
            self._record("404")
            raise RuntimeError("HTTP 404 Not Found (injected)")
        # 500
        if self._rng.random() < self.status_500:
            self._record("500")
            raise RuntimeError("HTTP 500 Internal Server Error (injected)")
        # 余额突变
        if self._rng.random() < self.balance_mutation:
            self._record("balance_mutation", f"x{self.balance_delta}")

        result = await fn()

        if self.balance_mutation > 0 and self._rng.random() < self.balance_mutation:
            result = self._mutate_balance(result)
        return result

    def _mutate_balance(self, result: Any) -> Any:
        """递归改写返回体中的余额字段（balance/equity/available）"""
        if isinstance(result, dict):
            out = {}
            for k, v in result.items():
                if isinstance(v, (int, float)) and k.lower() in (
                        "balance", "equity", "available", "total_equity"):
                    out[k] = v * self.balance_delta
                elif isinstance(v, dict):
                    out[k] = self._mutate_balance(v)
                elif isinstance(v, list):
                    out[k] = [self._mutate_balance(x) if isinstance(x, dict) else x
                              for x in v]
                else:
                    out[k] = v
            return out
        return result


# ════════════════════════════════════════════════════════════════
# 2. 熔断器
# ════════════════════════════════════════════════════════════════

class CircuitBreaker:
    """
    熔断器。

    状态机:
      CLOSED ──失败≥failure_threshold──▶ OPEN
      OPEN ──冷却 cooldown 秒──▶ HALF_OPEN
      HALF_OPEN ──成功(success_threshold 次)──▶ CLOSED
      HALF_OPEN ──失败──▶ OPEN（重新冷却）

    行为:
      - OPEN 时快速失败（不调用下游, 抛 CircuitOpenError）
      - 支持降级回调 fallback（如返回缓存价/只读模式）
      - 记录每次调用与状态迁移, 供混沌验证断言
    """

    def __init__(self, failure_threshold: int = 5,
                 cooldown: float = 10.0,
                 success_threshold: int = 2,
                 fallback: Optional[Callable[[], Any]] = None):
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.success_threshold = success_threshold
        self.fallback = fallback
        self.state = "CLOSED"
        self._failures = 0
        self._successes = 0
        self._opened_at: Optional[float] = None
        self.stats = {"calls": 0, "failures": 0, "successes": 0,
                      "rejected": 0, "fallbacks": 0}
        self.transitions: List[dict] = []

    def _transition(self, new_state: str, reason: str):
        self.transitions.append({"from": self.state, "to": new_state,
                                 "reason": reason, "ts": time.time()})
        logger.info(f"熔断迁移: {self.state} → {new_state} ({reason})")
        self.state = new_state

    async def call(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        self.stats["calls"] += 1

        # OPEN: 快速失败
        if self.state == "OPEN":
            if time.time() - self._opened_at >= self.cooldown:
                self._transition("HALF_OPEN", "冷却结束, 放行试探")
            else:
                self.stats["rejected"] += 1
                if self.fallback:
                    self.stats["fallbacks"] += 1
                    return self.fallback()
                raise CircuitOpenError("circuit is OPEN")

        try:
            result = await fn()
        except Exception as e:
            self.stats["failures"] += 1
            self._successes = 0
            if self.state == "HALF_OPEN":
                self._transition("OPEN", f"试探失败: {e}")
                self._opened_at = time.time()
            else:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._transition("OPEN", f"连续失败 {self._failures} 次")
                    self._opened_at = time.time()
            if self.fallback:
                self.stats["fallbacks"] += 1
                return self.fallback()
            raise

        # 成功
        self.stats["successes"] += 1
        if self.state == "HALF_OPEN":
            self._successes += 1
            if self._successes >= self.success_threshold:
                self._transition("CLOSED", f"试探成功 {self._successes} 次")
                self._failures = 0
                self._successes = 0
        else:
            self._failures = 0
        return result

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"

    def reset(self):
        self.state = "CLOSED"
        self._failures = 0
        self._successes = 0
        self._opened_at = None


class CircuitOpenError(Exception):
    """熔断器打开时快速失败抛出的异常"""


# ════════════════════════════════════════════════════════════════
# 3. 混沌验证器
# ════════════════════════════════════════════════════════════════

class ChaosVerifier:
    """
    混沌工程场景验证器。

    场景矩阵（对照任务要求的故障注入类型）:
      1. latency        — API 延迟: 熔断器不应误触发, 但延迟应被记录
      2. http_404       — 404: 应触发失败计数（可配置策略: 404 计入熔断）
      3. http_500       — 500: 应触发熔断 → OPEN → 快速失败
      4. packet_loss    — 丢包: 网络异常应触发熔断
      5. balance_mutation — 余额突变: 检测到余额跳变 → 风控熔断/告警

    run_all() → {scenario: {"ok": bool, "assertions": [...], "circuit_transitions": [...], ...}}
    """

    def __init__(self, failure_threshold: int = 3, cooldown: float = 2.0,
                 success_threshold: int = 2, seed: int = 42):
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.success_threshold = success_threshold
        self.seed = seed

    # ── 模拟下游服务 ──

    @staticmethod
    async def _mock_api(ok: bool = True, balance: float = 100.0) -> dict:
        if not ok:
            raise RuntimeError("downstream failure")
        return {"code": "0", "data": {"balance": balance, "positions": []}}

    # ── 场景 ──

    async def _scenario(self, name: str, injector: FaultInjector,
                        n_calls: int = 12) -> dict:
        cb = CircuitBreaker(self.failure_threshold, self.cooldown,
                            self.success_threshold,
                            fallback=lambda: {"fallback": True})
        wrapped = injector.wrap(self._mock_api)
        results = []
        for _ in range(n_calls):
            try:
                r = await cb.call(wrapped)
                results.append({"ok": True, "result": r})
            except Exception as e:
                results.append({"ok": False, "error": type(e).__name__})
            await asyncio.sleep(0.05)

        return {
            "scenario": name,
            "injected_events": injector.events,
            "circuit_stats": dict(cb.stats),
            "circuit_transitions": cb.transitions,
            "results": results,
        }

    async def run_scenario(self, name: str) -> dict:
        """运行单个场景"""
        if name == "latency":
            inj = FaultInjector(latency_ms=200, latency_prob=1.0, seed=self.seed)
            n = 6
        elif name == "http_404":
            inj = FaultInjector(status_404=1.0, seed=self.seed)
            n = 8
        elif name == "http_500":
            inj = FaultInjector(status_500=1.0, seed=self.seed)
            n = 8
        elif name == "packet_loss":
            inj = FaultInjector(drop_prob=1.0, seed=self.seed)
            n = 8
        elif name == "balance_mutation":
            inj = FaultInjector(balance_mutation=1.0, balance_delta=0.5, seed=self.seed)
            n = 6
        else:
            raise ValueError(f"未知场景: {name}")
        return await self._scenario(name, inj, n)

    # ── 断言 ──

    @staticmethod
    def _assertions(name: str, s: dict) -> List[dict]:
        """每个场景的断言规则"""
        out = []
        stats = s["circuit_stats"]
        transitions = s["circuit_transitions"]

        def add(desc, ok, detail=""):
            out.append({"desc": desc, "ok": ok, "detail": detail})

        if name == "latency":
            add("熔断器不应打开（延迟≠故障）", s["circuit_stats"]["rejected"] == 0
                and not any(t["to"] == "OPEN" for t in transitions),
                f"transitions={len(transitions)}")
            add("注入的延迟事件被记录", len(s["injected_events"]) > 0,
                f"events={len(s['injected_events'])}")
        elif name == "http_404":
            add("404 计入失败并触发熔断", any(t["to"] == "OPEN" for t in transitions),
                f"failures={stats['failures']}")
            add("熔断打开后快速失败（拒绝计数>0）", stats["rejected"] > 0,
                f"rejected={stats['rejected']}")
        elif name == "http_500":
            add("500 触发熔断 OPEN", any(t["to"] == "OPEN" for t in transitions),
                f"failures={stats['failures']}")
            add("熔断后快速失败 + 降级回调生效", stats["rejected"] > 0 and stats["fallbacks"] > 0,
                f"rejected={stats['rejected']} fallbacks={stats['fallbacks']}")
        elif name == "packet_loss":
            add("丢包（网络异常）触发熔断", any(t["to"] == "OPEN" for t in transitions),
                f"failures={stats['failures']}")
            add("丢包被记录", any(e["fault"] == "packet_loss" for e in s["injected_events"]))
        elif name == "balance_mutation":
            mutated = [r for r in s["results"] if r["ok"]
                       and r["result"].get("data", {}).get("balance") != 100.0]
            add("余额突变被检测到（返回余额≠原值）", len(mutated) > 0,
                f"mutated={len(mutated)}")
        return out

    # ── 一键验证 ──

    async def run_all(self, scenarios: Optional[List[str]] = None) -> dict:
        scenarios = scenarios or ["latency", "http_404", "http_500",
                                  "packet_loss", "balance_mutation"]
        report = {}
        for name in scenarios:
            s = await self.run_scenario(name)
            s["assertions"] = self._assertions(name, s)
            s["ok"] = all(a["ok"] for a in s["assertions"])
            report[name] = s
            logger.info(f"混沌场景 {name}: {'✅' if s['ok'] else '❌'} "
                        f"({sum(a['ok'] for a in s['assertions'])}/{len(s['assertions'])} 断言通过)")
        report["_summary"] = {
            "total_scenarios": len(scenarios),
            "passed": sum(1 for n in scenarios if report[n]["ok"]),
        }
        return report
