#!/usr/bin/env python3
"""
HighTempTation 天氣預報校準套利 Bot (單文件合併版)

核心算法:
  1. Open-Meteo API 獲取各城市目標日最高溫預報（均值+標準差）
  2. 高斯 CDF 計算每個溫度桶的模型概率 p_model
  3. Polymarket Gamma API 獲取市場 YES 價格 p_market
  4. 當 p_market - p_model ≥ 0.15 且 p_market 在 30-90¢ → 買 NO
  5. 倉位 $300-500，NO 漲到 98-99¢ 賣出或持有到結算

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
from typing import Optional

import httpx
from dateutil import parser as dateparser

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
        self.WEATHER_MODELS = os.getenv("WEATHER_MODELS", "best_match,ecmwf_seamless,gfs_seamless,icon_seamless,gem_seamless,jma_seamless").split(",")
        self.DEFAULT_SIGMA = float(os.getenv("DEFAULT_SIGMA", "2.0"))

        self.CALIB_THRESH = float(os.getenv("CALIB_THRESHOLD", "0.15"))
        self.MIN_YES = float(os.getenv("MIN_YES_PRICE", "0.30"))
        self.MAX_YES = float(os.getenv("MAX_YES_PRICE", "0.90"))

        self.POS_MIN = float(os.getenv("POSITION_MIN_USD", "1"))
        self.POS_MAX = float(os.getenv("POSITION_MAX_USD", "1"))  # 固定 $1 超小注測信號
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
        self.TRADE_START_HOUR = int(os.getenv("TRADE_START_HOUR", "12"))  # 交易窗口起始 (UTC)
        self.TRADE_END_HOUR = int(os.getenv("TRADE_END_HOUR", "20"))  # 交易窗口結束 (UTC)
        self.FORCE_EXIT_HOURS = int(os.getenv("FORCE_EXIT_HOURS", "4"))
        self.FORCE_EXIT_MIN_PROFIT = float(os.getenv("FORCE_EXIT_MIN_PROFIT", "0.05"))

        self.MAX_POS_PER_CITY_DAY = int(os.getenv("MAX_POS_PER_CITY_DAY", "2"))
        self.LADDER_ENABLED = os.getenv("LADDER_ENABLED", "true").lower() == "true"
        self.LADDER_SPREAD = int(os.getenv("LADDER_SPREAD", "1"))       # 两侧各 N 个相邻桶
        self.LADDER_EDGE_BOOST = float(os.getenv("LADDER_EDGE_BOOST", "1.3"))  # 阶梯桶 edge 乘数
        self.LADDER_SIZE_PCT = float(os.getenv("LADDER_SIZE_PCT", "0.5"))       # 阶梯桶仓位比例
        self.ENSEMBLE_ENABLED = os.getenv("ENSEMBLE_ENABLED", "true").lower() == "true"
        self.ENSEMBLE_MODELS = os.getenv("ENSEMBLE_MODELS", "ecmwf_seamless,gfs_seamless,icon_seamless").split(",")
        raw_weights = os.getenv("ENSEMBLE_WEIGHTS", "0.4,0.3,0.3")
        self.ENSEMBLE_WEIGHTS = [float(w) for w in raw_weights.split(",")]
        self.MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "50"))
        self.MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))

        self.INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "10000"))

        self.BIAS_ENABLED = os.getenv("BIAS_ENABLED", "true").lower() == "true"
        self.BIAS_DAYS = int(os.getenv("BIAS_DAYS", "30"))
        self.CROSS_VALIDATE = os.getenv("CROSS_VALIDATE", "true").lower() == "true"

        self.DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
        self.DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "3002"))

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
        with sqlite3.connect(self.bias_db) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS bias (
                    city TEXT, icao TEXT, fc_date TEXT,
                    forecast REAL, actual REAL, bias REAL,
                    UNIQUE(city, fc_date)
                )""")
            db.commit()

    def record_forecast(self, city: str, icao: str, fc_date: str, temp: float):
        with sqlite3.connect(self.bias_db) as db:
            db.execute(
                "INSERT OR IGNORE INTO bias(city,icao,fc_date,forecast) VALUES(?,?,?,?)",
                (city, icao, fc_date, temp))
            db.commit()

    def get_rolling_bias(self, city: str, days: int = 30) -> tuple[float, int]:
        """(rolling_bias, sample_count) — 偏差 = 預報 - 實際"""
        with sqlite3.connect(self.bias_db) as db:
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
            with sqlite3.connect(self.bias_db) as db:
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
    """從市場問題中提取城市名稱。"""
    q = question.lower()
    for c in STATION_IDX:
        if c.lower() in q:
            return c
    patterns = [
        r'\bin\s+([A-Z][a-zA-Z\s-]+?)(?:\s+on|\s+for|\s+is|,|$)',
        r"([A-Z][a-zA-Z\s-]+?)'s\s+(?:high|max|temp|temperature)",
        r'\bfor\s+([A-Z][a-zA-Z\s-]+?)(?:\s+on|\s+is|,|$)',
    ]
    for pat in patterns:
        m = re.search(pat, question)
        if m:
            name = m.group(1).strip().rstrip(",.!?;")
            if 1 < len(name) < 40:
                return name
    words = re.findall(r'[A-Z][a-z]+', question)
    for w in words:
        if len(w) > 2 and w.lower() not in ("will", "the", "be", "between", "above", "below"):
            return w
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


async def scan_markets(http: httpx.AsyncClient) -> list[dict]:
    """掃描 Polymarket 溫度市場"""
    seen = set()
    markets = []
    terms = [
        "highest temperature", "lowest temperature", "max temperature", "high temp",
        "°C", "°F", "celsius", "fahrenheit",
        "weather forecast", "high of", "low of",
        "temperature in", "temperature for",
    ]

    for term in terms:
        try:
            r = await http.get(f"{cfg.GAMMA_API}/public-search", params={
                "q": term, "events_status": "active", "keep_closed_markets": 0, "limit_per_type": 100,
            }, timeout=15)
            r.raise_for_status()
            for evt in r.json().get("events", []):
                for mkt in evt.get("markets", []):
                    mid = str(mkt.get("conditionId", "") or mkt.get("id", ""))
                    if mid and mid not in seen:
                        seen.add(mid)
                        markets.append(mkt)
        except Exception:
            continue

    parsed = []
    for m in markets:
        try:
            q = str(m.get("question", "") or m.get("title", ""))
            if not any(k in q.lower() for k in ["temperature", "temp", "°c", "°f", "high"]):
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
                "yes_price": yes, "no_price": no_,
                "liquidity": liquidity,
                "closed": bool(m.get("closed", False)),
                "end_date": m.get("endDate", ""),
                "no_token_id": no_token_id,
            })
        except Exception:
            continue

    return [p for p in parsed if p["id"] and not p["closed"] and p["liquidity"] >= cfg.MIN_MARKET_LIQUIDITY]


# ═══════════════════════════════════════════════════════════════════════
# 7. 出場邏輯 + 8. 持倉追蹤 + 9. 主循環
# ═══════════════════════════════════════════════════════════════════════

class Position:
    __slots__ = ("market_id", "city", "date", "bucket_label",
                 "entry_no", "size", "curr_no", "end_date",
                 "pnl", "pct", "is_open", "exit_reason",
                 "exit_time", "realized", "_settled")

    def __init__(self, market_id: str, city: str, date_: str, label: str,
                 entry_no: float, size: float, end_date: str = ""):
        self.market_id = market_id
        self.city = city
        self.date = date_
        self.bucket_label = label
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
    def __init__(self):
        self.positions: list[Position] = []
        self.closed: list[Position] = []
        self.capital = cfg.INITIAL_CAPITAL
        self.total = 0
        self.wins = 0
        self.losses = 0
        self.daily_pnl = 0.0
        self.today = date.today()
        self.capital_history: list[tuple[str, float]] = [(datetime.now(timezone.utc).isoformat(), cfg.INITIAL_CAPITAL)]

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

    def calc_position_size(self, diff: float) -> float:
        r = min(1.0, (diff - cfg.CALIB_THRESH) / 0.35)
        s = cfg.POS_MIN + (cfg.POS_MAX - cfg.POS_MIN) * r
        s = min(s, self.capital * cfg.POS_CAP_PCT)
        s = max(cfg.POS_MIN, min(s, cfg.POS_MAX))
        # 小額 (<=10) 不四捨五入到整十
        if s <= 10:
            return round(s, 1)
        return round(s / 10) * 10

    def open_position(self, mid: str, city: str, dt: str, label: str,
                      entry_no: float, size: float, end_date: str = ""):
        # Bug 1: 已在持倉或已平倉 → 拒開
        if self.has_position(mid):
            logger.warning(f"  ⛔ 防重複: {city} {label} (market_id 已存在)")
            return None
        # Bug 2: 冷卻期 → 拒開
        if self.is_on_cooldown(end_date):
            logger.info(f"  ⏳ 冷卻: {city} {label} (距結算 < 60 分鐘)")
            return None
        p = Position(mid, city, dt, label, entry_no, size, end_date)
        self.positions.append(p)
        self.total += 1
        self.capital -= size
        self.capital_history.append((datetime.now(timezone.utc).isoformat(), self.capital))
        logger.info(f"📦 開倉 [{city} {label}] NO@{entry_no:.4f} ${size:.0f}")
        return p

    def update_all(self, pmap: dict[str, float]):
        """pmap: market_id → no_price"""
        for p in self.positions:
            if p.is_open and p.market_id in pmap:
                p.update(pmap[p.market_id])

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

            if closed or yes >= 0.99 or no_ >= 0.99:
                if no_ >= 0.99:
                    prof = p.size * (no_ / p.entry_no - 1)
                    p.close(f"結算 NO贏 +{prof/p.size*100:.0f}%", prof)
                elif yes >= 0.99:
                    p.close("結算 YES贏 -100%", -p.size)
                else:
                    prof = p.size * (no_ / p.entry_no - 1) if p.entry_no > 0 else 0
                    p.close(f"結算 NO={no_:.3f}", prof)
                self._settle(p, p.realized)
            else:
                p.update(no_)

    def _settle(self, p: Position, pnl: float):
        if pnl > 0: self.wins += 1
        else: self.losses += 1
        self.daily_pnl += pnl
        self.closed.append(p)
        # 資金回收
        self.capital += p.size + pnl
        self.capital_history.append((datetime.now(timezone.utc).isoformat(), self.capital))
        p._settled = True
        logger.info(f"  ✅ 平倉 [{p.city} {p.bucket_label}] P&L=${pnl:.2f} | {p.exit_reason}")

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
        logger.info("🧹 模擬數據已重置，資金回到 ${:.0f}".format(cfg.INITIAL_CAPITAL))

    def summary(self) -> dict:
        ops = [p for p in self.positions if p.is_open]
        wr = round(self.wins / max(self.total, 1) * 100, 1)
        total_pnl = round(self.capital - cfg.INITIAL_CAPITAL, 2)
        return {
            "total": self.total, "wins": self.wins, "losses": self.losses,
            "win_rate": wr, "daily_pnl": round(self.daily_pnl, 2),
            "total_pnl": total_pnl,
            "capital": round(self.capital, 2),
            "initial_capital": cfg.INITIAL_CAPITAL,
            "open_count": len(ops), "closed_count": len(self.closed),
            "open": [p.to_dict() for p in ops],
            "recent_closed": [p.to_dict() for p in self.closed[-50:]],
            "capital_history": self.capital_history[-100:],
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
        elif self.path in ("/", "/dashboard"):
            self._html()
        else:
            self.send_error(404)

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
        logger.info(f"  🌐集成預報={'ON' if cfg.ENSEMBLE_ENABLED else 'OFF'} 模型={cfg.ENSEMBLE_MODELS} 權重={cfg.ENSEMBLE_WEIGHTS}")
    else:
        logger.info(f"  🌐集成預報=OFF")
    logger.info(f"  預設站點: {len(STATION_IDX)} 個 + 動態 geocode 其他城市")
    logger.info("=" * 60)

    _engine = Engine()
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

            # ── 2. 掃描市場 ──
            markets = await scan_markets(http)
            logger.info(f"🌡️ {len(markets)} 個活躍市場")

            # ── 1. 動態解析市場中的城市，獲取預報 ──
            # 收集所有市場中出現的城市
            market_cities: dict[str, tuple[float, float]] = {}
            for m in markets:
                parsed_q = parse_market_question(m["question"])
                if parsed_q:
                    c = parsed_q["city"]
                    if c not in market_cities and c not in STATION_IDX:
                        coords = await geocode_city(c, http)
                        if coords:
                            market_cities[c] = (coords[0], coords[1])
                        else:
                            market_cities[c] = (0.0, 0.0)  # 標記無效
                    elif c in STATION_IDX:
                        market_cities[c] = station_coords(c)

            # 合併已知站點 + 動態解析的城市
            city_forecast_list = []
            for c, (lat, lon) in market_cities.items():
                if lat != 0.0 or lon != 0.0:
                    city_forecast_list.append((c, lat, lon))
            # 也加上所有 STATION_IDX 中未出現的
            for c in STATION_IDX:
                if c not in market_cities:
                    lat, lon = station_coords(c)
                    city_forecast_list.append((c, lat, lon))

            logger.info(f"🗺️  需預報城市: {len(city_forecast_list)} 個")
            forecasts = await _weather.get_city_forecasts(city_forecast_list)
            if not forecasts:
                logger.warning("⚠️ 無預報")
                await asyncio.sleep(cfg.SCAN_INTERVAL_SEC)
                continue

            fc_index = {f"{f['city']}_{f['date']}": f for f in forecasts}

            # ── 3. 價格映射 ──
            pmap = {}  # market_id → no_price
            minfo = {}
            for m in markets:
                pmap[m["id"]] = m["no_price"]
                minfo[m["id"]] = {"yes": m["yes_price"], "no": m["no_price"], "closed": m.get("closed", False)}

            # ── 4. 更新持倉 ──
            _engine.update_all(pmap)

            # ── 5. 結算檢查 ──
            _engine.check_settled(minfo)

            # ── 6. 出場 ──
            exited = _engine.check_exit()
            if exited:
                logger.info(f"🏃 平倉 {exited} 筆")

            # ── 7. 風控 ──
            if _engine.should_pause():
                logger.warning("⏸️ 暫停交易")
                _last_scan = datetime.now(timezone.utc).isoformat()
                await asyncio.sleep(cfg.SCAN_INTERVAL_SEC)
                continue

            # ── 8. 校準分析 + 集成概率 + 溫度階梯 ──
            signals = []
            analyses = []
            ladder_signals = []
            parsed_count = 0
            matched_fc_count = 0
            ensemble_count = 0

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

                # 方法 A: 直接預報均值法（現有邏輯）
                if "threshold" in parsed:
                    z = (parsed["threshold"] - mu) / sigma
                    if parsed["dir"] == "below":
                        p_model_direct = _gaussian_cdf(z)
                    else:
                        p_model_direct = 1 - _gaussian_cdf(z)
                else:
                    p_model_direct = bucket_prob(parsed["lower"], parsed["upper"], mu, sigma)

                # 方法 B: 集成概率法（多模型加權 CDF 平均）
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

                # 使用更保守的概率：p_model 越小 diff 越大，信號越強
                # 取 p_model 較小的那個（更保守 = 更高的 edge）
                if p_model_ensemble is not None:
                    p_model = min(p_model_direct, p_model_ensemble)
                    p_model_src = "集成" if p_model_ensemble < p_model_direct else "直接"
                else:
                    p_model = p_model_direct
                    p_model_src = "直接"

                p_market = m["yes_price"]
                diff = p_market - p_model

                # Theta 懲罰: 距結算越近，edge 門檻越高
                theta_mult = Engine.calc_theta_multiplier(m.get("end_date", "")) if cfg.THETA_ENABLED else 1.0
                effective_thresh = cfg.CALIB_THRESH * theta_mult

                analyses.append({
                    "city": parsed["city"], "bucket": parsed["label"],
                    "date": parsed["date"], "mu": mu, "sigma": sigma,
                    "p_model": p_model, "p_model_direct": p_model_direct,
                    "p_model_ensemble": p_model_ensemble,
                    "p_market": p_market, "diff": diff,
                    "no_price": m["no_price"],
                    "theta_mult": theta_mult,
                    "model_src": p_model_src if p_model_ensemble else "direct",
                })

                is_primary = diff >= effective_thresh and cfg.MIN_YES <= p_market <= cfg.MAX_YES
                if is_primary:
                    sig = {
                        "market_id": m["id"], "city": parsed["city"],
                        "bucket": parsed["label"], "date": parsed["date"],
                        "mu": mu, "sigma": sigma,
                        "p_model": round(p_model, 4),
                        "p_market": round(p_market, 4),
                        "diff": round(diff, 4),
                        "no_price": round(m["no_price"], 4),
                        "end_date": m.get("end_date", ""),
                        "theta_mult": theta_mult,
                        "no_token_id": m.get("no_token_id", ""),
                        "is_ladder": False,
                        "ladder_parent": None,
                    }
                    logger.info(f"  🟢 {parsed['city']} {parsed['label']}: μ={mu:.1f} σ={sigma:.1f} "
                                f"model={p_model:.1%}({p_model_src}) mkt={p_market:.1%} "
                                f"diff={diff:+.1%} θx{theta_mult:.1f} → NO@{m['no_price']:.4f}")
                    signals.append(sig)
                else:
                    logger.info(f"  ⚪ {parsed['city']} {parsed['label']}: μ={mu:.1f} σ={sigma:.1f} "
                                f"model={p_model:.1%}({p_model_src}) mkt={p_market:.1%} "
                                f"diff={diff:+.1%} θx{theta_mult:.1f} {'(edge不足)' if diff < effective_thresh else '(價格區間外)'}")

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

                    # 向兩側擴展 LADDER_SPREAD 個相鄰桶
                    for side in range(-cfg.LADDER_SPREAD, cfg.LADDER_SPREAD + 1):
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

                        if diff_adj >= ladder_thresh and cfg.MIN_YES <= p_market_adj <= cfg.MAX_YES:
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
                                "no_price": round(adj_market["no_price"], 4),
                                "end_date": adj_market.get("end_date", ""),
                                "theta_mult": theta_mult_adj,
                                "no_token_id": adj_market.get("no_token_id", ""),
                                "is_ladder": True,
                                "ladder_parent": sig["bucket"],
                            }
                            ladder_signals.append(ladder_sig)
                            logger.info(f"  🪜 階梯 {lbl}: p_model={p_model_adj:.1%} mkt={p_market_adj:.1%} "
                                        f"diff={diff_adj:+.1%} θx{theta_mult_adj:.1f} → NO@{adj_market['no_price']:.4f}")

                if ladder_signals:
                    logger.info(f"  🪜 溫度階梯: 主信號 {len(signals)} → 衍生 {len(ladder_signals)} 個相鄰桶信號")
                    signals.extend(ladder_signals)

            logger.info(f"  🔍 分析: {len(markets)}市場 → {parsed_count}可解析 → {matched_fc_count}有預報 "
                        f"→ {len(signals)}信號(含階梯) | 集成概率={ensemble_count}次")
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

                    # 階梯桶倉位按比例縮小
                    base_size = _engine.calc_position_size(sig["diff"])
                    if is_ladder:
                        size = max(cfg.POS_MIN, base_size * cfg.LADDER_SIZE_PCT)
                        if size <= 10:
                            size = round(size, 1)
                        else:
                            size = round(size / 10) * 10
                    else:
                        size = base_size

                    if _engine.capital < size:
                        logger.debug(f"  資金不足: ${_engine.capital:.0f} < ${size:.0f}")
                        continue

                    pos = _engine.open_position(
                        sig["market_id"], sig["city"], sig["date"],
                        sig["bucket"], sig["no_price"], size, sig.get("end_date", ""))
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
