#!/usr/bin/env python3
"""
HighTempTation — SQLite 数据库管理器

四表:
  - forecasts:     预报记录（模型+城市+日期+mu+sigma）
  - actuals:       实况温度（结算后写入）
  - market_prices: 市场快照（定时采集 YES/NO 价格、深度）
  - trades:        交易记录（开平仓、盈亏、归因）

查询:
  - get_bias(city, days=30):         滚动偏差（预报 - 实况）
  - get_skew(city, days=30):         偏度（误差分布对称性）
  - get_recent_trades(limit=20):     最近交易
  - get_daily_pnl(date):             日盈亏汇总

用法:
  db = TradeDB("/path/to/hightemptation.db")
  db.store_forecast("2025-04-15", "Tokyo", 26.5, 5.0, "gfs_seamless")
  db.store_actual("2025-04-15", "Tokyo", 27.2)
  bias = db.get_bias("Tokyo", days=30)
"""
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("db_manager")


class TradeDB:
    """SQLite 数据库管理器"""

    def __init__(self, db_path: str = "hightemptation.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _init_db(self):
        """建表"""
        c = self.conn
        c.executescript("""
        CREATE TABLE IF NOT EXISTS forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            city TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT 'ensemble',
            mu REAL NOT NULL,
            sigma REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(date, city, model)
        );
        CREATE TABLE IF NOT EXISTS actuals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            city TEXT NOT NULL,
            actual_high REAL,
            source TEXT DEFAULT 'metar',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(date, city)
        );
        CREATE TABLE IF NOT EXISTS market_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            city TEXT NOT NULL,
            bucket_lower REAL,
            bucket_upper REAL,
            yes_price REAL,
            no_price REAL,
            depth REAL DEFAULT 0,
            volume_24h REAL DEFAULT 0,
            token_id TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id TEXT NOT NULL,
            city TEXT,
            bucket_lower REAL,
            bucket_upper REAL,
            side TEXT NOT NULL CHECK(side IN ('YES','NO')),
            entry_price REAL NOT NULL,
            exit_price REAL,
            size REAL NOT NULL,
            pnl REAL,
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            exit_reason TEXT DEFAULT '',
            status TEXT DEFAULT 'open' CHECK(status IN ('open','closed','cancelled')),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_market_ts ON market_prices(ts);
        CREATE INDEX IF NOT EXISTS idx_market_city ON market_prices(city);
        CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
        CREATE INDEX IF NOT EXISTS idx_trades_city ON trades(city);
        CREATE TABLE IF NOT EXISTS calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            bucket_lower REAL,
            bucket_upper REAL,
            model_prob REAL NOT NULL,
            realized_prob REAL,
            sample_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(city, bucket_lower, bucket_upper)
        );
        CREATE INDEX IF NOT EXISTS idx_forecasts_city ON forecasts(city, date);
        CREATE INDEX IF NOT EXISTS idx_actuals_city ON actuals(city, date);
        CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            bucket_label TEXT,
            signal_type TEXT NOT NULL DEFAULT 'MODEL',
            p_model REAL,
            p_market REAL,
            entry_price REAL,
            exit_price REAL,
            expected_result INTEGER,
            actual_result INTEGER,
            pnl REAL,
            side TEXT DEFAULT 'NO',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_signal_city ON signal_history(city);
        CREATE INDEX IF NOT EXISTS idx_signal_type ON signal_history(signal_type);
        CREATE INDEX IF NOT EXISTS idx_signal_created ON signal_history(created_at);

        -- v8: 贝叶斯实时概率更新决策表
        CREATE TABLE IF NOT EXISTS bayesian_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            date TEXT NOT NULL,
            bucket_label TEXT,
            prior_prob REAL NOT NULL,
            posterior_prob REAL NOT NULL,
            market_price REAL NOT NULL,
            deviation REAL NOT NULL,
            micro_factors TEXT DEFAULT '{}',
            signal_type TEXT DEFAULT 'BAYESIAN',
            executed INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_bayesian_city ON bayesian_decisions(city);
        CREATE INDEX IF NOT EXISTS idx_bayesian_created ON bayesian_decisions(created_at);
        CREATE INDEX IF NOT EXISTS idx_bayesian_executed ON bayesian_decisions(executed);
        """)
        self.conn.commit()

    # ── 写入 ──

    def store_forecast(self, date: str, city: str, mu: float, sigma: float,
                       model: str = "ensemble") -> bool:
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO forecasts(date, city, model, mu, sigma) VALUES(?,?,?,?,?)",
                (date, city, model, mu, sigma),
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入预报失败: {e}")
            return False

    def store_actual(self, date: str, city: str, actual_high: float,
                     source: str = "metar") -> bool:
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO actuals(date, city, actual_high, source) VALUES(?,?,?,?)",
                (date, city, actual_high, source),
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入实况失败: {e}")
            return False

    def store_market_snapshot(self, ts: int, city: str, bucket_lower: float,
                              bucket_upper: float, yes_price: float, no_price: float,
                              depth: float = 0, volume_24h: float = 0,
                              token_id: str = "") -> bool:
        try:
            self.conn.execute(
                "INSERT INTO market_prices(ts, city, bucket_lower, bucket_upper, "
                "yes_price, no_price, depth, volume_24h, token_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (ts, city, bucket_lower, bucket_upper, yes_price, no_price,
                 depth, volume_24h, token_id),
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入市场快照失败: {e}")
            return False

    def open_trade(self, token_id: str, city: str, bucket_lower: float,
                   bucket_upper: float, side: str, entry_price: float,
                   size: float) -> Optional[int]:
        try:
            cur = self.conn.execute(
                "INSERT INTO trades(token_id, city, bucket_lower, bucket_upper, "
                "side, entry_price, size, entry_time, status) "
                "VALUES(?,?,?,?,?,?,?,datetime('now'),'open')",
                (token_id, city, bucket_lower, bucket_upper, side, entry_price, size),
            )
            self.conn.commit()
            return cur.lastrowid
        except Exception as e:
            logger.error(f"开仓记录失败: {e}")
            return None

    def close_trade(self, trade_id: int, exit_price: float,
                    exit_reason: str = "") -> bool:
        try:
            row = self.conn.execute(
                "SELECT entry_price, size, side FROM trades WHERE id=?",
                (trade_id,),
            ).fetchone()
            if not row:
                logger.warning(f"交易 {trade_id} 不存在")
                return False
            entry, size = row["entry_price"], row["size"]
            pnl = (exit_price - entry) * size
            self.conn.execute(
                "UPDATE trades SET exit_price=?, pnl=?, exit_time=datetime('now'), "
                "exit_reason=?, status='closed' WHERE id=?",
                (exit_price, round(pnl, 2), exit_reason, trade_id),
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"平仓记录失败: {e}")
            return False

    # ── 查询 ──

    def get_bias(self, city: str, days: int = 30) -> float:
        """滚动偏差: 预报均值 - 实况, 正值=模型高估"""
        rows = self.conn.execute("""
            SELECT f.mu, a.actual_high
            FROM forecasts f JOIN actuals a ON f.date=a.date AND f.city=a.city
            WHERE f.city=? AND a.actual_high IS NOT NULL
            AND f.date >= date('now', '-' || ? || ' days')
        """, (city, days)).fetchall()
        if not rows:
            return 0.0
        errors = [r["mu"] - r["actual_high"] for r in rows]
        return sum(errors) / len(errors)

    def get_skew(self, city: str, days: int = 30) -> float:
        """偏度: 误差分布对称性 >0=右偏(模型低估极端高温)"""
        rows = self.conn.execute("""
            SELECT f.mu, a.actual_high
            FROM forecasts f JOIN actuals a ON f.date=a.date AND f.city=a.city
            WHERE f.city=? AND a.actual_high IS NOT NULL
            AND f.date >= date('now', '-' || ? || ' days')
        """, (city, days)).fetchall()
        if len(rows) < 3:
            return 0.0
        errors = [r["mu"] - r["actual_high"] for r in rows]
        n = len(errors)
        mean = sum(errors) / n
        m2 = sum((e - mean) ** 2 for e in errors) / n
        m3 = sum((e - mean) ** 3 for e in errors) / n
        if m2 == 0:
            return 0.0
        return m3 / (m2 ** 1.5)

    def get_recent_trades(self, limit: int = 20) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_open_trades(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY entry_time DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_daily_pnl(self, date_str: Optional[str] = None) -> dict:
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = self.conn.execute("""
            SELECT COUNT(*) as cnt, COALESCE(SUM(pnl),0) as total_pnl,
                   SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) as losses
            FROM trades
            WHERE date(exit_time)=? AND status='closed'
        """, (date_str,)).fetchone()
        return dict(rows) if rows else {"cnt": 0, "total_pnl": 0, "wins": 0, "losses": 0}

    def get_latest_forecast(self, city: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM forecasts WHERE city=? ORDER BY date DESC LIMIT 1",
            (city,),
        ).fetchone()
        return dict(row) if row else None

    # ── 校准 ──

    def store_calibration_record(self, city: str, bucket_lower: float,
                                   bucket_upper: float, model_prob: float,
                                   realized_prob: Optional[float] = None,
                                   sample_count: int = 0) -> bool:
        try:
            self.conn.execute("""
                INSERT OR REPLACE INTO calibration
                (city, bucket_lower, bucket_upper, model_prob, realized_prob, sample_count)
                VALUES(?,?,?,?,?,?)
            """, (city, bucket_lower, bucket_upper, model_prob, realized_prob, sample_count))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入校准记录失败: {e}")
            return False

    def get_calibration_data(self, city: Optional[str] = None,
                              min_samples: int = 0) -> List[dict]:
        if city:
            rows = self.conn.execute(
                "SELECT * FROM calibration WHERE city=? AND sample_count>=? ORDER BY bucket_lower",
                (city, min_samples),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM calibration WHERE sample_count>=? ORDER BY city, bucket_lower",
                (min_samples,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_market(self, city: str, bucket_lower: float,
                          bucket_upper: float) -> Optional[dict]:
        row = self.conn.execute("""
            SELECT * FROM market_prices
            WHERE city=? AND bucket_lower=? AND bucket_upper=?
            ORDER BY ts DESC LIMIT 1
        """, (city, bucket_lower, bucket_upper)).fetchone()
        return dict(row) if row else None

    # ── 信号历史 (Signal History) ──

    def get_signal_history(self, city: str, signal_type: str = "MODEL",
                            days: int = 30) -> List[dict]:
        """查询特定城市和信号类型的历史记录"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days))
        rows = self.conn.execute("""
            SELECT * FROM signal_history
            WHERE city=? AND signal_type=?
            AND created_at >= ?
            ORDER BY created_at DESC
        """, (city, signal_type, cutoff.isoformat())).fetchall()
        return [dict(r) for r in rows]

    def get_signal_win_rate(self, city: str, signal_type: str = "MODEL",
                            days: int = 30) -> dict:
        """查询信号历史胜率统计"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days))
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN actual_result=1 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN actual_result=0 THEN 1 ELSE 0 END) as losses,
                COALESCE(AVG(pnl), 0) as avg_pnl,
                COALESCE(SUM(pnl), 0) as total_pnl
            FROM signal_history
            WHERE city=? AND signal_type=?
            AND actual_result IS NOT NULL
            AND created_at >= ?
        """, (city, signal_type, cutoff.isoformat())).fetchone()
        if not row or row["total"] == 0:
            return {"total": 0, "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0.0}
        total = row["total"]
        wins = row["wins"] or 0
        return {
            "total": total,
            "win_rate": round(wins / total, 4),
            "wins": wins,
            "losses": row["losses"] or 0,
            "avg_pnl": round(row["avg_pnl"] or 0, 2),
            "total_pnl": round(row["total_pnl"] or 0, 2),
        }

    def backtest_signals(self, signal_type: Optional[str] = None) -> dict:
        """
        返回各信号类型的汇总表现。
        供 bot.py 的 Engine.backtest_signals() 调用。
        """
        if signal_type:
            rows = self.conn.execute("""
                SELECT signal_type, COUNT(*) as total,
                       SUM(CASE WHEN actual_result=1 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN actual_result=0 THEN 1 ELSE 0 END) as losses,
                       COALESCE(AVG(pnl),0) as avg_pnl,
                       COALESCE(SUM(pnl),0) as total_pnl
                FROM signal_history
                WHERE signal_type=?
                GROUP BY signal_type
            """, (signal_type,)).fetchall()
        else:
            rows = self.conn.execute("""
                SELECT signal_type, COUNT(*) as total,
                       SUM(CASE WHEN actual_result=1 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN actual_result=0 THEN 1 ELSE 0 END) as losses,
                       COALESCE(AVG(pnl),0) as avg_pnl,
                       COALESCE(SUM(pnl),0) as total_pnl
                FROM signal_history
                GROUP BY signal_type
                ORDER BY total_pnl DESC
            """).fetchall()
        result = {}
        for r in rows:
            st = r["signal_type"]
            total = r["total"]
            wins = r["wins"] or 0
            result[st] = {
                "total": total,
                "wins": wins,
                "losses": (r["losses"] or 0),
                "win_rate": round(wins / total, 4) if total > 0 else 0,
                "avg_pnl": round(r["avg_pnl"] or 0, 2),
                "total_pnl": round(r["total_pnl"] or 0, 2),
            }
        return result

    # ── 贝叶斯决策 (bayesian_decisions) ──

    def store_bayesian_decision(self, city: str, date_: str, bucket_label: str,
                                  prior_prob: float, posterior_prob: float,
                                  market_price: float, deviation: float,
                                  micro_factors: dict, signal_type: str = "BAYESIAN",
                                  executed: bool = False) -> Optional[int]:
        """
        记录贝叶斯引擎的每次决策到 bayesian_decisions 表。

        Args:
            city: 城市名
            date_: 目标日期 YYYY-MM-DD
            bucket_label: 温度桶标签 (如 "33-35°C")
            prior_prob: 先验概率 (模型概率)
            posterior_prob: 后验概率 (贝叶斯更新后)
            market_price: 当前市场价格
            deviation: posterior - market_price (正值表示市场低估)
            micro_factors: 微观因子快照 dict (辐射/云量/风速等)
            signal_type: "BAYESIAN" 或 "BAYESIAN_LADDER"
            executed: 是否实际执行了开仓

        Returns:
            记录 ID, 失败返回 None
        """
        try:
            factors_json = json.dumps(micro_factors, ensure_ascii=False)
            cur = self.conn.execute("""
                INSERT INTO bayesian_decisions
                (city, date, bucket_label, prior_prob, posterior_prob,
                 market_price, deviation, micro_factors, signal_type,
                 executed, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (
                city, date_, bucket_label,
                round(prior_prob, 6), round(posterior_prob, 6),
                round(market_price, 6), round(deviation, 6),
                factors_json, signal_type,
                1 if executed else 0,
            ))
            self.conn.commit()
            return cur.lastrowid
        except Exception as e:
            logger.error(f"写入贝叶斯决策失败: {e}")
            return None

    def get_bayesian_decisions(self, limit: int = 100,
                                city: Optional[str] = None,
                                start_date: Optional[str] = None) -> list[dict]:
        """获取贝叶斯决策记录。"""
        sql = "SELECT * FROM bayesian_decisions WHERE 1=1"
        params = []
        if city:
            sql += " AND city=?"
            params.append(city)
        if start_date:
            sql += " AND date(created_at) >= ?"
            params.append(start_date)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        try:
            rows = self.conn.execute(sql, params).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                # 反序列化 micro_factors
                try:
                    d["micro_factors"] = json.loads(d.get("micro_factors", "{}"))
                except Exception:
                    d["micro_factors"] = {}
                result.append(d)
            return result
        except Exception as e:
            logger.error(f"查询贝叶斯决策失败: {e}")
            return []

    def get_recent_bayesian_count(self, hours: int = 24) -> int:
        """统计最近 N 小时的贝叶斯决策总数。"""
        try:
            row = self.conn.execute("""
                SELECT COUNT(*) as cnt FROM bayesian_decisions
                WHERE created_at >= datetime('now', '-' || ? || ' hours')
            """, (hours,)).fetchone()
            return row["cnt"] if row else 0
        except Exception as e:
            logger.error(f"统计贝叶斯决策失败: {e}")
            return 0

    def get_bayesian_summary(self, days: int = 7) -> dict:
        """贝叶斯决策汇总统计 (归因面板用)。"""
        try:
            # 总决策数
            total = self.conn.execute("""
                SELECT COUNT(*) as cnt FROM bayesian_decisions
                WHERE created_at >= datetime('now', '-' || ? || ' days')
            """, (days,)).fetchone()["cnt"] or 0

            # 执行数
            executed = self.conn.execute("""
                SELECT COUNT(*) as cnt FROM bayesian_decisions
                WHERE executed=1
                AND created_at >= datetime('now', '-' || ? || ' days')
            """, (days,)).fetchone()["cnt"] or 0

            # 平均偏差
            avg_dev = self.conn.execute("""
                SELECT COALESCE(AVG(ABS(deviation)), 0) as avg_dev
                FROM bayesian_decisions
                WHERE created_at >= datetime('now', '-' || ? || ' days')
            """, (days,)).fetchone()["avg_dev"] or 0.0

            # 按城市分布
            city_rows = self.conn.execute("""
                SELECT city, COUNT(*) as cnt
                FROM bayesian_decisions
                WHERE created_at >= datetime('now', '-' || ? || ' days')
                GROUP BY city ORDER BY cnt DESC LIMIT 10
            """, (days,)).fetchall()

            return {
                "total_decisions": total,
                "total_executed": executed,
                "avg_abs_deviation": round(avg_dev, 6),
                "days": days,
                "by_city": {r["city"]: r["cnt"] for r in city_rows},
            }
        except Exception as e:
            logger.error(f"贝叶斯汇总失败: {e}")
            return {}

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
