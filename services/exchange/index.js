/**
 * 交易所工厂
 *
 * 统一获取交易所客户端实例，支持未来扩展多个交易所。
 * 当前支持: BYBIT
 */

import BybitClient from './bybit.js';

let _instance = null;

/**
 * 获取交易所客户端（单例）
 * @returns {BybitClient}
 */
export function getExchangeClient() {
  if (!_instance) {
    _instance = new BybitClient();
  }
  return _instance;
}

/**
 * 重置客户端（切换环境或凭据时使用）
 */
export function resetExchangeClient() {
  _instance = null;
}

export default { getExchangeClient, resetExchangeClient };
