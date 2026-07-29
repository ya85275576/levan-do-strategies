#!/usr/bin/env python3
"""
HighTempTation — Telegram + 飞书多渠道告警管理器

功能:
  - 开仓/平仓/风控触发时推送消息
  - 支持 Telegram Bot + 飞书 Webhook
  - 每日 UTC 0:00 自动发送日报
  - 去重（相同内容 5 分钟内不重复发送）

用法:
  alert = AlertManager()
  alert.send("开仓: Tokyo NO @ $0.45, edge=0.22")
  alert.send_trade_open("Tokyo", "NO", 0.45, 0.22, 1.0)
  alert.send_daily_report({"total_pnl": 12.5, "win_rate": 65.0})

环境变量:
  TELEGRAM_BOT_TOKEN     — Telegram Bot Token
  TELEGRAM_CHAT_ID       — 接收告警的 Chat ID
  FEISHU_WEBHOOK_URL     — 飞书机器人 Webhook URL
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

logger = logging.getLogger("alert_manager")


class AlertManager:
    """
    多渠道告警管理器。

    支持同时推送 Telegram + 飞书。
    自动去重（相同 text 在 DEDUP_SECONDS 内不重复发）。
    """

    DEDUP_SECONDS = 300  # 5 分钟去重

    def __init__(self):
        self._last_messages: Dict[str, float] = {}  # text → timestamp
        self._client: Optional[httpx.AsyncClient] = None

        # 从环境变量读取配置
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.feishu_webhook = os.environ.get("FEISHU_WEBHOOK_URL", "")

        self._enabled = bool(self.telegram_token or self.feishu_webhook)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    def _is_duplicate(self, text: str) -> bool:
        now = time.time()
        last = self._last_messages.get(text, 0)
        if now - last < self.DEDUP_SECONDS:
            return True
        self._last_messages[text] = now
        return False

    # ════════════════════════════════════════════════════════════════
    # Telegram
    # ════════════════════════════════════════════════════════════════

    async def _send_telegram(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self.telegram_token or not self.telegram_chat_id:
            return False
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            resp = await self.client.post(url, json=payload)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.debug(f"Telegram 发送失败: {e}")
            return False

    # ════════════════════════════════════════════════════════════════
    # 飞书
    # ════════════════════════════════════════════════════════════════

    async def _send_feishu(self, text: str) -> bool:
        if not self.feishu_webhook:
            return False
        payload = {
            "msg_type": "text",
            "content": {"text": text},
        }
        try:
            resp = await self.client.post(self.feishu_webhook, json=payload)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.debug(f"飞书发送失败: {e}")
            return False

    # ════════════════════════════════════════════════════════════════
    # 通用发送
    # ════════════════════════════════════════════════════════════════

    async def send(self, text: str) -> Dict[str, bool]:
        """
        发送告警到所有已启用的渠道。

        :returns: {"telegram": bool, "feishu": bool}
        """
        if not self._enabled:
            logger.debug(f"[告警] (未配置, 跳过): {text[:80]}")
            return {"telegram": False, "feishu": False}

        if self._is_duplicate(text):
            logger.debug(f"[告警] 去重跳过: {text[:60]}")
            return {"telegram": False, "feishu": False, "duplicated": True}

        tg_ok = await self._send_telegram(text)
        fs_ok = await self._send_feishu(text)

        if tg_ok or fs_ok:
            logger.info(f"📨 告警已发送 (TG={tg_ok}, FS={fs_ok}): {text[:60]}...")
        return {"telegram": tg_ok, "feishu": fs_ok}

    # ════════════════════════════════════════════════════════════════
    # 格式化消息
    # ════════════════════════════════════════════════════════════════

    async def send_trade_open(self, city: str, side: str, price: float,
                               edge: float, size: float) -> Dict[str, bool]:
        text = (
            f"🟢 开仓\n"
            f"城市: {city}\n"
            f"方向: {side}\n"
            f"价格: ${price:.3f}\n"
            f"边缘: {edge:.3f}\n"
            f"仓位: ${size:.2f}\n"
            f"时间: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
        )
        return await self.send(text)

    async def send_trade_close(self, city: str, side: str, entry: float,
                                exit_p: float, pnl: float, reason: str) -> Dict[str, bool]:
        emoji = "🟢" if pnl >= 0 else "🔴"
        text = (
            f"{emoji} 平仓 ({reason})\n"
            f"城市: {city}\n"
            f"方向: {side}\n"
            f"入场: ${entry:.3f} → 出场: ${exit_p:.3f}\n"
            f"盈亏: {pnl:+.2f}\n"
            f"时间: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
        )
        return await self.send(text)

    async def send_risk_alert(self, message: str) -> Dict[str, bool]:
        return await self.send(f"🚨 风控告警\n{message}")

    async def send_daily_report(self, stats: dict) -> Dict[str, bool]:
        text = (
            f"📊 日报 ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})\n"
            f"总盈亏: {stats.get('total_pnl', 0):+.2f}\n"
            f"交易: {stats.get('trades', 0)} 笔\n"
            f"胜率: {stats.get('win_rate', 0):.1f}%\n"
            f"持仓: {stats.get('open_positions', 0)} 个\n"
            f"资金: ${stats.get('equity', 0):.2f}"
        )
        return await self.send(text)

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
