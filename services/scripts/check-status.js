#!/usr/bin/env node
/**
 * LE VAN DO® — OKX 交易机器人定时检查脚本
 *
 * 用法：
 *   node scripts/check-status.js                       # 默认：http://43.133.210.83:3000
 *   node scripts/check-status.js --url http://localhost:3000
 *   node scripts/check-status.js --json                # JSON 格式输出
 *   node scripts/check-status.js --save-state          # 自动保存状态到文件
 *   node scripts/check-status.js --notify              # 单行简洁通知（适合推送）
 *   node scripts/check-status.js --webhook             # 输出 Slack/Discord 兼容 JSON payload
 *   node scripts/check-status.js --webhook-url=URL     # POST 报告到外部 Webhook URL
 *   node scripts/check-status.js --full                # 同时保存状态并输出通知（cron 推荐）
 *
 * 定时任务（每 15 分钟，推荐）：
 *   crontab -e
 *   每15分钟执行: cd /path/to/services && /usr/bin/node scripts/check-status.js --full >> /var/log/bot-check.log 2>&1
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
        // 检查是否有增量
        if (prevTotal !== undefined) {
          const diff = total - prevTotal;
          if (diff > 0) {
            lines.push(`    📢 自上次检查以来有 ${BOLD}${diff}${RESET} 条新信号`);
          } else {
            lines.push(`    ℹ️ 无新信号 — 累计 ${total} 条`);
          }
        }
        lines.push(`    最新信号: ${lastSignal.type} @ ${lastSignal.symbol}${lastSignal.price ? ` ($${lastSignal.price})` : ''} — ${fmtTime(lastSignal.time)}`);
        lines.push(`    累计信号: ${total} 条`);
      }
    } else {
      lines.push(`    ℹ️ 无任何信号记录 — 系统等待策略触发`);
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

// ======== 通知生成 ========

/**
 * 生成单行简洁通知（适合推送/通知栏）
 */
function generateNotifyLine(result) {
  const s = result.summary;
  if (!result.success) {
    return `❌ LE VAN DO® 检查失败: ${result.error}`;
  }

  const newSignals = s.newSignals;
  const parts = [];

  // 信号状态
  if (newSignals !== null && newSignals > 0) {
    parts.push(`📡 +${newSignals} 新信号 (共 ${s.totalSignals})`);
    // 附加最近信号摘要
    const recent = s.recentSignals || [];
    if (recent.length > 0) {
      const latest = recent[0];
      const signalLabels = { longE: '📗多开', shortE: '📕空开', longX: '📘多平', shortX: '📙空平' };
      const label = signalLabels[latest.type] || latest.type;
      parts.push(`${label} ${latest.symbol}`);
    }
  } else if (newSignals !== null && newSignals === 0) {
    parts.push(`📡 无新信号 (共 ${s.totalSignals})`);
  } else {
    parts.push(`📡 信号共 ${s.totalSignals}`);
  }

  // 进程状态
  parts.push(s.processesOnline ? '✅ 进程正常' : '⚠️ 进程异常');

  // 交易所
  parts.push(s.exchangeOnline ? '🔗 已连接' : '🔗 已断开');

  // 系统
  parts.push(s.systemHealthy ? '🖥️ 系统正常' : '⚠️ 系统高负载');

  return `LE VAN DO® | ${parts.join(' · ')}`;
}

/**
 * 生成 Slack/Discord 兼容的 Webhook JSON payload
 */
function generateWebhookPayload(result) {
  const s = result.summary;
  const data = result.data;

  if (!result.success) {
    return {
      text: `❌ LE VAN DO® 检查失败`,
      attachments: [{
        color: 'danger',
        title: '连接错误',
        text: result.error,
        ts: Math.floor(Date.now() / 1000),
      }],
    };
  }

  const newSignals = s.newSignals;
  let color = 'good'; // green
  let title = '✅ 系统运行正常';
  let fields = [];

  // 信号
  const counts = data.signals.counts || {};
  const signalInfo = `📗 ${counts.longE || 0} · 📕 ${counts.shortE || 0} · 📘 ${counts.longX || 0} · 📙 ${counts.shortX || 0}`;

  if (newSignals !== null && newSignals > 0) {
    color = '#3fb950';
    title = `📡 +${newSignals} 新信号 (共 ${s.totalSignals})`;
  } else if (newSignals !== null && newSignals === 0) {
    title = `📡 无新信号 (共 ${s.totalSignals})`;
  }

  if (!s.processesOnline || !s.exchangeOnline) {
    color = 'danger';
    title = '⚠️ 系统异常';
  } else if (!s.systemHealthy) {
    color = 'warning';
    title = '⚠️ 系统负载较高';
  }

  fields.push({
    title: '信号统计',
    value: `累计: **${s.totalSignals}** 条\n${signalInfo}`,
    short: true,
  });

  fields.push({
    title: '进程状态',
    value: s.processesOnline ? '✅ 所有进程正常运行' : '❌ 部分进程异常',
    short: true,
  });

  fields.push({
    title: '交易所',
    value: s.exchangeOnline ? '✅ 已连接' : '❌ 已断开',
    short: true,
  });

  fields.push({
    title: '系统健康',
    value: s.systemHealthy ? '✅ 正常' : '⚠️ 需要关注',
    short: true,
  });

  // 最近信号
  const recent = s.recentSignals || [];
  if (recent.length > 0) {
    const signalLabels = { longE: '📗', shortE: '📕', longX: '📘', shortX: '📙' };
    const recentText = recent.slice(0, 5).map(r =>
      `${signalLabels[r.type] || r.type} ${r.symbol}${r.price ? ` $${r.price}` : ''}`
    ).join('\n');
    fields.push({
      title: `最近信号 (最新 ${Math.min(recent.length, 5)} 条)`,
      value: recentText || '—',
      short: false,
    });
  }

  // 系统资源
  if (data.system) {
    const sys = data.system;
    const memPct = sys.memory.usagePercent;
    const diskPct = sys.disk.usagePercent;
    const cpuLoad = sys.cpu.loadAvg[0];
    fields.push({
      title: '系统资源',
      value: `内存: ${memPct}% · 磁盘: ${diskPct}% · CPU 负载: ${cpuLoad}`,
      short: false,
    });
  }

  return {
    text: `LE VAN DO® 状态报告 — ${title}`,
    attachments: [{
      color,
      title,
      fields,
      footer: 'LE VAN DO® Bot Check',
      ts: Math.floor(Date.now() / 1000),
    }],
  };
}

/**
 * 发送 Webhook POST 请求到指定 URL
 */
async function postWebhook(url, payload) {
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5000);
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    clearTimeout(timeoutId);
    return { success: resp.ok, status: resp.status };
  } catch (err) {
    return { success: false, error: err.message };
  }
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
  const webhookMode = args.includes('--webhook');
  const webhookUrlArg = args.find(a => a.startsWith('--webhook-url='));
  const webhookUrl = webhookUrlArg ? webhookUrlArg.split('=')[1] : null;
  const fullMode = args.includes('--full');

  // --full 模式 = --save-state + --notify
  const effectiveSaveState = saveStateFlag || fullMode;
  const effectiveNotify = notifyMode || fullMode;
  const hasWebhookOutput = webhookMode || !!webhookUrl;

  // 从状态文件读取上次记录
  let prevTotal = undefined;
  if (effectiveSaveState) {
    const state = loadState();
    prevTotal = state.totalSignals;
  }

  const result = await checkStatus({ url, prevTotal });

  if (!result.success) {
    if (hasWebhookOutput && webhookUrl) {
      const payload = generateWebhookPayload(result);
      const wr = await postWebhook(webhookUrl, payload);
      if (wr.success) {
        console.error(`[Webhook] ✅ 错误通知已发送 (HTTP ${wr.status})`);
      } else {
        console.error(`[Webhook] ❌ 发送失败: ${wr.error}`);
      }
    }
    console.error(`\n❌ ${result.error}\n`);
    process.exit(1);
  }

  // 保存当前状态
  if (effectiveSaveState && result.summary) {
    saveState({
      lastCheck: new Date().toISOString(),
      totalSignals: result.summary.totalSignals,
      processesOnline: result.summary.processesOnline,
      exchangeOnline: result.summary.exchangeOnline,
      systemHealthy: result.summary.systemHealthy,
    });
  }

  // ---- 输出模式 ----

  if (jsonMode) {
    // 标准 JSON 输出
    console.log(JSON.stringify(result, null, 2));
  } else if (effectiveNotify) {
    // 通知模式：输出单行简洁通知
    const notifyLine = generateNotifyLine(result);
    console.log(notifyLine);

    // 如果有新信号，附加最近信号详情
    const newSignals = result.summary?.newSignals;
    if (newSignals !== null && newSignals > 0) {
      const recent = result.summary?.recentSignals || [];
      if (recent.length > 0) {
        const signalLabels = { longE: '📗 多头开仓', shortE: '📕 空头开仓', longX: '📘 多头平仓', shortX: '📙 空头平仓' };
        for (const r of recent) {
          const label = signalLabels[r.type] || r.type;
          const price = r.price ? ` @ $${r.price}` : '';
          console.log(`  ${label} ${r.symbol}${price} — ${fmtTime(r.time)}`);
        }
      }
    }

    // 如果系统异常，附上完整报告
    if (!result.summary?.processesOnline || !result.summary?.exchangeOnline || !result.summary?.systemHealthy) {
      console.log('');
      console.log('⚠️ 系统存在异常，详细报告：');
      console.log(result.report);
    }

    // 无新信号时明确报告
    if (newSignals !== null && newSignals === 0 && result.summary?.totalSignals === 0) {
      console.log('  ℹ️ 无任何信号记录 — 系统等待策略触发');
    } else if (newSignals !== null && newSignals === 0 && result.summary?.totalSignals > 0) {
      console.log('  ℹ️ 无新信号产生 — 上次检查后无变化');
    }

  } else if (hasWebhookOutput) {
    // Webhook 模式
    const payload = generateWebhookPayload(result);

    if (webhookUrl) {
      // POST 到远程 Webhook URL
      const wr = await postWebhook(webhookUrl, payload);
      if (wr.success) {
        console.log(`[Webhook] ✅ 报告已发送 (HTTP ${wr.status})`);
      } else {
        console.error(`[Webhook] ❌ 发送失败: ${wr.error}`);
        process.exit(1);
      }
    } else {
      // 输出 JSON payload 到 stdout（供管道使用）
      console.log(JSON.stringify(payload, null, 2));
    }
  } else {
    // 默认：完整报告
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
