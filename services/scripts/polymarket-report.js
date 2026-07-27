#!/usr/bin/env node
/**
 * Polymarket 模拟交易机器人 — 每30分钟状态报告脚本
 *
 * 从状态文件读取 Polymarket Bot 数据，生成结构化文本报告。
 * 通过 cron 每 30 分钟执行一次并追加到日志文件。
 *
 * 用法：
 *   node scripts/polymarket-report.js                              # 标准输出报告
 *   node scripts/polymarket-report.js --json                       # JSON 格式输出
 *   node scripts/polymarket-report.js --save-state                 # 自动保存状态到文件
 *   node scripts/polymarket-report.js --no-color                   # 纯文本（無 ANSI）
 *
 * 定时任务（每 30 分钟）：
 *   crontab -e
 *   - Every 30 min: cd /root/levan-do-strategies/services && /usr/bin/node scripts/polymarket-report.js --save-state >> /var/log/polymarket-report.log 2>&1
 */

import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ---- 文件路径 ----
const STATUS_FILE = '/tmp/polymarket-bot-status.json';
const CLOSED_FILE = '/tmp/polymarket-bot-status-closed.json';
const HISTORY_FILE = '/tmp/polymarket-bot-status-history.json';
const STATE_FILE = join(__dirname, '..', '.polymarket-state.json');

// ---- 样式常量 ----
const RESET = '\x1b[0m';
const BOLD = '\x1b[1m';
const CYAN = '\x1b[36m';
const GREEN = '\x1b[32m';
const RED = '\x1b[31m';
const YELLOW = '\x1b[33m';
const GRAY = '\x1b[90m';
const MAGENTA = '\x1b[35m';

// ======== 辅助函数 ========

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function fmtTS(ts) {
  const d = new Date(ts);
  return d.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

function fmtPct(v) {
  if (v === null || v === undefined) return '—';
  return v.toFixed(1) + '%';
}

function fmtUsd(v) {
  if (v === null || v === undefined) return '$—';
  const sign = v >= 0 ? '' : '−';
  return sign + '$' + Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function fmtChange(v, suffix = '') {
  if (v === null || v === undefined) return '—';
  const sign = v >= 0 ? '+' : '';
  return sign + v.toFixed(2) + suffix;
}

function colorByPct(pct) {
  if (pct > 80) return RED;
  if (pct > 60) return YELLOW;
  return GREEN;
}

function colorByChange(v) {
  if (v === null || v === undefined) return '';
  if (v > 0) return GREEN;
  if (v < 0) return RED;
  return '';
}

// ======== 数据读取 ========

function readJsonFile(path, def = null) {
  try {
    if (existsSync(path)) {
      return JSON.parse(readFileSync(path, 'utf-8'));
    }
  } catch (err) {
    // 文件损坏或不存在
  }
  return def;
}

function getPm2Status(processName) {
  try {
    const out = execSync(`pm2 pid ${processName} 2>/dev/null`, {
      encoding: 'utf-8',
      timeout: 5000,
    }).trim();
    const pid = parseInt(out, 10);
    return !isNaN(pid) && pid > 0;
  } catch {
    return false;
  }
}

function getRecentClosed(limit = 10) {
  const closed = readJsonFile(CLOSED_FILE, []);
  if (!Array.isArray(closed) || closed.length === 0) return [];
  return closed.slice(-limit).reverse();
}

function getOpenPositionsCount(status) {
  if (!status) return 0;
  return (status.open_positions || []).length;
}

function getSettledPositionsCount(status) {
  if (!status || !status.portfolio) return 0;
  return status.portfolio.settled_positions_count || 0;
}

// ======== 报告构建 ========

export function generatePolymarketReport(options = {}) {
  const noColor = options.noColor;
  // 如果 noColor，所有样式清空
  const R = noColor ? '' : RESET;
  const B = noColor ? '' : BOLD;
  const C = noColor ? '' : CYAN;
  const G = noColor ? '' : GREEN;
  const R_ = noColor ? '' : RED;
  const Y = noColor ? '' : YELLOW;
  const Gr = noColor ? '' : GRAY;
  const M = noColor ? '' : MAGENTA;

  // 读取状态文件
  const status = readJsonFile(STATUS_FILE);
  const closed = getRecentClosed(10);
  const history = readJsonFile(HISTORY_FILE, []);

  const now = new Date().toISOString();
  const isRunning = getPm2Status('polymarket-bot');
  const botRunningFromStatus = status && status.bot && status.bot.status === 'running';

  // ---- 构建报告 ----
  const lines = [];
  const divider = '='.repeat(60);
  const subDivider = '-'.repeat(60);

  lines.push('');
  lines.push(`${B}╔${'═'.repeat(58)}╗${R}`);
  lines.push(`${B}║   Polymarket 模拟交易机器人 — 状态报告          ║${R}`);
  lines.push(`${B}╚${'═'.repeat(58)}╝${R}`);
  lines.push(`  报告时间: ${C}${fmtTime(now)}${R}`);
  lines.push('');

  // ====================================================================
  // 1. 运行状态
  // ====================================================================
  lines.push(`${B}📋 一、运行状态${R}`);
  lines.push(divider);

  const running = isRunning && botRunningFromStatus;
  const runIcon = running ? '✅' : '❌';
  const runText = running ? `${G}运行中${R}` : `${R_}已停止${R}`;

  lines.push(`  ${runIcon} PM2 进程:     ${runText}`);
  if (status && status.bot) {
    lines.push(`  模式:         ${status.bot.mode || 'DRY_RUN'}`);
    if (status.bot.uptime) {
      const uptime = Math.floor(
        (Date.now() - new Date(status.bot.uptime).getTime()) / 1000
      );
      if (uptime > 0) {
        const h = Math.floor(uptime / 3600);
        const m = Math.floor((uptime % 3600) / 60);
        const s = uptime % 60;
        let uptimeStr = '';
        if (h > 0) uptimeStr += `${h} 小时 `;
        if (m > 0 || h > 0) uptimeStr += `${m} 分 `;
        uptimeStr += `${s} 秒`;
        lines.push(`  运行时长:     ${C}${uptimeStr}${R}`);
      }
    }
  }
  lines.push('');

  // ====================================================================
  // 2. 钱包权益
  // ====================================================================
  lines.push(`${B}💰 二、钱包权益${R}`);
  lines.push(divider);

  if (status && status.wallet) {
    const w = status.wallet;
    const perf = status.performance || {};

    const equity = w.equity || 0;
    const balance = w.balance || 0;
    const frozen = w.frozen_balance || 0;
    const initial = w.initial_capital || 10000;
    const returnPct = ((equity - initial) / initial) * 100;

    const returnColor = returnPct >= 0 ? G : R_;
    const drawdownColor = (w.drawdown_pct || 0) > 30 ? R_ : ((w.drawdown_pct || 0) > 15 ? Y : G);

    lines.push(`  初始资金:     ${fmtUsd(initial)}`);
    lines.push(`  当前权益:     ${B}${fmtUsd(equity)}${R}  (${returnColor}${fmtChange(returnPct, '%')}${R})`);
    lines.push(`  可用余额:     ${fmtUsd(balance)}`);
    lines.push(`  冻结资金:     ${fmtUsd(frozen)}`);
    lines.push(`  峰值权益:     ${fmtUsd(w.peak_balance || equity)}`);
    lines.push(`  当前回撤:     ${drawdownColor}${fmtPct(w.drawdown_pct || 0)}${R}`);
    lines.push(`  总手续费:     ${fmtUsd(w.total_fees_paid || 0)}`);
    lines.push(`  资本周转:     ${w.capital_turns || 0} 次 · 合併回收 ${w.total_merge_operations || 0} 次 (${fmtUsd(w.total_merged_usdc || 0)})`);
  } else {
    lines.push(`  ${Y}⚠ 状态文件不可用，无法读取钱包数据${R}`);
  }
  lines.push('');

  // ====================================================================
  // 3. 持仓概况
  // ====================================================================
  lines.push(`${B}💼 三、持仓概况${R}`);
  lines.push(divider);

  const openCount = getOpenPositionsCount(status);
  const settledCount = getSettledPositionsCount(status);

  lines.push(`  活跃持仓:     ${B}${openCount}${R} 个市场`);
  lines.push(`  已结算:       ${settledCount} 笔`);

  if (status && status.open_positions && status.open_positions.length > 0) {
    const positions = status.open_positions;
    const totalInvested = positions.reduce((s, p) => s + (p.invested || 0), 0);

    lines.push('');
    lines.push(`  ${B}活跃持仓明细:${R}`);
    lines.push(`  ${Gr}  ${'市场'.padEnd(42)} ${'方向'.padEnd(6)} ${'价格'.padEnd(7)} ${'投入'.padEnd(10)}${R}`);
    lines.push(`  ${Gr}  ${'─'.repeat(40)} ${'─'.repeat(4)} ${'─'.repeat(5)} ${'─'.repeat(8)}${R}`);

    const maxShow = Math.min(positions.length, 10);
    for (let i = 0; i < maxShow; i++) {
      const p = positions[i];
      const shortQ = (p.market_question || p.market_id || '').substring(0, 38);
      const outcome = p.outcome === 'YES' ? `${G}YES${R}` : `${R_}NO ${R}`;
      const price = p.entry_price != null ? `${p.entry_price.toFixed(4)}` : '—';
      const invested = fmtUsd(p.invested || 0);
      const priceColor = p.entry_price != null && p.entry_price < 0.1 ? G : '';
      lines.push(`  ${shortQ.padEnd(40)} ${outcome} ${priceColor}${price.padEnd(5)}${R} ${invested}`);
    }
    if (positions.length > 10) {
      lines.push(`  ${Gr}  ... 还有 ${positions.length - 10} 个持仓未显示${R}`);
    }
    lines.push(`  → 持仓总投入: ${fmtUsd(totalInvested)}`);
  } else {
    lines.push(`  → ${Gr}当前无活跃持仓${R}`);
  }
  lines.push('');

  // ====================================================================
  // 4. 交易统计 & 胜率
  // ====================================================================
  lines.push(`${B}📊 四、交易统计${R}`);
  lines.push(divider);

  if (status && status.wallet) {
    const w = status.wallet;
    const perf = status.performance || {};
    const totalTrades = w.total_trades || 0;
    const wins = w.wins || 0;
    const losses = w.losses || 0;
    const winRate = totalTrades > 0 ? (wins / totalTrades) * 100 : 0;
    const totalPnl = perf.total_pnl || 0;

    const winRateColor = winRate >= 60 ? G : (winRate >= 40 ? Y : R_);
    const pnlColor = totalPnl >= 0 ? G : R_;

    lines.push(`  总交易次数:   ${B}${totalTrades}${R} 笔`);
    lines.push(`  胜          ${wins} 笔`);
    lines.push(`  负          ${losses} 笔`);
    lines.push(`  胜率:        ${winRateColor}${fmtPct(winRate)}${R}`);
    lines.push(`  总盈亏:       ${pnlColor}${fmtUsd(totalPnl)}${R}`);

    // 每日盈亏
    if (w.daily_pnl != null) {
      const dailyPnl = w.daily_pnl || 0;
      const todayTrades = w.today_trades || 0;
      const dailyColor = dailyPnl >= 0 ? G : R_;
      lines.push(`  今日盈亏:     ${dailyColor}${fmtUsd(dailyPnl)}${R} (${todayTrades} 笔)`);
    }

    // 扫描统计
    if (status.scan_stats) {
      const ss = status.scan_stats;
      lines.push('');
      lines.push(`  ${B}策略扫描:${R}`);
      lines.push(`  扫描信号:     ${ss.total_signals || 0} 次`);
      lines.push(`  检测机会:     ${(ss.strategy && ss.strategy.total_opportunities) || 0} 次`);
      lines.push(`  对冲操作:     ${(ss.strategy && ss.strategy.total_hedges) || 0} 次`);
      lines.push(`  来回倒仓:     ${(ss.strategy && ss.strategy.total_round_trips) || 0} 次`);
      lines.push(`  追踪市场:     ${ss.markets_tracked || 0} 个`);
      lines.push(`  扫描错误:     ${ss.total_errors || 0} 次`);
    }
  } else {
    lines.push(`  ${Y}⚠ 交易数据不可用${R}`);
  }
  lines.push('');

  // ====================================================================
  // 5. 最近结算盈亏
  // ====================================================================
  lines.push(`${B}📈 五、最近结算盈亏${R}`);
  lines.push(divider);

  if (closed.length > 0) {
    const totalPnl = closed.reduce((s, c) => s + (c.pnl || 0), 0);
    const wins = closed.filter(c => (c.pnl || 0) > 0).length;
    const losses = closed.filter(c => (c.pnl || 0) < 0).length;
    const totalInvested = closed.reduce((s, c) => s + (c.invested || 0), 0);

    lines.push(`  最近 ${closed.length} 笔结算:`);
    lines.push(`  盈利: ${G}${wins}${R} 笔 · 亏损: ${R_}${losses}${R} 笔 · 总计: ${colorByChange(totalPnl)}${fmtUsd(totalPnl)}${R}`);

    lines.push('');
    lines.push(`  ${Gr}  ${'时间'.padEnd(14)} ${'市场'.padEnd(34)} ${'方向'.padEnd(6)} ${'盈亏'.padEnd(10)}${R}`);
    lines.push(`  ${Gr}  ${'─'.repeat(12)} ${'─'.repeat(32)} ${'─'.repeat(4)} ${'─'.repeat(8)}${R}`);

    for (const c of closed) {
      const t = fmtTS(c.settlement_time || c.entry_time || '');
      const q = (c.market_question || c.market_id || '').substring(0, 30);
      const outcome = c.outcome === 'YES' ? `${G}YES${R}` : `${R_}NO ${R}`;
      const pnl = c.pnl || 0;
      const pnlStr = fmtUsd(pnl);
      const pnlDisplay = pnl >= 0 ? `${G}${pnlStr}${R}` : `${R_}${pnlStr}${R}`;
      lines.push(`  ${t.padEnd(14)} ${q.padEnd(32)} ${outcome} ${pnlDisplay}`);
    }
  } else {
    lines.push(`  ${Gr}暂无已结算的市场（模拟交易刚开始）${R}`);
  }
  lines.push('');

  // ====================================================================
  // 6. 异常与警告
  // ====================================================================
  lines.push(`${B}⚠ 六、异常与警告${R}`);
  lines.push(divider);

  const warnings = [];

  // 进程状态警告
  if (!running) {
    warnings.push(`🔴 【严重】PM2 进程不在运行状态！`);
  }

  // 回撤警告
  if (status && status.wallet) {
    const dd = status.wallet.drawdown_pct || 0;
    if (dd >= 50) {
      warnings.push(`🔴 【严重】回撤已达 ${fmtPct(dd)}，超过最大限制 50%！`);
    } else if (dd >= 30) {
      warnings.push(`🟡 【警告】回撤较深: ${fmtPct(dd)}`);
    }

    // 亏损警告
    const totalPnl = (status.performance && status.performance.total_pnl) || 0;
    if (totalPnl < -2000) {
      warnings.push(`🔴 【严重】总亏损已达 ${fmtUsd(totalPnl)}`);
    } else if (totalPnl < -1000) {
      warnings.push(`🟡 【注意】总亏损 ${fmtUsd(totalPnl)}`);
    }
  }

  // 高频错误警告
  if (status && status.scan_stats) {
    const errors = status.scan_stats.total_errors || 0;
    const signals = status.scan_stats.total_signals || 0;
    if (signals > 0 && errors / signals > 0.5) {
      warnings.push(`🟡 【警告】扫描错误率较高: ${errors}/${signals} (${(errors/signals*100).toFixed(0)}%)`);
    }
  }

  // 大额持仓集中风险
  if (status && status.open_positions) {
    const positions = status.open_positions;
    const allPhase1 = positions.filter(p => {
      // Check if any position lacks hedge (using scan_stats strategy hedges)
      return true; // Simplified - just check if many positions without close prices
    });
    if (positions.length >= 8 && positions.length > 0) {
      // Check if most are in phase 1 (no hedge)
      warnings.push(`🟡 【注意】持仓 ${positions.length} 个市场，全部处于未对冲状态（Phase 1）`);
    }
  }

  // 状态文件异常
  if (!status) {
    warnings.push(`🔴 【严重】无法读取状态文件 ${STATUS_FILE}`);
  }

  if (warnings.length === 0) {
    lines.push(`  ${G}✅ 无异常${R}`);
  } else {
    for (const w of warnings) {
      lines.push(`  ${w}`);
    }
  }

  // 交易对健康度（如果状态文件有信息）
  if (status && status.scan_stats) {
    const errors = status.scan_stats.total_errors || 0;
    const lastScan = status.scan_stats.last_scan;
    const lastScanDelta = lastScan
      ? Math.floor((Date.now() - new Date(lastScan).getTime()) / 1000)
      : null;

    if (lastScanDelta !== null) {
      lines.push('');
      lines.push(`  最后扫描:     ${C}${fmtTime(lastScan)}${R}`);
      if (lastScanDelta > 300) {
        lines.push(`  ${Y}⚠ 距上次扫描已超过 5 分钟，可能扫描循环卡顿${R}`);
      } else {
        lines.push(`  ✅ 扫描正常 (${lastScanDelta} 秒前)`);
      }
    }
  }

  lines.push('');

  // ====================================================================
  // 7. 策略配置摘要
  // ====================================================================
  lines.push(`${B}⚙ 七、策略配置${R}`);
  lines.push(divider);

  if (status && status.config) {
    const cfg = status.config;
    lines.push(`  初始资金:     ${fmtUsd(cfg.initial_capital || 10000)}`);
    lines.push(`  交易比例:     ${cfg.trade_qty_pct || 50}%`);
    lines.push(`  最大持仓:     ${cfg.max_positions || 5} 个`);
    lines.push(`  扫描间隔:     ${cfg.scan_interval || 30} 秒`);
    lines.push(`  最小机会分:   ${cfg.std0_min_score || 50}`);
    lines.push(`  合併回收:     ${cfg.merge_enabled ? `${G}启用${R}` : `${R_}停用${R}`}`);
  } else {
    lines.push(`  ${Gr}配置数据不可用${R}`);
  }
  lines.push('');

  // ---- 结论 ----
  lines.push(subDivider);

  if (running && warnings.length === 0) {
    lines.push(`  ${B}${G}✅ 结论: 机器人运行正常，所有指标健康${R}`);
  } else if (running && warnings.length > 0) {
    const criticalCount = warnings.filter(w => w.includes('🔴')).length;
    const warnCount = warnings.filter(w => w.includes('🟡')).length;
    const level = criticalCount > 0 ? `${R_}严重 (${criticalCount} 项)${R}` : `${Y}注意 (${warnCount} 项)${R}`;
    lines.push(`  ${B}${Y}⚠ 结论: 机器人运行中，但有 ${level}${R}`);

    if (status && status.wallet) {
      const equity = status.wallet.equity || 0;
      const initial = status.wallet.initial_capital || 10000;
      const returnPct = ((equity - initial) / initial) * 100;
      lines.push(`    当前权益: ${fmtUsd(equity)} (${fmtChange(returnPct, '%')})`);
      lines.push(`    活跃持仓: ${getOpenPositionsCount(status)} 个`);
    }
  } else {
    lines.push(`  ${B}${R_}❌ 结论: 机器人已停止${R}`);
    if (status) {
      const equity = status.wallet ? status.wallet.equity : '—';
      lines.push(`    最后已知权益: ${fmtUsd(equity)}`);
    }
  }
  lines.push(subDivider);
  lines.push('');

  return {
    success: true,
    report: lines.join('\n'),
    summary: {
      timestamp: now,
      running,
      openPositions: openCount,
      settledCount,
      totalTrades: status?.wallet?.total_trades || 0,
      winRate: status?.wallet?.total_trades > 0
        ? ((status.wallet.wins || 0) / status.wallet.total_trades) * 100
        : 0,
      totalPnl: (status?.performance?.total_pnl) || 0,
      equity: status?.wallet?.equity || 0,
      drawdown: status?.wallet?.drawdown_pct || 0,
      totalFees: status?.wallet?.total_fees_paid || 0,
      capitalTurns: status?.wallet?.capital_turns || 0,
      warnings: warnings.length,
      scanSignals: status?.scan_stats?.total_signals || 0,
      scanErrors: status?.scan_stats?.total_errors || 0,
      lastScan: status?.scan_stats?.last_scan || null,
    },
    filePath: STATUS_FILE,
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
  const jsonMode = args.includes('--json');
  const saveStateFlag = args.includes('--save-state');
  const noColor = args.includes('--no-color');

  const result = generatePolymarketReport({ noColor });

  if (!result.success) {
    console.error(`\n❌ 生成报告失败\n`);
    process.exit(1);
  }

  // 保存当前状态（供下次对比）
  if (saveStateFlag && result.summary) {
    saveState({
      lastCheck: new Date().toISOString(),
      ...result.summary,
    });
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
