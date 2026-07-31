/**
 * HighTempTation — PM2 进程管理配置
 *
 * 使用方式:
 *   pm2 start ecosystem.config.cjs           # 启动所有服务
 *   pm2 restart weather-dashboard            # 重启天气看板
 *   pm2 logs weather-dashboard               # 查看日志
 *   pm2 stop weather-dashboard               # 停止
 */
const path = require('path');

const PROJECT_ROOT = __dirname;

module.exports = {
    apps: [
        // ═══════════════════════════════════════════════════════════════
        // 天气看板独立服务器 (端口 3002)
        // 提供 HTML 仪表板 + /api/status REST API
        // ═══════════════════════════════════════════════════════════════
        {
            name: 'weather-dashboard',
            script: 'dashboard_server.py',
            cwd: path.join(PROJECT_ROOT, 'dashboard'),
            interpreter: 'python3',
            exec_mode: 'fork',
            instances: 1,
            autorestart: true,
            watch: false,
            restart_delay: 5000,
            max_restarts: 20,
            max_memory_restart: '500M',
            exp_backoff_restart_delay: 100,
            env: {
                DASHBOARD_HOST: '0.0.0.0',
                DASHBOARD_PORT: '3002',
                REFRESH_SEC: '30',
            },
        },

        // ═══════════════════════════════════════════════════════════════
        // HighTempTation 天气交易 Bot
        // 启动前请确保 .env 配置正确
        // ═══════════════════════════════════════════════════════════════
        {
            name: 'hightemptation-bot',
            script: 'bot.py',
            cwd: PROJECT_ROOT,
            interpreter: 'python3',
            exec_mode: 'fork',
            instances: 1,
            autorestart: true,
            watch: false,
            restart_delay: 10000,
            max_restarts: 10,
            exp_backoff_restart_delay: 100,
            max_memory_restart: '700M',
            env: {
                DRY_RUN: 'true',
                PYTHONUNBUFFERED: '1',
            },
        },

        // ═══════════════════════════════════════════════════════════════
        // Streamlit 高级看板 (端口 8501)
        // 需要安装: pip install streamlit plotly pandas
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
                PYTHONUNBUFFERED: '1',
            },
        },

        // ═══════════════════════════════════════════════════════════════
        // 5min Bot 子模块 (Benjam1nCup 整合: 套利/狙击/动量/阶梯)
        // 独立运行入口 (调试/回测用)。
        // 生产模式推荐: 由 hightemptation-bot 内部集成启动
        // (PM5_ENABLED=true, 共享风控+统一账户+看板桥接),
        // 不要与本进程同时运行, 避免重复交易同一账户。
        // ═══════════════════════════════════════════════════════════════
        {
            name: 'polymarket-5min-standalone',
            script: 'python3',
            args: '-m polymarket_5min_bot',
            cwd: PROJECT_ROOT,
            interpreter: 'none',
            exec_mode: 'fork',
            instances: 1,
            autorestart: true,
            watch: false,
            restart_delay: 10000,
            max_restarts: 10,
            exp_backoff_restart_delay: 100,
            max_memory_restart: '400M',
            env: {
                DRY_RUN: 'true',
                PYTHONUNBUFFERED: '1',
                PM5_ENABLED: 'true',
            },
        },
    ],
};
