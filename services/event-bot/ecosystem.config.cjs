/**
 * OKX 事件合約自動交易機器人 — PM2 進程管理配置
 *
 * 使用方式:
 *   pm2 start ecosystem.config.js                          # 啟動（模擬模式）
 *   pm2 start ecosystem.config.js --env production         # 實盤模式
 *   pm2 stop event-bot
 *   pm2 logs event-bot
 *   pm2 restart event-bot
 *
 * 注意：修改環境變數後需 pm2 delete event-bot && pm2 start ecosystem.config.js
 */
module.exports = {
    apps: [
        {
            name: 'event-bot',
            script: 'event_bot.py',
            cwd: __dirname,
            interpreter: '/usr/bin/python3',
            exec_mode: 'fork',
            instances: 1,
            autorestart: true,
            watch: false,
            max_memory_restart: '100M',
            restart_delay: 5000,
            max_restarts: 10,
            exp_backoff_restart_delay: 100,

            // 預設環境（模擬模式）
            env: {
                NODE_ENV: 'development',
                PYTHONUNBUFFERED: '1',
                // 交易系列（逗號分隔）
                EVENT_TRADING_SERIES: 'BTC-UPDOWN-5MIN,ETH-UPDOWN-5MIN,SOL-UPDOWN-5MIN',
                // 模擬模式
                EVENT_DRY_RUN: 'true',
                // 交易比例（帳戶餘額百分比）
                EVENT_TRADE_QTY_PCT: '10',
                // 每筆最大合約數
                EVENT_MAX_POSITION_SIZE: '50',
                // 每筆最小合約數
                EVENT_MIN_POSITION_SIZE: '5',
                // 初始資金（用於模擬模式）
                EVENT_INITIAL_CAPITAL: '500',
                // 每日虧損上限（USDT）
                EVENT_DAILY_LOSS_LIMIT: '30',
                // 連續虧損次數上限
                EVENT_CONSECUTIVE_LOSS_LIMIT: '3',
                // 最大同時持有合約數
                EVENT_MAX_CONCURRENT: '3',
                // 動量回看 K 線數量
                EVENT_MOMENTUM_LOOKBACK: '3',
                // 動量閾值（百分比）
                EVENT_MOMENTUM_THRESHOLD_PCT: '0.05',
                // 最大買入價格
                EVENT_MAX_BUY_PRICE: '0.7',
                // 輪詢間隔（秒）
                EVENT_POLL_INTERVAL: '30',
                // 日誌級別
                EVENT_LOG_LEVEL: 'INFO',
                // OKX CLI Profile
                OKX_PROFILE: 'demo',
                // 狀態檔案路徑
                EVENT_STATUS_FILE: '/tmp/event-bot-status.json',
                EVENT_CLOSED_TRADES_FILE: '/tmp/event-bot-closed.json',
            },

            // 實盤環境（使用 --env production）
            env_production: {
                NODE_ENV: 'production',
                PYTHONUNBUFFERED: '1',
                EVENT_TRADING_SERIES: 'BTC-UPDOWN-5MIN,ETH-UPDOWN-5MIN,SOL-UPDOWN-5MIN',
                EVENT_DRY_RUN: 'false',
                EVENT_TRADE_QTY_PCT: '10',
                EVENT_MAX_POSITION_SIZE: '100',
                EVENT_MIN_POSITION_SIZE: '10',
                EVENT_INITIAL_CAPITAL: '500',
                EVENT_DAILY_LOSS_LIMIT: '50',
                EVENT_CONSECUTIVE_LOSS_LIMIT: '3',
                EVENT_MAX_CONCURRENT: '3',
                EVENT_MOMENTUM_LOOKBACK: '3',
                EVENT_MOMENTUM_THRESHOLD_PCT: '0.05',
                EVENT_MAX_BUY_PRICE: '0.7',
                EVENT_POLL_INTERVAL: '30',
                EVENT_LOG_LEVEL: 'INFO',
                OKX_PROFILE: 'demo',
                EVENT_STATUS_FILE: '/tmp/event-bot-status.json',
                EVENT_CLOSED_TRADES_FILE: '/tmp/event-bot-closed.json',
            },

            // 日誌
            log_date_format: 'YYYY-MM-DD HH:mm:ss.SSS',
            error_file: './logs/event-bot-error.log',
            out_file: './logs/event-bot-out.log',
            merge_logs: true,
        },
    ],
};
