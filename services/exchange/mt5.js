/**
 * MT5 交易所客戶端封裝 (Node.js)
 *
 * 透過 Python 子行程 (services/mt5/server.py) 的 stdin/stdout JSON IPC 協議
 * 與 MetaTrader5 終端通信。支援模擬模式（DRY_RUN=true），無需 MT5 終端。
 *
 * 遵循 services/exchange/okx.js 的相同介面模式，
 * 讓 exchange/index.js 工廠可以統一調用。
 */
import { spawn } from 'node:child_process';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getConfig } from '../config/index.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, '..', '..');
const PYTHON_BIN = process.env.MT5_PYTHON || 'python3';

let _requestId = 0;
let _pending = new Map();  // id -> { resolve, reject }
let _pythonProcess = null;
let _buffer = '';

/**
 * MT5 交易所客戶端
 */
export class Mt5Client {
  constructor() {
    this.config = getConfig();
    this._simulatedOrders = [];
    this._simulatedPositions = {};
    this._ready = false;
    this._startupError = null;
    this._logMode();
  }

  _logMode() {
    if (this.config.dryRun) {
      console.log('[MT5] 🧪 模擬模式已啟用 — Python 子行程不啟動，所有操作僅在 JS 層模擬');
    }
  }

  /**
   * 初始化並啟動 Python IPC 子行程
   */
  async _ensurePythonProcess() {
    if (this._ready) return;
    if (this.config.dryRun) {
      this._ready = true;
      return;
    }
    if (this._startupError) {
      throw new Error(this._startupError);
    }

    const serverPath = resolve(PROJECT_ROOT, 'services', 'mt5', 'server.py');

    try {
      this._pythonProcess = spawn(PYTHON_BIN, [serverPath], {
        cwd: PROJECT_ROOT,
        stdio: ['pipe', 'pipe', 'pipe'],
        env: {
          ...process.env,
          DRY_RUN: 'false',
          // 傳遞 MT5 憑證
          MT5_ACCOUNT: process.env.MT5_ACCOUNT || '',
          MT5_PASSWORD: process.env.MT5_PASSWORD || '',
          MT5_SERVER: process.env.MT5_SERVER || 'ICMarkets-Demo',
          MT5_PATH: process.env.MT5_PATH || '',
        },
      });

      this._pythonProcess.stdout.on('data', (chunk) => {
        this._handleStdout(chunk);
      });

      this._pythonProcess.stderr.on('data', (chunk) => {
        console.error('[MT5 IPC]', chunk.toString().trim());
      });

      this._pythonProcess.on('exit', (code) => {
        console.warn(`[MT5] ⚠️ Python 子行程異常退出 (code=${code})`);
        this._ready = false;
        this._pythonProcess = null;
        // 拒絕所有待處理請求
        for (const [id, { reject }] of _pending) {
          reject(new Error(`Python 子行程已退出 (code=${code})`));
          _pending.delete(id);
        }
      });

      this._pythonProcess.on('error', (err) => {
        this._startupError = `Python 子行程啟動失敗: ${err.message}`;
        this._ready = false;
        console.error(`[MT5] ❌ ${this._startupError}`);
        // 拒絕所有待處理請求
        for (const [id, { reject }] of _pending) {
          reject(new Error(this._startupError));
          _pending.delete(id);
        }
      });

      // 發送初始化請求
      await this._sendIpc('initialize');
      this._ready = true;
      console.log('[MT5] ✅ Python IPC 子行程就緒');
    } catch (err) {
      this._startupError = err.message;
      throw err;
    }
  }

  /**
   * 處理 Python 行程 stdout（JSON 行協議）
   */
  _handleStdout(chunk) {
    _buffer += chunk.toString();
    const lines = _buffer.split('\n');
    _buffer = lines.pop(); // 保留不完整的行

    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const response = JSON.parse(line);
        const { id, result, error } = response;
        const pending = _pending.get(id);
        if (pending) {
          _pending.delete(id);
          if (error) {
            pending.reject(new Error(error));
          } else {
            pending.resolve(result);
          }
        }
      } catch (err) {
        console.warn(`[MT5 IPC] JSON 解析失敗: ${err.message} (行: ${line.slice(0, 100)})`);
      }
    }
  }

  /**
   * 發送 IPC 請求到 Python 子行程
   */
  async _sendIpc(method, params = {}) {
    if (this.config.dryRun) {
      // 模擬模式：由 JS 層直接處理，不啟動 Python
      return this._simulateIpc(method, params);
    }

    await this._ensurePythonProcess();

    return new Promise((resolve, reject) => {
      const id = ++_requestId;
      const request = JSON.stringify({ id, method, params }) + '\n';
      _pending.set(id, { resolve, reject });

      try {
        this._pythonProcess.stdin.write(request);
      } catch (err) {
        _pending.delete(id);
        reject(new Error(`寫入 IPC 失敗: ${err.message}`));
      }

      // 30 秒超時
      setTimeout(() => {
        if (_pending.has(id)) {
          _pending.delete(id);
          reject(new Error(`IPC 請求逾時 (method=${method}, id=${id})`));
        }
      }, 30000);
    });
  }

  /**
   * 模擬 IPC 回應（不啟動 Python 行程）
   */
  _simulateIpc(method, params = {}) {
    switch (method) {
      case 'initialize':
        return { success: true, message: '模擬初始化成功' };
      case 'shutdown':
        return { success: true, message: '模擬關閉成功' };
      case 'ping':
        return {
          success: true,
          data: { initialized: true, dry_run: true, mt5_available: false, python_version: 'simulated' },
        };
      case 'get_account_info':
        return {
          success: true,
          data: {
            login: 0, balance: 100000.0, equity: 100000.0, profit: 0.0,
            margin: 0.0, margin_free: 100000.0, margin_level: 0.0,
            name: 'Simulated MT5', server: 'Simulated', currency: 'USD',
            leverage: 100, trade_allowed: true, _mode: 'simulated',
          },
        };
      case 'get_positions':
        return {
          success: true,
          data: Object.entries(this._simulatedPositions)
            .filter(([_, v]) => Math.abs(v) > 1e-10)
            .map(([symbol, qty]) => ({
              ticket: 0, symbol, type: qty > 0 ? 'buy' : 'sell',
              volume: Math.abs(qty), price_open: 0, sl: 0, tp: 0,
              profit: 0, swap: 0, comment: 'simulated', magic: 234000,
              time: new Date().toISOString(),
            })),
        };
      case 'place_order': {
        const { symbol, side, qty } = params;
        const ticket = Date.now();
        const orderRecord = {
          ticket, symbol, type: side, volume: parseFloat(qty),
          price: 0, sl: params.sl || 0, tp: params.tp || 0,
          order_type: params.order_type || 'market',
          time: new Date().toISOString(), _mode: 'simulated',
        };
        this._simulatedOrders.push(orderRecord);

        const posKey = symbol.toUpperCase();
        const currentQty = this._simulatedPositions[posKey] || 0;
        const delta = side === 'buy' ? parseFloat(qty) : -parseFloat(qty);
        this._simulatedPositions[posKey] = currentQty + delta;
        if (Math.abs(this._simulatedPositions[posKey]) < 1e-10) {
          delete this._simulatedPositions[posKey];
        }

        console.log(`[MT5] [模擬] ${side.toUpperCase()} ${qty} ${symbol} @ 市價`);
        return {
          success: true,
          data: { ticket, order: orderRecord, message: `模擬下單成功 (ticket #${ticket})` },
        };
      }
      case 'close_position': {
        const targetSymbol = params.symbol ? params.symbol.toUpperCase() : null;
        if (targetSymbol) {
          const qty = this._simulatedPositions[targetSymbol] || 0;
          if (Math.abs(qty) < 1e-10) {
            return { success: true, data: [], message: `[模擬] ${targetSymbol} 無持倉` };
          }
          const side = qty > 0 ? 'long' : 'short';
          console.log(`[MT5] [模擬] 平倉: ${targetSymbol} (${side}) ${Math.abs(qty)}`);
          delete this._simulatedPositions[targetSymbol];
          return { success: true, data: [{ ticket: 0, symbol: targetSymbol, volume: Math.abs(qty), success: true }], message: `[模擬] 平倉成功: ${targetSymbol}` };
        }
        const total = Object.keys(this._simulatedPositions).length;
        this._simulatedPositions = {};
        return { success: true, data: [], message: `[模擬] 全部平倉完成: ${total} 個持倉` };
      }
      case 'get_orders':
        return { success: true, data: [...this._simulatedOrders], message: `[模擬] ${this._simulatedOrders.length} 筆記錄` };
      case 'get_simulated_orders':
        return { success: true, data: [...this._simulatedOrders] };
      case 'get_simulated_positions':
        return { success: true, data: { ...this._simulatedPositions } };
      case 'reset_simulation':
        this._simulatedOrders = [];
        this._simulatedPositions = {};
        return { success: true, message: '模擬狀態已重置' };
      default:
        return { success: false, error: `未知方法: ${method}` };
    }
  }

  // ═══════════════════════════════════════════════════
  // 公開 API（與 OkxClient 一致的介面）
  // ═══════════════════════════════════════════════════

  /**
   * 初始化 MT5 連線
   */
  async initialize() {
    try {
      const result = await this._sendIpc('initialize');
      console.log(result.message ? `[MT5] ℹ️ ${result.message}` : '');
      return result;
    } catch (err) {
      console.error('[MT5] ❌ 初始化失敗:', err.message);
      throw err;
    }
  }

  /**
   * 關閉 MT5 連線
   */
  async shutdown() {
    try {
      const result = await this._sendIpc('shutdown');
      if (this._pythonProcess) {
        this._pythonProcess.stdin.end();
        this._pythonProcess = null;
      }
      this._ready = false;
      return result;
    } catch (err) {
      console.error('[MT5] 關閉失敗:', err.message);
      throw err;
    }
  }

  /**
   * 查詢帳戶資訊
   */
  async getAccountInfo() {
    try {
      const result = await this._sendIpc('get_account_info');
      return result.data || result;
    } catch (err) {
      console.error('[MT5] 取得帳戶資訊失敗:', err.message);
      throw err;
    }
  }

  /**
   * 查詢持倉
   * @param {string} [symbol] - 交易品種（如 BTCUSD），可選
   */
  async getPositions(symbol) {
    try {
      const params = symbol ? { symbol: symbol.toUpperCase() } : {};
      const result = await this._sendIpc('get_positions', params);
      return result.data || [];
    } catch (err) {
      console.error('[MT5] 取得持倉失敗:', err.message);
      throw err;
    }
  }

  /**
   * 設置槓桿（MT5 在帳戶層級設定，此處僅記錄）
   * @param {string} symbol - 交易品種
   * @param {number} leverage - 槓桿倍數
   */
  async setLeverage(symbol, leverage) {
    console.log(`[MT5] ⚙️ 槓桿設置請求: ${symbol} ${leverage}x (MT5 帳戶層級設定，若未預設則無效)`);
    // MT5 槓桿由帳戶/券商設定，無法透過 API 動態修改
    return { code: '0', msg: `MT5: 槓桿 ${leverage}x 請求已記錄（請確認 MT5 帳戶槓桿設定）` };
  }

  /**
   * 執行市價單
   * @param {object} params
   * @param {string} params.symbol   - 交易品種（如 BTCUSD）
   * @param {string} params.side     - buy | sell
   * @param {string} params.qty      - 數量（手數）
   * @param {string} [params.orderType] - market | limit
   * @param {string} [params.price]  - 限價單價格
   */
  async placeOrder(params) {
    const { symbol, side, qty, orderType = 'market', price } = params;

    if (!symbol) throw new Error('缺少必填參數: symbol');
    if (!['buy', 'sell'].includes((side || '').toLowerCase())) {
      throw new Error('side 必須為 buy 或 sell');
    }
    if (!qty || parseFloat(qty) <= 0) throw new Error('無效數量: qty');

    const mt5Symbol = this._toMt5Symbol(symbol);
    const orderParams = {
      symbol: mt5Symbol,
      side: side.toLowerCase(),
      qty: parseFloat(qty),
      order_type: orderType.toLowerCase(),
    };
    if (price) orderParams.price = parseFloat(price);

    const symbolInfo = `[${mt5Symbol}] ${side} ${qty} @ ${orderType}${price ? ` $${price}` : ''}`;
    console.log(`[MT5] 📤 下單: ${symbolInfo}`);

    try {
      const result = await this._sendIpc('place_order', orderParams);
      if (result.success) {
        console.log(`[MT5] ✅ 訂單成功: ticket #${result.data?.ticket}`);
      } else {
        console.error(`[MT5] ❌ 下單失敗: ${result.error}`);
      }
      return result;
    } catch (err) {
      console.error(`[MT5] ❌ 下單異常 ${symbolInfo}:`, err.message);
      throw err;
    }
  }

  /**
   * 平倉
   * @param {string} symbol - 交易品種
   */
  async closePosition(symbol) {
    const mt5Symbol = this._toMt5Symbol(symbol);
    console.log(`[MT5] 📤 平倉: ${mt5Symbol}`);

    try {
      const result = await this._sendIpc('close_position', { symbol: mt5Symbol });
      if (result.success) {
        console.log(`[MT5] ✅ 平倉完成: ${mt5Symbol}`);
      } else {
        console.warn(`[MT5] ℹ️ 平倉: ${result.message || result.error}`);
      }
      return result;
    } catch (err) {
      console.error(`[MT5] ❌ 平倉失敗 ${mt5Symbol}:`, err.message);
      throw err;
    }
  }

  /**
   * 查詢歷史訂單
   */
  async getOrders() {
    try {
      const result = await this._sendIpc('get_orders');
      return result.data || [];
    } catch (err) {
      console.error('[MT5] 取得訂單失敗:', err.message);
      throw err;
    }
  }

  /**
   * 取得模擬訂單記錄
   */
  getSimulatedOrders() {
    return [...this._simulatedOrders];
  }

  /**
   * 取得模擬持倉快照
   */
  getSimulatedPositions() {
    return { ...this._simulatedPositions };
  }

  /**
   * 重置模擬狀態
   */
  resetSimulation() {
    this._simulatedOrders = [];
    this._simulatedPositions = {};
    console.log('[MT5] 🔄 模擬狀態已重置');
  }

  /**
   * 將統一格式交易對轉為 MT5 格式
   * BTCUSDT → BTCUSD（MT5 沒有 USDT 結尾，通常是 USD）
   * ETHUSDT → ETHUSD
   * 注意：實際需要根據券商設定調整
   */
  _toMt5Symbol(symbol) {
    if (!symbol) return symbol;
    let s = symbol.toUpperCase();
    // MT5 通常使用 6 字元格式如 BTCUSD, ETHUSD
    if (s.endsWith('USDT')) {
      s = s.replace('USDT', 'USD');
    }
    return s;
  }
}

export default Mt5Client;
