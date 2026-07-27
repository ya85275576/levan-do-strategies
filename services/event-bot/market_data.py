#!/usr/bin/env python3
"""
OKX 事件合約市場數據模塊

透過 okx CLI 查詢事件合約行情數據和底層資產價格。
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("event-bot.market_data")


class EventMarketData:
    """
    事件合約市場數據查詢

    使用 okx CLI 的 market 和 event 子命令查詢數據。
    """

    def __init__(self, profile: str = "demo"):
        self.profile = profile

    async def get_ticker(self, inst_id: str) -> dict:
        """
        獲取事件合約行情（買一/賣一價、最新價等）

        :param inst_id: 合約 ID（如 BTC-UPDOWN-5MIN-xxxx）
        :returns: ticker 字典，包含 askPx, bidPx, last, ts 等欄位
        """
        cmd = [
            "okx", "market", "ticker", inst_id,
            "--json",
            "--profile", self.profile,
        ]
        result = await self._run_cmd(cmd, f"獲取 {inst_id} 行情")
        if result.get("status") != "success":
            return {}
        try:
            data = json.loads(result["stdout"])
            if isinstance(data, list) and len(data) > 0:
                return data[0]
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            return {}

    async def get_browse(self, series_id: Optional[str] = None) -> list:
        """
        瀏覽活躍事件合約

        :param series_id: 可選，按系列篩選
        :returns: 合約列表，包含 seriesId, contracts[] 等
        """
        cmd = ["okx", "event", "browse", "--json", "--profile", self.profile]
        if series_id:
            cmd = ["okx", "event", "browse", series_id, "--json", "--profile", self.profile]
        result = await self._run_cmd(cmd, "瀏覽事件合約")
        if result.get("status") != "success":
            return []
        try:
            data = json.loads(result["stdout"])
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError, TypeError):
            return []

    async def get_series(self) -> list:
        """
        獲取所有事件合約系列

        :returns: 系列列表
        """
        cmd = ["okx", "event", "series", "--json", "--profile", self.profile]
        result = await self._run_cmd(cmd, "獲取事件系列")
        if result.get("status") != "success":
            return []
        try:
            data = json.loads(result["stdout"])
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError, TypeError):
            return []

    async def get_markets(self, series_id: str, state: str = "live") -> list:
        """
        獲取事件合約市場列表

        :param series_id: 系列 ID
        :param state: 狀態（live / expired / preopen）
        :returns: 合約市場列表
        """
        cmd = [
            "okx", "event", "markets", series_id,
            "--state", state,
            "--json",
            "--profile", self.profile,
        ]
        result = await self._run_cmd(cmd, f"獲取 {series_id} 市場 ({state})")
        if result.get("status") != "success":
            return []
        try:
            data = json.loads(result["stdout"])
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError, TypeError):
            return []

    async def get_underlying_price(self, underlying_symbol: str) -> Optional[float]:
        """
        獲取底層資產最新價格（如 BTC-USDT）

        :param underlying_symbol: 底層資產交易對（如 BTC-USDT）
        :returns: 最新價格，失敗返回 None
        """
        cmd = [
            "okx", "market", "ticker", underlying_symbol,
            "--json",
            "--profile", self.profile,
        ]
        result = await self._run_cmd(cmd, f"獲取 {underlying_symbol} 價格")
        if result.get("status") != "success":
            return None
        try:
            data = json.loads(result["stdout"])
            if isinstance(data, list) and len(data) > 0:
                last = data[0].get("last", "")
                return float(last) if last else None
            return None
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            return None

    async def get_underlying_candles(
        self, symbol: str, bar: str = "1m", limit: int = 10
    ) -> list:
        """
        獲取底層資產 K 線數據，用於動量分析

        :param symbol: 交易對（如 BTC-USDT）
        :param bar: 時間粒度（1m / 3m / 5m / 15m）
        :param limit: 返回數量
        :returns: K 線列表，每條含 [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        """
        cmd = [
            "okx", "market", "candles", symbol,
            "--bar", bar,
            "--limit", str(limit),
            "--json",
            "--profile", self.profile,
        ]
        result = await self._run_cmd(cmd, f"獲取 {symbol} K線({bar})")
        if result.get("status") != "success":
            return []
        try:
            data = json.loads(result["stdout"])
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError, TypeError):
            return []

    async def _run_cmd(self, cmd: list, description: str) -> dict:
        """執行 okx CLI 命令（僅市場查詢，不影響帳戶）"""
        import shlex
        cmd_str = " ".join(shlex.quote(str(c)) for c in cmd)

        logger.debug(f"📡 {description}")
        logger.debug(f"   {cmd_str}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=15
            )
            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if process.returncode != 0:
                logger.warning(f"⚠️ {description} 失敗: {stderr_str[:200]}")
                return {"status": "error", "stdout": stdout_str, "stderr": stderr_str}

            return {"status": "success", "stdout": stdout_str}

        except asyncio.TimeoutError:
            logger.warning(f"⏰ {description} 超時")
            return {"status": "timeout"}
        except Exception as e:
            logger.warning(f"⚠️ {description} 異常: {e}")
            return {"status": "error", "error": str(e)}
