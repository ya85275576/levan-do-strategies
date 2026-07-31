#!/usr/bin/env python3
"""
HighTempTation — ML 残差学习（高阶优化 #3）

功能:
  1. MLResidualLearner — LightGBM / XGBoost / sklearn GradientBoosting 修正物理模型残差
     target = actual_high - forecast_mu（或 model_prob 误差），预测后用残差修正 mu → 修正 bucket 概率
  2. 在线学习           — online_update(): 滚动窗口重训 + 增量样本加权, 适应季节漂移
  3. NLP 增强           — NLPEnhancement(): 新闻/标题关键词情绪打分, 微调预报均值（纯 Python 无重依赖）

设计要点:
  - 后端自动降级: lightgbm → xgboost → sklearn GradientBoosting → 均值回退（保证无依赖可运行）
  - 特征: 预报均值/σ、集合离散度、日期特征、城市、历史偏差/偏度
  - 训练数据最少样本保护（MIN_SAMPLES），不足时回退零修正

用法:
  from highopt.ml_residual import MLResidualLearner, NLPEnhancement
  learner = MLResidualLearner(backend="auto")       # auto 探测可用后端
  learner.fit(training_rows)                        # rows: dict 列表
  corr = learner.predict({"city":"Tokyo","mu":26.5,"sigma":2.0,...})
  corrected_mu = 26.5 + corr
  learner.online_update(new_rows)                   # 在线学习

  nlp = NLPEnhancement()
  score = nlp.score_texts(["Heatwave warning for Tokyo on Friday", ...])
  mu_adj = nlp.adjust_forecast(mu=26.5, score=score, max_shift=1.0)
"""
import json
import logging
import math
import os
import pickle
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("highopt.ml_residual")

# ── 默认参数 ──
MIN_SAMPLES = 30                # 最少训练样本
ROLLING_WINDOW = 500            # 在线学习滚动窗口
MAX_SHIFT_DEG = 1.0             # NLP 对预报的最大修正幅度 (°C)


# ════════════════════════════════════════════════════════════════
# 后端探测
# ════════════════════════════════════════════════════════════════

def _detect_backend(backend: str) -> str:
    """探测可用 ML 后端: lightgbm > xgboost > sklearn_gbr > mean"""
    if backend != "auto":
        return backend
    try:
        import lightgbm  # noqa: F401
        return "lightgbm"
    except ImportError:
        pass
    try:
        import xgboost  # noqa: F401
        return "xgboost"
    except ImportError:
        pass
    try:
        import sklearn  # noqa: F401
        return "sklearn_gbr"
    except ImportError:
        pass
    return "mean"


# ════════════════════════════════════════════════════════════════
# 特征工程
# ════════════════════════════════════════════════════════════════

class ResidualFeatureBuilder:
    """
    残差学习特征构造。

    输入 row 支持的键:
      mu, sigma, n_models, spread(=max-min 模型离散), city, date,
      bias(滚动偏差), skew(偏度), hour
    输出固定长度特征向量（保证训练/推理一致）。
    """

    FEATURE_NAMES = [
        "mu", "sigma", "spread", "n_models", "day_of_year",
        "city_id", "bias", "skew", "hour",
    ]

    def __init__(self, cities: Optional[List[str]] = None):
        self.cities = cities or []
        self._city_index = {c: i for i, c in enumerate(self.cities)}

    def add_city(self, city: str):
        if city not in self._city_index:
            self._city_index[city] = len(self.cities)
            self.cities.append(city)

    def build(self, row: dict) -> List[float]:
        self.add_city(row.get("city", ""))
        mu = float(row.get("mu", 0.0))
        sigma = float(row.get("sigma", 2.0))
        models = row.get("models")
        if isinstance(models, dict) and models:
            vals = [v for v in models.values() if v is not None]
            spread = (max(vals) - min(vals)) if len(vals) > 1 else 0.0
        else:
            spread = float(row.get("spread", 0.0))
        n_models = float(row.get("n_models", row.get("n", 1) if "n" in row else 1))
        try:
            doy = datetime.strptime(row["date"], "%Y-%m-%d").timetuple().tm_yday
        except (KeyError, ValueError, TypeError):
            doy = 0.0
        return [
            mu,
            max(sigma, 1e-6),
            spread,
            n_models,
            float(doy),
            float(self._city_index.get(row.get("city", ""), 0)),
            float(row.get("bias", 0.0)),
            float(row.get("skew", 0.0)),
            float(row.get("hour", 0.0)),
        ]

    def names(self) -> List[str]:
        return list(self.FEATURE_NAMES)


# ════════════════════════════════════════════════════════════════
# ML 残差学习器
# ════════════════════════════════════════════════════════════════

class MLResidualLearner:
    """
    残差学习器。

    训练目标: residual = actual_high - forecast_mu（°C）
    预测:     residual_hat = model.predict(features)
    修正:     corrected_mu = mu + residual_hat

    在线学习:
      online_update(new_rows) — 追加到滚动窗口并重训。
      sklearn GradientBoosting 不支持 partial_fit，故采用
      「滚动窗口 + 全量重训」策略；样本量受 ROLLING_WINDOW 上限保护。
    """

    def __init__(self, backend: str = "auto",
                 min_samples: int = MIN_SAMPLES,
                 rolling_window: int = ROLLING_WINDOW):
        self.backend = _detect_backend(backend)
        self.min_samples = min_samples
        self.rolling_window = rolling_window
        self.features = ResidualFeatureBuilder()
        self._model = None
        self._rows: List[dict] = []
        self._mean_residual = 0.0
        self._n_train = 0
        logger.info(f"ML 残差学习器后端: {self.backend}")

    # ── 数据 ──

    def add_rows(self, rows: List[dict]):
        """追加样本（含 actual 键）"""
        for r in rows:
            if r.get("actual") is not None:
                self._rows.append(r)
        if len(self._rows) > self.rolling_window:
            self._rows = self._rows[-self.rolling_window:]

    # ── 训练 ──

    def fit(self, rows: Optional[List[dict]] = None) -> bool:
        """
        训练残差模型。
        :returns: 是否成功训练（样本不足或后端 mean 时返回 True 但用均值回退）
        """
        if rows is not None:
            self.add_rows(rows)
        if len(self._rows) < self.min_samples:
            # 样本不足 → 均值回退（零修正兜底）
            residuals = [r["actual"] - r["mu"] for r in self._rows]
            self._mean_residual = (sum(residuals) / len(residuals)) if residuals else 0.0
            self._model = None
            self._n_train = len(self._rows)
            logger.info(f"样本不足({self._n_train}<{self.min_samples}) → 均值回退修正 {self._mean_residual:+.3f}")
            return True

        X = [self.features.build(r) for r in self._rows]
        y = [r["actual"] - r["mu"] for r in self._rows]
        self._n_train = len(y)

        if self.backend == "mean":
            self._mean_residual = sum(y) / len(y)
            self._model = None
            return True

        if self.backend == "lightgbm":
            import lightgbm as lgb
            self._model = lgb.LGBMRegressor(
                n_estimators=200, learning_rate=0.05, num_leaves=31,
                min_child_samples=5, random_state=42, verbose=-1)
            self._model.fit(X, y)
        elif self.backend == "xgboost":
            import xgboost as xgb
            self._model = xgb.XGBRegressor(
                n_estimators=200, learning_rate=0.05, max_depth=5,
                random_state=42, verbosity=0)
            self._model.fit(X, y)
        elif self.backend == "sklearn_gbr":
            from sklearn.ensemble import GradientBoostingRegressor
            self._model = GradientBoostingRegressor(
                n_estimators=200, learning_rate=0.05, max_depth=3,
                random_state=42)
            self._model.fit(X, y)
        else:
            logger.error(f"未知后端 {self.backend}，回退均值")
            self._mean_residual = sum(y) / len(y)
            self._model = None
            return True

        logger.info(f"训练完成: backend={self.backend} n={self._n_train} "
                    f"R2≈{self._r2(X, y):.3f}")
        return True

    def _r2(self, X, y) -> float:
        try:
            pred = self._model.predict(X)
            ss_res = sum((a - b) ** 2 for a, b in zip(y, pred))
            ss_tot = sum((a - sum(y) / len(y)) ** 2 for a in y)
            return 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        except Exception:
            return 0.0

    # ── 推理 ──

    def predict(self, row: dict) -> float:
        """预测残差修正量（°C）"""
        if self._model is None:
            return self._mean_residual
        X = [self.features.build(row)]
        try:
            return float(self._model.predict(X)[0])
        except Exception as e:
            logger.debug(f"预测失败: {e} → 均值回退")
            return self._mean_residual

    def correct_mu(self, mu: float, row: dict) -> Tuple[float, float]:
        """修正预报均值: (corrected_mu, residual_hat)"""
        res = self.predict(row)
        return mu + res, res

    def correct_prob(self, bucket_prob_fn, row: dict,
                     bucket_lower: float, bucket_upper: float) -> Tuple[float, float, float]:
        """
        修正后的桶概率。
        :param bucket_prob_fn: (mu, sigma, lower, upper) → 概率 的函数（复用 v6 bucket_prob）
        :returns: (corrected_prob, raw_prob, residual)
        """
        raw = bucket_prob_fn(row["mu"], row["sigma"], bucket_lower, bucket_upper)
        corrected_mu, res = self.correct_mu(row["mu"], row)
        corr = bucket_prob_fn(corrected_mu, row["sigma"], bucket_lower, bucket_upper)
        return corr, raw, res

    # ── 在线学习 ──

    def online_update(self, new_rows: List[dict]) -> bool:
        """
        在线学习：追加新样本 → 滚动窗口重训。
        季节漂移时新样本自动挤出旧样本（FIFO 滚动窗口）。
        """
        before = self._n_train
        self.add_rows(new_rows)
        ok = self.fit()
        logger.info(f"在线更新: 样本 {before} → {self._n_train}")
        return ok

    # ── 持久化 ──

    def save(self, path: str):
        with open(path, "wb") as f:
            pickle.dump({
                "backend": self.backend,
                "model": self._model,
                "mean_residual": self._mean_residual,
                "n_train": self._n_train,
                "rows": self._rows[-200:],
                "cities": self.features.cities,
            }, f)
        logger.info(f"模型已保存: {path}")

    def load(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        with open(path, "rb") as f:
            data = pickle.load(f)
        self.backend = data.get("backend", self.backend)
        self._model = data.get("model")
        self._mean_residual = data.get("mean_residual", 0.0)
        self._n_train = data.get("n_train", 0)
        self._rows = data.get("rows", [])
        self.features = ResidualFeatureBuilder(cities=data.get("cities", []))
        logger.info(f"模型已加载: {path} (backend={self.backend}, n={self._n_train})")
        return True

    def stats(self) -> dict:
        return {
            "backend": self.backend,
            "n_train": self._n_train,
            "mean_residual": round(self._mean_residual, 4),
            "has_model": self._model is not None,
            "rolling_window": self.rolling_window,
        }


# ════════════════════════════════════════════════════════════════
# NLP 增强
# ════════════════════════════════════════════════════════════════

class NLPEnhancement:
    """
    NLP 情绪增强（轻量、纯 Python 无重依赖）。

    对城市相关新闻/标题做关键词情绪打分:
      - 热词（heatwave/hot/record high/酷暑/高温…）→ 正分（推高温度预期）
      - 冷词（cold snap/freeze/寒潮/低温…）→ 负分
      - 否定词翻转（no heatwave / 无高温…）

    score_texts(texts) → score ∈ [-1, 1]
    adjust_forecast(mu, score, max_shift) → mu + score * max_shift
    """

    HEAT_WORDS = [
        "heatwave", "heat wave", "hot", "record high", "scorching", "swelter",
        "tropical night", "酷暑", "高温", "热浪", "猛暑", "猛暑日", "酷热",
    ]
    COLD_WORDS = [
        "cold snap", "cold wave", "freeze", "frost", "chilly", "record low",
        "寒潮", "低温", "冷空气", "冰冻", "严寒", "冷え込み",
    ]
    NEGATORS = ["no", "not", "without", "无", "没有", "不会", "免"]

    def score_texts(self, texts: List[str]) -> float:
        """对文本列表打分，返回 [-1, 1]"""
        if not texts:
            return 0.0
        scores = [self._score_one(t) for t in texts if t]
        if not scores:
            return 0.0
        return sum(scores) / len(scores)

    def _score_one(self, text: str) -> float:
        t = text.lower()
        score = 0.0
        for w in self.HEAT_WORDS:
            if w in t:
                score += 1.0
        for w in self.COLD_WORDS:
            if w in t:
                score -= 1.0
        # 否定翻转（粗略: 否定词出现在热/冷词前 12 字符内）
        for neg in self.NEGATORS:
            idx = t.find(neg)
            if idx >= 0:
                window = t[max(0, idx): idx + 12]
                if any(w in window for w in self.HEAT_WORDS):
                    score -= 1.0
                if any(w in window for w in self.COLD_WORDS):
                    score += 1.0
        if score == 0:
            return 0.0
        # 归一化到 [-1, 1]（tanh 平滑）
        return math.tanh(score / 2.0)

    def adjust_forecast(self, mu: float, score: float,
                        max_shift: float = MAX_SHIFT_DEG) -> float:
        """按情绪分微调预报均值（上限 ±max_shift °C）"""
        shift = max(-max_shift, min(max_shift, score * max_shift))
        return mu + shift
