"""
polymarket_5min_bot — Polymarket 5 分钟加密 Up/Down 市场交易子模块
===============================================================

整合自 Benjam1nCup/Polymarket-trading-bot-python-V2 的策略理念
(该上游仓库仅含 README 营销页, 无源码; 本模块依据其 12 篇策略文档
实现四大类策略):

  🧲 套利 (arbitrage)  — 互补套利: 先买高概率侧 (Buy1), 立即反手买
                         对面 (Buy2), 组合成本 ≈ $0.95, 结算赎回 $1.00
  🎯 狙击 (sniper)     — Endcycle Sniper: 周期结束前价格 ≥ 阈值 (0.95)
                         买入高概率侧, 结算自动赎回
  ⚡ 动量 (momentum)    — 订单簿流动性动量: OBI (order-book influence)
                         捕捉买卖压力突变 + 现货价与行权价差确认
  🪜 阶梯 (ladder)     — Ladder 双向挂单做市 (YES+NO 组合价值 > 1.01)
                         + Stair 结算前分批流动性感知出场

架构 (本模块独立可运行, 亦通过 adapters/polymarket_5min_adapter.py
挂入 HighTempTation 统一风控/账户/看板):

  markets.py   → Gamma API 扫描真实 5min 市场, 无市时回退模拟市场
  clob.py      → 轻量 CLOB 客户端 (订单簿/下单; DRY_RUN 模拟撮合)
  obi.py       → Order Book Influence 计算
  spot_price.py→ 现货价格轮询 (Coinbase), 失败时回退模拟行情
  engine.py    → FiveMinEngine 主循环 (asyncio)
  strategies/  → base / arbitrage / sniper / momentum / ladder / stair

依赖: httpx (无第三方 CLOB 客户端依赖, 保持轻量)
"""
__version__ = "1.0.0"
__all__ = ["engine", "markets", "clob", "obi", "spot_price"]
