"""polymarket_5min_bot — Order Book Influence (OBI) 计算

源自 Benjam1nCup 流动性动量策略的核心信号:
  用订单簿挂单量而非价格变化衡量买卖压力。

OBI = (买方挂单价值 - 卖方挂单价值) / (买方挂单价值 + 卖方挂单价值)
  ∈ [-1, 1]; OBI > 0 → 买方压力占优 (看涨), < 0 → 卖方压力占优。

相比价格动量, OBI 领先 1~3 秒, 适合 5 分钟周期内捕捉流动性突变。
"""
from typing import Optional


def compute_obi(bids: list, asks: list,
                depth_cents: float = 0.05) -> Optional[float]:
    """
    计算订单簿不均衡度。

    Args:
        bids: [{price, size}, ...] 按价格降序 (最优买价在前)
        asks: [{price, size}, ...] 按价格升序 (最优卖价在前)
        depth_cents: 只统计最优价上下 depth 范围内的挂单 (0.05 = 5 分)

    Returns:
        OBI ∈ [-1, 1]; 无有效深度时返回 None
    """
    if not bids or not asks:
        return None
    best_bid = bids[0]["price"]
    best_ask = asks[0]["price"]
    mid = (best_bid + best_ask) / 2.0

    buy_vol = 0.0   # 买方挂单名义价值
    sell_vol = 0.0  # 卖方挂单名义价值
    for b in bids:
        if b["price"] >= mid - depth_cents:
            buy_vol += b["price"] * b["size"]
    for a in asks:
        if a["price"] <= mid + depth_cents:
            sell_vol += a["price"] * a["size"]

    total = buy_vol + sell_vol
    if total <= 0:
        return None
    return (buy_vol - sell_vol) / total


def compute_imbalance_signal(obi: float, threshold: float) -> str:
    """
    将 OBI 映射为信号方向。
      OBI ≥ +threshold → "YES" (看涨)
      OBI ≤ -threshold → "NO"  (看跌)
      否则 → "" (无信号)
    """
    if obi >= threshold:
        return "YES"
    if obi <= -threshold:
        return "NO"
    return ""


def price_deviation(spot: float, strike: float) -> float:
    """现货价相对行权价的偏离 (比例, 可正可负)"""
    if strike <= 0:
        return 0.0
    return (spot - strike) / strike


def confirm_direction(obi_signal: str, dev: float) -> bool:
    """
    动量确认: OBI 方向与现货偏离方向一致才算数。
    OBI=YES 且 dev>0 (现价高于行权价) → 确认看涨。
    """
    if obi_signal == "YES" and dev > 0:
        return True
    if obi_signal == "NO" and dev < 0:
        return True
    return False
