#!/usr/bin/env python3
"""
HighTempTation — 自动策略权重优化器

基于过去 N 天的信号历史表现（胜率、夏普、盈亏），自动调整各策略的权重。
权重写入 optimization_log.json 供仪表盘读取。

使用方法:
    python auto_optimizer.py                    # 默认 30 天
    python auto_optimizer.py --days 60          # 60 天回看
    python auto_optimizer.py --dry-run          # 预览不写入
    python auto_optimizer.py --min-samples 5    # 最少样本要求
"""

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("auto_optimizer")

# 默认 DB 路径（与 bot.py 一致）
DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "dashboard", "hightemptation.db"
)
OPTIMIZATION_LOG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "dashboard", "optimization_log.json"
)

# 策略标签列表
STRATEGY_KEYS = [
    "ladder",
    "extreme_coldmath",
    "near_lock",
    "model_edge",
    "hko_confirm",
]

STRATEGY_LABELS = {
    "ladder": "🪜 温度阶梯",
    "extreme_coldmath": "❄️ 极端低估",
    "near_lock": "🔒 近结算锁利",
    "model_edge": "📐 模型边缘",
    "hko_confirm": "🏛️ 天文台确认",
}


def load_signal_history(db_path: str, days: int = 30) -> list[dict]:
    """
    从 SQLite 读取信号历史记录。

    返回 [{
        strategy, signal_type, pnl, actual_result, created_at, ...
    }]
    """
    import sqlite3

    if not os.path.exists(db_path):
        logger.warning(f"⚠️ DB 文件不存在: {db_path}")
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT * FROM signal_history
            WHERE actual_result IS NOT NULL
              AND created_at >= ?
            ORDER BY created_at ASC
        """, (cutoff,)).fetchall()

        conn.close()
        results = [dict(r) for r in rows]
        logger.info(f"📥 读取 {len(results)} 条信号历史 (过去 {days} 天)")
        return results
    except Exception as e:
        logger.error(f"❌ 读取信号历史失败: {e}")
        return []


def compute_strategy_metrics(rows: list[dict]) -> dict:
    """
    对每个策略计算综合指标。

    Returns:
        dict[strategy_name] = {
            total, wins, losses, win_rate,
            total_pnl, avg_pnl, sharpe, max_drawdown,
            pnl_series, score, weight
        }
    """
    # 按策略分组
    groups: dict[str, list[dict]] = {}
    for r in rows:
        s = r.get("strategy") or "model_edge"
        if s not in groups:
            groups[s] = []
        groups[s].append(r)

    result = {}
    for s_key in STRATEGY_KEYS:
        grp = groups.get(s_key, [])
        label = STRATEGY_LABELS.get(s_key, s_key)

        total = len(grp)
        if total == 0:
            result[s_key] = {
                "label": label,
                "total": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
                "sharpe": 0.0, "max_drawdown": 0.0,
                "pnl_series": [],
                "score": 0.0, "weight": 0.0,
            }
            continue

        wins = sum(1 for r in grp if r.get("actual_result") == 1)
        losses = sum(1 for r in grp if r.get("actual_result") == 0)
        pnl_series = [float(r.get("pnl", 0) or 0) for r in grp]
        total_pnl = sum(pnl_series)
        avg_pnl = total_pnl / total if total > 0 else 0
        win_rate = wins / total if total > 0 else 0

        # 夏普比率
        sharpe = 0.0
        if len(pnl_series) >= 3:
            mean_pnl = total_pnl / len(pnl_series)
            var_pnl = sum((p - mean_pnl) ** 2 for p in pnl_series) / len(pnl_series)
            if var_pnl > 0:
                daily_sharpe = mean_pnl / (var_pnl ** 0.5)
                sharpe = daily_sharpe * (365 ** 0.5)  # 年化

        # 最大回撤
        max_drawdown = 0.0
        cum = 0.0
        peak = 0.0
        for p in pnl_series:
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_drawdown:
                max_drawdown = dd

        result[s_key] = {
            "label": label,
            "total": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown": round(max_drawdown, 2),
            "pnl_series": [round(p, 2) for p in pnl_series[-60:]],
            "score": 0.0,
            "weight": 0.0,
        }

    # 补充未在 STRATEGY_KEYS 中的策略
    for s_key in groups:
        if s_key not in result:
            grp = groups[s_key]
            total = len(grp)
            wins = sum(1 for r in grp if r.get("actual_result") == 1)
            pnl_series = [float(r.get("pnl", 0) or 0) for r in grp]
            total_pnl = sum(pnl_series)
            avg_pnl = total_pnl / total if total > 0 else 0
            win_rate = wins / total if total > 0 else 0
            sharpe = 0.0
            if len(pnl_series) >= 3:
                mean_pnl = total_pnl / len(pnl_series)
                var_pnl = sum((p - mean_pnl) ** 2 for p in pnl_series) / len(pnl_series)
                if var_pnl > 0:
                    daily_sharpe = mean_pnl / (var_pnl ** 0.5)
                    sharpe = daily_sharpe * (365 ** 0.5)
            max_drawdown = 0.0
            cum = 0.0
            peak = 0.0
            for p in pnl_series:
                cum += p
                if cum > peak:
                    peak = cum
                dd = peak - cum
                if dd > max_drawdown:
                    max_drawdown = dd
            result[s_key] = {
                "label": s_key,
                "total": total, "wins": wins,
                "losses": total - wins,
                "win_rate": round(win_rate, 4),
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(avg_pnl, 2),
                "sharpe": round(sharpe, 2),
                "max_drawdown": round(max_drawdown, 2),
                "pnl_series": [round(p, 2) for p in pnl_series[-60:]],
                "score": 0.0, "weight": 0.0,
            }

    return result


def compute_weights(metrics: dict, min_samples: int = 3) -> dict:
    """
    根据指标计算每个策略的动态权重。

    权重公式:
        score = max(0, sharpe) * (1 + total_pnl / max_abs_pnl) * min(1, total / min_samples_factor)
    其中:
        - sharpe: 夏普比率，低于 0 的设为 0
        - total_pnl: 正贡献
        - total: 交易量因子，用 sqrt(total/min_samples) 做平滑

    归一化后确保和为 1.0。
    """
    scores = {}
    # 找最大 |总PnL| 用于归一化
    max_abs_pnl = max(
        (abs(m["total_pnl"]) for m in metrics.values() if m["total"] > 0),
        default=1.0
    )

    for s_key, m in metrics.items():
        if m["total"] < min_samples:
            scores[s_key] = 0.0
            continue

        raw_sharpe = max(0.0, m["sharpe"])  # 只需正夏普

        # PnL 贡献因子: 正总PnL加分，负总PnL扣分
        if m["total_pnl"] > 0:
            pnl_factor = 1.0 + m["total_pnl"] / max_abs_pnl
        elif m["total_pnl"] < 0:
            pnl_factor = max(0.1, 1.0 + m["total_pnl"] / max_abs_pnl)
        else:
            pnl_factor = 1.0

        # 样本量平滑: sqrt(N / min_samples)，避免小样本的偶然高分
        sample_factor = math.sqrt(m["total"] / min_samples)

        score = raw_sharpe * pnl_factor * sample_factor
        scores[s_key] = max(0.0, score)

    # 归一化
    total_score = sum(scores.values())
    if total_score > 0:
        for s_key in scores:
            metrics[s_key]["score"] = round(scores[s_key], 4)
            metrics[s_key]["weight"] = round(scores[s_key] / total_score * 100, 1)
    else:
        # 均匀分配
        active = sum(1 for m in metrics.values() if m["total"] >= min_samples)
        if active > 0:
            even = round(100.0 / active, 1)
            for s_key in metrics:
                if metrics[s_key]["total"] >= min_samples:
                    metrics[s_key]["weight"] = even

    return metrics


def save_optimization_log(metrics: dict, days: int, dry_run: bool = False) -> str:
    """
    将优化结果写入 optimization_log.json。

    文件结构:
    {
        "timestamp": "2025-07-15T12:00:00Z",
        "lookback_days": 30,
        "strategies": { ... },
        "summary": { ... }
    }
    """
    total_trades = sum(m["total"] for m in metrics.values())
    total_pnl = sum(m["total_pnl"] for m in metrics.values())
    total_wins = sum(m["wins"] for m in metrics.values())
    win_rate = round(total_wins / total_trades, 4) if total_trades > 0 else 0

    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lookback_days": days,
        "strategies": metrics,
        "summary": {
            "total_trades": total_trades,
            "total_pnl": round(total_pnl, 2),
            "overall_win_rate": win_rate,
            "active_strategies": sum(1 for m in metrics.values() if m["weight"] > 0),
        },
    }

    if dry_run:
        logger.info("🔍 [DRY RUN] 预览优化结果:")
        print(json.dumps(log_entry, indent=2, ensure_ascii=False))
        return ""

    try:
        os.makedirs(os.path.dirname(OPTIMIZATION_LOG), exist_ok=True)
        with open(OPTIMIZATION_LOG, "w", encoding="utf-8") as f:
            json.dump(log_entry, f, indent=2, ensure_ascii=False)
        logger.info(f"✅ 优化结果已写入: {OPTIMIZATION_LOG}")
        return OPTIMIZATION_LOG
    except Exception as e:
        logger.error(f"❌ 写入优化日志失败: {e}")
        return ""


def print_summary(metrics: dict):
    """打印优化结果摘要"""
    print("\n" + "=" * 70)
    print(f"  📊 策略权重优化报告 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("=" * 70)
    print(f"  {'策略':<20} {'交易':>6} {'胜率':>8} {'总PnL':>12} {'夏普':>8} {'回撤':>10} {'权重':>8}")
    print("  " + "-" * 70)

    sorted_items = sorted(metrics.items(), key=lambda x: x[1]["weight"], reverse=True)
    for s_key, m in sorted_items:
        label = STRATEGY_LABELS.get(s_key, m.get("label", s_key))
        wr = m["win_rate"] * 100
        total_pnl = m["total_pnl"]
        print(f"  {label:<20} {m['total']:>6} {wr:>7.1f}% "
              f"{'$'+f'{total_pnl:+.2f}':>12} {m['sharpe']:>7.2f} "
              f"{'$'+f'{m['max_drawdown']:.2f}':>9} {m['weight']:>7.1f}%")

    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="HighTempTation 自动策略权重优化")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite DB 路径 (默认: {DEFAULT_DB})")
    parser.add_argument("--days", type=int, default=30, help="回看天数 (默认: 30)")
    parser.add_argument("--min-samples", type=int, default=3, help="最少样本要求 (默认: 3)")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    args = parser.parse_args()

    logger.info(f"🚀 启动策略权重优化器 (回看 {args.days} 天, 最少样本 {args.min_samples})")

    rows = load_signal_history(args.db, args.days)
    if not rows:
        logger.warning("⚠️ 无信号历史数据，无法优化")
        print(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "lookback_days": args.days,
            "strategies": {},
            "summary": {"total_trades": 0, "total_pnl": 0, "overall_win_rate": 0, "active_strategies": 0},
            "error": "No signal history data found",
        }, indent=2, ensure_ascii=False))
        return

    metrics = compute_strategy_metrics(rows)
    metrics = compute_weights(metrics, args.min_samples)
    save_optimization_log(metrics, args.days, args.dry_run)
    print_summary(metrics)

    # 输出 JSON 摘要到 stdout 供外部消费
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lookback_days": args.days,
        "weights": {k: v["weight"] for k, v in metrics.items()},
    }
    print(f"\n📋 权重摘要 (JSON):")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
