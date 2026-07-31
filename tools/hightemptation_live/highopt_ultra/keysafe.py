#!/usr/bin/env python3
"""
HighTempTation — 密钥安全（第四波优化 #2）

功能:
  1. WalletTier        — 冷热钱包分级（HOT/WARM/COLD, 单笔/日限额/签名策略）
  2. KMSBackend        — KMS/HSM 集成接口（签名在 KMS 内完成, 私钥永不落盘）
  3. EncryptedKeystore — 最小可行加密密钥库（scrypt 派生 + HMAC 认证, 生产建议 Fernet/KMS）
  4. AuditLogger       — 不可篡改审计日志（SHA-256 哈希链 + 周期锚定 + 全链校验）
  5. DualControl       — 双人控制（M-of-N 审批流, 模拟 Gnosis Safe 多签收集）

用法:
  from highopt_ultra.keysafe import (
      WalletTier, TierPolicy, KMSBackend, SimKMSBackend,
      EncryptedKeystore, AuditLogger, DualControl,
  )

  # 冷热钱包分级
  hot = TierPolicy.HOT
  allowed, reason = hot.approve_tx(amount=50, daily_spent=10)

  # KMS 签名（私钥不出 KMS）
  kms = SimKMSBackend(seed="dev")
  key_id = kms.create_key("hot")
  sig = kms.sign(hashlib.sha256(b"tx").hexdigest(), key_id)

  # 不可篡改审计日志
  audit = AuditLogger()
  audit.append("trader-1", "OPEN_POSITION", {"token": "Tokyo-NO", "size": 10})
  audit.append("trader-2", "APPROVE", {"request": "r1"})
  audit.verify_chain()                       # → True

  # 双人控制
  dc = DualControl(threshold=2, approvers=["alice", "bob", "carol"])
  req = dc.request("WITHDRAW", {"amount": 1000})
  dc.approve(req.id, "alice")
  dc.approve(req.id, "bob")
  dc.status(req.id)                           # → APPROVED
"""
import hashlib
import hmac
import json
import logging
import os
import struct
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("highopt_ultra.keysafe")

# ════════════════════════════════════════════════════════════════
# 1. 冷热钱包分级
# ════════════════════════════════════════════════════════════════

class WalletTier(str, Enum):
    HOT = "HOT"      # 高频小额: 自动签名, 严格限额, 私钥在线(HSM 内)
    WARM = "WARM"    # 中频中额: 单签 + 限流, 私钥在独立 KMS key
    COLD = "COLD"    # 低频大额: 离线/多签, 私钥冷存储, 需人工审批


@dataclass
class TierPolicy:
    tier: WalletTier
    per_tx_limit: float          # 单笔限额(USD)
    daily_limit: float           # 日累计限额(USD)
    auth_required: bool          # 是否需人工审批
    m_of_n: int                  # 多签门槛(1=单签)
    sign_mode: str               # auto / auto+rate_limit / offline_multisig

    @classmethod
    def defaults(cls) -> Dict[WalletTier, "TierPolicy"]:
        return {
            WalletTier.HOT:  cls(WalletTier.HOT,  200,   2000,  False, 1, "auto"),
            WalletTier.WARM: cls(WalletTier.WARM, 2000,  10000, True,  1, "auto+rate_limit"),
            WalletTier.COLD: cls(WalletTier.COLD, 100000, 500000, True, 2, "offline_multisig"),
        }


class WalletManager:
    """
    冷热钱包分级管理器。

    按金额自动选择层级: amount ≤ HOT 限额 → HOT; ≤ WARM 限额 → WARM;
    其余 → COLD（要求多签审批）。并做单笔/日累计限额校验。
    """

    def __init__(self, policies: Optional[Dict[WalletTier, TierPolicy]] = None):
        self.policies = policies or TierPolicy.defaults()
        self._daily_spent: Dict[str, float] = {}     # tier → 当日已花

    def pick_tier(self, amount: float) -> WalletTier:
        if amount <= self.policies[WalletTier.HOT].per_tx_limit:
            return WalletTier.HOT
        if amount <= self.policies[WalletTier.WARM].per_tx_limit:
            return WalletTier.WARM
        return WalletTier.COLD

    def approve_tx(self, amount: float, daily_spent: Optional[float] = None) -> Tuple[bool, str, dict]:
        """校验一笔交易: (允许, 原因, 决策详情)"""
        tier = self.pick_tier(amount)
        policy = self.policies[tier]
        spent = self._daily_spent.get(tier.value, daily_spent or 0.0)
        if amount > policy.per_tx_limit:
            return False, f"{tier.value} 单笔超限: {amount} > {policy.per_tx_limit}", {}
        if spent + amount > policy.daily_limit:
            return False, f"{tier.value} 日累计超限: {spent + amount} > {policy.daily_limit}", {}
        self._daily_spent[tier.value] = spent + amount
        return True, "", {
            "tier": tier.value, "sign_mode": policy.sign_mode,
            "auth_required": policy.auth_required, "m_of_n": policy.m_of_n,
        }


# ════════════════════════════════════════════════════════════════
# 2. KMS / HSM 集成
# ════════════════════════════════════════════════════════════════

class KMSBackend:
    """
    KMS/HSM 抽象接口。

    生产实现（AWS KMS / GCP KMS / Azure Key Vault / 本地 HSM）:
      - 私钥永不离开 KMS; 应用只提交 digest, 拿到签名
      - create_key / get_public_key / sign / verify
      - 访问控制 + 审计由 KMS 侧完成

    必须实现的方法见下。业务代码只依赖本接口, 可无缝切换后端。
    """

    def create_key(self, alias: str, key_spec: str = "secp256k1") -> str:
        raise NotImplementedError

    def get_public_key(self, key_id: str) -> str:
        raise NotImplementedError

    def sign(self, digest_hex: str, key_id: str) -> str:
        raise NotImplementedError

    def verify(self, digest_hex: str, key_id: str, signature_hex: str) -> bool:
        raise NotImplementedError


class SimKMSBackend(KMSBackend):
    """
    模拟 KMS（开发/自检用）。

    签名 = HMAC-SHA256(digest, key_material), 公钥 = key_id 的 SHA256 摘要。
    私钥仅存在于内存。生产环境替换为真实 KMS SDK 实现。
    """

    def __init__(self, seed: str = "sim-kms"):
        self._seed = seed
        self._keys: Dict[str, bytes] = {}

    def create_key(self, alias: str, key_spec: str = "secp256k1") -> str:
        key_id = f"{alias}-{uuid.uuid4().hex[:8]}"
        self._keys[key_id] = hashlib.sha256(f"{self._seed}:{key_id}".encode()).digest()
        return key_id

    def get_public_key(self, key_id: str) -> str:
        if key_id not in self._keys:
            raise KeyError(f"KMS key 不存在: {key_id}")
        return hashlib.sha256(b"pub:" + self._keys[key_id]).hexdigest()

    def sign(self, digest_hex: str, key_id: str) -> str:
        if key_id not in self._keys:
            raise KeyError(f"KMS key 不存在: {key_id}")
        return hmac.new(self._keys[key_id], bytes.fromhex(digest_hex), hashlib.sha256).hexdigest()

    def verify(self, digest_hex: str, key_id: str, signature_hex: str) -> bool:
        try:
            return hmac.compare_digest(self.sign(digest_hex, key_id), signature_hex)
        except KeyError:
            return False


# ════════════════════════════════════════════════════════════════
# 3. 最小可行加密密钥库（私钥防静态泄露）
# ════════════════════════════════════════════════════════════════

class EncryptedKeystore:
    """
    加密密钥库（标准库实现, 防静态泄露用）。

      - scrypt 从口令派生 64 字节密钥（前 32 = 加密, 后 32 = 认证）
      - XOR 流式加密 + HMAC 认证（篡改即校验失败）
      - 生产环境请替换为 Fernet(KMS 包装) 或直接使用云 KMS

    用法:
      ks = EncryptedKeystore("keystore.bin")
      ks.save("hot", b"private-key-bytes", password="s3cret")
      raw = ks.load("hot", password="s3cret")   # → b"private-key-bytes"
    """

    SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1

    def __init__(self, path: str = "keystore.bin"):
        self.path = path

    @staticmethod
    def _derive(password: str, salt: bytes) -> Tuple[bytes, bytes]:
        key = hashlib.scrypt(password.encode(), salt=salt,
                             n=EncryptedKeystore.SCRYPT_N,
                             r=EncryptedKeystore.SCRYPT_R,
                             p=EncryptedKeystore.SCRYPT_P, dklen=64)
        return key[:32], key[32:]

    @staticmethod
    def _xor(data: bytes, key: bytes) -> bytes:
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

    def save(self, alias: str, secret: bytes, password: str):
        salt = os.urandom(16)
        ek, ak = self._derive(password, salt)
        blob = {alias: {"salt": salt.hex(), "cipher": self._xor(secret, ek).hex(),
                        "mac": hmac.new(ak, secret, hashlib.sha256).hexdigest()}}
        with open(self.path, "wb") as f:
            f.write(json.dumps(blob).encode())

    def load(self, alias: str, password: str) -> bytes:
        with open(self.path, "rb") as f:
            blob = json.loads(f.read().decode())
        if alias not in blob:
            raise KeyError(f"别名不存在: {alias}")
        entry = blob[alias]
        ek, ak = self._derive(password, bytes.fromhex(entry["salt"]))
        secret = self._xor(bytes.fromhex(entry["cipher"]), ek)
        if not hmac.compare_digest(hmac.new(ak, secret, hashlib.sha256).hexdigest(),
                                   entry["mac"]):
            raise ValueError("MAC 校验失败: 密钥文件被篡改或口令错误")
        return secret


# ════════════════════════════════════════════════════════════════
# 4. 不可篡改审计日志（哈希链）
# ════════════════════════════════════════════════════════════════

class AuditLogger:
    """
    哈希链审计日志。

    每条记录: hash = SHA256(prev_hash || seq || actor || action || payload_hash)。
    任何一条被篡改都会导致后续所有 hash 失配 → 全链校验失败。
    可选 anchor() 把当前链头摘要"锚定"到外部（链上 tx / 第三时间戳服务）。

    用法:
      audit = AuditLogger()
      audit.append("alice", "OPEN", {"token": "x", "size": 10})
      audit.append("bob", "APPROVE", {"req": "r1"})
      assert audit.verify_chain()
      anchor_ref = audit.anchor()
      # 模拟篡改 → verify_chain() == False
    """

    def __init__(self):
        self._entries: List[dict] = []
        self._anchors: List[dict] = []

    @staticmethod
    def _h(*parts: str) -> str:
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def append(self, actor: str, action: str, payload: dict) -> dict:
        seq = len(self._entries) + 1
        prev = self._entries[-1]["hash"] if self._entries else "GENESIS"
        payload_hash = self._h(json.dumps(payload, sort_keys=True))
        entry = {
            "seq": seq, "ts": time.time(), "actor": actor, "action": action,
            "payload_hash": payload_hash, "prev_hash": prev,
            "hash": self._h(prev, str(seq), actor, action, payload_hash),
        }
        self._entries.append(entry)
        return entry

    def verify_chain(self) -> Tuple[bool, Optional[int]]:
        """全链校验: (是否完整无篡改, 首个失配序号)"""
        prev = "GENESIS"
        for e in self._entries:
            recalc = self._h(e["prev_hash"], str(e["seq"]), e["actor"],
                             e["action"], e["payload_hash"])
            if e["prev_hash"] != prev or recalc != e["hash"]:
                return False, e["seq"]
            prev = e["hash"]
        return True, None

    def anchor(self) -> dict:
        """锚定链头（模拟: 生成可发往链上/公证的摘要引用）"""
        head = self._entries[-1]["hash"] if self._entries else "EMPTY"
        ref = {"anchor_id": uuid.uuid4().hex, "head_hash": head,
               "ts": time.time(), "external_ref": f"anchor://{head[:16]}"}
        self._anchors.append(ref)
        return ref

    def tail(self, n: int = 5) -> List[dict]:
        return self._entries[-n:]

    def tamper(self, seq: int, actor: str = "HACKER"):
        """故意篡改（自检用）"""
        self._entries[seq - 1]["actor"] = actor


# ════════════════════════════════════════════════════════════════
# 5. 双人控制（M-of-N 审批）
# ════════════════════════════════════════════════════════════════

class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    TIMEOUT = "TIMEOUT"


@dataclass
class ApprovalRequest:
    id: str
    action: str
    payload: dict
    threshold: int
    approvers: List[str]
    status: ApprovalStatus = ApprovalStatus.PENDING
    approved_by: List[str] = field(default_factory=list)
    denied_by: str = ""
    created_at: float = field(default_factory=time.time)
    timeout_at: float = 0.0


class DualControl:
    """
    双人控制（M-of-N 多签审批）。

    与链上 Gnosis Safe 语义一致: 达到 threshold 个不同审批人签名才
    放行。支持拒绝/超时。审批记录全部写入审计日志（哈希链）, 事后可查。

    用法:
      dc = DualControl(threshold=2, approvers=["alice", "bob", "carol"])
      req = dc.request("WITHDRAW", {"amount": 1000, "to": "0x..."})
      dc.approve(req.id, "alice")
      dc.approve(req.id, "bob")          # threshold 达成 → APPROVED
      dc.status(req.id)
    """

    def __init__(self, threshold: int = 2, approvers: Optional[List[str]] = None,
                 timeout_sec: float = 3600.0, audit: Optional[AuditLogger] = None,
                 on_approved: Optional[Callable] = None):
        self.threshold = threshold
        self.approvers = approvers or ["alice", "bob"]
        self.timeout_sec = timeout_sec
        self.audit = audit or AuditLogger()
        self.on_approved = on_approved
        self._requests: Dict[str, ApprovalRequest] = {}

    def request(self, action: str, payload: dict, threshold: Optional[int] = None) -> ApprovalRequest:
        rid = uuid.uuid4().hex[:12]
        req = ApprovalRequest(
            id=rid, action=action, payload=payload,
            threshold=threshold or self.threshold,
            approvers=list(self.approvers),
            timeout_at=time.time() + self.timeout_sec,
        )
        self._requests[rid] = req
        self.audit.append("system", "APPROVAL_REQUEST", {"id": rid, "action": action,
                                                         "threshold": req.threshold})
        return req

    def approve(self, request_id: str, actor: str) -> ApprovalStatus:
        req = self._requests.get(request_id)
        if not req:
            raise KeyError(request_id)
        self._expire_if_needed(req)
        if req.status != ApprovalStatus.PENDING:
            return req.status
        if actor not in req.approvers:
            return req.status
        if actor not in req.approved_by:
            req.approved_by.append(actor)
            self.audit.append(actor, "APPROVE", {"id": request_id, "action": req.action})
        if len(req.approved_by) >= req.threshold:
            req.status = ApprovalStatus.APPROVED
            self.audit.append("system", "APPROVAL_PASSED", {"id": request_id})
            if self.on_approved:
                self.on_approved(req)
        return req.status

    def deny(self, request_id: str, actor: str) -> ApprovalStatus:
        req = self._requests.get(request_id)
        if not req:
            raise KeyError(request_id)
        req.status = ApprovalStatus.DENIED
        req.denied_by = actor
        self.audit.append(actor, "DENY", {"id": request_id})
        return req.status

    def _expire_if_needed(self, req: ApprovalRequest):
        if req.status == ApprovalStatus.PENDING and req.timeout_at and \
           time.time() > req.timeout_at:
            req.status = ApprovalStatus.TIMEOUT
            self.audit.append("system", "APPROVAL_TIMEOUT", {"id": req.id})

    def status(self, request_id: str) -> ApprovalStatus:
        req = self._requests.get(request_id)
        if not req:
            raise KeyError(request_id)
        self._expire_if_needed(req)
        return req.status

    def pending(self) -> List[str]:
        return [r.id for r in self._requests.values() if r.status == ApprovalStatus.PENDING]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    wm = WalletManager()
    print("tier:", wm.approve_tx(50), wm.approve_tx(500), wm.approve_tx(50000))
    kms = SimKMSBackend()
    kid = kms.create_key("hot")
    d = hashlib.sha256(b"txdata").hexdigest()
    s = kms.sign(d, kid)
    print("kms verify:", kms.verify(d, kid, s))
    ks = EncryptedKeystore("/tmp/ks_test.bin")
    ks.save("hot", b"deadbeef", "pw")
    print("keystore roundtrip:", ks.load("hot", "pw") == b"deadbeef")
    al = AuditLogger()
    al.append("a", "X", {"n": 1})
    al.append("b", "Y", {"n": 2})
    print("audit ok:", al.verify_chain())
    al.tamper(1)
    print("audit after tamper:", al.verify_chain())
    dc = DualControl(threshold=2, approvers=["a", "b", "c"])
    r = dc.request("WITHDRAW", {"amount": 100})
    dc.approve(r.id, "a")
    print("after 1 approve:", dc.status(r.id))
    dc.approve(r.id, "b")
    print("after 2 approves:", dc.status(r.id))
