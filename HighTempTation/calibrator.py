#!/usr/bin/env python3
"""
HighTempTation — 生产级概率校准器 (v2)

核心思想 (Day1 关键项 #1):
  高斯 CDF 算出的 p_model 是"模型认为的概率"，不是"实际发生的频率"。
  概率校准用保序回归 (Isotonic Regression) 把 原始概率 → 实际频率，
  消除系统性高估/低估，这是最容易把策略从亏转赚的一步。

与旧版 (dashboard/calibrator.py) 的差异:
  1. 训练数据源升级: 优先从 signal_history 表取信号级 (p_model, actual_result)
     配对 (最实时、无桶级聚合偏差); 旧版只读 calibration 聚合表
  2. 样本加权: IsotonicRegression 支持 sample_weight (样本量即置信度)
  3. 评估指标: ECE (期望校准误差) / MCE / Brier score + K-fold 交叉验证 ECE
     (交叉验证 ECE 能暴露过拟合: 训练 ECE 低但 CV-ECE 高 = 过拟合)
  4. Reliability Diagram: matplotlib 绘制 PNG + binned 数据 (供 FastAPI /docs)
  5. 训练闭环: retrain(force=True) 由 bot 主循环每日调用一次
  6. 回退链保持: 城市模型 → 全局模型 → 原始概率

用法:
  calibrator = ProbabilityCalibrator(db=TradeDB("hightemptation.db"))
  calibrator.retrain()                      # 从 signal_history 训练全局+城市模型
  p_cal = calibrator.calibrate_prob(0.35, city="Tokyo")
  metrics = calibrator.get_metrics()        # ECE/MCE/Brier/reliability bins
  calibrator.plot_reliability_diagram("reliability.png")
"""
import logging
import math
import os
import pickle
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    np = None

try:
    from sklearn.isotonic import IsotonicRegression
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False
    IsotonicRegression = None

try:
    import matplotlib
    matplotlib.use("Agg")  # 无头环境绘图
    import matplotlib.pyplot as plt
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False
    plt = None

from db_manager import TradeDB

logger = logging.getLogger("calibrator")

CALIBRATOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibrator_models")


class ProbabilityCalibrator:
    """
    概率校准器 (v2)。

    对每个城市独立训练 IsotonicRegression；样本不足时回退全局模型；
    全局样本也不足时返回原始概率。

    训练数据 (优先级):
      1. signal_history 表: (p_model, actual_result) 信号级配对
      2. calibration 表: (model_prob, realized_prob, sample_count) 桶级聚合
    """

    MIN_SAMPLES = 10          # 城市级最少样本数
    MIN_GLOBAL_SAMPLES = 30   # 全局最少样本数
    N_BINS = 10               # Reliability Diagram 分箱数

    def __init__(self, db: TradeDB, model_dir: str = CALIBRATOR_DIR):
        self.db = db
        self.model_dir = model_dir
        os.makedirs(self.model_dir, exist_ok=True)
        self._models: Dict[str, object] = {}     # city → isotonic model
        self._global_model: Optional[object] = None
        self._metrics: Dict[str, object] = {}    # 最近一次训练的评估指标
        self._last_train_at: Optional[str] = None

    # ════════════════════════════════════════════════════════════════
    # 训练数据准备 (双数据源)
    # ════════════════════════════════════════════════════════════════

    def _fetch_signal_pairs(self, city: Optional[str] = None,
                            days: int = 90) -> Tuple[List[float], List[int]]:
        """
        从 signal_history 表取信号级 (p_model, actual_result) 配对。

        p_model 是模型给出的 YES 概率；actual_result=1 表示该合约最终
        YES 实现 (盈利)。这是校准最干净的标签: 模型概率 vs 真实频率。

        只取已结算 (actual_result IS NOT NULL) 的信号。
        """
        if self.db is None:
            return [], []
        try:
            sql = """
                SELECT p_model, actual_result FROM signal_history
                WHERE p_model IS NOT NULL AND actual_result IS NOT NULL
                  AND p_model > 0.0 AND p_model < 1.0
            """
            params: list = []
            if city:
                sql += " AND city=?"
                params.append(city)
            if days and days > 0:
                sql += " AND created_at >= datetime('now', ?)"
                params.append(f"-{days} days")
            rows = self.db.conn.execute(sql, params).fetchall()
            xs = [float(r["p_model"]) for r in rows]
            ys = [int(r["actual_result"]) for r in rows]
            return xs, ys
        except Exception as e:
            logger.debug(f"signal_history 训练数据读取失败: {e}")
            return [], []

    def _fetch_bucket_pairs(self, city: Optional[str] = None) -> Tuple[List[float], List[float], List[int]]:
        """
        从 calibration 聚合表取 (model_prob, realized_prob, sample_count)。
        作为 signal_history 数据不足时的补充数据源。
        """
        if self.db is None:
            return [], [], []
        try:
            records = self.db.get_calibration_data(city=city, min_samples=0)
            xs, ys, ws = [], [], []
            for r in records:
                mp = r.get("model_prob")
                rp = r.get("realized_prob")
                if mp is None or rp is None:
                    continue
                if not (0.0 < mp < 1.0 and 0.0 <= rp <= 1.0):
                    continue
                xs.append(float(mp))
                ys.append(float(rp))
                ws.append(max(1, int(r.get("sample_count") or 1)))
            return xs, ys, ws
        except Exception as e:
            logger.debug(f"calibration 表训练数据读取失败: {e}")
            return [], [], []

    def _fetch_training_data(self, city: Optional[str] = None
                             ) -> Tuple[List[float], List[float], Optional[List[float]]]:
        """
        合并双数据源，返回 (x, y, sample_weight?)。

        策略: signal_history 信号级为主 (逐笔标签)，calibration 桶级为补。
        y 归一化到 [0,1] (信号级是 0/1，桶级是 0~1 频率)。
        """
        xs, ys = self._fetch_signal_pairs(city)
        weights = None
        if len(xs) >= self.MIN_SAMPLES:
            return xs, ys, weights

        # 信号级不足 → 补充桶级聚合数据 (以其 sample_count 为权重)
        bx, by, bw = self._fetch_bucket_pairs(city)
        xs2, ys2 = list(xs), [float(y) for y in ys]
        ws2 = [1.0] * len(xs2)
        for x, y, w in zip(bx, by, bw):
            xs2.append(x)
            ys2.append(y)
            ws2.append(float(w))
        if len(xs2) >= self.MIN_SAMPLES:
            return xs2, ys2, ws2
        return xs2, ys2, None

    # ════════════════════════════════════════════════════════════════
    # 训练
    # ════════════════════════════════════════════════════════════════

    def _fit(self, x: List[float], y: List[float],
             weights: Optional[List[float]] = None) -> Optional[object]:
        """IsotonicRegression 拟合，失败返回 None"""
        if not _HAS_SKLEARN:
            logger.warning("sklearn 未安装，跳过 IsotonicRegression 训练")
            return None
        try:
            model = IsotonicRegression(increasing=True, out_of_bounds="clip")
            if weights is not None:
                model.fit(x, y, sample_weight=weights)
            else:
                model.fit(x, y)
            return model
        except Exception as e:
            logger.warning(f"IsotonicRegression 拟合失败: {e}")
            return None

    def train(self, city: Optional[str] = None) -> bool:
        """
        训练校准模型 (城市级或全局)。

        :param city: None=训练全局模型，指定城市=训练该城市专属模型
        :returns: 是否成功
        """
        x, y, w = self._fetch_training_data(city)
        min_need = self.MIN_GLOBAL_SAMPLES if city is None else self.MIN_SAMPLES
        if len(x) < min_need:
            logger.info(f"校准样本不足: {len(x)} < {min_need} "
                        f"({'全局' if city is None else city}), 跳过")
            return False

        model = self._fit(x, y, w)
        if model is None:
            return False

        # 保存到内存 + 磁盘
        if city is None:
            self._global_model = model
            pkl_path = os.path.join(self.model_dir, "_global.pkl")
            tag = "全局"
        else:
            self._models[city] = model
            pkl_path = os.path.join(self.model_dir, f"{city}.pkl")
            tag = city
        try:
            with open(pkl_path, "wb") as f:
                pickle.dump(model, f)
        except Exception as e:
            logger.warning(f"校准模型持久化失败 {pkl_path}: {e}")

        # 训练集内评估
        self._metrics[tag if city else "_global"] = self._evaluate(x, y, model)
        self._last_train_at = datetime.now(timezone.utc).isoformat()
        logger.info(f"✅ 校准模型训练完成: {tag} ({len(x)} 样本)")
        return True

    def train_all(self) -> Dict[str, bool]:
        """训练全局 + 所有有数据城市的模型"""
        results = {"_global": self.train(city=None)}
        if self.db is None:
            return results
        try:
            cities = set()
            for r in self.db.conn.execute(
                    "SELECT DISTINCT city FROM signal_history WHERE actual_result IS NOT NULL"):
                cities.add(r["city"])
            for r in self.db.conn.execute(
                    "SELECT DISTINCT city FROM calibration WHERE realized_prob IS NOT NULL"):
                cities.add(r["city"])
            for c in sorted(cities):
                results[c] = self.train(city=c)
        except Exception as e:
            logger.debug(f"枚举城市失败: {e}")
        return results

    def retrain(self, force: bool = False) -> bool:
        """
        完整重训闭环: 先从已结算交易重建 calibration 聚合表，再训练全部模型。
        bot 主循环每日调用一次。
        """
        if self.db is not None:
            try:
                self.update_calibration_from_trades()
            except Exception as e:
                logger.warning(f"更新校准聚合表失败: {e}")
        results = self.train_all()
        return any(results.values())

    # ════════════════════════════════════════════════════════════════
    # 加载
    # ════════════════════════════════════════════════════════════════

    def load(self, city: Optional[str] = None) -> bool:
        """从磁盘加载校准模型"""
        pkl_path = os.path.join(self.model_dir,
                                "_global.pkl" if city is None else f"{city}.pkl")
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
        if not os.path.isdir(self.model_dir):
            return
        for fname in os.listdir(self.model_dir):
            if fname.endswith(".pkl") and fname != "_global.pkl":
                self.load(city=fname[:-4])

    # ════════════════════════════════════════════════════════════════
    # 校准 (核心接口)
    # ════════════════════════════════════════════════════════════════

    def calibrate_prob(self, model_prob: float, city: str = "") -> float:
        """
        校准模型概率: 原始高斯 CDF 概率 → 实际频率。

        优先级: 城市专属模型 → 全局模型 → 原始概率。
        输出 clamp 到 (0.001, 0.999) 防止退化。

        :param model_prob: 原始模型概率 (0~1)
        :param city: 城市名
        :returns: 校准后概率 (0~1)
        """
        if not _HAS_SKLEARN:
            return model_prob

        # 城市专属
        if city in self._models:
            try:
                p = float(self._models[city].predict([model_prob])[0])
                return min(0.999, max(0.001, p))
            except Exception:
                pass

        # 全局
        if self._global_model is not None:
            try:
                p = float(self._global_model.predict([model_prob])[0])
                return min(0.999, max(0.001, p))
            except Exception:
                pass

        # 懒加载后重试
        if city and city not in self._models:
            self.load(city=city)
            if city in self._models:
                return self.calibrate_prob(model_prob, city)
        if self._global_model is None:
            self.load()
            if self._global_model is not None:
                return self.calibrate_prob(model_prob, city)

        return model_prob

    def calibrate_many(self, probs: List[float], city: str = "") -> List[float]:
        """批量校准"""
        return [self.calibrate_prob(p, city) for p in probs]

    # ════════════════════════════════════════════════════════════════
    # 评估指标 (ECE / MCE / Brier + 交叉验证)
    # ════════════════════════════════════════════════════════════════

    def _evaluate(self, x: List[float], y: List[float], model: object) -> Dict:
        """对给定 (x, y) 计算校准质量指标"""
        if not _HAS_NUMPY:
            return {}
        try:
            preds = np.asarray(model.predict(x), dtype=float)
            y_arr = np.asarray(y, dtype=float)
        except Exception:
            return {}
        return self._compute_metrics(preds, y_arr)

    @staticmethod
    def _compute_metrics(preds: "np.ndarray", y: "np.ndarray",
                         n_bins: int = 10) -> Dict:
        """ECE / MCE / Brier score / 分箱可靠性曲线"""
        if not _HAS_NUMPY or len(preds) == 0:
            return {}
        preds = np.clip(preds, 0.0, 1.0)
        n = len(preds)
        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
        bin_idx = np.clip(np.digitize(preds, bin_edges[1:-1]), 0, n_bins - 1)

        ece, mce = 0.0, 0.0
        bins = []
        for b in range(n_bins):
            mask = bin_idx == b
            cnt = int(mask.sum())
            if cnt == 0:
                continue
            conf = float(preds[mask].mean())
            freq = float(y[mask].mean())
            w = cnt / n
            gap = abs(conf - freq)
            ece += w * gap
            mce = max(mce, gap)
            bins.append({
                "bin": f"{bin_edges[b]:.1f}-{bin_edges[b+1]:.1f}",
                "count": cnt,
                "avg_model": round(conf, 4),
                "avg_realized": round(freq, 4),
                "gap": round(gap, 4),
            })

        # Brier score: mean((pred - y)^2)，随机基准 0.25
        brier = float(np.mean((preds - y) ** 2))
        return {
            "ece": round(ece, 4),
            "mce": round(mce, 4),
            "brier": round(brier, 4),
            "brier_ref_random": 0.25,
            "n_samples": n,
            "bins": bins,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

    def cross_validated_ece(self, city: Optional[str] = None, folds: int = 5) -> Dict:
        """
        K-fold 交叉验证 ECE: 用留出数据评估真实泛化校准质量。

        训练 ECE 低但 CV-ECE 高 = 过拟合 (校准器对训练集过度拟合)。
        生产环境应监控 CV-ECE。
        """
        if not _HAS_NUMPY or not _HAS_SKLEARN:
            return {}
        x, y, w = self._fetch_training_data(city)
        if len(x) < max(self.MIN_SAMPLES, folds * 2):
            return {"cv_ece": None, "note": "样本不足"}
        try:
            xa, ya = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
            idx = np.random.RandomState(42).permutation(len(xa))
            fold_size = max(1, len(xa) // folds)
            eces = []
            for f in range(folds):
                test_idx = idx[f * fold_size:(f + 1) * fold_size]
                if len(test_idx) == 0:
                    continue
                train_mask = np.ones(len(xa), dtype=bool)
                train_mask[test_idx] = False
                model = self._fit(xa[train_mask].tolist(), ya[train_mask].tolist())
                if model is None:
                    continue
                preds = np.asarray(model.predict(xa[test_idx].tolist()), dtype=float)
                m = self._compute_metrics(preds, ya[test_idx])
                if m.get("ece") is not None:
                    eces.append(m["ece"])
            if not eces:
                return {"cv_ece": None, "note": "交叉验证失败"}
            return {
                "cv_ece": round(float(np.mean(eces)), 4),
                "cv_ece_std": round(float(np.std(eces)), 4),
                "folds": folds,
                "n_samples": len(xa),
            }
        except Exception as e:
            logger.debug(f"交叉验证 ECE 失败: {e}")
            return {"cv_ece": None, "note": str(e)}

    # ════════════════════════════════════════════════════════════════
    # Reliability Diagram
    # ════════════════════════════════════════════════════════════════

    def get_reliability_curve(self) -> List[dict]:
        """可靠性曲线数据 (binned: avg_model vs avg_realized)，供 API/前端"""
        # 优先用全局模型的训练数据重算 bins；否则用内存缓存的指标
        if self._global_model is not None:
            x, y, _ = self._fetch_training_data(city=None)
            if len(x) >= self.MIN_GLOBAL_SAMPLES:
                m = self._evaluate(x, y, self._global_model)
                if m.get("bins"):
                    return m["bins"]
        for v in self._metrics.values():
            if isinstance(v, dict) and v.get("bins"):
                return v["bins"]
        return []

    def plot_reliability_diagram(self, path: str) -> Optional[str]:
        """
        绘制 Reliability Diagram (PNG)。

        - 蓝点: 每 bin 的平均模型概率 vs 实际频率 (校准曲线)
        - 灰虚线: 完美校准对角线 y=x
        返回保存路径；matplotlib 缺失时返回 None。
        """
        if not _HAS_MPL:
            logger.warning("matplotlib 未安装，跳过 Reliability Diagram 绘制")
            return None
        curve = self.get_reliability_curve()
        if not curve:
            return None
        # 中文字体探测: 无 CJK 字体时回退英文标签 (避免方块字)
        _has_cjk_font = False
        try:
            from matplotlib import font_manager
            _cjk_names = [f.name for f in font_manager.fontManager.ttflist
                          if any(k in f.name for k in ("Noto Sans CJK", "WenQuanYi",
                                                       "Source Han", "PingFang",
                                                       "Microsoft YaHei", "SimHei", "AR PL"))]
            _has_cjk_font = len(_cjk_names) > 0
            if _has_cjk_font:
                plt.rcParams["font.sans-serif"] = _cjk_names + plt.rcParams.get("font.sans-serif", [])
                plt.rcParams["axes.unicode_minus"] = False
        except Exception:
            pass
        try:
            fig, ax = plt.subplots(figsize=(6, 6))
            xs = [c["avg_model"] for c in curve]
            ys = [c["avg_realized"] for c in curve]
            sizes = [max(20, c["count"] * 2) for c in curve]
            if _has_cjk_font:
                label_curve, label_perfect = "校准曲线 (气泡=样本量)", "完美校准"
                title_ece, x_lbl, y_lbl = "Reliability Diagram (全局 ECE={:.3f})", "平均预测概率 p_model", "实际频率 (realized)"
            else:
                label_curve, label_perfect = "Calibration curve (size=count)", "Perfect calibration"
                title_ece, x_lbl, y_lbl = "Reliability Diagram (global ECE={:.3f})", "Mean predicted prob p_model", "Observed frequency"
            ax.scatter(xs, ys, s=sizes, color="#1f77b4", alpha=0.75,
                       label=label_curve)
            ax.plot([0, 1], [0, 1], "--", color="gray", label=label_perfect)
            ax.fill_between([0, 1], [0, 1], [1, 0], alpha=0.05, color="red")
            ece = self._metrics.get("_global", {}).get("ece")
            if ece is not None:
                ax.set_title(title_ece.format(ece))
            else:
                ax.set_title("Reliability Diagram")
            ax.set_xlabel(x_lbl)
            ax.set_ylabel(y_lbl)
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.legend(loc="upper left")
            ax.grid(alpha=0.3)
            fig.tight_layout()
            fig.savefig(path, dpi=110)
            plt.close(fig)
            logger.info(f"📈 Reliability Diagram 已保存: {path}")
            return path
        except Exception as e:
            logger.warning(f"Reliability Diagram 绘制失败: {e}")
            return None

    # ════════════════════════════════════════════════════════════════
    # 汇总指标 (供 /api/metrics)
    # ════════════════════════════════════════════════════════════════

    def get_metrics(self, with_cv: bool = True) -> Dict:
        """校准器汇总指标，供 FastAPI /api/metrics"""
        global_m = self._metrics.get("_global", {})
        city_metrics = {c: m for c, m in self._metrics.items() if c != "_global"}
        result = {
            "enabled": True,
            "has_sklearn": _HAS_SKLEARN,
            "has_global_model": self._global_model is not None,
            "city_models": sorted(self._models.keys()),
            "n_city_models": len(self._models),
            "global_ece": global_m.get("ece"),
            "global_mce": global_m.get("mce"),
            "global_brier": global_m.get("brier"),
            "global_n_samples": global_m.get("n_samples"),
            "reliability_curve": self.get_reliability_curve(),
            "city_ece": {c: m.get("ece") for c, m in city_metrics.items()},
            "last_train_at": self._last_train_at,
        }
        if with_cv:
            result["cv"] = self.cross_validated_ece(city=None)
        return result

    # ════════════════════════════════════════════════════════════════
    # 更新 DB 校准聚合表 (训练闭环)
    # ════════════════════════════════════════════════════════════════

    def update_calibration_from_trades(self):
        """
        从已结算交易更新 calibration 聚合表。

        对每个城市-温度桶组合:
          - model_prob = 买入价反推的 YES 概率均值 (trades 表无 p_model 列,
            用 entry_price 近似: NO 仓买入价=1-p_yes, YES 仓买入价=p_yes)
          - realized_prob = YES 实现的频率 (NO 仓盈利 → YES 未实现 → 0)
        该表同时供旧版校准器/看板兼容使用。
        """
        if self.db is None:
            return
        try:
            rows = self.db.conn.execute("""
                SELECT city, bucket_lower, bucket_upper,
                       COUNT(*) as cnt,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                       AVG(CASE WHEN side='NO' THEN 1-entry_price ELSE entry_price END) as avg_model_prob
                FROM trades t
                WHERE status='closed' AND pnl IS NOT NULL
                GROUP BY city, bucket_lower, bucket_upper
            """).fetchall()
            updated = 0
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
                updated += 1
            if updated:
                logger.info(f"📊 校准聚合表更新: {updated} 个城市-桶组合")
        except Exception as e:
            logger.warning(f"校准聚合表更新失败: {e}")
