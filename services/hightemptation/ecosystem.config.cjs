/**
 * HighTempTation 天氣預報校準套利 Bot — PM2 進程管理配置
 *
 * 核心策略：
 *   1. Open-Meteo 多模型預報 → 高斯分布 CDF
 *   2. 計算每個溫度桶的模型概率 p_model
 *   3. Polymarket Gamma API → 市場價格 p_market
 *   4. p_market - p_model ≥ 15% → 買 NO
 *
 * 補充：
 *   #1 使用 ICAO 氣象站坐標（station_map.py）
 *   #2 偏差校正（Open-Meteo Archive）
 *   #3 嚴格出場：≥98¢ 止盈 / <4h 強制平
 *   #4 流動性過濾：24h 量 >$3k、價差 <3¢
 *   #5 風控：$200-700、同日同城最多 2 檔、日虧 >5% 停手
 *   #6 Open-Meteo + Archive 交叉驗證
 *   #8 私鑰僅環境變量（永不寫入文件）
 *
 * 使用方式:
 *   pm2 start ecosystem.config.cjs                # 啟動 (backtest 模式)
 *   pm2 start ecosystem.config.cjs --env observe   # 信號觀察模式
 *   DRY_RUN=false pm2 restart hightemptation       # 轉實盤（需私鑰）
 *   pm2 logs hightemptation
 *   pm2 stop hightemptation
 */
const path = require('path');

const PROJECT_ROOT = __dirname;
const VENV_PYTHON = path.join(PROJECT_ROOT, '.venv', 'bin', 'python3');

module.exports = {
    apps: [
        {
            name: 'hightemptation',
            script: 'bot.py',
            cwd: PROJECT_ROOT,
            interpreter: VENV_PYTHON,
            exec_mode: 'fork',
            instances: 1,
            autorestart: true,
            watch: false,
            max_memory_restart: '400M',
            restart_delay: 10000,
            max_restarts: 10,
            exp_backoff_restart_delay: 100,

            // backtest 模式（預設）— 不下單，只分析
            env: {
                NODE_ENV: 'development',
                PYTHONUNBUFFERED: '1',
                DRY_RUN: 'true',
                LOG_LEVEL: 'INFO',
                SCAN_INTERVAL_SEC: '120',
                DASHBOARD_PORT: '3002',
                INITIAL_CAPITAL: '10000',

                // #2 偏差校正
                BIAS_ENABLED: 'true',
                BIAS_HISTORY_DAYS: '30',
                BIAS_MIN_SAMPLES: '5',

                // #6 交叉驗證
                CROSS_VALIDATE_ENABLED: 'true',

                // #4 流動性
                MIN_24H_VOLUME_USD: '3000',
                MAX_BID_ASK_SPREAD: '0.03',

                // #3 出場
                NO_TARGET_EXIT_PRICE: '0.98',
                FORCE_EXIT_BEFORE_SETTLEMENT_HOURS: '4',

                // #5 風控
                POSITION_MIN_USD: '200',
                POSITION_MAX_USD: '700',
                POSITION_CAPITAL_PCT: '0.02',
                MAX_POSITIONS_PER_CITY_PER_DAY: '2',
                MAX_CONCURRENT_POSITIONS: '10',
                MAX_DAILY_LOSS_PCT: '0.05',

                // #7 實盤階段
                LIVE_MODE_PHASE: 'backtest',

                // 🪜 溫度階梯套利
                LADDER_ENABLED: 'true',
                LADDER_SPREAD: '1',
                LADDER_EDGE_BOOST: '1.3',
                LADDER_SIZE_PCT: '0.5',

                // 🌐 集成預報
                ENSEMBLE_ENABLED: 'true',
                ENSEMBLE_MODELS: 'ecmwf_seamless,gfs_seamless,icon_seamless',
                ENSEMBLE_WEIGHTS: '0.4,0.3,0.3',
            },

            // observe 模式 — 記錄信號但不執行交易
            env_observe: {
                NODE_ENV: 'production',
                PYTHONUNBUFFERED: '1',
                DRY_RUN: 'true',
                LOG_LEVEL: 'INFO',
                SCAN_INTERVAL_SEC: '120',
                DASHBOARD_PORT: '3002',
                INITIAL_CAPITAL: '10000',
                BIAS_ENABLED: 'true',
                BIAS_HISTORY_DAYS: '30',
                CROSS_VALIDATE_ENABLED: 'true',
                MIN_24H_VOLUME_USD: '3000',
                MAX_BID_ASK_SPREAD: '0.03',
                NO_TARGET_EXIT_PRICE: '0.98',
                FORCE_EXIT_BEFORE_SETTLEMENT_HOURS: '4',
                POSITION_MIN_USD: '200',
                POSITION_MAX_USD: '700',
                MAX_POSITIONS_PER_CITY_PER_DAY: '2',
                MAX_CONCURRENT_POSITIONS: '10',
                MAX_DAILY_LOSS_PCT: '0.05',
                LIVE_MODE_PHASE: 'observe',
                LADDER_ENABLED: 'true',
                LADDER_SPREAD: '1',
                LADDER_EDGE_BOOST: '1.3',
                LADDER_SIZE_PCT: '0.5',
                ENSEMBLE_ENABLED: 'true',
                ENSEMBLE_MODELS: 'ecmwf_seamless,gfs_seamless,icon_seamless',
                ENSEMBLE_WEIGHTS: '0.4,0.3,0.3',
            },

            // small 模式 — 小倉實盤測試
            env_small: {
                NODE_ENV: 'production',
                PYTHONUNBUFFERED: '1',
                DRY_RUN: 'false',
                LOG_LEVEL: 'INFO',
                SCAN_INTERVAL_SEC: '120',
                DASHBOARD_PORT: '3002',
                INITIAL_CAPITAL: '5000',
                BIAS_ENABLED: 'true',
                BIAS_HISTORY_DAYS: '60',
                CROSS_VALIDATE_ENABLED: 'true',
                MIN_24H_VOLUME_USD: '5000',
                MAX_BID_ASK_SPREAD: '0.03',
                NO_TARGET_EXIT_PRICE: '0.98',
                FORCE_EXIT_BEFORE_SETTLEMENT_HOURS: '4',
                POSITION_MIN_USD: '50',
                POSITION_MAX_USD: '200',
                MAX_POSITIONS_PER_CITY_PER_DAY: '1',
                MAX_CONCURRENT_POSITIONS: '3',
                MAX_DAILY_LOSS_PCT: '0.03',
                LIVE_MODE_PHASE: 'small',
                LADDER_ENABLED: 'true',
                LADDER_SPREAD: '1',
                LADDER_EDGE_BOOST: '1.2',
                LADDER_SIZE_PCT: '0.4',
                ENSEMBLE_ENABLED: 'true',
                ENSEMBLE_MODELS: 'ecmwf_seamless,gfs_seamless,icon_seamless',
                ENSEMBLE_WEIGHTS: '0.4,0.3,0.3',
            },

            // full 模式 — 全倉實盤
            env_full: {
                NODE_ENV: 'production',
                PYTHONUNBUFFERED: '1',
                DRY_RUN: 'false',
                LOG_LEVEL: 'INFO',
                SCAN_INTERVAL_SEC: '60',
                DASHBOARD_PORT: '3002',
                INITIAL_CAPITAL: '10000',
                BIAS_ENABLED: 'true',
                BIAS_HISTORY_DAYS: '60',
                CROSS_VALIDATE_ENABLED: 'true',
                MIN_24H_VOLUME_USD: '3000',
                MAX_BID_ASK_SPREAD: '0.03',
                NO_TARGET_EXIT_PRICE: '0.98',
                FORCE_EXIT_BEFORE_SETTLEMENT_HOURS: '4',
                POSITION_MIN_USD: '200',
                POSITION_MAX_USD: '700',
                MAX_POSITIONS_PER_CITY_PER_DAY: '2',
                MAX_CONCURRENT_POSITIONS: '10',
                MAX_DAILY_LOSS_PCT: '0.05',
                LIVE_MODE_PHASE: 'full',
                LADDER_ENABLED: 'true',
                LADDER_SPREAD: '1',
                LADDER_EDGE_BOOST: '1.3',
                LADDER_SIZE_PCT: '0.5',
                ENSEMBLE_ENABLED: 'true',
                ENSEMBLE_MODELS: 'ecmwf_seamless,gfs_seamless,icon_seamless',
                ENSEMBLE_WEIGHTS: '0.4,0.3,0.3',
            },
        },
    ],
};
