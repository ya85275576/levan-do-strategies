#!/usr/bin/env python3
"""
HighTempTation — 自动从 Gamma API 发现天气市场

功能:
  1. 搜索 Polymarket Gamma API 获取所有活跃天气事件
  2. 解析城市/日期/温度桶, 提取 token_id
  3. 存入 market_tokens 表（扩展 db_manager）
  4. 增量更新（只处理新事件）

用法:
  python update_market_slugs.py --db hightemptation.db
  python update_market_slugs.py --db hightemptation.db --dry-run
"""
import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("market_slugs")

GAMMA_API = "https://gamma-api.polymarket.com"

# 天气城市关键词 → 标准化城市名
CITY_MAP = {
    "tokyo": "Tokyo", "seoul": "Seoul", "singapore": "Singapore",
    "hong kong": "Hong Kong", "shanghai": "Shanghai", "bangkok": "Bangkok",
    "mumbai": "Mumbai", "dubai": "Dubai", "istanbul": "Istanbul",
    "new york": "New York", "los angeles": "Los Angeles", "chicago": "Chicago",
    "miami": "Miami", "san francisco": "San Francisco",
    "toronto": "Toronto", "mexico city": "Mexico City",
    "london": "London", "paris": "Paris", "berlin": "Berlin",
    "sydney": "Sydney",
}


def create_tokens_table(db_path: str):
    """扩展 DB: 创建 market_tokens 表"""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id TEXT NOT NULL UNIQUE,
            condition_id TEXT,
            event_slug TEXT,
            question TEXT,
            city TEXT,
            date TEXT,
            bucket_lower REAL,
            bucket_upper REAL,
            outcome TEXT CHECK(outcome IN ('YES','NO')),
            discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
            active INTEGER DEFAULT 1
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_city ON market_tokens(city)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_active ON market_tokens(active)")
    conn.commit()
    conn.close()


def parse_weather_question(question: str) -> Optional[Tuple[str, str, float, float]]:
    """
    解析天气问题字符串。

    "Will the high temperature in Tokyo on 2025-04-15 be ≥ 25°C?"
    → ("Tokyo", "2025-04-15", 25, inf)

    "Will the high temperature in London on 2025-04-15 be ≥ 20°C and < 25°C?"
    → ("London", "2025-04-15", 20, 25)
    """
    q = question.lower()
    # 找城市
    city = None
    for kw, norm in CITY_MAP.items():
        if kw in q:
            city = norm
            break
    if not city:
        return None

    # 找日期 YYYY-MM-DD
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", q)
    if not date_match:
        return None
    date_str = date_match.group(1)

    # 找温度区间: ≥X and <Y 或 ≥X
    bucket_lower = 0.0
    bucket_upper = float("inf")

    range_match = re.search(r">=\s*(\d+)\s*°?\s*(?:c|f)?\s*and\s*<\s*(\d+)", q)
    if range_match:
        bucket_lower = float(range_match.group(1))
        bucket_upper = float(range_match.group(2))
    else:
        gte_match = re.search(r">=\s*(\d+)", q)
        if gte_match:
            bucket_lower = float(gte_match.group(1))

    return city, date_str, bucket_lower, bucket_upper


async def discover_weather_markets(db_path: str, dry_run: bool = False,
                                    limit: int = 200) -> List[dict]:
    """
    从 Gamma API 发现天气市场。

    搜索条件: tag=weather, 活跃事件, 按手续费排序。
    """
    create_tokens_table(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 分页搜索活跃天气事件
        all_markets = []
        cursor = None

        for page in range(5):  # 最多 5 页
            params = {
                "tag": "weather",
                "limit": "50",
                "state": "active",
                "closed": "false",
            }
            if cursor:
                params["cursor"] = cursor

            try:
                resp = await client.get(f"{GAMMA_API}/events", params=params)
                resp.raise_for_status()
                events = resp.json()
                if not events:
                    break

                for event in events:
                    slug = event.get("slug", "")
                    markets = event.get("markets", [])
                    for m in markets:
                        question = m.get("question", "")
                        parsed = parse_weather_question(question)
                        if not parsed:
                            continue
                        city, date_str, bl, bu = parsed

                        tokens = m.get("tokens") or []
                        for token in tokens:
                            token_id = token.get("token_id", "")
                            outcome = token.get("outcome", "")
                            if not token_id:
                                continue

                            market_info = {
                                "token_id": token_id,
                                "condition_id": m.get("condition_id", ""),
                                "event_slug": slug,
                                "question": question,
                                "city": city,
                                "date": date_str,
                                "bucket_lower": bl,
                                "bucket_upper": bu,
                                "outcome": outcome,
                            }
                            all_markets.append(market_info)

                cursor = events[-1].get("slug", "")
                await asyncio.sleep(0.3)

            except httpx.HTTPError as e:
                logger.warning(f"API 请求失败 (page {page}): {e}")
                break

    logger.info(f"发现 {len(all_markets)} 个天气市场")

    if dry_run:
        conn.close()
        return all_markets

    # 写入 DB
    new_count = 0
    for m in all_markets:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO market_tokens
                (token_id, condition_id, event_slug, question, city, date,
                 bucket_lower, bucket_upper, outcome)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (m["token_id"], m["condition_id"], m["event_slug"],
                  m["question"], m["city"], m["date"],
                  m["bucket_lower"], m["bucket_upper"], m["outcome"]))
            if conn.total_changes > 0:
                new_count += 1
        except Exception as e:
            logger.warning(f"写入失败: {e}")

    conn.commit()
    conn.close()
    logger.info(f"新增 {new_count} 个市场 (共 {len(all_markets)} 个)")
    return all_markets


async def main():
    parser = argparse.ArgumentParser(description="发现 Polymarket 天气市场")
    parser.add_argument("--db", default="hightemptation.db", help="数据库路径")
    parser.add_argument("--dry-run", action="store_true", help="仅打印不写入")
    args = parser.parse_args()

    markets = await discover_weather_markets(args.db, dry_run=args.dry_run)
    if args.dry_run:
        print(f"\n发现 {len(markets)} 个市场:")
        for m in markets[:10]:
            print(f"  {m['city']:15s} {m['date']}  {m['bucket_lower']:>3.0f}-{m['bucket_upper']:>3.0f}°C  "
                  f"{m['outcome']:>3s}  token={m['token_id'][:12]}...  {m['question'][:60]}")
        if len(markets) > 10:
            print(f"  ... 还有 {len(markets)-10} 个")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
