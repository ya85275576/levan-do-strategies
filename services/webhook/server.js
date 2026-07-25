/**
 * LE VAN DO® Webhook 接收服务
 *
 * TradingView 策略警报 → HTTP POST → 信号解析 → 交易所挂单
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
 */
import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import { getConfig, validateConfig } from '../config/index.js';
import { validateWebhookRequest } from './validator.js';
import { getExchangeClient } from '../exchange/index.js';
import { getSignalAction } from '../signals/parser.js';

const app = express();
const config = getConfig();

// ---- 启动前配置检查 ----
const cfgCheck = validateConfig();
if (!cfgCheck.valid) {
  console.warn('⚠️ ======== 配置警告 ========');
  cfgCheck.errors.forEach(e => console.warn(`  ⚠️  ${e}`));
  console.warn('==============================');
}

// ---- 中间件 ----
app.use(cors());
app.use(express.json({ limit: '1mb' }));
app.use(express.urlencoded({ extended: true }));

// ---- 请求日志 ----
app.use((req, _res, next) => {
  const ts = new Date().toISOString();
  console.log(`[${ts}] ${req.method} ${req.path} — ${req.ip}`);
  next();
});

// ---- 健康检查 ----
app.get('/health', async (_req, res) => {
  const client = getExchangeClient();
  let serverTime = null;
  let accountStatus = '未检查';

  try {
    const timeRes = await client.getServerTime();
    serverTime = timeRes;
    const balance = await client.getUSDTBalance();
    accountStatus = `USDT 余额: $${parseFloat(balance).toFixed(2)}`;
  } catch (err) {
    accountStatus = `连接失败: ${err.message}`;
  }

  res.json({
    status: 'ok',
    exchange: config.exchange,
    network: config.network.toUpperCase(),
    orderType: config.defaultOrderType,
    serverTime,
    accountStatus,
    allowedSymbols: Object.keys(config.symbols),
  });
});

// ---- Webhook 主端点 ----
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

// ---- 启动 ----
app.listen(config.port, config.host, () => {
  console.log('╔══════════════════════════════════════════════╗');
  console.log('║   LE VAN DO® 交易信号执行服务                ║');
  console.log('╠══════════════════════════════════════════════╣');
  console.log(`║  交易所:     ${config.exchange.padEnd(28)}║`);
  console.log(`║  网络:       ${(config.isTestnet ? '🟡 测试网' : '🔴 实盘').padEnd(28)}║`);
  console.log(`║  端口:       ${String(config.port).padEnd(28)}║`);
  console.log(`║  订单类型:   ${config.defaultOrderType.padEnd(28)}║`);
  console.log(`║  Webhook:    POST /webhook                    ║`);
  console.log(`║  健康检查:   GET  /health                      ║`);
  console.log('╚══════════════════════════════════════════════╝');

  if (!config.webhookSecret) {
    console.warn('⚠️  WEBHOOK_SECRET 未设置 — 任何来源均可触发交易！');
    console.warn('   请在 .env 文件中设置 WEBHOOK_SECRET');
  }
});
