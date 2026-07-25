/**
 * Webhook 请求验证器
 *
 * 验证 TradingView Webhook 请求的合法性：
 * 1. 共享密钥验证（推荐）
 * 2. 请求体格式验证
 * 3. 风控校验
 */
import { getConfig } from '../config/index.js';
import { parseSignal } from '../signals/parser.js';

/**
 * 验证 Webhook 请求的完整流程
 *
 * @param {object} body - 请求体
 * @param {object} headers - 请求头
 * @returns {{ valid: boolean, data?: object, error?: string, statusCode?: number }}
 */
export function validateWebhookRequest(body, headers) {
  const config = getConfig();

  // ---- 1. 请求体存在性检查 ----
  if (!body || (typeof body === 'object' && Object.keys(body).length === 0)) {
    return {
      valid: false,
      error: '请求体为空',
      statusCode: 400,
    };
  }

  // ---- 2. 共享密钥验证 ----
  const authHeader =
    headers['x-webhook-secret'] ||
    headers['x-webhook-token'] ||
    headers['authorization'] ||
    '';

  const receivedSecret = authHeader.replace(/^Bearer\s+/i, '').trim();

  if (config.webhookSecret && receivedSecret !== config.webhookSecret) {
    console.warn(`[Webhook] ⚠️ 密钥验证失败: 收到 "${receivedSecret}", 期望 "${config.webhookSecret}"`);
    return {
      valid: false,
      error: 'Webhook 密钥不匹配',
      statusCode: 401,
    };
  }

  // ---- 3. 信号内容解析 ----
  const signalResult = parseSignal(body);

  if (!signalResult.valid) {
    console.warn(`[Webhook] ⚠️ 信号解析失败: ${signalResult.error}`);
    return {
      valid: false,
      error: signalResult.error,
      statusCode: 400,
    };
  }

  // ---- 4. 风控：不在允许的交易对列表时拒绝 ----
  const allowedSymbols = Object.keys(config.symbols).map(s => s.toUpperCase());
  if (!allowedSymbols.includes(signalResult.signal.symbol)) {
    return {
      valid: false,
      error: `交易对 ${signalResult.signal.symbol} 不在允许列表中 (允许: ${allowedSymbols.join(', ')})`,
      statusCode: 400,
    };
  }

  return {
    valid: true,
    data: signalResult.signal,
  };
}

export default { validateWebhookRequest };
