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
    ],
};
