#!/usr/bin/env python3
"""
HighTempTation — 预言机风险（第四波优化 #3）

功能:
  1. UMASettlementModel    — UMA 结算延迟/争议期建模（定价请求生命周期 + 风险窗口）
  2. ContractPhraseParser  — 合约措辞解析（正则提取阈值/比较符/单位/时间窗）
  3. OracleRiskMatrix      — Oracle Risk 矩阵（6 维评分 → 等级 + 仓位系数建议）
  4. SameSourceCalibration — 同源数据校准（多模型一致性 / 相对实况偏差 / 漂移检测）

用法:
  from highopt_ultra.oracle_risk import (
      UMASettlementModel, ContractPhraseParser, OracleRiskMatrix, SameSourceCalibration,
  )

  uma = UMASettlementModel()
  risk = uma.risk_window(proposed_at=now, dispute_window_h=2)
  spec = ContractPhraseParser().parse(
      "Is the high temperature in Tokyo on 2026-08-05 at least 30 degrees Celsius?")
  matrix = OracleRiskMatrix().assess(city="Tokyo", scores={...})
  cal = SameSourceCalibration().calibrate(forecasts=[...], actuals=[...])
"""
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("highopt_ultra.oracle_risk")

# ════════════════════════════════════════════════════════════════
# 1. UMA 结算延迟 / 争议期
# ════════════════════════════════════════════════════════════════

class UMASettlementModel:
    """
    UMA 定价请求生命周期与结算延迟建模。

    UMA 流程: 请求定价 → 提案(propose) → 争议窗口(dispute) → 结算(settle)。
    结算可能因争议被推迟 2 个周期。对预测市场（如温度事件）这意味着:
      - 资金占用时间超出预期（机会成本）
      - 争议期间价格剧烈波动（流动性风险）

    用法:
      uma = UMASettlementModel()
      now = time.time()
      r = uma.schedule(now)                       # 全周期时间线
      r2 = uma.risk_window(proposed_at=now, settle_h=4, dispute_rate=0.15)
    """

    def __init__(self, request_window_h: float = 2.0,
                 propose_window_h: float = 2.0,
                 dispute_window_h: float = 2.0,
                 settle_window_h: float = 1.0):
        self.request_window_h = request_window_h
        self.propose_window_h = propose_window_h
        self.dispute_window_h = dispute_window_h
        self.settle_window_h = settle_window_h

    def schedule(self, start_ts: float) -> dict:
        """理想（无争议）时间线"""
        t = start_ts
        out = {"request_at": t}
        t += self.request_window_h * 3600
        out["proposed_at"] = t
        t += self.dispute_window_h * 3600
        out["settle_at"] = t
        out["ideal_total_h"] = round((t - start_ts) / 3600, 2)
        return out

    def risk_window(self, proposed_at: float, settle_h: float = 4.0,
                    dispute_rate: float = 0.0) -> dict:
        """
        结算风险窗口评估。

        :param proposed_at:    提案时间戳
        :param settle_h:       期望的结算倒计时（小时）
        :param dispute_rate:   该市场历史争议率 0~1
        :return: 剩余时间是否充足 + 争议导致的预期额外延迟
        """
        now = time.time()
        remaining_h = (proposed_at - now) / 3600.0
        # 争议会重启提案+争议周期, 吃掉部分可靠时间余量
        expected_extra_h = dispute_rate * (self.propose_window_h + self.dispute_window_h)
        effective_h = remaining_h - expected_extra_h
        safe = effective_h + 1e-9 >= settle_h
        return {
            "remaining_h": round(remaining_h, 2),
            "dispute_expected_extra_h": round(expected_extra_h, 2),
            "effective_h": round(effective_h, 2),
            "settle_h_required": settle_h,
            "safe": safe,
            "level": "SAFE" if safe else
                     ("CAUTION" if effective_h >= settle_h * 0.6 else "DANGER"),
        }


# ════════════════════════════════════════════════════════════════
# 2. 合约措辞解析
# ════════════════════════════════════════════════════════════════

@dataclass
class ConditionSpec:
    """解析出的市场条件规格"""
    raw: str
    op: str = ""                 # ge / le / between / exact
    lower: Optional[float] = None
    upper: Optional[float] = None
    unit: str = ""               # °C / °F
    city: str = ""
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    ambiguity: List[str] = field(default_factory=list)

    def to_prob_keys(self) -> dict:
        """给概率引擎的边界键: (lower, upper) 桶"""
        if self.op == "between" and self.lower is not None and self.upper is not None:
            return {"lower": self.lower, "upper": self.upper}
        if self.op == "ge" and self.lower is not None:
            return {"lower": self.lower, "upper": None}
        if self.op == "le" and self.upper is not None:
            return {"lower": None, "upper": self.upper}
        return {"lower": None, "upper": None}


class ContractPhraseParser:
    """
    合约条件文本解析器。

    从 Polymarket / UMA 市场标题与描述中提取结构化条件:
      - 比较符: "at least X" / "below X" / "between A and B" / ">= X"
      - 单位: °C / °F / 摄氏度 / 华氏度
      - 城市与日期窗口

    用法:
      p = ContractPhraseParser()
      spec = p.parse("Is the high temperature in Tokyo on 2026-08-05 at least 30 degrees Celsius?")
      print(spec.op, spec.lower, spec.unit)      # ge 30 °C
    """

    _RE_GE = re.compile(r"(?:at least|greater than|or above|>=|above|higher than)\s*([\d.]+)")
    _RE_LE = re.compile(r"(?:at most|less than|or below|<=|below|lower than)\s*([\d.]+)")
    _RE_BETWEEN = re.compile(r"between\s+([\d.]+)\s*(?:and|-)\s*([\d.]+)")
    _RE_UNIT = re.compile(r"(°\s*[CF]|degrees?\s*(celsius|centigrade|fahrenheit)|摄氏度|华氏度)",
                         re.IGNORECASE)
    _RE_CITY = re.compile(r"(?:in|of)\s+([A-Z][a-z]+(?:\s(?!on|of|at|by|in|for|from|between)[A-Z][a-z]+)?)",
                         re.IGNORECASE)
    _RE_DATE = re.compile(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})")

    def parse(self, text: str) -> ConditionSpec:
        spec = ConditionSpec(raw=text)
        m = self._RE_BETWEEN.search(text)
        if m:
            spec.op = "between"
            spec.lower, spec.upper = float(m.group(1)), float(m.group(2))
        else:
            m = self._RE_GE.search(text)
            if m:
                spec.op, spec.lower = "ge", float(m.group(1))
            m = self._RE_LE.search(text)
            if m:
                spec.op, spec.upper = "le", float(m.group(1))
        if not spec.op:
            spec.op = "exact"
            spec.ambiguity.append("未识别比较符, 按精确值处理（高歧义）")

        um = self._RE_UNIT.search(text)
        if um:
            u = um.group(1).lower()
            spec.unit = "°F" if "fahrenheit" in u or u.strip(" °") == "f" else "°C"
        else:
            spec.ambiguity.append("未识别温度单位")

        cm = self._RE_CITY.search(text)
        if cm:
            spec.city = cm.group(1).title()
        dm = self._RE_DATE.search(text)
        if dm:
            spec.window_start = dm.group(1).replace("/", "-")
            spec.window_end = spec.window_start

        # 区间合法性
        if spec.op == "between" and spec.lower >= spec.upper:
            spec.ambiguity.append(f"区间上下界倒置 {spec.lower} >= {spec.upper}")
        return spec


# ════════════════════════════════════════════════════════════════
# 3. Oracle Risk 矩阵
# ════════════════════════════════════════════════════════════════

# 评分维度: 每维 1(优) ~ 5(劣)
RISK_DIMENSIONS = [
    "source_reliability",   # 数据源可靠性（实况观测 vs 单一模型）
    "settle_latency",       # 结算延迟（距结算越近、争议越多越差）
    "dispute_rate",         # 历史争议率
    "wording_ambiguity",    # 措辞歧义度
    "historical_bias",      # 历史偏差（模型 vs 实况）
    "same_source_drift",    # 同源数据漂移（多源分歧）
]


@dataclass
class OracleRiskVerdict:
    market: str
    scores: Dict[str, float]
    total: float
    level: str              # LOW / MEDIUM / HIGH / CRITICAL
    position_factor: float  # 仓位系数（1.0 = 正常, 0 = 禁开）
    recommendations: List[str]


class OracleRiskMatrix:
    """
    Oracle Risk 矩阵评估。

    6 维 1~5 分求和 → 总分 → 等级 + 仓位系数建议:
      ≤ 12 LOW      仓位 × 1.0
      ≤ 18 MEDIUM   仓位 × 0.6, 加深度过滤
      ≤ 24 HIGH     仓位 × 0.3, 强制限价单 + 双源确认
      >  24 CRITICAL 禁止开仓

    用法:
      matrix = OracleRiskMatrix()
      v = matrix.assess("Tokyo-NO", scores={"source_reliability": 2, ...})
    """

    def __init__(self):
        self.dimensions = RISK_DIMENSIONS

    def assess(self, market: str, scores: Dict[str, float],
               phrase: Optional[ConditionSpec] = None) -> OracleRiskVerdict:
        full = {d: scores.get(d, 1.0) for d in self.dimensions}
        # 措辞歧义自动并入
        if phrase and phrase.ambiguity:
            full["wording_ambiguity"] = max(full["wording_ambiguity"], 3.0)
        total = sum(full.values())
        if total <= 12:
            level, factor, recs = "LOW", 1.0, []
        elif total <= 18:
            level, factor = "MEDIUM", 0.6
            recs = ["仓位降至 60%", "增加最小深度过滤"]
        elif total <= 24:
            level, factor = "HIGH", 0.3
            recs = ["仓位降至 30%", "强制限价单", "要求双数据源一致确认"]
        else:
            level, factor = "CRITICAL", 0.0
            recs = ["禁止开仓", "人工复核市场条件与预言机来源"]

        if phrase and phrase.ambiguity:
            recs.append("合约措辞歧义: " + "; ".join(phrase.ambiguity))
        return OracleRiskVerdict(market=market, scores=full, total=round(total, 1),
                                 level=level, position_factor=factor,
                                 recommendations=recs)


# ════════════════════════════════════════════════════════════════
# 4. 同源数据校准
# ════════════════════════════════════════════════════════════════

@dataclass
class CalibrationReport:
    sources: List[str]
    mean_forecast: float
    std_forecast: float
    max_divergence: float      # 最大源间分歧(°C)
    bias: float                # 相对实况的平均偏差（模型 - 实况）
    drift: float               # 漂移量（近期偏差 EWMA 变化）
    calibrated: bool
    notes: List[str]


class SameSourceCalibration:
    """
    同源数据校准。

    对同一物理量（如次日高温）的不同数据源（Open-Meteo 5 模型、
    METAR 实况、ERA5 再分析）做一致性分析:
      - 源间标准差 / 最大分歧 → 可信度
      - 相对实况偏差 → 系统性偏置（可校正）
      - 近期漂移（EWMA 偏差变化）→ 数据源退化检测

    用法:
      cal = SameSourceCalibration()
      r = cal.calibrate(
          forecasts={"gfs": 29.8, "icon": 30.2, "gem": 30.5, "metar_ref": 30.0},
          actual=30.1, history_bias=[0.4, 0.3, 0.2, 0.1])
    """

    def __init__(self, drift_alpha: float = 0.3, max_std: float = 0.8,
                 max_divergence: float = 2.0):
        self.alpha = drift_alpha
        self.max_std = max_std
        self.max_divergence = max_divergence
        self._bias_ewma: Optional[float] = None

    def calibrate(self, forecasts: Dict[str, float], actual: Optional[float] = None,
                  history_bias: Optional[List[float]] = None,
                  live_bias: Optional[float] = None) -> CalibrationReport:
        vals = [float(v) for v in forecasts.values()]
        mean = sum(vals) / len(vals)
        std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
        max_div = max(vals) - min(vals)

        notes: List[str] = []
        bias = 0.0
        if actual is not None:
            bias = mean - actual
            notes.append(f"相对实况偏差 {bias:+.2f}°C")

        drift = 0.0
        if history_bias:
            seq = [float(b) for b in history_bias]
            recent = sum(seq[-3:]) / len(seq[-3:])
            older = sum(seq[:-3]) / len(seq[:-3]) if len(seq) > 3 else recent
            drift = recent - older
            if abs(drift) > 0.3:
                notes.append(f"检测到偏差漂移 {drift:+.2f}°C, 数据源可能退化")

        if live_bias is not None:
            self._bias_ewma = (self.alpha * live_bias +
                               (1 - self.alpha) * (self._bias_ewma or live_bias))
            drift = max(drift, abs(self._bias_ewma or 0.0) * 0.5)

        if std > self.max_std:
            notes.append(f"源间标准差 {std:.2f}°C 超限 {self.max_std}, 模型分歧大")
        if max_div > self.max_divergence:
            notes.append(f"最大分歧 {max_div:.2f}°C 超限 {self.max_divergence}")

        calibrated = (std <= self.max_std and max_div <= self.max_divergence and
                      abs(drift) <= 0.3)
        return CalibrationReport(
            sources=list(forecasts.keys()), mean_forecast=round(mean, 2),
            std_forecast=round(std, 3), max_divergence=round(max_div, 2),
            bias=round(bias, 2), drift=round(drift, 2),
            calibrated=calibrated, notes=notes,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uma = UMASettlementModel()
    s = uma.schedule(time.time())
    print("uma timeline:", {k: round(v, 1) for k, v in s.items() if isinstance(v, float)})
    print("uma risk:", uma.risk_window(proposed_at=time.time() + 6 * 3600,
                                       settle_h=4, dispute_rate=0.2))
    p = ContractPhraseParser()
    sp = p.parse("Is the high temperature in Tokyo on 2026-08-05 at least 30 degrees Celsius?")
    print("phrase:", sp.op, sp.lower, sp.unit, sp.city, sp.window_start, sp.ambiguity)
    m = OracleRiskMatrix()
    v = m.assess("Tokyo-NO", {"source_reliability": 2, "settle_latency": 3,
                              "dispute_rate": 1, "wording_ambiguity": 1,
                              "historical_bias": 2, "same_source_drift": 2})
    print("risk:", v.level, v.total, v.position_factor)
    c = SameSourceCalibration()
    r = c.calibrate({"gfs": 29.8, "icon": 30.2, "gem": 30.5}, actual=30.1,
                    history_bias=[0.4, 0.3, 0.2, 0.1])
    print("cal:", r.calibrated, r.std_forecast, r.drift, r.notes)
