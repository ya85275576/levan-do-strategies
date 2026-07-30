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
            max_memory_restart: '700M',
            restart_delay: 15000,
            max_restarts: 20,
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
                ENSEMBLE_MODELS: 'ecmwf_ifs,gfs_global,icon_global',
                ENSEMBLE_WEIGHTS: '0.4,0.3,0.3',

                // 🌐 多模型聚合 + 一致性评分（默认关闭, DRY_RUN 先跑）
                ENABLE_MULTI_MODEL: 'false',
                MULTI_MODEL_MODELS: 'ecmwf_ifs,gfs_global,icon_global,meteofrance_seamless',
                MULTI_MODEL_AGREEMENT_THRESH: '0.30',
                MULTI_MODEL_SPREAD_CAP: '15.0',

                // 🎯 v7: 自适应宽度
                ADAPTIVE_WIDTH_ENABLED: 'true',
                ADAPTIVE_WIDTH_MIN_SPREAD: '1',
                ADAPTIVE_WIDTH_MAX_SPREAD: '3',
                ADAPTIVE_WIDTH_THRESH_LOW: '0.35',
                ADAPTIVE_WIDTH_THRESH_HIGH: '0.70',

                // 🏙️ v7: 多城市联合扫描
                MULTI_CITY_ENABLED: 'true',
                MULTI_CITY_TOP_N: '2',

                // 💰 v7: 动态预算分配
                DYNAMIC_BUDGET_ENABLED: 'true',
                DYNAMIC_BUDGET_MIN: '0.20',
                DYNAMIC_BUDGET_MAX: '1.00',

                // 🧠 v8: 贝叶斯实时概率更新
                BAYESIAN_ENABLED: 'true',
                BAYESIAN_SCAN_INTERVAL: '600',
                BAYESIAN_EDGE: '0.08',
                BAYESIAN_COOLDOWN: '1800',
                BAYESIAN_DAILY_LIMIT: '5',
                BAYESIAN_POSITION_SIZE: '50',
                BAYESIAN_LIKELIHOOD_STRENGTH: '0.40',

                // 🧱 v7: 逐层止盈
                LAYERED_TP_ENABLED: 'true',
                LAYERED_TP_PROFIT_PCT: '0.10',
                LAYERED_TP_HOURS_BEFORE: '2.0',
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
                ENSEMBLE_MODELS: 'ecmwf_ifs,gfs_global,icon_global',
                ENSEMBLE_WEIGHTS: '0.4,0.3,0.3',

                // 🌐 多模型聚合 + 一致性评分（默认关闭, DRY_RUN 先跑）
                ENABLE_MULTI_MODEL: 'false',
                MULTI_MODEL_MODELS: 'ecmwf_ifs,gfs_global,icon_global,meteofrance_seamless',
                MULTI_MODEL_AGREEMENT_THRESH: '0.30',
                MULTI_MODEL_SPREAD_CAP: '15.0',

                // 🎯 v7: 自适应宽度
                ADAPTIVE_WIDTH_ENABLED: 'true',
                ADAPTIVE_WIDTH_MIN_SPREAD: '1',
                ADAPTIVE_WIDTH_MAX_SPREAD: '3',
                ADAPTIVE_WIDTH_THRESH_LOW: '0.35',
                ADAPTIVE_WIDTH_THRESH_HIGH: '0.70',

                // 🏙️ v7: 多城市联合扫描
                MULTI_CITY_ENABLED: 'true',
                MULTI_CITY_TOP_N: '2',

                // 💰 v7: 动态预算分配
                DYNAMIC_BUDGET_ENABLED: 'true',
                DYNAMIC_BUDGET_MIN: '0.20',
                DYNAMIC_BUDGET_MAX: '1.00',

                // 🧠 v8: 贝叶斯实时概率更新
                BAYESIAN_ENABLED: 'true',
                BAYESIAN_SCAN_INTERVAL: '600',
                BAYESIAN_EDGE: '0.08',
                BAYESIAN_COOLDOWN: '1800',
                BAYESIAN_DAILY_LIMIT: '5',
                BAYESIAN_POSITION_SIZE: '50',
                BAYESIAN_LIKELIHOOD_STRENGTH: '0.40',

                // 🧱 v7: 逐层止盈
                LAYERED_TP_ENABLED: 'true',
                LAYERED_TP_PROFIT_PCT: '0.10',
                LAYERED_TP_HOURS_BEFORE: '2.0',
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
                ENSEMBLE_MODELS: 'ecmwf_ifs,gfs_global,icon_global',
                ENSEMBLE_WEIGHTS: '0.4,0.3,0.3',

                // 🌐 多模型聚合 + 一致性评分（默认关闭, DRY_RUN 先跑）
                ENABLE_MULTI_MODEL: 'false',
                MULTI_MODEL_MODELS: 'ecmwf_ifs,gfs_global,icon_global,meteofrance_seamless',
                MULTI_MODEL_AGREEMENT_THRESH: '0.30',
                MULTI_MODEL_SPREAD_CAP: '15.0',

                // 🎯 v7: 自适应宽度
                ADAPTIVE_WIDTH_ENABLED: 'true',
                ADAPTIVE_WIDTH_MIN_SPREAD: '1',
                ADAPTIVE_WIDTH_MAX_SPREAD: '3',
                ADAPTIVE_WIDTH_THRESH_LOW: '0.35',
                ADAPTIVE_WIDTH_THRESH_HIGH: '0.70',

                // 🏙️ v7: 多城市联合扫描
                MULTI_CITY_ENABLED: 'true',
                MULTI_CITY_TOP_N: '2',

                // 💰 v7: 动态预算分配
                DYNAMIC_BUDGET_ENABLED: 'true',
                DYNAMIC_BUDGET_MIN: '0.20',
                DYNAMIC_BUDGET_MAX: '1.00',

                // 🧠 v8: 贝叶斯实时概率更新
                BAYESIAN_ENABLED: 'true',
                BAYESIAN_SCAN_INTERVAL: '600',
                BAYESIAN_EDGE: '0.08',
                BAYESIAN_COOLDOWN: '1800',
                BAYESIAN_DAILY_LIMIT: '5',
                BAYESIAN_POSITION_SIZE: '50',
                BAYESIAN_LIKELIHOOD_STRENGTH: '0.40',

                // 🧱 v7: 逐层止盈
                LAYERED_TP_ENABLED: 'true',
                LAYERED_TP_PROFIT_PCT: '0.10',
                LAYERED_TP_HOURS_BEFORE: '2.0',
            },

            // 全球保守模式 — 全倉實盤
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
                ENSEMBLE_MODELS: 'ecmwf_ifs,gfs_global,icon_global',
                ENSEMBLE_WEIGHTS: '0.4,0.3,0.3',

                // 🌐 多模型聚合 + 一致性评分（默认关闭, DRY_RUN 先跑）
                ENABLE_MULTI_MODEL: 'false',
                MULTI_MODEL_MODELS: 'ecmwf_ifs,gfs_global,icon_global,meteofrance_seamless',
                MULTI_MODEL_AGREEMENT_THRESH: '0.30',
                MULTI_MODEL_SPREAD_CAP: '15.0',
            },

            // 全球保守模式 — 香港參數推廣到全部天氣市場
            // 使用:  DRY_RUN=true pm2 start ecosystem.config.cjs --only hightemptation --env hk
            // 或:    pm2 restart hightemptation --update-env --env hk
            env_hk: {
                NODE_ENV: 'production',
                PYTHONUNBUFFERED: '1',
                // #1 全部城市 — 原香港參數推廣到全球 673 個天氣市場
                DRY_RUN: 'true',
                LOG_LEVEL: 'INFO',
                SCAN_INTERVAL_SEC: '120',
                DASHBOARD_PORT: '3002',
                INITIAL_CAPITAL: '5000',
                // #1 取消城市限制，掃描全部天氣市場
                // #2 雙向交易
                ALLOWED_SIDES: 'YES,NO',
                // #2 保守 edge 閾值 (8%，更高置信度)
                MIN_EDGE: '0.08',
                // #4 每倉 $1，高頻小額
                POSITION_MIN_USD: '1',
                POSITION_MAX_USD: '1',
                POSITION_CAPITAL_PCT: '0.001',
                // #5 持有至結算為主，快盈 +5% 為輔
                NO_TARGET_EXIT_PRICE: '0.98',
                QUICK_PROFIT_PCT: '0.05',
                FIXED_TAKE_PROFIT_PCT: '0.09',
                STOP_LOSS_PCT: '0.065',
                FORCE_EXIT_BEFORE_SETTLEMENT_HOURS: '4',
                // 掃單模式：放寬同城同天限制
                MAX_POSITIONS_PER_CITY_PER_DAY: '20',
                MAX_CONCURRENT_POSITIONS: '20',
                MAX_DAILY_LOSS_PCT: '0.10',
                LIVE_MODE_PHASE: 'hk',
                // 集成預報（保持高精度）
                ENSEMBLE_ENABLED: 'true',
                ENSEMBLE_MODELS: 'ecmwf_ifs,gfs_global,icon_global',
                ENSEMBLE_WEIGHTS: '0.4,0.3,0.3',
                // 🌐 多模型聚合 + 一致性评分（默认关闭, DRY_RUN 先跑）
                ENABLE_MULTI_MODEL: 'false',
                MULTI_MODEL_MODELS: 'ecmwf_ifs,gfs_global,icon_global,meteofrance_seamless',
                MULTI_MODEL_AGREEMENT_THRESH: '0.30',
                MULTI_MODEL_SPREAD_CAP: '15.0',
                // 偏差校正
                BIAS_ENABLED: 'true',
                BIAS_HISTORY_DAYS: '30',
                // 關閉階梯（簡化為單桶高置信度）
                LADDER_ENABLED: 'false',
                // Theta 懲罰 + 全天候交易窗口
                THETA_ENABLED: 'true',
                TRADE_START_HOUR: '0',
                TRADE_END_HOUR: '24',
                // OBI 過濾保持
                OBI_ENABLED: 'true',
                OBI_MIN_IMBALANCE: '0.3',

                // 🎯 v7: 自适应宽度
                ADAPTIVE_WIDTH_ENABLED: 'true',
                ADAPTIVE_WIDTH_MIN_SPREAD: '1',
                ADAPTIVE_WIDTH_MAX_SPREAD: '3',
                ADAPTIVE_WIDTH_THRESH_LOW: '0.35',
                ADAPTIVE_WIDTH_THRESH_HIGH: '0.70',

                // 🏙️ v7: 多城市联合扫描
                MULTI_CITY_ENABLED: 'true',
                MULTI_CITY_TOP_N: '2',

                // 💰 v7: 动态预算分配
                DYNAMIC_BUDGET_ENABLED: 'true',
                DYNAMIC_BUDGET_MIN: '0.20',
                DYNAMIC_BUDGET_MAX: '1.00',

                // 🧠 v8: 贝叶斯实时概率更新
                BAYESIAN_ENABLED: 'true',
                BAYESIAN_SCAN_INTERVAL: '600',
                BAYESIAN_EDGE: '0.08',
                BAYESIAN_COOLDOWN: '1800',
                BAYESIAN_DAILY_LIMIT: '5',
                BAYESIAN_POSITION_SIZE: '50',
                BAYESIAN_LIKELIHOOD_STRENGTH: '0.40',

                // 🧱 v7: 逐层止盈
                LAYERED_TP_ENABLED: 'true',
                LAYERED_TP_PROFIT_PCT: '0.10',
                LAYERED_TP_HOURS_BEFORE: '2.0',
            },
        },

        // ═══════════════════════════════════════════════════════════════
        // Streamlit 高级看板 (端口 8501)
        // Caddy 路由: shtdjf.indevs.in/dashboard/* → localhost:8501
        // ═══════════════════════════════════════════════════════════════
        {
            name: 'streamlit-dashboard',
            script: 'bash',
            args: '-c "streamlit run dashboard.py --server.port 8501"',
            cwd: path.join(PROJECT_ROOT, 'dashboard'),
            interpreter: 'none',
            exec_mode: 'fork',
            instances: 1,
            autorestart: true,
            watch: false,
            restart_delay: 5000,
            max_restarts: 20,
            exp_backoff_restart_delay: 100,
            max_memory_restart: '500M',
            env: {
                NODE_ENV: 'production',
                PYTHONUNBUFFERED: '1',
            },
        },

        // ═══════════════════════════════════════════════════════════════
        // v9 全优化版 — 10 模块升级一次性集成
        // ═══════════════════════════════════════════════════════════════
        // 使用: pm2 start ecosystem.config.cjs --only hightemptation-v9
        // 或:   pm2 restart hightemptation-v9 --update-env --env v9_backtest
        // ═══════════════════════════════════════════════════════════════
        {
            name: 'hightemptation-v9',
            script: 'hightemptation_live_v9.py',
            cwd: PROJECT_ROOT,
            interpreter: VENV_PYTHON,
            exec_mode: 'fork',
            instances: 1,
            autorestart: true,
            watch: false,
            max_memory_restart: '1G',
            restart_delay: 15000,
            max_restarts: 20,
            exp_backoff_restart_delay: 100,

            // v9 backtest 模式 (默认)
            env: {
                NODE_ENV: 'development',
                PYTHONUNBUFFERED: '1',
                DRY_RUN: 'true',
                LOG_LEVEL: 'INFO',
                SCAN_INTERVAL_SEC: '120',
                DASHBOARD_PORT: '3002',
                INITIAL_CAPITAL: '10000',

                // v8 核心配置
                BIAS_ENABLED: 'true',
                BIAS_HISTORY_DAYS: '30',
                CROSS_VALIDATE_ENABLED: 'true',
                MIN_24H_VOLUME_USD: '3000',
                MAX_BID_ASK_SPREAD: '0.03',
                NO_TARGET_EXIT_PRICE: '0.98',
                FORCE_EXIT_BEFORE_SETTLEMENT_HOURS: '4',
                POSITION_MIN_USD: '1',
                POSITION_MAX_USD: '1',
                POSITION_CAPITAL_PCT: '0.001',
                MAX_POSITIONS_PER_CITY_PER_DAY: '20',
                MAX_CONCURRENT_POSITIONS: '20',
                MAX_DAILY_LOSS_PCT: '0.10',
                ALLOWED_SIDES: 'YES,NO',
                MIN_EDGE: '0.08',
                QUICK_PROFIT_PCT: '0.05',
                FIXED_TAKE_PROFIT_PCT: '0.09',
                STOP_LOSS_PCT: '0.065',

                // 集成预报
                ENSEMBLE_ENABLED: 'true',
                ENSEMBLE_MODELS: 'ecmwf_ifs,gfs_global,icon_global',
                ENSEMBLE_WEIGHTS: '0.4,0.3,0.3',
                ENSEMBLE_40_ENABLED: 'true',
                ENSEMBLE_40_MODE: 'hybrid',

                // 🌐 多模型聚合
                ENABLE_MULTI_MODEL: 'true',
                MULTI_MODEL_MODELS: 'ecmwf_ifs,gfs_global,icon_global,meteofrance_seamless',

                // 🧠 v8: 贝叶斯
                BAYESIAN_ENABLED: 'true',
                BAYESIAN_SCAN_INTERVAL: '600',
                BAYESIAN_EDGE: '0.08',

                // 🎯 v7: 核心增强
                ADAPTIVE_WIDTH_ENABLED: 'true',
                MULTI_CITY_ENABLED: 'true',
                DYNAMIC_BUDGET_ENABLED: 'true',
                LAYERED_TP_ENABLED: 'true',

                // ====================================================
                // v9 新增模块 (全部默认启用)
                // ====================================================
                V9_MICRO_ENABLED: 'true',
                V9_TWAP_SLICES: '5',
                V9_DYNAMIC_STOPS_ENABLED: 'true',
                V9_ATR_PERIOD: '6',
                V9_PAPER_READER_ENABLED: 'true',
                V9_PAPER_SCAN_INTERVAL: '3600',
                V9_PORTFOLIO_ENABLED: 'true',
                V9_CORRELATION_ENABLED: 'true',
                V9_STRESS_TEST_ENABLED: 'true',
                V9_STRESS_TEST_INTERVAL: '21600',
                V9_DQM_ENABLED: 'true',
                V9_DQM_CHECK_INTERVAL: '600',
                V9_CIRCUIT_BREAKER_ENABLED: 'true',
                V9_REPORTER_ENABLED: 'true',
            },

            // v9 实盘模式 (DRY_RUN=false)
            env_v9_live: {
                NODE_ENV: 'production',
                PYTHONUNBUFFERED: '1',
                DRY_RUN: 'false',
                LOG_LEVEL: 'INFO',
                SCAN_INTERVAL_SEC: '60',
                DASHBOARD_PORT: '3002',
                INITIAL_CAPITAL: '10000',
                POSITION_MIN_USD: '50',
                POSITION_MAX_USD: '200',
                MAX_CONCURRENT_POSITIONS: '10',
                MAX_DAILY_LOSS_PCT: '0.05',
                LIVE_MODE_PHASE: 'v9_live',

                // v9 全部开启
                V9_MICRO_ENABLED: 'true',
                V9_DYNAMIC_STOPS_ENABLED: 'true',
                V9_PAPER_READER_ENABLED: 'true',
                V9_PORTFOLIO_ENABLED: 'true',
                V9_CORRELATION_ENABLED: 'true',
                V9_STRESS_TEST_ENABLED: 'true',
                V9_DQM_ENABLED: 'true',
                V9_CIRCUIT_BREAKER_ENABLED: 'true',
                V9_REPORTER_ENABLED: 'true',
            },
        },
    ],
};
