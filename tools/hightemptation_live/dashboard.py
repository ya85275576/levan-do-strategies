#!/usr/bin/env python3
"""
HighTempTation — Streamlit 实时看板

通过 HTTP API 读取天气 Bot 数据 (http://localhost:3002/api/status)，
展示:
  - 今日盈亏 / 胜率 / 资金曲线
  - 持仓（city / bucket / entry_no / curr_no / pnl）
  - 平仓记录
  - 自动 30s 刷新

启动:
  streamlit run dashboard.py
  streamlit run dashboard.py --server.port 8501

生产 (PM2):
  pm2 start ecosystem.dashboard.config.cjs

依赖:
  pip install streamlit plotly pandas
"""
import json
import logging
import os
import sys
import time as _time
from datetime import datetime, timezone
from typing import List, Optional
from urllib.request import urlopen, Request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import streamlit as st
    import plotly.graph_objects as go
    import pandas as pd
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

logger = logging.getLogger("dashboard")

API_URL = os.environ.get("API_URL", "http://localhost:3002/api/status")
REFRESH_SEC = 30


# ════════════════════════════════════════════════════════════════
# HTTP 数据获取
# ════════════════════════════════════════════════════════════════

def fetch_api_data() -> dict:
    """从 /api/status 获取天气 Bot 实时数据"""
    try:
        req = Request(API_URL, headers={"Accept": "application/json"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return data
    except Exception as e:
        logger.warning(f"API 请求失败: {e}")
        return {}


def load_data() -> dict:
    """从 API 加载所有看板数据，映射为 dashboard 格式"""
    now = datetime.now(timezone.utc)
    raw = fetch_api_data()

    if not raw:
        return {
            "daily_pnl": 0, "daily_trades": 0, "daily_wins": 0, "daily_losses": 0,
            "daily_win_rate": 0, "closed_trades": [], "open_positions": [],
            "equity_curve": [], "calibration": [],
            "signal_count_24h": 0, "total_trades": 0,
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "error": "无法连接天气 Bot API",
        }

    # ── 持仓: 将 API 字段映射为看板列名 ──
    open_positions = []
    for p in raw.get("open", []):
        open_positions.append({
            "city": p.get("city", "--"),
            "side": p.get("bucket", "--"),           # "37°C" 等温度桶
            "entry_price": p.get("entry_no", 0),
            "size": p.get("size", 0),
            "entry_time": p.get("entry_time", ""),
            "curr_no": p.get("curr_no", 0),
            "pnl": p.get("pnl", 0),
        })

    # ── 平仓 ──
    closed_trades = []
    for t in raw.get("recent_closed", []):
        closed_trades.append({
            "city": t.get("city", "--"),
            "side": t.get("bucket", "--"),
            "entry_price": t.get("entry_no", 0),
            "exit_price": t.get("curr_no", 0),
            "pnl": t.get("pnl", 0),
            "exit_reason": t.get("exit_reason", ""),
            "exit_time": t.get("exit_time", ""),
        })

    # ── 资金曲线: capital_history → 按日聚合 ──
    equity_curve = []
    capital_history = raw.get("capital_history", [])
    if capital_history:
        # capital_history 格式: [[iso_timestamp, capital_value], ...]
        # 按日期聚合：每天的最后一条减去上一天的最后一条 = 日盈亏
        daily_capitals = {}
        for ts_str, cap in capital_history:
            try:
                day = ts_str[:10]  # "2026-07-29"
                daily_capitals[day] = cap
            except Exception:
                pass
        sorted_days = sorted(daily_capitals.keys())
        prev_cap = None
        for day in sorted_days:
            cap = daily_capitals[day]
            if prev_cap is not None:
                day_pnl = round(cap - prev_cap, 2)
            else:
                day_pnl = round(cap - raw.get("initial_capital", cap), 2)
            equity_curve.append({"d": day, "day_pnl": day_pnl})
            prev_cap = cap

    return {
        "daily_pnl": raw.get("daily_pnl", 0),
        "daily_trades": raw.get("total", 0),
        "daily_wins": raw.get("wins", 0),
        "daily_losses": raw.get("losses", 0),
        "daily_win_rate": raw.get("win_rate", 0),
        "closed_trades": closed_trades,
        "open_positions": open_positions,
        "equity_curve": equity_curve,
        "calibration": [],                         # 校准需 DB，API 不提供
        "signal_count_24h": 0,                     # 信号计数需 DB
        "total_trades": raw.get("total", 0),
        "current_time": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "capital": raw.get("capital", 0),
        "initial_capital": raw.get("initial_capital", 0),
        "open_count": raw.get("open_count", 0),
    }


# ════════════════════════════════════════════════════════════════
# 渲染函数
# ════════════════════════════════════════════════════════════════

def render_metric_card(label: str, value: str, delta: str = "",
                        color: str = "normal"):
    """指标卡片"""
    delta_color = "normal"
    if delta.startswith("+"):
        delta_color = "green"
    elif delta.startswith("-"):
        delta_color = "red"

    st.metric(label=label, value=value, delta=delta if delta else None,
              delta_color=delta_color)


def render_equity_chart(equity_rows: List[dict]):
    """资金曲线图 (Plotly)"""
    if not equity_rows:
        st.info("暂无资金曲线数据")
        return

    df = pd.DataFrame(equity_rows)
    df["d"] = pd.to_datetime(df["d"])
    df["cum_pnl"] = df["day_pnl"].cumsum()

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["d"], y=df["cum_pnl"],
        mode="lines+markers",
        name="累计盈亏",
        line=dict(color="#58a6ff", width=2),
        fill="tozeroy",
        fillcolor="rgba(88, 166, 255, 0.15)",
    ))
    fig.update_layout(
        title="资金曲线",
        xaxis_title="日期",
        yaxis_title="累计盈亏 ($)",
        template="plotly_dark",
        height=350,
        margin=dict(l=40, r=20, t=40, b=40),
    )
    fig.add_hline(y=0, line=dict(color="#484f58", width=1, dash="dash"))
    st.plotly_chart(fig, use_container_width=True)


def render_calibration_chart(calibration: List[dict]):
    """校准可靠性图 — API 不提供此数据"""
    st.info("📊 校准数据需连接数据库，实时 API 暂未提供")


def render_positions(positions: List[dict]):
    """持仓表 — 映射: city | bucket(温度桶) | entry_no | curr_no | pnl | size"""
    if not positions:
        st.info("🟢 无持仓")
        return
    df = pd.DataFrame(positions)
    # 显示 API 原生字段名以便查看实时数据
    cols = ["city", "side", "entry_price", "curr_no", "pnl", "size", "entry_time"]
    available = [c for c in cols if c in df.columns]
    st.dataframe(df[available], use_container_width=True, height=250)


def render_closed_trades(trades: List[dict]):
    """最近平仓"""
    if not trades:
        st.info("⏳ 暂无平仓记录")
        return
    df = pd.DataFrame(trades)
    cols = ["city", "side", "entry_price", "exit_price", "pnl", "exit_reason", "exit_time"]
    available = [c for c in cols if c in df.columns]
    st.dataframe(df[available].head(20), use_container_width=True, height=300)


# ════════════════════════════════════════════════════════════════
# Streamlit 主界面
# ════════════════════════════════════════════════════════════════

def main():
    if not HAS_STREAMLIT:
        print("请安装 streamlit: pip install streamlit plotly pandas")
        print("然后运行: streamlit run dashboard.py")
        return

    st.set_page_config(
        page_title="HighTempTation 看板",
        page_icon="🌤️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    # 自定义暗色主题 CSS
    st.markdown("""
    <style>
    .stApp { background-color: #0d1117; color: #c9d1d9; }
    .stMetric { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px; }
    .stMetric label { color: #8b949e !important; }
    .stMetric .metric-value { color: #f0f6fc !important; }
    h1, h2, h3 { color: #58a6ff !important; }
    </style>
    """, unsafe_allow_html=True)

    st.title("🌤️ HighTempTation 实时看板")
    st.caption(f"数据源: {API_URL}  (来自天气 Bot 实时内存)")

    # 自动刷新
    last_refresh = st.empty()
    placeholder = st.empty()

    with placeholder.container():
        data = load_data()

        # 时间
        last_refresh.caption(
            f"最后刷新: {data['current_time']}  (每 {REFRESH_SEC}s 自动)"
            + (f"  ⚠️ {data.get('error', '')}" if data.get('error') else "")
        )

        # ── 指标行 ──
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            pnl = data["daily_pnl"]
            render_metric_card("今日 P&L", f"${pnl:+.2f}",
                               delta=f"{'🟢' if pnl>=0 else '🔴'} {pnl:+.2f}")
        with col2:
            render_metric_card("总交易", str(data["daily_trades"]))
        with col3:
            wr = data["daily_win_rate"]
            render_metric_card("胜率", f"{wr:.1f}%",
                               delta=f"{data['daily_wins']}胜 / {data['daily_losses']}负")
        with col4:
            oc = data.get("open_count", len(data["open_positions"]))
            render_metric_card("持仓", str(oc),
                               delta=f"资金: ${data.get('capital', 0):.0f}")
        with col5:
            render_metric_card("累计交易", str(data["total_trades"]))

        # ── 图表行 ──
        col1, col2 = st.columns(2)
        with col1:
            render_equity_chart(data["equity_curve"])
        with col2:
            render_calibration_chart(data["calibration"])

        # ── 持仓 + 平仓 ──
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("📋 当前持仓")
            render_positions(data["open_positions"])
        with col2:
            st.subheader("📜 最近平仓")
            render_closed_trades(data["closed_trades"])

    # 自动刷新
    _time.sleep(REFRESH_SEC)
    st.rerun()


if __name__ == "__main__":
    main()
