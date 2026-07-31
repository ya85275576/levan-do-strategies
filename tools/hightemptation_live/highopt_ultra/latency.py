#!/usr/bin/env python3
"""
HighTempTation — 延迟优化（第四波优化 #4）

功能:
  1. RPCNodeSelector — Polygon 节点地理部署（延迟探测 + 就近排序 + 故障转移）
  2. WSOrderBookFeed — WebSocket 订单簿（快照+增量 → 本地 LOB 维护, 事件回调）
  3. KernelTuner     — CPU 绑核 / TCP BBR（亲和性设置 + 拥塞控制检测与调优建议）

用法:
  from highopt_ultra.latency import (
      RPCNodeSelector, WSOrderBookFeed, OrderBookState, KernelTuner,
  )

  sel = RPCNodeSelector(nodes=[("polygon-io", "https://polygon-rpc.com", "us-east"), ...])
  sel.probe()                          # 实际测延迟 → ranked
  best = sel.pick()

  feed = WSOrderBookFeed(symbol="Tokyo-NO")
  feed.on_snapshot = cb_snap
  feed.on_delta = cb_delta
  for msg in feed.simulate(20):        # 模拟 WS 消息流
      feed.apply(msg)

  kt = KernelTuner()
  report = kt.report()
"""
import logging
import os
import platform
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("highopt_ultra.latency")

# ════════════════════════════════════════════════════════════════
# 1. Polygon 节点地理部署
# ════════════════════════════════════════════════════════════════

# 地域评分（粗略地理启发: 数字越小越近）
REGION_NEAR = {"tokyo": 1, "singapore": 2, "hk": 2, "seoul": 2, "us-west": 3,
               "us-east": 4, "eu": 5, "default": 9}

DEFAULT_NODES = [
    ("polygon-io",    "https://polygon-rpc.com",            "us-east"),
    ("publicnode",    "https://polygon-bor-rpc.publicnode.com", "tokyo"),
    ("1rpc",          "https://1rpc.io/matic",              "singapore"),
    ("ankr",          "https://rpc.ankr.com/polygon",       "us-west"),
    ("self-hosted",   "http://10.0.0.8:8545",               "tokyo"),
]


@dataclass
class NodeStats:
    name: str
    url: str
    region: str
    latency_ms: float = 999.0
    region_score: int = 9
    alive: bool = False
    last_error: str = ""

    @property
    def rank_score(self) -> float:
        """综合排序分: 延迟为主, 地域为辅（就近 + 低延迟）"""
        return self.latency_ms + self.region_score * 20.0


class RPCNodeSelector:
    """
    节点选择器（就近 + 低延迟 + 故障转移）。

    probe() 对每个节点做 TCP/HTTP 延迟测量（socket connect 计时,
    失败则 latency=999 / alive=False）。按 rank_score 排序,
    pick() 返回最优节点, fallback() 依次给出备选（自动跳过不可用）。

    用法:
      sel = RPCNodeSelector(DEFAULT_NODES)
      sel.probe()
      best = sel.pick()                  # NodeStats
      for node in sel.fallback(limit=3): # 备选链路
          ...
    """

    def __init__(self, nodes: Optional[List[Tuple[str, str, str]]] = None,
                 probe_timeout: float = 2.0):
        self.nodes = [NodeStats(n, u, r,
                                region_score=REGION_NEAR.get(r, REGION_NEAR["default"]))
                      for n, u, r in (nodes or DEFAULT_NODES)]
        self.probe_timeout = probe_timeout
        self._probed_at = 0.0

    def _measure(self, url: str) -> Tuple[Optional[float], str]:
        """socket connect 计时（http 则解析 host:port）"""
        host, port = "127.0.0.1", 443
        try:
            if "://" in url:
                rest = url.split("://", 1)[1]
                host = rest.split("/")[0].split(":")[0]
                if ":" in rest.split("/")[0]:
                    port = int(rest.split("/")[0].split(":")[1])
            t0 = time.perf_counter()
            with socket.create_connection((host, port), timeout=self.probe_timeout):
                ms = (time.perf_counter() - t0) * 1000.0
            return ms, ""
        except Exception as e:  # noqa: BLE001
            return None, str(e)[:80]

    def probe(self) -> List[NodeStats]:
        for node in self.nodes:
            ms, err = self._measure(node.url)
            node.latency_ms = ms if ms is not None else 999.0
            node.alive = ms is not None
            node.last_error = err
        self._probed_at = time.time()
        self.nodes.sort(key=lambda n: n.rank_score)
        return self.nodes

    def pick(self) -> Optional[NodeStats]:
        alive = [n for n in self.nodes if n.alive]
        return alive[0] if alive else None

    def fallback(self, limit: int = 3) -> List[NodeStats]:
        return [n for n in self.nodes if n.alive][:limit]

    def report(self) -> dict:
        return {
            "probed_at": self._probed_at,
            "best": self.pick().name if self.pick() else None,
            "alive_count": sum(1 for n in self.nodes if n.alive),
            "ranking": [{"name": n.name, "latency_ms": round(n.latency_ms, 1),
                         "region": n.region, "alive": n.alive}
                        for n in self.nodes],
        }


# ════════════════════════════════════════════════════════════════
# 2. WebSocket 订单簿
# ════════════════════════════════════════════════════════════════

class OrderBookState:
    """本地维护的订单簿状态（快照 + 增量应用）"""

    def __init__(self):
        self.bids: Dict[float, float] = {}    # price → size
        self.asks: Dict[float, float] = {}
        self.last_update = 0.0

    def apply_snapshot(self, bids: List[Tuple[float, float]],
                       asks: List[Tuple[float, float]]):
        self.bids = {p: s for p, s in bids}
        self.asks = {p: s for p, s in asks}
        self.last_update = time.time()

    def apply_delta(self, bids: List[Tuple[float, float]],
                    asks: List[Tuple[float, float]]):
        """增量: size=0 表示删除该档"""
        for p, s in bids:
            if s == 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = s
        for p, s in asks:
            if s == 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = s
        self.last_update = time.time()

    @property
    def best_bid(self) -> float:
        return max(self.bids) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return min(self.asks) if self.asks else 0.0

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0 if self.bids and self.asks else 0.0

    def top_n(self, n: int = 5) -> dict:
        return {
            "bids": sorted(self.bids.items(), reverse=True)[:n],
            "asks": sorted(self.asks.items())[:n],
        }


class WSOrderBookFeed:
    """
    WebSocket 订单簿订阅器。

    生产接入: 用 websockets/httpx 连交易所 WS 端点, 把消息喂给 apply()。
    这里提供 simulate() 生成合成消息流, 验证增量合并正确性。

    用法:
      feed = WSOrderBookFeed("Tokyo-NO")
      for msg in feed.simulate(steps=30):
          feed.apply(msg)
      feed.state.top_n(3)
    """

    def __init__(self, symbol: str = ""):
        self.symbol = symbol
        self.state = OrderBookState()
        self.msg_count = 0
        self.on_message: Optional[Callable[[dict], None]] = None

    def apply(self, msg: dict):
        self.msg_count += 1
        kind = msg.get("type")
        if kind == "snapshot":
            self.state.apply_snapshot(msg.get("bids", []), msg.get("asks", []))
        elif kind == "delta":
            self.state.apply_delta(msg.get("bids", []), msg.get("asks", []))
        if self.on_message:
            self.on_message(msg)

    def simulate(self, steps: int = 30, mid: float = 0.40) -> List[dict]:
        """生成合成 WS 消息流（快照 + 增量, 保持 best_ask > best_bid）"""
        rng = __import__("random").Random(9)
        bid_prices = [round(mid - 0.01 * i, 2) for i in range(1, 6)]
        ask_prices = [round(mid + 0.01 * i, 2) for i in range(1, 6)]
        msgs = [{"type": "snapshot",
                 "bids": [(p, 200 + rng.randint(0, 80)) for p in bid_prices],
                 "asks": [(p, 200 + rng.randint(0, 80)) for p in ask_prices]}]
        for _ in range(steps - 1):
            side = rng.choice(["bids", "asks"])
            price = rng.choice(bid_prices if side == "bids" else ask_prices)
            msgs.append({"type": "delta", side: [(price, rng.randint(1, 300))]})
        return msgs


# ════════════════════════════════════════════════════════════════
# 3. CPU 绑核 / TCP BBR
# ════════════════════════════════════════════════════════════════

class KernelTuner:
    """
    内核级延迟调优（CPU 绑核 + TCP BBR）。

      - set_affinity(pid, cores): 进程绑核（Linux os.sched_setaffinity）
      - get_tcp_congestion(): 读取当前 TCP 拥塞控制算法
      - set_tcp_congestion("bbr"): 切换 BBR（需 root, 失败给出建议）
      - report(): 汇总检测结果与调优建议

    用法:
      kt = KernelTuner()
      kt.set_affinity(os.getpid(), cores=[0, 1])   # 绑定前两个核
      print(kt.get_tcp_congestion())               # 'cubic' / 'bbr'
      kt.set_tcp_congestion("bbr")
      print(kt.report())
    """

    SYSCTL_PATH = "/proc/sys/net/ipv4/tcp_congestion_control"

    def __init__(self):
        self.affinity_ok = False
        self.bbr_ok = False

    def set_affinity(self, pid: int = None, cores: Optional[List[int]] = None) -> bool:
        """绑定进程到指定 CPU 核（Linux）"""
        pid = pid or os.getpid()
        if not hasattr(os, "sched_setaffinity"):
            logger.warning("当前平台不支持 sched_setaffinity")
            return False
        try:
            avail = list(os.sched_getaffinity(pid))
            cores = cores or avail[:1]
            bad = [c for c in cores if c not in avail]
            if bad:
                logger.warning(f"核 {bad} 不在可用集合 {avail} 内")
                return False
            os.sched_setaffinity(pid, set(cores))
            self.affinity_ok = True
            return True
        except (OSError, AttributeError) as e:
            logger.warning(f"绑核失败: {e}")
            return False

    def get_affinity(self, pid: int = None) -> List[int]:
        pid = pid or os.getpid()
        if hasattr(os, "sched_getaffinity"):
            try:
                return sorted(os.sched_getaffinity(pid))
            except OSError:
                return []
        return []

    def get_tcp_congestion(self) -> str:
        try:
            with open(self.SYSCTL_PATH) as f:
                return f.read().strip()
        except OSError:
            return "unknown"

    def set_tcp_congestion(self, algo: str = "bbr") -> Tuple[bool, str]:
        """切换拥塞控制算法（需 root; BBR 需内核 >= 4.9）"""
        try:
            with open(self.SYSCTL_PATH, "w") as f:
                f.write(algo)
            self.bbr_ok = (self.get_tcp_congestion() == algo)
            return self.bbr_ok, f"已切换为 {algo}"
        except PermissionError:
            return False, "需要 root 权限: 请以 sudo 执行或加入系统调优脚本"
        except OSError as e:
            return False, f"切换失败: {e}"

    def report(self) -> dict:
        cores = os.cpu_count() or 0
        cc = self.get_tcp_congestion()
        suggestions = []
        if cc != "bbr":
            suggestions.append("建议启用 BBR: sudo sysctl -w net.ipv4.tcp_congestion_control=bbr")
        if cores < 4:
            suggestions.append("CPU 核数过少, 建议专用实例或降低并发")
        if platform.system() != "Linux":
            suggestions.append("非 Linux 平台无法绑核/BBR, 建议 Linux 生产环境")
        return {
            "platform": platform.system(),
            "cpu_cores": cores,
            "current_affinity": self.get_affinity(),
            "tcp_congestion": cc,
            "bbr_enabled": cc == "bbr",
            "suggestions": suggestions,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sel = RPCNodeSelector()
    sel.probe()
    print("node report:", sel.report())
    feed = WSOrderBookFeed("Tokyo-NO")
    for m in feed.simulate(25):
        feed.apply(m)
    print("ws feed:", feed.msg_count, "mid=", round(feed.state.mid, 4),
          "top_bid=", feed.state.top_n(1)["bids"])
    kt = KernelTuner()
    print("affinity:", kt.set_affinity(cores=[0]), kt.get_affinity())
    print("kernel:", kt.report())
