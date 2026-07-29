#!/usr/bin/env python3
"""
HighTempTation — Streamlit 实时看板

连接 SQLite，展示:
  - 今日盈亏 / 胜率 / 资金曲线
  - 持仓 / 信号 / 熔断状态
  - 校准可靠性图 (Plotly)
  - 自动 30s 刷新

启动:
  streamlit run tools/hightemptation_live/dashboard.py
  streamlit run tools/hightemptation_live/dashboard.py --server.port 8501

依赖:
  pip install streamlit plotly pandas
"""
import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

# 确保能导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import streamlit as st
    import plotly.graph_objects as go
    import plotly.express as px
    import pandas as pd
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

from db_manager import TradeDB
from calibrator import ProbabilityCalibrator

logger = logging.getLogger("dashboard")

DB_PATH = os.environ.get("DB_PATH", "hightemptation.db")
REFRESH_SEC = 30


def load_data(db: TradeDB) -> dict:
    """一次性加载所有看板数据"""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    # 今日统计
    daily = db.get_daily_pnl(today)

    # 所有已平仓交易
    closed = db.conn.execute(
        "SELECT * FROM trades WHERE status='closed' ORDER BY exit_time DESC LIMIT 100"
    ).fetchall()

    # 持仓
    open_positions = db.get_open_trades()

    # 资金曲线: 按日聚合 PnL
    equity_rows = db.conn.execute("""
        SELECT date(exit_time) as d, SUM(pnl) as day_pnl, COUNT(*) as cnt
        FROM trades WHERE status='closed' AND exit_time IS NOT NULL
        GROUP BY date(exit_time) ORDER BY d
    """).fetchall()

    # 校准数据
    calibrator = ProbabilityCalibrator(db)
    reliability = calibrator.get_calibration_reliability()

    # 信号计数
    signal_count = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM market_prices WHERE ts > ?",
        (int((now - timedelta(days=1)).timestamp() * 1000),),
    ).fetchone()["cnt"] or 0

    # 总交易数
    total_trades = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM trades"
    ).fetchone()["cnt"] or 0

    return {
        "daily_pnl": daily["total_pnl"],
        "daily_trades": daily["cnt"],
        "daily_wins": daily["wins"],
        "daily_losses": daily["losses"],
        "daily_win_rate": (daily["wins"] / daily["cnt"] * 100) if daily["cnt"] > 0 else 0,
        "closed_trades": [dict(r) for r in closed],
        "open_positions": open_positions,
        "equity_curve": [dict(r) for r in equity_rows],
        "calibration": reliability,
        "signal_count_24h": signal_count,
        "total_trades": total_trades,
        "current_time": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def render_metric_card(label: str, value: str, delta: str = "",
                        color: str = "normal"):
    """指标卡片"""
    delta_color = "normal"
    if delta.startswith("+"):
        delta_color = "green" if "﹣" not in label else "red"
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
    """校准可靠性图 (Plotly)"""
    if not calibration:
        st.info("暂无校准数据 (需要更多已结算交易)")
        return

    df = pd.DataFrame(calibration)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["avg_model"], y=df["avg_realized"],
        mode="markers+text",
        name="校准点",
        marker=dict(size=df["count"], sizemode="area", sizeref=2.*max(df["count"])/(40.**2),
                    color=df["count"], colorscale="Viridis", showscale=True,
                    colorbar=dict(title="样本数")),
        text=df["bin"],
        textposition="top center",
    ))
    # 理想校准线 (对角线)
    fig.add_trace(go.Scatter(
        x=[0, 1], y=[0, 1],
        mode="lines",
        name="理想校准",
        line=dict(color="#0ecb81", width=1, dash="dash"),
    ))
    fig.update_layout(
        title="校准可靠性",
        xaxis=dict(title="模型概率", range=[0, 1]),
        yaxis=dict(title="实现概率", range=[0, 1]),
        template="plotly_dark",
        height=400,
        margin=dict(l=40, r=20, t=40, b=40),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_positions(positions: List[dict]):
    """持仓表"""
    if not positions:
        st.info("🟢 无持仓")
        return
    df = pd.DataFrame(positions)
    cols = ["city", "side", "entry_price", "size", "entry_time"]
    available = [c for c in cols if c in df.columns]
    st.dataframe(df[available], use_container_width=True, height=200)


def render_closed_trades(trades: List[dict]):
    """最近平仓"""
    if not trades:
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

    # 自动刷新
    last_refresh = st.empty()
    placeholder = st.empty()

    db = TradeDB(DB_PATH)

    with placeholder.container():
        data = load_data(db)

        # 时间
        last_refresh.caption(f"最后刷新: {data['current_time']}  (每 {REFRESH_SEC}s 自动)")

        # ── 指标行 ──
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            pnl = data["daily_pnl"]
            render_metric_card("今日 P&L", f"${pnl:+.2f}",
                               delta=f"{'🟢' if pnl>=0 else '🔴'} {pnl:+.2f}")
        with col2:
            render_metric_card("今日交易", str(data["daily_trades"]))
        with col3:
            wr = data["daily_win_rate"]
            render_metric_card("今日胜率", f"{wr:.1f}%",
                               delta=f"{data['daily_wins']}胜 / {data['daily_losses']}负")
        with col4:
            render_metric_card("持仓", str(len(data["open_positions"])),
                               delta=f"24h信号: {data['signal_count_24h']}")
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
    import time as _time
    _time.sleep(REFRESH_SEC)
    st.rerun()


if __name__ == "__main__":
    main()
