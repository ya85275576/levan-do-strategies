/**
 * LE VAN DO® OKX 原生交易机器人 — PM2 进程管理配置
 *
 * 使用方式:
 *   pm2 start ecosystem.config.js           # 启动（默认模拟模式）
 *   pm2 start ecosystem.config.js --env production  # 实盘
 *   pm2 stop le-van-do-bot
 *   pm2 logs le-van-do-bot
 *   pm2 restart le-van-do-bot
 */
module.exports = {
    apps: [
        {
            name: 'le-van-do-bot',
            script: 'bot.py',
            cwd: __dirname,
            interpreter: '/usr/bin/python3',
            exec_mode: 'fork',
            instances: 1,
            autorestart: true,
            watch: false,
            max_memory_restart: '200M',
            restart_delay: 5000,
            max_restarts: 10,
            exp_backoff_restart_delay: 100,

            // 环境变量（默认：测试网 / 模拟模式）
            env: {
                NODE_ENV: 'development',
                PYTHONUNBUFFERED: '1',
                EXCHANGE_NETWORK: 'testnet',
                DRY_RUN: 'true',
                LOG_LEVEL: 'INFO',
                SETUP_TYPE: 'Open/Close',
                TPS_TYPE: 'Trailing',
                SIDEWAYS_FILTER: 'No Filtering',
                TRADING_SYMBOLS: 'BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,DOGE-USDT,ADA-USDT,AVAX-USDT,DOT-USDT,LINK-USDT,MATIC-USDT,UNI-USDT,SHIB-USDT,LTC-USDT,BCH-USDT,ATOM-USDT,ETC-USDT,XLM-USDT,TRX-USDT,FIL-USDT,APT-USDT,ARB-USDT,OP-USDT,SUI-USDT,PEPE-USDT,INJ-USDT,TIA-USDT,SEI-USDT,RUNE-USDT,FET-USDT,GRT-USDT,NEAR-USDT,ICP-USDT,RENDER-USDT,IMX-USDT,MKR-USDT,AAVE-USDT,CRV-USDT,SNX-USDT,COMP-USDT,EOS-USDT,ALGO-USDT,FLOW-USDT,SAND-USDT,MANA-USDT,AXS-USDT,THETA-USDT,FTM-USDT,CVX-USDT,1INCH-USDT,STX-USDT',
                BASE_TIMEFRAME_MIN: '15',
                TF_MULT: '18',
            },

            // 实盘环境（使用 --env production）
            env_production: {
                NODE_ENV: 'production',
                PYTHONUNBUFFERED: '1',
                EXCHANGE_NETWORK: 'production',
                DRY_RUN: 'false',
                LOG_LEVEL: 'INFO',
                SETUP_TYPE: 'Open/Close',
                TPS_TYPE: 'Trailing',
                SIDEWAYS_FILTER: 'No Filtering',
                TRADING_SYMBOLS: 'BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,DOGE-USDT,ADA-USDT,AVAX-USDT,DOT-USDT,LINK-USDT,MATIC-USDT,UNI-USDT,SHIB-USDT,LTC-USDT,BCH-USDT,ATOM-USDT,ETC-USDT,XLM-USDT,TRX-USDT,FIL-USDT,APT-USDT,ARB-USDT,OP-USDT,SUI-USDT,PEPE-USDT,INJ-USDT,TIA-USDT,SEI-USDT,RUNE-USDT,FET-USDT,GRT-USDT,NEAR-USDT,ICP-USDT,RENDER-USDT,IMX-USDT,MKR-USDT,AAVE-USDT,CRV-USDT,SNX-USDT,COMP-USDT,EOS-USDT,ALGO-USDT,FLOW-USDT,SAND-USDT,MANA-USDT,AXS-USDT,THETA-USDT,FTM-USDT,CVX-USDT,1INCH-USDT,STX-USDT',
                BASE_TIMEFRAME_MIN: '15',
                TF_MULT: '18',
            },

            // 日志
            log_date_format: 'YYYY-MM-DD HH:mm:ss.SSS',
            error_file: './logs/bot-error.log',
            out_file: './logs/bot-out.log',
            merge_logs: true,
        },
    ],
};
