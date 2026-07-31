#!/usr/bin/env python3
"""
HighTempTation 天氣預報校準套利 Bot (單文件合併版)

核心算法:
  1. Open-Meteo API 獲取各城市目標日最高溫預報（均值+標準差）
  2. 高斯 CDF 計算每個溫度桶的模型概率 p_model
  3. Polymarket Gamma API 全面掃描所有天氣市場（分頁+去重）
  4. 當 p_market - p_model ≥ 0.10 且 p_market 在 30-90¢ → 買 NO
  5. 倉位 $1-1（超小注測信號），NO 漲到 98-99¢ 賣出或持有到結算

增強功能:
  🪜 溫度階梯套利 — 當主桶信號觸發時自動掃描相鄰桶並批量掛單
  🌐 多模型集成預報 — ECMWF/GFS/ICON 加權 CDF 概率平均
  📊 集成概率取最小值（更保守）— 降低假信號風險

模塊:
  1. 結算站對照表 — 城市→ICAO 坐標
  2. Open-Meteo 預報 — 站點級別最高溫（多模型）
  3. 集成概率 — ensemble_prob() 加權平均
  4. 溫度階梯 — 主信號相鄰桶自動擴散
  5. 偏差校正 — BIAS_CACHE 可配置
  6. METAR 實際值 — aviationweather.gov
  7. 市場解析 — 正則提取城市/日期/溫度檔
  8. 高斯概率 — bucket_prob()
  9. 出場邏輯 — 0.98 快速兌現 / 接近結算鎖利 / 強制平倉 / 止損
  10. 持倉追蹤 — open_positions + closed_trades + realized_pnl
  11. 主循環 — 掃描→開倉→監控→出場→統計

參考開源:
  - BallesJr/polymarket-weather-edge
  - natestokens/polymarket-weather-bot

部署:
  pip install scipy python-dateutil httpx
  pm2 start ecosystem.config.cjs
"""

import asyncio
import json
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional, List

import httpx
from dateutil import parser as dateparser
# ── 导入 TradeDB ──────────────────────────────────────────────────────
_dashboard_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard')
if _dashboard_dir not in sys.path:
    sys.path.insert(0, _dashboard_dir)
try:
    from db_manager import TradeDB
    _HAS_TRADE_DB = True
except ImportError:
    TradeDB = None
    _HAS_TRADE_DB = False
    logger.warning("⚠️ 无法导入 db_manager，交易记录将不会被持久化")


# ── 高斯 CDF ──────────────────────────────────────────────────────────
try:
    from scipy.stats import norm as _scipy_norm
    def _gaussian_cdf(x: float) -> float:
        return float(_scipy_norm.cdf(x))
except ImportError:
    def _gaussian_cdf(x: float) -> float:
        """Abramowitz & Stegun 近似, 誤差 < 1.5e-7"""
        if x < -8: return 0.0
        if x > 8: return 1.0
        a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
        p = 0.3275911
        sign = 1.0 if x >= 0 else -1.0
        t = 1.0 / (1.0 + p * abs(x))
        y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2)
        return y if sign > 0 else 1.0 - y


def bucket_prob(lower: float, upper: float, mu: float, sigma: float) -> float:
    """P(lower < X < upper) = Φ((upper-μ)/σ) - Φ((lower-μ)/σ)"""
    if sigma <= 0:
        return 1.0 if lower <= mu < upper else 0.0
    return max(0.0, min(1.0, _gaussian_cdf((upper - mu) / sigma) - _gaussian_cdf((lower - mu) / sigma)))


def ensemble_prob(models: dict[str, float], weights: list[float],
                  lower: float, upper: float, bias_correction: float = 0.0) -> float:
    """
    多模型集成概率平均：对每个模型独立计算 bucket_prob，然后加权平均。

    比先平均温度再算概率更鲁棒 —— 极端模型不会污染整体概率。

    Args:
        models: {model_name: temperature_mean}
        weights: 与 models 顺序对应的权重列表
        lower/upper: 温度桶边界
        bias_correction: 偏差校正值

    Returns:
        加权平均后的 P(temp in bucket)
    """
    model_temps = list(models.values())
    if not model_temps:
        return 0.0

    # 用所有模型温度计算 sigma（模型间标准差表示预报不确定性）
    mu = sum(model_temps) / len(model_temps) - bias_correction
    if len(model_temps) > 1:
        sigma = math.sqrt(sum((t - mu - bias_correction) ** 2 for t in model_temps) / len(model_temps))
    else:
        sigma = 2.0
    if sigma < 0.5:
        sigma = 2.0  # 最小不确定性垫底

    # 原方法：用平均温度直接算概率
    direct_prob = bucket_prob(lower, upper, mu, sigma)

    # 如果用权重且模型数与权重一致，做加权平均
    if len(weights) == len(model_temps) and len(model_temps) > 1:
        total_w = sum(weights)
        if total_w > 0:
            weighted_prob = 0.0
            for i, temp in enumerate(model_temps):
                corrected_temp = temp - bias_correction
                prob_i = bucket_prob(lower, upper, corrected_temp, sigma)
                weighted_prob += weights[i] * prob_i
            weighted_prob /= total_w
            # 混合：80% 加权 + 20% 直接（防止极端权重过拟合）
            return 0.8 * weighted_prob + 0.2 * direct_prob

    return direct_prob


# ═══════════════════════════════════════════════════════════════════════
# 🌐 多模型聚合 + 一致性评分 (Multi-Model Aggregation)
# ═══════════════════════════════════════════════════════════════════════
# 对标专业天气交易Bot: 对每个模型的温度预报独立计算桶概率,
# 然后聚合出 agreement (模型一致性%) 和 spread (离散度°C),
# 生成 consistency_mult 仓位乘数, 叠加到现有阶梯和精确模式中.
# 不换数据源, 利用 Open-Meteo 已有的多模型 Ensemble 输出.
# ═══════════════════════════════════════════════════════════════════════

class MultiModelAggregator:
    """多模型聚合 + 一致性评分，为每个温度桶生成 agreement/spread 和仓位乘数。"""

    @staticmethod
    def compute_model_probabilities(models: dict[str, float], lower: float, upper: float,
                                     sigma: float) -> dict[str, float]:
        """
        对每个模型独立计算指定温度桶的高斯概率。
        models: {model_name: temperature_prediction}
        lower/upper: 温度桶下界/上界
        sigma: 预报标准差
        """
        probs: dict[str, float] = {}
        for mname, temp in models.items():
            p = bucket_prob(lower, upper, temp, sigma)
            probs[mname] = p
        return probs

    @staticmethod
    def compute_threshold_probs(models: dict[str, float], threshold: float,
                                 direction: str, sigma: float) -> dict[str, float]:
        """
        对每个模型计算阈值型市场概率 P(temp ≤ threshold) 或 P(temp ≥ threshold)。
        direction: "below" 或 "above"
        """
        probs: dict[str, float] = {}
        for mname, temp in models.items():
            z = (threshold - temp) / sigma if sigma > 0 else 0.0
            if direction == "below":
                p = _gaussian_cdf(z)
            else:
                p = 1.0 - _gaussian_cdf(z)
            probs[mname] = p
        return probs

    @staticmethod
    def compute_agreement(model_probs: dict[str, float], threshold: float = 0.30) -> float:
        """
        模型一致性百分比: 概率 >= threshold 的模型占比。
        threshold: 默认 0.30, 即模型认为该桶有 30%+ 概率即算"同意"。
        """
        if not model_probs:
            return 0.0
        agree = sum(1 for p in model_probs.values() if p >= threshold)
        return agree / len(model_probs)

    @staticmethod
    def compute_spread(models: dict[str, float]) -> float:
        """模型间离散度: 温度预测值的标准差 (°C)。"""
        temps = list(models.values())
        if len(temps) < 2:
            return 0.0
        mu = sum(temps) / len(temps)
        var = sum((t - mu) ** 2 for t in temps) / len(temps)
        return math.sqrt(var)

    @staticmethod
    def consistency_multiplier(agreement: float, spread: float,
                                spread_cap: float = 15.0) -> float:
        """
        根据模型一致性和离散度计算仓位乘数。

        核心规则:
          - agreement >= 70% → x1.0 满仓
          - agreement >= 50% → x0.7
          - agreement >= 30% → x0.4
          - agreement < 30% → x0.15 (强烈避免)
          - spread 额外惩罚: final *= max(0.5, 1 - spread/spread_cap)

        Args:
            agreement: 模型一致性百分比 (0~1)
            spread: 模型间离散度 (°C)
            spread_cap: 离散度惩罚上限, 达到此值时惩罚最大
        """
        if agreement >= 0.70:
            base = 1.0
        elif agreement >= 0.50:
            base = 0.7
        elif agreement >= 0.30:
            base = 0.4
        else:
            base = 0.15

        # spread 惩罚: 离散度越大折扣越多
        spread_factor = max(0.5, 1.0 - spread / spread_cap)

        return max(0.1, min(1.0, base * spread_factor))


# ═══════════════════════════════════════════════════════════════════════
# 1. 結算站對照表 ── 城市 → ICAO / 坐標
# ═══════════════════════════════════════════════════════════════════════
# (city, icao, station_name, lat, lon, country)
STATIONS = [
    ("Tokyo",       "RJTT",  "Tokyo Haneda",          35.5494, 139.7798, "JP"),
    ("Seoul",       "RKSS",  "Seoul Gimpo",           37.5583, 126.7906, "KR"),
    ("Singapore",   "WSSS",  "Singapore Changi",       1.3502, 103.9944, "SG"),
    ("Hong Kong",   "VHHH",  "Hong Kong Intl",        22.3080, 113.9185, "HK"),
    ("Shanghai",    "ZSSS",  "Shanghai Hongqiao",     31.1979, 121.3363, "CN"),
    ("Bangkok",     "VTBS",  "Bangkok Suvarnabhumi",  13.6811, 100.7470, "TH"),
    ("Mumbai",      "VABB",  "Mumbai CSIA",           19.0887, 72.8679, "IN"),
    ("Dubai",       "OMDB",  "Dubai Intl",            25.2528, 55.3644, "AE"),
    ("Istanbul",    "LTFM",  "Istanbul Airport",      41.2613, 28.7419, "TR"),
    ("New York",    "KNYC",  "NY Central Park",       40.7789, -73.9692, "US"),
    ("Los Angeles", "KLAX",  "LA Intl",               33.9416, -118.4085, "US"),
    ("Chicago",     "KORD",  "Chicago O'Hare",        41.9786, -87.9048, "US"),
    ("Miami",       "KMIA",  "Miami Intl",            25.7932, -80.2906, "US"),
    ("San Francisco", "KSFO", "SFO Intl",             37.6188, -122.3756, "US"),
    ("Toronto",     "CYYZ",  "Toronto Pearson",       43.6777, -79.6305, "CA"),
    ("Mexico City", "MMMX",  "Mexico City Intl",      19.4361, -99.0720, "MX"),
    ("London",      "EGLL",  "London Heathrow",       51.4700, -0.4543, "GB"),
    ("Paris",       "LFPG",  "Paris CDG",             49.0097,   2.5479, "FR"),
    ("Berlin",      "EDDT",  "Berlin Tegel",          52.5597,  13.2877, "DE"),
    ("Sydney",      "YSSY",  "Sydney Kingsford",     -33.9399, 151.1753, "AU"),
    ("Sao Paulo",   "SBGR",  "Sao Paulo GRU",        -23.4320, -46.4694, "BR"),
]

STATION_IDX = {s[0]: s for s in STATIONS}  # city → tuple


# ── 城市名标准化映射（Polymarket 上的城市名 → STATION_IDX 键名）──
# 许多天气市场使用非标准城市名，需要映射到气象站对照表中的标准名
CITY_ALIASES: dict[str, str] = {
    # 常见简写/变体 → 标准名
    "nyc": "New York",
    "new york city": "New York",
    "new york": "New York",
    "seoul (incheon)": "Seoul",
    "seoul": "Seoul",
    "sao paulo": "Sao Paulo",
    "los angeles": "Los Angeles",
    "san francisco": "San Francisco",
    "mexico city": "Mexico City",
    "hong kong": "Hong Kong",
    # 新增动态城市（不在 STATION_IDX 中，但需要保留原名用于 geocode）
    "amsterdam": None, "ankara": None, "atlanta": None, "austin": None,
    "beijing": None, "buenos aires": None, "busan": None,
    "chengdu": None, "chongqing": None,
    "dallas": None, "denver": None,
    "guangzhou": None,
    "houston": None,
    "jeddah": None,
    "kuala lumpur": None,
    "madrid": None, "manila": None, "milan": None, "munich": None,
    "qingdao": None,
    "seattle": None,
    "taipei": None, "tel aviv": None,
}


def normalize_city_name(raw: str) -> str:
    """
    将 Polymarket 市场中的城市名标准化为统一的名称。
    优先匹配 STATION_IDX，然后匹配 CITY_ALIASES，最后原样返回。
    """
    name = raw.strip()
    name_lower = name.lower()

    # 1. 直接匹配 STATION_IDX
    if name in STATION_IDX:
        return name

    # 2. 精确匹配别名
    if name_lower in CITY_ALIASES:
        mapped = CITY_ALIASES[name_lower]
        if mapped:
            return mapped
        return name  # None 表示新城市，保留原名

    # 3. 模糊匹配：遍历已知城市
    for std_city in STATION_IDX:
        if std_city.lower() in name_lower or name_lower in std_city.lower():
            return std_city

    return name


def station_coords(city: str) -> tuple[float, float]:
    s = STATION_IDX.get(city)
    return (s[3], s[4]) if s else (0.0, 0.0)


def station_icao(city: str) -> str:
    s = STATION_IDX.get(city)
    return s[1] if s else ""


# ── Open-Meteo Geocoding ──
GEO_CACHE: dict[str, tuple[float, float, str]] = {}  # city_lower → (lat, lon, display_name)
GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"


async def geocode_city(city_name: str, http: httpx.AsyncClient) -> Optional[tuple[float, float, str]]:
    """
    用 Open-Meteo Geocoding API 解析城市名 → (緯度, 經度, 顯示名稱)。
    結果緩存在 GEO_CACHE 中避免重複查詢。
    """
    key = city_name.strip().lower()
    if not key:
        return None
    if key in GEO_CACHE:
        return GEO_CACHE[key]
    try:
        r = await http.get(GEOCODING_API, params={
            "name": city_name.strip(), "count": 1, "language": "en", "format": "json",
        }, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
        if results:
            best = results[0]
            lat, lon = float(best["latitude"]), float(best["longitude"])
            display = best.get("name", city_name)
            country = best.get("country_code", "")
            GEO_CACHE[key] = (lat, lon, display)
            logger.info(f"  🗺️  Geocode: {city_name} → ({lat}, {lon}) {country}")
            return (lat, lon, display)
        else:
            logger.debug(f"Geocode 無結果: {city_name}")
            GEO_CACHE[key] = (0.0, 0.0, city_name)
            return None
    except Exception as e:
        logger.debug(f"Geocode 失敗 {city_name}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# 配置（環境變量驅動）
# ═══════════════════════════════════════════════════════════════════════

class Config:
    def __init__(self):
        self.DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

        self.SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "120"))
        self.MIN_MARKET_LIQUIDITY = float(os.getenv("MIN_MARKET_LIQUIDITY", "500"))

        self.GAMMA_API = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
        self.OPEN_METEO_GEO = os.getenv("OPEN_METEO_GEO", "https://geocoding-api.open-meteo.com/v1/search")
        self.OPEN_METEO = os.getenv("OPEN_METEO", "https://api.open-meteo.com/v1/forecast")
        self.ARCHIVE_API = os.getenv("ARCHIVE_API", "https://archive-api.open-meteo.com/v1/archive")

        self.FORECAST_DAYS = int(os.getenv("FORECAST_DAYS", "7"))
        self.WEATHER_MODELS = os.getenv("WEATHER_MODELS", "best_match,ecmwf_ifs,gfs_global,icon_global").split(",")
        self.DEFAULT_SIGMA = float(os.getenv("DEFAULT_SIGMA", "2.0"))

        self.CALIB_THRESH = float(os.getenv("CALIB_THRESHOLD", os.getenv("MIN_EDGE", "0.08")))
        self.MIN_YES = float(os.getenv("MIN_YES_PRICE", "0.30"))
        self.MAX_YES = float(os.getenv("MAX_YES_PRICE", "0.90"))

        self.POS_MIN = float(os.getenv("POSITION_MIN_USD", "1"))
        self.POS_MAX = float(os.getenv("POSITION_MAX_USD", "1"))  # @to010: $1 固定超小注掃單
        self.POS_CAP_PCT = float(os.getenv("POSITION_CAPITAL_PCT", "0.02"))
        self.NO_EXIT_TARGET = float(os.getenv("NO_EXIT_TARGET", "0.98"))
        self.QUICK_PROFIT_PCT = float(os.getenv("QUICK_PROFIT_PCT", "0.05"))  # 快速止盈 +5%
        self.FIXED_TAKE_PROFIT_PCT = float(os.getenv("FIXED_TAKE_PROFIT_PCT", "0.09"))  # 固定止盈 +9%
        self.STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.065"))  # 止損 -6.5%
        self.TRAILING_ACTIVATE_PCT = float(os.getenv("TRAILING_ACTIVATE_PCT", "0.05"))  # 移動止盈激活: 浮盈 5%
        self.TRAILING_RETRACE_PCT = float(os.getenv("TRAILING_RETRACE_PCT", "0.03"))  # 移動止盈回撤: 3% 平倉
        self.TIME_STOP_HOURS = int(os.getenv("TIME_STOP_HOURS", "24"))  # 時間止損 24h
        self.THETA_ENABLED = os.getenv("THETA_ENABLED", "true").lower() == "true"  # Theta 懲罰開關
        self.OBI_ENABLED = os.getenv("OBI_ENABLED", "true").lower() == "true"  # OBI 過濾開關
        self.OBI_MIN_IMBALANCE = float(os.getenv("OBI_MIN_IMBALANCE", "0.3"))  # OBI 最小不均衡閾值
        self.TRADE_START_HOUR = int(os.getenv("TRADE_START_HOUR", "0"))  # 交易窗口起始 (UTC, 默认全天候)
        self.TRADE_END_HOUR = int(os.getenv("TRADE_END_HOUR", "24"))  # 交易窗口結束 (UTC, 默认全天候)
        self.FORCE_EXIT_HOURS = int(os.getenv("FORCE_EXIT_HOURS", "4"))
        self.FORCE_EXIT_MIN_PROFIT = float(os.getenv("FORCE_EXIT_MIN_PROFIT", "0.05"))

        self.MAX_POS_PER_CITY_DAY = int(os.getenv("MAX_POS_PER_CITY_DAY", "2"))
        self.MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "10"))
        self.LADDER_ENABLED = os.getenv("LADDER_ENABLED", "true").lower() == "true"
        self.LADDER_SPREAD = int(os.getenv("LADDER_SPREAD", "1"))       # 两侧各 N 个相邻桶
        self.LADDER_EDGE_BOOST = float(os.getenv("LADDER_EDGE_BOOST", "1.3"))  # 阶梯桶 edge 乘数
        self.LADDER_SIZE_PCT = float(os.getenv("LADDER_SIZE_PCT", "0.5"))       # 阶梯桶仓位比例
        self.ENSEMBLE_ENABLED = os.getenv("ENSEMBLE_ENABLED", "true").lower() == "true"
        self.ENSEMBLE_MODELS = os.getenv("ENSEMBLE_MODELS", "ecmwf_ifs,gfs_global,icon_global").split(",")
        raw_weights = os.getenv("ENSEMBLE_WEIGHTS", "0.4,0.3,0.3")
        self.ENSEMBLE_WEIGHTS = [float(w) for w in raw_weights.split(",")]

        # === 🌪️ 40成员 ICON Ensemble (PolyWeather 信号源) ===
        self.ENSEMBLE_40_ENABLED = os.getenv("ENSEMBLE_40_ENABLED", "true").lower() == "true"
        self.ENSEMBLE_40_MODEL = os.getenv("ENSEMBLE_40_MODEL", "icon_seamless")
        self.ENSEMBLE_API = os.getenv("ENSEMBLE_API", "https://ensemble-api.open-meteo.com/v1/ensemble")
        # ensemble_mode: "hybrid"=40成员优先, "ensemble_only"=只用40成员, "deterministic_only"=只用3模型
        self.ENSEMBLE_40_MODE = os.getenv("ENSEMBLE_40_MODE", "hybrid").lower()
        # 偏差信号阈值: 40成员ensemble概率 vs Polymarket价格的偏离>=此值触发开仓
        self.ENSEMBLE_40_EDGE = float(os.getenv("ENSEMBLE_40_EDGE", "0.06"))

        # === 🌐 多模型聚合 + 一致性评分 (Multi-Model Aggregation) ===
        self.ENABLE_MULTI_MODEL = os.getenv("ENABLE_MULTI_MODEL", "false").lower() == "true"
        # 指定参与聚合的模型名称子串（支持模糊匹配，如 'ecmwf' 匹配 'ecmwf_ifs'）
        self.MULTI_MODEL_MODELS = os.getenv("MULTI_MODEL_MODELS", "ecmwf_ifs,gfs_global,icon_global,meteofrance_seamless").split(",")
        # 模型一致性阈值: 单个模型桶概率 >= 此值计为"同意"
        self.MULTI_MODEL_AGREEMENT_THRESH = float(os.getenv("MULTI_MODEL_AGREEMENT_THRESH", "0.30"))
        # 离散度惩罚上限 (°C): spread >= 此值时额外惩罚达最大值
        self.MULTI_MODEL_SPREAD_CAP = float(os.getenv("MULTI_MODEL_SPREAD_CAP", "15.0"))
        self.MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))

        self.INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "10000"))

        self.BIAS_ENABLED = os.getenv("BIAS_ENABLED", "true").lower() == "true"
        self.BIAS_DAYS = int(os.getenv("BIAS_DAYS", "30"))
        self.CROSS_VALIDATE = os.getenv("CROSS_VALIDATE", "true").lower() == "true"

        # 城市白名单过滤（可选，默认全部）
        raw_cities = os.getenv("ALLOWED_CITIES", "")
        self.ALLOWED_CITIES = [c.strip() for c in raw_cities.split(",")] if raw_cities else []
        raw_sides = os.getenv("ALLOWED_SIDES", "YES,NO")
        self.ALLOWED_SIDES = [s.strip().upper() for s in raw_sides.split(",")]

        # === 🌡️ METAR 实时观测数据 ===
        self.ENABLE_METAR = os.getenv("ENABLE_METAR", "true").lower() == "true"
        # METAR 默认使用 STATIONS 表中的 ICAO 映射，也可通过环境变量自定义
        raw_metar = os.getenv("METAR_STATIONS", "")
        self.METAR_STATIONS: dict[str, str] = {}
        if raw_metar:
            # 格式: City1=ICAO1,City2=ICAO2
            for part in raw_metar.split(","):
                if "=" in part:
                    c, code = part.split("=", 1)
                    self.METAR_STATIONS[c.strip()] = code.strip()
        # 用 STATIONS 中的 ICAO 做默认补全
        for s in STATIONS:
            city, icao = s[0], s[1]
            if city not in self.METAR_STATIONS:
                self.METAR_STATIONS[city] = icao
        # 当 METAR 温度偏离预报 >= 此值时生成 extreme 信号
        self.METAR_DEVIATION_THRESH = float(os.getenv("METAR_DEVIATION_THRESH", "3.0"))
        # 极端低估合约阈值: p_market < 此值视为极低估
        self.EXTREME_BUY_THRESH = float(os.getenv("EXTREME_BUY_THRESH", "0.05"))
        # METAR 信号覆盖预报: 启用后 METAR 温度直接用于概率计算
        self.METAR_OVERRIDE_ENABLED = os.getenv("METAR_OVERRIDE_ENABLED", "false").lower() == "true"

        # === 🎯 Fractional Kelly 仓位优化 ===
        self.KELLY_ENABLED = os.getenv("KELLY_ENABLED", "true").lower() == "true"
        # 各信号类型的 Kelly 分数 (fraction of full Kelly)
        self.KELLY_FRACTION_METAR = float(os.getenv("KELLY_FRACTION_METAR", "0.50"))
        self.KELLY_FRACTION_MODEL = float(os.getenv("KELLY_FRACTION_MODEL", "0.25"))
        self.KELLY_FRACTION_LADDER = float(os.getenv("KELLY_FRACTION_LADDER", "0.15"))
        # 单笔最大风险限制 (占资本比例)
        self.KELLY_MAX_RISK_PCT = float(os.getenv("KELLY_MAX_RISK_PCT", "0.02"))
        # 最小和最大仓位绝对值 ($)
        self.KELLY_MIN_SIZE = float(os.getenv("KELLY_MIN_SIZE", "1.0"))
        self.KELLY_MAX_SIZE = float(os.getenv("KELLY_MAX_SIZE", "100.0"))

        # === 📊 信号历史验证闭环 ===
        self.SIGNAL_HISTORY_ENABLED = os.getenv("SIGNAL_HISTORY_ENABLED", "true").lower() == "true"
        # 历史信号最低胜率: 低于此值自动拒绝开仓
        self.SIGNAL_HISTORY_MIN_WR = float(os.getenv("SIGNAL_HISTORY_MIN_WR", "0.40"))
        # 信号历史查询窗口 (天)
        self.SIGNAL_HISTORY_DAYS = int(os.getenv("SIGNAL_HISTORY_DAYS", "30"))

        # === 🎯 v7: 自适应宽度 ===
        self.ADAPTIVE_WIDTH_ENABLED = os.getenv("ADAPTIVE_WIDTH_ENABLED", "true").lower() == "true"
        self.ADAPTIVE_WIDTH_MIN_SPREAD = int(os.getenv("ADAPTIVE_WIDTH_MIN_SPREAD", "1"))  # 最小扩撒桶数
        self.ADAPTIVE_WIDTH_MAX_SPREAD = int(os.getenv("ADAPTIVE_WIDTH_MAX_SPREAD", "3"))  # 最大扩撒桶数
        self.ADAPTIVE_WIDTH_THRESH_LOW = float(os.getenv("ADAPTIVE_WIDTH_THRESH_LOW", "0.35"))  # 低估阈值 (p_market < 0.35 → 缩小)
        self.ADAPTIVE_WIDTH_THRESH_HIGH = float(os.getenv("ADAPTIVE_WIDTH_THRESH_HIGH", "0.70"))  # 高估阈值 (p_market > 0.70 → 扩大)

        # === 🏙️ v7: 多城市联合扫描 ===
        self.MULTI_CITY_ENABLED = os.getenv("MULTI_CITY_ENABLED", "true").lower() == "true"
        self.MULTI_CITY_TOP_N = int(os.getenv("MULTI_CITY_TOP_N", "2"))  # 选 TOP N 个城市

        # === 💰 v7: 动态预算分配 ===
        self.DYNAMIC_BUDGET_ENABLED = os.getenv("DYNAMIC_BUDGET_ENABLED", "true").lower() == "true"
        self.DYNAMIC_BUDGET_MIN = float(os.getenv("DYNAMIC_BUDGET_MIN", "0.20"))  # 最低预算
        self.DYNAMIC_BUDGET_MAX = float(os.getenv("DYNAMIC_BUDGET_MAX", "1.00"))  # 最高预算

        # === 🧱 v7: 逐层止盈 ===
        self.LAYERED_TP_ENABLED = os.getenv("LAYERED_TP_ENABLED", "true").lower() == "true"
        self.LAYERED_TP_PROFIT_PCT = float(os.getenv("LAYERED_TP_PROFIT_PCT", "0.10"))  # 浮盈 >10% 触发
        self.LAYERED_TP_HOURS_BEFORE = float(os.getenv("LAYERED_TP_HOURS_BEFORE", "2.0"))  # 结算前 2 小时

        self.DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
        self.DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "3002"))

        # 持久化数据库路径
        _default_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard", "hightemptation.db")
        self.DB_PATH = os.getenv("DB_PATH", _default_db)

        # 溫度桶: 下界,上界,標籤（多組用 | 分隔）
        raw = os.getenv("TEMP_BUCKETS",
            "25,27,25-27|27,29,27-29|29,31,29-31|31,33,31-33|33,35,33-35|"
            "35,37,35-37|37,39,37-39|39,41,39-41|41,43,41-43|43,100,43+")
        self.buckets: list[tuple[float, float, str]] = []
        for part in raw.split("|"):
            xs = part.split(",")
            if len(xs) >= 3:
                self.buckets.append((float(xs[0]), float(xs[1]), xs[2].strip()))

    @property
    def daily_loss_limit(self) -> float:
        return self.INITIAL_CAPITAL * self.MAX_DAILY_LOSS_PCT


cfg = Config()


def reload_config():
    """從環境變量重新加載配置，不重置引擎狀態，實現熱更新"""
    global cfg
    old = cfg
    cfg = Config()
    # 保留原有值不受 env 影響的字段
    logger.info(f"♻️ 配置熱更新: 倉位 ${cfg.POS_MIN:.0f}-${cfg.POS_MAX:.0f} | "
                f"最大持倉 {cfg.MAX_CONCURRENT} | "
                f"快盈+{cfg.QUICK_PROFIT_PCT*100:.0f}% 固定止盈+{cfg.FIXED_TAKE_PROFIT_PCT*100:.0f}% 止損-{cfg.STOP_LOSS_PCT*100:.1f}% | "
                f"閾值 {cfg.CALIB_THRESH*100:.0f}%")
    return cfg

# ── 日誌 ──
logging.basicConfig(level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("hightemptation")


# ═══════════════════════════════════════════════════════════════════════
# 2. Open-Meteo 預報 + 3. 偏差校正 + 4. METAR
# ═══════════════════════════════════════════════════════════════════════

class WeatherClient:
    def __init__(self):
        self._http: Optional[httpx.AsyncClient] = None
        # ── 偏差快取 ──
        self.bias_db = Path.home() / ".hightemptation" / "bias.db"
        self.bias_db.parent.mkdir(parents=True, exist_ok=True)
        self._init_bias_db()

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def close(self):
        if self._http:
            await self._http.aclose(); self._http = None

    # ── 偏差資料庫 ──
    def _init_bias_db(self):
        with sqlite3.connect(self.bias_db, check_same_thread=False) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS bias (
                    city TEXT, icao TEXT, fc_date TEXT,
                    forecast REAL, actual REAL, bias REAL,
                    UNIQUE(city, fc_date)
                )""")
            db.commit()

    def record_forecast(self, city: str, icao: str, fc_date: str, temp: float):
        with sqlite3.connect(self.bias_db, check_same_thread=False) as db:
            db.execute(
                "INSERT OR IGNORE INTO bias(city,icao,fc_date,forecast) VALUES(?,?,?,?)",
                (city, icao, fc_date, temp))
            db.commit()

    def get_rolling_bias(self, city: str, days: int = 30) -> tuple[float, int]:
        """(rolling_bias, sample_count) — 偏差 = 預報 - 實際"""
        with sqlite3.connect(self.bias_db, check_same_thread=False) as db:
            rows = db.execute(
                "SELECT bias FROM bias WHERE city=? AND bias IS NOT NULL "
                "ORDER BY fc_date DESC LIMIT ?", (city, days)).fetchall()
        if not rows:
            return (0.0, 0)
        biases = [r[0] for r in rows if r[0] is not None]
        return (sum(biases) / len(biases), len(biases)) if biases else (0.0, 0)

    async def update_bias_from_archive(self, city: str, lat: float, lon: float, days: int = 30):
        """用 Open-Meteo Archive 更新實際值，計算偏差"""
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=days)
        url = (f"{cfg.ARCHIVE_API}?latitude={lat}&longitude={lon}"
               f"&daily=temperature_2m_max&start_date={start}&end_date={end}&timezone=auto")
        try:
            r = await self.http.get(url, timeout=20)
            r.raise_for_status()
            data = r.json()
            times = data.get("daily", {}).get("time", [])
            temps = data.get("daily", {}).get("temperature_2m_max", [])
            count = 0
            with sqlite3.connect(self.bias_db, check_same_thread=False) as db:
                for i, day in enumerate(times):
                    if i < len(temps) and temps[i] is not None:
                        actual = float(temps[i])
                        db.execute(
                            "UPDATE bias SET actual=?, bias=forecast-? "
                            "WHERE city=? AND fc_date=? AND actual IS NULL",
                            (actual, actual, city, day))
                        count += db.rowcount
                db.commit()
            if count:
                logger.info(f"  📊 偏差更新 {city}: {count} 筆")
        except Exception as e:
            logger.debug(f"Archive {city}: {e}")

    # ── Open-Meteo 40成员 ICON Ensemble 预报 ──
    async def get_ensemble_40_forecast(self, lat: float, lon: float) -> list[dict]:
        """
        从 Open-Meteo Ensemble API 获取 40-member ICON-EPS 成员级预报。

        返回 [{
            date, ensemble_mean: float,
            members: [float, ...]  (39个成员, member01~member39),
            mu: float  (成员平均值),
            sigma: float  (成员标准差),
            n_members: int
        }]
        """
        url = (f"{cfg.ENSEMBLE_API}?latitude={lat}&longitude={lon}"
               f"&daily=temperature_2m_max&models={cfg.ENSEMBLE_40_MODEL}"
               f"&timezone=auto&forecast_days={cfg.FORECAST_DAYS}")
        try:
            r = await self.http.get(url, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning(f"Ensemble40 预报失败 ({lat},{lon}): {e}")
            return []

        daily = data.get("daily", {})
        times = daily.get("time", [])
        ens_mean_list = daily.get("temperature_2m_max", [])
        result = []
        for i, day in enumerate(times):
            members = []
            for m in range(1, 40):
                key = f"temperature_2m_max_member{m:02d}"
                if key in daily and i < len(daily[key]) and daily[key][i] is not None:
                    members.append(float(daily[key][i]))
            if not members:
                continue
            mu = sum(members) / len(members)
            sigma = math.sqrt(sum((t - mu) ** 2 for t in members) / len(members)) if len(members) > 1 else cfg.DEFAULT_SIGMA
            if sigma < 0.3:
                sigma = cfg.DEFAULT_SIGMA

            ens_mean = float(ens_mean_list[i]) if i < len(ens_mean_list) and ens_mean_list[i] is not None else mu

            result.append({
                "date": day,
                "ensemble_mean": ens_mean,
                "members": members,
                "mu": mu,
                "sigma": sigma,
                "n_members": len(members),
            })
        return result

    # ── METAR 批量获取 + 缓存 ──
    _metar_cache: dict[str, tuple[float, float]] = {}  # icao → (temp, timestamp)
    _metar_cache_ttl = 300  # 5 分钟缓存

    async def fetch_metar_temperature(self, icao: str) -> Optional[float]:
        """获取单个 METAR 温度，带缓存"""
        now = time.time()
        if icao in self._metar_cache:
            temp, ts = self._metar_cache[icao]
            if now - ts < self._metar_cache_ttl:
                return temp
        temp = await self.fetch_metar(icao)
        if temp is not None:
            self._metar_cache[icao] = (temp, now)
        return temp

    async def fetch_all_metar(self) -> dict[str, float]:
        """
        批量获取所有已配置城市的 METAR 实时温度。

        Returns:
            dict[city_name] = temperature_celsius
        """
        if not cfg.ENABLE_METAR:
            return {}
        results: dict[str, float] = {}
        tasks = []
        cities = []
        for city in cfg.METAR_STATIONS:
            icao = cfg.METAR_STATIONS[city]
            if icao:
                tasks.append(self.fetch_metar_temperature(icao))
                cities.append(city)
        if not tasks:
            return results
        temps = await asyncio.gather(*tasks, return_exceptions=True)
        for i, city in enumerate(cities):
            t = temps[i]
            if isinstance(t, Exception):
                logger.debug(f"METAR {city} ({cfg.METAR_STATIONS.get(city,'')}): {t}")
                continue
            if t is not None:
                results[city] = t
                logger.info(f"  🌡️ METAR {city} ({cfg.METAR_STATIONS.get(city,'')}): {t:.1f}°C")
        if results:
            logger.info(f"  🌡️ METAR 批量采集: {len(results)}/{len(tasks)} 城市成功")
        return results

    # ── 计算 40 成员集合概率（非参数法）──
    @staticmethod
    def calc_ensemble_40_prob(members: list[float], lower: float, upper: float) -> float:
        """
        直接用 40 个成员的温度值计算落在 [lower, upper) 内的比例。
        这是完全非参数的概率估计，不依赖高斯假设。
        """
        if not members:
            return 0.0
        count = sum(1 for t in members if lower <= t < upper)
        return count / len(members)

    @staticmethod
    def calc_ensemble_40_threshold_prob(members: list[float], threshold: float, direction: str) -> float:
        """
        非参数概率: P(temp < threshold) 或 P(temp > threshold)
        """
        if not members:
            return 0.0
        if direction == "below":
            return sum(1 for t in members if t < threshold) / len(members)
        else:
            return sum(1 for t in members if t > threshold) / len(members)

    # ── Open-Meteo 多模型預報 ──
    async def get_forecast(self, lat: float, lon: float) -> list[dict]:
        """[{date, models:{model:temp,...}}]"""
        models = ",".join(cfg.WEATHER_MODELS)
        url = (f"{cfg.OPEN_METEO}?latitude={lat}&longitude={lon}"
               f"&daily=temperature_2m_max&models={models}"
               f"&timezone=auto&forecast_days={cfg.FORECAST_DAYS}")
        try:
            r = await self.http.get(url, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning(f"預報失敗 ({lat},{lon}): {e}")
            return []

        daily = data.get("daily", {})
        times = daily.get("time", [])
        result = []
        for i, day in enumerate(times):
            mt = {}
            for key, vals in daily.items():
                if key == "time": continue
                if i < len(vals) and vals[i] is not None:
                    mname = key.replace("temperature_2m_max_", "") or "best_match"
                    mt[mname] = float(vals[i])
            result.append({"date": day, "models": mt})
        return result

    async def get_city_forecasts(self, city_list: Optional[list[tuple[str, float, float]]] = None) -> list[dict]:
        """
        獲取指定列表城市的預報。
        city_list: [(city_name, lat, lon), ...]，None 則用 STATION_IDX
        返回 [{
            city, icao, date, raw_mean, corrected_mean, sigma,
            model_count, is_reliable, bias_correction
        }]
        """
        results = []
        sources = city_list if city_list else [(c, *station_coords(c)[:2]) for c in STATION_IDX]
        for item in sources:
            if len(item) == 3:
                city, lat, lon = item
            else:
                city, lat, lon = item[0], item[1], item[2]
            icao = station_icao(city) if city in STATION_IDX else ""
            if lat == 0 and lon == 0:
                logger.debug(f"跳過 {city}: 無效坐標")
                continue
            lat, lon = station_coords(city)
            icao = station_icao(city)
            if lat == 0: continue

            days = await self.get_forecast(lat, lon)
            for day in days:
                mt = day["models"]
                if not mt: continue
                temps = list(mt.values())
                raw = sum(temps) / len(temps)
                sigma = math.sqrt(sum((t - raw) ** 2 for t in temps) / len(temps)) if len(temps) > 1 else cfg.DEFAULT_SIGMA
                if sigma < 0.3: sigma = cfg.DEFAULT_SIGMA

                # 偏差校正
                bias_corr = 0.0
                if cfg.BIAS_ENABLED:
                    rb, n = self.get_rolling_bias(city, cfg.BIAS_DAYS)
                    if n >= 5:
                        bias_corr = rb
                corrected = raw - bias_corr

                # 存預報供後續偏差計算
                self.record_forecast(city, icao, day["date"], raw)

                # 交叉驗證（粗校：Archive vs 預報）
                reliable = True
                if cfg.CROSS_VALIDATE:
                    # 用 Archive 今天實際值做粗校
                    pass  # 簡化版跳過耗時驗證

                results.append({
                    "city": city, "icao": icao,
                    "date": day["date"],
                    "raw_mean": raw,
                    "mean": corrected,
                    "sigma": sigma,
                    "model_count": len(mt),
                    "models": mt,
                    "bias_correction": bias_corr,
                    "is_reliable": reliable,
                })
                bias_info = f" 校正{bias_corr:+.1f}°C" if bias_corr else ""
                label = icao or f"({lat},{lon})"
                logger.info(f"🌤️  {city}({label}) {day['date']}: μ={corrected:.1f}°C σ={sigma:.1f}°C ({len(mt)}模型){bias_info}")

        return results

    # ── METAR 實際值 ──
    async def fetch_metar(self, icao: str) -> Optional[float]:
        """從 aviationweather.gov 獲取 METAR 當前溫度"""
        url = f"https://www.aviationweather.gov/metar/data?ids={icao}&format=decoded&hours=1"
        try:
            r = await self.http.get(url, timeout=15)
            r.raise_for_status()
            text = r.text
            # 解析 "Temperature: 28.0°C (82°F)"
            m = re.search(r'Temperature:\s*([\d.]+)', text)
            if m: return float(m.group(1))
            # 或 "Temp: 28"
            m = re.search(r'Temp[^:]*:\s*([\d.]+)', text)
            if m: return float(m.group(1))
        except Exception as e:
            logger.debug(f"METAR {icao}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# 5. 市場解析 + 6. 高斯概率
# ═══════════════════════════════════════════════════════════════════════

def extract_city_name(question: str) -> Optional[str]:
    """從市場問題中提取城市名稱，使用 normalize_city_name 標準化。"""
    q = question.lower()
    # 1. 直接匹配已知城市名（含 CITY_ALIASES 中的所有城市）
    for c in STATION_IDX:
        if c.lower() in q:
            return c
    for alias in CITY_ALIASES:
        if alias in q:
            mapped = CITY_ALIASES[alias]
            return mapped if mapped else alias.title()

    # 2. 使用正則提取
    patterns = [
        r'\bin\s+([A-Z][a-zA-Z\s-]+?)(?:\s+on|\s+for|\s+is|\(|,|$)',
        r"([A-Z][a-zA-Z\s-]+?)'s\s+(?:high|max|temp|temperature)",
        r'\bfor\s+([A-Z][a-zA-Z\s-]+?)(?:\s+on|\s+is|\(|,|$)',
        r'^([A-Z][a-zA-Z\s-]+?)\s+on\s',  # "Paris on July 30?"
    ]
    for pat in patterns:
        m = re.search(pat, question)
        if m:
            name = m.group(1).strip().rstrip(",.!?;")
            if 1 < len(name) < 40:
                return normalize_city_name(name)

    # 3. Fallback: 從事件標題中提取
    words = re.findall(r'[A-Z][a-z]+', question)
    for w in words:
        if len(w) > 2 and w.lower() not in ("will", "the", "be", "between", "above", "below", "and", "for", "with"):
            return normalize_city_name(w)
    return None


def parse_date_from_question(question: str) -> Optional[str]:
    """
    用 dateutil 解析市場問題中的自然語言日期。
    支援: "July 29", "Jul 29", "29 July", "2025-07-15", "07/15/2025" 等。
    返回 YYYY-MM-DD 格式，年份缺失時使用當年。
    """
    # 先試 ISO 格式
    dm = re.search(r'(\d{4}-\d{2}-\d{2})', question)
    if dm:
        return dm.group(1)

    # 用 dateutil 模糊解析
    # 常見模式: "on July 29", "on Jul 29", "for 29 July 2025"
    # 找 "on <...>" 或 "for <...>" 中的日期部分
    date_candidates = re.findall(
        r'(?:on|for)\s+((?:(?:\d{1,2})(?:st|nd|rd|th)?\s+)?'
        r'(?:January|February|March|April|May|June|July|August|September|'
        r'October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
        r'\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)',
        question, re.IGNORECASE)
    for txt in date_candidates:
        try:
            dt = dateparser.parse(txt, fuzzy=True)
            if dt:
                return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    # 再試 "<Month> <Day>" 出現在任何位置
    try:
        dt = dateparser.parse(question, fuzzy=True)
        if dt:
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass

    return None


def parse_market_question(question: str) -> Optional[dict]:
    """
    從市場問題提取 {city, date, threshold_temp, threshold_dir, label}
    或 {city, date, lower, upper, label}

    支援格式:
      "Will the highest temperature in Hong Kong be 25°C or below on July 29?"
      "Will the highest temperature in Hong Kong be 26°C on July 29?"
      "Will the highest temperature in Tokyo on 2025-07-15 be between 33°C and 35°C?"
      "Will New York's max temp on 2025-07-15 exceed 35°C?"
    """
    q = question.lower()

    city = extract_city_name(question)
    if not city:
        return None

    fc_date = parse_date_from_question(question)
    if not fc_date:
        return None

    nums = [float(x) for x in re.findall(r'(\d+)\s*°', question)]
    if not nums:
        return None

    # 判斷溫度類型
    is_below = any(kw in q for kw in ["below", "under", "<", "≤"])
    is_above = any(kw in q for kw in ["above", "over", "exceed", ">", "≥", "at least", "higher"])

    if len(nums) >= 2:
        # 範圍: "between X°C and Y°C"
        return {"city": city, "date": fc_date, "lower": nums[0], "upper": nums[1],
                "label": f"{nums[0]:.0f}-{nums[1]:.0f}°C"}
    elif is_below:
        # 單閾值以下: "X°C or below"
        return {"city": city, "date": fc_date, "threshold": nums[0], "dir": "below",
                "label": f"≤{nums[0]:.0f}°C"}
    elif is_above:
        # 單閾值以上: "X°C or above"
        return {"city": city, "date": fc_date, "threshold": nums[0], "dir": "above",
                "label": f"≥{nums[0]:.0f}°C"}
    else:
        # 單值: "X°C" (映射為 ±0.5°C 範圍)
        return {"city": city, "date": fc_date, "lower": nums[0] - 0.5, "upper": nums[0] + 0.5,
                "label": f"{nums[0]:.0f}°C"}


async def scan_all_weather_markets(http: httpx.AsyncClient) -> list[dict]:
    """
    全面掃描所有 Polymarket 活躍天氣市場。

    使用 Gamma API public-search 分頁拉取 `highest temperature` 相關事件，
    並補充搜索 `weather market` 關鍵詞以捕獲其他格式的天氣市場。
    自動分頁（每頁50）直到沒有更多結果，去重後返回所有合格市場。

    Returns:
        list[dict]: 每個市場包含 id, question, yes_price, no_price,
                     liquidity, end_date, no_token_id
    """
    seen_market_ids: set[str] = set()
    seen_event_ids: set[str] = set()
    raw_markets: list[dict] = []

    # ── 第一輪：用 "highest temperature" 分頁搜索 ──
    search_queries = [
        "temperature",          # 主查询: 返回全部温度事件（177+）
        "highest temperature",  # 备用: 捕获特定措辞
        "weather market",       # 备用: 捕获非温度天气市场
        "°C",                   # 备用: 捕获未命中的事件
        "high temp",           # 备用
    ]

    for query in search_queries:
        offset = 0
        page = 0
        while True:
            try:
                r = await http.get(f"{cfg.GAMMA_API}/public-search", params={
                    "q": query,
                    "events_status": "active",
                    "keep_closed_markets": 0,
                    "limit_per_type": 50,
                    "offset": offset,
                }, timeout=20)
                r.raise_for_status()
                body = r.json()
                events = body.get("events", [])
                pagination = body.get("pagination", {})
                total = pagination.get("totalResults", 0)
                has_more = pagination.get("hasMore", False)

                if not events:
                    break

                for evt in events:
                    eid = str(evt.get("id", ""))
                    if eid and eid in seen_event_ids:
                        continue
                    if eid:
                        seen_event_ids.add(eid)

                    title = evt.get("title", "")
                    evt_markets = evt.get("markets", [])

                    # 只保留天氣相關事件（檢查事件標題 + slug + 市場問題）
                    combined = (title + " " + evt.get("slug", "")).lower()
                    weather_kw = ["temperature", "temp", "°c", "celsius", "°f", "fahrenheit", "high", "weather"]
                    if not any(k in combined for k in weather_kw):
                        # fallback: 檢查市場問題
                        has_weather_market = False
                        for mkt in evt_markets:
                            mq = str(mkt.get("question", "") or "").lower()
                            if any(k in mq for k in weather_kw):
                                has_weather_market = True
                                break
                        if not has_weather_market:
                            continue

                    for mkt in evt_markets:
                        mid = str(mkt.get("conditionId", "") or mkt.get("id", ""))
                        if mid and mid not in seen_market_ids:
                            seen_market_ids.add(mid)
                            raw_markets.append(mkt)

                page += 1
                offset = page * 50

                # 分頁終止條件
                if not has_more or offset >= total or offset >= 500:
                    break

            except Exception as e:
                logger.debug(f"public-search 失敗 (q={query}, offset={offset}): {e}")
                break

        logger.info(f"  🔍 搜索 '{query}': 掃描 {page*50}+ 事件, 收集 {len(raw_markets)} 個原始市場")

    # ── 補充掃描：用 /markets 端點 + question__icontains 補漏 ──
    try:
        r = await http.get(f"{cfg.GAMMA_API}/markets", params={
            "limit": 100,
            "closed": False,
            "active": True,
            "question__icontains": "temperature",
        }, timeout=20)
        r.raise_for_status()
        direct_markets = r.json()
        added = 0
        for m in direct_markets:
            mid = str(m.get("conditionId", "") or m.get("id", ""))
            if mid and mid not in seen_market_ids:
                q = str(m.get("question", "") or "").lower()
                if any(k in q for k in ["temperature", "temp", "°c", "celsius", "°f", "high"]):
                    seen_market_ids.add(mid)
                    raw_markets.append(m)
                    added += 1
        if added:
            logger.info(f"  🔁 /markets 補充掃描: +{added} 個天氣市場")
    except Exception as e:
        logger.debug(f"/markets 補充掃描失敗: {e}")

    # ── 第二輪：解析 + 過濾 ──
    parsed = []
    for m in raw_markets:
        try:
            q = str(m.get("question", "") or m.get("title", ""))
            # 強制天氣關鍵詞過濾
            q_lower = q.lower()
            if not any(k in q_lower for k in ["temperature", "temp", "°c", "°f", "high"]):
                continue

            op = m.get("outcomePrices", "[]")
            op = json.loads(op) if isinstance(op, str) else op
            yes = float(op[0]) if len(op) > 0 else 0.5
            no_ = float(op[1]) if len(op) > 1 else 0.5

            liquidity = float(str(m.get("liquidity", "0") or "0"))

            # 提取 token ID 用於 OBI 查詢
            tokens = m.get("tokens", [])
            no_token_id = ""
            for t in tokens:
                if t.get("outcome", "").upper() == "NO":
                    no_token_id = str(t.get("token_id", ""))
                    break

            parsed.append({
                "id": str(m.get("conditionId", "") or m.get("id", "")),
                "question": q,
                "yes_price": yes,
                "no_price": no_,
                "liquidity": liquidity,
                "closed": bool(m.get("closed", False)),
                "end_date": m.get("endDate", ""),
                "no_token_id": no_token_id,
            })
        except Exception:
            continue

    # 過濾：ID有效 + 未結算 + 有流動性
    filtered = [p for p in parsed if p["id"] and not p["closed"] and p["liquidity"] >= cfg.MIN_MARKET_LIQUIDITY]
    logger.info(f"  🌡️ 全量天氣市場: {len(raw_markets)}原始 → {len(parsed)}可解析 → {len(filtered)}合格")
    return filtered


# ═══════════════════════════════════════════════════════════════════════
# 7. 出場邏輯 + 8. 持倉追蹤 + 9. 主循環
# ═══════════════════════════════════════════════════════════════════════

class Position:
    __slots__ = ("market_id", "city", "date", "bucket_label",
                 "side", "entry_no", "size", "curr_no", "end_date",
                 "pnl", "pct", "is_open", "exit_reason",
                 "exit_time", "realized", "_settled",
                 "peak_no", "entry_time", "_db_id",
                 "_p_model", "_signal_type", "_strategy")

    def __init__(self, market_id: str, city: str, date_: str, label: str,
                 entry_no: float, size: float, end_date: str = "",
                 side: str = "NO"):
        self.market_id = market_id
        self.city = city
        self.date = date_
        self.bucket_label = label
        self.side = side
        self.entry_no = entry_no
        self.size = size
        self.curr_no = entry_no
        self.end_date = end_date
        self.pnl = 0.0
        self.pct = 0.0
        self.is_open = True
        self.exit_reason = ""
        self.exit_time = ""
        self.realized = 0.0
        self._settled = False
        self.peak_no = entry_no
        self.entry_time = datetime.now(timezone.utc).isoformat()
        self._db_id = None
        self._strategy = "model_edge"

    def update(self, no_price: float):
        self.curr_no = no_price
        if no_price > self.peak_no:
            self.peak_no = no_price
        if self.entry_no > 0:
            self.pct = (no_price - self.entry_no) / self.entry_no
            self.pnl = self.size * self.pct

    def close(self, reason: str, pnl: float):
        self.is_open = False
        self.exit_reason = reason
        self.exit_time = datetime.now(timezone.utc).isoformat()
        self.realized = pnl

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "city": self.city,
            "date": self.date,
            "bucket": self.bucket_label,
            "side": self.side,
            "entry_no": round(self.entry_no, 4),
            "curr_no": round(self.curr_no, 4),
            "size": round(self.size, 0),
            "pnl": round(self.pnl, 2),
            "pct": round(self.pct * 100, 1),
            "open": self.is_open,
            "exit_reason": self.exit_reason,
            "exit_time": self.exit_time,
            "realized": round(self.realized, 2) if not self.is_open else None,
        }


class Engine:
    def __init__(self, db_path: Optional[str] = None):
        self.positions: list[Position] = []
        self.closed: list[Position] = []
        self.capital = cfg.INITIAL_CAPITAL
        self.total = 0
        self.wins = 0
        self.losses = 0
        self.daily_pnl = 0.0
        self.today = date.today()
        self.capital_history: list[tuple[str, float]] = [(datetime.now(timezone.utc).isoformat(), cfg.INITIAL_CAPITAL)]
        # ── 持久化 TradeDB ──
        self.db_path = db_path or cfg.DB_PATH
        try:
            self.db = TradeDB(self.db_path) if _HAS_TRADE_DB else None
            if self.db:
                logger.info(f"🗄️ TradeDB 已连接: {self.db_path}")
        except Exception as e:
            logger.warning(f"⚠️ TradeDB 连接失败: {e}")
            self.db = None

    def has_position(self, mid: str) -> bool:
        """檢查 market_id 是否已存在（含持倉 + 已平倉），防止重複開倉"""
        if any(p.is_open and p.market_id == mid for p in self.positions):
            return True
        if any(p.market_id == mid for p in self.closed):
            return True
        return False

    def is_on_cooldown(self, end_date_str: str) -> bool:
        """距結算 < 60 分鐘不開新倉"""
        if not end_date_str:
            return False
        try:
            ed = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            remaining = (ed - datetime.now(timezone.utc)).total_seconds() / 60
            return remaining < 60
        except Exception:
            return False

    def city_day_count(self, city: str, dt: str) -> int:
        return sum(1 for p in self.positions if p.is_open and p.city == city and p.date == dt)

    def calc_kelly_size(self, p_model: float, p_market: float,
                          side: str, signal_type: str = "MODEL") -> float:
        """
        Fractional Kelly 仓位计算。

        标准 Kelly 公式: f* = (bp - q) / b
          - b: 净赔率 (对于 NO 方向: b = p_market / (1-p_market); 对于 YES: b = (1-p_market) / p_market)
          - p: 我们估计的概率 (p_model)
          - q: 1 - p
          - f*: 应投入资本的比例

        按信号类型动态调整 Kelly 分数:
          - METAR:  0.50 (实时数据更可靠)
          - MODEL:  0.25 (预报模型，不确定性中等)
          - LADDER: 0.15 (衍生信号，风险最高)

        单笔最大风险: KELLY_MAX_RISK_PCT (默认 2%)

        Args:
            p_model: 模型估计概率
            p_market: 市场价格
            side: "YES" 或 "NO"
            signal_type: "METAR", "MODEL", "LADDER"

        Returns:
            建议仓位大小 (美元)
        """
        if not cfg.KELLY_ENABLED:
            # 回退到旧逻辑
            return self._legacy_position_size(abs(p_market - p_model))

        if p_model <= 0 or p_model >= 1 or p_market <= 0 or p_market >= 1:
            return cfg.KELLY_MIN_SIZE

        # 1. 赔率 b = (1 - entry_price) / entry_price
        #    entry_price 是我们买入的价格: NO 方向用 NO 价格, YES 方向用 YES 价格
        if side == "NO":
            # 买 NO: entry_price = 1 - p_market (NO 价格)
            entry_price = 1.0 - p_market
            # 我们的概率: p_model 是 YES 概率, 所以 NO 概率 = 1 - p_model
            p = 1.0 - p_model
        else:
            # 买 YES: entry_price = p_market (YES 价格)
            entry_price = p_market
            # 我们的概率 = p_model
            p = p_model

        if entry_price <= 0 or entry_price >= 1:
            return 0.0

        # 净赔率: 如果赢, 每投入 $1 赚 (1 - entry_price) / entry_price
        b = (1.0 - entry_price) / entry_price
        q = 1.0 - p
        bp_minus_q = b * p - q

        # 没有 edge → 不开仓
        if bp_minus_q <= 0:
            return 0.0

        full_kelly = bp_minus_q / b  # f* = (bp - q) / b

        # 3. 按信号类型选择 Kelly 分数
        fraction_map = {
            "METAR": cfg.KELLY_FRACTION_METAR,
            "MODEL": cfg.KELLY_FRACTION_MODEL,
            "LADDER": cfg.KELLY_FRACTION_LADDER,
        }
        kelly_frac = fraction_map.get(signal_type, cfg.KELLY_FRACTION_MODEL)

        # 4. 计算建议仓位 (占资本比例)
        recommended_pct = full_kelly * kelly_frac

        # 5. 风险限制: 不超过 N% 资本
        max_risk_pct = cfg.KELLY_MAX_RISK_PCT
        recommended_pct = min(recommended_pct, max_risk_pct)

        # 6. 转为具体金额
        size = self.capital * recommended_pct

        # 7. 上下界截断
        size = max(cfg.KELLY_MIN_SIZE, min(size, cfg.KELLY_MAX_SIZE))

        # 小額保留一位小数
        if size <= 10:
            size = round(size, 1)
        else:
            size = round(size / 10) * 10

        edge_pct = abs(p_market - p_model) * 100
        logger.debug(f"  💰 Kelly {signal_type}: p_model={p:.3f} mkt={p_market:.3f} "
                     f"b={b:.2f} f={full_kelly:.4f} fraction={kelly_frac} "
                     f"→ {recommended_pct*100:.2f}% 仓位=${size:.0f} (edge={edge_pct:.1f}%)")
        return size

    def _legacy_position_size(self, diff: float) -> float:
        """旧的线性仓位公式，作为 Kelly 回退"""
        r = min(1.0, (diff - cfg.CALIB_THRESH) / 0.35)
        s = cfg.POS_MIN + (cfg.POS_MAX - cfg.POS_MIN) * r
        s = min(s, self.capital * cfg.POS_CAP_PCT)
        s = max(cfg.POS_MIN, min(s, cfg.POS_MAX))
        if s <= 10:
            return round(s, 1)
        return round(s / 10) * 10

    def calc_position_size(self, diff: float) -> float:
        """兼容性包装: 旧接口调用 Kelly"""
        return self._legacy_position_size(diff)

    def check_signal_history(self, city: str, bucket_label: str,
                               signal_type: str = "MODEL") -> tuple[bool, float, int]:
        """
        查询 30 天内同一城市同类型信号的历史表现。

        Returns:
            (should_reject: bool, win_rate: float, sample_count: int)
            should_reject=True 表示历史胜率过低，应拒绝开仓。
        """
        if not cfg.SIGNAL_HISTORY_ENABLED or not self.db:
            return (False, 0.0, 0)
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg.SIGNAL_HISTORY_DAYS))
            # 从 signal_history 表中查询
            rows = self.db.conn.execute("""
                SELECT actual_result, pnl FROM signal_history
                WHERE city=? AND signal_type=?
                AND actual_result IS NOT NULL
                AND created_at >= ?
                ORDER BY created_at DESC
            """, (city, signal_type, cutoff.isoformat())).fetchall()

            if not rows:
                return (False, 0.0, 0)

            wins = sum(1 for r in rows if r["actual_result"] == 1)
            total = len(rows)
            win_rate = wins / total if total > 0 else 0.0

            should_reject = win_rate < cfg.SIGNAL_HISTORY_MIN_WR
            if should_reject:
                logger.warning(f"  ⛔ 信号历史过滤 [{city}] [{signal_type}]: "
                              f"WR={win_rate:.0%} ({wins}/{total}) < "
                              f"{cfg.SIGNAL_HISTORY_MIN_WR:.0%} → 拒绝")
            else:
                logger.debug(f"  ✅ 信号历史通过 [{city}] [{signal_type}]: "
                            f"WR={win_rate:.0%} ({wins}/{total})")
            return (should_reject, win_rate, total)
        except Exception as e:
            logger.debug(f"信号历史查询失败: {e}")
            return (False, 0.0, 0)

    def record_signal_history(self, position: Position, signal_type: str = "MODEL"):
        """
        结算后将信号记录写入 signal_history 表，包含策略标签。

        字段: city, bucket_label, signal_type, strategy, p_model, entry_price,
              exit_price, expected_result, actual_result, pnl
        """
        if not cfg.SIGNAL_HISTORY_ENABLED or not self.db:
            return
        if not position._settled:
            return
        try:
            expected_result = 1 if position.realized > 0 else 0
            actual_result = expected_result

            p_model = None
            try:
                if hasattr(position, '_p_model'):
                    p_model = position._p_model
            except Exception:
                pass

            strategy = getattr(position, '_strategy', 'model_edge')

            self.db.conn.execute("""
                INSERT INTO signal_history
                (city, bucket_label, signal_type, strategy, p_model, p_market,
                 entry_price, exit_price, expected_result, actual_result,
                 pnl, side, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (
                position.city,
                position.bucket_label,
                signal_type,
                strategy,
                p_model,
                position.entry_no,
                position.entry_no,
                position.curr_no,
                expected_result,
                actual_result,
                round(position.realized, 2),
                position.side,
            ))
            self.db.conn.commit()
            logger.debug(f"  📝 信号历史已记录 [{position.city}] [{position.bucket_label}] "
                        f"type={signal_type} strategy={strategy} "
                        f"result={'WIN' if actual_result else 'LOSS'} "
                        f"pnl=${position.realized:.2f}")
        except Exception as e:
            logger.warning(f"记录信号历史失败: {e}")

    @staticmethod
    def classify_strategy(signal_type: str, is_ladder: bool,
                          is_extreme: bool = False,
                          is_near_lock: bool = False,
                          city: str = "") -> str:
        """
        将信号类型映射为策略归因标签：
          - ladder: 温度阶梯衍生信号
          - extreme_coldmath: METAR 极端低估 + 赔率密集计算
          - near_lock: 接近结算的锁利信号
          - model_edge: 标准模型边缘套利
          - hko_confirm: 香港天文台数据确认的保守信号
        """
        if is_ladder:
            return "ladder"
        if is_extreme:
            return "extreme_coldmath"
        if is_near_lock:
            return "near_lock"
        if city.lower() in ("hong kong",):
            return "hko_confirm"
        return "model_edge"

    def open_position(self, mid: str, city: str, dt: str, label: str,
                      entry_no: float, size: float, end_date: str = "",
                      side: str = "NO", signal_type: str = "MODEL",
                      strategy: Optional[str] = None):
        # Bug 1: 已在持倉或已平倉 → 拒開
        if self.has_position(mid):
            logger.warning(f"  ⛔ 防重複: {city} {label} (market_id 已存在)")
            return None
        # Bug 2: 冷卻期 → 拒開
        if self.is_on_cooldown(end_date):
            logger.info(f"  ⏳ 冷卻: {city} {label} (距結算 < 60 分鐘)")
            return None
        # Bug 3: 信号历史胜率过低 → 拒開
        should_reject, hist_wr, hist_n = self.check_signal_history(city, label, signal_type)
        if should_reject:
            logger.info(f"  ⛔ 信号历史拒绝 [{city}] [{label}] {signal_type}: WR={hist_wr:.0%} ({hist_n} samples)")
            return None
        p = Position(mid, city, dt, label, entry_no, size, end_date, side)
        p._p_model = None  # will be set by caller if available
        p._signal_type = signal_type
        # ── 策略归因标签 ──
        if strategy is None:
            is_ladder = False
            is_extreme = (signal_type == "METAR")
            is_near_lock = False
            strategy = Engine.classify_strategy(signal_type, is_ladder, is_extreme, is_near_lock, city)
        p._strategy = strategy
        self.positions.append(p)
        self.total += 1
        self.capital -= size
        self.capital_history.append((datetime.now(timezone.utc).isoformat(), self.capital))
        side_icon = "🔵" if side == "YES" else "📦"
        logger.info(f"{side_icon} 開倉 [{city} {label}] {side}@{entry_no:.4f} ${size:.0f} "
                    f"type={signal_type} strategy={strategy}")
        # ── 持久化到 TradeDB ──
        self._persist_open(p)
        return p

    def _persist_open(self, p: Position):
        """将开仓记录写入 TradeDB，包含策略标签"""
        if not self.db:
            return
        try:
            bucket_lower, bucket_upper = 0.0, 0.0
            for lo, hi, lbl in cfg.buckets:
                if lbl == p.bucket_label:
                    bucket_lower, bucket_upper = lo, hi
                    break
            strategy = getattr(p, '_strategy', 'model_edge')
            trade_id = self.db.open_trade(
                token_id=p.market_id,
                city=p.city,
                bucket_lower=bucket_lower,
                bucket_upper=bucket_upper,
                side=p.side,
                entry_price=p.entry_no,
                size=p.size,
                strategy=strategy,
            )
            if trade_id is not None:
                p._db_id = trade_id
        except Exception as e:
            logger.warning(f"⚠️ 持久化开仓记录失败: {e}")

    def _persist_close(self, p: Position):
        """将平仓记录写入 TradeDB"""
        if not self.db:
            return
        db_id = getattr(p, '_db_id', None)
        if db_id is None:
            return
        try:
            self.db.close_trade(
                trade_id=db_id,
                exit_price=p.curr_no,
                exit_reason=p.exit_reason or '',
            )
        except Exception as e:
            logger.warning(f"⚠️ 持久化平仓记录失败: {e}")

    def update_all(self, no_pmap: dict[str, float], yes_pmap: dict[str, float]):
        """no_pmap: market_id → no_price, yes_pmap: market_id → yes_price"""
        for p in self.positions:
            if not p.is_open:
                continue
            price = None
            if p.side == "NO":
                price = no_pmap.get(p.market_id)
            else:
                price = yes_pmap.get(p.market_id)
            if price is not None:
                p.update(price)

    def check_exit(self) -> int:
        """
        增強出場規則 (按優先級):
          1. 快速止盈 +5%
          2. 固定止盈 +9%
          3. 移動止盈 (浮盈 5% 後啟動, 回撤 3% 平倉)
          4. 目標止盈 NO ≥ 0.98
          5. 止損 -6.5%
          6. 時間止損 24h
          7. 強制平倉 (距結算 < N 小時且有利潤)
        """
        now = datetime.now(timezone.utc)
        n = 0

        for p in list(self.positions):
            if not p.is_open: continue

            # ── 1. 快速止盈 +5% (最高優先, 保本第一) ──
            quick_profit = p.entry_no * (1 + cfg.QUICK_PROFIT_PCT)
            if p.curr_no >= quick_profit:
                prof = p.size * cfg.QUICK_PROFIT_PCT
                p.close(f"快盈+{cfg.QUICK_PROFIT_PCT*100:.0f}% ${p.entry_no:.3f}→${p.curr_no:.3f}", prof)
                self._settle(p, prof)
                n += 1
                continue

            # ── 2. 固定止盈 +9% ──
            fixed_target = p.entry_no * (1 + cfg.FIXED_TAKE_PROFIT_PCT)
            if p.curr_no >= fixed_target:
                prof = p.size * (p.curr_no / p.entry_no - 1)
                p.close(f"固定止盈+{cfg.FIXED_TAKE_PROFIT_PCT*100:.0f}% ${p.entry_no:.3f}→${p.curr_no:.3f}", prof)
                self._settle(p, prof)
                n += 1
                continue

            # ── 3. 移動止盈 (浮盈 ≥5% 後啟動, 從峰頂回撤 3% 平倉) ──
            activate_threshold = p.entry_no * (1 + cfg.TRAILING_ACTIVATE_PCT)
            if p.peak_no >= activate_threshold:
                retrace_price = p.peak_no * (1 - cfg.TRAILING_RETRACE_PCT)
                if p.curr_no <= retrace_price:
                    prof = p.size * (p.curr_no / p.entry_no - 1)
                    p.close(f"移動止盈 峰{round(p.peak_no,4)}回撤{cfg.TRAILING_RETRACE_PCT*100:.0f}%→{round(p.curr_no,4)}", prof)
                    self._settle(p, prof)
                    n += 1
                    continue

            # ── 4. 目標止盈 NO ≥ 0.98 ──
            if p.curr_no >= cfg.NO_EXIT_TARGET:
                prof = p.size * (p.curr_no / p.entry_no - 1)
                p.close(f"NO≥{cfg.NO_EXIT_TARGET:.2f} ${p.entry_no:.3f}→${p.curr_no:.3f}", prof)
                self._settle(p, prof)
                n += 1
                continue

            # ── 5. 止損 -6.5% (entry 跌 6.5%) ──
            stop_price = p.entry_no * (1 - cfg.STOP_LOSS_PCT)
            if p.curr_no <= stop_price:
                loss = p.size * (p.curr_no / p.entry_no - 1)
                p.close(f"止損-{cfg.STOP_LOSS_PCT*100:.1f}% ${p.entry_no:.3f}→${p.curr_no:.3f}", loss)
                self._settle(p, loss)
                n += 1
                continue

            # ── 6. 時間止損 24h ──
            if p.entry_time:
                try:
                    entry_dt = datetime.fromisoformat(p.entry_time)
                    age_hours = (now - entry_dt).total_seconds() / 3600
                except Exception:
                    age_hours = 0
                if age_hours >= cfg.TIME_STOP_HOURS:
                    prof = p.size * (p.curr_no / p.entry_no - 1)
                    p.close(f"時間止損 {cfg.TIME_STOP_HOURS}h到 NO={p.curr_no:.4f}", prof)
                    self._settle(p, prof)
                    n += 1
                    continue

            # ── 7. 強制平倉（距結算 < N 小時且有利潤） ──
            if p.end_date:
                try:
                    ed = datetime.fromisoformat(p.end_date.replace("Z", "+00:00"))
                except Exception:
                    continue
                rem = (ed - now).total_seconds() / 3600
                if rem <= 0:
                    # 已過結算
                    prof = p.size * (p.curr_no / p.entry_no - 1)
                    p.close(f"結算時間到 NO=${p.curr_no:.3f}", prof)
                    self._settle(p, prof)
                    n += 1
                elif rem <= cfg.FORCE_EXIT_HOURS:
                    prof_pct = (p.curr_no - p.entry_no) / p.entry_no
                    if prof_pct >= cfg.FORCE_EXIT_MIN_PROFIT:
                        prof = p.size * prof_pct
                        p.close(f"距結算{rem:.1f}h強平 +{prof_pct*100:.0f}%", prof)
                        self._settle(p, prof)
                        n += 1

        return n

    def check_settled(self, minfo: dict[str, dict]):
        """minfo: market_id → {yes, no, closed}"""
        for p in list(self.positions):
            if not p.is_open: continue
            info = minfo.get(p.market_id)
            if not info: continue
            yes, no_, closed = info["yes"], info["no"], info.get("closed", False)

            current_price = no_ if p.side == "NO" else yes

            if closed or yes >= 0.99 or no_ >= 0.99:
                if p.side == "NO" and no_ >= 0.99:
                    prof = p.size * (no_ / p.entry_no - 1)
                    p.close(f"結算 NO贏 +{prof/p.size*100:.0f}%", prof)
                elif p.side == "YES" and yes >= 0.99:
                    prof = p.size * (yes / p.entry_no - 1)
                    p.close(f"結算 YES贏 +{prof/p.size*100:.0f}%", prof)
                elif p.side == "NO" and yes >= 0.99:
                    p.close("結算 YES贏 -100%", -p.size)
                elif p.side == "YES" and no_ >= 0.99:
                    p.close("結算 NO贏 -100%", -p.size)
                else:
                    prof = p.size * (current_price / p.entry_no - 1) if p.entry_no > 0 else 0
                    p.close(f"結算 {p.side}={current_price:.3f}", prof)
                self._settle(p, p.realized)
            else:
                p.update(current_price)

    def backtest_signals(self, signal_type: Optional[str] = None) -> dict:
        """
        随时验证各信号类型的真实表现。

        从 signal_history 表中汇总统计:
          - 各信号类型的总交易数、胜率、平均盈亏
          - 可单独查询某类信号

        Returns:
            dict[signal_type] = {
                total, wins, losses, win_rate, avg_pnl, total_pnl
            }
        """
        if not self.db:
            return {}
        try:
            if signal_type:
                rows = self.db.conn.execute("""
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
                rows = self.db.conn.execute("""
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
                wins = r["wins"]
                result[st] = {
                    "total": total,
                    "wins": wins,
                    "losses": total - wins,
                    "win_rate": round(wins / total, 4) if total > 0 else 0,
                    "avg_pnl": round(r["avg_pnl"], 2),
                    "total_pnl": round(r["total_pnl"], 2),
                }
                logger.info(f"  📊 信号回测 [{st}]: {total}笔 WR={result[st]['win_rate']:.1%} "
                           f"avg=${result[st]['avg_pnl']:.2f} total=${result[st]['total_pnl']:.2f}")
            return result
        except Exception as e:
            logger.warning(f"信号回测失败: {e}")
            return {}

    def _settle(self, p: Position, pnl: float):
        if pnl > 0: self.wins += 1
        else: self.losses += 1
        self.daily_pnl += pnl
        self.closed.append(p)
        # 資金回收
        self.capital += p.size + pnl
        self.capital_history.append((datetime.now(timezone.utc).isoformat(), self.capital))
        p._settled = True
        # ── 持久化平仓到 TradeDB ──
        self._persist_close(p)
        # ── 记录信号历史 (self-learning loop) ──
        signal_type = getattr(p, '_signal_type', 'MODEL')
        strategy = getattr(p, '_strategy', 'model_edge')
        logger.info(f"  ✅ 平倉 [{p.city} {p.bucket_label}] strategy={strategy} P&L=${pnl:.2f} | {p.exit_reason}")
        self.record_signal_history(p, signal_type)

    # ── v7: 自适应宽度 ──
    @staticmethod
    def calc_adaptive_spread(p_market: float) -> int:
        """
        根据市场定价动态调整阶梯跨度：
        - p_market < THRESH_LOW (低估): 缩小跨度，聚焦核心
        - p_market > THRESH_HIGH (高估): 扩大跨度，捕获相邻机会
        - 居中: 使用默认跨度 LADDER_SPREAD
        """
        if not cfg.ADAPTIVE_WIDTH_ENABLED:
            return cfg.LADDER_SPREAD
        if p_market < cfg.ADAPTIVE_WIDTH_THRESH_LOW:
            spread = cfg.ADAPTIVE_WIDTH_MIN_SPREAD
        elif p_market > cfg.ADAPTIVE_WIDTH_THRESH_HIGH:
            spread = cfg.ADAPTIVE_WIDTH_MAX_SPREAD
        else:
            spread = cfg.LADDER_SPREAD
        if spread != cfg.LADDER_SPREAD:
            logger.debug(f"  📐 自适应宽度: p_market={p_market:.2f} → spread={spread} (原={cfg.LADDER_SPREAD})")
        return spread

    # ── v7: 动态预算 ──
    def calc_dynamic_budget(self, city: str) -> float:
        """
        基于历史胜率和盈亏比动态调整每个城市的预算。
        胜率+盈亏比越高，预算越高；新城市或表现差的城市用最低预算。
        返回 $0.20-$1.00 之间的预算值。
        """
        if not cfg.DYNAMIC_BUDGET_ENABLED:
            return cfg.POS_MIN

        # 统计该城市历史交易
        city_closed = [p for p in self.closed if p.city == city]
        if not city_closed:
            return cfg.DYNAMIC_BUDGET_MIN

        wins = sum(1 for p in city_closed if p.realized > 0)
        losses = sum(1 for p in city_closed if p.realized <= 0)
        total = wins + losses
        if total == 0:
            return cfg.DYNAMIC_BUDGET_MIN

        win_rate = wins / total

        # 计算盈亏比 (avg_win / avg_loss)
        avg_win = sum(p.realized for p in city_closed if p.realized > 0) / max(wins, 1)
        avg_loss = abs(sum(p.realized for p in city_closed if p.realized < 0)) / max(losses, 1)
        profit_ratio = avg_win / max(avg_loss, 0.01)

        # 综合评分 = 胜率 * 0.5 + 盈亏比/(盈亏比+1) * 0.5
        score = win_rate * 0.5 + (profit_ratio / (profit_ratio + 1)) * 0.5

        # 映射到预算范围
        budget = cfg.DYNAMIC_BUDGET_MIN + (cfg.DYNAMIC_BUDGET_MAX - cfg.DYNAMIC_BUDGET_MIN) * score
        budget = max(cfg.DYNAMIC_BUDGET_MIN, min(cfg.DYNAMIC_BUDGET_MAX, budget))

        logger.debug(f"  💰 动态预算 [{city}]: WR={win_rate:.0%} PR={profit_ratio:.2f} score={score:.2f} → ${budget:.2f} ({total}笔)")
        return round(budget, 2)

    # ── v7: 多城市联合评分 ──
    @staticmethod
    def rank_cities_by_ev(analyses: list[dict]) -> list[dict]:
        """
        对所有城市的分析结果按预期价值排序，返回 TOP N 城市。
        评分 = |diff| * (1 - p_market) — 差值越大、价格越低，价值越高。
        """
        if not cfg.MULTI_CITY_ENABLED or not analyses:
            return analyses

        # 聚合每个城市的最高评分
        city_scores: dict[str, float] = {}
        city_best_analysis: dict[str, dict] = {}
        for a in analyses:
            city = a.get("city", "")
            if not city:
                continue
            diff = abs(a.get("diff", 0))
            p_market = a.get("p_market", 0.5)
            # EV 评分 = |diff| * (1 - p_market)  差值越大且价格越低越好
            ev_score = diff * (1 - p_market)
            if city not in city_scores or ev_score > city_scores[city]:
                city_scores[city] = ev_score
                city_best_analysis[city] = a

        # 按 EV 评分降序排列
        sorted_cities = sorted(city_scores.items(), key=lambda x: x[1], reverse=True)
        top_n = min(cfg.MULTI_CITY_TOP_N, len(sorted_cities))
        top_cities = {c for c, _ in sorted_cities[:top_n]}

        filtered = [a for a in analyses if a.get("city", "") in top_cities]
        skipped = len(analyses) - len(filtered)
        if skipped:
            city_list = [f"{c}({s:.3f})" for c, s in sorted_cities[:top_n]]
            logger.info(f"  🏙️ 多城市TOP{top_n}: {', '.join(city_list)} (过滤掉{skipped}个次优信号)")

        return filtered

    # ── v7: 逐层止盈 ──
    def check_layered_tp(self) -> int:
        """
        边缘桶在结算前 LAYERED_TP_HOURS_BEFORE 小时且浮盈 > LAYERED_TP_PROFIT_PCT 时提前平仓。
        只在阶梯衍生桶上执行，主桶不受影响。
        阶梯桶的特征：仓位大小小于主桶标准（LADDER_SIZE_PCT * 标准仓），且 bucket_label 属于 cfg.buckets。
        """
        if not cfg.LAYERED_TP_ENABLED:
            return 0

        now = datetime.now(timezone.utc)
        n = 0

        for p in list(self.positions):
            if not p.is_open:
                continue

            # 判断是否为阶梯桶：仓位大小明显小于 cfg.POS_MIN（阶梯桶仓位 = base * LADDER_SIZE_PCT）
            # 由于阶梯桶仓位可能为 0.5-50，主桶至少为 cfg.POS_MIN
            standard_min = max(cfg.POS_MIN, 0.5)
            is_edge_bucket = p.size < standard_min * 0.8 or (
                p.size <= cfg.POS_MIN * cfg.LADDER_SIZE_PCT + 0.1
            )

            if not is_edge_bucket:
                continue

            if not p.end_date:
                continue

            try:
                ed = datetime.fromisoformat(p.end_date.replace("Z", "+00:00"))
            except Exception:
                continue

            # 结算前 LAYERED_TP_HOURS_BEFORE 小时内
            hours_left = (ed - now).total_seconds() / 3600
            if hours_left > cfg.LAYERED_TP_HOURS_BEFORE or hours_left <= 0:
                continue

            # 浮盈 > LAYERED_TP_PROFIT_PCT
            if p.pct < cfg.LAYERED_TP_PROFIT_PCT:
                continue

            # 锁定利润
            prof = p.size * p.pct
            p.close(f"逐层止盈 +{p.pct*100:.1f}% 距结算{hours_left:.1f}h", prof)
            self._settle(p, prof)
            n += 1
            logger.info(f"  🧱 逐层止盈 [{p.city} {p.bucket_label}] 边缘桶 +{p.pct*100:.1f}% ${prof:.2f}")

        if n:
            logger.info(f"  🧱 逐层止盈: 提前平仓 {n} 笔边缘桶")
        return n

    @staticmethod
    def calc_theta_multiplier(end_date_str: str) -> float:
        """
        根據距離結算天數計算 Theta 懲罰倍數。
        距結算越近 edge 門檻越高，防止臨近結算時被波動收割。
        """
        if not end_date_str:
            return 1.0
        try:
            ed = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            days_left = (ed - datetime.now(timezone.utc)).total_seconds() / 86400
            if days_left >= 7:
                return 1.0       # ≥7 天: 無懲罰
            elif days_left >= 3:
                return 1.2       # 3-7 天: +20%
            elif days_left >= 1:
                return 1.5       # 1-3 天: +50%
            elif days_left > 0:
                return 2.0       # <1 天: 翻倍
            else:
                return 3.0       # 已過期: 三倍
        except Exception:
            return 1.0

    @staticmethod
    def in_trading_window() -> bool:
        """UTC 時間窗口檢查 (預設 12-20 UTC)"""
        now_hour = datetime.now(timezone.utc).hour
        return cfg.TRADE_START_HOUR <= now_hour < cfg.TRADE_END_HOUR

    def should_pause(self) -> bool:
        if sum(1 for p in self.positions if p.is_open) >= cfg.MAX_CONCURRENT:
            return True
        if date.today() != self.today:
            self.today = date.today(); self.daily_pnl = 0.0
        return self.daily_pnl <= -cfg.daily_loss_limit

    def reset_state(self):
        """重置所有模擬數據，資金回到初始值"""
        self.positions.clear()
        self.closed.clear()
        self.capital = cfg.INITIAL_CAPITAL
        self.total = 0
        self.wins = 0
        self.losses = 0
        self.daily_pnl = 0.0
        self.today = date.today()
        self.capital_history = [(datetime.now(timezone.utc).isoformat(), cfg.INITIAL_CAPITAL)]
        # 重新连接 DB（可在不重启 bot 的情况下切换数据库）
        try:
            if _HAS_TRADE_DB:
                self.db = TradeDB(cfg.DB_PATH)
                logger.info(f"🗄️ TradeDB 已重新连接: {cfg.DB_PATH}")
        except Exception as e:
            logger.warning(f"⚠️ TradeDB 重连失败: {e}")
            self.db = None
        logger.info("🧹 模擬數據已重置，資金回到 ${:.0f}".format(cfg.INITIAL_CAPITAL))

    def summary(self) -> dict:
        ops = [p for p in self.positions if p.is_open]
        wr = round(self.wins / max(self.total, 1) * 100, 1)
        total_pnl = round(self.capital - cfg.INITIAL_CAPITAL, 2)
        # @to010: side breakdown
        no_count = sum(1 for p in self.closed if p.side == "NO")
        yes_count = sum(1 for p in self.closed if p.side == "YES")
        no_open = sum(1 for p in ops if p.side == "NO")
        yes_open = sum(1 for p in ops if p.side == "YES")

        # v7: 动态预算信息（按城市）
        city_budgets = {}
        if cfg.DYNAMIC_BUDGET_ENABLED:
            unique_cities = set(p.city for p in self.closed + self.positions if p.city)
            for c in sorted(unique_cities):
                city_budgets[c] = self.calc_dynamic_budget(c)

        # v7: 逐层止盈统计
        layered_tp_count = sum(1 for p in self.closed if "逐层止盈" in (p.exit_reason or ""))

        return {
            "total": self.total, "wins": self.wins, "losses": self.losses,
            "win_rate": wr, "daily_pnl": round(self.daily_pnl, 2),
            "total_pnl": total_pnl,
            "capital": round(self.capital, 2),
            "initial_capital": cfg.INITIAL_CAPITAL,
            "open_count": len(ops), "closed_count": len(self.closed),
            "no_trades": no_count + no_open,
            "yes_trades": yes_count + yes_open,
            "open": [p.to_dict() for p in ops],
            "recent_closed": [p.to_dict() for p in self.closed[-50:]],
            "capital_history": self.capital_history[-100:],
            # v7 扩展字段
            "city_budgets": city_budgets,
            "layered_tp_count": layered_tp_count,
            "features": {
                "adaptive_width": cfg.ADAPTIVE_WIDTH_ENABLED,
                "multi_city": cfg.MULTI_CITY_ENABLED,
                "dynamic_budget": cfg.DYNAMIC_BUDGET_ENABLED,
                "layered_tp": cfg.LAYERED_TP_ENABLED,
            },
        }


# ═══════════════════════════════════════════════════════════════════════
# HTTP 儀表板
# ═══════════════════════════════════════════════════════════════════════

_engine: Optional[Engine] = None
_weather: Optional[WeatherClient] = None
_latest_signals: list[dict] = []
_latest_analyses: list[dict] = []
_last_scan = ""


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            self._json(_engine.summary() if _engine else {})
        elif self.path == "/api/signals":
            self._json({"signals": _latest_signals, "scan_time": _last_scan})
        elif self.path == "/api/analyses":
            self._json({"analyses": _latest_analyses[-100:]})
        elif self.path == "/api/positions":
            s = _engine.summary() if _engine else {}
            self._json({"open": s.get("open", []), "closed": s.get("recent_closed", [])})
        elif self.path == "/api/reload":
            reload_config()
            self._json({"status": "ok", "message": "配置已熱更新"})
        elif self.path == "/api/capital":
            s = _engine.summary() if _engine else {}
            self._json({
                "initial": s.get("initial_capital", 0),
                "current": s.get("capital", 0),
                "total_pnl": s.get("total_pnl", 0),
                "daily_pnl": s.get("daily_pnl", 0),
                "history": s.get("capital_history", []),
            })
        elif self.path.startswith("/api/trades"):
            self._handle_trades()
        elif self.path in ("/", "/dashboard"):
            self._html()
        else:
            self.send_error(404)

    def _handle_trades(self):
        """
        GET /api/trades?limit=100
        从 TradeDB 读取持久化的历史交易记录
        """
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        limit = int(params.get("limit", ["100"])[0])
        limit = max(1, min(limit, 500))

        trades_data = {"trades": [], "open_trades": [], "total_closed": 0}

        if _engine and _engine.db:
            try:
                closed_trades = _engine.db.get_recent_trades(limit=limit)
                open_trades = _engine.db.get_open_trades()
                # 格式化时间戳
                for t in closed_trades:
                    if isinstance(t.get("entry_time"), str):
                        t["entry_time"] = t["entry_time"][:19]
                    if isinstance(t.get("exit_time"), str):
                        t["exit_time"] = t["exit_time"][:19]
                    if isinstance(t.get("created_at"), str):
                        t["created_at"] = t["created_at"][:19]
                for t in open_trades:
                    if isinstance(t.get("entry_time"), str):
                        t["entry_time"] = t["entry_time"][:19]
                    if isinstance(t.get("created_at"), str):
                        t["created_at"] = t["created_at"][:19]
                trades_data["trades"] = closed_trades
                trades_data["open_trades"] = open_trades
                trades_data["total_closed"] = len(closed_trades)
            except Exception as e:
                logger.warning(f"读取 TradeDB 失败: {e}")
                trades_data["error"] = str(e)
        else:
            trades_data["error"] = "TradeDB 未初始化"

        self._json(trades_data)

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode())

    def _html(self):
        path = os.path.join(os.path.dirname(__file__), '../../tools/hightemptation_live/dashboard.html')
        try:
            with open(path, 'r', encoding='utf-8') as f:
                html = f.read()
        except Exception as e:
            logger.warning(f"读取 dashboard.html 失败: {e}")
            html = '<html><body><h1>Dashboard file not found</h1></body></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, *a): pass


def start_dashboard():
    try:
        s = HTTPServer((cfg.DASHBOARD_HOST, cfg.DASHBOARD_PORT), DashboardHandler)
        t = threading.Thread(target=s.serve_forever, daemon=True)
        t.start()
        logger.info(f"🌐 儀表板 http://{cfg.DASHBOARD_HOST}:{cfg.DASHBOARD_PORT}")
    except Exception as e:
        logger.warning(f"儀表板失敗: {e}")


# ═══════════════════════════════════════════════════════════════════════
# 主循環
# ═══════════════════════════════════════════════════════════════════════

async def _check_single_obi(http: httpx.AsyncClient, sig: dict) -> Optional[bool]:
    """
    檢查單個信號的 OBI (Order Book Imbalance)。
    返回 True=通過, False=阻擋, None=無法判斷(不阻擋)。
    """
    if not cfg.OBI_ENABLED:
        return True
    token_id = sig.get("no_token_id", "")
    if not token_id:
        return None
    try:
        r = await http.get(f"https://clob.polymarket.com/book/{token_id}", timeout=10)
        r.raise_for_status()
        book = r.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        bid_vol = sum(float(b.get("size", 0)) for b in bids[:10])
        ask_vol = sum(float(a.get("size", 0)) for a in asks[:10])
        total = bid_vol + ask_vol
        if total == 0:
            return None
        obi = (bid_vol - ask_vol) / total
        cid_short = sig.get("market_id", "")[:8]
        logger.debug(f"  📊 OBI {sig['city']} {cid_short}: {obi:.2f} (b={bid_vol:.0f} a={ask_vol:.0f})")
        return obi >= cfg.OBI_MIN_IMBALANCE
    except Exception as e:
        logger.debug(f"OBI 失敗 {sig.get('market_id','')[:8]}: {e}")
        return None


async def main():
    global _engine, _weather, _latest_signals, _latest_analyses, _last_scan

    logger.info("=" * 60)
    logger.info("🌡️ HighTempTation Bot 啟動")
    logger.info(f"  DRY_RUN={cfg.DRY_RUN} | 間隔={cfg.SCAN_INTERVAL_SEC}s | 資金=${cfg.INITIAL_CAPITAL}")
    logger.info(f"  門檻: P_mkt-P_model ≥ {cfg.CALIB_THRESH*100:.0f}% | P_mkt ∈ [{cfg.MIN_YES*100:.0f}¢,{cfg.MAX_YES*100:.0f}¢]")
    logger.info(f"  倉位: ${cfg.POS_MIN:.0f}-${cfg.POS_MAX:.0f} | "
                f"止盈: 快盈+{cfg.QUICK_PROFIT_PCT*100:.0f}% 固定+{cfg.FIXED_TAKE_PROFIT_PCT*100:.0f}% 移動激活+{cfg.TRAILING_ACTIVATE_PCT*100:.0f}%回撤{cfg.TRAILING_RETRACE_PCT*100:.0f}% | "
                f"止損: 硬-{cfg.STOP_LOSS_PCT*100:.1f}% 時間{cfg.TIME_STOP_HOURS}h | "
                f"NO≥{cfg.NO_EXIT_TARGET:.2f}")
    logger.info(f"  Theta懲罰={'ON' if cfg.THETA_ENABLED else 'OFF'} | "
                f"OBI過濾={'ON' if cfg.OBI_ENABLED else 'OFF'} 閾值{cfg.OBI_MIN_IMBALANCE:.1f} | "
                f"交易窗口 UTC {cfg.TRADE_START_HOUR}-{cfg.TRADE_END_HOUR}")
    logger.info(f"  🪜溫度階梯={'ON' if cfg.LADDER_ENABLED else 'OFF'} 擴散{cfg.LADDER_SPREAD}桶 edge乘{cfg.LADDER_EDGE_BOOST:.1f} 倉位{cfg.LADDER_SIZE_PCT*100:.0f}%")
    if cfg.ENSEMBLE_ENABLED:
        logger.info(f"  🌐3-集成預報={'ON' if cfg.ENSEMBLE_ENABLED else 'OFF'} 模型={cfg.ENSEMBLE_MODELS} 權重={cfg.ENSEMBLE_WEIGHTS}")
    else:
        logger.info(f"  🌐3-集成預報=OFF")
    ens40_mode_name = {"hybrid": "混合(40优先+3兜底)", "ensemble_only": "仅40成员", "deterministic_only": "仅直接"}
    logger.info(f"  🌪️40-Ensemble={'ON' if cfg.ENSEMBLE_40_ENABLED else 'OFF'} "
                f"模型={cfg.ENSEMBLE_40_MODEL} 模式={ens40_mode_name.get(cfg.ENSEMBLE_40_MODE, cfg.ENSEMBLE_40_MODE)} "
                f"edge={cfg.ENSEMBLE_40_EDGE:.1%} 端点={cfg.ENSEMBLE_API}")
    if cfg.ENABLE_MULTI_MODEL:
        logger.info(f"  🌐 多模型聚合={'ON' if cfg.ENABLE_MULTI_MODEL else 'OFF'} "
                    f"模型={cfg.MULTI_MODEL_MODELS} "
                    f"同意阈>{cfg.MULTI_MODEL_AGREEMENT_THRESH:.0%} 离散上限{cfg.MULTI_MODEL_SPREAD_CAP:.0f}°C")
    else:
        logger.info(f"  🌐 多模型聚合=OFF")
    # 🌡️ METAR 实时观测
    logger.info(f"  🌡️ METAR={'ON' if cfg.ENABLE_METAR else 'OFF'} "
                f"极端低估<{cfg.EXTREME_BUY_THRESH:.0%} 偏离>{cfg.METAR_DEVIATION_THRESH:.0f}°C "
                f"覆盖预报={'ON' if cfg.METAR_OVERRIDE_ENABLED else 'OFF'}")
    # 🎯 Fractional Kelly 仓位优化
    logger.info(f"  🎯 Kelly仓位={'ON' if cfg.KELLY_ENABLED else 'OFF'} "
                f"METAR×{cfg.KELLY_FRACTION_METAR:.0%} MODEL×{cfg.KELLY_FRACTION_MODEL:.0%} "
                f"LADDER×{cfg.KELLY_FRACTION_LADDER:.0%} 单笔≤{cfg.KELLY_MAX_RISK_PCT:.0%}资本")
    # 📊 信号历史验证闭环
    logger.info(f"  📊 信号历史={'ON' if cfg.SIGNAL_HISTORY_ENABLED else 'OFF'} "
                f"最低胜率>{cfg.SIGNAL_HISTORY_MIN_WR:.0%} 窗口{cfg.SIGNAL_HISTORY_DAYS}d")
    # v7 新功能状态
    logger.info(f"  🎯 v7自适应宽度={'ON' if cfg.ADAPTIVE_WIDTH_ENABLED else 'OFF'} "
                f"(低估<{cfg.ADAPTIVE_WIDTH_THRESH_LOW:.0%}→缩小{cfg.ADAPTIVE_WIDTH_MIN_SPREAD}, "
                f"高估>{cfg.ADAPTIVE_WIDTH_THRESH_HIGH:.0%}→扩大{cfg.ADAPTIVE_WIDTH_MAX_SPREAD})")
    logger.info(f"  🏙️ v7多城市联合={'ON' if cfg.MULTI_CITY_ENABLED else 'OFF'} TOP{cfg.MULTI_CITY_TOP_N}EV评分")
    logger.info(f"  💰 v7动态预算={'ON' if cfg.DYNAMIC_BUDGET_ENABLED else 'OFF'} "
                f"${cfg.DYNAMIC_BUDGET_MIN:.2f}-${cfg.DYNAMIC_BUDGET_MAX:.2f} (基于历史WR+PR)")
    logger.info(f"  🧱 v7逐层止盈={'ON' if cfg.LAYERED_TP_ENABLED else 'OFF'} "
                f"浮盈>{cfg.LAYERED_TP_PROFIT_PCT:.0%} 结算前{cfg.LAYERED_TP_HOURS_BEFORE:.0f}h边缘桶提前平仓")

    # 全球模式信息
    if cfg.ALLOWED_CITIES:
        logger.info(f"  🎯 城市白名单: {cfg.ALLOWED_CITIES} | 雙向={cfg.ALLOWED_SIDES} | MIN_EDGE={cfg.CALIB_THRESH*100:.0f}%")
    else:
        logger.info(f"  🌍 全球模式: {len(STATION_IDX)} 個已知站點 + 動態 geocode | 雙向={cfg.ALLOWED_SIDES} | MIN_EDGE={cfg.CALIB_THRESH*100:.0f}% | 每倉 ${cfg.POS_MIN:.0f}")
    logger.info("=" * 60)

    _engine = Engine(db_path=cfg.DB_PATH)
    _weather = WeatherClient()
    http = httpx.AsyncClient(timeout=30.0)

    start_dashboard()

    # 初始偏差更新（只對已知站點）
    if cfg.BIAS_ENABLED:
        logger.info("📊 更新偏差校正（已知站點）...")
        for city in STATION_IDX:
            lat, lon = station_coords(city)
            await _weather.update_bias_from_archive(city, lat, lon, cfg.BIAS_DAYS)

    running = True
    signal.signal(signal.SIGTERM, lambda *a: setattr(sys.modules[__name__], 'running', False))
    signal.signal(signal.SIGINT, lambda *a: setattr(sys.modules[__name__], 'running', False))
    # SIGHUP → 熱更新配置，不重啟進程
    signal.signal(signal.SIGHUP, lambda *a: reload_config())

    try:
        while running:
            loop_start = time.time()
            logger.info(f"\n🔄 掃描 {datetime.now(timezone.utc).isoformat()}")

            # ── 2. 全面掃描所有活躍天氣市場 ──
            markets = await scan_all_weather_markets(http)
            logger.info(f"🌡️ {len(markets)} 個活躍天氣市場（全量）")

            # ── 1. 動態解析市場中的城市，獲取預報 ──
            # 從所有市場中提取所有唯一城市
            seen_cities: set[str] = set()
            market_cities: dict[str, tuple[float, float]] = {}
            for m in markets:
                parsed_q = parse_market_question(m["question"])
                if parsed_q:
                    c = parsed_q["city"]
                    if c not in seen_cities:
                        seen_cities.add(c)
                        if c in STATION_IDX:
                            market_cities[c] = station_coords(c)
                        else:
                            coords = await geocode_city(c, http)
                            if coords:
                                market_cities[c] = (coords[0], coords[1])
                            else:
                                market_cities[c] = (0.0, 0.0)

            # 加上所有 STATION_IDX 中未出現在市場的（確保站點全覆蓋）
            for c in STATION_IDX:
                if c not in seen_cities:
                    lat, lon = station_coords(c)
                    market_cities[c] = (lat, lon)

            # 過濾無效坐標
            city_forecast_list = [(c, lat, lon) for c, (lat, lon) in market_cities.items() if lat != 0.0 or lon != 0.0]
            logger.info(f"🗺️  需預報城市: {len(city_forecast_list)} 個（含 {len(seen_cities)} 個市場發現+{len(STATION_IDX)} 個已知站點）")
            forecasts = await _weather.get_city_forecasts(city_forecast_list)
            if not forecasts:
                logger.warning("⚠️ 無預報")
                await asyncio.sleep(cfg.SCAN_INTERVAL_SEC)
                continue

            fc_index = {f"{f['city']}_{f['date']}": f for f in forecasts}

            # ── 🌡️ METAR 实时观测采集 ──
            metar_temps: dict[str, float] = {}
            if cfg.ENABLE_METAR:
                metar_temps = await _weather.fetch_all_metar()
                if metar_temps:
                    logger.info(f"  🌡️ METAR 实时: {len(metar_temps)} 个城市当前温度可用")
                    for mcity, mtemp in metar_temps.items():
                        logger.info(f"    {mcity}: {mtemp:.1f}°C")

            # ── 🌪️ 40成员 ICON Ensemble 预报（PolyWeather 信号源）──
            ensemble_40_index = {}  # city_date → {date, members, mu, sigma,...}
            ens40_fetch_count = 0
            if cfg.ENSEMBLE_40_ENABLED and cfg.ENSEMBLE_40_MODE != "deterministic_only":
                ens40_tasks = []
                ens40_cities = []
                for c, lat, lon in city_forecast_list:
                    ens40_tasks.append(_weather.get_ensemble_40_forecast(lat, lon))
                    ens40_cities.append(c)
                ens40_results = await asyncio.gather(*ens40_tasks, return_exceptions=True)
                for ci, result in enumerate(ens40_results):
                    city = ens40_cities[ci]
                    if isinstance(result, Exception):
                        logger.debug(f"Ensemble40 {city}: {result}")
                        continue
                    for day in result:
                        key = f"{city}_{day['date']}"
                        ensemble_40_index[key] = day
                        ens40_fetch_count += 1
                if ens40_fetch_count:
                    logger.info(f"  🌪️ 40成员Ensemble: {ens40_fetch_count} 个城市/日期组合 ({cfg.ENSEMBLE_40_MODEL})")

            # ── 3. 價格映射（雙向）──
            no_pmap = {}  # market_id → no_price
            yes_pmap = {}  # market_id → yes_price
            minfo = {}
            for m in markets:
                no_pmap[m["id"]] = m["no_price"]
                yes_pmap[m["id"]] = m["yes_price"]
                minfo[m["id"]] = {"yes": m["yes_price"], "no": m["no_price"], "closed": m.get("closed", False)}

            # ── 4. 更新持倉（雙向）──
            _engine.update_all(no_pmap, yes_pmap)

            # ── 5. 結算檢查 ──
            _engine.check_settled(minfo)

            # ── 6. 出場 ──
            exited = _engine.check_exit()
            if exited:
                logger.info(f"🏃 平倉 {exited} 筆")

            # ── v7: 逐层止盈（边缘桶提前锁定利润） ──
            layered_exits = _engine.check_layered_tp()
            if layered_exits:
                exited += layered_exits

            # ── 7. 風控 ──
            if _engine.should_pause():
                logger.warning("⏸️ 暫停交易")
                _last_scan = datetime.now(timezone.utc).isoformat()
                await asyncio.sleep(cfg.SCAN_INTERVAL_SEC)
                continue

            # ── 🌡️ METAR 极端低估信号扫描 ──
            # 当 METAR 实时温度显著偏离预报时，扫描 < 5¢ 的极端低估合约
            # 高赔率 YES 买入
            metar_signals: list[dict] = []
            if cfg.ENABLE_METAR and metar_temps:
                for m in markets:
                    if _engine.has_position(m["id"]):
                        continue
                    parsed_ex = parse_market_question(m["question"])
                    if not parsed_ex:
                        continue
                    city = parsed_ex["city"]
                    if city not in metar_temps:
                        continue
                    p_market = m["yes_price"]
                    # 只关注 < 5¢ 的极端低估合约
                    if p_market >= cfg.EXTREME_BUY_THRESH:
                        continue

                    metar_temp = metar_temps[city]
                    fc_key = f"{city}_{parsed_ex['date']}"
                    fc = fc_index.get(fc_key)
                    if not fc:
                        continue
                    forecast_mean = fc.get("mean", fc.get("raw_mean", 25.0))

                    # METAR 温度偏离预报 >= 阈值时视为强信号
                    deviation = abs(metar_temp - forecast_mean)
                    if deviation < cfg.METAR_DEVIATION_THRESH:
                        continue

                    # 计算用 METAR 温度替代预报后的概率
                    sigma = fc.get("sigma", cfg.DEFAULT_SIGMA)
                    if "threshold" in parsed_ex:
                        z = (parsed_ex["threshold"] - metar_temp) / sigma
                        p_metar = _gaussian_cdf(z) if parsed_ex["dir"] == "below" else 1 - _gaussian_cdf(z)
                    else:
                        p_metar = bucket_prob(parsed_ex["lower"], parsed_ex["upper"], metar_temp, sigma)

                    # METAR 显示概率远高于市场价格 → 买入 YES
                    metar_diff = p_metar - p_market
                    if metar_diff >= cfg.CALIB_THRESH:
                        sig = {
                            "market_id": m["id"],
                            "city": city,
                            "bucket": parsed_ex["label"],
                            "date": parsed_ex["date"],
                            "mu": metar_temp,
                            "sigma": sigma,
                            "p_model": round(p_metar, 4),
                            "p_market": round(p_market, 4),
                            "diff": round(metar_diff, 4),
                            "entry_price": round(p_market, 4),
                            "end_date": m.get("end_date", ""),
                            "theta_mult": 1.0,
                            "no_token_id": m.get("no_token_id", ""),
                            "side": "YES",
                            "is_ladder": False,
                            "ladder_parent": None,
                            "signal_type": "METAR",
                        }
                        metar_signals.append(sig)
                        logger.info(f"  🌡️🔥 METAR极端 [{city}] {parsed_ex['label']}: "
                                   f"实时={metar_temp:.1f}°C 预报μ={forecast_mean:.1f}°C "
                                   f"p_metar={p_metar:.1%} mkt={p_market:.4f} "
                                   f"diff={metar_diff:+.1%} → 买YES@{p_market:.4f}")

                if metar_signals:
                    logger.info(f"  🌡️🔥 METAR极端低估信号: {len(metar_signals)} 笔")

            # ── 8. 校準分析 + 集成概率 + 雙向信號 ──
            signals = []
            analyses = []
            _default_signal_type = "MODEL"
            ladder_signals = []
            parsed_count = 0
            matched_fc_count = 0
            ensemble_count = 0
            city_filtered_count = 0

            # 構建市場索引: (city_lower, date, bucket_label) → market
            # 用於快速查找相鄰桶對應的真實市場
            market_idx: dict[tuple[str, str, str], dict] = {}
            for m in markets:
                pq = parse_market_question(m["question"])
                if pq:
                    key = (pq["city"].lower(), pq["date"], pq["label"])
                    market_idx[key] = m

            for m in markets:
                if _engine.has_position(m["id"]):
                    continue
                parsed = parse_market_question(m["question"])
                if parsed:
                    parsed_count += 1
                if not parsed:
                    continue

                key = f"{parsed['city']}_{parsed['date']}"
                fc = fc_index.get(key)
                if not fc or not fc["is_reliable"]:
                    continue
                matched_fc_count += 1

                # @to010: ALLOWED_CITIES 過濾
                if cfg.ALLOWED_CITIES and parsed["city"] not in cfg.ALLOWED_CITIES:
                    city_filtered_count += 1
                    continue

                if _engine.city_day_count(parsed["city"], parsed["date"]) >= cfg.MAX_POS_PER_CITY_DAY:
                    continue

                mu = fc["mean"]
                sigma = fc["sigma"]
                models_raw = fc.get("models", {})
                bias_corr = fc.get("bias_correction", 0.0)

                # 溫度合理性檢查：跳過 °F 或離譜值
                q_lower = m["question"].lower()
                if "°f" in q_lower or "fahrenheit" in q_lower:
                    continue
                temp_val = parsed.get("threshold", parsed.get("lower", 0))
                if temp_val > 50 and parsed["city"] not in ("Dubai", "Mumbai", "Bangkok", "Singapore"):
                    continue

                # ── 集成概率 (ECMWF/GFS/ICON 加權平均) ──
                p_model_direct = None
                p_model_ensemble = None
                p_model_40 = None  # 40-member 非参数概率
                p_model_40_gaussian = None  # 40-member 高斯 CDF
                ens40_mu = None
                ens40_sigma = None

                # 方法 A: 直接預報均值法（現有邏輯）
                if "threshold" in parsed:
                    z = (parsed["threshold"] - mu) / sigma
                    if parsed["dir"] == "below":
                        p_model_direct = _gaussian_cdf(z)
                    else:
                        p_model_direct = 1 - _gaussian_cdf(z)
                else:
                    p_model_direct = bucket_prob(parsed["lower"], parsed["upper"], mu, sigma)

                # 方法 B: 3模型集成概率法（多模型加權 CDF 平均）
                if cfg.ENSEMBLE_ENABLED and models_raw and len(cfg.ENSEMBLE_WEIGHTS) > 0:
                    # 從 models_raw 中提取指定的集成模型
                    ens_models = {}
                    for em in cfg.ENSEMBLE_MODELS:
                        if em in models_raw:
                            ens_models[em] = models_raw[em]
                    if len(ens_models) >= 2:
                        if "threshold" in parsed:
                            # 單閾值場景：對每個模型算概率
                            ens_temps = list(ens_models.values())
                            ens_mu = sum(ens_temps) / len(ens_temps) - bias_corr
                            ens_sigma = math.sqrt(sum((t - ens_mu - bias_corr) ** 2 for t in ens_temps) / len(ens_temps)) if len(ens_temps) > 1 else sigma
                            if ens_sigma < 0.5:
                                ens_sigma = sigma
                            z = (parsed["threshold"] - ens_mu) / ens_sigma
                            if parsed["dir"] == "below":
                                p_model_ensemble = _gaussian_cdf(z)
                            else:
                                p_model_ensemble = 1 - _gaussian_cdf(z)
                        else:
                            p_model_ensemble = ensemble_prob(
                                ens_models, cfg.ENSEMBLE_WEIGHTS,
                                parsed["lower"], parsed["upper"], bias_corr)
                        ensemble_count += 1

                # ── 🌪️ 方法 C: 40成员 ICON Ensemble 概率（PolyWeather 信号源）──
                ens40_key = key
                ens40_data = ensemble_40_index.get(ens40_key) if cfg.ENSEMBLE_40_ENABLED and cfg.ENSEMBLE_40_MODE != "deterministic_only" else None
                if ens40_data and ens40_data.get("members"):
                    ens40_members = ens40_data["members"]
                    ens40_mu = ens40_data["mu"]
                    ens40_sigma = ens40_data["sigma"]

                    # C1: 非参数概率（直接用成员计数）
                    if "threshold" in parsed:
                        p_model_40 = WeatherClient.calc_ensemble_40_threshold_prob(
                            ens40_members, parsed["threshold"], parsed["dir"])
                        # 同时计算高斯版本用于对比
                        z_40 = (parsed["threshold"] - ens40_mu) / ens40_sigma
                        p_model_40_gaussian = _gaussian_cdf(z_40) if parsed["dir"] == "below" else 1 - _gaussian_cdf(z_40)
                    else:
                        p_model_40 = WeatherClient.calc_ensemble_40_prob(
                            ens40_members, parsed["lower"], parsed["upper"])
                        p_model_40_gaussian = bucket_prob(parsed["lower"], parsed["upper"], ens40_mu, ens40_sigma)

                # ── 策略选择: 取最保守的概率（最大偏差）──
                # 可用候选: p_model_direct, p_model_ensemble, p_model_40, p_model_40_gaussian
                candidates = [(p_model_direct, "直接")]
                if p_model_ensemble is not None:
                    candidates.append((p_model_ensemble, "3-集成"))
                if p_model_40 is not None:
                    candidates.append((p_model_40, "40-非参数"))
                if p_model_40_gaussian is not None:
                    candidates.append((p_model_40_gaussian, "40-高斯"))

                # hybrid模式: 40-member优先，3模型兜底
                if cfg.ENSEMBLE_40_MODE == "ensemble_only" and p_model_40 is not None:
                    p_model = p_model_40
                    p_model_src = "40-非参数"
                elif cfg.ENSEMBLE_40_MODE == "ensemble_only" and p_model_40_gaussian is not None:
                    p_model = p_model_40_gaussian
                    p_model_src = "40-高斯"
                elif cfg.ENSEMBLE_40_MODE == "hybrid":
                    # 40-member优先，取更保守的那个
                    if p_model_40 is not None and p_model_ensemble is not None:
                        p_model = min(p_model_40, p_model_ensemble, p_model_direct)
                        # 记录来源
                        min_src = "40-非参数"
                        if p_model_ensemble < p_model_40 and p_model_ensemble < p_model_direct:
                            min_src = "3-集成"
                        elif p_model_direct < p_model_40 and p_model_direct < p_model_ensemble:
                            min_src = "直接"
                        p_model_src = min_src
                    elif p_model_40 is not None:
                        p_model = min(p_model_40, p_model_direct)
                        p_model_src = "40-非参数" if p_model_40 < p_model_direct else "直接"
                    elif p_model_ensemble is not None:
                        p_model = min(p_model_direct, p_model_ensemble)
                        p_model_src = "3-集成" if p_model_ensemble < p_model_direct else "直接"
                    else:
                        p_model = p_model_direct
                        p_model_src = "直接"
                else:
                    # deterministic_only: 只用直接预报
                    p_model = p_model_direct
                    p_model_src = "直接"

                p_market = m["yes_price"]
                diff = p_market - p_model

                # Theta 懲罰: 距結算越近，edge 門檻越高
                theta_mult = Engine.calc_theta_multiplier(m.get("end_date", "")) if cfg.THETA_ENABLED else 1.0
                # 40-member 使用独立的edge阈值（通常更紧）
                if p_model_40 is not None:
                    effective_thresh = cfg.ENSEMBLE_40_EDGE * theta_mult
                else:
                    effective_thresh = cfg.CALIB_THRESH * theta_mult

                # ── 🌐 多模型聚合 + 一致性评分 (ENABLE_MULTI_MODEL) ──
                consistency_mult = 1.0
                agreement = 0.0
                spread = 0.0
                if cfg.ENABLE_MULTI_MODEL and models_raw and len(models_raw) >= 2:
                    # 从所有模型中选择配置中指定的参与聚合的模型
                    selected: dict[str, float] = {}
                    for mname, mtemp in models_raw.items():
                        if any(cfg_m in mname for cfg_m in cfg.MULTI_MODEL_MODELS):
                            selected[mname] = mtemp
                    if len(selected) >= 2:
                        spread = MultiModelAggregator.compute_spread(selected)
                        # 根据市场类型（阈值vs范围）计算各模型的桶概率
                        if "threshold" in parsed:
                            model_probs = MultiModelAggregator.compute_threshold_probs(
                                selected, parsed["threshold"], parsed["dir"], sigma)
                        else:
                            model_probs = MultiModelAggregator.compute_model_probabilities(
                                selected, parsed["lower"], parsed["upper"], sigma)
                        agreement = MultiModelAggregator.compute_agreement(
                            model_probs, cfg.MULTI_MODEL_AGREEMENT_THRESH)
                        consistency_mult = MultiModelAggregator.consistency_multiplier(
                            agreement, spread, cfg.MULTI_MODEL_SPREAD_CAP)
                        logger.debug(
                            f"  🌐 {parsed['city']} {parsed['label']}: "
                            f"agreement={agreement:.0%} spread={spread:.1f}°C mult={consistency_mult:.2f} "
                            f"({len(selected)}模型参与)")

                analyses.append({
                    "city": parsed["city"], "bucket": parsed["label"],
                    "date": parsed["date"], "mu": mu, "sigma": sigma,
                    "p_model": p_model, "p_model_direct": p_model_direct,
                    "p_model_ensemble": p_model_ensemble,
                    "p_model_40": p_model_40,
                    "p_model_40_gaussian": p_model_40_gaussian,
                    "ens40_mu": ens40_mu,
                    "ens40_sigma": ens40_sigma,
                    "p_market": p_market, "diff": diff,
                    "no_price": m["no_price"],
                    "theta_mult": theta_mult,
                    "model_src": p_model_src,
                    "agreement": round(agreement, 4),
                    "spread": round(spread, 2),
                    "consistency_mult": round(consistency_mult, 4),
                })

                # ── @to010: 雙向交易信號生成 ──
                # NO 方向: diff > 0 → 市場高估 YES → 買 NO
                if "NO" in cfg.ALLOWED_SIDES and diff >= effective_thresh and cfg.MIN_YES <= p_market <= cfg.MAX_YES:
                    sig = {
                        "market_id": m["id"], "city": parsed["city"],
                        "bucket": parsed["label"], "date": parsed["date"],
                        "mu": mu, "sigma": sigma,
                        "p_model": round(p_model, 4),
                        "p_market": round(p_market, 4),
                        "diff": round(diff, 4),
                        "entry_price": round(m["no_price"], 4),
                        "end_date": m.get("end_date", ""),
                        "theta_mult": theta_mult,
                        "no_token_id": m.get("no_token_id", ""),
                        "side": "NO",
                        "is_ladder": False,
                        "ladder_parent": None,
                        "agreement": round(agreement, 4),
                        "spread": round(spread, 2),
                        "consistency_mult": round(consistency_mult, 4),
                    }
                    _ens40_log = f" | 40μ={ens40_mu:.1f} σ={ens40_sigma:.1f}" if ens40_mu is not None else ""
                    _multi_log = f" 🌐同意{agreement:.0%}离散{spread:.1f}°C乘数{consistency_mult:.2f}" if cfg.ENABLE_MULTI_MODEL and consistency_mult < 1.0 else ""
                    logger.info(f"  🟢 買NO {parsed['city']} {parsed['label']}: μ={mu:.1f} σ={sigma:.1f} "
                                f"model={p_model:.1%}({p_model_src}) mkt={p_market:.1%} "
                                f"diff={diff:+.1%} θx{theta_mult:.1f} → NO@{m['no_price']:.4f}{_ens40_log}{_multi_log}")
                    signals.append(sig)

                # YES 方向: diff < 0 → 市場低估 YES → 買 YES
                yes_entry_min = 1.0 - cfg.MAX_YES  # 0.10
                yes_entry_max = 1.0 - cfg.MIN_YES  # 0.70
                if "YES" in cfg.ALLOWED_SIDES and diff <= -effective_thresh and yes_entry_min <= p_market <= yes_entry_max:
                    sig = {
                        "market_id": m["id"], "city": parsed["city"],
                        "bucket": parsed["label"], "date": parsed["date"],
                        "mu": mu, "sigma": sigma,
                        "p_model": round(p_model, 4),
                        "p_market": round(p_market, 4),
                        "diff": round(diff, 4),
                        "entry_price": round(p_market, 4),  # YES price = p_market
                        "end_date": m.get("end_date", ""),
                        "theta_mult": theta_mult,
                        "no_token_id": m.get("no_token_id", ""),
                        "side": "YES",
                        "is_ladder": False,
                        "ladder_parent": None,
                        "agreement": round(agreement, 4),
                        "spread": round(spread, 2),
                        "consistency_mult": round(consistency_mult, 4),
                    }
                    _ens40_log = f" | 40μ={ens40_mu:.1f} σ={ens40_sigma:.1f}" if ens40_mu is not None else ""
                    _multi_log = f" 🌐同意{agreement:.0%}离散{spread:.1f}°C乘数{consistency_mult:.2f}" if cfg.ENABLE_MULTI_MODEL and consistency_mult < 1.0 else ""
                    logger.info(f"  🔵 買YES {parsed['city']} {parsed['label']}: μ={mu:.1f} σ={sigma:.1f} "
                                f"model={p_model:.1%}({p_model_src}) mkt={p_market:.1%} "
                                f"diff={diff:+.1%} θx{theta_mult:.1f} → YES@{p_market:.4f}{_ens40_log}{_multi_log}")
                    signals.append(sig)

                if not (diff >= effective_thresh or diff <= -effective_thresh):
                    _ens40_log_de = f" 40μ={ens40_mu:.1f}" if ens40_mu is not None else ""
                    logger.info(f"  ⚪ {parsed['city']} {parsed['label']}: μ={mu:.1f} σ={sigma:.1f} "
                                f"model={p_model:.1%}({p_model_src}) mkt={p_market:.1%} "
                                f"diff={diff:+.1%} θx{theta_mult:.1f} (edge不足){_ens40_log_de}")
                elif not (cfg.MIN_YES <= p_market <= cfg.MAX_YES or yes_entry_min <= p_market <= yes_entry_max):
                    _ens40_log_de = f" 40μ={ens40_mu:.1f}" if ens40_mu is not None else ""
                    logger.info(f"  ⚪ {parsed['city']} {parsed['label']}: μ={mu:.1f} σ={sigma:.1f} "
                                f"model={p_model:.1%}({p_model_src}) mkt={p_market:.1%} "
                                f"diff={diff:+.1%} θx{theta_mult:.1f} (價格區間外){_ens40_log_de}")

            # ── v7: 多城市联合过滤（按 EV 评分选 TOP N 城市） ──
            if cfg.MULTI_CITY_ENABLED and analyses:
                filtered_cities = Engine.rank_cities_by_ev(analyses)
                if filtered_cities:
                    top_city_set = set(a.get("city", "") for a in filtered_cities)
                    before = len(signals)
                    signals = [s for s in signals if s.get("city", "") in top_city_set]
                    if len(signals) < before:
                        logger.info(f"  🏙️ 多城市联合: 信号从 {before} 过滤到 {len(signals)} (仅TOP{cfg.MULTI_CITY_TOP_N}城市)")

            # ── 溫度階梯：為主信號生成相鄰桶信號 ──
            if cfg.LADDER_ENABLED and signals:
                for sig in signals:
                    if sig.get("is_ladder"):
                        continue  # 階梯信號不再產生子階梯
                    city = sig["city"]
                    dt = sig["date"]
                    # 找到當前桶在 cfg.buckets 中的索引
                    bucket_idx = -1
                    for i, (lo, hi, lbl) in enumerate(cfg.buckets):
                        if lbl == sig["bucket"]:
                            bucket_idx = i
                            break
                    if bucket_idx < 0:
                        continue

                    # v7: 自适应宽度 — 根据市场定价动态调整阶梯跨度
                    actual_spread = Engine.calc_adaptive_spread(sig.get("p_market", 0.5))
                    for side in range(-actual_spread, actual_spread + 1):
                        if side == 0:
                            continue
                        adj_idx = bucket_idx + side
                        if adj_idx < 0 or adj_idx >= len(cfg.buckets):
                            continue
                        lo, hi, lbl = cfg.buckets[adj_idx]

                        # 在市場索引中查找該鄰桶的真實市場
                        mkt_key = (city.lower(), dt, lbl)
                        adj_market = market_idx.get(mkt_key)
                        if not adj_market:
                            logger.debug(f"    ⏭️ 階梯 {lbl}: 無對應市場")
                            continue

                        # 防重複: 已持倉或已平倉
                        adj_mid = adj_market["id"]
                        if _engine.has_position(adj_mid):
                            continue

                        # 用相同 mu/sigma 計算相鄰桶的概率
                        p_model_adj = bucket_prob(lo, hi, sig["mu"], sig["sigma"])
                        p_market_adj = adj_market["yes_price"]
                        diff_adj = p_market_adj - p_model_adj

                        # 階梯桶使用更高的 edge 門檻
                        ladder_thresh = effective_thresh * cfg.LADDER_EDGE_BOOST
                        theta_mult_adj = Engine.calc_theta_multiplier(adj_market.get("end_date", "")) if cfg.THETA_ENABLED else 1.0

                        analyses.append({
                            "city": city, "bucket": lbl,
                            "date": dt, "mu": sig["mu"], "sigma": sig["sigma"],
                            "p_model": p_model_adj,
                            "p_market": p_market_adj, "diff": diff_adj,
                            "no_price": adj_market["no_price"],
                            "theta_mult": theta_mult_adj,
                            "model_src": "ladder",
                        })

                        yes_entry_min_adj = 1.0 - cfg.MAX_YES
                        yes_entry_max_adj = 1.0 - cfg.MIN_YES
                        is_no_signal = diff_adj >= ladder_thresh and cfg.MIN_YES <= p_market_adj <= cfg.MAX_YES
                        is_yes_signal = diff_adj <= -ladder_thresh and yes_entry_min_adj <= p_market_adj <= yes_entry_max_adj

                        if is_no_signal and "NO" in cfg.ALLOWED_SIDES:
                            ladder_sig = {
                                "market_id": adj_mid,
                                "city": city,
                                "bucket": lbl,
                                "date": dt,
                                "mu": sig["mu"],
                                "sigma": sig["sigma"],
                                "p_model": round(p_model_adj, 4),
                                "p_market": round(p_market_adj, 4),
                                "diff": round(diff_adj, 4),
                                "entry_price": round(adj_market["no_price"], 4),
                                "end_date": adj_market.get("end_date", ""),
                                "theta_mult": theta_mult_adj,
                                "no_token_id": adj_market.get("no_token_id", ""),
                                "side": "NO",
                                "is_ladder": True,
                                "ladder_parent": sig["bucket"],
                            }
                            ladder_signals.append(ladder_sig)
                            logger.info(f"  🪜 階梯NO {lbl}: p_model={p_model_adj:.1%} mkt={p_market_adj:.1%} "
                                        f"diff={diff_adj:+.1%} θx{theta_mult_adj:.1f} → NO@{adj_market['no_price']:.4f}")
                        elif is_yes_signal and "YES" in cfg.ALLOWED_SIDES:
                            ladder_sig = {
                                "market_id": adj_mid,
                                "city": city,
                                "bucket": lbl,
                                "date": dt,
                                "mu": sig["mu"],
                                "sigma": sig["sigma"],
                                "p_model": round(p_model_adj, 4),
                                "p_market": round(p_market_adj, 4),
                                "diff": round(diff_adj, 4),
                                "entry_price": round(p_market_adj, 4),
                                "end_date": adj_market.get("end_date", ""),
                                "theta_mult": theta_mult_adj,
                                "no_token_id": adj_market.get("no_token_id", ""),
                                "side": "YES",
                                "is_ladder": True,
                                "ladder_parent": sig["bucket"],
                            }
                            ladder_signals.append(ladder_sig)
                            logger.info(f"  🪜 階梯YES {lbl}: p_model={p_model_adj:.1%} mkt={p_market_adj:.1%} "
                                        f"diff={diff_adj:+.1%} θx{theta_mult_adj:.1f} → YES@{p_market_adj:.4f}")

                if ladder_signals:
                    logger.info(f"  🪜 溫度階梯: 主信號 {len(signals)} → 衍生 {len(ladder_signals)} 個相鄰桶信號")
                    signals.extend(ladder_signals)

            # ── 添加 METAR 极端信号 ──
            if metar_signals:
                signals.extend(metar_signals)
                logger.info(f"  🌡️ 添加 METAR 极端信号: {len(metar_signals)} 笔")

            city_filter_info = f" | 城市過濾={city_filtered_count}" if cfg.ALLOWED_CITIES else ""
            ens40_log = f" | 40成员Ensemble={ens40_fetch_count}组合" if ens40_fetch_count else ""
            logger.info(f"  🔍 分析: {len(markets)}市場 → {parsed_count}可解析 → {matched_fc_count}有預報{city_filter_info}{ens40_log} "
                        f"→ {len(signals)}信號(含階梯) | 3-模型集成={ensemble_count}次")
            _latest_signals = signals
            _latest_analyses = analyses
            _last_scan = datetime.now(timezone.utc).isoformat()

            # ── 9. 開倉, 防重複 + 風控 + 時間窗口 + OBI 過濾 + 階梯倉位 ──
            opened = 0
            ladder_opened = 0
            if not Engine.in_trading_window():
                logger.info(f"  ⏰ 非交易窗口 (UTC {cfg.TRADE_START_HOUR}-{cfg.TRADE_END_HOUR})，跳過開倉")
            elif signals:
                for sig in signals:
                    # 已在持倉或已平倉 → 跳過
                    if _engine.has_position(sig["market_id"]):
                        continue
                    # 冷卻期 → 跳過
                    if _engine.is_on_cooldown(sig.get("end_date", "")):
                        continue
                    # OBI 過濾
                    if cfg.OBI_ENABLED:
                        obi_ok = await _check_single_obi(http, sig)
                        if obi_ok is False:
                            logger.debug(f"  ⛔ OBI 過濾: {sig['city']} {sig['bucket']} (不均衡不足)")
                            continue
                    # 風控：並發限制
                    open_count = sum(1 for p in _engine.positions if p.is_open)
                    if open_count >= cfg.MAX_CONCURRENT:
                        logger.info(f"  ⏸️ 達最大並發 ({cfg.MAX_CONCURRENT})，停止開倉")
                        break
                    # 風控：同日同城限制（階梯桶不佔用主桶配額）
                    is_ladder = sig.get("is_ladder", False)
                    if not is_ladder:
                        city_day = _engine.city_day_count(sig["city"], sig["date"])
                        if city_day >= cfg.MAX_POS_PER_CITY_DAY:
                            logger.debug(f"  ⏭️ {sig['city']} {sig['date']} 已 {city_day}/{cfg.MAX_POS_PER_CITY_DAY} 檔")
                            continue

                    # v7: 动态预算分配（基于历史胜率和盈亏比）
                    dyn_budget = _engine.calc_dynamic_budget(sig["city"])
                    if dyn_budget < cfg.POS_MIN:
                        dyn_budget = cfg.POS_MIN

                    # ── 🎯 Fractional Kelly 仓位计算 ──
                    # 信号类型决定 Kelly 分数
                    sig_type = sig.get("signal_type", "MODEL")
                    p_model_for_kelly = sig.get("p_model", 0.5)
                    p_market_for_kelly = sig.get("p_market", 0.5)
                    side_for_kelly = sig.get("side", "NO")

                    if cfg.KELLY_ENABLED:
                        base_size = _engine.calc_kelly_size(
                            p_model_for_kelly, p_market_for_kelly,
                            side_for_kelly, sig_type)
                    else:
                        base_size = _engine.calc_position_size(sig["diff"])

                    # 如果动态预算小于默认仓位，使用动态预算
                    if cfg.DYNAMIC_BUDGET_ENABLED and dyn_budget < base_size:
                        base_size = dyn_budget
                    if is_ladder:
                        # 阶梯桶在 Kelly 基础上再按 LADDER_SIZE_PCT 缩小
                        size = max(cfg.KELLY_MIN_SIZE, base_size * cfg.LADDER_SIZE_PCT)
                        if size <= 10:
                            size = round(size, 1)
                        else:
                            size = round(size / 10) * 10
                    else:
                        size = base_size

                    if _engine.capital < size:
                        logger.debug(f"  資金不足: ${_engine.capital:.0f} < ${size:.0f}")
                        continue

                    sig_type = sig.get("signal_type", "MODEL")
                    # 计算策略归因标签
                    is_extreme = (sig_type == "METAR")
                    is_ladder_sig = sig.get("is_ladder", False)
                    # near_lock: 距结算 < 24h 且 diff 足够
                    is_near_lock = False
                    end_date_str = sig.get("end_date", "")
                    if end_date_str:
                        try:
                            ed = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                            hours_left = (ed - datetime.now(timezone.utc)).total_seconds() / 3600
                            if hours_left < 24 and hours_left > 0 and abs(sig.get("diff", 0)) > 0.05:
                                is_near_lock = True
                        except:
                            pass
                    strategy = Engine.classify_strategy(
                        sig_type, is_ladder_sig, is_extreme, is_near_lock, sig["city"])
                    pos = _engine.open_position(
                        sig["market_id"], sig["city"], sig["date"],
                        sig["bucket"], sig["entry_price"], size,
                        sig.get("end_date", ""), sig.get("side", "NO"),
                        signal_type=sig_type, strategy=strategy)
                    if pos is not None:
                        if is_ladder:
                            ladder_opened += 1
                        else:
                            opened += 1

            total_opened = opened + ladder_opened
            if total_opened:
                logger.info(f"  📦 本輪開倉: {total_opened} 筆 (主桶{opened} + 階梯{ladder_opened})")

            # ── 10. 狀態 ──
            s = _engine.summary()
            logger.info(f"📊 {s['total']}筆 | {s['win_rate']}% | P&L=${s['daily_pnl']:.2f} | 資金=${s['capital']:.2f} | 持倉={s['open_count']}")

            elapsed = time.time() - loop_start
            sleep = max(1, cfg.SCAN_INTERVAL_SEC - elapsed)
            await asyncio.sleep(sleep)

    except Exception as e:
        logger.error(f"主循環錯誤: {e}", exc_info=True)
    finally:
        await _weather.close()
        await http.aclose()
        logger.info("🛑 Bot 已停止")


if __name__ == "__main__":
    asyncio.run(main())
