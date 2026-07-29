#!/usr/bin/env python3
"""
HighTempTation — 升级数据采集器

功能:
  1. Open-Meteo Ensemble API → 多模型集合预报
  2. 直接写入 SQLite（替代 CSV）
  3. 滚动偏差校正（从 DB 读取历史偏差）
  4. METAR 实况温度采集

用法:
  fetcher = DataFetcher(db=TradeDB("hightemptation.db"))
  await fetcher.fetch_ensemble_forecast("Tokyo")
  await fetcher.fetch_metar_actual("Tokyo")
"""
import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import httpx

from db_manager import TradeDB

logger = logging.getLogger("data_fetcher")


# ── 气象站坐标对照 ──
STATIONS = {
    "Tokyo":       (35.5494, 139.7798),
    "Seoul":       (37.5583, 126.7906),
    "Singapore":   (1.3502,  103.9944),
    "Hong Kong":   (22.3080, 113.9185),
    "Shanghai":    (31.1979, 121.3363),
    "Bangkok":     (13.6811, 100.7470),
    "Mumbai":      (19.0887, 72.8679),
    "Dubai":       (25.2528, 55.3644),
    "Istanbul":    (41.2613, 28.7419),
    "New York":    (40.7789, -73.9692),
    "Los Angeles": (33.9416, -118.4085),
    "Chicago":     (41.9786, -87.9048),
    "Miami":       (25.7932, -80.2906),
    "San Francisco": (37.6188, -122.3756),
    "Toronto":     (43.6777, -79.6305),
    "Mexico City": (19.4361, -99.0720),
    "London":      (51.4700, -0.4543),
    "Paris":       (49.0097, 2.5479),
    "Berlin":      (52.5597, 13.2877),
    "Sydney":      (-33.9399, 151.1753),
}

OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo 支持的模型列表
ENSEMBLE_MODELS = ["best_match", "icon_seamless", "gfs_seamless", "gem_seamless", "jma_seamless"]


class DataFetcher:
    """
    升级版数据采集器。

    与旧版区别:
      - 使用 Ensemble API（5 个模型 vs 旧版单模型）
      - 直接写入 SQLite（vs 旧版 CSV）
      - 内置偏差校正（vs 旧版 BIAS_CACHE 字典）
    """

    def __init__(self, db: TradeDB, api_key: str = ""):
        self.db = db
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    # ════════════════════════════════════════════════════════════════
    # 1. Open-Meteo Ensemble API → 多模型集合预报
    # ════════════════════════════════════════════════════════════════

    async def fetch_ensemble_forecast(self, city: str,
                                       forecast_days: int = 7) -> Optional[Dict[str, dict]]:
        """
        获取单个城市的多模型集合预报。

        对每个模型调用 Open-Meteo API → 解析 daily.temperature_2m_max
        → 计算 ensemble mu = 各模型均值, sigma = 各模型标准差

        :returns: {date_str: {"mu": float, "sigma": float, "models": {...}}}
        """
        coords = STATIONS.get(city)
        if not coords:
            logger.warning(f"未知城市: {city}")
            return None

        lat, lon = coords
        results = {}

        for model in ENSEMBLE_MODELS:
            try:
                data = await self._fetch_single_model(lat, lon, model, forecast_days)
                if data:
                    for date_str, temp in data.items():
                        if date_str not in results:
                            results[date_str] = {"temps": [], "models": {}}
                        results[date_str]["temps"].append(temp)
                        results[date_str]["models"][model] = temp
            except Exception as e:
                logger.warning(f"  [{city}] 模型 {model} 失败: {e}")

        if not results:
            logger.warning(f"[{city}] 所有模型均失败")
            return None

        # 计算 ensemble mu/sigma 并写入 DB
        forecasts = {}
        for date_str, r in results.items():
            temps = r["temps"]
            if not temps:
                continue
            mu = sum(temps) / len(temps)
            sigma = math.sqrt(sum((t - mu) ** 2 for t in temps) / len(temps)) if len(temps) > 1 else 2.0

            # 偏差校正
            bias = self.db.get_bias(city, days=30)
            adjusted_mu = mu - bias

            self.db.store_forecast(date_str, city, adjusted_mu, sigma,
                                   model="ensemble")
            forecasts[date_str] = {
                "mu": adjusted_mu,
                "sigma": sigma,
                "raw_mu": mu,
                "bias": bias,
                "n_models": len(temps),
                "models": r["models"],
            }
            logger.info(f"  [{city}] {date_str}: μ={adjusted_mu:.1f}°C σ={sigma:.1f} bias={bias:+.2f} ({len(temps)}模型)")

        return forecasts

    async def _fetch_single_model(self, lat: float, lon: float, model: str,
                                   forecast_days: int) -> Optional[Dict[str, float]]:
        """调用 Open-Meteo API 获取单个模型的日最高温序列"""
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "models": model,
            "timezone": "auto",
            "forecast_days": str(forecast_days),
        }
        try:
            resp = await self.client.get(OPEN_METEO_BASE, params=params)
            resp.raise_for_status()
            data = resp.json()
            daily = data.get("daily", {})
            times = daily.get("time", [])
            temps = daily.get("temperature_2m_max", [])
            return {t: v for t, v in zip(times, temps) if v is not None}
        except httpx.HTTPError as e:
            logger.debug(f"  {model}: HTTP {e}")
            return None

    # ════════════════════════════════════════════════════════════════
    # 2. 批量获取所有城市预报
    # ════════════════════════════════════════════════════════════════

    async def fetch_all_cities(self, forecast_days: int = 7) -> Dict[str, dict]:
        """遍历所有城市获取集合预报"""
        all_results = {}
        for city in STATIONS:
            logger.info(f"🌤️  {city}...")
            result = await self.fetch_ensemble_forecast(city, forecast_days)
            if result:
                all_results[city] = result
            await asyncio.sleep(0.5)  # 限速
        logger.info(f"✅ {len(all_results)}/{len(STATIONS)} 城市预报完成")
        return all_results

    # ════════════════════════════════════════════════════════════════
    # 3. METAR 实况温度
    # ════════════════════════════════════════════════════════════════

    async def fetch_metar_actual(self, city: str) -> Optional[float]:
        """
        从 aviationweather.gov 获取 METAR 实际温度。
        写入 DB 的 actuals 表。
        """
        coords = STATIONS.get(city)
        if not coords:
            return None

        # 用坐标最近的 METAR 站
        lat, lon = coords
        url = "https://aviationweather.gov/api/data/metar"
        params = {
            "ids": "",
            "format": "json",
            "taf": "false",
            "hours": "2",
            "bbox": f"{lon-2},{lat-2},{lon+2},{lat+2}",
        }
        try:
            resp = await self.client.get(url, params=params)
            resp.raise_for_status()
            reports = resp.json()
            if not reports:
                logger.warning(f"[{city}] 无 METAR 数据")
                return None

            # 取最新报告
            latest = max(reports, key=lambda r: r.get("obsTime", 0))
            temp = latest.get("temp")
            if temp is None:
                return None

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.db.store_actual(today, city, float(temp), source="metar")
            logger.info(f"  [{city}] 实况: {temp}°C")
            return float(temp)
        except Exception as e:
            logger.debug(f"[{city}] METAR 失败: {e}")
            return None

    async def fetch_all_actuals(self) -> Dict[str, float]:
        """获取所有城市实况温度"""
        results = {}
        for city in STATIONS:
            temp = await self.fetch_metar_actual(city)
            if temp is not None:
                results[city] = temp
            await asyncio.sleep(0.3)
        return results

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
