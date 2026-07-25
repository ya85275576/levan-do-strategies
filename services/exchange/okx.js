/**
 * OKX V5 API 封装
 *
 * 处理交易所连接、订单执行、仓位管理等核心操作。
 * 支持测试网/实盘切换、市价单/限价单。
 *
 * OKX API 特性：
 *   - 认证三件套：API Key + Secret Key + Passphrase
 *   - V5 REST API，路径格式 /api/v5/...
 *   - 交易对格式：BTC-USDT（带连字符）
 *   - 测试网与实盘使用相同 URL，凭据区分
 */
import crypto from 'node:crypto';
import { getConfig } from '../config/index.js';

/**
 * 将统一格式的交易对转为 OKX 格式（BTCUSDT → BTC-USDT）
 */
function toOkxSymbol(symbol) {
  // 如果已包含连字符则跳过
  if (symbol.includes('-')) return symbol.toUpperCase();
  // 在币种与计价单位间插入连字符
  const match = symbol.match(/^([A-Za-z]+)(USDT|USD|USDC|BTC|ETH)$/);
  if (match) return `${match[1].toUpperCase()}-${match[2].toUpperCase()}`;
  return symbol.toUpperCase();
}

/**
 * OKX 交易所客户端
 */
export class OkxClient {
  constructor() {
    this.config = getConfig();
    this.lastOrderTime = 0;
    this._simulatedOrders = [];       // 模拟模式下的订单记录
    this._simulatedPositions = {};    // 模拟模式下的持仓记录
    this._checkCredentials();
    this._logMode();
  }

  /**
   * 输出运行模式日志
   */
  _logMode() {
    if (this.config.dryRun) {
      console.log('[OKX] 🧪 模拟模式已启用 — 所有操作仅输出日志，不实际连接交易所');
    }
  }

  /**
   * 检查凭据是否完整
   */
  _checkCredentials() {
    const { apiKey, apiSecret, apiPassphrase } = this.config;

    if (!apiKey || !apiSecret || !apiPassphrase) {
      console.warn('[OKX] ⚠️ API 凭据未完整配置（需 Key + Secret + Passphrase），客户端将以只读模式运行');
      if (!apiKey)       console.warn('[OKX]    缺少 OKX_API_KEY');
      if (!apiSecret)    console.warn('[OKX]    缺少 OKX_API_SECRET');
      if (!apiPassphrase) console.warn('[OKX]    缺少 OKX_API_PASSPHRASE');
    }

    console.log(`[OKX] 🔌 客户端初始化完成 (URL: ${this.config.baseUrl})`);
  }

  /**
   * OKX V5 HMAC-SHA256 签名
   */
  _sign(timestamp, method, requestPath, body = '') {
    const message = timestamp + method + requestPath + body;
    return crypto.createHmac('sha256', this.config.apiSecret).update(message).digest('base64');
  }

  /**
   * 生成 ISO 时间戳
   */
  _timestamp() {
    return new Date().toISOString();
  }

  /**
   * 发送已签名的 REST 请求
   * 模拟模式下仅输出日志，不真实发送
   */
  async _request(method, requestPath, body = null) {
    if (this.config.dryRun) {
      const bodyStr = body ? JSON.stringify(body) : '';
      console.log(`[OKX] [模拟] ${method} ${requestPath} ${bodyStr ? 'body: ' + bodyStr : ''}`);
      return {
        code: '0',
        msg: '模拟模式 — 请求已记录（未实际发送）',
        data: [{}],
      };
    }

    const { baseUrl, apiKey, apiPassphrase } = this.config;
    const timestamp = this._timestamp();
    const bodyStr = body ? JSON.stringify(body) : '';
    const sign = this._sign(timestamp, method, requestPath, bodyStr);

    const url = `${baseUrl}${requestPath}`;
    const headers = {
      'OK-ACCESS-KEY': apiKey,
      'OK-ACCESS-SIGN': sign,
      'OK-ACCESS-TIMESTAMP': timestamp,
      'OK-ACCESS-PASSPHRASE': apiPassphrase,
      'Content-Type': 'application/json',
    };

    const options = { method, headers };
    if (bodyStr) options.body = bodyStr;

    const response = await fetch(url, options);
    const data = await response.json();

    if (data.code !== '0') {
      const errMsg = `[OKX] API 错误: code=${data.code}, msg=${data.msg}`;
      console.error(errMsg);
      throw new Error(errMsg);
    }

    return data;
  }

  /**
   * 获取服务器时间
   */
  async getServerTime() {
    try {
      const res = await this._request('GET', '/api/v5/public/time');
      return res;
    } catch (err) {
      console.error('[OKX] 获取服务器时间失败:', err.message);
      throw err;
    }
  }

  /**
   * 获取账户信息（所有币种余额）
   */
  async getAccountInfo() {
    try {
      const res = await this._request('GET', '/api/v5/account/balance');
      return res.data;
    } catch (err) {
      console.error('[OKX] 获取账户信息失败:', err.message);
      throw err;
    }
  }

  /**
   * 获取 USDT 余额
   */
  async getUSDTBalance() {
    try {
      const data = await this.getAccountInfo();
      const details = data?.[0]?.details || [];
      const usdt = details.find(d => d.ccy === 'USDT');
      return usdt?.eq || '0';
    } catch (err) {
      console.error('[OKX] 获取 USDT 余额失败:', err.message);
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
    const instId = toOkxSymbol(symbol);
    const mgnMode = mode === 'cross' ? 'cross' : 'isolated';

    try {
      const res = await this._request('POST', '/api/v5/account/set-leverage', {
        instId,
        lever: String(leverage),
        mgnMode,
      });
      console.log(`[OKX] ⚙️ 杠杆已设置: ${instId} ${leverage}x (${mgnMode})`);
      return res;
    } catch (err) {
      console.error(`[OKX] 设置杠杆失败 ${instId}:`, err.message);
      throw err;
    }
  }

  /**
   * 执行订单
   *
   * @param {object} params
   * @param {string} params.symbol          - 交易对（如 BTCUSDT）
   * @param {'buy'|'sell'} params.side      - 买卖方向
   * @param {string} params.qty             - 数量
   * @param {'market'|'limit'} [params.orderType='market'] - 订单类型
   * @param {string} [params.price]         - 限价单价格
   * @returns {Promise<object>} 订单结果
   */
  async placeOrder({ symbol, side, qty, orderType = 'market', price }) {
    const { orderTypes, riskLimits } = this.config;

    // ---- 风控检查 ----
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

    const instId = toOkxSymbol(symbol);

    // ---- 构建订单参数 ----
    const orderParams = {
      instId,
      tdMode: this.config.positionMode === 'cross' ? 'cross' : 'isolated',
      side: side.toLowerCase(),
      ordType: orderTypeConfig.okxValue,
      sz: String(qty),
    };

    // 限价单需要价格
    if (normalizedOrderType === 'limit' && price) {
      orderParams.px = String(price);
    }

    const symbolInfo = `[${instId}] ${side} ${qty} @ ${normalizedOrderType}${price ? ` $${price}` : ''}`;
    console.log(`[OKX] 📤 下单: ${symbolInfo}`);

    // ---- 执行 ----
    try {
      this.lastOrderTime = now;
      const res = await this._request('POST', '/api/v5/trade/order', orderParams);

      const orderId = res.data?.[0]?.ordId || 'sim-' + Date.now();
      console.log(`[OKX] ✅ 订单成功: orderId=${orderId}`);

      // 模拟模式：记录订单并更新虚拟持仓
      if (this.config.dryRun) {
        const orderRecord = {
          id: orderId,
          symbol: instId,
          side: side.toLowerCase(),
          qty: parseFloat(qty),
          type: normalizedOrderType,
          price: price || null,
          time: new Date().toISOString(),
        };
        this._simulatedOrders.push(orderRecord);

        // 更新模拟持仓
        const posKey = instId;
        const currentPos = this._simulatedPositions[posKey] || 0;
        if (side.toLowerCase() === 'buy') {
          this._simulatedPositions[posKey] = currentPos + parseFloat(qty);
        } else {
          this._simulatedPositions[posKey] = currentPos - parseFloat(qty);
        }
        console.log(`[OKX] [模拟持仓] ${instId}: ${this._simulatedPositions[posKey].toFixed(4)}`);
      }

      return res;
    } catch (err) {
      console.error(`[OKX] ❌ 下单异常 ${symbolInfo}:`, err.message);
      throw err;
    }
  }

  /**
   * 平仓
   * @param {string} symbol - 交易对
   */
  async closePosition(symbol) {
    const instId = toOkxSymbol(symbol);
    console.log(`[OKX] 📤 平仓: ${instId}`);

    try {
      // 模拟模式：直接查询虚拟持仓
      if (this.config.dryRun) {
        const posKey = instId;
        const posSize = this._simulatedPositions[posKey] || 0;
        if (posSize === 0) {
          console.log(`[OKX] ℹ️ [模拟] ${instId} 无持仓，无需平仓`);
          return { code: '0', msg: 'No position to close (simulated)' };
        }

        const posSide = posSize > 0 ? 'long' : 'short';
        const closeQty = Math.abs(posSize);
        console.log(`[OKX] [模拟] 平仓 ${instId} (${posSide}): ${closeQty}`);

        // 清零持仓
        this._simulatedPositions[posKey] = 0;

        return {
          code: '0',
          msg: `模拟平仓成功: ${instId} ${posSide} ${closeQty}`,
          data: [{ ordId: 'sim-close-' + Date.now() }],
        };
      }

      // 获取当前持仓
      const positions = await this.getPositions(symbol);
      const pos = positions?.[0];

      if (!pos || parseFloat(pos.pos) === 0) {
        console.log(`[OKX] ℹ️ ${instId} 无持仓，无需平仓`);
        return { code: '0', msg: 'No position to close' };
      }

      // 使用 OKX 平仓接口
      const res = await this._request('POST', '/api/v5/trade/close-position', {
        instId,
        mgnMode: pos.mgnMode || this.config.positionMode,
        posSide: pos.posSide, // 'long' or 'short'
      });

      console.log(`[OKX] ✅ 平仓成功: ${instId}`);
      return res;
    } catch (err) {
      console.error(`[OKX] ❌ 平仓失败 ${instId}:`, err.message);
      throw err;
    }
  }

  /**
   * 获取当前持仓
   * @param {string} [symbol] - 可选，指定交易对
   */
  async getPositions(symbol) {
    // 模拟模式：返回虚拟持仓数据
    if (this.config.dryRun) {
      if (symbol) {
        const instId = toOkxSymbol(symbol);
        const posSize = this._simulatedPositions[instId] || 0;
        if (posSize === 0) {
          return [];
        }
        return [{
          instId,
          pos: String(posSize),
          posSide: posSize > 0 ? 'long' : 'short',
          mgnMode: this.config.positionMode,
          uTime: Date.now().toString(),
        }];
      }

      // 返回所有持仓
      return Object.entries(this._simulatedPositions)
        .filter(([_, v]) => v !== 0)
        .map(([instId, posSize]) => ({
          instId,
          pos: String(posSize),
          posSide: posSize > 0 ? 'long' : 'short',
          mgnMode: this.config.positionMode,
          uTime: Date.now().toString(),
        }));
    }

    try {
      let requestPath = '/api/v5/account/positions';
      if (symbol) {
        const instId = toOkxSymbol(symbol);
        requestPath += `?instId=${instId}`;
      }

      const res = await this._request('GET', requestPath);
      return res.data || [];
    } catch (err) {
      console.error('[OKX] 获取持仓失败:', err.message);
      throw err;
    }
  }

  /**
   * 获取模拟模式下的订单记录
   */
  getSimulatedOrders() {
    return [...this._simulatedOrders];
  }

  /**
   * 获取模拟模式下的持仓快照
   */
  getSimulatedPositions() {
    return { ...this._simulatedPositions };
  }

  /**
   * 重置模拟状态
   */
  resetSimulation() {
    this._simulatedOrders = [];
    this._simulatedPositions = {};
    console.log('[OKX] 🔄 模拟状态已重置');
  }

  /**
   * 获取交易产品信息（合约规格）
   * @param {string} [symbol] - 可选，指定交易对
   */
  async getInstruments(symbol) {
    try {
      let requestPath = '/api/v5/public/instruments?instType=SWAP';
      if (symbol) {
        const instId = toOkxSymbol(symbol);
        requestPath += `&instId=${instId}`;
      }

      const res = await this._request('GET', requestPath);
      return res.data || [];
    } catch (err) {
      console.error('[OKX] 获取产品信息失败:', err.message);
      throw err;
    }
  }
}

export default OkxClient;
