/**
 * LE VAN DO® 配置管理器
 *
 * 统一管理交易所连接、交易参数、风控设置。
 * 配置来源优先级：环境变量 > exchange.json 默认值
 */
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// 加载默认配置
const defaultConfig = JSON.parse(
  readFileSync(join(__dirname, 'exchange.json'), 'utf-8')
);

/**
 * 获取当前运行环境
 * @returns {'testnet' | 'production'}
 */
function getNetwork() {
  const env = (process.env.EXCHANGE_NETWORK || 'testnet').toLowerCase();
  if (env !== 'production' && env !== 'live') return 'testnet';
  return 'production';
}

/**
 * 获取完整的运行时配置
 */
export function getConfig() {
  const network = getNetwork();
  const isTestnet = network === 'testnet';

  const config = {
    // 交易所
    exchange: defaultConfig.exchange,

    // 网络环境
    network,
    isTestnet,
    baseUrl: defaultConfig.network[network].baseUrl,

    // API 凭据 — OKX 三件套（来自环境变量 / Secrets）
    apiKey: process.env.OKX_API_KEY || '',
    apiSecret: process.env.OKX_API_SECRET || '',
    apiPassphrase: process.env.OKX_API_PASSPHRASE || '',

    // 模拟模式（不实际发送 API 请求）
    dryRun: (process.env.DRY_RUN || 'false').toLowerCase() === 'true',

    // Webhook 安全
    webhookSecret: process.env.WEBHOOK_SECRET || '',

    // 服务端口
    port: parseInt(process.env.PORT || '3000', 10),
    host: process.env.HOST || '0.0.0.0',

    // 订单默认设置
    defaultOrderType: (process.env.DEFAULT_ORDER_TYPE || 'market').toLowerCase(),
    defaultLeverage: parseInt(process.env.DEFAULT_LEVERAGE || '1', 10),
    positionMode: (process.env.POSITION_MODE || 'isolated').toLowerCase(),

    // 风控
    riskLimits: defaultConfig.riskLimits,

    // 交易对信息
    symbols: defaultConfig.symbols,

    // 支持的订单类型
    orderTypes: defaultConfig.orderTypes,

    // 信号格式映射
    signalFormats: defaultConfig.tradingViewAlertFormats,
  };

  return config;
}

/**
 * 验证配置完整性
 * @returns {{ valid: boolean, errors: string[] }}
 */
export function validateConfig() {
  const cfg = getConfig();
  const errors = [];

  // 模拟模式下不强制要求 API 凭据和 Webhook 密钥
  if (cfg.dryRun) {
    if (!cfg.apiKey)       console.warn('[配置] ⚠️ 模拟模式: OKX_API_KEY 未配置 — 以占位值运行');
    if (!cfg.apiSecret)    console.warn('[配置] ⚠️ 模拟模式: OKX_API_SECRET 未配置 — 以占位值运行');
    if (!cfg.apiPassphrase) console.warn('[配置] ⚠️ 模拟模式: OKX_API_PASSPHRASE 未配置 — 以占位值运行');
    if (!cfg.webhookSecret) console.warn('[配置] ⚠️ 模拟模式: WEBHOOK_SECRET 未配置 — 以占位值运行');
  } else {
    if (!cfg.apiKey)        errors.push('OKX_API_KEY 未配置 — 请在 Secrets 页面添加');
    if (!cfg.apiSecret)     errors.push('OKX_API_SECRET 未配置 — 请在 Secrets 页面添加');
    if (!cfg.apiPassphrase) errors.push('OKX_API_PASSPHRASE 未配置 — 请在 Secrets 页面添加（创建 API Key 时设置）');
    if (!cfg.webhookSecret) errors.push('WEBHOOK_SECRET 未配置 — 请设置一个随机密钥用于 TradingView 请求验证');
  }

  const validOrderTypes = Object.keys(cfg.orderTypes);
  if (!validOrderTypes.includes(cfg.defaultOrderType)) {
    errors.push(
      `DEFAULT_ORDER_TYPE 无效: "${cfg.defaultOrderType}"，有效值: ${validOrderTypes.join(', ')}`
    );
  }

  return { valid: errors.length === 0, errors };
}

export default { getConfig, validateConfig };
