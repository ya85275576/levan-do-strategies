#!/usr/bin/env python3
"""
HighTempTation — SQLite 数据库管理器

表:
  - forecasts:         预报记录（模型+城市+日期+mu+sigma）
  - actuals:           实况温度（结算后写入）
  - market_prices:     市场快照（定时采集 YES/NO 价格、深度）
  - trades:            交易记录（开平仓、盈亏、归因）
  - calibration:       校准记录（model_prob vs realized_prob）
  - health_checks:     健康检查记录（新增）
  - deviation_logs:    偏差监控记录（新增）
  - ab_tests:          A/B 测试配置（新增）
  - ab_trade_mapping:  A/B 测试交易关联（新增）

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
            self._conn = sqlite3.connect(self.db_path)
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
        CREATE INDEX IF NOT EXISTS idx_cal_city ON calibration(city);

        -- ── 新增: 健康检查记录 ──
        CREATE TABLE IF NOT EXISTS health_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pass','warn','fail')),
            message TEXT,
            detail TEXT DEFAULT '',
            checked_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_hc_name ON health_checks(check_name, checked_at);

        -- ── 新增: 偏差监控记录 ──
        CREATE TABLE IF NOT EXISTS deviation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric TEXT NOT NULL,
            live_value REAL,
            backtest_expected REAL,
            deviation_ratio REAL,
            threshold REAL,
            status TEXT NOT NULL DEFAULT 'alert',
            message TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_dev_metric ON deviation_logs(metric, created_at);

        -- ── 新增: A/B 测试配置 ──
        CREATE TABLE IF NOT EXISTS ab_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_name TEXT NOT NULL,
            variant_name TEXT NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            is_active INTEGER NOT NULL DEFAULT 1,
            start_time TEXT NOT NULL DEFAULT (datetime('now')),
            stop_time TEXT,
            total_pnl REAL DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            win_count INTEGER DEFAULT 0,
            loss_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(test_name, variant_name)
        );

        -- ── 新增: A/B 测试交易关联 ──
        CREATE TABLE IF NOT EXISTS ab_trade_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_name TEXT NOT NULL,
            variant_name TEXT NOT NULL,
            trade_id INTEGER NOT NULL,
            pnl REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ab_trade ON ab_trade_mapping(test_name, variant_name);
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
            entry, size, side = row["entry_price"], row["size"], row["side"]
            if side == "NO":
                pnl = (entry - exit_price) * size
            else:
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

    # ── 健康检查 ──

    def store_health_check(self, check_name: str, status: str,
                            message: str = "", detail: str = "") -> bool:
        try:
            self.conn.execute(
                "INSERT INTO health_checks(check_name, status, message, detail) VALUES(?,?,?,?)",
                (check_name, status, message, detail),
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入健康检查失败: {e}")
            return False

    def get_latest_health_checks(self, check_name: Optional[str] = None,
                                   limit: int = 20) -> List[dict]:
        if check_name:
            rows = self.conn.execute(
                "SELECT * FROM health_checks WHERE check_name=? ORDER BY checked_at DESC LIMIT ?",
                (check_name, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM health_checks ORDER BY checked_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_health_check_summary(self) -> List[dict]:
        """获取每个检查项的最新状态"""
        rows = self.conn.execute("""
            SELECT hc.check_name, hc.status, hc.message, hc.checked_at
            FROM health_checks hc
            INNER JOIN (
                SELECT check_name, MAX(checked_at) as max_ts
                FROM health_checks GROUP BY check_name
            ) latest ON hc.check_name=latest.check_name AND hc.checked_at=latest.max_ts
            ORDER BY hc.check_name
        """).fetchall()
        return [dict(r) for r in rows]

    # ── 偏差监控 ──

    def store_deviation_log(self, metric: str, live_value: float,
                              backtest_expected: float, deviation_ratio: float,
                              threshold: float, status: str = "alert",
                              message: str = "") -> bool:
        try:
            self.conn.execute(
                "INSERT INTO deviation_logs(metric, live_value, backtest_expected, "
                "deviation_ratio, threshold, status, message) "
                "VALUES(?,?,?,?,?,?,?)",
                (metric, live_value, backtest_expected, deviation_ratio,
                 threshold, status, message),
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入偏差日志失败: {e}")
            return False

    def get_recent_deviations(self, limit: int = 50) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM deviation_logs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── A/B 测试 ──

    def register_ab_test(self, test_name: str, variant_name: str,
                          params: dict) -> bool:
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO ab_tests(test_name, variant_name, params_json, "
                "is_active, start_time) VALUES(?,?,?,1,datetime('now'))",
                (test_name, variant_name, json.dumps(params)),
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"注册 AB test 失败: {e}")
            return False

    def deactivate_ab_test(self, test_name: str, variant_name: str) -> bool:
        try:
            self.conn.execute(
                "UPDATE ab_tests SET is_active=0, stop_time=datetime('now') "
                "WHERE test_name=? AND variant_name=?",
                (test_name, variant_name),
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"停用 AB test 失败: {e}")
            return False

    def get_active_ab_tests(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM ab_tests WHERE is_active=1 ORDER BY test_name, variant_name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_ab_test_results(self, test_name: Optional[str] = None) -> List[dict]:
        if test_name:
            rows = self.conn.execute(
                "SELECT * FROM ab_tests WHERE test_name=? ORDER BY total_pnl DESC",
                (test_name,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM ab_tests ORDER BY test_name, total_pnl DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def record_ab_trade(self, test_name: str, variant_name: str,
                         trade_id: int, pnl: Optional[float] = None) -> bool:
        try:
            self.conn.execute(
                "INSERT INTO ab_trade_mapping(test_name, variant_name, trade_id, pnl) "
                "VALUES(?,?,?,?)",
                (test_name, variant_name, trade_id, pnl),
            )
            # 同时更新 ab_tests 统计
            stats = self.conn.execute("""
                SELECT COUNT(*) as cnt,
                       COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) as wins,
                       COALESCE(SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END),0) as losses,
                       COALESCE(SUM(pnl),0) as total_pnl
                FROM ab_trade_mapping
                WHERE test_name=? AND variant_name=?
            """, (test_name, variant_name)).fetchone()

            self.conn.execute(
                "UPDATE ab_tests SET total_pnl=?, total_trades=?, win_count=?, loss_count=? "
                "WHERE test_name=? AND variant_name=?",
                (round(stats["total_pnl"], 2), stats["cnt"], stats["wins"],
                 stats["losses"], test_name, variant_name),
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"记录 AB trade 失败: {e}")
            return False

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
