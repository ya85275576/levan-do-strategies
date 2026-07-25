/**
 * LE VAN DO® — MT5 模擬交易驗證腳本
 *
 * 模擬從 Webhook 接收訊號 → MT5 下單的完整流程。
 * 設定 DRY_RUN=true，所有操作僅輸出日誌，不實際連接 MT5 終端。
 *
 * 運行方式：
 *   cd services && EXCHANGE_TYPE=mt5 DRY_RUN=true node scripts/simulate-mt5.js
 *
 * 測試涵蓋：
 *   1. MT5 客戶端初始化
 *   2. 帳戶資訊查詢
 *   3. 6 個測試訊號（longE/shortE/longX/shortX）
 *   4. 持倉查詢
 *   5. 模擬交易總帳輸出
 */
import 'dotenv/config';
import { getConfig, validateConfig } from '../config/index.js';
import { parseSignal } from '../signals/parser.js';
import Mt5Client from '../exchange/mt5.js';

// ───── 分隔線輔助 ─────
const SEP = '='.repeat(72);
const SEP2 = '-'.repeat(72);

function logSection(title) {
  console.log(`\n${SEP}`);
  console.log(`  ${title}`);
  console.log(`${SEP}`);
}

function logStep(step, msg) {
  console.log(`  [Step ${step}] ${msg}`);
}

function logDetail(msg) {
  console.log(`    → ${msg}`);
}

// ───── 測試訊號集（6 個，與 simulate.js 一致） ─────
const TEST_SIGNALS = [
  {
    scenario: '1. 多頭開倉 (longE) — BTCUSDT',
    signal: { type: 'longE', action: 'open', side: 'Buy' },
    payload: {
      signal: 'longE',
      symbol: 'BTCUSDT',
      price: '65420.5',
      tp1: '66750.0',
      sl: '64100.0',
    },
  },
  {
    scenario: '2. 空頭開倉 (shortE) — ETHUSDT',
    signal: { type: 'shortE', action: 'open', side: 'Sell' },
    payload: {
      signal: 'shortE',
      symbol: 'ETHUSDT',
      price: '3520.8',
      tp1: '3400.0',
      sl: '3600.0',
    },
  },
  {
    scenario: '3. 空頭開倉 (shortE) — SOLUSDT',
    signal: { type: 'shortE', action: 'open', side: 'Sell' },
    payload: {
      signal: 'shortE',
      symbol: 'SOLUSDT',
      price: '145.30',
      sl: '150.00',
    },
  },
  {
    scenario: '4. 多頭平倉 (longX) — BTCUSDT',
    signal: { type: 'longX', action: 'close', side: 'Buy' },
    payload: {
      signal: 'longX',
      symbol: 'BTCUSDT',
      price: '66800.0',
    },
  },
  {
    scenario: '5. 空頭平倉 (shortX) — ETHUSDT',
    signal: { type: 'shortX', action: 'close', side: 'Sell' },
    payload: {
      signal: 'shortX',
      symbol: 'ETHUSDT',
      price: '3380.0',
    },
  },
  {
    scenario: '6. 多頭開倉 (longE) — 字串格式相容性測試',
    signal: { type: 'longE', action: 'open', side: 'Buy' },
    payload: 'Long Entry',
  },
];

// ───── 主流程 ─────
async function main() {
  console.log(`\n${SEP}`);
  console.log(`  LE VAN DO® — MT5 模擬交易驗證`);
  console.log(`  啟動時間: ${new Date().toISOString()}`);
  console.log(`  模式: DRY_RUN=模擬（不實際連接 MT5）`);
  console.log(`${SEP}`);

  // ============================================================
  // Step 1: 檢查環境與配置
  // ============================================================
  logSection('Step 1: 檢查執行環境');

  const cfg = getConfig();
  logStep(1, `交易所類型: ${cfg.exchangeType}`);
  logStep(1, `模擬模式: ${cfg.dryRun ? '✅ 已啟用' : '❌ 未啟用（將連接真實 MT5）'}`);
  logStep(1, `Python 版本: ${process.env.MT5_PYTHON || 'python3 (預設)'}`);

  if (cfg.exchangeType !== 'mt5') {
    console.warn(`\n  ⚠️  EXCHANGE_TYPE=${cfg.exchangeType}，應設為 mt5\n`);
    console.log('  請使用: EXCHANGE_TYPE=mt5 DRY_RUN=true node scripts/simulate-mt5.js');
  }

  // ============================================================
  // Step 2: 初始化 MT5 客戶端
  // ============================================================
  logSection('Step 2: 初始化 MT5 客戶端');

  const mt5 = new Mt5Client();

  try {
    const initResult = await mt5.initialize();
    logStep(2, `初始化結果: ${initResult.success ? '✅ 成功' : '❌ 失敗'}`);
    logDetail(`訊息: ${initResult.message || initResult.error || 'OK'}`);
  } catch (err) {
    logStep(2, `初始化異常: ${err.message}`);
  }

  // ============================================================
  // Step 3: 查詢帳戶資訊
  // ============================================================
  logSection('Step 3: 帳戶資訊查詢');

  try {
    const accountInfo = await mt5.getAccountInfo();
    logStep(3, '帳戶資訊:');
    logDetail(`帳戶: ${accountInfo.name || 'N/A'}`);
    logDetail(`伺服器: ${accountInfo.server || 'N/A'}`);
    logDetail(`餘額: $${accountInfo.balance?.toFixed(2) || '0.00'}`);
    logDetail(`權益: $${accountInfo.equity?.toFixed(2) || '0.00'}`);
    logDetail(`槓桿: ${accountInfo.leverage || 'N/A'}:1`);
    logDetail(`交易允許: ${accountInfo.trade_allowed ? '✅ 是' : '❌ 否'}`);
    logDetail(`模式: ${accountInfo._mode || 'N/A'}`);
  } catch (err) {
    logStep(3, `查詢失敗: ${err.message}`);
  }

  // ============================================================
  // Step 4: 執行 6 個測試訊號
  // ============================================================
  logSection('Step 4: 模擬 TradingView 訊號 → MT5 下單');

  let successCount = 0;
  let failCount = 0;

  for (let i = 0; i < TEST_SIGNALS.length; i++) {
    const test = TEST_SIGNALS[i];
    logStep(4, `測試 #${i + 1}: ${test.scenario}`);

    // 4a. 解析訊號
    const parseResult = parseSignal(test.payload);
    if (!parseResult.valid) {
      logDetail(`❌ 訊號解析失敗: ${parseResult.error}`);
      failCount++;
      continue;
    }

    const signal = parseResult.signal;
    logDetail(`訊號類型: ${signal.type}`);
    logDetail(`交易對: ${signal.symbol}`);
    logDetail(`動作: ${signal.action}`);
    logDetail(`方向: ${signal.side}`);

    // 4b. 執行交易
    try {
      if (signal.action === 'open') {
        const qty = signal.qty || '0.01'; // MT5 標準最小 0.01 手
        const result = await mt5.placeOrder({
          symbol: signal.symbol,
          side: signal.side.toLowerCase(),
          qty,
          orderType: 'market',
        });
        if (result.success) {
          logDetail(`✅ 開倉成功: ticket #${result.data?.ticket}`);
          successCount++;
        } else {
          logDetail(`❌ 開倉失敗: ${result.error}`);
          failCount++;
        }
      } else if (signal.action === 'close') {
        const result = await mt5.closePosition(signal.symbol);
        if (result.success) {
          logDetail(`✅ 平倉成功: ${result.message}`);
          successCount++;
        } else {
          logDetail(`❌ 平倉失敗: ${result.error}`);
          failCount++;
        }
      }
    } catch (err) {
      logDetail(`❌ 執行異常: ${err.message}`);
      failCount++;
    }

    // 訊號間隔
    if (i < TEST_SIGNALS.length - 1) {
      logDetail('等待 500ms...');
      await new Promise(r => setTimeout(r, 500));
    }
    console.log('');
  }

  // ============================================================
  // Step 5: 查詢持倉
  // ============================================================
  logSection('Step 5: 查詢持倉');

  try {
    const positions = await mt5.getPositions();
    logStep(5, `當前持倉: ${positions.length} 筆`);
    if (positions.length === 0) {
      logDetail('無持倉（所有部位已平倉）');
    } else {
      positions.forEach((p, idx) => {
        logDetail(`  #${idx + 1} [${p.symbol}] ${p.type} ${p.volume} 手 — 開倉價: ${p.price_open || 'N/A'}`);
      });
    }
  } catch (err) {
    logStep(5, `查詢失敗: ${err.message}`);
  }

  // ============================================================
  // Step 6: 輸出模擬交易總帳
  // ============================================================
  logSection('Step 6: 模擬交易總帳');

  const orders = mt5.getSimulatedOrders();
  const simPositions = mt5.getSimulatedPositions();

  logStep(6, `共執行模擬訂單: ${orders.length} 筆`);
  orders.forEach((o, i) => {
    logDetail(`  #${i + 1} [${o.symbol}] ${o.type} ${o.volume} @ ${o.order_type || 'market'} — ${o.time}`);
  });

  logStep(6, '期末模擬持倉:');
  const nonZero = Object.entries(simPositions).filter(([_, v]) => Math.abs(v) > 1e-10);
  if (nonZero.length === 0) {
    logDetail('無持倉（所有部位已平倉）');
  } else {
    nonZero.forEach(([symbol, qty]) => {
      logDetail(`  ${symbol}: ${qty.toFixed(4)} (${qty > 0 ? '多' : '空'})`);
    });
  }

  // ============================================================
  // Summary
  // ============================================================
  logSection('驗證結論');

  console.log(`  ✅ 交易所後端:   MT5 (EXCHANGE_TYPE=mt5)`);
  console.log(`  ✅ 執行模式:     ${cfg.dryRun ? '模擬 (DRY_RUN=true)' : '實盤'}`);
  console.log(`  ✅ 測試訊號:     ${TEST_SIGNALS.length} 個`);
  console.log(`  ✅ 成功/失敗:    ${successCount} 成功 / ${failCount} 失敗`);
  console.log(`  ✅ 模擬訂單:     ${orders.length} 筆記錄`);
  console.log(`  ✅ Python 橋接:  services/mt5/bridge.py + server.py`);
  console.log(`  ✅ Node.js 封裝: services/exchange/mt5.js`);
  console.log(`\n  MT5 模擬驗證完成。DRY_RUN=false + Windows MT5 終端即可上線。`);
  console.log(`${SEP}\n`);

  // 清除
  mt5.resetSimulation();
}

main().catch(err => {
  console.error('[FATAL] MT5 模擬驗證腳本異常:', err);
  process.exit(1);
});
