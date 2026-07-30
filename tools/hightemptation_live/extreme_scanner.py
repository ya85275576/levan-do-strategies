#!/usr/bin/env python3
"""
HighTempTation — ColdMath 风格极端低估扫描模块

核心策略（彩票式套利）:
  1. TAF 机场预报 → 解析 TX 最高温（与 Polymarket 未来结算日对应）
  2. 遍历 ICAO → TAF 预报最高温 → 匹配 Polymarket 温度桶 → 价格 < $0.03 触发
  3. $1-$5 微仓位, 靠 8200+ 次交易的大数定律, 少数命中覆盖全部亏损
  4. 独立运行：可被 hightemptation_live.py 导入，也可单独跑
  5. ENABLE_EXTREME_SCANNER 开关, 每日触发上限, DRY_RUN 先跑

参考: Polymarket 天气榜一 ColdMath 彩票式策略

用法:
  # 独立运行（单次扫描）
  ENABLE_EXTREME_SCANNER=true DRY_RUN=true python extreme_scanner.py

  # 持续循环扫描
  ENABLE_EXTREME_SCANNER=true python extreme_scanner.py --loop

  # 作为模块导入
  from extreme_scanner import ExtremeScanner
  scanner = ExtremeScanner(db_path="hightemptation.db")
  opportunities = await scanner.run_once()

环境变量:
  ENABLE_EXTREME_SCANNER  true/false (默认 true)
  EXTREME_PRICE_THRESHOLD  0.03        (极端低估价格阈值)
  EXTREME_MIN_SIZE         1.0         (最小仓位 $)
  EXTREME_MAX_SIZE         5.0         (最大仓位 $)
  EXTREME_DAILY_LIMIT      50          (每日触发上限)
  EXTREME_SCAN_INTERVAL    300         (扫描间隔秒)
  EXTREME_MIN_LIQUIDITY    100         (最低流动性 $)
  TARGET_DAY_OFFSET        1           (TAF 与结算日的偏移天数)
  DRY_RUN                  true/false  (默认 true)
  DB_PATH                  hightemptation.db
  GAMMA_API                https://gamma-api.polymarket.com
  TAF_API_BASE             https://aviationweather.gov/api/data/taf
"""
import asyncio
import json
import logging
import math
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import httpx

try:
    from tools.hightemptation_live.db_manager import TradeDB
except ImportError:
    # 独立运行时的回退
    from db_manager import TradeDB

logger = logging.getLogger("extreme_scanner")


# ════════════════════════════════════════════════════════════════
# ICAO 对照表 (与 services/hightemptation/bot.py 保持一致)
# (city, icao, lat, lon)
# ════════════════════════════════════════════════════════════════

STATIONS: dict[str, Tuple[str, float, float]] = {
    "Tokyo":         ("RJTT",  35.5494, 139.7798),
    "Seoul":         ("RKSS",  37.5583, 126.7906),
    "Singapore":     ("WSSS",   1.3502, 103.9944),
    "Hong Kong":     ("VHHH",  22.3080, 113.9185),
    "Shanghai":      ("ZSSS",  31.1979, 121.3363),
    "Bangkok":       ("VTBS",  13.6811, 100.7470),
    "Mumbai":        ("VABB",  19.0887,  72.8679),
    "Dubai":         ("OMDB",  25.2528,  55.3644),
    "Istanbul":      ("LTFM",  41.2613,  28.7419),
    "New York":      ("KNYC",  40.7789, -73.9692),
    "Los Angeles":   ("KLAX",  33.9416, -118.4085),
    "Chicago":       ("KORD",  41.9786, -87.9048),
    "Miami":         ("KMIA",  25.7932, -80.2906),
    "San Francisco": ("KSFO",  37.6188, -122.3756),
    "Toronto":       ("CYYZ",  43.6777, -79.6305),
    "Mexico City":   ("MMMX",  19.4361, -99.0720),
    "London":        ("EGLL",  51.4700,  -0.4543),
    "Paris":         ("LFPG",  49.0097,   2.5479),
    "Berlin":        ("EDDT",  52.5597,  13.2877),
    "Sydney":        ("YSSY", -33.9399, 151.1753),
}

# 城市别名 → 标准名（与主 bot 一致）
CITY_ALIASES: dict[str, str] = {
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
}


def normalize_city(raw: str) -> Optional[str]:
    """标准化城市名"""
    name = raw.strip()
    lower = name.lower()
    if name in STATIONS:
        return name
    if lower in CITY_ALIASES:
        return CITY_ALIASES[lower]
    # 直接匹配 key
    for k in STATIONS:
        if k.lower() == lower:
            return k
    return name if name else None


# ════════════════════════════════════════════════════════════════
# 环境变量
# ════════════════════════════════════════════════════════════════

ENABLE_EXTREME_SCANNER = os.environ.get("ENABLE_EXTREME_SCANNER", "true").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
DB_PATH = os.environ.get("DB_PATH", "hightemptation.db")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# 极端低估参数
EXTREME_PRICE_THRESHOLD = float(os.environ.get("EXTREME_PRICE_THRESHOLD", "0.03"))
EXTREME_MIN_SIZE = float(os.environ.get("EXTREME_MIN_SIZE", "1.0"))
EXTREME_MAX_SIZE = float(os.environ.get("EXTREME_MAX_SIZE", "5.0"))
EXTREME_DAILY_LIMIT = int(os.environ.get("EXTREME_DAILY_LIMIT", "50"))
EXTREME_MIN_LIQUIDITY = float(os.environ.get("EXTREME_MIN_LIQUIDITY", "100"))

# TAF 偏移天数: 0 = 当天, 1 = 次日（默认）
TARGET_DAY_OFFSET = int(os.environ.get("TARGET_DAY_OFFSET", "1"))

# API 端点
TAF_API_BASE = os.environ.get("TAF_API_BASE", "https://aviationweather.gov/api/data/taf")
GAMMA_API = os.environ.get("GAMMA_API", "https://gamma-api.polymarket.com")
SCAN_INTERVAL = int(os.environ.get("EXTREME_SCAN_INTERVAL", "300"))


# ════════════════════════════════════════════════════════════════
# 高斯 CDF (内联实现, 无 scipy 依赖)
# ════════════════════════════════════════════════════════════════

def _gaussian_cdf(x: float) -> float:
    """Abramowitz & Stegun 近似, 误差 < 1.5e-7"""
    if x < -8:
        return 0.0
    if x > 8:
        return 1.0
    a = [0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429]
    p = 0.3275911
    s = 1.0 if x >= 0 else -1.0
    t = 1.0 / (1.0 + p * abs(x))
    y = 1.0 - (((((a[4] * t + a[3]) * t) + a[2]) * t + a[1]) * t + a[0]) * t * math.exp(-x * x / 2)
    return y if s > 0 else 1.0 - y


def bucket_prob(lower: float, upper: float, mu: float, sigma: float = 2.0) -> float:
    """P(lower < X < upper) for normal distribution"""
    if sigma <= 0:
        return 1.0 if lower <= mu < upper else 0.0
    return max(0.0, min(1.0, _gaussian_cdf((upper - mu) / sigma) - _gaussian_cdf((lower - mu) / sigma)))


# ════════════════════════════════════════════════════════════════
# ExtremeScanner 类
# ════════════════════════════════════════════════════════════════

class ExtremeScanner:
    """
    ColdMath 风格极端低估扫描器。

    核心逻辑:
      1. 对每个 ICAO 站, 获取 TAF 预报 → 解析 TX (最高温)
      2. 通过 Gamma API 搜索匹配该城市+日期的温度市场
      3. 筛选 YES 价格 < $0.03 (EXTREME_PRICE_THRESHOLD) 的极端低估市场
      4. 下 $1-$5 微仓位买入 YES（彩票式）
      5. 每日触发上限 EXTREME_DAILY_LIMIT

    设计哲学（ColdMath 彩票式套利）:
      - 极低价格买入 (<3¢) 意味着极小的下行风险
      - 少数命中（例如 1/50）即可覆盖全部亏损并盈利
      - 依靠 8000+ 次交易的大数定律
      - TAF 比 METAR 更适合预报未来的结算温度
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db = TradeDB(db_path)
        self._client: Optional[httpx.AsyncClient] = None

        # 日计数器
        self.daily_trigger_count = 0
        self.last_reset_date = ""

        # 全局统计
        self.total_trades = 0
        self.total_pnl = 0.0
        self.total_yes_bought = 0.0
        self._running = False

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    # ════════════════════════════════════════════════════════════════
    # 1. TAF 数据获取与解析
    # ════════════════════════════════════════════════════════════════

    async def fetch_taf(self, icao: str) -> Optional[dict]:
        """
        从 aviationweather.gov 获取单个 ICAO 站的 TAF 预报。

        TAF JSON 返回格式（aviationweather.gov API）:
        [
            {
                "icaoId": "RJTT",
                "bulletinTime": "2025-07-29T00:00:00Z",
                "validTimeFrom": "2025-07-29T00:00:00Z",
                "validTimeTo": "2025-07-30T00:00:00Z",
                "rawTAF": "TAF RJTT ... TX28/15Z...",
                "maxTemp": 28,
                "maxTempTime": "2025-07-29T15:00:00Z",
                "minTemp": 22,
                ...
            }
        ]

        :param icao: 4字母 ICAO 代码
        :returns: 解析后的 TAF dict 或 None
        """
        params = {
            "ids": icao,
            "format": "json",
        }
        try:
            resp = await self.client.get(TAF_API_BASE, params=params)
            resp.raise_for_status()
            body = resp.json()

            # TAF API 返回列表（即使只查一个站）
            reports = body if isinstance(body, list) else [body]
            if not reports:
                logger.debug(f"  [{icao}] 无 TAF 数据")
                return None

            # 取最新的一份预报
            report = max(reports, key=lambda r: r.get("bulletinTime", ""))

            icao_id = report.get("icaoId", "").upper()
            if icao_id != icao.upper():
                logger.debug(f"  [{icao}] ICAO 不匹配: {icao_id}")
                return None

            # 解析温度
            max_temp = self._parse_temperature(report, "maxTemp")
            min_temp = self._parse_temperature(report, "minTemp")
            raw_taf = report.get("rawTAF", "")

            # JSON 中没有直出字段时，回退到文本解析 TX/TN
            if max_temp is None:
                max_temp = self._parse_tx_from_taf(raw_taf)
            if min_temp is None:
                min_temp = self._parse_tn_from_taf(raw_taf)

            if max_temp is None:
                logger.debug(f"  [{icao}] 无法解析 TX 最高温度")
                return None

            result = {
                "icao": icao,
                "bulletin_time": report.get("bulletinTime", ""),
                "valid_from": report.get("validTimeFrom", ""),
                "valid_to": report.get("validTimeTo", ""),
                "max_temp": max_temp,
                "min_temp": min_temp,
                "raw_taf": raw_taf[:500],  # 截断保存
            }

            logger.info(f"  [{icao}] TAF TX={max_temp}°C, TN={min_temp}")
            return result

        except httpx.HTTPStatusError as e:
            logger.warning(f"  [{icao}] TAF HTTP {e.response.status_code}")
            return None
        except httpx.RequestError as e:
            logger.debug(f"  [{icao}] TAF 连接失败: {e}")
            return None
        except Exception as e:
            logger.debug(f"  [{icao}] TAF 解析异常: {e}")
            return None

    @staticmethod
    def _parse_temperature(report: dict, key: str) -> Optional[float]:
        """从 TAF JSON 中解析温度字段"""
        val = report.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _taf_temp_to_float(temp_str: str) -> Optional[float]:
        """
        将 TAF 温度字符串转为浮点数。

        TAF 使用 M 前缀表示负值（METAR 习惯）:
          M02 → -2°C
          M10 → -10°C
          28  → 28°C
        """
        if not temp_str:
            return None
        temp_str = temp_str.strip()
        if temp_str.startswith('M'):
            return -float(temp_str[1:])
        if temp_str.startswith('-'):
            return -float(temp_str[1:])
        try:
            return float(temp_str)
        except ValueError:
            return None

    @staticmethod
    def _parse_tx_from_taf(raw_taf: str) -> Optional[float]:
        """
        从 TAF 原始文本中解析 TX（最高温）。

        TAF 温度编码格式:
          TX28/1522Z → 最高 28°C, 在 15日22Z
          TXM02/1522Z → 最高 -2°C (M 前缀表示负数)
        """
        if not raw_taf:
            return None
        # 标准 TX 格式: TX28/1522Z
        m = re.search(r'TX\s*([A-Z]?\d{2})\s*/', raw_taf)
        if m:
            return ExtremeScanner._taf_temp_to_float(m.group(1))
        # 无 / 的变体
        m = re.search(r'TX\s*([A-Z]?\d{2})(?:\s|$)', raw_taf)
        if m:
            return ExtremeScanner._taf_temp_to_float(m.group(1))
        # 备用: maxT / MAX text
        m = re.search(r'(?:maxT|MAX)\D*(\d{2})', raw_taf, re.IGNORECASE)
        if m:
            return ExtremeScanner._taf_temp_to_float(m.group(1))
        return None

    @staticmethod
    def _parse_tn_from_taf(raw_taf: str) -> Optional[float]:
        """从 TAF 原始文本中解析 TN（最低温）"""
        if not raw_taf:
            return None
        m = re.search(r'TN\s*([A-Z]?\d{2})\s*/', raw_taf)
        if m:
            return ExtremeScanner._taf_temp_to_float(m.group(1))
        m = re.search(r'TN\s*([A-Z]?\d{2})(?:\s|$)', raw_taf)
        if m:
            return ExtremeScanner._taf_temp_to_float(m.group(1))
        m = re.search(r'(?:minT|MIN)\D*(\d{2})', raw_taf, re.IGNORECASE)
        if m:
            return ExtremeScanner._taf_temp_to_float(m.group(1))
        return None

    # ════════════════════════════════════════════════════════════════
    # 2. Polymarket 市场查询
    # ════════════════════════════════════════════════════════════════

    async def search_markets_for_city(self, city: str, target_date: str) -> List[dict]:
        """
        通过 Gamma API 搜索指定城市+目标日的所有活跃温度市场。

        分两步:
          1. 从 /events 按 weather tag 拉取事件列表
          2. 从 /markets 用 question__icontains 补充

        :param city: 城市名 (如 "Tokyo")
        :param target_date: 目标日期 YYYY-MM-DD
        :returns: 匹配的市场列表
        """
        seen_ids: set[str] = set()
        matched_markets: List[dict] = []

        # ── 方法1: /events 搜索 ──
        try:
            resp = await self.client.get(
                f"{GAMMA_API}/events",
                params={
                    "tag": "weather",
                    "limit": 100,
                    "closed": False,
                    "active": True,
                },
                timeout=20,
            )
            resp.raise_for_status()
            events = resp.json()
            for evt in events:
                title = evt.get("title", "").lower()
                slug = evt.get("slug", "").lower()
                if city.lower() not in title and city.lower() not in slug:
                    continue
                evt_markets = evt.get("markets", [])
                for m in evt_markets:
                    mid = str(m.get("conditionId", "") or m.get("id", ""))
                    if mid and mid not in seen_ids:
                        q = str(m.get("question", "") or "").lower()
                        if city.lower() in q or "temp" in q or "°c" in q:
                            seen_ids.add(mid)
                            matched_markets.append(m)
        except Exception as e:
            logger.debug(f"  /events 搜索失败 ({city}): {e}")

        # ── 方法2: /markets question__icontains 补充 ──
        try:
            resp = await self.client.get(
                f"{GAMMA_API}/markets",
                params={
                    "question__icontains": city,
                    "closed": False,
                    "active": True,
                    "limit": 100,
                },
                timeout=20,
            )
            resp.raise_for_status()
            markets = resp.json()
            for m in markets:
                mid = str(m.get("conditionId", "") or m.get("id", ""))
                if mid and mid not in seen_ids:
                    q = str(m.get("question", "") or "").lower()
                    if any(kw in q for kw in ["temperature", "temp", "°c", "high"]):
                        seen_ids.add(mid)
                        matched_markets.append(m)
        except Exception as e:
            logger.debug(f"  /markets 搜索失败 ({city}): {e}")

        if not matched_markets:
            return []

        logger.debug(f"  [{city}] 原始匹配: {len(matched_markets)} 个市场")

        # ── 按目标日期过滤 ──
        date_filtered = []
        for m in matched_markets:
            q = str(m.get("question", "") or "")
            # 精确日期匹配
            plain = target_date.replace("-", "")
            if plain in q:
                date_filtered.append(m)
                continue
            # 模糊日期: 用 dateutil 或正则
            month_day = target_date[5:]  # "MM-DD"
            if month_day in q:
                date_filtered.append(m)
                continue
            # 检查 "on July 29" 等格式
            try:
                from dateutil import parser as dateparser
                dt = dateparser.parse(q, fuzzy=True)
                if dt and dt.strftime("%Y-%m-%d") == target_date:
                    date_filtered.append(m)
                    continue
            except Exception:
                pass

        # 日期过滤不产生结果时，放宽返回全部
        if date_filtered:
            logger.debug(f"  [{city}] 日期匹配: {len(date_filtered)}/{len(matched_markets)}")
            return date_filtered

        return matched_markets

    @staticmethod
    def _parse_market_bucket(question: str) -> Optional[Tuple[float, float, str]]:
        """
        从市场问题中解析温度桶。

        Returns:
            (lower, upper, label) 或 None
            对于 "below 25°C" → (-inf, 25, "≤25°C")
            对于 "above 35°C" → (35, +inf, "≥35°C")
            对于 "between 30°C and 32°C" → (30, 32, "30-32°C")
            对于 "will be 25°C" → (24.5, 25.5, "25°C")
        """
        q = question.lower()
        nums = [float(x) for x in re.findall(r'(\d+)\s*°', question)]
        if not nums:
            # 无 ° 符号时尝试纯数字
            nums = [float(x) for x in re.findall(r'\b(\d+)\b', question) if 10 <= float(x) <= 50]

        if not nums:
            return None

        is_range = any(kw in q for kw in ["between", "-°c", "–°c"])
        is_below = any(kw in q for kw in ["below", "under", "<", "≤", "or less"])
        is_above = any(kw in q for kw in ["above", "over", "exceed", ">", "≥", "or more",
                                           "at least"])

        if len(nums) >= 2 and is_range:
            lo, hi = min(nums[0], nums[1]), max(nums[0], nums[1])
            return (lo, hi, f"{lo:.0f}-{hi:.0f}°C")
        elif is_below:
            return (float("-inf"), nums[0], f"≤{nums[0]:.0f}°C")
        elif is_above:
            return (nums[0], float("inf"), f"≥{nums[0]:.0f}°C")
        else:
            # 单值桶: 映射为 ±0.5°C 范围
            return (nums[0] - 0.5, nums[0] + 0.5, f"{nums[0]:.0f}°C")

    @staticmethod
    def _extract_city_from_question(question: str) -> Optional[str]:
        """从市场问题中提取城市名"""
        q = question.lower()
        for c in STATIONS:
            if c.lower() in q:
                return c
        for alias, std in CITY_ALIASES.items():
            if alias in q:
                return std
        return None

    # ════════════════════════════════════════════════════════════════
    # 3. 极端低估扫描核心
    # ════════════════════════════════════════════════════════════════

    async def scan_extreme_opportunities(self) -> List[dict]:
        """
        核心扫描管道:
          对每个城市 → TAF 预报 → 匹配市场 → 筛选 <3¢ 极端低估

        Returns:
            按预期价值降序排列的机会列表
        """
        target_date = (datetime.now(timezone.utc) + timedelta(days=TARGET_DAY_OFFSET)).strftime("%Y-%m-%d")
        opportunities: List[dict] = []

        for city, (icao, lat, lon) in STATIONS.items():
            # ── 步骤1: TAF 预报 ──
            taf = await self.fetch_taf(icao)
            await asyncio.sleep(0.25)  # aviationweather.gov 限速

            if not taf or taf["max_temp"] is None:
                logger.debug(f"  [{city}] 无 TAF 数据，跳过")
                continue

            forecast_temp = taf["max_temp"]

            # ── 步骤2: 搜索 Polymarket 市场 ──
            markets = await self.search_markets_for_city(city, target_date)
            await asyncio.sleep(0.25)

            if not markets:
                logger.debug(f"  [{city}] 无匹配市场")
                continue

            # ── 步骤3: 遍历市场，寻找极端低估 ──
            city_opps = []
            for m in markets:
                try:
                    question = str(m.get("question", "") or m.get("title", ""))
                    bucket = self._parse_market_bucket(question)
                    if not bucket:
                        continue

                    lower, upper, label = bucket

                    # 检查 TAF 预报温度是否落在桶内或附近
                    temp_in_bucket = (lower <= forecast_temp <= upper) if lower != float("-inf") and upper != float("inf") else True

                    # 对于 "below" 类型: 预报温度应 ≤ 阈值
                    if lower == float("-inf") and forecast_temp > upper:
                        continue
                    # 对于 "above" 类型: 预报温度应 ≥ 阈值
                    if upper == float("inf") and forecast_temp < lower:
                        continue

                    # 获取价格
                    op = m.get("outcomePrices", "[]")
                    op_parsed = json.loads(op) if isinstance(op, str) else op
                    yes_price = float(op_parsed[0]) if len(op_parsed) > 0 else 0.5
                    no_price = float(op_parsed[1]) if len(op_parsed) > 1 else 0.5

                    # ★ 核心: 只有极端低估才触发
                    if yes_price >= EXTREME_PRICE_THRESHOLD:
                        continue

                    # 流动性检查
                    liquidity = float(str(m.get("liquidity", "0") or "0"))
                    if liquidity < EXTREME_MIN_LIQUIDITY:
                        continue

                    # 提取 token ID
                    tokens = m.get("tokens", [])
                    yes_token_id = ""
                    no_token_id = ""
                    for t in tokens:
                        outcome = t.get("outcome", "").upper()
                        if outcome == "YES":
                            yes_token_id = str(t.get("token_id", ""))
                        elif outcome == "NO":
                            no_token_id = str(t.get("token_id", ""))

                    ev = (1.0 - yes_price) / yes_price if yes_price > 0 else float("inf")

                    opp = {
                        "city": city,
                        "icao": icao,
                        "question": question,
                        "bucket_lower": lower,
                        "bucket_upper": upper,
                        "bucket_label": label,
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "forecast_temp": forecast_temp,
                        "forecast_min": taf.get("min_temp"),
                        "target_date": target_date,
                        "liquidity": liquidity,
                        "condition_id": str(m.get("conditionId", "") or m.get("id", "")),
                        "condition_id_fallback": str(m.get("id", "")),
                        "yes_token_id": yes_token_id,
                        "no_token_id": no_token_id,
                        "edge": 1.0 - yes_price,
                        "expected_value": ev,
                    }

                    city_opps.append(opp)
                    logger.info(f"  🎯 [{city}] {label} YES=${yes_price:.4f} "
                                f"TAF={forecast_temp}°C, EV={ev:.0f}x, "
                                f"liq=${liquidity:.0f}")

                except Exception as e:
                    logger.debug(f"  [{city}] 市场解析异常: {e}")
                    continue

            opportunities.extend(city_opps)

        # 按预期价值 (EV) 降序排列
        opportunities.sort(key=lambda x: x["expected_value"], reverse=True)
        logger.info(f"📊 极端低估扫描完成: {len(opportunities)} 个机会, "
                    f"目标日期={target_date}")
        return opportunities

    # ════════════════════════════════════════════════════════════════
    # 4. 仓位管理 & 开仓执行
    # ════════════════════════════════════════════════════════════════

    def _reset_daily_counter(self):
        """重置日计数器（跨日时）"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.last_reset_date:
            self.daily_trigger_count = 0
            self.last_reset_date = today
            logger.info(f"📅 日计数器重置: {today}")

    @staticmethod
    def _get_position_size(opp: dict) -> float:
        """
        彩票式仓位计算: $1-$5 微仓。

        ColdMath 风格:
          - 价格越低 (预期价值越高), 仓位略大
          - 原则: 极小单笔, 靠数量
          - 价格 1¢ → $5, 价格 2¢ → $3, 价格 3¢ → $1
        """
        yes_price = opp["yes_price"]
        ev = opp["expected_value"]

        # 基于价格分档
        if yes_price < 0.01:
            mult = 1.0       # <1¢ → $5
        elif yes_price < 0.02:
            mult = 0.7       # 1-2¢ → ~$3.8
        elif yes_price < 0.025:
            mult = 0.5       # 2-2.5¢ → ~$3
        else:
            mult = 0.25      # 2.5-3¢ → ~$2

        # EV 乘数修正: EV > 100x 时加大, EV < 10x 时减小
        if ev > 100:
            mult = min(1.0, mult * 1.2)
        elif ev < 20:
            mult = mult * 0.7
        elif ev < 10:
            mult = mult * 0.4

        size = EXTREME_MIN_SIZE + (EXTREME_MAX_SIZE - EXTREME_MIN_SIZE) * mult
        return round(size, 2)

    async def execute_opportunity(self, opp: dict) -> Optional[dict]:
        """
        执行一笔极端低估交易。

        流程:
          1. 检查每日限额
          2. 计算仓位大小
          3. 写入 DB
          4. (实盘) 挂限价单

        Returns:
            position dict 或 None (被限额或 DB 失败)
        """
        self._reset_daily_counter()
        if self.daily_trigger_count >= EXTREME_DAILY_LIMIT:
            logger.warning(f"  ⛔ 每日触发达上限 ({EXTREME_DAILY_LIMIT})")
            return None

        size = self._get_position_size(opp)

        # 确保 token_id 非空
        token_id = opp["condition_id"] or opp["condition_id_fallback"] or f"extreme_{opp['city']}_{opp['target_date']}"

        position = {
            "city": opp["city"],
            "side": "YES",
            "entry_price": opp["yes_price"],
            "size_usd": size,
            "contracts": size / opp["yes_price"] if opp["yes_price"] > 0 else 0,
            "bucket_lower": opp["bucket_lower"],
            "bucket_upper": opp["bucket_upper"],
            "bucket_label": opp["bucket_label"],
            "forecast_temp": opp["forecast_temp"],
            "target_date": opp["target_date"],
            "expected_value": opp["expected_value"],
            "condition_id": token_id,
            "yes_token_id": opp["yes_token_id"],
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "signal_type": "EXTREME_TAF",
            "dry_run": DRY_RUN,
        }

        # 写入 DB
        trade_id = self.db.open_trade(
            token_id=token_id,
            city=opp["city"],
            bucket_lower=opp["bucket_lower"],
            bucket_upper=opp["bucket_upper"],
            side="YES",
            entry_price=opp["yes_price"],
            size=size,
        )

        if trade_id is None:
            logger.error(f"  ❌ [{opp['city']}] DB 开仓失败")
            return None

        position["trade_id"] = trade_id
        self.daily_trigger_count += 1
        self.total_trades += 1
        self.total_yes_bought += size

        logger.info(f"  🟢 [EXTREME#{self.total_trades}] {opp['city']} {opp['bucket_label']} "
                    f"YES @ ${opp['yes_price']:.4f} x${size:.2f} "
                    f"(TAF={opp['forecast_temp']}°C, EV={opp['expected_value']:.0f}x)")

        if not DRY_RUN:
            logger.info(f"  → 实盘执行: condition={token_id}, "
                        f"金额=${size:.2f}")
            # TODO: 集成 Polymarket CLOB API 挂限价买单
            # 参考 limit_order_executor.py
            pass

        return position

    # ════════════════════════════════════════════════════════════════
    # 5. 持仓监控与出场
    # ════════════════════════════════════════════════════════════════

    def check_extreme_positions(self) -> List[dict]:
        """
        检查已开仓的极端低估持仓，寻找出场机会。

        出场规则:
          - 价格涨到 $0.50+ (50倍+) 或结算时 > $0.90: 止盈
          - 结算日后: 强制平仓
          - 持仓 > 7天: 时间止损

        Returns:
            需要平仓的 trade_id 列表
        """
        open_trades = self.db.get_open_trades()
        to_close: List[dict] = []
        now = datetime.now(timezone.utc)

        for t in open_trades:
            # 只处理极端扫描开仓的 YES 仓位
            if t.get("side") != "YES":
                continue

            entry_price = t["entry_price"]
            # 获取最新价格
            latest = self.db.get_latest_market(
                t.get("city", ""),
                t.get("bucket_lower", 0),
                t.get("bucket_upper", 0),
            )
            if not latest:
                continue

            current_yes = latest.get("yes_price", entry_price)

            # 止盈: 涨到 50¢+ (16x+ 回报)
            if current_yes >= 0.50:
                to_close.append({
                    "trade_id": t["id"],
                    "exit_price": current_yes,
                    "reason": "ExtremeTP",
                    "pnl": (current_yes - entry_price) * t["size"],
                })
                continue

            # 接近结算: 涨到 90¢+
            if current_yes >= 0.90:
                to_close.append({
                    "trade_id": t["id"],
                    "exit_price": current_yes,
                    "reason": "ExtremeSettlementTP",
                    "pnl": (current_yes - entry_price) * t["size"],
                })
                continue

            # 时间止损: 持仓 > 7 天
            try:
                entry_time = datetime.fromisoformat(t["entry_time"])
                if (now - entry_time).days >= 7:
                    to_close.append({
                        "trade_id": t["id"],
                        "exit_price": current_yes,
                        "reason": "ExtremeTimeStop",
                        "pnl": (current_yes - entry_price) * t["size"],
                    })
                    continue
            except (ValueError, TypeError):
                pass

        return to_close

    def close_extreme_trade(self, trade_id: int, exit_price: float, reason: str = "") -> bool:
        """平仓一笔极端低估持仓"""
        ok = self.db.close_trade(trade_id, exit_price, reason)
        if ok:
            # 重新读取已更新的交易记录以获取 PnL
            row = self.db.conn.execute(
                "SELECT pnl, size, entry_price FROM trades WHERE id=?", (trade_id,)
            ).fetchone()
            if row:
                pnl = row["pnl"] or 0.0
                self.total_pnl += pnl
                mult = (exit_price / row["entry_price"]) if row["entry_price"] > 0 else 0
                logger.info(f"  🔴 [EXTREME] 平仓 trade#{trade_id} {reason}: "
                            f"PnL=${pnl:.2f}, 倍数={mult:.1f}x")
        return ok

    # ════════════════════════════════════════════════════════════════
    # 6. 统计报告
    # ════════════════════════════════════════════════════════════════

    def print_stats(self):
        """输出 ColdMath 风格统计"""
        open_count = len(self.db.get_open_trades())
        total_closed = self.db.conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(pnl),0) as total_pnl, "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) as losses "
            "FROM trades WHERE status='closed'"
        ).fetchone()

        if total_closed:
            cnt = total_closed["cnt"] or 0
            total = total_closed["total_pnl"] or 0.0
            wins = total_closed["wins"] or 0
            losses = total_closed["losses"] or 0
            wr = wins / cnt * 100 if cnt > 0 else 0

            logger.info("=" * 60)
            logger.info("  🎰 ColdMath 极端低估扫描器 — 统计")
            logger.info(f"  总交易: {self.total_trades} (已平仓: {cnt})")
            logger.info(f"  总 PnL: ${total:.2f}")
            logger.info(f"  胜率: {wr:.1f}% ({wins}胜/{losses}负)")
            logger.info(f"  当前持仓: {open_count}")
            logger.info(f"  总投入: ${self.total_yes_bought:.2f}")
            if total != 0 and self.total_yes_bought > 0:
                roi = total / self.total_yes_bought * 100
                logger.info(f"  ROI: {roi:.1f}%")
            logger.info("=" * 60)

    # ════════════════════════════════════════════════════════════════
    # 7. 主运行管道
    # ════════════════════════════════════════════════════════════════

    async def run_once(self) -> List[dict]:
        """
        单次扫描运行。

        步骤:
          1. 获取 TAF 预报
          2. 搜索匹配市场
          3. 筛选 <3¢ 极端低估
          4. 开仓
          5. 检查已有持仓出场
          6. 输出统计

        Returns:
            所有成功开仓的 position 列表
        """
        if not ENABLE_EXTREME_SCANNER:
            logger.info("⏸️  Extreme Scanner 未启用 (ENABLE_EXTREME_SCANNER=false)")
            return []

        logger.info("=" * 60)
        logger.info("  🎰 ColdMath 风格极端低估扫描器 v1.0")
        logger.info(f"  价格阈值: < ${EXTREME_PRICE_THRESHOLD:.2f}")
        logger.info(f"  仓位范围: ${EXTREME_MIN_SIZE:.0f}-${EXTREME_MAX_SIZE:.0f}")
        logger.info(f"  每日上限: {EXTREME_DAILY_LIMIT} 次")
        logger.info(f"  TAF 偏移: {TARGET_DAY_OFFSET}d")
        logger.info(f"  最小流动性: ${EXTREME_MIN_LIQUIDITY:.0f}")
        logger.info(f"  运行模式: {'🟢 实盘' if not DRY_RUN else '🟡 模拟'}")
        logger.info("=" * 60)

        # 扫描机会
        opportunities = await self.scan_extreme_opportunities()

        # 开仓 (不超过日限额)
        opened: List[dict] = []
        for opp in opportunities:
            if self.daily_trigger_count >= EXTREME_DAILY_LIMIT:
                break
            pos = await self.execute_opportunity(opp)
            if pos:
                opened.append(pos)

        # 报告
        if opened:
            total_usd = sum(p["size_usd"] for p in opened)
            avg_ev = sum(p["expected_value"] for p in opened) / len(opened)
            logger.info(f"  📊 本轮开仓: {len(opened)} 笔, "
                        f"总计 ${total_usd:.2f}, "
                        f"平均 EV={avg_ev:.0f}x")
        else:
            logger.info("  📊 本轮无符合条件的极端低估机会")

        # 检查已有持仓出场
        to_close = self.check_extreme_positions()
        for tc in to_close:
            self.close_extreme_trade(tc["trade_id"], tc["exit_price"], tc["reason"])

        # 统计
        self.print_stats()

        return opened

    async def run_loop(self):
        """持续扫描循环"""
        if not ENABLE_EXTREME_SCANNER:
            logger.info("⏸️  Extreme Scanner 未启用")
            return

        self._running = True
        logger.info(f"🔁 极端低估扫描循环启动 (间隔={SCAN_INTERVAL}s)")

        while self._running:
            try:
                await self.run_once()
            except Exception as e:
                logger.error(f"扫描异常: {e}", exc_info=True)

            for _ in range(SCAN_INTERVAL):
                if not self._running:
                    break
                await asyncio.sleep(1)

        logger.info("⏹️  极端扫描循环已停止")

    async def stop(self):
        """停止扫描循环"""
        self._running = False

    async def close(self):
        """释放资源"""
        if self._client:
            await self._client.aclose()
            self._client = None
        self.db.close()


# ════════════════════════════════════════════════════════════════
# 独立运行入口
# ════════════════════════════════════════════════════════════════

async def main():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    scanner = ExtremeScanner()
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--loop":
            await scanner.run_loop()
        else:
            await scanner.run_once()
    except KeyboardInterrupt:
        await scanner.stop()
    finally:
        await scanner.close()


if __name__ == "__main__":
    asyncio.run(main())
