/**
 * Bybit V5 API 封装
 *
 * 处理交易所连接、订单执行、仓位管理等核心操作。
 * 支持测试网/实盘切换、市价单/限价单。
 */
import { RestClientV5 } from 'bybit-api';
import { getConfig } from '../config/index.js';

/**
 * Bybit 交易所客户端
 */
export class BybitClient {
  constructor() {
    this.config = getConfig();
    this.client = null;
    this.lastOrderTime = 0;
    this._init();
  }

  /**
   * 初始化 REST 客户端
   */
  _init() {
    const { isTestnet, apiKey, apiSecret } = this.config;

    if (!apiKey || !apiSecret) {
      console.warn('[Bybit] ⚠️ API 凭据未配置，客户端将以只读模式运行');
      console.warn('[Bybit]    请通过 Secrets 页面添加 BYBIT_API_KEY 和 BYBIT_API_SECRET');
    }

    this.client = new RestClientV5({
      testnet: isTestnet,
      key: apiKey,
      secret: apiSecret,
    });

    console.log(`[Bybit] 🔌 客户端初始化完成 (${isTestnet ? '🟡 测试网' : '🔴 实盘'})`);
  }

  /**
   * 获取当前服务器时间（用于调试和签名验证）
   */
  async getServerTime() {
    try {
      const res = await this.client.getServerTime();
      return res;
    } catch (err) {
      console.error('[Bybit] 获取服务器时间失败:', err.message);
      throw err;
    }
  }

  /**
   * 获取账户信息
   */
  async getAccountInfo() {
    try {
      const res = await this.client.getWalletBalance({ accountType: 'UNIFIED' });
      return res.result;
    } catch (err) {
      console.error('[Bybit] 获取账户信息失败:', err.message);
      throw err;
    }
  }

  /**
   * 获取 USDT 余额
   */
  async getUSDTBalance() {
    try {
      const account = await this.getAccountInfo();
      const usdtWallet = account.list?.find(w => w.coin === 'USDT');
      return usdtWallet?.walletBalance || '0';
    } catch (err) {
      console.error('[Bybit] 获取 USDT 余额失败:', err.message);
      throw err;
    }
  }

  /**
   * 设置杠杆
   * @param {string} symbol - 交易对（如 BTCUSDT）
   * @param {number} leverage - 杠杆倍数
   * @param {'isolated'|'cross'} mode - 仓位模式
   */
  async setLeverage(symbol, leverage, mode = 'isolated') {
    try {
      const res = await this.client.setLeverage({
        category: 'linear',
        symbol,
        buyLeverage: String(leverage),
        sellLeverage: String(leverage),
      });
      console.log(`[Bybit] ⚙️ 杠杆已设置: ${symbol} ${leverage}x (${mode})`);
      return res;
    } catch (err) {
      console.error(`[Bybit] 设置杠杆失败 ${symbol}:`, err.message);
      throw err;
    }
  }

  /**
   * 执行订单
   *
   * @param {object} params
   * @param {string} params.symbol         - 交易对（如 BTCUSDT）
   * @param {'Buy'|'Sell'} params.side     - 买卖方向
   * @param {string} params.qty            - 数量
   * @param {'market'|'limit'} [params.orderType='market'] - 订单类型
   * @param {string} [params.price]        - 限价单价格（orderType=limit 时必填）
   * @param {string} [params.timeInForce]  - 有效期限（限价单默认 PostOnly）
   * @returns {Promise<object>} 订单结果
   */
  async placeOrder({ symbol, side, qty, orderType = 'market', price, timeInForce }) {
    const { orderTypes, riskLimits, isTestnet } = this.config;

    // ---- 风控检查 ----
    // 请求频率限制
    const now = Date.now();
    const elapsed = now - this.lastOrderTime;
    if (elapsed < riskLimits.minOrderIntervalMs) {
      throw new Error(
        `[风控] 订单请求过于频繁，请间隔 ${riskLimits.minOrderIntervalMs}ms 以上 (当前间隔: ${elapsed}ms)`
      );
    }

    // ---- 参数验证 ----
    if (!symbol) throw new Error('缺少必填参数: symbol');
    if (!side) throw new Error('缺少必填参数: side');
    if (!qty || parseFloat(qty) <= 0) throw new Error('无效数量: qty');

    const normalizedOrderType = orderType.toLowerCase();
    const orderTypeConfig = orderTypes[normalizedOrderType];
    if (!orderTypeConfig) {
      throw new Error(`不支持的订单类型: "${orderType}"，有效值: ${Object.keys(orderTypes).join(', ')}`);
    }

    if (normalizedOrderType === 'limit' && !price) {
      throw new Error('限价单必须指定 price 参数');
    }

    // ---- 构建订单参数 ----
    const orderParams = {
      category: 'linear',                   // 永续合约
      symbol: symbol.toUpperCase(),
      side,
      orderType: orderTypeConfig.bybitValue,
      qty: String(qty),
      timeInForce: normalizedOrderType === 'limit' ? (timeInForce || 'PostOnly') : 'IOC',
    };

    if (price) {
      orderParams.price = String(price);
    }

    const symbolInfo = `[${symbol}] ${side} ${qty} @ ${normalizedOrderType}${price ? ` $${price}` : ''}`;
    console.log(`[Bybit] 📤 下单: ${symbolInfo}`);

    // ---- 测试网提示 ----
    if (isTestnet) {
      console.log('[Bybit] 🟡 测试网模式 — 订单不会实际执行');
    }

    // ---- 执行 ----
    try {
      this.lastOrderTime = now;
      const res = await this.client.submitOrder(orderParams);

      if (res.retCode === 0) {
        console.log(`[Bybit] ✅ 订单成功: orderId=${res.result.orderId}`);
      } else {
        console.error(`[Bybit] ❌ 订单失败: retCode=${res.retCode}, msg=${res.retMsg}`);
      }

      return res;
    } catch (err) {
      console.error(`[Bybit] ❌ 下单异常 ${symbolInfo}:`, err.message);
      throw err;
    }
  }

  /**
   * 平仓（通过反向市价单）
   * @param {string} symbol - 交易对
   */
  async closePosition(symbol) {
    console.log(`[Bybit] 📤 平仓: ${symbol}`);

    try {
      // 获取当前仓位
      const positions = await this.client.getPositionInfo({
        category: 'linear',
        symbol: symbol.toUpperCase(),
      });

      const pos = positions.result?.list?.[0];
      if (!pos || parseFloat(pos.size) === 0) {
        console.log(`[Bybit] ℹ️ ${symbol} 无持仓，无需平仓`);
        return { retCode: 0, retMsg: 'No position to close' };
      }

      const closeSide = pos.side === 'Buy' ? 'Sell' : 'Buy';
      const res = await this.placeOrder({
        symbol: symbol.toUpperCase(),
        side: closeSide,
        qty: pos.size,
        orderType: 'market',
      });

      console.log(`[Bybit] ✅ 平仓成功: ${symbol} (${pos.side}→${closeSide} ${pos.size})`);
      return res;
    } catch (err) {
      console.error(`[Bybit] ❌ 平仓失败 ${symbol}:`, err.message);
      throw err;
    }
  }

  /**
   * 获取当前持仓
   * @param {string} [symbol] - 可选，指定交易对
   */
  async getPositions(symbol) {
    try {
      const params = { category: 'linear' };
      if (symbol) params.symbol = symbol.toUpperCase();

      const res = await this.client.getPositionInfo(params);
      return res.result?.list || [];
    } catch (err) {
      console.error('[Bybit] 获取持仓失败:', err.message);
      throw err;
    }
  }
}

export default BybitClient;
