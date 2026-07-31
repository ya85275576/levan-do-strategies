#!/usr/bin/env python3
"""
HighTempTation — 组合风控 (Day1 关键项 #5)

问题: 现有风控只有单仓维度 (MAX_CONCURRENT / MAX_POS_PER_CITY_DAY)，
      同一事件 (城市+日期) 的多个桶可以同时开仓 → 系统性风险叠加:
        - 预报偏差 (如模型系统性高估 2°C) 会让同一事件的所有相邻桶
          同时触发同向信号 → 同涨同跌
        - 相邻桶合约高度相关 (温度只落在一个桶), 同时持有多桶 = 重复下注

解决方案 (三层):
  1. 同事件限制: 同一 (city, date) 最大持仓笔数 + 最大总暴露金额
  2. 相邻桶限制: 同一事件相邻温度桶最多 N 笔 (桶相关性惩罚)
  3. 相关性熔断: 同一事件同向信号 ≥ CORR_BREAKER_COUNT 个 → 判定模型
     系统性偏差 → 熔断该事件全部信号 (冷却期 CORR_BREAKER_COOLDOWN 秒),
     熔断事件计入统计并暴露给 /api/metrics

用法:
  prm = PortfolioRiskManager()
  allowed, rejected, breakers = prm.filter(signals, engine, cfg)
  # allowed: 通过风控的信号; rejected: 被拒信号(带原因);
  # breakers: 本次熔断的事件列表
"""
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("portfolio_risk")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class PortfolioRiskManager:
    """
    组合风控管理器。

    参数 (环境变量):
      RISK_MAX_POS_PER_EVENT    : 同事件 (city+date) 最大持仓笔数 (默认 2)
      RISK_MAX_EXPOSURE_EVENT   : 同事件最大总暴露金额 $ (默认 150)
      RISK_MAX_ADJACENT_BUCKETS : 同事件相邻桶最大笔数 (含主桶, 默认 2)
      RISK_CORR_BREAKER_COUNT   : 同事件同向信号数 ≥ N 触发相关性熔断 (默认 3)
      RISK_CORR_BREAKER_COOLDOWN: 熔断冷却秒数 (默认 3600)
      RISK_ENABLED              : 总开关 (默认 true)
    """

    def __init__(self):
        self.enabled = os.getenv("RISK_ENABLED", "true").lower() == "true"
        self.max_pos_per_event = _env_int("RISK_MAX_POS_PER_EVENT", 2)
        self.max_exposure_event = _env_float("RISK_MAX_EXPOSURE_EVENT", 150.0)
        self.max_adjacent_buckets = _env_int("RISK_MAX_ADJACENT_BUCKETS", 2)
        self.corr_breaker_count = _env_int("RISK_CORR_BREAKER_COUNT", 3)
        self.corr_breaker_cooldown = _env_int("RISK_CORR_BREAKER_COOLDOWN", 3600)

        # 状态
        self._breaker_until: Dict[str, float] = {}   # "city|date" → 熔断截止时间戳
        self.breaker_events: List[dict] = []         # 历史熔断记录
        self.n_rejected = 0
        self.n_allowed = 0
        self._rejected_reasons: Dict[str, int] = defaultdict(int)

    # ════════════════════════════════════════════════════════════════
    # 辅助
    # ════════════════════════════════════════════════════════════════

    def _event_key(self, city: str, dt: str) -> str:
        return f"{city}|{dt}"

    def _is_breaked(self, city: str, dt: str) -> bool:
        key = self._event_key(city, dt)
        until = self._breaker_until.get(key, 0.0)
        if time.time() < until:
            return True
        if until:
            self._breaker_until.pop(key, None)  # 冷却结束自动清除
        return False

    def _bucket_index(self, label: str, cfg) -> int:
        """温度桶在 cfg.buckets 中的索引, -1 = 未知"""
        for i, (lo, hi, lbl) in enumerate(cfg.buckets):
            if lbl == label:
                return i
        return -1

    # ════════════════════════════════════════════════════════════════
    # 核心: 信号过滤
    # ════════════════════════════════════════════════════════════════

    def filter(self, signals: List[dict], engine, cfg) -> Tuple[List[dict], List[dict], List[dict]]:
        """
        组合风控过滤。

        Args:
            signals: 待开仓信号列表
            engine: Engine 实例 (读已有持仓)
            cfg: Config 实例 (读 buckets / MAX_POS_PER_CITY_DAY 等)

        Returns:
            (allowed, rejected, breakers)
            - allowed: 通过全部风控的信号
            - rejected: 被拒信号, 每个含 reason 字段
            - breakers: 本次触发熔断的事件 [{city, date, n_signals, side, until}]
        """
        if not self.enabled:
            return signals, [], []
        if not signals:
            return [], [], []

        # 开放持仓按事件聚合 (含已有持仓, 防止与在持仓叠加超限)
        open_by_event: Dict[str, int] = defaultdict(int)
        exposure_by_event: Dict[str, float] = defaultdict(float)
        open_bucket_idx: Dict[str, set] = defaultdict(set)
        if engine:
            for p in engine.positions:
                if not p.is_open:
                    continue
                key = self._event_key(p.city, p.date)
                open_by_event[key] += 1
                exposure_by_event[key] += getattr(p, "size", 0) or 0
                bi = self._bucket_index(p.bucket_label, cfg)
                if bi >= 0:
                    open_bucket_idx[key].add(bi)

        allowed: List[dict] = []
        rejected: List[dict] = []
        breakers: List[dict] = []

        # ── 第 0 层: 相关性熔断检查 (先于一切) ──
        # 同一事件同向信号 ≥ N 个 → 模型系统性偏差 → 熔断该事件
        corr_groups: Dict[str, dict] = defaultdict(lambda: {"n": 0, "side": ""})
        for sig in signals:
            key = self._event_key(sig.get("city", ""), sig.get("date", ""))
            g = corr_groups[key]
            g["n"] += 1
            g["side"] = sig.get("side", "NO")
            g["city"] = sig.get("city", "")
            g["date"] = sig.get("date", "")
        now = time.time()
        for key, g in corr_groups.items():
            if g["n"] < self.corr_breaker_count:
                continue
            if self._is_breaked(g["city"], g["date"]):
                continue  # 已在熔断冷却中
            self._breaker_until[key] = now + self.corr_breaker_cooldown
            evt = {
                "city": g["city"], "date": g["date"],
                "n_signals": g["n"], "side": g["side"],
                "reason": f"相关性熔断: 同事件 {g['n']} 个同向信号 (疑似模型系统性偏差)",
                "until": datetime.fromtimestamp(now + self.corr_breaker_cooldown,
                                                timezone.utc).isoformat(),
            }
            self.breaker_events.append(evt)
            breakers.append(evt)
            logger.warning(f"⛔ 相关性熔断 [{g['city']} {g['date']}]: "
                          f"{g['n']} 个同向信号, 冷却 {self.corr_breaker_cooldown}s")

        # ── 逐信号过滤 ──
        for sig in signals:
            city = sig.get("city", "")
            dt = sig.get("date", "")
            key = self._event_key(city, dt)
            size = float(sig.get("planned_size", 10.0) or 10.0)

            # 熔断冷却中的事件 → 拒绝
            if self._is_breaked(city, dt):
                self._reject(sig, rejected, f"事件熔断冷却中")
                continue

            # 已有持仓的 market_id → 拒绝 (双保险, 主循环也会查)
            if engine and engine.has_position(sig.get("market_id", "")):
                continue  # 不算风控拒绝, 直接跳过

            # ── 1. 同事件笔数限制 ──
            event_count = open_by_event[key] + sum(
                1 for s in allowed if self._event_key(s.get("city", ""), s.get("date", "")) == key)
            if event_count >= self.max_pos_per_event:
                self._reject(sig, rejected,
                             f"同事件持仓超限 {event_count}/{self.max_pos_per_event}")
                continue

            # ── 2. 同事件暴露金额限制 ──
            event_exposure = exposure_by_event[key] + sum(
                float(s.get("planned_size", 10.0) or 10.0) for s in allowed
                if self._event_key(s.get("city", ""), s.get("date", "")) == key)
            if event_exposure + size > self.max_exposure_event:
                self._reject(sig, rejected,
                             f"同事件暴露超限 ${event_exposure + size:.0f} > "
                             f"${self.max_exposure_event:.0f}")
                continue

            # ── 3. 相邻桶限制: 同事件相邻桶合计 ≤ max_adjacent_buckets ──
            bi = self._bucket_index(sig.get("bucket", ""), cfg)
            if bi >= 0:
                adjacent = open_bucket_idx[key].union(
                    self._bucket_index(s.get("bucket", ""), cfg)
                    for s in allowed
                    if self._event_key(s.get("city", ""), s.get("date", "")) == key
                    and self._bucket_index(s.get("bucket", ""), cfg) >= 0)
                # 计算与已开桶相邻 (索引差 ≤1) 的数量
                adj_count = 0
                for obi in adjacent:
                    if abs(obi - bi) <= 1:
                        adj_count += 1
                # 主桶本身 +1
                if bi in adjacent:
                    adj_count = max(adj_count, 1)
                if adj_count >= self.max_adjacent_buckets:
                    self._reject(sig, rejected,
                                 f"相邻桶超限 ({adj_count} 桶相邻, "
                                 f"上限 {self.max_adjacent_buckets})")
                    continue

            # 通过
            allowed.append(sig)
            open_by_event[key] += 1
            exposure_by_event[key] += size
            if bi >= 0:
                open_bucket_idx[key].add(bi)

        self.n_allowed += len(allowed)
        self.n_rejected += len(rejected)
        if rejected:
            logger.info(f"🛡️ 组合风控: {len(signals)} 信号 → 放行 {len(allowed)}, "
                        f"拒绝 {len(rejected)}" +
                        (f", 熔断 {len(breakers)} 事件" if breakers else ""))
        return allowed, rejected, breakers

    def _reject(self, sig: dict, rejected: List[dict], reason: str):
        r = dict(sig)
        r["reason"] = reason
        rejected.append(r)
        self._rejected_reasons[reason.split(":")[0]] += 1
        logger.info(f"  ⛔ 风控拒绝 [{sig.get('city','')} {sig.get('bucket','')}]: {reason}")

    # ════════════════════════════════════════════════════════════════
    # 状态 (供 /api/metrics)
    # ════════════════════════════════════════════════════════════════

    def get_stats(self) -> dict:
        active_breakers = []
        now = time.time()
        for key, until in self._breaker_until.items():
            if now < until:
                city, dt = key.split("|", 1)
                active_breakers.append({
                    "city": city, "date": dt,
                    "until": datetime.fromtimestamp(until, timezone.utc).isoformat(),
                })
        return {
            "enabled": self.enabled,
            "params": {
                "max_pos_per_event": self.max_pos_per_event,
                "max_exposure_event": self.max_exposure_event,
                "max_adjacent_buckets": self.max_adjacent_buckets,
                "corr_breaker_count": self.corr_breaker_count,
                "corr_breaker_cooldown": self.corr_breaker_cooldown,
            },
            "n_allowed": self.n_allowed,
            "n_rejected": self.n_rejected,
            "rejected_reasons": dict(self._rejected_reasons),
            "active_breakers": active_breakers,
            "breaker_history": self.breaker_events[-20:],
            "n_breaker_events": len(self.breaker_events),
        }
