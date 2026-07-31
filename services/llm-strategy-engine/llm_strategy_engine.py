#!/usr/bin/env python3
"""
LLM Strategy Engine — DeepSeek Edition
========================================

基于 DeepSeek API (OpenAI 兼容格式) 的策略生成引擎。
从自然语言研究论文 / 描述中提取结构化策略 JSON，
并提供模板、校验、回测、部署全流程。

API 端点: https://api.deepseek.com/v1/chat/completions
读取环境变量: DEEPSEEK_API_KEY
无 Key 时自动降级到 Mock 模式。

用法:
  python llm_strategy_engine.py --paper "path/to/research.pdf"
  python llm_strategy_engine.py --list
  python llm_strategy_engine.py --validate ./output.json
  python llm_strategy_engine.py --backtest ./output.json
  python llm_strategy_engine.py --deploy ./output.json
  python llm_strategy_engine.py --demo          # 演示模式
  python llm_strategy_engine.py --auto          # 全自动流水线
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("llm_strategy_engine")

# ============================================================
# 配置
# ============================================================
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "4096"))
DEEPSEEK_TEMPERATURE = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.3"))

OUTPUT_DIR = os.getenv("STRATEGY_OUTPUT_DIR", "generated_strategies")
DEFAULT_CAPITAL = 10000.0
DEFAULT_RISK_PCT = 0.02


# ============================================================
# 策略模板库 (6 种)
# ============================================================

STRATEGY_TEMPLATES = {
    "weather_arbitrage": {
        "name": "天气套利策略",
        "description": "基于温度预报与Polymarket赔率的偏差进行套利交易",
        "type": "weather_arbitrage",
        "entry": {
            "conditions": [
                "polymarket_market_price < 0.3 AND forecast_CDF < polymarket_market_price - calibration_threshold",
                "polymarket_market_price > 0.7 AND forecast_CDF > polymarket_market_price + calibration_threshold",
            ],
            "side": "conditional",
            "signal_type": "price_discrepancy",
        },
        "exit": {
            "take_profit_pct": 0.09,
            "stop_loss_pct": -0.065,
            "time_stop_hours": 24,
            "trailing_activate_pct": 0.05,
            "trailing_retrace_pct": 0.03,
        },
        "sizing": {
            "method": "fractional_kelly",
            "kelly_fraction": 0.25,
            "max_risk_pct": 0.02,
            "min_size_usd": 1.0,
            "max_size_usd": 100.0,
        },
        "filters": [
            "Min liquidity: 500 USD",
            "Max bid-ask spread: 0.03",
            "Min 24h volume: 3000 USD",
            "Signal history win rate >= 0.40",
        ],
        "parameters": {
            "calibration_threshold": 0.15,
            "forecast_days": 7,
            "min_market_price": 0.30,
            "max_market_price": 0.90,
        },
    },
    "mean_reversion": {
        "name": "均值回归策略",
        "description": "基于RSI和布林带的均值回归交易",
        "type": "mean_reversion",
        "entry": {
            "conditions": [
                "RSI(14) < 30 AND price < lower_band(20, 2)",
                "RSI(14) > 70 AND price > upper_band(20, 2)",
            ],
            "side": "both",
            "signal_type": "overbought_oversold",
        },
        "exit": {
            "take_profit_pct": 0.05,
            "stop_loss_pct": -0.03,
            "trailing_activate_pct": 0.03,
            "trailing_retrace_pct": 0.015,
        },
        "sizing": {
            "method": "fixed_pct",
            "risk_pct": 0.02,
            "max_size_usd": 500.0,
        },
        "filters": [
            "ATR(14) > median(ATR, 50)",
            "Volume > average(volume, 20) * 1.5",
        ],
        "parameters": {
            "rsi_period": 14,
            "bb_period": 20,
            "bb_std": 2.0,
            "atr_period": 14,
        },
    },
    "trend_following": {
        "name": "趋势跟踪策略",
        "description": "基于EMA交叉和ADX的趋势跟踪系统",
        "type": "trend_following",
        "entry": {
            "conditions": [
                "EMA(12) CROSSES_ABOVE EMA(26) AND ADX(14) > 25",
                "EMA(12) CROSSES_BELOW EMA(26) AND ADX(14) > 25",
            ],
            "side": "both",
            "signal_type": "crossover",
        },
        "exit": {
            "take_profit_atr_multiple": 3.0,
            "stop_loss_atr_multiple": 1.5,
            "trailing_stop_atr_multiple": 2.0,
        },
        "sizing": {
            "method": "atr_based",
            "risk_usd_per_position": 100.0,
            "atr_period": 14,
        },
        "filters": [
            "Volume > SMA(volume, 20)",
            "Price > SMA(price, 200)",
        ],
        "parameters": {
            "ema_fast": 12,
            "ema_slow": 26,
            "adx_period": 14,
            "atr_period": 14,
        },
    },
    "volatility": {
        "name": "波动率策略",
        "description": "基于波动率扩张和收缩的交易策略",
        "type": "volatility",
        "entry": {
            "conditions": [
                "ATR(14) / SMA(close, 14) > percentile(ATR/SMA, 90)",
                "IV_percentile > 80 AND HV_percentile < 20",
            ],
            "side": "both",
            "signal_type": "volatility_breakout",
        },
        "exit": {
            "take_profit_pct": 0.08,
            "stop_loss_pct": -0.04,
            "time_stop_hours": 48,
        },
        "sizing": {
            "method": "volatility_adjusted",
            "target_volatility_pct": 0.02,
            "max_size_usd": 1000.0,
        },
        "filters": [
            "VWAP deviation < 2%",
            "Spread < 0.1%",
        ],
        "parameters": {
            "atr_period": 14,
            "lookback_period": 90,
            "iv_period": 30,
            "hv_period": 20,
        },
    },
    "ml_signal": {
        "name": "机器学习信号策略",
        "description": "基于ML模型预测信号的自动交易策略",
        "type": "ml_signal",
        "entry": {
            "conditions": [
                "ml_prediction > 0.7 AND model_confidence > 0.8",
                "ml_prediction < 0.3 AND model_confidence > 0.8",
            ],
            "side": "both",
            "signal_type": "ml_prediction",
        },
        "exit": {
            "take_profit_pct": 0.06,
            "stop_loss_pct": -0.04,
            "trailing_activate_pct": 0.04,
            "trailing_retrace_pct": 0.02,
        },
        "sizing": {
            "method": "confidence_based",
            "base_risk_pct": 0.01,
            "max_risk_pct": 0.03,
            "confidence_scaling": True,
        },
        "filters": [
            "model_accuracy_last_30d > 0.55",
            "Min training_samples: 1000",
        ],
        "parameters": {
            "prediction_threshold_high": 0.7,
            "prediction_threshold_low": 0.3,
            "confidence_threshold": 0.8,
            "retrain_frequency_days": 7,
        },
    },
    "general": {
        "name": "通用策略模板",
        "description": "可定制的通用交易策略框架",
        "type": "general",
        "entry": {
            "conditions": [
                "条件_买入",
                "条件_卖出",
            ],
            "side": "both",
            "signal_type": "custom",
        },
        "exit": {
            "take_profit_pct": 0.05,
            "stop_loss_pct": -0.03,
        },
        "sizing": {
            "method": "fixed_pct",
            "risk_pct": 0.02,
        },
        "filters": [],
        "parameters": {
            "param1": "value1",
            "param2": "value2",
        },
    },
}


# ============================================================
# DeepSeek API 调用
# ============================================================

def _deepseek_chat_completion(
    messages: List[Dict[str, str]],
    model: str = None,
    max_tokens: int = None,
    temperature: float = None,
) -> Dict[str, Any]:
    """
    调用 DeepSeek Chat API（OpenAI 兼容格式）。

    Args:
        messages: 对话消息列表 [{"role": "...", "content": "..."}]
        model: 模型名 (默认 DEEPSEEK_MODEL)
        max_tokens: 最大输出 token (默认 DEEPSEEK_MAX_TOKENS)
        temperature: 温度参数 (默认 DEEPSEEK_TEMPERATURE)

    Returns:
        API 响应字典 (含 choices 等字段)

    Raises:
        ConnectionError: API 不可达
        RuntimeError: API 返回错误
    """
    model = model or DEEPSEEK_MODEL
    max_tokens = max_tokens or DEEPSEEK_MAX_TOKENS
    temperature = temperature or DEEPSEEK_TEMPERATURE

    if not DEEPSEEK_API_KEY:
        logger.warning("⚠️  DEEPSEEK_API_KEY 未设置，降级到 Mock 模式")
        return _mock_completion(messages)

    import requests

    url = urljoin(DEEPSEEK_API_BASE.rstrip("/") + "/", "chat/completions")
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        logger.info(f"🤖 调用 DeepSeek API: model={model}, messages={len(messages)}, max_tokens={max_tokens}")
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        if "choices" not in data or len(data["choices"]) == 0:
            raise RuntimeError(f"DeepSeek API 返回空的 choices: {json.dumps(data, ensure_ascii=False)}")

        logger.info("✅ DeepSeek API 调用成功")
        return data

    except requests.exceptions.Timeout:
        logger.error("⏰ DeepSeek API 超时 (60s)")
        raise ConnectionError("DeepSeek API 请求超时")
    except requests.exceptions.ConnectionError as e:
        logger.error(f"🔌 无法连接 DeepSeek API: {e}")
        raise ConnectionError(f"无法连接 DeepSeek API: {e}")
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        body = e.response.text[:500] if e.response is not None else ""
        logger.error(f"❌ DeepSeek API HTTP {status}: {body}")
        raise RuntimeError(f"DeepSeek API 返回 HTTP {status}: {body}")
    except Exception as e:
        logger.error(f"❌ DeepSeek API 调用异常: {e}")
        raise


def _mock_completion(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Mock 模式：当 DEEPSEEK_API_KEY 缺失时，模拟 API 响应。
    返回与真实 API 相同格式的模拟结果，用于开发和测试。
    """
    logger.info("🎭 Mock 模式: 生成模拟策略")

    # 从最后一条用户消息中提取关键词，选择合适的模板
    user_content = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_content = m.get("content", "")
            break

    # 根据关键词选择策略类型
    strategy_type = "general"
    if any(kw in user_content.lower() for kw in ["weather", "温度", "天气", "arbitrage", "套利"]):
        strategy_type = "weather_arbitrage"
    elif any(kw in user_content.lower() for kw in ["mean reversion", "均值回归", "reversal"]):
        strategy_type = "mean_reversion"
    elif any(kw in user_content.lower() for kw in ["trend", "趋势", "momentum", "动量"]):
        strategy_type = "trend_following"
    elif any(kw in user_content.lower() for kw in ["volatility", "波动率", "breakout", "突破"]):
        strategy_type = "volatility"
    elif any(kw in user_content.lower() for kw in ["ml", "machine learning", "机器学习", "ai"]):
        strategy_type = "ml_signal"

    template = STRATEGY_TEMPLATES.get(strategy_type, STRATEGY_TEMPLATES["general"])
    mock_strategy = dict(template)  # 浅拷贝
    mock_strategy["source"] = "mock"
    mock_strategy["generated_at"] = datetime.now(timezone.utc).isoformat()
    mock_strategy["description"] = f"[Mock] {mock_strategy['description']}"

    content = json.dumps(mock_strategy, ensure_ascii=False, indent=2)

    return {
        "id": f"mock-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock-deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


# ============================================================
# 验证 DeepSeek 连接
# ============================================================

def verify_deepseek_connection() -> bool:
    """
    验证 DeepSeek API 连接是否正常。
    发送一个简单的对话请求，确认能收到回复。

    Returns:
        True 如果连接成功，False 如果降级到 Mock 模式或失败
    """
    if not DEEPSEEK_API_KEY:
        logger.warning("⚠️  DEEPSEEK_API_KEY 未设置，运行在 Mock 模式")
        mock_resp = _mock_completion([
            {"role": "user", "content": "说'Hello, DeepSeek!'"}
        ])
        content = mock_resp["choices"][0]["message"]["content"]
        logger.info(f"🎭 Mock 回复: {content[:100]}")
        return False  # 返回 False 表示非真实连接

    try:
        resp = _deepseek_chat_completion([
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "回复'DeepSeek connection OK'即可，不要多说。"},
        ], max_tokens=50, temperature=0.0)

        content = resp["choices"][0]["message"]["content"]
        logger.info(f"✅ DeepSeek 连接验证成功: {content[:100]}")

        # 打印用量信息
        usage = resp.get("usage", {})
        if usage:
            logger.info(f"   Token usage: prompt={usage.get('prompt_tokens')}, "
                        f"completion={usage.get('completion_tokens')}, "
                        f"total={usage.get('total_tokens')}")

        return True

    except (ConnectionError, RuntimeError) as e:
        logger.error(f"❌ DeepSeek 连接验证失败: {e}")
        logger.info("🎭 降级到 Mock 模式")
        return False


# ============================================================
# 论文 / 描述解析
# ============================================================

PARSE_SYSTEM_PROMPT = """你是一个专业的量化交易策略分析师。请从用户提供的交易策略描述或研究论文中，提取结构化的策略参数。

请严格按照以下 JSON 格式输出（不要包含 Markdown 代码块标记）：

{
  "name": "策略名称",
  "description": "策略简要描述",
  "type": "策略类型 (weather_arbitrage | mean_reversion | trend_following | volatility | ml_signal | general)",
  "entry": {
    "conditions": ["条件1", "条件2"],
    "side": "long | short | both | conditional",
    "signal_type": "信号类型"
  },
  "exit": {
    "take_profit_pct": 0.05,
    "stop_loss_pct": -0.03
  },
  "sizing": {
    "method": "fixed_pct | fractional_kelly | atr_based | volatility_adjusted | confidence_based",
    "risk_pct": 0.02
  },
  "filters": ["过滤器1", "过滤器2"],
  "parameters": {
    "key": "value"
  }
}

如果描述中缺乏某些信息，请根据策略类型使用合理的默认值。
只输出 JSON，不要有任何额外文字。"""


def parse_research_paper(text: str) -> Optional[Dict[str, Any]]:
    """
    从交易策略描述或研究论文文本中提取结构化策略 JSON。

    Args:
        text: 策略描述文本

    Returns:
        结构化策略字典，解析失败返回 None
    """
    logger.info(f"📄 解析策略描述 ({len(text)} 字符)")

    try:
        resp = _deepseek_chat_completion([
            {"role": "system", "content": PARSE_SYSTEM_PROMPT},
            {"role": "user", "content": f"请分析以下交易策略描述，提取结构化参数：\n\n{text}"},
        ])

        content = resp["choices"][0]["message"]["content"].strip()

        # 移除可能的 Markdown 代码块标记
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)

        strategy = json.loads(content)

        # 补充元数据
        strategy["source"] = "deepseek" if DEEPSEEK_API_KEY else "mock"
        strategy["generated_at"] = datetime.now(timezone.utc).isoformat()
        strategy["raw_input_preview"] = text[:200]

        logger.info(f"✅ 策略解析成功: {strategy.get('name', 'Unnamed')}")
        return strategy

    except json.JSONDecodeError as e:
        logger.error(f"❌ 解析失败: JSON 格式错误 — {e}")
        logger.debug(f"原始响应: {content if 'content' in dir() else 'N/A'}")
        return None
    except Exception as e:
        logger.error(f"❌ 解析失败: {e}")
        return None


# ============================================================
# 策略校验
# ============================================================

class ValidationError(Exception):
    """策略校验错误"""
    pass


class StrategyValidator:
    """策略 Schema 校验器"""

    REQUIRED_FIELDS = ["name", "description", "type", "entry", "exit", "sizing"]
    VALID_TYPES = {"weather_arbitrage", "mean_reversion", "trend_following",
                   "volatility", "ml_signal", "general"}
    VALID_SIZING_METHODS = {"fixed_pct", "fractional_kelly", "atr_based",
                            "volatility_adjusted", "confidence_based"}

    @classmethod
    def validate(cls, strategy: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        校验策略结构是否完整有效。

        Returns:
            (is_valid, errors)
        """
        errors = []

        # 检查必填字段
        for field in cls.REQUIRED_FIELDS:
            if field not in strategy:
                errors.append(f"缺少必填字段: {field}")

        if errors:
            return False, errors

        # 类型检查
        if strategy["type"] not in cls.VALID_TYPES:
            errors.append(f"无效的策略类型: {strategy['type']}, "
                          f"合法值: {', '.join(sorted(cls.VALID_TYPES))}")

        # Entry 检查
        entry = strategy.get("entry", {})
        if "conditions" not in entry:
            errors.append("entry 缺少 conditions")
        if "side" not in entry:
            errors.append("entry 缺少 side")
        elif entry["side"] not in ("long", "short", "both", "conditional"):
            errors.append(f"无效的 entry.side: {entry['side']}")

        # Exit 检查
        exit_section = strategy.get("exit", {})
        if not exit_section:
            errors.append("exit 为空")

        # Sizing 检查
        sizing = strategy.get("sizing", {})
        if "method" not in sizing:
            errors.append("sizing 缺少 method")
        elif sizing["method"] not in cls.VALID_SIZING_METHODS:
            errors.append(f"无效的 sizing.method: {sizing['method']}, "
                          f"合法值: {', '.join(sorted(cls.VALID_SIZING_METHODS))}")

        return len(errors) == 0, errors


# ============================================================
# 沙盒回测
# ============================================================

@dataclass
class BacktestResult:
    """回测结果"""
    strategy_name: str = ""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    initial_capital: float = DEFAULT_CAPITAL
    final_capital: float = DEFAULT_CAPITAL
    equity_curve: List[float] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)


class BacktestEngine:
    """
    沙盒回测引擎（模拟估算模式）。

    根据策略的 entry/exit/sizing 参数，生成模拟交易序列。
    非实盘回测，仅用于快速验证策略逻辑是否合理。
    """

    def __init__(self, strategy: Dict[str, Any],
                 capital: float = DEFAULT_CAPITAL):
        self.strategy = strategy
        self.capital = capital

    def run(self, num_simulations: int = 200) -> BacktestResult:
        """
        执行模拟回测。

        Args:
            num_simulations: 模拟交易次数

        Returns:
            BacktestResult 对象
        """
        logger.info(f"🔬 回测启动: {self.strategy.get('name', 'Unnamed')}, "
                    f"模拟次数={num_simulations}")

        result = BacktestResult(
            strategy_name=self.strategy.get("name", "Unnamed"),
            initial_capital=self.capital,
            final_capital=self.capital,
        )

        sizing = self.strategy.get("sizing", {})
        risk_pct = sizing.get("risk_pct", 0.02)
        max_size = sizing.get("max_size_usd", 500.0)

        exit_config = self.strategy.get("exit", {})
        tp_pct = exit_config.get("take_profit_pct", 0.05)
        sl_pct = exit_config.get("stop_loss_pct", -0.03)

        entry = self.strategy.get("entry", {})
        side = entry.get("side", "both")

        equity = self.capital
        equity_curve = [equity]
        wins = 0
        losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        peak_equity = equity
        max_drawdown = 0.0
        trade_records = []

        random.seed(42)

        for i in range(num_simulations):
            # 模拟入场概率 (基于策略类型)
            base_win_rate = {
                "weather_arbitrage": 0.55,
                "mean_reversion": 0.50,
                "trend_following": 0.45,
                "volatility": 0.48,
                "ml_signal": 0.52,
                "general": 0.50,
            }.get(self.strategy.get("type", "general"), 0.50)

            noise = random.uniform(-0.05, 0.05)
            win_prob = min(0.80, max(0.20, base_win_rate + noise))

            # 仓位计算
            position_size = min(self.capital * risk_pct, max_size)
            if position_size <= 0:
                position_size = 10.0

            is_win = random.random() < win_prob

            if is_win:
                pnl = position_size * tp_pct * random.uniform(0.8, 1.2)
                wins += 1
                gross_profit += pnl
            else:
                pnl = position_size * sl_pct * random.uniform(0.8, 1.2)
                losses += 1
                gross_loss += abs(pnl)

            equity += pnl

            # 模拟随机间隔的权益记录
            if i % 5 == 0:
                equity_curve.append(equity)

            peak_equity = max(peak_equity, equity)
            dd = (peak_equity - equity) / peak_equity * 100
            max_drawdown = max(max_drawdown, dd)

            if i < 20 or i % 20 == 0:
                trade_records.append({
                    "trade_no": i + 1,
                    "side": "long" if side in ("both", "long") else "short",
                    "size": round(position_size, 2),
                    "pnl": round(pnl, 2),
                    "result": "win" if is_win else "loss",
                    "equity": round(equity, 2),
                })

        total_trades = wins + losses
        win_rate = wins / total_trades if total_trades > 0 else 0.0

        # 夏普比率 (简化版)
        returns = []
        for j in range(1, len(equity_curve)):
            if equity_curve[j - 1] > 0:
                returns.append(
                    (equity_curve[j] - equity_curve[j - 1]) / equity_curve[j - 1]
                )
        avg_return = sum(returns) / len(returns) if returns else 0.0
        std_return = (
            (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5
            if returns and len(returns) > 1
            else 0.0001
        )
        sharpe = (avg_return / std_return) * (252 ** 0.5) if std_return > 0 else 0.0

        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        result.total_trades = total_trades
        result.wins = wins
        result.losses = losses
        result.win_rate = round(win_rate, 4)
        result.gross_profit = round(gross_profit, 2)
        result.gross_loss = round(gross_loss, 2)
        result.net_pnl = round(gross_profit - gross_loss, 2)
        result.max_drawdown_pct = round(max_drawdown, 2)
        result.sharpe_ratio = round(sharpe, 4)
        result.profit_factor = round(profit_factor, 4)
        result.avg_win = round(gross_profit / wins, 2) if wins > 0 else 0.0
        result.avg_loss = round(gross_loss / losses, 2) if losses > 0 else 0.0
        result.final_capital = round(equity, 2)
        result.equity_curve = equity_curve
        result.trades = trade_records

        logger.info(
            f"📊 回测完成: WinRate={result.win_rate:.1%}, "
            f"PnL=${result.net_pnl:+.2f}, "
            f"DD={result.max_drawdown_pct:.1f}%, "
            f"Sharpe={result.sharpe_ratio:.2f}"
        )

        return result


# ============================================================
# 策略部署器
# ============================================================

class StrategyDeployer:
    """
    策略部署器。

    将验证通过的策略输出为 .json (策略定义) 和 .env (参数) 双格式文件。
    """

    def __init__(self, output_dir: str = OUTPUT_DIR):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def deploy(self, strategy: Dict[str, Any],
               backtest_result: Optional[BacktestResult] = None) -> Dict[str, str]:
        """
        部署策略：生成 .json 和 .env 文件。

        Args:
            strategy: 策略字典
            backtest_result: 可选的回测结果（会附加到部署包中）

        Returns:
            { "json_path": "...", "env_path": "..." }
        """
        name = strategy.get("name", "unnamed_strategy")
        safe_name = re.sub(r'[^\w\-_]', '_', name).lower()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"{safe_name}_{timestamp}"

        # -- 构建完整部署包 --
        deploy_package = {
            "strategy": strategy,
            "deploy_info": {
                "deployed_at": datetime.now(timezone.utc).isoformat(),
                "version": "1.0.0",
                "engine": "deepseek",
                "source": strategy.get("source", "deepseek"),
            },
        }

        if backtest_result:
            deploy_package["backtest"] = {
                "win_rate": backtest_result.win_rate,
                "net_pnl": backtest_result.net_pnl,
                "max_drawdown_pct": backtest_result.max_drawdown_pct,
                "sharpe_ratio": backtest_result.sharpe_ratio,
                "profit_factor": backtest_result.profit_factor,
                "total_trades": backtest_result.total_trades,
                "final_capital": backtest_result.final_capital,
            }

        # -- JSON 输出 --
        json_path = self.output_dir / f"{base_name}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(deploy_package, f, ensure_ascii=False, indent=2)
        logger.info(f"📄 JSON 策略已保存: {json_path}")

        # -- .env 输出 --
        env_path = self.output_dir / f"{base_name}.env"
        env_lines = [
            f"# 策略: {strategy.get('name', 'Unnamed')}",
            f"# 生成时间: {datetime.now().isoformat()}",
            f"# 引擎: DeepSeek",
            f"",
            f"STRATEGY_TYPE={strategy.get('type', 'general')}",
            f"STRATEGY_SIDE={strategy.get('entry', {}).get('side', 'both')}",
            f"",
            f"# Entry 条件",
        ]
        conditions = strategy.get("entry", {}).get("conditions", [])
        for i, cond in enumerate(conditions):
            env_lines.append(f"ENTRY_CONDITION_{i + 1}={cond}")

        env_lines.extend([
            f"",
            f"# Exit 参数",
        ])
        for k, v in strategy.get("exit", {}).items():
            env_lines.append(f"EXIT_{k.upper()}={v}")

        env_lines.extend([
            f"",
            f"# Sizing 参数",
        ])
        for k, v in strategy.get("sizing", {}).items():
            env_lines.append(f"SIZING_{k.upper()}={v}")

        env_lines.extend([
            f"",
            f"# 过滤器",
        ])
        for i, f in enumerate(strategy.get("filters", [])):
            env_lines.append(f"FILTER_{i + 1}={f}")

        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(env_lines) + "\n")
        logger.info(f"📄 .env 配置已保存: {env_path}")

        return {
            "json_path": str(json_path),
            "env_path": str(env_path),
        }


# ============================================================
# CLI 入口
# ============================================================

def create_demo_strategy() -> Dict[str, Any]:
    """生成一个示例天气套利策略"""
    template = dict(STRATEGY_TEMPLATES["weather_arbitrage"])
    template["source"] = "deepseek" if DEEPSEEK_API_KEY else "mock"
    template["generated_at"] = datetime.now(timezone.utc).isoformat()
    template["parameters"]["city"] = "Hong Kong"
    template["parameters"]["forecast_source"] = "Open-Meteo ECMWF"
    return template


def cmd_demo():
    """--demo: 演示模式，生成示例策略并部署"""
    logger.info("🎯 演示模式启动")
    strategy = create_demo_strategy()

    print("\n" + "=" * 60)
    print(f"📋 策略: {strategy['name']}")
    print(f"📝 描述: {strategy['description']}")
    print(f"🔢 类型: {strategy['type']}")
    print(f"📊 入场条件: {json.dumps(strategy['entry'], ensure_ascii=False)}")
    print(f"🚪 出场参数: {json.dumps(strategy['exit'], ensure_ascii=False)}")
    print(f"📏 仓位规则: {json.dumps(strategy['sizing'], ensure_ascii=False)}")
    print(f"🔍 过滤器: {json.dumps(strategy['filters'], ensure_ascii=False)}")
    print("=" * 60)

    # 校验
    valid, errors = StrategyValidator.validate(strategy)
    if valid:
        print("✅ 策略校验通过")
    else:
        print(f"❌ 策略校验失败: {errors}")

    # 回测
    print("\n📊 运行模拟回测...")
    engine = BacktestEngine(strategy)
    result = engine.run(200)
    print(f"   胜率: {result.win_rate:.1%}")
    print(f"   净收益: ${result.net_pnl:+.2f}")
    print(f"   最大回撤: {result.max_drawdown_pct:.1f}%")
    print(f"   Sharpe: {result.sharpe_ratio:.2f}")
    print(f"   权益: ${result.initial_capital:.0f} → ${result.final_capital:.0f}")

    # 部署
    print("\n📦 部署策略...")
    deployer = StrategyDeployer()
    paths = deployer.deploy(strategy, result)
    print(f"   JSON: {paths['json_path']}")
    print(f"   .env: {paths['env_path']}")
    print("\n✅ 演示完成\n")


def cmd_paper(text: str):
    """--paper: 从论文/描述提取策略"""
    logger.info(f"📄 从文本提取策略 ({len(text)} 字符)")
    strategy = parse_research_paper(text)

    if strategy is None:
        print("❌ 策略解析失败")
        return

    print("\n" + "=" * 60)
    print(f"📋 策略: {strategy.get('name', 'Unnamed')}")
    print(f"{json.dumps(strategy, ensure_ascii=False, indent=2)}")
    print("=" * 60)

    valid, errors = StrategyValidator.validate(strategy)
    print(f"{'✅ 校验通过' if valid else '❌ 校验失败: ' + str(errors)}")


def cmd_list():
    """--list: 列出所有可用策略模板"""
    print("\n" + "=" * 60)
    print("📚 可用策略模板")
    print("=" * 60)
    for key, template in STRATEGY_TEMPLATES.items():
        print(f"\n  [{key}] {template['name']}")
        print(f"     {template['description']}")
        print(f"     类型: {template['type']}")
        print(f"     入场: {template['entry'].get('signal_type', 'custom')}")
        print(f"     出场: TP={template['exit'].get('take_profit_pct', 'N/A')}, "
              f"SL={template['exit'].get('stop_loss_pct', 'N/A')}")
        print(f"     仓位: {template['sizing'].get('method', 'N/A')}")
    print("=" * 60 + "\n")


def cmd_show(template_key: str):
    """--show: 显示指定策略模板详情"""
    if template_key not in STRATEGY_TEMPLATES:
        print(f"❌ 未找到模板: {template_key}")
        print(f"   可用模板: {', '.join(STRATEGY_TEMPLATES.keys())}")
        return

    template = STRATEGY_TEMPLATES[template_key]
    print(f"\n{'=' * 60}")
    print(f"📋 模板: {template['name']} ({template_key})")
    print(f"{'=' * 60}")
    print(json.dumps(template, ensure_ascii=False, indent=2))
    print("=" * 60 + "\n")


def cmd_validate(path: str):
    """--validate: 校验一个策略 JSON 文件"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"❌ 无法读取文件: {e}")
        return

    strategy = data.get("strategy", data)

    print(f"\n📋 校验策略: {strategy.get('name', path)}")
    valid, errors = StrategyValidator.validate(strategy)

    if valid:
        print("✅ 策略校验通过")
    else:
        print(f"❌ 校验失败 ({len(errors)} 个问题):")
        for err in errors:
            print(f"   - {err}")
    print()


def cmd_backtest(path_or_strategy):
    """--backtest: 回测策略"""
    if isinstance(path_or_strategy, str):
        try:
            with open(path_or_strategy, "r", encoding="utf-8") as f:
                data = json.load(f)
            strategy = data.get("strategy", data)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"❌ 无法读取文件: {e}")
            return
    else:
        strategy = path_or_strategy

    engine = BacktestEngine(strategy)
    result = engine.run(500)

    print(f"\n{'=' * 60}")
    print(f"📊 回测结果: {result.strategy_name}")
    print(f"{'=' * 60}")
    print(f"   初始资金: ${result.initial_capital:,.2f}")
    print(f"   最终资金: ${result.final_capital:,.2f}")
    print(f"   总交易: {result.total_trades}")
    print(f"   胜率: {result.win_rate:.1%}")
    print(f"   净收益: ${result.net_pnl:+,.2f}")
    print(f"   毛利: ${result.gross_profit:+,.2f}")
    print(f"   毛损: ${result.gross_loss:+,.2f}")
    print(f"   盈亏比: {result.profit_factor:.2f}")
    print(f"   平均盈利: ${result.avg_win:+,.2f}")
    print(f"   平均亏损: ${result.avg_loss:+,.2f}")
    print(f"   最大回撤: {result.max_drawdown_pct:.1f}%")
    print(f"   Sharpe 比率: {result.sharpe_ratio:.2f}")
    print(f"\n   最近交易:")
    for t in result.trades[-5:]:
        print(f"     #{t['trade_no']} {t['side']} ${t['size']:.0f} → "
              f"{'✅' if t['result'] == 'win' else '❌'} ${t['pnl']:+.2f}")
    print("=" * 60 + "\n")


def cmd_deploy(path: str):
    """--deploy: 部署策略"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        strategy = data.get("strategy", data)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"❌ 无法读取文件: {e}")
        return

    valid, errors = StrategyValidator.validate(strategy)
    if not valid:
        print(f"❌ 策略校验失败，部署中止:")
        for err in errors:
            print(f"   - {err}")
        return

    # 先回测
    print("📊 部署前回测...")
    engine = BacktestEngine(strategy)
    result = engine.run(500)

    deployer = StrategyDeployer()
    paths = deployer.deploy(strategy, result)

    print(f"\n✅ 部署成功:")
    print(f"   JSON: {paths['json_path']}")
    print(f"   .env: {paths['env_path']}")
    print(f"\n   回测摘要:")
    print(f"     胜率: {result.win_rate:.1%}")
    print(f"     净收益: ${result.net_pnl:+,.2f}")
    print(f"     Sharpe: {result.sharpe_ratio:.2f}")
    print()


def cmd_auto():
    """--auto: 全自动流水线"""
    print("\n" + "=" * 60)
    print("🤖 全自动策略生成流水线")
    print("=" * 60)

    # 1. 验证连接
    print("\n[1/5] 验证 DeepSeek 连接...")
    is_live = verify_deepseek_connection()
    print(f"   {'✅ 真实 API' if is_live else '🎭 Mock 模式'}")

    # 2. 生成本地策略模板
    print("\n[2/5] 生成默认策略...")
    strategy = create_demo_strategy()
    print(f"   ✅ {strategy['name']}")

    # 3. 校验
    print("\n[3/5] 策略校验...")
    valid, errors = StrategyValidator.validate(strategy)
    if valid:
        print(f"   ✅ 校验通过")
    else:
        print(f"   ❌ 校验失败: {errors}")
        return

    # 4. 回测
    print("\n[4/5] 模拟回测...")
    engine = BacktestEngine(strategy)
    result = engine.run(500)
    print(f"   ✅ 胜率={result.win_rate:.1%}, "
          f"PnL=${result.net_pnl:+.2f}, "
          f"Sharpe={result.sharpe_ratio:.2f}")

    # 5. 部署
    print("\n[5/5] 部署...")
    deployer = StrategyDeployer()
    paths = deployer.deploy(strategy, result)
    print(f"   ✅ JSON: {paths['json_path']}")
    print(f"   ✅ .env: {paths['env_path']}")

    print("\n" + "=" * 60)
    print("✅ 全自动流水线完成")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="LLM Strategy Engine — DeepSeek Edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python llm_strategy_engine.py --demo
  python llm_strategy_engine.py --list
  python llm_strategy_engine.py --show weather_arbitrage
  python llm_strategy_engine.py --validate ./output.json
  python llm_strategy_engine.py --backtest ./output.json
  python llm_strategy_engine.py --deploy ./output.json
  python llm_strategy_engine.py --paper "基于RSI的均值回归策略..."
  python llm_strategy_engine.py --auto
        """,
    )

    parser.add_argument("--demo", action="store_true", help="演示模式")
    parser.add_argument("--paper", type=str, help="从论文/描述文本提取策略")
    parser.add_argument("--list", action="store_true", help="列出可用策略模板")
    parser.add_argument("--show", type=str, help="显示指定策略模板详情")
    parser.add_argument("--validate", type=str, help="校验策略 JSON 文件")
    parser.add_argument("--backtest", type=str, help="回测策略 JSON 文件")
    parser.add_argument("--deploy", type=str, help="部署策略 JSON 文件")
    parser.add_argument("--auto", action="store_true", help="全自动流水线")
    parser.add_argument("--verify", action="store_true", help="仅验证 DeepSeek 连接")

    args = parser.parse_args()

    # 无参数 / 仅 verify
    if args.verify or len(sys.argv) == 1:
        print("🔌 验证 DeepSeek API 连接...")
        ok = verify_deepseek_connection()
        print(f"\n{'✅ DeepSeek 连接正常' if ok else '🎭 Mock 模式运行中'}")
        return

    # 各子命令（互斥）
    if args.demo:
        cmd_demo()
    elif args.paper:
        cmd_paper(args.paper)
    elif args.list:
        cmd_list()
    elif args.show:
        cmd_show(args.show)
    elif args.validate:
        cmd_validate(args.validate)
    elif args.backtest:
        cmd_backtest(args.backtest)
    elif args.deploy:
        cmd_deploy(args.deploy)
    elif args.auto:
        cmd_auto()


if __name__ == "__main__":
    main()
