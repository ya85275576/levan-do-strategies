#!/usr/bin/env node
/**
 * LE VAN DO® — OKX 交易机器人定时检查脚本
 *
 * 用法：
 *   node scripts/check-status.js                     # 默认：http://43.133.210.83:3000
 *   node scripts/check-status.js --url http://localhost:3000
 *   node scripts/check-status.js --json              # JSON 格式输出
 *   node scripts/check-status.js --save-state        # 自动保存状态到文件（增量对比）
 *   node scripts/check-status.js --notify            # 简洁通知行（适合推送）
 *   node scripts/check-status.js --webhook <url>     # POST 结果到外部服务
 *   node scripts/check-status.js --save-state --notify  # 组合使用
 *
 * 定时任务（每 15 分钟）：
 *   crontab -e
 *   每15分钟执行: cd /path/to/services && /usr/bin/node scripts/check-status.js --save-state --notify
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

const SIGNAL_LABELS = {
  longE:   '📗 多头开仓',
  shortE:  '📕 空头开仓',
  longX:   '📘 多头平仓',
  shortX:  '📙 空头平仓',
  longTP1: '🔵 多头止盈',
  shortTP1:'🟣 空头止盈',
  longSL:  '⛔ 多头止损',
  shortSL: '⛔ 空头止损',
};

function signalTypeLabel(type) {
  return SIGNAL_LABELS[type] || type;
}

const SIGNAL_COLORS = {
  longE:   '\x1b[32m',
  shortE:  '\x1b[31m',
  longX:   '\x1b[34m',
  shortX:  '\x1b[33m',
  longTP1: '\x1b[35m',
  shortTP1:'\x1b[36m',
  longSL:  '\x1b[91m',
  shortSL: '\x1b[93m',
};

function signalTypeColor(type) {
  return SIGNAL_COLORS[type] || '\x1b[37m';
}

/** 信号是否为主要交易信号（开/平仓，不含 TP/SL） */
function isTradeSignal(type) {
  return ['longE', 'shortE', 'longX', 'shortX'].includes(type);
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

  // 计算 TP/SL 信号数量（从 recent 中统计）
  const recent = sig.recent || [];
  const tpSlCounts = { longTP1: 0, shortTP1: 0, longSL: 0, shortSL: 0 };
  for (const r of recent) {
    if (tpSlCounts.hasOwnProperty(r.type)) tpSlCounts[r.type]++;
  }
  // 使用最近信号中出现的类型作为近似，也可以从 API 中获取更精确的数据

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

  // 交易信号（开/平仓）
  lines.push(`  ${BOLD}交易信号:${RESET}`);
  lines.push(`    📗 longE  (多头开仓):  ${String(counts.longE || 0).padStart(4)}`);
  lines.push(`    📕 shortE (空头开仓):  ${String(counts.shortE || 0).padStart(4)}`);
  lines.push(`    📘 longX  (多头平仓):  ${String(counts.longX || 0).padStart(4)}`);
  lines.push(`    📙 shortX (空头平仓):  ${String(counts.shortX || 0).padStart(4)}`);
  lines.push(`  ${BOLD}止盈/止损:${RESET}`);
  lines.push(`    🔵 longTP1 (多头止盈):  ${String(counts.longTP1 || tpSlCounts.longTP1 || 0).padStart(4)}`);
  lines.push(`    🟣 shortTP1(空头止盈):  ${String(counts.shortTP1 || tpSlCounts.shortTP1 || 0).padStart(4)}`);
  lines.push(`    ⛔ longSL  (多头止损):  ${String(counts.longSL || tpSlCounts.longSL || 0).padStart(4)}`);
  lines.push(`    ⛔ shortSL (空头止损):  ${String(counts.shortSL || tpSlCounts.shortSL || 0).padStart(4)}`);

  // 最近信号
  if (recent.length > 0) {
    lines.push('');
    lines.push(`  ${BOLD}最近信号 (最新 ${Math.min(recent.length, 10)} 条):${RESET}`);
    lines.push(`  ${GRAY}  ${'时间'.padEnd(19)} ${'类型'.padEnd(10)} ${'交易对'.padEnd(14)} 价格${RESET}`);
    lines.push(`  ${GRAY}  ${'─'.repeat(18)} ${'─'.repeat(8)} ${'─'.repeat(12)} ${'─'.repeat(10)}${RESET}`);

    const showCount = Math.min(recent.length, 10);
    for (let i = 0; i < showCount; i++) {
      const r = recent[i];
      const t = fmtTime(r.time);
      const label = signalTypeLabel(r.type);
      const sym = r.symbol.padEnd(14);
      const price = r.price ? `$${parseFloat(r.price).toFixed(2)}` : '—';
      const color = signalTypeColor(r.type);
      lines.push(`  ${t} ${color}${label}${RESET}  ${sym} ${price}`);
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
      signalCounts: counts,
      tpSlCounts: {
        longTP1: counts.longTP1 || tpSlCounts.longTP1 || 0,
        shortTP1: counts.shortTP1 || tpSlCounts.shortTP1 || 0,
        longSL: counts.longSL || tpSlCounts.longSL || 0,
        shortSL: counts.shortSL || tpSlCounts.shortSL || 0,
      },
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

/** 生成简洁通知行 */
function buildNotifyLine(result) {
  const s = result.summary;
  if (!s) return '❌ 状态检查失败';

  const parts = [];
  // 信号状态
  if (s.newSignals !== null) {
    if (s.newSignals > 0) {
      parts.push(`📊 +${s.newSignals} 新信号 (共${s.totalSignals})`);
      // 列出最近的主要交易信号
      const tradeSignals = (s.recentSignals || []).filter(r => isTradeSignal(r.type));
      if (tradeSignals.length > 0) {
        const last = tradeSignals[0];
        const label = SIGNAL_LABELS[last.type] || last.type;
        parts.push(`${label} ${last.symbol}`);
      }
    } else {
      parts.push('📊 无新信号');
    }
  } else {
    parts.push(`📊 共${s.totalSignals}信号`);
  }

  // 进程状态
  parts.push(s.processesOnline ? '✅ 进程正常' : '⚠️ 进程异常');

  // 系统健康
  parts.push(s.systemHealthy ? '🟢 系统健康' : '🔴 系统告警');

  return `[Bot状态] ${parts.join(' | ')}`;
}

/** POST 结果到外部 Webhook */
async function postToWebhook(webhookUrl, result) {
  const payload = {
    source: 'le-van-do-bot-check',
    timestamp: new Date().toISOString(),
    success: result.success,
    summary: result.summary,
    // 只包含最近的信号
    recentSignals: result.summary?.recentSignals?.slice(0, 5) || [],
  };

  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 8000);
    const resp = await fetch(webhookUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    clearTimeout(timeoutId);
    return { ok: resp.ok, status: resp.status };
  } catch (err) {
    return { ok: false, error: err.message };
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
  const webhookIndex = args.indexOf('--webhook');
  const webhookUrl = webhookIndex >= 0 && webhookIndex + 1 < args.length
    ? args[webhookIndex + 1]
    : null;

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

  // 保存当前状态
  if (saveStateFlag && result.summary) {
    saveState({
      lastCheck: new Date().toISOString(),
      totalSignals: result.summary.totalSignals,
      processesOnline: result.summary.processesOnline,
      exchangeOnline: result.summary.exchangeOnline,
      systemHealthy: result.summary.systemHealthy,
    });
  }

  // 简洁通知模式
  if (notifyMode) {
    console.log(buildNotifyLine(result));
    return;
  }

  // POST 到外部 Webhook
  if (webhookUrl) {
    const whResult = await postToWebhook(webhookUrl, result);
    if (whResult.ok) {
      console.log(`[Webhook] ✅ 已发送到 ${webhookUrl} (HTTP ${whResult.status})`);
    } else {
      console.error(`[Webhook] ❌ 发送失败: ${whResult.error || whResult.status}`);
    }
  }

  if (jsonMode) {
    console.log(JSON.stringify(result, null, 2));
  } else {
    console.log(result.report);
  }
}

// 直接运行
if (typeof process !== 'undefined' && process.argv[1] && import.meta.url.endsWith(process.argv[1].replace(/^.*[\\/]/, ''))) {
  main().catch(err => {
    console.error(`\n❌ 脚本执行失败: ${err.message}\n`);
    process.exit(1);
  });
}
