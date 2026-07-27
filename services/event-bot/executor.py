#!/usr/bin/env python3
"""
OKX 事件合約訂單執行器

透過 subprocess 呼叫 okx CLI 來執行事件合約交易操作。

命令參考:
  okx event place <instId> <side> <outcome> <sz> [--px <price>] [--ordType <type>] [--json]
  okx event cancel <instId> <ordId> [--json]
  okx event orders [--status <open|history|archive>] [--json]
  okx event fills [--json]

事件合約特點：
  - 二元結果（漲/跌、觸達、區間）
  - 每份 1 USDT
  - 價格區間 0.001–0.999
  - 無槓桿、無爆倉、全額保證金
"""
import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger("event-bot.executor")


class EventContractExecutor:
    """
    事件合約執行器

    使用 subprocess 呼叫 okx CLI 來執行事件合約訂單。
    """

    def __init__(self, dry_run: bool = True, profile: str = "demo"):
        """
        :param dry_run: 模擬模式（僅記錄，不下單）
        :param profile: okx CLI profile 名稱（預設 demo，對應模擬盤）
        """
        self.dry_run = dry_run
        self.profile = profile

    async def place_order(
        self,
        inst_id: str,
        side: str,
        outcome: str,
        size: int,
        price: Optional[float] = None,
    ) -> dict:
        """
        下單事件合約

        :param inst_id: 合約 instrument ID（如 BTC-UPDOWN-5MIN-xxxx）
        :param side: 買賣方向（buy / sell）
        :param outcome: 結果（UP / DOWN）
        :param size: 合約數量（每份 1 USDT）
        :param price: 限價（可選，預設市價）
        :returns: 執行結果字典
        """
        sz = max(1, int(size))
        cmd = [
            "okx", "event", "place",
            inst_id,
            side,
            outcome,
            str(sz),
            "--json",
            "--profile", self.profile,
        ]
        if price is not None:
            cmd += ["--px", str(price)]

        desc = f"事件合約 {side.upper()} {outcome} {sz}份 {inst_id}"
        if price:
            desc += f" @ {price}"
        return await self._execute(cmd, desc)

    async def buy_up(self, inst_id: str, size: int, price: Optional[float] = None) -> dict:
        """買入 UP 結果"""
        return await self.place_order(inst_id, "buy", "UP", size, price)

    async def buy_down(self, inst_id: str, size: int, price: Optional[float] = None) -> dict:
        """買入 DOWN 結果"""
        return await self.place_order(inst_id, "buy", "DOWN", size, price)

    async def sell_up(self, inst_id: str, size: int, price: Optional[float] = None) -> dict:
        """賣出 UP 結果（看跌 UP）"""
        return await self.place_order(inst_id, "sell", "UP", size, price)

    async def sell_down(self, inst_id: str, size: int, price: Optional[float] = None) -> dict:
        """賣出 DOWN 結果（看跌 DOWN）"""
        return await self.place_order(inst_id, "sell", "DOWN", size, price)

    async def cancel_order(self, inst_id: str, ord_id: str) -> dict:
        """取消事件合約訂單"""
        cmd = [
            "okx", "event", "cancel",
            inst_id,
            ord_id,
            "--json",
            "--profile", self.profile,
        ]
        return await self._execute(cmd, f"取消訂單 {inst_id} {ord_id}")

    async def get_orders(self, status: str = "open") -> list:
        """
        查詢事件合約訂單

        :param status: open / history / archive
        :returns: 訂單列表
        """
        cmd = [
            "okx", "event", "orders",
            "--status", status,
            "--json",
            "--profile", self.profile,
        ]
        result = await self._execute(cmd, f"查詢訂單 ({status})")
        if result.get("status") != "success":
            return []
        try:
            data = json.loads(result["stdout"])
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError, TypeError):
            return []

    async def get_fills(self) -> list:
        """
        查詢事件合約成交記錄

        :returns: 成交記錄列表
        """
        cmd = [
            "okx", "event", "fills",
            "--json",
            "--profile", self.profile,
        ]
        result = await self._execute(cmd, "查詢成交記錄")
        if result.get("status") != "success":
            return []
        try:
            data = json.loads(result["stdout"])
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError, TypeError):
            return []

    async def _execute(self, cmd: list, description: str) -> dict:
        """
        執行 okx CLI 命令

        :param cmd: 命令列表
        :param description: 命令描述（用於日誌）
        :returns: 執行結果字典
        """
        import shlex
        cmd_str = " ".join(shlex.quote(str(c)) for c in cmd)

        if self.dry_run:
            logger.info(f"[DRY] {description}")
            logger.debug(f"[DRY] 命令: {cmd_str}")
            return {
                "status": "dry_run",
                "description": description,
                "command": cmd_str,
                "dry_run": True,
            }

        logger.info(f"▶ 執行: {description}")
        logger.debug(f"▶ 命令: {cmd_str}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=30
            )

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if process.returncode == 0:
                logger.info(f"✅ {description} 成功")
            else:
                logger.error(f"❌ {description} 失敗 (code={process.returncode})")
                if stderr_str:
                    logger.error(f"stderr: {stderr_str[:500]}")
                if stdout_str:
                    logger.info(f"stdout: {stdout_str[:500]}")

            return {
                "status": "success" if process.returncode == 0 else "error",
                "returncode": process.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "description": description,
                "command": cmd_str,
            }

        except asyncio.TimeoutError:
            logger.error(f"⏰ {description} 超時 (30s)")
            return {"status": "timeout", "description": description, "command": cmd_str}
        except FileNotFoundError:
            logger.error("❌ okx CLI 未找到，請安裝: npm i -g @okx_ai/okx-trade-cli")
            return {"status": "error", "error": "okx CLI not found", "description": description}
        except Exception as e:
            logger.error(f"❌ {description} 異常: {e}")
            return {"status": "error", "error": str(e), "description": description}
