#!/usr/bin/env python3
"""
HighTempTation — 人机协同（第四波优化 #7）

功能:
  1. AutomationLevel — 分级自动化（AUTO / NOTIFY / CONFIRM / PAUSE）
  2. HumanInTheLoop  — 分级执行器（通知 / 人工确认 / 超时策略 / 暂停阻断）
  3. HumanLoopController — 全局/按策略级别管理 + 人工失联自动降级

用法:
  from highopt_ultra.human_loop import HumanInTheLoop, AutomationLevel, HumanLoopController

  async def notifier(msg): print("[通知]", msg)      # 可接 AlertManager

  htl = HumanInTheLoop(level=AutomationLevel.CONFIRM, notifier=notifier,
                       confirm_timeout=300, timeout_policy="reject")
  result = await htl.execute("OPEN_POSITION", {"token": "Tokyo-NO", "size": 10})
  # → 生成审批请求 → 人工 approve() 后返回 {"approved": True, ...}
  #   超时未批 → 按 timeout_policy 拒绝/执行

  ctl = HumanLoopController(default_level=AutomationLevel.AUTO)
  ctl.set_level("Tokyo", AutomationLevel.CONFIRM)   # 按市场覆盖
  ctl.monitor_human_alive()                          # 人工失联 → 自动降级 PAUSE
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("highopt_ultra.human_loop")


class AutomationLevel(str, Enum):
    AUTO = "AUTO"          # 全自动: 无需人工
    NOTIFY = "NOTIFY"      # 自动执行 + 通知
    CONFIRM = "CONFIRM"    # 人工确认后才执行
    PAUSE = "PAUSE"        # 暂停: 阻断所有交易动作


LEVEL_DESC = {
    AutomationLevel.AUTO: "全自动: 信号即执行",
    AutomationLevel.NOTIFY: "通知: 执行后推送人工",
    AutomationLevel.CONFIRM: "人工确认: 批准后才执行",
    AutomationLevel.PAUSE: "暂停: 阻断开仓/平仓",
}


@dataclass
class ApprovalRequest:
    id: str
    action: str
    payload: dict
    level: AutomationLevel
    status: str = "pending"        # pending / approved / rejected / timeout
    decided_by: str = ""
    created_at: float = field(default_factory=time.time)
    timeout_at: float = 0.0


class HumanInTheLoop:
    """
    分级自动化执行器。

    各级别行为:
      AUTO    — 直接执行
      NOTIFY  — 执行 + 通知
      CONFIRM — 创建审批请求 → 等人工 → 批准则执行 / 拒绝则不执行 /
                超时按 timeout_policy（reject=默认拒绝, execute=默认执行）
      PAUSE   — 阻断（返回 blocked）

    用法:
      htl = HumanInTheLoop(level=AutomationLevel.CONFIRM,
                           notifier=notify_fn, confirm_timeout=300)
      res = await htl.execute("OPEN", {...})
      if res["pending_approval"]:
          htl.approve(res["request_id"], "boss")     # 人工批准
          res = await htl.execute("OPEN", {...})     # 重试即执行
    """

    def __init__(self, level: AutomationLevel = AutomationLevel.AUTO,
                 notifier: Optional[Callable[[str], Awaitable]] = None,
                 confirm_timeout: float = 300.0,
                 timeout_policy: str = "reject"):
        self.level = level
        self.notifier = notifier
        self.confirm_timeout = confirm_timeout
        self.timeout_policy = timeout_policy          # reject / execute
        self.approvals: Dict[str, ApprovalRequest] = {}
        self._approved_keys = set()                  # (action, payload_key) 已批准集合
        self.log: List[dict] = []

    @staticmethod
    def _payload_key(action: str, payload: dict) -> str:
        import json as _json
        return f"{action}|{_json.dumps(payload, sort_keys=True)}"

    def set_level(self, level: AutomationLevel):
        self.level = level

    def _log(self, entry: dict):
        entry["ts"] = time.time()
        self.log.append(entry)

    async def _notify(self, text: str):
        if self.notifier:
            try:
                await self.notifier(text)
            except Exception as e:                    # noqa: BLE001
                logger.warning(f"通知失败: {e}")

    async def execute(self, action: str, payload: dict) -> dict:
        """按当前级别执行一个动作"""
        if self.level == AutomationLevel.PAUSE:
            self._log({"action": action, "result": "blocked"})
            await self._notify(f"🚫 已暂停: {action} 被阻断")
            return {"ok": False, "reason": "PAUSE", "message": "自动化已暂停"}

        if self.level == AutomationLevel.CONFIRM:
            pkey = self._payload_key(action, payload)
            # 同 payload 已被批准 → 直接执行
            if pkey in self._approved_keys:
                self._log({"action": action, "result": "executed_approved"})
                await self._notify(f"✅ 已批准执行: {action} {payload}")
                return {"ok": True, "reason": "APPROVED"}
            # 查找同 action+payload 的既有审批请求
            req = next((r for r in self.approvals.values()
                        if r.action == action and
                        self._payload_key(r.action, r.payload) == pkey), None)
            if not req:
                req = ApprovalRequest(id=f"apr-{uuid.uuid4().hex[:8]}",
                                      action=action, payload=payload,
                                      level=self.level,
                                      timeout_at=time.time() + self.confirm_timeout)
                self.approvals[req.id] = req
                self._log({"action": action, "result": "awaiting_approval",
                           "request_id": req.id})
                await self._notify(f"🕐 待人工确认: {action} {payload} (id={req.id})")
                return {"ok": False, "reason": "AWAITING_APPROVAL",
                        "request_id": req.id, "timeout_policy": self.timeout_policy}
            if time.time() > req.timeout_at:
                req.status = "timeout"
                if self.timeout_policy == "execute":
                    self._log({"action": action, "result": "executed_timeout"})
                    await self._notify(f"⏰ 审批超时, 按策略默认执行: {action}")
                    return {"ok": True, "reason": "TIMEOUT_EXECUTE", "request_id": req.id}
                self._log({"action": action, "result": "rejected_timeout"})
                await self._notify(f"⏰ 审批超时, 默认拒绝: {action}")
                return {"ok": False, "reason": "TIMEOUT_REJECT", "request_id": req.id}
            if req.status == "approved":
                self._log({"action": action, "result": "executed_approved",
                           "request_id": req.id})
                await self._notify(f"✅ 已批准执行: {action} {payload}")
                return {"ok": True, "reason": "APPROVED", "request_id": req.id}
            if req.status == "rejected":
                return {"ok": False, "reason": "REJECTED", "request_id": req.id}
            return {"ok": False, "reason": "PENDING", "request_id": req.id}

        # AUTO / NOTIFY: 直接执行
        self._log({"action": action, "result": "executed"})
        if self.level == AutomationLevel.NOTIFY:
            await self._notify(f"📢 已执行: {action} {payload}")
        return {"ok": True, "reason": "EXECUTED", "level": self.level.value}

    def approve(self, request_id: str, by: str) -> bool:
        req = self.approvals.get(request_id)
        if not req or req.status != "pending":
            return False
        req.status = "approved"
        req.decided_by = by
        self._approved_keys.add(self._payload_key(req.action, req.payload))
        self._log({"action": req.action, "result": "approved", "by": by})
        return True

    def reject(self, request_id: str, by: str) -> bool:
        req = self.approvals.get(request_id)
        if not req or req.status != "pending":
            return False
        req.status = "rejected"
        req.decided_by = by
        self._log({"action": req.action, "result": "rejected", "by": by})
        return True

    def pending_approvals(self) -> List[str]:
        return [r.id for r in self.approvals.values() if r.status == "pending"]


class HumanLoopController:
    """
    人机协同控制器。

      - 全局级别 + 按市场覆盖级别
      - 人工失联检测（无心跳超时）→ 自动降级 PAUSE（安全兜底）
      - 级别变更全记录（审计可查）

    用法:
      ctl = HumanLoopController(default_level=AutomationLevel.NOTIFY,
                                human_timeout_sec=1800)
      ctl.set_level("Tokyo", AutomationLevel.CONFIRM)
      ctl.heartbeat()                     # 人工侧周期性心跳
      if ctl.monitor_human_alive(): ...   # 失联 → 返回降级事件
    """

    def __init__(self, default_level: AutomationLevel = AutomationLevel.NOTIFY,
                 human_timeout_sec: float = 1800.0):
        self.default_level = default_level
        self.human_timeout_sec = human_timeout_sec
        self.overrides: Dict[str, AutomationLevel] = {}
        self._last_human_heartbeat = time.time()
        self._downgraded = False
        self.level_changes: List[dict] = []

    def level_for(self, key: str = "") -> AutomationLevel:
        if self._downgraded:
            return AutomationLevel.PAUSE
        return self.overrides.get(key, self.default_level)

    def set_level(self, key: str, level: AutomationLevel):
        if key:
            self.overrides[key] = level
        else:
            self.default_level = level
        self.level_changes.append({"ts": time.time(), "key": key or "*",
                                   "level": level.value})

    def heartbeat(self):
        """人工侧心跳（看板点击 / 确认操作时调用）"""
        self._last_human_heartbeat = time.time()
        self._downgraded = False

    def monitor_human_alive(self) -> Optional[dict]:
        """检测人工是否失联; 失联 → 自动降级 PAUSE"""
        if time.time() - self._last_human_heartbeat > self.human_timeout_sec:
            if not self._downgraded:
                self._downgraded = True
                evt = {"ts": time.time(), "event": "DOWNGRADE_TO_PAUSE",
                       "reason": f"人工心跳超时 {self.human_timeout_sec}s"}
                self.level_changes.append(evt)
                logger.warning(f"🚨 {evt['reason']}")
                return evt
        return None

    def status(self) -> dict:
        return {
            "default_level": self.default_level.value,
            "overrides": {k: v.value for k, v in self.overrides.items()},
            "effective": self.level_for().value,
            "human_alive": time.time() - self._last_human_heartbeat
                           <= self.human_timeout_sec,
            "downgraded": self._downgraded,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def notify(msg):
        print("  [通知]", msg)

    async def main():
        # CONFIRM 流程
        htl = HumanInTheLoop(level=AutomationLevel.CONFIRM, notifier=notify,
                             confirm_timeout=300)
        r1 = await htl.execute("OPEN_POSITION", {"token": "Tokyo-NO", "size": 10})
        print("第一次执行:", r1["reason"])
        htl.approve(r1["request_id"], "trader-boss")
        r2 = await htl.execute("OPEN_POSITION", {"token": "Tokyo-NO", "size": 10})
        print("批准后执行:", r2["reason"])

        # 分级控制器
        ctl = HumanLoopController(default_level=AutomationLevel.NOTIFY)
        ctl.set_level("Tokyo", AutomationLevel.CONFIRM)
        print("Tokyo 级别:", ctl.level_for("Tokyo").value)
        ctl._last_human_heartbeat = time.time() - 99999
        evt = ctl.monitor_human_alive()
        print("失联降级:", evt["event"] if evt else None, "→ 有效级别:", ctl.level_for().value)

    asyncio.run(main())
