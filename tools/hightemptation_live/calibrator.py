#!/usr/bin/env python3
"""
HighTempTation — 概率校准器

功能:
  1. 从 DB 读取 (model_prob, realized_prob) 历史匹配对
  2. 用 IsotonicRegression 训练校准模型
  3. 将校准器序列化为 .pkl 文件持久化
  4. calibrate_prob() 替换原始 model_prob，集成到开仓逻辑

用法:
  calibrator = ProbabilityCalibrator(db=TradeDB("hightemptation.db"))
  calibrator.train(city="Tokyo")
  calibrated = calibrator.calibrate_prob(0.35, city="Tokyo")
  # → 返回校准后概率（如 0.31），更接近真实市场实现概率
"""
import logging
import math
import os
import pickle
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from db_manager import TradeDB

logger = logging.getLogger("calibrator")

CALIBRATOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibrator_models")


class ProbabilityCalibrator:
    """
    概率校准器。

    对每个城市独立训练 IsotonicRegression（保序回归）。
    校准模型保存为 {city}.pkl 文件。
    训练数据从 DB calibration 表读取 (model_prob, realized_prob)。

    回退策略:
      - 如果该城市样本不足 (< MIN_SAMPLES)，使用全局模型
      - 如果全局样本也不足，返回原始概率
    """

    MIN_SAMPLES = 10

    def __init__(self, db: TradeDB, model_dir: str = CALIBRATOR_DIR):
        self.db = db
        self.model_dir = model_dir
        os.makedirs(self.model_dir, exist_ok=True)
        self._models: Dict[str, object] = {}  # city → isotonic model
        self._global_model: Optional[object] = None

    # ════════════════════════════════════════════════════════════════
    # 数据准备
    # ════════════════════════════════════════════════════════════════

    def _fetch_training_data(self, city: Optional[str] = None) -> Tuple[List[float], List[float]]:
        """
        从 DB 获取训练数据。

        匹配逻辑: 对每个城市-温度桶组合:
          - model_prob = p_model (高斯 CDF)
          - realized_prob = 该桶 NO 最终结算为 1 的比例
            (简化: 用 historical market prices 近似)

        从 calibration 表读取已记录的 (model_prob, realized_prob) 对。
        """
        records = self.db.get_calibration_data(city=city, min_samples=0)
        x_vals = [r["model_prob"] for r in records if r.get("realized_prob") is not None]
        y_vals = [r["realized_prob"] for r in records if r.get("realized_prob") is not None]
        return x_vals, y_vals

    # ════════════════════════════════════════════════════════════════
    # 训练
    # ════════════════════════════════════════════════════════════════

    def train(self, city: Optional[str] = None) -> bool:
        """
        训练校准模型。

        :param city: None=训练全局模型，指定城市=训练该城市专属模型
        :returns: 是否成功
        """
        try:
            from sklearn.isotonic import IsotonicRegression
        except ImportError:
            logger.warning("sklearn 未安装, 使用线性校准回退")
            return False

        x, y = self._fetch_training_data(city)

        if len(x) < self.MIN_SAMPLES:
            logger.warning(f"样本不足: {len(x)} < {self.MIN_SAMPLES}, 跳过训练{'全局' if city is None else city}")
            return False

        # 训练 IsotonicRegression
        model = IsotonicRegression(increasing=True, out_of_bounds="clip")
        model.fit(x, y)

        # 保存到内存
        if city is None:
            self._global_model = model
            pkl_path = os.path.join(self.model_dir, "_global.pkl")
        else:
            self._models[city] = model
            pkl_path = os.path.join(self.model_dir, f"{city}.pkl")

        with open(pkl_path, "wb") as f:
            pickle.dump(model, f)

        logger.info(f"✅ 校准模型训练完成: {'全局' if city is None else city} ({len(x)} 样本)")
        return True

    def train_all(self) -> Dict[str, bool]:
        """训练全局 + 所有有数据城市的模型"""
        results = {"_global": self.train(city=None)}
        # 获取所有有校准数据的城市
        rows = self.db.conn.execute(
            "SELECT DISTINCT city FROM calibration WHERE realized_prob IS NOT NULL"
        ).fetchall()
        for row in rows:
            c = row["city"]
            results[c] = self.train(city=c)
        return results

    # ════════════════════════════════════════════════════════════════
    # 加载
    # ════════════════════════════════════════════════════════════════

    def load(self, city: Optional[str] = None) -> bool:
        """从磁盘加载校准模型"""
        if city is None:
            pkl_path = os.path.join(self.model_dir, "_global.pkl")
        else:
            pkl_path = os.path.join(self.model_dir, f"{city}.pkl")

        if not os.path.exists(pkl_path):
            return False

        try:
            with open(pkl_path, "rb") as f:
                model = pickle.load(f)
            if city is None:
                self._global_model = model
            else:
                self._models[city] = model
            return True
        except Exception as e:
            logger.warning(f"加载校准模型失败 {pkl_path}: {e}")
            return False

    def load_all(self):
        """加载所有已保存的校准模型"""
        self.load(city=None)
        for fname in os.listdir(self.model_dir):
            if fname.endswith(".pkl") and fname != "_global.pkl":
                city = fname[:-4]
                self.load(city=city)

    # ════════════════════════════════════════════════════════════════
    # 校准
    # ════════════════════════════════════════════════════════════════

    def calibrate_prob(self, model_prob: float, city: str = "") -> float:
        """
        校准模型概率。

        优先级: 城市专属模型 → 全局模型 → 原始概率

        :param model_prob: 原始模型概率 (0~1)
        :param city: 城市名 (用于查找城市专属模型)
        :returns: 校准后概率 (0~1)
        """
        # 城市专属
        if city in self._models:
            try:
                return float(self._models[city].predict([model_prob])[0])
            except Exception:
                pass

        # 全局
        if self._global_model is not None:
            try:
                return float(self._global_model.predict([model_prob])[0])
            except Exception:
                pass

        # 尝试加载
        if city and city not in self._models:
            self.load(city=city)
            if city in self._models:
                return self.calibrate_prob(model_prob, city)

        if self._global_model is None:
            self.load()
        if self._global_model is not None:
            try:
                return float(self._global_model.predict([model_prob])[0])
            except Exception:
                pass

        return model_prob

    # ════════════════════════════════════════════════════════════════
    # 更新 DB 校准记录
    # ════════════════════════════════════════════════════════════════

    def update_calibration_from_trades(self):
        """
        从已结算的交易更新校准表。

        对每个城市-温度桶组合:
          - model_prob = entry 时的 p_model
          - realized_prob = 该桶所有交易中盈利比例
        """
        rows = self.db.conn.execute("""
            SELECT t.city, t.bucket_lower, t.bucket_upper,
                   COUNT(*) as cnt,
                   SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END) as wins,
                   AVG(CASE WHEN t.side='NO' THEN t.entry_price ELSE 1-t.entry_price END) as avg_model_prob
            FROM trades t
            WHERE t.status='closed' AND t.pnl IS NOT NULL
            GROUP BY t.city, t.bucket_lower, t.bucket_upper
        """).fetchall()

        for row in rows:
            realized = row["wins"] / row["cnt"] if row["cnt"] > 0 else 0.5
            self.db.store_calibration_record(
                city=row["city"],
                bucket_lower=row["bucket_lower"],
                bucket_upper=row["bucket_upper"],
                model_prob=round(row["avg_model_prob"], 4) if row["avg_model_prob"] else 0.5,
                realized_prob=round(realized, 4),
                sample_count=row["cnt"],
            )

        logger.info(f"校准表更新: {len(rows)} 个城市-桶组合")

    def get_calibration_reliability(self) -> List[dict]:
        """
        计算校准可靠性曲线。

        :returns: [{"bin": "0.0-0.1", "avg_model": 0.05, "avg_realized": 0.07, "count": 100}, ...]
        """
        records = self.db.get_calibration_data(min_samples=1)
        if not records:
            return []

        bins = {}
        for r in records:
            mp = r["model_prob"]
            rp = r.get("realized_prob", 0.5)
            if rp is None:
                continue
            bin_key = f"{math.floor(mp * 10) / 10:.1f}-{math.ceil(mp * 10) / 10:.1f}"
            if bin_key not in bins:
                bins[bin_key] = {"model_sum": 0.0, "realized_sum": 0.0, "count": 0}
            bins[bin_key]["model_sum"] += mp
            bins[bin_key]["realized_sum"] += rp
            bins[bin_key]["count"] += 1

        result = []
        for bin_label in sorted(bins.keys()):
            b = bins[bin_label]
            result.append({
                "bin": bin_label,
                "avg_model": round(b["model_sum"] / b["count"], 4),
                "avg_realized": round(b["realized_sum"] / b["count"], 4),
                "count": b["count"],
            })
        return result
