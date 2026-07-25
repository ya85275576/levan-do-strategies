/**
 * TradingView 信号解析器
 *
 * 将 TradingView Webhook 警报消息解析为结构化的交易指令。
 * 支持 LE VAN DO® 策略的四种信号格式：
 *   - longE  : 多头开仓
 *   - shortE : 空头开仓
 *   - longX  : 多头平仓
 *   - shortX : 空头平仓
 */
import { getConfig } from '../config/index.js';

/**
 * 解析 TradingView Webhook 消息体
 *
 * TradingView 警报消息格式（推荐使用 JSON）：
 * ```json
 * {
 *   "signal": "longE",
 *   "symbol": "BTCUSDT",
 *   "price": 50000,
 *   "tp1": 51000,
 *   "tp2": 52000,
 *   "tp3": 53000,
 *   "sl": 49000,
 *   "timestamp": "2024-01-01T00:00:00Z"
 * }
 * ```
 *
 * 也支持纯字符串格式（兼容旧版）：
 *   "longE" 或 "Long Entry"
 *
 * @param {string|object} message - TradingView 发送的警报消息
 * @returns {{ valid: boolean, signal: object, error?: string }}
 */
export function parseSignal(message) {
  const config = getConfig();
  const signalFormats = config.signalFormats;

  // ---- 1. 解析输入 ----
  let parsed;

  if (typeof message === 'string') {
    // 尝试解析 JSON
    try {
      parsed = JSON.parse(message);
    } catch {
      // 纯字符串消息
      parsed = { signal: message.trim() };
    }
  } else if (typeof message === 'object' && message !== null) {
    parsed = message;
  } else {
    return { valid: false, signal: null, error: '无效的消息格式：必须为 JSON 对象或字符串' };
  }

  // ---- 2. 提取信号标识 ----
  const rawSignal = (parsed.signal || parsed.message || '').toString().trim();

  if (!rawSignal) {
    return { valid: false, signal: null, error: '消息中未找到 signal 字段' };
  }

  // ---- 3. 匹配信号格式 ----
  // 支持的标准格式
  const signalKey = rawSignal.toLowerCase();

  // 兼容旧版 Pine Script 的 alert_message 字符串
  const legacyMapping = {
    'long entry': 'longE',
    'short entry': 'shortE',
    'long exit': 'longX',
    'short exit': 'shortX',
    'go long': 'longE',
    'go short': 'shortE',
    'long tp1': 'tp1',
    'long tp2': 'tp2',
    'long tp3': 'tp3',
    'long sl': 'sl',
    'short tp1': 'tp1',
    'short tp2': 'tp2',
    'short tp3': 'tp3',
    'short sl': 'sl',
  };

  let matchedSignal = signalFormats[signalKey]
    ? signalKey
    : legacyMapping[signalKey] || null;

  if (!matchedSignal) {
    return {
      valid: false,
      signal: null,
      error: `无法识别的信号: "${rawSignal}"，支持的信号: ${Object.keys(signalFormats).join(', ')}`,
    };
  }

  // ---- 4. 合成结构化信号 ----
  const signalDef = signalFormats[matchedSignal];
  const signal = {
    type: matchedSignal,
    action: signalDef.action,      // 'open' | 'close'
    side: signalDef.side,          // 'Buy' | 'Sell'
    description: signalDef.description,

    // 从消息中提取的交易参数
    symbol: (parsed.symbol || 'BTCUSDT').toUpperCase(),
    price: parsed.price || null,
    qty: parsed.qty || null,        // 可选：覆盖默认仓位大小

    // TP/SL（ATR 模式时由策略计算，此处为消息透传）
    tp1: parsed.tp1 || null,
    tp2: parsed.tp2 || null,
    tp3: parsed.tp3 || null,
    sl: parsed.sl || null,

    timestamp: parsed.timestamp || new Date().toISOString(),
  };

  return { valid: true, signal };
}

/**
 * 根据信号类型推断是否需要开仓还是平仓
 * @param {string} signalType
 * @returns {'enter' | 'exit'}
 */
export function getSignalAction(signalType) {
  if (['longE', 'shortE'].includes(signalType)) return 'enter';
  if (['longX', 'shortX'].includes(signalType)) return 'exit';
  return 'unknown';
}

export default { parseSignal, getSignalAction };
