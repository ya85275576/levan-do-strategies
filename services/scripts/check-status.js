#!/usr/bin/env node
/**
 * LE VAN DO® — OKX 交易机器人定时检查脚本
 *
 * 用法：
 *   node scripts/check-status.js                         # 默认：http://43.133.210.83:3000
 *   node scripts/check-status.js --url http://localhost:3000
 *   node scripts/check-status.js --json                  # JSON 格式输出
 *   node scripts/check-status.js --save-state            # 自动保存状态到文件（增量对比）
 *   node scripts/check-status.js --notify                # 单行简洁通知
 *   node scripts/check-status.js --webhook               # 输出 Webhook JSON payload 到 stdout
 *   node scripts/check-status.js --webhook-url=URL       # POST 到外部 Webhook
 *   node scripts/check-status.js --full                  # 完整报告（--save-state + --notify 同时生效）
 *
 * 定时任务（每 15 分钟, cron: every 15 min）：
 *   crontab -e
 *   # 简洁通知模式：
 *     every 15 min  cd /path/to/services && node scripts/check-status.js --notify --save-state
 *   # Slack/Discord 通知：
 *     every 15 min  cd /path/to/services && node scripts/check-status.js --webhook-url=URL
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
const WEBHOOK_USER_AGENT = 'LE-VAN-DO-Bot-Check/1.0';

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

function fmtTimeShort(iso) {
  const d = new Date(iso);
  return d.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function fmtPct(v) {
  return v.toFixed(1) + '%';
}

function colorByPct(pct) {
  if (pct > 80) return '\u{1F534}';
  if (pct > 60) return '\u{1F7E1}';
  return '\u{1F7E2}';
}

function signalTypeLabel(type) {
  const labels = {
    longE:  '\u{1F4D7} 多头开仓',
    shortE: '\u{1F4D5} 空头开仓',
    longX:  '\u{1F4D8} 多头平仓',
    shortX: '\u{1F4D9} 空头平仓',
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
  lines.push(`${BOLD}\u{2554}${'\u{2550}'.repeat(58)}\u{2557}${RESET}`);
  lines.push(`${BOLD}\u{2551}   LE VAN DO\u00ae \u2014 OKX 交易机器人状态报告          \u{2551}${RESET}`);
  lines.push(`${BOLD}\u{255A}${'\u{2550}'.repeat(58)}\u{255D}${RESET}`);
  lines.push(`  检查时间: ${CYAN}${fmtTime(now)}${RESET}`);
  lines.push(`  服务器:   ${url}`);
  lines.push(`  运行时长: ${data.server.uptime}`);
  lines.push('');

  // ---- 1. 进程状态 ----
  lines.push(`${BOLD}\u{1F4CB} 一、进程状态${RESET}`);
  lines.push(divider);
  if (data.processes && data.processes.length > 0) {
    for (const p of data.processes) {
      const isOnline = p.status === 'online';
      const icon = isOnline ? '\u2705' : '\u274C';
      lines.push(`  ${icon} [${p.name}]`);
      lines.push(`     PID:     ${p.pid}`);
      lines.push(`     状态:    ${isOnline ? '\u{1F7E2} 运行中' : '\u{1F534} 已停止'}`);
      lines.push(`     运行:    ${p.uptime}`);
      lines.push(`     重启:    ${p.restartCount} 次`);
      lines.push(`     CPU:     ${p.cpu}%`);
      lines.push(`     内存:    ${fmtBytes(p.memory)}`);
    }
    const allOnline = data.processes.every(p => p.status === 'online');
    lines.push(`   \u2192 总体状态: ${allOnline ? '\u2705 所有进程正常运行' : '\u26A0\uFE0F 部分进程异常'}`);
  } else {
    lines.push(`  \u26A0\uFE0F 无 PM2 进程数据`);
  }
  lines.push('');

  // ---- 2. 交易所状态 ----
  lines.push(`${BOLD}\u{1F517} 二、交易所状态${RESET}`);
  lines.push(divider);
  const ex = data.exchange;
  lines.push(`  交易所:    ${ex.name} (${ex.exchangeType})`);
  lines.push(`  网络:      ${ex.network} ${ex.isTestnet ? '(测试网)' : '(实盘)'}`);
  lines.push(`  连接:      ${ex.status === 'connected' ? '\u2705 已连接' : '\u274C 已断开'}`);
  lines.push(`  模拟模式:  ${ex.dryRun ? '\u{1F7E2} 启用' : '\u{1F534} 关闭'}`);
  lines.push(`  账户:      ${ex.accountStatus}`);
  lines.push(`  订单类型:  ${ex.orderType}`);
  lines.push(`  杠杆:      ${ex.leverage}x`);
  lines.push(`  仓位模式:  ${ex.positionMode}`);
  lines.push('');

  // ---- 3. 信号统计 ----
  lines.push(`${BOLD}\u{1F4E1} 三、策略信号统计${RESET}`);
  lines.push(divider);
  const sig = data.signals;
  const counts = sig.counts || { longE: 0, shortE: 0, longX: 0, shortX: 0 };
  const total = sig.total || 0;

  // 与上次对比
  if (prevTotal !== undefined && prevTotal !== null) {
    const diff = total - prevTotal;
    if (diff > 0) {
      lines.push(`  \u{1F4CA} 累计信号: ${BOLD}${total}${RESET} 条 ${CYAN}(较上次 +${diff} 条 \u2014 有新信号！)${RESET}`);
    } else if (diff === 0) {
      lines.push(`  \u{1F4CA} 累计信号: ${total} 条 ${GRAY}(与上次相同 \u2014 无新信号)${RESET}`);
    } else {
      lines.push(`  \u{1F4CA} 累计信号: ${total} 条 ${GRAY}(上次记录为 ${prevTotal})${RESET}`);
    }
  } else {
    lines.push(`  \u{1F4CA} 累计信号: ${BOLD}${total}${RESET} 条`);
  }
  lines.push(`    \u{1F4D7} longE  (多头开仓):  ${String(counts.longE || 0).padStart(4)}`);
  lines.push(`    \u{1F4D5} shortE (空头开仓):  ${String(counts.shortE || 0).padStart(4)}`);
  lines.push(`    \u{1F4D8} longX  (多头平仓):  ${String(counts.longX || 0).padStart(4)}`);
  lines.push(`    \u{1F4D9} shortX (空头平仓):  ${String(counts.shortX || 0).padStart(4)}`);

  // 最近信号
  const recent = sig.recent || [];
  if (recent.length > 0) {
    lines.push('');
    lines.push(`  ${BOLD}最近信号 (最新 ${Math.min(recent.length, 10)} 条):${RESET}`);
    lines.push(`  ${GRAY}  ${'时间'.padEnd(19)} ${'类型'.padEnd(14)} ${'交易对'.padEnd(12)} 价格${RESET}`);
    lines.push(`  ${GRAY}  ${'\u2500'.repeat(18)} ${'\u2500'.repeat(12)} ${'\u2500'.repeat(10)} ${'\u2500'.repeat(10)}${RESET}`);

    const showCount = Math.min(recent.length, 10);
    for (let i = 0; i < showCount; i++) {
      const r = recent[i];
      const t = fmtTime(r.time);
      const st = r.type.padEnd(10);
      const sym = r.symbol.padEnd(10);
      const price = r.price ? `$${parseFloat(r.price).toFixed(2)}` : '\u2014';
      const color = signalTypeColor(r.type);
      lines.push(`  ${t} ${color}${r.type}${RESET}  ${sym} ${price}`);
    }
  } else {
    lines.push(`   \u2192 ${GRAY}暂无信号记录${RESET}`);
  }
  lines.push('');

  // ---- 4. 持仓信息 ----
  lines.push(`${BOLD}\u{1F4BC} 四、当前持仓${RESET}`);
  lines.push(divider);
  const positions = data.positions || [];
  if (positions.length > 0) {
    lines.push(`  ${GRAY}  ${'交易对'.padEnd(12)} ${'方向'.padEnd(8)} ${'数量'.padEnd(10)} 入场价格${RESET}`);
    lines.push(`  ${GRAY}  ${'\u2500'.repeat(10)} ${'\u2500'.repeat(6)} ${'\u2500'.repeat(8)} ${'\u2500'.repeat(10)}${RESET}`);
    for (const p of positions) {
      const dir = p.side === 'long' ? '\u{1F4C8} 多头' : '\u{1F4C9} 空头';
      const price = p.price ? `$${p.price.toFixed(2)}` : '待更新';
      lines.push(`  ${p.symbol.padEnd(12)} ${dir.padEnd(8)} ${String(p.size).padEnd(8)} ${price}`);
    }
  } else {
    lines.push(`  \u{1F4E6} 当前无持仓`);
  }
  lines.push('');

  // ---- 5. 系统资源 ----
  lines.push(`${BOLD}\u{1F5A5}\uFE0F 五、系统资源状况${RESET}`);
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
    lines.push(`  CPU:  ${cpu.cores} 核心 \u00b7 负载: 1m=${cpu.loadAvg[0]} / 5m=${cpu.loadAvg[1]} / 15m=${cpu.loadAvg[2]}`);

    // 总评
    memOk = mem.usagePercent < 80;
    diskOk = disk.usagePercent < 80;
    cpuOk = cpu.loadAvg[0] < cpu.cores * 0.8;
    allHealthy = memOk && diskOk && cpuOk;
    lines.push(`  \u2192 系统健康: ${allHealthy ? '\u2705 正常' : '\u26A0\uFE0F 需要关注'}`);
  } else {
    lines.push(`  系统资源信息不可用`);
  }
  lines.push('');

  // ---- 6. 交易对行情概要 ----
  const symbols = data.symbols || [];
  const okCount = symbols.filter(s => s.status === 'ok').length;
  const noDataCount = symbols.filter(s => s.status === 'no_data').length;
  const errCount = symbols.filter(s => s.status === 'error').length;
  lines.push(`  ${BOLD}\u{1F4CA} 交易对行情: ${okCount} 正常 / ${noDataCount} 等待 / ${errCount} 异常 (共 ${symbols.length})${RESET}`);

  // ---- 结论 ----
  lines.push('');
  lines.push(subDivider);
  const processHealthy = data.processes.every(p => p.status === 'online');
  const exchangeHealthy = data.exchange.status === 'connected';

  if (processHealthy && exchangeHealthy) {
    lines.push(`  ${BOLD}${CYAN}\u2705 结论: 系统运行正常${RESET}`);
    if (total > 0) {
      const lastSignal = recent.length > 0 ? recent[0] : null;
      if (lastSignal) {
        lines.push(`    最新信号: ${lastSignal.type} @ ${lastSignal.symbol}${lastSignal.price ? ` ($${lastSignal.price})` : ''} \u2014 ${fmtTime(lastSignal.time)}`);
        lines.push(`    累计信号: ${total} 条`);
      }
    } else {
      lines.push(`    无新信号 \u2014 系统等待策略触发`);
    }
  } else {
    lines.push(`  ${BOLD}\u26A0\uFE0F 结论: 系统存在异常${RESET}`);
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
    console.error(`[状态保存] \u26A0\uFE0F 写入失败: ${err.message}`);
  }
}

/**
 * 构建单行简洁通知
 */
function formatNotifyLine(result, hasNewSignals) {
  const { summary } = result;
  if (!summary) {
    return `\u274C LE VAN DO\u00ae Bot Check | 数据不可用`;
  }

  const processIcon = summary.processesOnline ? '\u2705' : '\u274C';
  const exchangeIcon = summary.exchangeOnline ? '\u{1F517}' : '\u{1F534}';
  const systemIcon = summary.systemHealthy ? '\u{1F5A5}\uFE0F' : '\u26A0\uFE0F';
  const signalIcon = hasNewSignals ? '\u{1F4E1}\u{1F195}' : '\u{1F4E1}';

  let signalStr;
  if (summary.newSignals !== null && summary.newSignals > 0) {
    signalStr = `\u{1F4CA} ${summary.totalSignals} (+${summary.newSignals})`;
  } else if (summary.newSignals !== null && summary.newSignals === 0) {
    signalStr = `\u{1F4CA} ${summary.totalSignals} 无新信号`;
  } else {
    signalStr = `\u{1F4CA} ${summary.totalSignals}`;
  }

  const processStr = summary.processesOnline ? '进程正常' : '进程异常';
  const exchangeStr = summary.exchangeOnline ? '交易所已连接' : '交易所断开';
  const systemStr = summary.systemHealthy ? '系统正常' : '系统告警';

  const timeStr = fmtTimeShort(summary.timestamp);

  return `\u{1F916} LE VAN DO\u00ae Bot Check | ${processIcon} ${processStr} | ${exchangeIcon} ${exchangeStr} | ${signalIcon} ${signalStr} | ${systemIcon} ${systemStr} | \u{1F550} ${timeStr}`;
}

/**
 * 构建 Webhook Payload（兼容 Slack / Discord / Telegram）
 */
function buildWebhookPayload(result, hasNewSignals) {
  const { data, summary, serverUrl } = result;
  const sig = data.signals || {};
  const counts = sig.counts || { longE: 0, shortE: 0, longX: 0, shortX: 0 };
  const total = sig.total || 0;
  const recent = sig.recent || [];
  const sys = data.system || {};
  const ex = data.exchange || {};

  const title = `\u{1F916} LE VAN DO\u00ae \u2014 OKX 交易机器人状态报告`;
  const timestamp = fmtTime(summary ? summary.timestamp : new Date().toISOString());

  // Slack / Discord 通用格式（带 attachments）
  const slackPayload = {
    text: title,
    attachments: [
      {
        color: hasNewSignals ? '#36a64f' : (summary && summary.newSignals === 0 ? '#cccccc' : '#ffcc00'),
        title: '\u{1F4CB} 概览',
        fields: [
          { title: '检查时间', value: timestamp, short: true },
          { title: '信号总数', value: String(total), short: true },
          { title: '进程状态', value: summary && summary.processesOnline ? '\u2705 正常' : '\u274C 异常', short: true },
          { title: '交易所连接', value: summary && summary.exchangeOnline ? '\u2705 已连接' : '\u274C 断开', short: true },
          { title: '系统健康', value: summary && summary.systemHealthy ? '\u2705 正常' : '\u26A0\uFE0F 告警', short: true },
        ],
        footer: `服务器: ${serverUrl}`,
        ts: Math.floor(Date.now() / 1000),
      },
    ],
  };

  // 如果有新信号，添加信号详情 attachment
  if (hasNewSignals && recent.length > 0) {
    const fields = recent.slice(0, 10).map(r => ({
      title: `${r.type} @ ${r.symbol}`,
      value: `价格: ${r.price ? `$${r.price}` : '市价'} | 时间: ${fmtTimeShort(r.time)}`,
      short: true,
    }));
    slackPayload.attachments.push({
      color: '#36a64f',
      title: `\u{1F195} 新增信号 (${summary.newSignals} 条)`,
      fields,
      ts: Math.floor(Date.now() / 1000),
    });
  }

  // 添加系统资源 attachment
  slackPayload.attachments.push({
    color: summary && summary.systemHealthy ? '#36a64f' : '#ffcc00',
    title: '\u{1F5A5}\uFE0F 系统资源',
    fields: [
      { title: '内存', value: sys.memory ? `${sys.memory.usagePercent.toFixed(1)}% (${fmtBytes(sys.memory.used)}/${fmtBytes(sys.memory.total)})` : 'N/A', short: true },
      { title: '磁盘', value: sys.disk ? `${sys.disk.usagePercent.toFixed(1)}% (${fmtBytes(sys.disk.used)}/${fmtBytes(sys.disk.total)})` : 'N/A', short: true },
      { title: 'CPU 负载', value: sys.cpu ? `${sys.cpu.loadAvg[0]} / ${sys.cpu.loadAvg[1]} / ${sys.cpu.loadAvg[2]}` : 'N/A', short: true },
    ],
    ts: Math.floor(Date.now() / 1000),
  });

  return slackPayload;
}

/**
 * POST Webhook 到外部 URL
 */
async function postWebhook(url, payload) {
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 10000);
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'User-Agent': WEBHOOK_USER_AGENT,
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (!response.ok) {
      console.error(`[Webhook] HTTP ${response.status}: ${response.statusText}`);
      return false;
    }
    return true;
  } catch (err) {
    console.error(`[Webhook] 发送失败: ${err.message}`);
    return false;
  }
}

// ======== CLI 入口 ========

async function main() {
  const args = process.argv.slice(2);
  const urlArg = args.find(a => a.startsWith('--url='));
  const url = urlArg ? urlArg.split('=')[1] : API_URL;
  const jsonMode = args.includes('--json');
  const saveStateFlag = args.includes('--save-state') || args.includes('--full');
  const notifyMode = args.includes('--notify') || args.includes('--full');
  const webhookMode = args.includes('--webhook');
  const webhookUrlArg = args.find(a => a.startsWith('--webhook-url='));
  const webhookUrl = webhookUrlArg ? webhookUrlArg.split('=')[1] : null;

  // 从状态文件读取上次记录
  let prevTotal = undefined;
  if (saveStateFlag) {
    const state = loadState();
    prevTotal = state.totalSignals;
  }

  const result = await checkStatus({ url, prevTotal });

  if (!result.success) {
    const errMsg = `\n\u274C ${result.error}\n`;
    console.error(errMsg);
    if (webhookUrl) {
      await postWebhook(webhookUrl, {
        text: `\u274C LE VAN DO\u00ae Bot 检查失败: ${result.error}`,
        attachments: [{ color: 'danger', text: `服务器: ${url}\n时间: ${new Date().toISOString()}` }],
      });
    }
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

  // 检查是否有新信号
  const hasNewSignals = result.summary && result.summary.newSignals !== null && result.summary.newSignals > 0;

  if (jsonMode) {
    // JSON 模式：输出完整 JSON
    console.log(JSON.stringify(result, null, 2));
  } else if (webhookUrl) {
    // Webhook URL 模式：构建并 POST
    const payload = buildWebhookPayload(result, hasNewSignals);
    console.log(`[Webhook] 发送到 ${webhookUrl} ...`);
    const whResult = await postWebhook(webhookUrl, payload);
    console.log(`[Webhook] ${whResult ? '\u2705 发送成功' : '\u274C 发送失败'}`);
    // 也输出简要文本
    console.log(formatNotifyLine(result, hasNewSignals));
  } else if (webhookMode) {
    // Webhook stdout 模式：输出 JSON payload 到 stdout
    const payload = buildWebhookPayload(result, hasNewSignals);
    console.log(JSON.stringify(payload, null, 2));
  } else if (notifyMode) {
    // 简洁通知模式：单行输出
    console.log(formatNotifyLine(result, hasNewSignals));
  } else {
    // 默认：完整报告
    console.log(result.report);
  }
}

// 直接运行
if (typeof process !== 'undefined' && process.argv[1] && import.meta.url.endsWith(process.argv[1].replace(/^.*[\\/]/, ''))) {
  main().catch(err => {
    console.error(`\n\u274C 脚本执行失败: ${err.message}\n`);
    process.exit(1);
  });
}
