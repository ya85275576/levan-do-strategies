"""
OpenClaw (okx CLI) 订单执行器

通过 subprocess 调用 okx CLI 执行交易操作。
与 OkxOrderManager 并行工作，作为额外的执行通道。

命令参考:
  okx swap place --instId <id> --side buy --posSide long --ordType market --sz <qty> --profile demo
  okx swap close --instId <id> --mgnMode isolated --profile demo
"""
import asyncio
import logging
import shlex
from typing import Optional

logger = logging.getLogger("bot.openclaw")


class OpenClawExecutor:
    """
    OpenClaw 订单执行器

    使用 subprocess 调用 okx CLI 来执行订单。
    在 DRY_RUN=true 时仅记录，不下单。
    """

    def __init__(self, dry_run: bool = True, profile: str = "demo"):
        """
        :param dry_run: 模拟模式（仅记录，不下单）
        :param profile: okx CLI profile 名称（默认为 demo，对应模拟盘）
        """
        self.dry_run = dry_run
        self.profile = profile

        # 检查 okx CLI 是否可用
        self._check_cli()

    def _check_cli(self):
        """检查 okx CLI 是否可执行"""
        try:
            result = subprocess_run(
                ["okx", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.warning(f"okx CLI 版本检查失败: {result.stderr.strip()}")
            else:
                logger.info(f"okx CLI 可用: {result.stdout.strip()}")
        except FileNotFoundError:
            logger.error("okx CLI 未安装，请执行: npm i -g @okx_ai/okx-trade-cli")
        except Exception as e:
            logger.error(f"okx CLI 检查异常: {e}")

    async def place_long(self, symbol: str, qty: float) -> dict:
        """
        开多仓（永续合约）

        :param symbol: 交易对（如 BTC-USDT）
        :param qty: 数量（合约张数，自动取整）
        :returns: 执行结果字典
        """
        sz = max(1, int(qty))
        cmd = [
            "okx", "swap", "place",
            "--instId", symbol,
            "--side", "buy",
            "--ordType", "market",
            "--sz", str(sz),
            "--profile", self.profile,
        ]
        return await self._execute(cmd, f"开多仓 {symbol} {sz}")

    async def place_short(self, symbol: str, qty: float) -> dict:
        """
        开空仓（永续合约）

        :param symbol: 交易对（如 BTC-USDT）
        :param qty: 数量（合约张数，自动取整）
        :returns: 执行结果字典
        """
        sz = max(1, int(qty))
        cmd = [
            "okx", "swap", "place",
            "--instId", symbol,
            "--side", "sell",
            "--ordType", "market",
            "--sz", str(sz),
            "--profile", self.profile,
        ]
        return await self._execute(cmd, f"开空仓 {symbol} {sz}")

    async def close_position(self, symbol: str, mgn_mode: str = "isolated") -> dict:
        """
        平仓（永续合约）

        :param symbol: 交易对（如 BTC-USDT）
        :param mgn_mode: 保证金模式 (isolated/cross)
        :returns: 执行结果字典
        """
        cmd = [
            "okx", "swap", "close",
            "--instId", symbol,
            "--mgnMode", mgn_mode,
            "--profile", self.profile,
        ]
        return await self._execute(cmd, f"平仓 {symbol}")

    async def get_balance(self, ccy: str = "USDT") -> float:
        """
        获取交易账户指定币种余额（透過 okx account balance）。

        解析 JSON 輸出，提取 equity (eq) 字段。

        :param ccy: 币种，默认 USDT
        :returns: 余额（float），失败时返回 0.0
        """
        cmd = [
            "okx", "account", "balance", ccy,
            "--json",
            "--profile", self.profile,
        ]
        result = await self._execute(cmd, f"获取 {ccy} 余额")

        if result.get("status") != "success":
            return 0.0

        try:
            import json
            data = json.loads(result["stdout"])
            if isinstance(data, list) and len(data) > 0:
                details = data[0].get("details", [])
                for d in details:
                    if d.get("ccy") == ccy:
                        return float(d.get("eq", 0))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.warning(f"解析 {ccy} 余额 JSON 失败: {e}")

        return 0.0

    async def get_positions(self) -> list:
        """
        获取当前持仓（永续合约 SWAP）。

        调用: okx account positions --instType SWAP --json --profile demo

        :returns: 持仓列表，每个元素为 dict，包含 instId, posSide, pos, avgPx,
                  markPx, upl, uplRatio, lever, liqPx, mgnMode, imr, mmr,
                  notionalUsd, ccy, cTime, uTime
        """
        cmd = [
            "okx", "account", "positions",
            "--instType", "SWAP",
            "--json",
            "--profile", self.profile,
        ]
        result = await self._execute(cmd, "获取持仓")

        if result.get("status") != "success":
            return []

        try:
            import json
            data = json.loads(result["stdout"])
            if not isinstance(data, list):
                return []
            return data
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"解析持仓 JSON 失败: {e}")
            return []

    async def set_leverage(self, symbol: str, leverage: int, mgn_mode: str = "isolated") -> dict:
        """
        设置杠杆

        :param symbol: 交易对（如 BTC-USDT）
        :param leverage: 杠杆倍数
        :param mgn_mode: 保证金模式 (isolated/cross)
        :returns: 执行结果字典
        """
        cmd = [
            "okx", "swap", "leverage",
            "--instId", symbol,
            "--lever", str(leverage),
            "--mgnMode", mgn_mode,
            "--profile", self.profile,
        ]
        return await self._execute(cmd, f"设置杠杆 {symbol} {leverage}x")

    async def _execute(self, cmd: list, description: str) -> dict:
        """
        执行 okx CLI 命令

        :param cmd: 命令列表
        :param description: 命令描述（用于日志）
        :returns: 执行结果字典
        """
        cmd_str = " ".join(shlex.quote(str(c)) for c in cmd)

        if self.dry_run:
            logger.info(f"[OpenClaw][DRY] {description}")
            logger.info(f"[OpenClaw][DRY] 命令: {cmd_str}")
            return {
                "status": "dry_run",
                "description": description,
                "command": cmd_str,
                "dry_run": True,
            }

        logger.info(f"[OpenClaw] ▶ 执行: {description}")
        logger.info(f"[OpenClaw] ▶ 命令: {cmd_str}")

        try:
            # 使用 asyncio 的子进程执行
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if process.returncode == 0:
                logger.info(f"[OpenClaw] ✅ {description} 成功")
                if stdout_str:
                    # 输出可能包含 JSON，截断避免刷屏
                    logger.info(f"[OpenClaw] stdout: {stdout_str[:500]}")
            else:
                logger.error(f"[OpenClaw] ❌ {description} 失败 (code={process.returncode})")
                if stderr_str:
                    logger.error(f"[OpenClaw] stderr: {stderr_str[:500]}")
                if stdout_str:
                    logger.info(f"[OpenClaw] stdout: {stdout_str[:500]}")

            return {
                "status": "success" if process.returncode == 0 else "error",
                "returncode": process.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "description": description,
                "command": cmd_str,
            }

        except asyncio.TimeoutError:
            logger.error(f"[OpenClaw] ⏰ {description} 超时 (30s)")
            return {
                "status": "timeout",
                "description": description,
                "command": cmd_str,
            }
        except FileNotFoundError:
            logger.error(f"[OpenClaw] ❌ okx CLI 未找到，请安装: npm i -g @okx_ai/okx-trade-cli")
            return {
                "status": "error",
                "error": "okx CLI not found",
                "description": description,
            }
        except Exception as e:
            logger.error(f"[OpenClaw] ❌ {description} 异常: {e}")
            return {
                "status": "error",
                "error": str(e),
                "description": description,
            }


def subprocess_run(cmd, **kwargs):
    """同步调用 subprocess.run（用于初始化检查）"""
    import subprocess
    return subprocess.run(cmd, **kwargs)
