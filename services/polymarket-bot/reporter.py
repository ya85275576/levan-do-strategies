"""
Polymarket 套利报告器

提供多种输出方式：
  - 控制台日志（通过 scanner 的 _print_report 实现）
  - Webhook 通知（Slack / Discord）
  - JSON 文件写入
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

import aiohttp

from polymarket_api import ArbitrageOpportunity

logger = logging.getLogger("polymarket.reporter")


def format_opportunity_text(opp: ArbitrageOpportunity) -> str:
    """
    将套利机会格式化为人类可读的文本。

    Example:
        [套利] 0.45+0.50=0.95 | 利润 5.26% | 深度 1200 USDC
        Will BTC exceed $100k by Dec 31 2025?
        https://polymarket.com/event/btc-100k-2025
    """
    market = opp.market
    lines = [
        f"💎 套利机会 ({opp.profit_pct:.2f}% 利润)",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"  问题: {market.question}",
        f"  YES: ${opp.yes_ask:.4f} | NO: ${opp.no_ask:.4f}",
        f"  总成本: ${opp.cost:.4f} → 到期 $1.0000",
        f"  每股利润: ${opp.profit_per_share:.4f} ({opp.profit_pct:.2f}%)",
        f"  最大规模: {opp.max_trade_size:.2f} 股 (${opp.max_trade_size:.2f})",
        f"  YES深度: {opp.yes_depth:.2f} | NO深度: {opp.no_depth:.2f}",
        f"  https://polymarket.com/event/{market.slug}",
    ]
    return "\n".join(lines)


def format_opportunity_slack(opp: ArbitrageOpportunity) -> dict:
    """
    将套利机会格式化为 Slack Block Kit 消息格式。

    Returns:
        dict: Slack 消息 body
    """
    market = opp.market

    return {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"💎 套利机会: {opp.profit_pct:.2f}%",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{market.question}*\n\n"
                        f"• YES 买入价: `${opp.yes_ask:.4f}`\n"
                        f"• NO 买入价:  `${opp.no_ask:.4f}`\n"
                        f"• 总成本:     `${opp.cost:.4f}` → `$1.0000`\n"
                        f"• 每股利润:   `${opp.profit_per_share:.4f}` "
                        f"(`{opp.profit_pct:.2f}%`)\n"
                        f"• 最大规模:   `{opp.max_trade_size:.2f}` 股\n"
                        f"• YES深度: `{opp.yes_depth:.2f}` | "
                        f"NO深度: `{opp.no_depth:.2f}`\n"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "🔗 查看市场",
                            "emoji": True,
                        },
                        "url": f"https://polymarket.com/event/{market.slug}",
                    },
                ],
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"发现时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} | "
                            f"条件ID: `{market.condition_id[:16]}...`"
                        ),
                    },
                ],
            },
            {"type": "divider"},
        ],
    }


def format_opportunity_discord(opp: ArbitrageOpportunity) -> dict:
    """
    将套利机会格式化为 Discord Webhook Embed 格式。

    Returns:
        dict: Discord Webhook body
    """
    market = opp.market
    profit_emoji = "🟢" if opp.profit_pct > 3.0 else "🟡"

    return {
        "username": "Polymarket Arbitrage Bot",
        "embeds": [
            {
                "title": f"{profit_emoji} 套利机会: {opp.profit_pct:.2f}%",
                "url": f"https://polymarket.com/event/{market.slug}",
                "color": 0x00FF00 if opp.profit_pct > 3.0 else 0xFFD700,
                "fields": [
                    {
                        "name": "市场问题",
                        "value": market.question[:256],
                        "inline": False,
                    },
                    {
                        "name": "YES 买入价",
                        "value": f"${opp.yes_ask:.4f}",
                        "inline": True,
                    },
                    {
                        "name": "NO 买入价",
                        "value": f"${opp.no_ask:.4f}",
                        "inline": True,
                    },
                    {
                        "name": "总成本",
                        "value": f"${opp.cost:.4f} → $1.0000",
                        "inline": True,
                    },
                    {
                        "name": "每股利润",
                        "value": f"${opp.profit_per_share:.4f} ({opp.profit_pct:.2f}%)",
                        "inline": True,
                    },
                    {
                        "name": "最大规模",
                        "value": f"{opp.max_trade_size:.2f} 股 (${opp.max_trade_size:.2f})",
                        "inline": True,
                    },
                    {
                        "name": "流动性",
                        "value": f"YES: {opp.yes_depth:.2f} | NO: {opp.no_depth:.2f}",
                        "inline": True,
                    },
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {
                    "text": f"条件ID: {market.condition_id[:16]}...",
                },
            }
        ],
    }


def format_summary_text(
    total_rounds: int,
    total_opps: int,
    known_markets: int,
    new_opps_count: int,
) -> str:
    """格式化扫描摘要"""
    return (
        f"📊 Polymarket 套利扫描报告\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"  扫描轮次: {total_rounds}\n"
        f"  累计机会: {total_opps}\n"
        f"  已知市场: {known_markets}\n"
        f"  本轮新增: {new_opps_count}\n"
        f"  时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )


async def send_webhook_notification(
    webhook_url: str,
    opportunities: List[ArbitrageOpportunity],
    max_ops: int = 5,
):
    """
    通过 Webhook 发送套利通知。

    自动检测目标平台（Slack / Discord）并使用对应格式。

    Args:
        webhook_url: Webhook URL
        opportunities: 套利机会列表
        max_ops: 最大发送数量
    """
    if not webhook_url or not opportunities:
        return

    # 只发送最有利可图的前几个
    top_opps = opportunities[:max_ops]

    async with aiohttp.ClientSession() as session:
        for opp in top_opps:
            # 判断是 Slack 还是 Discord
            is_discord = "discord.com" in webhook_url or "discordapp.com" in webhook_url
            is_slack = "hooks.slack.com" in webhook_url

            if is_discord:
                payload = format_opportunity_discord(opp)
            elif is_slack:
                payload = format_opportunity_slack(opp)
            else:
                # 通用 Markdown 纯文本
                payload = {"text": format_opportunity_text(opp)}

            try:
                async with session.post(
                    webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status not in (200, 204):
                        text = await resp.text()
                        logger.warning(
                            f"[Webhook] 发送失败: HTTP {resp.status}: {text[:200]}"
                        )
                    else:
                        logger.info(
                            f"[Webhook] 已发送通知: {opp.market.question[:40]}..."
                        )
            except Exception as e:
                logger.error(f"[Webhook] 发送异常: {e}")

            # 避免发送过快
            await asyncio.sleep(0.5)



