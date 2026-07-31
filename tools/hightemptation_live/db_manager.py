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
  - fsm_orders:        订单状态机 FSM 记录（第二波）
  - microstructure_snapshots: 订单簿微观结构快照（第二波）
  - arbitrage_signals: 套利信号（第二波）
  - ml_residuals:      ML 残差学习记录（第二波）
  - chaos_events:      混沌工程事件（第二波）
  - onchain_txs:       链上交易（Gas/Nonce/MEV/路由, 第四波）
  - gas_history:       Gas 价格历史（第四波）
  - audit_logs:        不可篡改审计日志（哈希链, 第四波）
  - oracle_risk:       预言机风险评估（第四波）
  - antigame_actions:  反博弈动作日志（第四波）
  - meta_decisions:    元控制器决策（第四波）
  - human_approvals:   人机协同审批记录（第四波）

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

        -- ── 高阶优化: 订单状态机 FSM 订单台账 ──
        CREATE TABLE IF NOT EXISTS fsm_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_order_id TEXT NOT NULL UNIQUE,
            token_id TEXT NOT NULL DEFAULT '',
            symbol TEXT DEFAULT '',
            side TEXT NOT NULL CHECK(side IN ('buy','sell')),
            qty REAL NOT NULL,
            filled_qty REAL DEFAULT 0,
            avg_fill_price REAL,
            limit_price REAL,
            state TEXT NOT NULL DEFAULT 'NEW',
            source TEXT DEFAULT 'fsm',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_fsm_state ON fsm_orders(state);

        -- ── 高阶优化: 微观结构快照 ──
        CREATE TABLE IF NOT EXISTS microstructure_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            city TEXT,
            token_id TEXT DEFAULT '',
            mid_price REAL,
            depth_bid REAL,
            depth_ask REAL,
            lob_shape TEXT,
            depth_slope REAL,
            impact_est REAL,
            vpin REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ms_ts ON microstructure_snapshots(ts);

        -- ── 高阶优化: 套利信号 ──
        CREATE TABLE IF NOT EXISTS arbitrage_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            arb_type TEXT NOT NULL,
            description TEXT,
            expected_pnl REAL,
            status TEXT DEFAULT 'open',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_arb_ts ON arbitrage_signals(ts);

        -- ── 高阶优化: ML 残差学习 ──
        CREATE TABLE IF NOT EXISTS ml_residuals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            city TEXT,
            date TEXT,
            model_prob REAL,
            residual REAL,
            prediction REAL,
            features_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ml_city ON ml_residuals(city, date);

        -- ── 高阶优化: 混沌工程事件 ──
        CREATE TABLE IF NOT EXISTS chaos_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            scenario TEXT,
            injected_fault TEXT,
            circuit_state TEXT,
            outcome TEXT,
            detail TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_chaos_ts ON chaos_events(ts);

        -- ── 第四波: 链上执行层 ──
        CREATE TABLE IF NOT EXISTS onchain_txs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            account TEXT,
            nonce INTEGER,
            gas_base REAL,
            gas_priority REAL,
            gas_max_fee REAL,
            mev_score REAL,
            route TEXT DEFAULT 'public',
            multicall_batch INTEGER DEFAULT 1,
            status TEXT DEFAULT 'pending',
            tx_hash TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_onchain_ts ON onchain_txs(ts);

        -- ── 第四波: Gas 历史 ──
        CREATE TABLE IF NOT EXISTS gas_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            base_fee REAL NOT NULL,
            priority_fee REAL DEFAULT 0,
            congestion REAL DEFAULT 0,
            network TEXT DEFAULT 'polygon',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_gas_ts ON gas_history(ts);

        -- ── 第四波: 不可篡改审计日志（哈希链）──
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seq INTEGER NOT NULL UNIQUE,
            ts REAL NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL,
            anchor_ref TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_audit_seq ON audit_logs(seq);

        -- ── 第四波: 预言机风险评估 ──
        CREATE TABLE IF NOT EXISTS oracle_risk (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            market TEXT NOT NULL,
            total_score REAL,
            level TEXT,
            position_factor REAL,
            scores_json TEXT DEFAULT '{}',
            recommendations TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_oracle_ts ON oracle_risk(ts);

        -- ── 第四波: 反博弈动作 ──
        CREATE TABLE IF NOT EXISTS antigame_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            detail TEXT DEFAULT '',
            is_noise INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_antigame_ts ON antigame_actions(ts);

        -- ── 第四波: 元控制器决策 ──
        CREATE TABLE IF NOT EXISTS meta_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            context_json TEXT DEFAULT '{}',
            chosen_strategy TEXT,
            confidence REAL,
            bandit_arm TEXT,
            ppo_arm TEXT,
            bma_weights TEXT DEFAULT '[]',
            reward REAL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_meta_ts ON meta_decisions(ts);

        -- ── 第四波: 人机协同审批 ──
        CREATE TABLE IF NOT EXISTS human_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            level TEXT NOT NULL,
            action TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending',
            decided_by TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            decided_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_approval_status ON human_approvals(status);
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

    # ── 高阶优化: FSM 订单台账 ──

    def upsert_fsm_order(self, client_order_id: str, token_id: str = "",
                          side: str = "buy", qty: float = 0.0,
                          limit_price: Optional[float] = None,
                          state: str = "NEW", source: str = "fsm") -> bool:
        """写入或更新 FSM 订单台账（client_order_id 幂等）"""
        try:
            self.conn.execute("""
                INSERT INTO fsm_orders(client_order_id, token_id, side, qty,
                                       limit_price, state, source)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    state=excluded.state,
                    updated_at=datetime('now')
            """, (client_order_id, token_id, side, qty, limit_price, state, source))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入 FSM 订单失败: {e}")
            return False

    def update_fsm_order_fill(self, client_order_id: str, filled_qty: float,
                               avg_fill_price: float, state: str) -> bool:
        """更新 FSM 订单成交状态"""
        try:
            self.conn.execute("""
                UPDATE fsm_orders SET filled_qty=?, avg_fill_price=?, state=?,
                       updated_at=datetime('now')
                WHERE client_order_id=?
            """, (filled_qty, avg_fill_price, state, client_order_id))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"更新 FSM 订单失败: {e}")
            return False

    def get_fsm_order(self, client_order_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM fsm_orders WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_open_fsm_orders(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM fsm_orders WHERE state IN ('SUBMITTED','PARTIALLY_FILLED','UNKNOWN')"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 高阶优化: 微观结构快照 ──

    def store_microstructure_snapshot(self, ts: int, city: str, token_id: str,
                                       mid_price: float, depth_bid: float,
                                       depth_ask: float, lob_shape: str = "",
                                       depth_slope: Optional[float] = None,
                                       impact_est: Optional[float] = None,
                                       vpin: Optional[float] = None) -> bool:
        try:
            self.conn.execute("""
                INSERT INTO microstructure_snapshots
                (ts, city, token_id, mid_price, depth_bid, depth_ask,
                 lob_shape, depth_slope, impact_est, vpin)
                VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (ts, city, token_id, mid_price, depth_bid, depth_ask,
                   lob_shape, depth_slope, impact_est, vpin))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入微观结构快照失败: {e}")
            return False

    # ── 高阶优化: 套利信号 ──

    def store_arbitrage_signal(self, ts: int, arb_type: str, description: str,
                                expected_pnl: Optional[float] = None,
                                status: str = "open") -> bool:
        try:
            self.conn.execute("""
                INSERT INTO arbitrage_signals(ts, arb_type, description, expected_pnl, status)
                VALUES(?,?,?,?,?)
            """, (ts, arb_type, description, expected_pnl, status))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入套利信号失败: {e}")
            return False

    # ── 高阶优化: ML 残差学习 ──

    def store_ml_residual(self, ts: Optional[int], city: str, date: str,
                           model_prob: Optional[float], residual: float,
                           prediction: Optional[float], features: dict) -> bool:
        try:
            self.conn.execute("""
                INSERT INTO ml_residuals(ts, city, date, model_prob, residual, prediction, features_json)
                VALUES(?,?,?,?,?,?,?)
            """, (ts, city, date, model_prob, residual, prediction,
                   json.dumps(features, ensure_ascii=False)))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入 ML 残差失败: {e}")
            return False

    # ── 高阶优化: 混沌工程事件 ──

    def store_chaos_event(self, ts: int, scenario: str, injected_fault: str,
                           circuit_state: str, outcome: str, detail: str = "") -> bool:
        try:
            self.conn.execute("""
                INSERT INTO chaos_events(ts, scenario, injected_fault, circuit_state, outcome, detail)
                VALUES(?,?,?,?,?,?)
            """, (ts, scenario, injected_fault, circuit_state, outcome, detail))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入混沌事件失败: {e}")
            return False

    def get_recent_chaos_events(self, limit: int = 50) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM chaos_events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 第四波: 链上执行层 ──

    def store_onchain_tx(self, ts: int, account: str = "", nonce: int = -1,
                         gas_base: float = 0, gas_priority: float = 0,
                         gas_max_fee: float = 0, mev_score: float = 0,
                         route: str = "public", multicall_batch: int = 1,
                         status: str = "pending", tx_hash: str = "") -> bool:
        try:
            self.conn.execute("""
                INSERT INTO onchain_txs(ts, account, nonce, gas_base, gas_priority,
                                        gas_max_fee, mev_score, route, multicall_batch,
                                        status, tx_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (ts, account, nonce, gas_base, gas_priority, gas_max_fee,
                   mev_score, route, multicall_batch, status, tx_hash))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入链上交易失败: {e}")
            return False

    def store_gas(self, ts: int, base_fee: float, priority_fee: float = 0,
                  congestion: float = 0, network: str = "polygon") -> bool:
        try:
            self.conn.execute("""
                INSERT INTO gas_history(ts, base_fee, priority_fee, congestion, network)
                VALUES(?,?,?,?,?)
            """, (ts, base_fee, priority_fee, congestion, network))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入 Gas 历史失败: {e}")
            return False

    # ── 第四波: 密钥安全（审计哈希链）──

    def append_audit(self, seq: int, ts: float, actor: str, action: str,
                     payload_hash: str, prev_hash: str, hash_: str,
                     anchor_ref: str = "") -> bool:
        try:
            self.conn.execute("""
                INSERT INTO audit_logs(seq, ts, actor, action, payload_hash,
                                       prev_hash, hash, anchor_ref)
                VALUES(?,?,?,?,?,?,?,?)
            """, (seq, ts, actor, action, payload_hash, prev_hash, hash_, anchor_ref))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入审计日志失败: {e}")
            return False

    def verify_audit_chain(self) -> Tuple[bool, Optional[int]]:
        """重算哈希链, 校验审计日志未被篡改"""
        import hashlib as _hl
        rows = self.conn.execute("SELECT * FROM audit_logs ORDER BY seq").fetchall()
        prev = "GENESIS"
        for r in rows:
            recalc = _hl.sha256((f"{r['prev_hash']}|{r['seq']}|{r['actor']}|"
                                 f"{r['action']}|{r['payload_hash']}").encode()).hexdigest()
            if r["prev_hash"] != prev or recalc != r["hash"]:
                return False, r["seq"]
            prev = r["hash"]
        return True, None

    # ── 第四波: 预言机风险 ──

    def store_oracle_risk(self, ts: int, market: str, total_score: float,
                          level: str, position_factor: float,
                          scores: Optional[dict] = None,
                          recommendations: str = "") -> bool:
        try:
            self.conn.execute("""
                INSERT INTO oracle_risk(ts, market, total_score, level, position_factor,
                                        scores_json, recommendations)
                VALUES(?,?,?,?,?,?,?)
            """, (ts, market, total_score, level, position_factor,
                   json.dumps(scores or {}, ensure_ascii=False), recommendations))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入预言机风险失败: {e}")
            return False

    # ── 第四波: 反博弈 ──

    def store_antigame_action(self, ts: int, action_type: str,
                              detail: str = "", is_noise: int = 0) -> bool:
        try:
            self.conn.execute("""
                INSERT INTO antigame_actions(ts, action_type, detail, is_noise)
                VALUES(?,?,?,?)
            """, (ts, action_type, detail, is_noise))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入反博弈动作失败: {e}")
            return False

    # ── 第四波: 元控制器 ──

    def store_meta_decision(self, ts: int, context: Optional[dict] = None,
                            chosen_strategy: str = "", confidence: float = 0,
                            bandit_arm: str = "", ppo_arm: str = "",
                            bma_weights: Optional[list] = None,
                            reward: float = 0) -> bool:
        try:
            self.conn.execute("""
                INSERT INTO meta_decisions(ts, context_json, chosen_strategy, confidence,
                                           bandit_arm, ppo_arm, bma_weights, reward)
                VALUES(?,?,?,?,?,?,?,?)
            """, (ts, json.dumps(context or {}, ensure_ascii=False), chosen_strategy,
                   confidence, bandit_arm, ppo_arm,
                   json.dumps(bma_weights or [], ensure_ascii=False), reward))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入元控制器决策失败: {e}")
            return False

    # ── 第四波: 人机协同 ──

    def store_approval(self, request_id: str, level: str, action: str,
                       payload: Optional[dict] = None) -> bool:
        try:
            self.conn.execute("""
                INSERT INTO human_approvals(request_id, level, action, payload)
                VALUES(?,?,?,?)
            """, (request_id, level, action, json.dumps(payload or {}, ensure_ascii=False)))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入审批请求失败: {e}")
            return False

    def update_approval(self, request_id: str, status: str, decided_by: str = "") -> bool:
        try:
            self.conn.execute("""
                UPDATE human_approvals SET status=?, decided_by=?,
                    decided_at=datetime('now')
                WHERE request_id=?
            """, (status, decided_by, request_id))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"更新审批状态失败: {e}")
            return False

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
