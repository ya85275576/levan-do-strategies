#!/usr/bin/env node
/**
 * LE VAN DO® — OKX 交易机器人定时检查脚本
 *
 * 用法：
 *   node scripts/check-status.js                              # 详细报告
 *   node scripts/check-status.js --url http://localhost:3000  # 自定义 URL
 *   node scripts/check-status.js --json                       # JSON 格式输出
 *   node scripts/check-status.js --save-state                 # 自动保存状态到文件（增量检测）
 *   node scripts/check-status.js --notify                     # 简洁通知模式（无新信号时报告"无新信号"）
 *   node scripts/check-status.js --webhook                    # 输出 Webhook JSON 到 stdout（Slack/Discord 兼容）
 *   node scripts/check-status.js --webhook-url=https://...    # POST Webhook JSON 到外部 URL
 *   node scripts/check-status.js --full                       # save-state + notify 完整模式
 *
 * 定时任务（每 15-30 分钟）：
 *   crontab -e
 *   每15分钟执行: cd /path/to/services && /usr/bin/node scripts/check-status.js --full
 *
 * 也可作为模块导入：
 *   import { checkStatus, loadCheckState, saveCheckState } from './check-status.js';
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
    longE:   '📗 多头开仓',
    shortE:  '📕 空头开仓',
    longX:   '📘 多头平仓',
    shortX:  '📙 空头平仓',
    longTP1: '🎯 多头TP1',
    longTP2: '🎯 多头TP2',
    longTP3: '🎯 多头TP3',
    shortTP1:'🎯 空头TP1',
    shortTP2:'🎯 空头TP2',
    shortTP3:'🎯 空头TP3',
    longSL:  '🛑 多头止损',
    shortSL: '🛑 空头止损',
  };
  return labels[type] || type;
}

/** 判断是否为核心开仓/平仓信号（longE/shortE/longX/shortX） */
function isTradeSignal(type) {
  return ['longE', 'shortE', 'longX', 'shortX'].includes(type);
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
const GREEN = '\x1b[32m';
const RED = '\x1b[31m';
const YELLOW = '\x1b[33m';

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

  // ---- 解析关键数据 ----
  const sig = data.signals || {};
  const counts = sig.counts || { longE: 0, shortE: 0, longX: 0, shortX: 0 };
  const total = sig.total || 0;
  const recent = sig.recent || [];
  const processHealthy = data.processes ? data.processes.every(p => p.status === 'online') : false;
  const exchangeHealthy = data.exchange ? data.exchange.status === 'connected' : false;
  const sys = data.system;
  let allHealthy = true;
  if (sys) {
    const memOk = sys.memory ? sys.memory.usagePercent < 80 : true;
    const diskOk = sys.disk ? sys.disk.usagePercent < 80 : true;
    const cpuOk = sys.cpu ? sys.cpu.loadAvg[0] < sys.cpu.cores * 0.8 : true;
    allHealthy = memOk && diskOk && cpuOk;
  }

  const diff = (prevTotal !== undefined && prevTotal !== null) ? total - prevTotal : null;

  // 找出新增的交易信号（longE/shortE/longX/shortX）——从 recent 中筛选
  const newTradeSignals = [];
  // 只取最近的信号（最多50条）来匹配新增
  const recentTradeSignals = recent.filter(r => isTradeSignal(r.type));

  const now = new Date().toISOString();

  // 构建 summary
  const summary = {
    timestamp: now,
    totalSignals: total,
    prevTotal,
    newSignals: diff,
    hasNewSignals: diff !== null && diff > 0,
    processesOnline: processHealthy,
    exchangeOnline: exchangeHealthy,
    systemHealthy: allHealthy,
    tradeSignalCounts: { ...counts },
    recentSignals: recent.slice(0, 10),
    recentTradeSignals: recentTradeSignals.slice(0, 10),
  };

  // ======== 完整报告 ========
  const lines = [];
  const divider = '='.repeat(60);
  const subDivider = '-'.repeat(60);

  lines.push('');
  lines.push(`${BOLD}╔${'═'.repeat(58)}╗${RESET}`);
  lines.push(`${BOLD}║   LE VAN DO® — OKX 交易机器人状态报告          ║${RESET}`);
  lines.push(`${BOLD}╚${'═'.repeat(58)}╝${RESET}`);
  lines.push(`  检查时间: ${CYAN}${fmtTime(now)}${RESET}`);
  lines.push(`  服务器:   ${url}`);
  lines.push(`  运行时长: ${data.server ? data.server.uptime : '未知'}`);
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
    lines.push(`   → 总体状态: ${processHealthy ? '✅ 所有进程正常运行' : '⚠️ 部分进程异常'}`);
  } else {
    lines.push(`  ⚠️ 无 PM2 进程数据`);
  }
  lines.push('');

  // ---- 2. 交易所状态 ----
  lines.push(`${BOLD}🔗 二、交易所状态${RESET}`);
  lines.push(divider);
  const ex = data.exchange || {};
  lines.push(`  交易所:    ${ex.name || '未知'} (${ex.exchangeType || '?'})`);
  lines.push(`  网络:      ${ex.network || '?'} ${ex.isTestnet ? '(测试网)' : '(实盘)'}`);
  lines.push(`  连接:      ${ex.status === 'connected' ? '✅ 已连接' : '❌ 已断开'}`);
  lines.push(`  模拟模式:  ${ex.dryRun ? '🟢 启用' : '🔴 关闭'}`);
  lines.push(`  账户:      ${ex.accountStatus || '未知'}`);
  lines.push(`  订单类型:  ${ex.orderType || '?'}`);
  lines.push(`  杠杆:      ${ex.leverage || '?'}x`);
  lines.push(`  仓位模式:  ${ex.positionMode || '?'}`);
  lines.push('');

  // ---- 3. 信号统计 ----
  lines.push(`${BOLD}📡 三、策略信号统计${RESET}`);
  lines.push(divider);

  // 与上次对比
  if (diff !== null) {
    if (diff > 0) {
      lines.push(`  📊 累计信号: ${BOLD}${total}${RESET} 条 ${CYAN}(较上次 +${diff} 条 🆕)${RESET}`);
    } else if (diff === 0) {
      lines.push(`  📊 累计信号: ${total} 条 ${GRAY}(与上次相同 — 无新信号)${RESET}`);
    } else {
      lines.push(`  📊 累计信号: ${total} 条 ${GRAY}(上次记录为 ${prevTotal})${RESET}`);
    }
  } else {
    lines.push(`  📊 累计信号: ${BOLD}${total}${RESET} 条`);
  }

  // 有新增信号时高亮核心信号
  const tradeTotal = (counts.longE || 0) + (counts.shortE || 0) + (counts.longX || 0) + (counts.shortX || 0);
  const tpSlTotal = total - tradeTotal;

  if (diff !== null && diff > 0) {
    lines.push(`  ${YELLOW}  ⚡ 新增 ${diff} 条信号${RESET}`);
  }
  lines.push(`    📗 longE  (多头开仓):  ${String(counts.longE || 0).padStart(4)}`);
  lines.push(`    📕 shortE (空头开仓):  ${String(counts.shortE || 0).padStart(4)}`);
  lines.push(`    📘 longX  (多头平仓):  ${String(counts.longX || 0).padStart(4)}`);
  lines.push(`    📙 shortX (空头平仓):  ${String(counts.shortX || 0).padStart(4)}`);
  lines.push(`    ${GRAY}   交易信号合计: ${tradeTotal} 条 / TP&SL: ${tpSlTotal} 条${RESET}`);

  // 最新交易信号（longE/shortE/longX/shortX）
  if (recentTradeSignals.length > 0) {
    lines.push('');
    lines.push(`  ${BOLD}📌 最近交易信号 (longE/shortE/longX/shortX) — ${recentTradeSignals.length} 条:${RESET}`);
    lines.push(`  ${GRAY}  ${'时间'.padEnd(19)} ${'类型'.padEnd(14)} ${'交易对'.padEnd(14)} 价格${RESET}`);
    lines.push(`  ${GRAY}  ${'─'.repeat(18)} ${'─'.repeat(12)} ${'─'.repeat(12)} ${'─'.repeat(10)}${RESET}`);
    for (const r of recentTradeSignals.slice(0, 10)) {
      const t = fmtTime(r.time);
      const sym = (r.symbol || '').padEnd(12);
      const price = r.price ? `$${parseFloat(r.price).toFixed(2)}` : '—';
      const color = signalTypeColor(r.type);
      lines.push(`  ${t} ${color}${r.type.padEnd(10)}${RESET}  ${sym} ${price}`);
    }
  } else {
    lines.push(`   → ${GRAY}暂无交易信号记录${RESET}`);
  }

  // 最近所有信号
  if (recent.length > 0) {
    lines.push('');
    lines.push(`  ${BOLD}最近全部信号 (最新 ${Math.min(recent.length, 10)} 条):${RESET}`);
    lines.push(`  ${GRAY}  ${'时间'.padEnd(19)} ${'类型'.padEnd(14)} ${'交易对'.padEnd(12)} 价格${RESET}`);
    lines.push(`  ${GRAY}  ${'─'.repeat(18)} ${'─'.repeat(12)} ${'─'.repeat(10)} ${'─'.repeat(10)}${RESET}`);
    for (let i = 0; i < Math.min(recent.length, 10); i++) {
      const r = recent[i];
      const t = fmtTime(r.time);
      const sym = (r.symbol || '').padEnd(10);
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
  let memOk = true, diskOk = true, cpuOk = true;
  if (sys) {
    const mem = sys.memory;
    const disk = sys.disk;
    const cpu = sys.cpu;

    if (mem) {
      const memColor = colorByPct(mem.usagePercent);
      lines.push(`  内存: ${memColor} ${fmtPct(mem.usagePercent)}`);
      lines.push(`       已用 ${fmtBytes(mem.used)} / 总计 ${fmtBytes(mem.total)}`);
      memOk = mem.usagePercent < 80;
    }
    if (disk) {
      const diskColor = colorByPct(disk.usagePercent);
      lines.push(`  磁盘: ${diskColor} ${fmtPct(disk.usagePercent)}`);
      lines.push(`       已用 ${fmtBytes(disk.used)} / 总计 ${fmtBytes(disk.total)}`);
      diskOk = disk.usagePercent < 80;
    }
    if (cpu) {
      lines.push(`  CPU:  ${cpu.cores} 核心 · 负载: 1m=${cpu.loadAvg[0]} / 5m=${cpu.loadAvg[1]} / 15m=${cpu.loadAvg[2]}`);
      cpuOk = cpu.loadAvg[0] < cpu.cores * 0.8;
    }
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
  if (processHealthy && exchangeHealthy) {
    // 有新增信号？
    if (diff !== null && diff > 0) {
      lines.push(`  ${BOLD}${GREEN}✅ 结论: 系统运行正常 — 检测到 ${diff} 条新信号${RESET}`);
    } else if (diff !== null && diff === 0) {
      lines.push(`  ${BOLD}${CYAN}✅ 结论: 系统运行正常 — 无新信号${RESET}`);
    } else {
      lines.push(`  ${BOLD}${CYAN}✅ 结论: 系统运行正常${RESET}`);
    }
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

  // 更新 summary
  summary.systemHealthy = allHealthy;

  return {
    success: true,
    data,
    report: lines.join('\n'),
    summary,
    serverUrl: url,
  };
}

// ======== 状态文件管理 ========

export function loadCheckState() {
  try {
    if (existsSync(STATE_FILE)) {
      return JSON.parse(readFileSync(STATE_FILE, 'utf-8'));
    }
  } catch { /* ignore */ }
  return {};
}

export function saveCheckState(data) {
  try {
    writeFileSync(STATE_FILE, JSON.stringify(data, null, 2), 'utf-8');
  } catch (err) {
    console.error(`[状态保存] ⚠️ 写入失败: ${err.message}`);
  }
}

// ======== 通知/Webhook 模式 ========

/**
 * 生成单行简洁通知（适合推送/Telegram）
 */
function buildNotifyMessage(result) {
  const s = result.summary;
  const lines = [];

  lines.push(`🤖 LE VAN DO® 信号报告 [${fmtTime(s.timestamp)}]`);

  // 进程健康
  const processOk = s.processesOnline;
  const exchangeOk = s.exchangeOnline;
  const systemOk = s.systemHealthy;

  if (!processOk || !exchangeOk) {
    lines.push(`⚠️ 异常: ${!processOk ? '进程异常' : ''}${!processOk && !exchangeOk ? ' / ' : ''}${!exchangeOk ? '交易所断开' : ''}`);
    return lines.join(' | ');
  }

  // 信号增量
  if (s.newSignals !== null && s.newSignals > 0) {
    lines.push(`🆕 新增 ${s.newSignals} 条信号`);
  } else {
    lines.push(`📭 无新信号`);
  }

  // 累计交易信号
  const c = s.tradeSignalCounts || {};
  const tradeTotal = (c.longE || 0) + (c.shortE || 0) + (c.longX || 0) + (c.shortX || 0);
  lines.push(`累计 ${tradeTotal} 笔交易`);

  // 最近交易信号
  const recentTrade = s.recentTradeSignals || [];
  if (recentTrade.length > 0 && s.newSignals !== null && s.newSignals > 0) {
    const top = recentTrade.slice(0, 3);
    for (const r of top) {
      const label = signalTypeLabel(r.type);
      const price = r.price ? `$${parseFloat(r.price).toFixed(2)}` : '';
      lines.push(`${label} ${r.symbol} ${price}`);
    }
  }

  // 系统
  lines.push(`🖥️ ${systemOk ? '正常' : '需关注'}`);

  return lines.join(' | ');
}

/**
 * 生成 Webhook JSON payload（Slack/Discord 兼容）
 */
function buildWebhookPayload(result) {
  const s = result.summary;
  const c = s.tradeSignalCounts || {};
  const allOk = s.processesOnline && s.exchangeOnline && s.systemHealthy;

  // 构建 Slack 格式的附件
  const fields = [];

  fields.push({ title: '信号总计', value: `${s.totalSignals} 条`, short: true });

  if (s.newSignals !== null) {
    fields.push({
      title: '新增信号',
      value: s.newSignals > 0 ? `+${s.newSignals} 🆕` : '0 (无新信号)',
      short: true,
    });
  }

  fields.push({ title: '多头开仓 (longE)', value: String(c.longE || 0), short: true });
  fields.push({ title: '空头开仓 (shortE)', value: String(c.shortE || 0), short: true });
  fields.push({ title: '多头平仓 (longX)', value: String(c.longX || 0), short: true });
  fields.push({ title: '空头平仓 (shortX)', value: String(c.shortX || 0), short: true });

  // 最近交易信号
  const recentTrade = s.recentTradeSignals || [];
  if (recentTrade.length > 0) {
    const tradeLines = recentTrade.slice(0, 5).map(r => {
      const label = signalTypeLabel(r.type);
      const price = r.price ? `$${parseFloat(r.price).toFixed(2)}` : '—';
      return `${label} ${r.symbol} @ ${price}`;
    });
    fields.push({
      title: `最近交易信号 (${recentTrade.length})`,
      value: tradeLines.join('\n') || '无',
      short: false,
    });
  }

  const color = allOk ? (s.newSignals !== null && s.newSignals > 0 ? '#36a64f' : '#cccccc') : '#ff0000';

  const payload = {
    username: 'LE VAN DO® Bot',
    icon_emoji: ':robot_face:',
    attachments: [{
      color,
      title: `LE VAN DO® — OKX 交易机器人状态`,
      title_link: result.serverUrl,
      fields,
      footer: `检查时间: ${fmtTime(s.timestamp)}`,
      ts: Math.floor(new Date(s.timestamp).getTime() / 1000),
    }],
  };

  return payload;
}

/**
 * POST Webhook JSON 到外部 URL
 */
async function postWebhook(url, payload) {
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 8000);
    const resp = await fetch(url, {
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
  const webhookMode = args.includes('--webhook');
  const webhookUrlArg = args.find(a => a.startsWith('--webhook-url='));
  const webhookUrl = webhookUrlArg ? webhookUrlArg.split('=')[1] : null;
  const fullMode = args.includes('--full');

  // 从状态文件读取上次记录
  let prevTotal = undefined;
  let prevState = {};
  if (saveStateFlag || fullMode) {
    prevState = loadCheckState();
    prevTotal = prevState.totalSignals;
  }

  const result = await checkStatus({ url, prevTotal });

  if (!result.success) {
    console.error(`\n❌ ${result.error}\n`);
    process.exit(1);
  }

  // 保存当前状态（save-state 或 full 模式）
  if ((saveStateFlag || fullMode) && result.summary) {
    saveCheckState({
      lastCheck: new Date().toISOString(),
      totalSignals: result.summary.totalSignals,
      processesOnline: result.summary.processesOnline,
      exchangeOnline: result.summary.exchangeOnline,
      systemHealthy: result.summary.systemHealthy,
    });
  }

  // 通知模式（简洁单行输出）
  if (notifyMode || fullMode) {
    const msg = buildNotifyMessage(result);
    console.log(msg);
    // 如果 fullMode 已有新信号，同时输出详细报告
    if (fullMode) {
      const hasNew = result.summary.newSignals !== null && result.summary.newSignals > 0;
      if (hasNew) {
        console.log('');
        console.log(result.report);
      }
    }
  }

  // Webhook JSON 模式
  if (webhookMode) {
    const payload = buildWebhookPayload(result);
    if (webhookUrl) {
      // POST 到外部 URL
      const wResp = await postWebhook(webhookUrl, payload);
      if (wResp.ok) {
        console.log(`[Webhook] ✅ 已发送到 ${webhookUrl} (HTTP ${wResp.status})`);
      } else {
        console.error(`[Webhook] ❌ 发送失败: ${wResp.error || `HTTP ${wResp.status}`}`);
        process.exit(1);
      }
    } else {
      // 输出 JSON 到 stdout
      console.log(JSON.stringify(payload, null, 2));
    }
  }

  // 默认或 --json 模式
  if (!notifyMode && !fullMode && !webhookMode) {
    if (jsonMode) {
      console.log(JSON.stringify(result, null, 2));
    } else {
      console.log(result.report);
    }
  }
}

// 直接运行
if (typeof process !== 'undefined' && process.argv[1] && import.meta.url.endsWith(process.argv[1].replace(/^.*[\\/]/, ''))) {
  main().catch(err => {
    console.error(`\n❌ 脚本执行失败: ${err.message}\n`);
    process.exit(1);
  });
}
