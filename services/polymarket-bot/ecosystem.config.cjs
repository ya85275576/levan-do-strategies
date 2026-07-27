/**
 * Polymarket YES+NO=$1 互补套利机器人 — PM2 进程管理配置
 *
 * 使用方式:
 *   pm2 start ecosystem.config.cjs                    # 启动（默认模拟模式）
 *   pm2 start ecosystem.config.cjs --env production   # 实盘
 *   pm2 stop polymarket-arbitrage
 *   pm2 logs polymarket-arbitrage
 *   pm2 restart polymarket-arbitrage
 */
module.exports = {
    apps: [
        {
            name: 'polymarket-arbitrage',
            script: 'main.py',
            args: '--loop',
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

            // 环境变量（默认：模拟模式）
            env: {
                NODE_ENV: 'development',
                PYTHONUNBUFFERED: '1',
                DRY_RUN: 'true',
                LOG_LEVEL: 'INFO',
                ARBITRAGE_THRESHOLD: '0.98',
                SCAN_INTERVAL_SEC: '60',
                MIN_LIQUIDITY_USDC: '100',
                MAX_PAGES: '5',
                MIN_YES_PRICE: '0.02',
                MAX_YES_PRICE: '0.98',
                MIN_NO_PRICE: '0.02',
                MAX_NO_PRICE: '0.98',
                TRADE_SIZE: '100',
                STATE_FILE: '/tmp/polymarket-arbitrage-state.json',
                OPPORTUNITIES_FILE: '/tmp/polymarket-arbitrage-opportunities.json',
            },

            // 实盘环境（使用 --env production）
            env_production: {
                NODE_ENV: 'production',
                PYTHONUNBUFFERED: '1',
                DRY_RUN: 'false',
                LOG_LEVEL: 'INFO',
                ARBITRAGE_THRESHOLD: '0.98',
                SCAN_INTERVAL_SEC: '30',
                MIN_LIQUIDITY_USDC: '500',
                MAX_PAGES: '5',
                MIN_YES_PRICE: '0.02',
                MAX_YES_PRICE: '0.98',
                MIN_NO_PRICE: '0.02',
                MAX_NO_PRICE: '0.98',
                TRADE_SIZE: '500',
                STATE_FILE: '/tmp/polymarket-arbitrage-state.json',
                OPPORTUNITIES_FILE: '/tmp/polymarket-arbitrage-opportunities.json',
                ARBITRAGE_WEBHOOK_URL: '',
            },

            // 日志
            log_date_format: 'YYYY-MM-DD HH:mm:ss.SSS',
            error_file: './logs/polymarket-error.log',
            out_file: './logs/polymarket-out.log',
            merge_logs: true,
        },
    ],
};
