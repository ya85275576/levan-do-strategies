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
import { readFileSync, writeFileSync, statfsSync, existsSync, readFile } from 'node:fs';
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

const BOT_CLOSED_TRADES_FILE = '/tmp/le-van-do-bot-closed.json';

// ======== HighTempTation 天气预测市场 DB ========
const WEATHER_DB_PATH = join(__dirname, '..', '..', 'tools', 'hightemptation_live', 'hightemptation.db');

/**
 * 查询 HighTempTation SQLite DB（JSON 输出）
 */
async function queryWeatherDB(sql) {
  try {
    if (!existsSync(WEATHER_DB_PATH)) return null;
    // 用临时文件传递 SQL，避免 shell 转义问题
    const tmpSqlFile = '/tmp/ht-query.sql';
    writeFileSync(tmpSqlFile, sql, 'utf-8');
    const { stdout, err } = await execAsync(`sqlite3 -json "${WEATHER_DB_PATH}" ".read ${tmpSqlFile}"`, 5000);
    if (err) return null;
    return JSON.parse(stdout);
  } catch (e) {
    console.warn(`[天气DB] 查询失败: ${e.message}`);
    return null;
  }
}

/**
 * 从 HighTempTation DB 读取天气预测市场数据
 */
async function getWeatherData() {
  try {
    if (!existsSync(WEATHER_DB_PATH)) return null;

    // 1. 开放持仓（status='open'）
    const openPositions = await queryWeatherDB(`
      SELECT t.token_id, t.city,
             t.bucket_lower, t.bucket_upper,
             t.side, t.entry_price AS entry_no,
             t.size, t.entry_time,
             COALESCE((
               SELECT mp.no_price FROM market_prices mp
               WHERE mp.city=t.city AND mp.bucket_lower=t.bucket_lower
                 AND mp.bucket_upper=t.bucket_upper
               ORDER BY mp.ts DESC LIMIT 1
             ), t.entry_price) AS curr_no,
             t.pnl
      FROM trades t
      WHERE t.status='open'
      ORDER BY t.entry_time DESC
    `);

    // 2. 最近平仓记录
    const closedTrades = await queryWeatherDB(`
      SELECT t.token_id, t.city,
             t.bucket_lower, t.bucket_upper,
             t.side, t.entry_price AS entry_no,
             COALESCE((
               SELECT mp.no_price FROM market_prices mp
               WHERE mp.city=t.city AND mp.bucket_lower=t.bucket_lower
                 AND mp.bucket_upper=t.bucket_upper
               ORDER BY mp.ts DESC LIMIT 1
             ), t.exit_price) AS curr_no,
             t.exit_price, t.size, t.pnl,
             t.entry_time, t.exit_time, t.exit_reason
      FROM trades t
      WHERE t.status='closed'
      ORDER BY t.exit_time DESC LIMIT 20
    `);

    // 3. 今日 PnL
    const todayStr = new Date().toISOString().slice(0, 10);
    const dailyResult = await queryWeatherDB(`
      SELECT COUNT(*) AS cnt,
             COALESCE(SUM(pnl),0) AS total_pnl,
             SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins,
             SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) AS losses
      FROM trades
      WHERE status='closed' AND date(exit_time)='${todayStr}'
    `);

    // 4. 总统计
    const totalStats = await queryWeatherDB(`
      SELECT COUNT(*) AS total_trades,
             COALESCE(SUM(CASE WHEN status='closed' THEN pnl ELSE 0 END),0) AS total_pnl,
             SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_count
      FROM trades
    `);

    // 5. 最近预报
    const latestForecasts = await queryWeatherDB(`
      SELECT city, date, mu, sigma, created_at
      FROM forecasts
      WHERE (city, date) IN (
        SELECT city, MAX(date) FROM forecasts GROUP BY city
      )
      ORDER BY city
    `);

    const openList = openPositions || [];
    const closedList = closedTrades || [];
    const daily = (dailyResult && dailyResult[0]) || {cnt:0,total_pnl:0,wins:0,losses:0};
    const total = (totalStats && totalStats[0]) || {total_trades:0,total_pnl:0,open_count:0};

    return {
      db_path: WEATHER_DB_PATH,
      db_exists: true,
      open_positions: openList,
      closed_trades: closedList,
      daily: {
        count: daily.cnt,
        total_pnl: daily.total_pnl,
        wins: daily.wins,
        losses: daily.losses,
        win_rate: daily.cnt > 0 ? ((daily.wins / daily.cnt) * 100).toFixed(1) : 0,
      },
      summary: {
        total_trades: total.total_trades,
        total_pnl: total.total_pnl,
        open_count: total.open_count,
        equity: (10000 + total.total_pnl),
      },
      forecasts: latestForecasts || [],
    };
  } catch (e) {
    console.warn(`[天气DB] 读取数据失败: ${e.message}`);
    return null;
  }
}

/**
 * 从持久化文件读取完整历史平仓记录
 */
function readClosedTrades() {
  try {
    if (!existsSync(BOT_CLOSED_TRADES_FILE)) {
      return [];
    }
    const raw = readFileSync(BOT_CLOSED_TRADES_FILE, 'utf-8');
    const data = JSON.parse(raw);
    return Array.isArray(data) ? data : [];
  } catch (err) {
    console.warn(`[仪表板] ⚠️ 读取历史平仓文件失败: ${err.message}`);
    return [];
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

  // 持倉列表：優先使用 Bot 狀態檔中的持倉資料（含盈虧）
  let positionsList = [];
  let botEquity = null;
  let botTotalPnl = null;
  let botInitialCapital = null;
  let botClosedTrades = [];
  let botClosedTradesSummary = null;
  if (botStatus && botStatus.positions && botStatus.positions.length > 0) {
    botEquity = botStatus.equity != null ? botStatus.equity : null;
    botTotalPnl = botStatus.total_pnl != null ? botStatus.total_pnl : null;
    botInitialCapital = botStatus.initial_capital != null ? botStatus.initial_capital : null;
    botClosedTrades = readClosedTrades();
    {
      const ctCount = botClosedTrades.length;
      const ctPnl = botClosedTrades.reduce((sum, t) => sum + (t.pnl || 0), 0);
      botClosedTradesSummary = botClosedTradesSummary || { count: ctCount, total_closed_pnl: ctPnl };
      botClosedTradesSummary.count = ctCount;
      botClosedTradesSummary.total_closed_pnl = ctPnl;
    }
    positionsList = botStatus.positions.map(p => ({
      symbol: p.symbol,
      side: p.side,
      size: p.size,
      entry_price: p.entry_price != null ? p.entry_price : null,
      current_price: p.current_price != null ? p.current_price : null,
      liquidation_price: p.liquidation_price != null ? p.liquidation_price : null,
      break_even_price: p.break_even_price != null ? p.break_even_price : null,
      pnl: p.pnl != null ? p.pnl : null,
      pnl_pct: p.pnl_pct != null ? p.pnl_pct : null,
      leverage: p.leverage != null ? p.leverage : null,
      margin: p.margin != null ? p.margin : null,
      maintenance_margin_rate: p.maintenance_margin_rate != null ? p.maintenance_margin_rate : null,
      tp_prices: p.tp_prices || [null, null, null],
      tp_status: p.tp_status || ["pending", "pending", "pending"],
      tp_pnl: p.tp_pnl || [0, 0, 0],
      tp_hit_level: p.tp_hit_level != null ? p.tp_hit_level : 0,
      tp_margins: p.tp_margins || [null, null, null],
      entry_time: p.entry_time || null,
      condition: p.condition != null ? p.condition : (p.side === 'long' ? 1.0 : -1.0),
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
        entry_price: price || null,
        current_price: price || null,
        pnl: null,
        pnl_pct: null,
        entry_time: null,
        condition: size > 0 ? 1.0 : -1.0,
      });
    }
  }

  // 最近信号（最近的 50 条）
  const recent50 = recentSignals.slice(-50).reverse();

  // ======== 读取天气预测市场数据 ========
  const weatherData = await getWeatherData();

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
    closed_trades: botClosedTrades,
    closed_trades_summary: botClosedTradesSummary || { count: 0, total_closed_pnl: 0 },
    equity: {
      initial_capital: botInitialCapital || BOT_INITIAL_CAPITAL,
      total_pnl: botTotalPnl,
      equity: botEquity,
    },
    system: systemInfo,
    capital_history: botStatus ? (botStatus.capital_history || []) : [],
    config: {
      baseTimeframe: '15m',
      tfMult: 18,
    },
    weather: weatherData || null,
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

const DASHBOARD_HTML_PATH = join(__dirname, '../../tools/hightemptation_live/dashboard.html');
let DASHBOARD_HTML_CACHE = '';

/**
 * 读取仪表板 HTML 文件（每次请求读取，支持热更新）
 */
function getDashboardHtml() {
  try {
    DASHBOARD_HTML_CACHE = readFileSync(DASHBOARD_HTML_PATH, 'utf-8');
  } catch (err) {
    console.warn(`[仪表板] ⚠️ 读取 dashboard.html 失败: ${err.message}`);
    if (!DASHBOARD_HTML_CACHE) {
      DASHBOARD_HTML_CACHE = '<html><body><h1>仪表板文件加载失败</h1></body></html>';
    }
  }
  return DASHBOARD_HTML_CACHE;
}

app.get('/', (_req, res) => {
  const html = getDashboardHtml();
  res.type('html').send(html);
});

app.get('/dashboard', (_req, res) => {
  const html = getDashboardHtml();
  res.type('html').send(html);
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
