/**
 * 交易所工厂
 *
 * 统一获取交易所客户端实例，支持未来扩展多个交易所。
 * 当前支持: OKX, MT5
 *
 * 切换方式：設定 EXCHANGE_TYPE=okx 或 EXCHANGE_TYPE=mt5（環境變數）
 * 預設為 okx（保持向後相容）
 */

import OkxClient from './okx.js';
import Mt5Client from './mt5.js';

let _instance = null;
let _currentType = null;

/**
 * 取得目前設定的交易所類型
 */
function getExchangeType() {
  return (process.env.EXCHANGE_TYPE || 'okx').toLowerCase();
}

/**
 * 获取交易所客户端（单例）
 * @returns {OkxClient|Mt5Client}
 */
export function getExchangeClient() {
  const type = getExchangeType();

  if (!_instance || _currentType !== type) {
    _currentType = type;

    switch (type) {
      case 'mt5':
        console.log('[Exchange] 🔀 使用 MT5 執行後端');
        _instance = new Mt5Client();
        break;
      case 'okx':
      default:
        console.log('[Exchange] 🔀 使用 OKX 執行後端');
        _instance = new OkxClient();
        break;
    }
  }

  return _instance;
}

/**
 * 重置客户端（切换环境或凭据时使用）
 */
export function resetExchangeClient() {
  _instance = null;
  _currentType = null;
}

export default { getExchangeClient, resetExchangeClient };
