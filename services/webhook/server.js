/**
 * LE VAN DO® Webhook 接收服务 + 网页仪表板
 *
 * TradingView 策略警报 → HTTP POST → 信号解析 → 交易所挂单
 * 同时提供网页仪表板（GET / 或 GET /dashboard）查看机器人运行状态
 *
 * 启动方式：
 *   npm start                  # 默认：测试网
 *   npm run start:testnet      # 测试网
 *   npm run start:live         # 实盘
 *
 * TradingView 警报配置：
 *   - Webhook URL: http://<你的服务器>:3000/webhook
 *   - 消息格式（推荐 JSON）:
 *     {
 *       "signal": "longE",
 *       "symbol": "BTCUSDT",
 *       "price": "50000",
 *       "tp1": "51000",
 *       "sl": "49000"
 *     }
 *   - Header: x-webhook-secret = <你的 WEBHOOK_SECRET>
 *
 * 仪表板：
 *   - 网页：GET / 或 GET /dashboard
 *   - 数据：GET /api/status（JSON）
 */
import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import { exec } from 'node:child_process';
import { createRequire } from 'node:module';
import os from 'node:os';
import { readFileSync, statfsSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getConfig, validateConfig } from '../config/index.js';
import { validateWebhookRequest } from './validator.js';
import { getExchangeClient } from '../exchange/index.js';
import { getSignalAction } from '../signals/parser.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const app = express();
const config = getConfig();

// ======== 從 Bot ecosystem.config.cjs 讀取實際配置 ========
const require_bot = createRequire(import.meta.url);
let botEnv = {};
try {
  const ecoConfig = require_bot('../bot/ecosystem.config.cjs');
  if (ecoConfig.apps && ecoConfig.apps.length > 0) {
    botEnv = ecoConfig.apps[0].env || {};
  }
} catch (err) {
  console.warn(`[配置] ⚠️ 無法讀取 Bot ecosystem.config.cjs: ${err.message}`);
}

const BOT_INITIAL_CAPITAL = parseFloat(botEnv.INITIAL_CAPITAL || '10000');
const BOT_DEFAULT_LEVERAGE = parseInt(botEnv.DEFAULT_LEVERAGE || '100', 10);
const BOT_TRADE_QTY_PCT = parseInt(botEnv.TRADE_QTY_PCT || '100', 10);
const BOT_TRADING_SYMBOLS = (botEnv.TRADING_SYMBOLS || '').split(',').filter(Boolean);

// 用 Bot 實際配置覆蓋 Webhook 的預設值
config.defaultLeverage = BOT_DEFAULT_LEVERAGE;

const BOT_ACCOUNT_STATUS = `USDT 餘額: $${BOT_INITIAL_CAPITAL.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// ======== 仪表板数据跟踪 ========

/** @type {{ time: string, type: string, symbol: string, price: string|null }[]} 最近信号记录 */
const recentSignals = [];
const MAX_SIGNALS = 200;
const signalCounts = { longE: 0, shortE: 0, longX: 0, shortX: 0 };
let signalsTotal = 0;

/** Bot 内部产生的信号总数（从状态文件读取） */
let botSignalsTotal = 0;
let botTotalSignals = 0;

/** 最近符号价格缓存 */
const symbolPrices = {};

/** 服务器启动时间 */
const serverStartTime = Date.now();

/** 50 个标准交易对列表（与 bot/config.py 一致） */
const ALL_SYMBOLS = [
  'BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'XRP-USDT', 'DOGE-USDT',
  'ADA-USDT', 'AVAX-USDT', 'DOT-USDT', 'LINK-USDT', 'MATIC-USDT',
  'UNI-USDT', 'SHIB-USDT', 'LTC-USDT', 'BCH-USDT', 'ATOM-USDT',
  'ETC-USDT', 'XLM-USDT', 'TRX-USDT', 'FIL-USDT', 'APT-USDT',
  'ARB-USDT', 'OP-USDT', 'SUI-USDT', 'PEPE-USDT', 'INJ-USDT',
  'TIA-USDT', 'SEI-USDT', 'RUNE-USDT', 'FET-USDT', 'GRT-USDT',
  'NEAR-USDT', 'ICP-USDT', 'RENDER-USDT', 'IMX-USDT', 'MKR-USDT',
  'AAVE-USDT', 'CRV-USDT', 'SNX-USDT', 'COMP-USDT', 'EOS-USDT',
  'ALGO-USDT', 'FLOW-USDT', 'SAND-USDT', 'MANA-USDT', 'AXS-USDT',
  'THETA-USDT', 'FTM-USDT', 'CVX-USDT', '1INCH-USDT', 'STX-USDT',
];

/** 每个交易对的状态（ok / no_data / error） */
const symbolStatus = {};
for (const sym of ALL_SYMBOLS) {
  symbolStatus[sym] = 'no_data';
}

// ======== 辅助函数 ========

/**
 * 安全执行 shell 命令并返回 stdout
 */
function execAsync(cmd, timeout = 5000) {
  return new Promise((resolve) => {
    exec(cmd, { timeout, shell: '/bin/bash' }, (err, stdout, stderr) => {
      resolve({ stdout: stdout || '', stderr: stderr || '', err });
    });
  });
}

/**
 * 获取 PM2 进程列表（JSON 格式）
 */
async function getPm2Status() {
  try {
    const { stdout } = await execAsync('pm2 jlist --no-color 2>/dev/null');
    if (!stdout) return [];
    return JSON.parse(stdout);
  } catch {
    return [];
  }
}

/**
 * 获取系统资源信息
 */
function getSystemInfo() {
  const totalMem = os.totalmem();
  const freeMem = os.freemem();
  const usedMem = totalMem - freeMem;
  const loadAvg = os.loadavg();

  let diskInfo = { total: 0, used: 0, free: 0, usagePercent: 0 };
  try {
    const stats = statfsSync('/');
    const total = stats.blocks * stats.bsize;
    const free = stats.bfree * stats.bsize;
    const used = total - free;
    diskInfo = {
      total,
      used,
      free,
      usagePercent: total > 0 ? Math.round((used / total) * 100) : 0,
    };
  } catch { /* ignore */ }

  return {
    memory: {
      total: totalMem,
      used: usedMem,
      free: freeMem,
      usagePercent: totalMem > 0 ? Math.round((usedMem / totalMem) * 100) : 0,
    },
    disk: diskInfo,
    cpu: {
      loadAvg: loadAvg.map(v => Math.round(v * 100) / 100),
      cores: os.cpus().length,
    },
    hostname: os.hostname(),
    platform: os.platform(),
    uptime: os.uptime(),
  };
}

/**
 * 格式化字节为可读字符串
 */
function formatBytes(bytes) {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return (bytes / Math.pow(1024, i)).toFixed(1) + ' ' + units[i];
}

/**
 * 格式化秒数为可读 uptime
 */
function formatUptime(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const parts = [];
  if (d > 0) parts.push(`${d}d`);
  if (h > 0) parts.push(`${h}h`);
  if (m > 0) parts.push(`${m}m`);
  parts.push(`${s}s`);
  return parts.join(' ');
}

/**
 * 获取服务器运行时长
 */
function getServerUptime() {
  return Math.floor((Date.now() - serverStartTime) / 1000);
}

// ======== 读取 Bot 状态文件 ========

const BOT_STATUS_FILE = '/tmp/le-van-do-bot-status.json';

/**
 * 从状态文件读取 Bot 内部信号统计数据
 */
function readBotStatus() {
  try {
    if (!existsSync(BOT_STATUS_FILE)) {
      return null;
    }
    const raw = readFileSync(BOT_STATUS_FILE, 'utf-8');
    return JSON.parse(raw);
  } catch (err) {
    console.warn(`[仪表板] ⚠️ 读取 Bot 状态文件失败: ${err.message}`);
    return null;
  }
}

// ======== 启动前配置检查 ========

const cfgCheck = validateConfig();
if (!cfgCheck.valid) {
  console.warn('⚠️ ======== 配置警告 ========');
  cfgCheck.errors.forEach(e => console.warn(`  ⚠️  ${e}`));
  console.warn('==============================');
}

// ======== 中间件 ========

app.use(cors());
app.use(express.json({ limit: '1mb' }));
app.use(express.urlencoded({ extended: true }));

// 请求日志
app.use((req, _res, next) => {
  const ts = new Date().toISOString();
  console.log(`[${ts}] ${req.method} ${req.path} — ${req.ip}`);
  next();
});

// ======== 健康检查 ========

app.get('/health', async (_req, res) => {
  const client = getExchangeClient();
  let serverTime = null;
  let accountStatus = BOT_ACCOUNT_STATUS;

  try {
    const timeRes = await client.getServerTime();
    serverTime = timeRes;
    await client.getUSDTBalance();
  } catch (err) {
    accountStatus = `连接失败: ${err.message}`;
  }

  res.json({
    status: 'ok',
    exchange: config.exchange,
    exchangeType: config.exchangeType,
    network: config.network.toUpperCase(),
    orderType: config.defaultOrderType,
    dryRun: config.dryRun,
    serverTime,
    accountStatus,
    allowedSymbols: Object.keys(config.symbols),
  });
});

// ======== API: 完整状态数据（仪表板使用） ========

app.get('/api/status', async (_req, res) => {
  const client = getExchangeClient();
  const systemInfo = getSystemInfo();
  const pm2Processes = await getPm2Status();

  // 交易所状态
  let exchangeStatus = 'disconnected';
  let exchangeAccount = BOT_ACCOUNT_STATUS;
  let serverTime = null;
  const positions = client.getSimulatedPositions ? client.getSimulatedPositions() : {};
  try {
    await client.getUSDTBalance();
    exchangeStatus = 'connected';
  } catch (err) {
    exchangeAccount = `连接失败: ${err.message}`;
  }

  // PM2 进程摘要
  const processes = pm2Processes.map(p => ({
    pm_id: p.pm_id,
    name: p.name,
    status: p.pm2_env?.status || 'unknown',
    pid: p.pid,
    uptime: p.pm2_env?.pm_uptime ? formatUptime(Math.floor((Date.now() - p.pm2_env.pm_uptime) / 1000)) : 'N/A',
    restartCount: p.pm2_env?.restart_time || 0,
    cpu: p.monit?.cpu ?? 0,
    memory: p.monit?.memory ?? 0,
  }));

  // 读取 Bot 状态
  const botStatus = readBotStatus();
  if (botStatus) {
    botTotalSignals = botStatus.total_signals || 0;
    // 用 Bot 的讯号更新符号状态
    if (botStatus.symbols) {
      for (const [sym, info] of Object.entries(botStatus.symbols)) {
        if (info.total_signals > 0 && (!symbolStatus[sym] || symbolStatus[sym] === 'no_data')) {
          symbolStatus[sym] = 'ok';
        }
      }
    }

    // ======== 从 Bot 状态文件同步信号数据 ========
    // 使用 Bot 的 total_signals 覆盖仪表板的信号总数
    signalsTotal = botTotalSignals;

    // 从每个交易对的 last_signal 推算信号分类计数
    const botCounts = { longE: 0, shortE: 0, longX: 0, shortX: 0 };
    if (botStatus.symbols) {
      for (const [sym, info] of Object.entries(botStatus.symbols)) {
        const sig = info.last_signal;
        if (!sig) continue;
        if (sig === 'longE') botCounts.longE++;
        else if (sig === 'shortE') botCounts.shortE++;
        else if (sig.startsWith('long')) botCounts.longX++;
        else if (sig.startsWith('short')) botCounts.shortX++;
      }
    }
    // 合并：以 Bot 计数为准（覆盖 Webhook 本地计数）
    signalCounts.longE = botCounts.longE;
    signalCounts.shortE = botCounts.shortE;
    signalCounts.longX = botCounts.longX;
    signalCounts.shortX = botCounts.shortX;

    // 从 signal_queue 更新 recent 信号列表
    if (botStatus.signal_queue && botStatus.signal_queue.length > 0) {
      for (const entry of botStatus.signal_queue) {
        const sym = entry.symbol || '';
        const sig = entry.signal || '';
        // 忽略已经存在的相同信号（避免重复）
        const exists = recentSignals.some(
          r => r.time === entry.time && r.symbol === sym && r.type === sig
        );
        if (!exists) {
          recentSignals.push({
            time: entry.time,
            type: sig,
            symbol: sym,
            price: entry.price != null ? String(entry.price) : null,
          });
        }
      }
      // 保留最新的 MAX_SIGNALS 条
      if (recentSignals.length > MAX_SIGNALS) {
        recentSignals.splice(0, recentSignals.length - MAX_SIGNALS);
      }
    }
  }

  // 持仓列表：優先使用 Bot 狀態檔中的持倉資料
  let positionsList = [];
  if (botStatus && botStatus.positions && botStatus.positions.length > 0) {
    positionsList = botStatus.positions.map(p => ({
      symbol: p.symbol,
      side: p.side,
      size: p.size,
      price: p.entry_price || null,
    }));
  } else {
    // 降級：使用 Webhook 本地的模擬持倉
    for (const [symbol, size] of Object.entries(positions)) {
      if (size === 0) continue;
      const price = symbolPrices[symbol];
      positionsList.push({
        symbol,
        side: size > 0 ? 'long' : 'short',
        size: Math.abs(size),
        price: price || null,
      });
    }
  }

  // 最近信号（最近的 50 条）
  const recent50 = recentSignals.slice(-50).reverse();

  res.json({
    server: {
      version: '1.0.0',
      uptime: formatUptime(getServerUptime()),
      startTime: new Date(serverStartTime).toISOString(),
      time: new Date().toISOString(),
    },
    exchange: {
      name: config.exchange,
      exchangeType: config.exchangeType,
      network: config.network.toUpperCase(),
      isTestnet: config.isTestnet,
      dryRun: config.dryRun,
      status: exchangeStatus,
      accountStatus: exchangeAccount,
      orderType: config.defaultOrderType,
      leverage: config.defaultLeverage,
      positionMode: config.positionMode,
    },
    processes,
    symbols: ALL_SYMBOLS.map(sym => ({
      symbol: sym,
      status: symbolStatus[sym] || 'no_data',
      price: symbolPrices[sym] || null,
      hasConfig: BOT_TRADING_SYMBOLS.length > 0 ? BOT_TRADING_SYMBOLS.includes(sym) : (!!config.symbols[sym.replace('-', '')] || !!config.symbols[sym]),
    })),
    signals: {
      total: signalsTotal,
      botTotal: botTotalSignals,
      counts: { ...signalCounts },
      recent: recent50,
    },
    positions: positionsList,
    system: systemInfo,
    config: {
      baseTimeframe: '15m',
      tfMult: 18,
    },
  });
});

// ======== Webhook 主端点 ========

app.post('/webhook', async (req, res) => {
  console.log('[Webhook] 📩 收到新警报');

  // 1. 验证请求
  const validation = validateWebhookRequest(req.body, req.headers);

  if (!validation.valid) {
    console.warn(`[Webhook] ✋ 请求被拒绝: ${validation.error}`);
    return res.status(validation.statusCode || 400).json({
      success: false,
      error: validation.error,
    });
  }

  const signal = validation.data;
  console.log(`[Webhook] ✅ 信号验证通过: ${signal.type} ${signal.symbol}`);

  // 记录信号到仪表板
  signalsTotal++;
  const sym = signal.symbol.includes('-') ? signal.symbol : signal.symbol.replace(/(USDT|USD|USDC|BTC|ETH)$/, '-$1');
  if (signalCounts[signal.type] !== undefined) {
    signalCounts[signal.type]++;
  }
  recentSignals.push({
    time: new Date().toISOString(),
    type: signal.type,
    symbol: sym,
    price: signal.price ? String(signal.price) : null,
  });
  if (recentSignals.length > MAX_SIGNALS) recentSignals.shift();

  // 更新符号状态和价格
  if (symbolPrices[sym] === undefined) {
    symbolStatus[sym] = 'ok';
  }
  if (signal.price) {
    symbolPrices[sym] = parseFloat(signal.price);
  }

  // 2. 执行交易
  try {
    const exchange = getExchangeClient();
    const action = getSignalAction(signal.type);
    let result;

    if (action === 'enter') {
      // ---- 开仓 ----
      console.log(`[Webhook] 🚀 执行开仓: ${signal.side} ${signal.symbol}`);

      // 设置杠杆
      await exchange.setLeverage(signal.symbol, config.defaultLeverage, config.positionMode);

      // 下单
      result = await exchange.placeOrder({
        symbol: signal.symbol,
        side: signal.side,
        qty: signal.qty || '0.001', // 默认最小交易量（BTC）
        orderType: config.defaultOrderType,
      });
    } else if (action === 'exit') {
      // ---- 平仓 ----
      console.log(`[Webhook] 🚪 执行平仓: ${signal.symbol}`);
      result = await exchange.closePosition(signal.symbol);
    } else {
      return res.status(400).json({
        success: false,
        error: `未知的信号动作: ${signal.type}`,
      });
    }

    // 3. 返回结果
    console.log(`[Webhook] ✅ 交易完成:`, JSON.stringify(result));
    res.json({
      success: true,
      signal: signal.type,
      symbol: signal.symbol,
      network: config.network,
      orderResult: result,
      timestamp: new Date().toISOString(),
    });
  } catch (err) {
    console.error(`[Webhook] ❌ 交易执行失败:`, err.message);
    res.status(502).json({
      success: false,
      error: `交易执行失败: ${err.message}`,
      signal: signal.type,
      symbol: signal.symbol,
    });
  }
});

// ======== 仪表板页面 ========

const DASHBOARD_HTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LE VAN DO® 交易仪表板</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    min-height: 100vh;
    padding: 0;
    margin: 0;
  }
  .container { max-width: 1400px; margin: 0 auto; padding: 20px; }

  /* Header */
  .header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 16px 24px; margin-bottom: 24px;
    background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
    border: 1px solid #30363d; border-radius: 12px;
    flex-wrap: wrap; gap: 12px;
  }
  .header-left { display: flex; align-items: center; gap: 16px; }
  .logo { font-size: 22px; font-weight: 800; letter-spacing: 0.5px; color: #58a6ff; }
  .logo span { color: #f0883e; }
  .header-status { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
  .status-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: 600;
  }
  .status-pill.online { background: #1b3d2b; color: #3fb950; border: 1px solid #2ea043; }
  .status-pill.offline { background: #3d1b1b; color: #f85149; border: 1px solid #da3633; }
  .status-pill.dry-run { background: #1b2d3d; color: #58a6ff; border: 1px solid #1f6feb; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .status-dot.green { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
  .status-dot.red { background: #f85149; box-shadow: 0 0 6px #f85149; }
  .status-dot.blue { background: #58a6ff; box-shadow: 0 0 6px #58a6ff; }

  .last-update { font-size: 12px; color: #8b949e; margin-left: auto; }

  /* Cards */
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 20px; margin-bottom: 20px;
  }
  .card-title {
    font-size: 14px; font-weight: 600; color: #8b949e; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 16px; display: flex; align-items: center; gap: 8px;
  }

  /* Grid layouts */
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; }
  .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; }
  @media (max-width: 900px) {
    .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; }
  }

  /* Stats */
  .stat-value { font-size: 28px; font-weight: 700; color: #f0f6fc; }
  .stat-label { font-size: 12px; color: #8b949e; margin-top: 4px; }
  .stat-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #21262d; }
  .stat-row:last-child { border-bottom: none; }
  .stat-row .label { color: #8b949e; font-size: 13px; }
  .stat-row .value { font-weight: 600; font-size: 13px; }

  /* Symbol grid */
  .symbol-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
    gap: 6px;
  }
  .symbol-cell {
    padding: 6px 8px; border-radius: 6px; font-size: 11px; font-weight: 500;
    text-align: center; border: 1px solid #21262d; transition: all 0.2s;
  }
  .symbol-cell.ok { background: #0d2818; border-color: #2ea043; color: #3fb950; }
  .symbol-cell.no_data { background: #1c1c1c; border-color: #30363d; color: #484f58; }
  .symbol-cell.error { background: #2d1215; border-color: #da3633; color: #f85149; }
  .symbol-cell .sym-name { display: block; }
  .symbol-cell .sym-price { font-size: 10px; opacity: 0.7; display: block; margin-top: 2px; }

  /* Signal table */
  .signal-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .signal-table th {
    text-align: left; padding: 10px 12px; border-bottom: 2px solid #30363d;
    color: #8b949e; font-weight: 600; font-size: 12px; text-transform: uppercase;
  }
  .signal-table td { padding: 8px 12px; border-bottom: 1px solid #21262d; }
  .signal-table tr:hover td { background: #1c2128; }
  .signal-type {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 700; font-family: 'Consolas', monospace;
  }
  .sig-longE { background: #0d2818; color: #3fb950; }
  .sig-shortE { background: #2d1215; color: #f85149; }
  .sig-longX { background: #1b2d3d; color: #58a6ff; }
  .sig-shortX { background: #2d1b0e; color: #d29922; }

  /* Progress bars */
  .progress-bar { height: 8px; background: #21262d; border-radius: 4px; overflow: hidden; }
  .progress-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }
  .progress-fill.green { background: linear-gradient(90deg, #2ea043, #3fb950); }
  .progress-fill.blue { background: linear-gradient(90deg, #1f6feb, #58a6ff); }
  .progress-fill.red { background: linear-gradient(90deg, #da3633, #f85149); }

  /* Process cards */
  .process-card {
    padding: 14px; border-radius: 8px; border: 1px solid #21262d;
    display: flex; justify-content: space-between; align-items: center;
  }
  .process-name { font-weight: 600; font-size: 14px; }
  .process-meta { font-size: 11px; color: #8b949e; margin-top: 4px; }
  .process-stats { text-align: right; }
  .process-stats .cpu-mem { font-size: 12px; color: #8b949e; }

  /* Pair columns in signal counts */
  .signal-counts { display: flex; gap: 12px; flex-wrap: wrap; }
  .signal-count-item {
    padding: 12px 16px; border-radius: 8px; border: 1px solid #21262d;
    text-align: center; min-width: 90px;
  }
  .signal-count-item .sc-value { font-size: 24px; font-weight: 700; }
  .signal-count-item .sc-label { font-size: 11px; color: #8b949e; margin-top: 2px; }

  /* Empty state */
  .empty-state { text-align: center; padding: 40px 20px; color: #484f58; }
  .empty-state .icon { font-size: 36px; margin-bottom: 8px; }
  .empty-state .text { font-size: 14px; }

  /* Loading overlay */
  .loading { text-align: center; padding: 60px; color: #8b949e; }
  .loading .spinner {
    width: 36px; height: 36px; border: 3px solid #21262d;
    border-top-color: #58a6ff; border-radius: 50%;
    animation: spin 0.8s linear infinite; margin: 0 auto 16px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Footer */
  .footer {
    text-align: center; padding: 20px; color: #484f58; font-size: 12px;
    border-top: 1px solid #21262d; margin-top: 40px;
  }
</style>
</head>
<body>
<div class="container" id="app">
  <div class="loading" id="loading">
    <div class="spinner"></div>
    <div>加载仪表板数据...</div>
  </div>
</div>

<script>
const FETCH_INTERVAL = 30000;

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtBytes(b) {
  if (b === 0) return '0 B';
  const u = ['B','KB','MB','GB','TB'];
  const i = Math.floor(Math.log(b) / Math.log(1024));
  return (b / Math.pow(1024, i)).toFixed(1) + ' ' + u[i];
}

function fmtPct(v) { return v + '%'; }

function esc(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function statusClass(s) {
  if (s === 'online' || s === 'connected' || s === 'ok') return 'online';
  return 'offline';
}

function renderStatus(status) {
  const cls = statusClass(status);
  return '<span class="status-pill ' + cls + '"><span class="status-dot ' +
    (cls === 'online' ? 'green' : 'red') + '"></span>' + esc(status) + '</span>';
}

async function fetchData() {
  try {
    const resp = await fetch('/api/status');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return await resp.json();
  } catch (e) {
    console.error('Fetch error:', e);
    return null;
  }
}

function renderHeader(d) {
  const ex = d.exchange;
  const isOnline = d.processes.some(p => p.status === 'online');
  const isDryRun = ex.dryRun;

  let statusText = isOnline ? '🟢 运行中' : '🔴 离线';
  if (isDryRun) statusText += ' · 模拟模式';

  let exStatus = ex.status === 'connected' ?
    '<span class="status-pill online"><span class="status-dot green"></span>' + esc(ex.network) + '</span>' :
    '<span class="status-pill offline"><span class="status-dot red"></span>断开</span>';

  let statusClass = isOnline ? 'online' : 'offline';
  if (isDryRun) statusClass += ' dry-run';

  return '<div class="header">' +
    '<div class="header-left">' +
      '<div class="logo">LE VAN <span>DO</span>®</div>' +
      '<span class="status-pill ' + statusClass + '">' +
        '<span class="status-dot ' + (isOnline ? 'green' : 'red') + '"></span>' +
        esc(statusText) +
      '</span>' +
      exStatus +
    '</div>' +
    '<div class="header-status">' +
      '<span style="font-size:12px;color:#8b949e">⬆ ' + esc(d.server.uptime) + '</span>' +
      '<span class="last-update">🔄 ' + fmtTime(d.server.time) + '</span>' +
    '</div>' +
  '</div>';
}

function renderSystem(d) {
  const sys = d.system;
  const mem = sys.memory;
  const disk = sys.disk;
  const cpu = sys.cpu;

  const memPct = mem.usagePercent;
  const diskPct = disk.usagePercent;

  let memColor = 'green';
  if (memPct > 80) memColor = 'red';
  else if (memPct > 60) memColor = 'blue';

  let diskColor = 'green';
  if (diskPct > 80) diskColor = 'red';
  else if (diskPct > 60) diskColor = 'blue';

  return '<div class="card">' +
    '<div class="card-title">🖥️ 系统资源</div>' +
    '<div class="grid-3">' +
      '<div>' +
        '<div class="stat-value">' + memPct + '%</div>' +
        '<div class="stat-label">内存</div>' +
        '<div style="margin-top:8px;font-size:12px;color:#8b949e">' +
          fmtBytes(mem.used) + ' / ' + fmtBytes(mem.total) +
        '</div>' +
        '<div class="progress-bar" style="margin-top:6px"><div class="progress-fill ' + memColor + '" style="width:' + memPct + '%"></div></div>' +
      '</div>' +
      '<div>' +
        '<div class="stat-value">' + diskPct + '%</div>' +
        '<div class="stat-label">磁盘</div>' +
        '<div style="margin-top:8px;font-size:12px;color:#8b949e">' +
          fmtBytes(disk.used) + ' / ' + fmtBytes(disk.total) +
        '</div>' +
        '<div class="progress-bar" style="margin-top:6px"><div class="progress-fill ' + diskColor + '" style="width:' + diskPct + '%"></div></div>' +
      '</div>' +
      '<div>' +
        '<div class="stat-value">' + cpu.cores + '</div>' +
        '<div class="stat-label">CPU 核心 · 负载</div>' +
        '<div style="margin-top:8px;font-size:12px;color:#8b949e">' +
          '1m: ' + cpu.loadAvg[0] + ' · 5m: ' + cpu.loadAvg[1] + ' · 15m: ' + cpu.loadAvg[2] +
        '</div>' +
      '</div>' +
    '</div>' +
  '</div>';
}

function renderProcesses(d) {
  const procs = d.processes;
  if (procs.length === 0) {
    return '<div class="card"><div class="card-title">⚙️ 进程状态</div><div class="empty-state"><div class="text">无 PM2 进程数据</div></div></div>';
  }

  let html = '<div class="card"><div class="card-title">⚙️ 进程状态</div><div class="grid-2">';
  for (const p of procs) {
    const isOnline = p.status === 'online';
    html += '<div class="process-card">' +
      '<div>' +
        '<div class="process-name">' +
          '<span class="status-dot ' + (isOnline ? 'green' : 'red') + '" style="margin-right:6px;vertical-align:middle"></span>' +
          esc(p.name) +
        '</div>' +
        '<div class="process-meta">' +
          'PID ' + p.pid + ' · 运行 ' + p.uptime +
          (p.restartCount > 0 ? ' · 重启 ' + p.restartCount + ' 次' : '') +
        '</div>' +
      '</div>' +
      '<div class="process-stats">' +
        '<div class="cpu-mem">CPU: ' + p.cpu + '%</div>' +
        '<div class="cpu-mem">MEM: ' + fmtBytes(p.memory) + '</div>' +
      '</div>' +
    '</div>';
  }
  html += '</div></div>';
  return html;
}

function renderSymbols(d) {
  const syms = d.symbols;
  if (!syms || syms.length === 0) {
    return '<div class="card"><div class="card-title">📊 交易对行情</div><div class="empty-state"><div class="text">无交易对数据</div></div></div>';
  }

  const ok = syms.filter(s => s.status === 'ok').length;
  const noData = syms.filter(s => s.status === 'no_data').length;
  const err = syms.filter(s => s.status === 'error').length;
  const total = syms.length;

  let html = '<div class="card"><div class="card-title">📊 交易对行情 <span style="font-weight:400;color:#484f58;font-size:12px">' +
    ok + '/' + total + ' 正常 · ' + noData + ' 等待 · ' + err + ' 异常</span></div>' +
    '<div class="symbol-grid">';

  for (const s of syms) {
    let cls = s.status;
    let label = s.status === 'ok' ? '✓' : (s.status === 'error' ? '✗' : '—');
    html += '<div class="symbol-cell ' + esc(cls) + '">' +
      '<span class="sym-name">' + esc(s.symbol) + '</span>' +
      (s.price ? '<span class="sym-price">$' + parseFloat(s.price).toFixed(s.price < 1 ? 6 : 2) + '</span>' :
       '<span class="sym-price">' + label + '</span>') +
    '</div>';
  }

  html += '</div></div>';
  return html;
}

function renderSignals(d) {
  const sig = d.signals;
  const counts = sig.counts || { longE: 0, shortE: 0, longX: 0, shortX: 0 };
  const recent = sig.recent || [];

  const botTotal = sig.botTotal || 0;
  const totalLabel = botTotal > 0
    ? ('累计 ' + sig.total + ' 条（Webhook） · Bot 内部 ' + botTotal + ' 条')
    : ('累计 ' + sig.total + ' 条');

  let html = '<div class="card"><div class="card-title">📡 策略信号 <span style="font-weight:400;color:#484f58;font-size:12px">' + totalLabel + '</span></div>';

  // Signal counts
  html += '<div class="signal-counts" style="margin-bottom:16px">';
  const countConfigs = [
    { key: 'longE', label: '多头开仓', color: 'green' },
    { key: 'shortE', label: '空头开仓', color: 'red' },
    { key: 'longX', label: '多头平仓', color: 'blue' },
    { key: 'shortX', label: '空头平仓', color: 'orange' },
  ];
  const colorMap = { green: '#3fb950', red: '#f85149', blue: '#58a6ff', orange: '#d29922' };
  for (const cc of countConfigs) {
    const v = counts[cc.key] || 0;
    html += '<div class="signal-count-item" style="border-left: 3px solid ' + colorMap[cc.color] + '">' +
      '<div class="sc-value" style="color:' + colorMap[cc.color] + '">' + v + '</div>' +
      '<div class="sc-label">' + esc(cc.label) + '</div>' +
    '</div>';
  }
  html += '</div>';

  // Recent signals table
  if (recent.length === 0) {
    html += '<div class="empty-state"><div class="icon">📭</div><div class="text">暂无信号记录</div></div>';
  } else {
    html += '<table class="signal-table"><thead><tr><th>时间</th><th>信号</th><th>交易对</th><th>价格</th></tr></thead><tbody>';
    for (const r of recent) {
      const sigClass = 'sig-' + r.type;
      html += '<tr>' +
        '<td style="color:#8b949e;font-size:12px">' + fmtTime(r.time) + '</td>' +
        '<td><span class="signal-type ' + sigClass + '">' + esc(r.type) + '</span></td>' +
        '<td>' + esc(r.symbol) + '</td>' +
        '<td>' + (r.price ? '$' + esc(r.price) : '—') + '</td>' +
      '</tr>';
    }
    html += '</tbody></table>';
  }

  html += '</div>';
  return html;
}

function renderPositions(d) {
  const positions = d.positions || [];
  let html = '<div class="card"><div class="card-title">💼 模拟持仓 <span style="font-weight:400;color:#484f58;font-size:12px">' + positions.length + ' 个</span></div>';

  if (positions.length === 0) {
    html += '<div class="empty-state"><div class="icon">📦</div><div class="text">暂无持仓</div></div>';
  } else {
    html += '<table class="signal-table"><thead><tr><th>交易对</th><th>方向</th><th>数量</th><th>入场价格</th></tr></thead><tbody>';
    for (const p of positions) {
      const sideColor = p.side === 'long' ? '#3fb950' : '#f85149';
      const sideLabel = p.side === 'long' ? '📈 多头' : '📉 空头';
      html += '<tr>' +
        '<td><strong>' + esc(p.symbol) + '</strong></td>' +
        '<td><span style="color:' + sideColor + ';font-weight:600">' + sideLabel + '</span></td>' +
        '<td>' + p.size + '</td>' +
        '<td>' + (p.price ? '$' + p.price.toFixed(2) : '待更新') + '</td>' +
      '</tr>';
    }
    html += '</tbody></table>';
  }

  html += '</div>';
  return html;
}

function renderExchange(d) {
  const ex = d.exchange;
  return '<div class="card"><div class="card-title">🔗 交易所状态</div>' +
    '<div class="grid-2">' +
      '<div>' +
        '<div class="stat-row"><span class="label">交易所</span><span class="value">' + esc(ex.name) + ' · ' + esc(ex.exchangeType) + '</span></div>' +
        '<div class="stat-row"><span class="label">网络</span><span class="value">' + esc(ex.network) + (ex.isTestnet ? ' 🟡 测试网' : ' 🔴 实盘') + '</span></div>' +
        '<div class="stat-row"><span class="label">状态</span><span class="value">' + renderStatus(ex.status) + '</span></div>' +
        '<div class="stat-row"><span class="label">账户</span><span class="value" style="font-size:12px;color:#8b949e">' + esc(ex.accountStatus) + '</span></div>' +
      '</div>' +
      '<div>' +
        '<div class="stat-row"><span class="label">模拟模式</span><span class="value">' + (ex.dryRun ? '🟢 启用' : '🔴 关闭') + '</span></div>' +
        '<div class="stat-row"><span class="label">订单类型</span><span class="value">' + esc(ex.orderType) + '</span></div>' +
        '<div class="stat-row"><span class="label">杠杆</span><span class="value">' + ex.leverage + 'x</span></div>' +
        '<div class="stat-row"><span class="label">仓位模式</span><span class="value">' + esc(ex.positionMode) + '</span></div>' +
      '</div>' +
    '</div></div>';
}

function renderFooter() {
  return '<div class="footer">LE VAN DO® 交易机器人 · 数据每 30 秒自动刷新 · ' +
    '<a href="/health" style="color:#58a6ff;text-decoration:none">/health</a></div>';
}

function renderDashboard(d) {
  return renderHeader(d) +
    renderSystem(d) +
    renderProcesses(d) +
    renderExchange(d) +
    renderSymbols(d) +
    renderSignals(d) +
    renderPositions(d) +
    renderFooter();
}

async function refresh() {
  const d = await fetchData();
  if (!d) {
    document.getElementById('app').innerHTML =
      '<div class="container"><div class="empty-state" style="padding:80px 20px">' +
      '<div class="icon">⚠️</div><div class="text">无法连接到服务器</div>' +
      '<p style="color:#8b949e;font-size:13px;margin-top:8px">请确认 /api/status 端点可访问</p></div></div>';
    return;
  }
  document.getElementById('loading').style.display = 'none';
  document.getElementById('app').innerHTML = renderDashboard(d);
}

document.addEventListener('DOMContentLoaded', () => {
  refresh();
  setInterval(refresh, FETCH_INTERVAL);
});
</script>
</body>
</html>`;

app.get('/', (_req, res) => {
  res.type('html').send(DASHBOARD_HTML);
});

app.get('/dashboard', (_req, res) => {
  res.type('html').send(DASHBOARD_HTML);
});

// ======== 启动 ========

app.listen(config.port, config.host, () => {
  console.log('╔══════════════════════════════════════════════╗');
  console.log('║   LE VAN DO® 交易信号执行服务                ║');
  console.log('╠══════════════════════════════════════════════╣');
  console.log(`║  交易所:     ${(config.exchangeType === 'mt5' ? 'MT5' : config.exchange).padEnd(28)}║`);
  console.log(`║  後端:       ${config.exchangeType.padEnd(28)}║`);
  console.log(`║  网络:       ${(config.isTestnet ? '🟡 测试网' : '🔴 实盘').padEnd(28)}║`);
  console.log(`║  模擬模式:   ${(config.dryRun ? '🟢 啟用' : '🔴 關閉').padEnd(28)}║`);
  console.log(`║  端口:       ${String(config.port).padEnd(28)}║`);
  console.log(`║  订单类型:   ${config.defaultOrderType.padEnd(28)}║`);
  console.log(`║  Webhook:    POST /webhook                    ║`);
  console.log(`║  健康检查:   GET  /health                      ║`);
  console.log(`║  仪表板:     GET  / 或 /dashboard              ║`);
  console.log('╚══════════════════════════════════════════════╝');

  if (!config.webhookSecret) {
    console.warn('⚠️  WEBHOOK_SECRET 未设置 — 任何来源均可触发交易！');
    console.warn('   请在 .env 文件中设置 WEBHOOK_SECRET');
  }
});
