#!/usr/bin/env node
/**
 * LE VAN DO® — OKX 交易机器人定时检查脚本
 *
 * 用法：
 *   node scripts/check-status.js                          # 默认：http://43.133.210.83:3000
 *   node scripts/check-status.js --url http://localhost:3000
 *   node scripts/check-status.js --json                   # JSON 格式输出
 *   node scripts/check-status.js --save-state             # 自动保存状态到文件
 *   node scripts/check-status.js --notify                 # 通知模式（简洁输出）
 *   node scripts/check-status.js --save-state --notify    # 保存状态 + 通知
 *   node scripts/check-status.js --webhook=https://...    # POST 通知到 Webhook
 *
 * 定时任务（每 15 分钟）：
 *   crontab -e
 *   每15分钟执行: cd /path/to/services && /usr/bin/node scripts/check-status.js --save-state
 *   带通知:      cd /path/to/services && /usr/bin/node scripts/check-status.js --save-state --notify
 *
 * 也可作为模块导入：
 *   import { checkStatus } from './check-status.js';
 */

import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const API_URL = process.env.API_URL || 'http://43.133.210.83:3000';
const TIMEOUT = 10000; // 10s
const STATE_FILE = join(__dirname, '..', '.check-state.json');

// ======== 辅助函数 ========

function fmtBytes(bytes) {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return (bytes / Math.pow(1024, i)).toFixed(1) + ' ' + units[i];
}

function fmtUptime(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const parts = [];
  if (d > 0) parts.push(`${d} 天`);
  if (h > 0) parts.push(`${h} 小时`);
  if (m > 0) parts.push(`${m} 分`);
  parts.push(`${s} 秒`);
  return parts.join('');
}

function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function fmtPct(v) {
  return v.toFixed(1) + '%';
}

function colorByPct(pct) {
  if (pct > 80) return '🔴';
  if (pct > 60) return '🟡';
  return '🟢';
}

function signalTypeLabel(type) {
  const labels = {
    longE:  '📗 多头开仓',
    shortE: '📕 空头开仓',
    longX:  '📘 多头平仓',
    shortX: '📙 空头平仓',
  };
  return labels[type] || type;
}

function signalTypeColor(type) {
  const colors = {
    longE:  '\x1b[32m', // green
    shortE: '\x1b[31m', // red
    longX:  '\x1b[34m', // blue
    shortX: '\x1b[33m', // yellow
  };
  return colors[type] || '\x1b[37m';
}

const RESET = '\x1b[0m';
const BOLD = '\x1b[1m';
const CYAN = '\x1b[36m';
const GRAY = '\x1b[90m';

// ======== 主检查逻辑 ========

export async function checkStatus(options = {}) {
  const url = options.url || API_URL;
  const prevTotal = options.prevTotal;

  let response;
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), options.timeout || TIMEOUT);
    response = await fetch(`${url}/api/status`, { signal: controller.signal });
    clearTimeout(timeoutId);
  } catch (err) {
    return {
      success: false,
      error: `连接服务器失败: ${err.message}`,
      serverUrl: url,
      timestamp: new Date().toISOString(),
    };
  }

  if (!response.ok) {
    return {
      success: false,
      error: `HTTP ${response.status}: ${response.statusText}`,
      serverUrl: url,
      timestamp: new Date().toISOString(),
    };
  }

  const data = await response.json();

  // ---- 构建报告 ----

  const lines = [];
  const divider = '='.repeat(60);
  const subDivider = '-'.repeat(60);
  const now = new Date().toISOString();

  lines.push('');
  lines.push(`${BOLD}╔${'═'.repeat(58)}╗${RESET}`);
  lines.push(`${BOLD}║   LE VAN DO® — OKX 交易机器人状态报告          ║${RESET}`);
  lines.push(`${BOLD}╚${'═'.repeat(58)}╝${RESET}`);
  lines.push(`  检查时间: ${CYAN}${fmtTime(now)}${RESET}`);
  lines.push(`  服务器:   ${url}`);
  lines.push(`  运行时长: ${data.server.uptime}`);
  lines.push('');

  // ---- 1. 进程状态 ----
  lines.push(`${BOLD}📋 一、进程状态${RESET}`);
  lines.push(divider);
  if (data.processes && data.processes.length > 0) {
    for (const p of data.processes) {
      const isOnline = p.status === 'online';
      const icon = isOnline ? '✅' : '❌';
      lines.push(`  ${icon} [${p.name}]`);
      lines.push(`     PID:     ${p.pid}`);
      lines.push(`     状态:    ${isOnline ? '🟢 运行中' : '🔴 已停止'}`);
      lines.push(`     运行:    ${p.uptime}`);
      lines.push(`     重启:    ${p.restartCount} 次`);
      lines.push(`     CPU:     ${p.cpu}%`);
      lines.push(`     内存:    ${fmtBytes(p.memory)}`);
    }
    const allOnline = data.processes.every(p => p.status === 'online');
    lines.push(`   → 总体状态: ${allOnline ? '✅ 所有进程正常运行' : '⚠️ 部分进程异常'}`);
  } else {
    lines.push(`  ⚠️ 无 PM2 进程数据`);
  }
  lines.push('');

  // ---- 2. 交易所状态 ----
  lines.push(`${BOLD}🔗 二、交易所状态${RESET}`);
  lines.push(divider);
  const ex = data.exchange;
  lines.push(`  交易所:    ${ex.name} (${ex.exchangeType})`);
  lines.push(`  网络:      ${ex.network} ${ex.isTestnet ? '(测试网)' : '(实盘)'}`);
  lines.push(`  连接:      ${ex.status === 'connected' ? '✅ 已连接' : '❌ 已断开'}`);
  lines.push(`  模拟模式:  ${ex.dryRun ? '🟢 启用' : '🔴 关闭'}`);
  lines.push(`  账户:      ${ex.accountStatus}`);
  lines.push(`  订单类型:  ${ex.orderType}`);
  lines.push(`  杠杆:      ${ex.leverage}x`);
  lines.push(`  仓位模式:  ${ex.positionMode}`);
  lines.push('');

  // ---- 3. 信号统计 ----
  lines.push(`${BOLD}📡 三、策略信号统计${RESET}`);
  lines.push(divider);
  const sig = data.signals;
  const counts = sig.counts || { longE: 0, shortE: 0, longX: 0, shortX: 0 };
  const total = sig.total || 0;

  // 与上次对比
  if (prevTotal !== undefined && prevTotal !== null) {
    const diff = total - prevTotal;
    if (diff > 0) {
      lines.push(`  📊 累计信号: ${BOLD}${total}${RESET} 条 ${CYAN}(较上次 +${diff} 条)${RESET}`);
    } else if (diff === 0) {
      lines.push(`  📊 累计信号: ${total} 条 ${GRAY}(与上次相同)${RESET}`);
    } else {
      lines.push(`  📊 累计信号: ${total} 条 ${GRAY}(上次记录为 ${prevTotal})${RESET}`);
    }
  } else {
    lines.push(`  📊 累计信号: ${BOLD}${total}${RESET} 条`);
  }
  lines.push(`    📗 longE  (多头开仓):  ${String(counts.longE || 0).padStart(4)}`);
  lines.push(`    📕 shortE (空头开仓):  ${String(counts.shortE || 0).padStart(4)}`);
  lines.push(`    📘 longX  (多头平仓):  ${String(counts.longX || 0).padStart(4)}`);
  lines.push(`    📙 shortX (空头平仓):  ${String(counts.shortX || 0).padStart(4)}`);

  // 最近信号
  const recent = sig.recent || [];
  if (recent.length > 0) {
    lines.push('');
    lines.push(`  ${BOLD}最近信号 (最新 ${Math.min(recent.length, 10)} 条):${RESET}`);
    lines.push(`  ${GRAY}  ${'时间'.padEnd(19)} ${'类型'.padEnd(14)} ${'交易对'.padEnd(12)} 价格${RESET}`);
    lines.push(`  ${GRAY}  ${'─'.repeat(18)} ${'─'.repeat(12)} ${'─'.repeat(10)} ${'─'.repeat(10)}${RESET}`);

    const showCount = Math.min(recent.length, 10);
    for (let i = 0; i < showCount; i++) {
      const r = recent[i];
      const t = fmtTime(r.time);
      const st = r.type.padEnd(10);
      const sym = r.symbol.padEnd(10);
      const price = r.price ? `$${parseFloat(r.price).toFixed(2)}` : '—';
      const color = signalTypeColor(r.type);
      lines.push(`  ${t} ${color}${r.type}${RESET}  ${sym} ${price}`);
    }
  } else {
    lines.push(`   → ${GRAY}暂无信号记录${RESET}`);
  }
  lines.push('');

  // ---- 4. 持仓信息 ----
  lines.push(`${BOLD}💼 四、当前持仓${RESET}`);
  lines.push(divider);
  const positions = data.positions || [];
  if (positions.length > 0) {
    lines.push(`  ${GRAY}  ${'交易对'.padEnd(12)} ${'方向'.padEnd(8)} ${'数量'.padEnd(10)} 入场价格${RESET}`);
    lines.push(`  ${GRAY}  ${'─'.repeat(10)} ${'─'.repeat(6)} ${'─'.repeat(8)} ${'─'.repeat(10)}${RESET}`);
    for (const p of positions) {
      const dir = p.side === 'long' ? '📈 多头' : '📉 空头';
      const price = p.price ? `$${p.price.toFixed(2)}` : '待更新';
      lines.push(`  ${p.symbol.padEnd(12)} ${dir.padEnd(8)} ${String(p.size).padEnd(8)} ${price}`);
    }
  } else {
    lines.push(`  📦 当前无持仓`);
  }
  lines.push('');

  // ---- 5. 系统资源 ----
  lines.push(`${BOLD}🖥️ 五、系统资源状况${RESET}`);
  lines.push(divider);
  const sys = data.system;
  let memOk = true, diskOk = true, cpuOk = true;
  let allHealthy = true;
  if (sys) {
    const mem = sys.memory;
    const disk = sys.disk;
    const cpu = sys.cpu;

    // 内存
    const memColor = colorByPct(mem.usagePercent);
    lines.push(`  内存: ${memColor} ${fmtPct(mem.usagePercent)}`);
    lines.push(`       已用 ${fmtBytes(mem.used)} / 总计 ${fmtBytes(mem.total)}`);

    // 磁盘
    const diskColor = colorByPct(disk.usagePercent);
    lines.push(`  磁盘: ${diskColor} ${fmtPct(disk.usagePercent)}`);
    lines.push(`       已用 ${fmtBytes(disk.used)} / 总计 ${fmtBytes(disk.total)}`);

    // CPU
    lines.push(`  CPU:  ${cpu.cores} 核心 · 负载: 1m=${cpu.loadAvg[0]} / 5m=${cpu.loadAvg[1]} / 15m=${cpu.loadAvg[2]}`);

    // 总评
    memOk = mem.usagePercent < 80;
    diskOk = disk.usagePercent < 80;
    cpuOk = cpu.loadAvg[0] < cpu.cores * 0.8;
    allHealthy = memOk && diskOk && cpuOk;
    lines.push(`  → 系统健康: ${allHealthy ? '✅ 正常' : '⚠️ 需要关注'}`);
  } else {
    lines.push(`  系统资源信息不可用`);
  }
  lines.push('');

  // ---- 6. 交易对行情概要 ----
  const symbols = data.symbols || [];
  const okCount = symbols.filter(s => s.status === 'ok').length;
  const noDataCount = symbols.filter(s => s.status === 'no_data').length;
  const errCount = symbols.filter(s => s.status === 'error').length;
  lines.push(`  ${BOLD}📊 交易对行情: ${okCount} 正常 / ${noDataCount} 等待 / ${errCount} 异常 (共 ${symbols.length})${RESET}`);

  // ---- 结论 ----
  lines.push('');
  lines.push(subDivider);
  const processHealthy = data.processes.every(p => p.status === 'online');
  const exchangeHealthy = data.exchange.status === 'connected';

  if (processHealthy && exchangeHealthy) {
    lines.push(`  ${BOLD}${CYAN}✅ 结论: 系统运行正常${RESET}`);
    if (total > 0) {
      const lastSignal = recent.length > 0 ? recent[0] : null;
      if (lastSignal) {
        lines.push(`    最新信号: ${lastSignal.type} @ ${lastSignal.symbol}${lastSignal.price ? ` ($${lastSignal.price})` : ''} — ${fmtTime(lastSignal.time)}`);
        lines.push(`    累计信号: ${total} 条`);
      }
    } else {
      lines.push(`    无新信号 — 系统等待策略触发`);
    }
  } else {
    lines.push(`  ${BOLD}⚠️ 结论: 系统存在异常${RESET}`);
    if (!processHealthy) lines.push(`    - 进程状态异常, 请检查 PM2`);
    if (!exchangeHealthy) lines.push(`    - 交易所连接异常, 请检查网络/API`);
  }
  lines.push(subDivider);
  lines.push('');

  return {
    success: true,
    data,
    report: lines.join('\n'),
    summary: {
      timestamp: now,
      totalSignals: total,
      prevTotal,
      newSignals: prevTotal !== undefined ? total - prevTotal : null,
      processesOnline: processHealthy,
      exchangeOnline: exchangeHealthy,
      systemHealthy: allHealthy,
      recentSignals: recent.slice(0, 10),
    },
    serverUrl: url,
  };
}

// ======== 状态文件管理 ========

function loadState() {
  try {
    if (existsSync(STATE_FILE)) {
      return JSON.parse(readFileSync(STATE_FILE, 'utf-8'));
    }
  } catch { /* ignore */ }
  return {};
}

function saveState(data) {
  try {
    writeFileSync(STATE_FILE, JSON.stringify(data, null, 2), 'utf-8');
  } catch (err) {
    console.error(`[状态保存] ⚠️ 写入失败: ${err.message}`);
  }
}

// ======== CLI 入口 ========

async function main() {
  const args = process.argv.slice(2);
  const urlArg = args.find(a => a.startsWith('--url='));
  const url = urlArg ? urlArg.split('=')[1] : API_URL;
  const jsonMode = args.includes('--json');
  const saveStateFlag = args.includes('--save-state');
  const notifyMode = args.includes('--notify');
  const webhookArg = args.find(a => a.startsWith('--webhook='));
  const webhookUrl = webhookArg ? webhookArg.split('=')[1] : null;

  // 从状态文件读取上次记录
  let prevTotal = undefined;
  if (saveStateFlag) {
    const state = loadState();
    prevTotal = state.totalSignals;
  }

  const result = await checkStatus({ url, prevTotal });

  if (!result.success) {
    console.error(`\n❌ ${result.error}\n`);
    process.exit(1);
  }

  const summary = result.summary;
  const newSignals = summary.newSignals;

  // 保存当前状态
  if (saveStateFlag && summary) {
    saveState({
      lastCheck: new Date().toISOString(),
      totalSignals: summary.totalSignals,
      processesOnline: summary.processesOnline,
      exchangeOnline: summary.exchangeOnline,
      systemHealthy: summary.systemHealthy,
    });
  }

  // ---- 通知模式：输出简洁通知行 ----
  if (notifyMode) {
    const hasNew = newSignals !== null && newSignals > 0;
    const procIcon = summary.processesOnline ? '✅' : '❌';
    const exchIcon = summary.exchangeOnline ? '✅' : '❌';
    const sysIcon = summary.systemHealthy ? '✅' : '⚠️';

    if (hasNew) {
      // 有新信号 — 详细通知
      const recent = summary.recentSignals || [];
      const recentStr = recent.slice(0, 3).map(r => `${r.type}/${r.symbol}`).join(' ');
      console.log(`[NOTIFY] 🔔 新信号 +${newSignals} 条 | 总计 ${summary.totalSignals} 条 | 最近: ${recentStr} | 进程 ${procIcon} 交易所 ${exchIcon} 系统 ${sysIcon}`);
    } else {
      console.log(`[NOTIFY] 📭 无新信号 | 总计 ${summary.totalSignals} 条 | 进程 ${procIcon} 交易所 ${exchIcon} 系统 ${sysIcon}`);
    }
  }

  // ---- Webhook 通知 ----
  if (webhookUrl && summary) {
    const payload = {
      timestamp: summary.timestamp,
      server: url,
      newSignals: newSignals,
      totalSignals: summary.totalSignals,
      processesOnline: summary.processesOnline,
      exchangeOnline: summary.exchangeOnline,
      systemHealthy: summary.systemHealthy,
      recentSignals: (summary.recentSignals || []).slice(0, 5),
    };
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000);
      const resp = await fetch(webhookUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
      if (!resp.ok) {
        console.error(`[Webhook] ⚠️ 发送失败: HTTP ${resp.status}`);
      }
    } catch (err) {
      console.error(`[Webhook] ⚠️ 发送失败: ${err.message}`);
    }
  }

  // ---- 输出 ----
  if (jsonMode) {
    console.log(JSON.stringify(result, null, 2));
  } else if (!notifyMode) {
    // 通知模式下已经输出过通知行；如果同时需要完整报告，加上 --verbose
    if (args.includes('--verbose')) {
      console.log(result.report);
    }
  } else {
    // notify 模式：只输出通知行 + 分隔线（便于日志阅读）
    console.log('─'.repeat(50));
  }
}

// 直接运行
if (typeof process !== 'undefined' && process.argv[1] && import.meta.url.endsWith(process.argv[1].replace(/^.*[\\/]/, ''))) {
  main().catch(err => {
    console.error(`\n❌ 脚本执行失败: ${err.message}\n`);
    process.exit(1);
  });
}
