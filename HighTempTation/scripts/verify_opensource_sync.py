#!/usr/bin/env python3
"""
verify_opensource_sync.py — 开源仓库同步检查脚本

用途:
  在把内部天气代码同步到开源仓库 (github.com/ya85275576/HighTempTation) 之前,
  检查本地目录中是否混入了加密策略相关内容 (5min/PM5 集成等), 防止泄密。

用法:
  # 1. 只扫描本地目录中的加密敏感项
  python3 HighTempTation/scripts/verify_opensource_sync.py \
      --local HighTempTation

  # 2. 与已克隆的开源仓库目录对比 (推荐, 会给出逐文件差异)
  git clone https://github.com/ya85275576/HighTempTation.git /tmp/HTT_remote
  python3 HighTempTation/scripts/verify_opensource_sync.py \
      --local HighTempTation --remote /tmp/HTT_remote

退出码:
  0 = 检查通过 (本地目录不含加密敏感项 / 与开源版差异仅限天气增强)
  1 = 检查未通过 (发现加密敏感项, 或差异无法解释)

说明:
  - 加密敏感文件白名单: 文件名命中即视为加密模块, 必须排除在开源仓库之外
  - 加密敏感内容: 代码/文档中的关键词命中即视为加密引用
  - 纯标准库实现, 无需安装任何依赖
"""

import argparse
import os
import re
import sys
from pathlib import Path

# ── 加密敏感文件 (文件级白名单: 命中即必须排除) ──────────────────────
SENSITIVE_DIRS = {
    "polymarket_5min_bot",
    "adapters",
}
SENSITIVE_FILES = {
    "shared_risk.py",
    "account_manager.py",
    "5MIN_INTEGRATION.md",
    "verify_5min_integration.py",
}

# ── 加密敏感内容 (关键词级: 命中即视为加密引用) ──────────────────────
SENSITIVE_PATTERNS = [
    r"\b5min\b", r"\bPM5\b", r"\bpolymarket_5min\b",
    r"\bBenjam1nCup\b", r"\bshared_risk\b", r"\baccount_manager\b",
    r"\bccxt\b", r"\bpolymarket-client\b", r"\bwebsockets\b",
    r"\b5分钟套利\b", r"\b5分钟结算\b", r"\b加密结算\b",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_PATTERNS]

# 二进制文件不扫描内容
_BINARY_EXTS = {".js", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pkl"}

# 跳过目录
_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "calibrator_models", ".pm2"}


def is_sensitive_path(rel: str) -> bool:
    """文件路径是否命中加密白名单。"""
    parts = set(Path(rel).parts)
    if parts & SENSITIVE_DIRS:
        return True
    if Path(rel).name in SENSITIVE_FILES:
        return True
    return False


def scan_content_sensitive(root: Path) -> list[tuple[str, str]]:
    """扫描目录中的加密敏感内容引用, 返回 [(相对路径, 命中行摘要)]。"""
    hits: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            fp = Path(dirpath) / fn
            rel = fp.relative_to(root).as_posix()
            if is_sensitive_path(rel):
                continue  # 文件级白名单已覆盖
            if fn.endswith(tuple(_BINARY_EXTS)):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        for pat in _COMPILED:
                            if pat.search(line):
                                snippet = line.strip()[:100]
                                hits.append((f"{rel}:{lineno}", snippet))
                                break
            except (OSError, UnicodeDecodeError):
                continue
    return hits


def list_files(root: Path) -> dict[str, str]:
    """返回 {相对路径: 文件内容}。"""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            fp = Path(dirpath) / fn
            rel = fp.relative_to(root).as_posix()
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    out[rel] = f.read()
            except (OSError, UnicodeDecodeError):
                out[rel] = "<binary>"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="开源仓库同步检查: 防止加密策略混入天气开源仓库")
    ap.add_argument("--local", required=True, help="本地内部目录 (如 HighTempTation)")
    ap.add_argument("--remote", default=None, help="已克隆的开源仓库目录 (可选, 对比模式)")
    args = ap.parse_args()

    local = Path(args.local)
    if not local.is_dir():
        print(f"❌ 目录不存在: {local}")
        return 1

    fail = False

    # ── 1. 本地加密敏感文件检查 ────────────────────────────────────
    print(f"🔍 扫描本地目录: {local}")
    print("  ── 加密敏感文件 (必须排除) ──")
    sensitive_files = []
    for dirpath, dirnames, filenames in os.walk(local):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            fp = Path(dirpath) / fn
            rel = fp.relative_to(local).as_posix()
            if is_sensitive_path(rel):
                sensitive_files.append(rel)
    if sensitive_files:
        for s in sorted(sensitive_files):
            print(f"    ⚠️  {s}")
        fail = True
    else:
        print("    ✅ 无加密敏感文件")

    # ── 2. 本地加密敏感内容检查 ────────────────────────────────────
    print("  ── 加密敏感内容引用 ──")
    content_hits = scan_content_sensitive(local)
    if content_hits:
        for rel, snippet in content_hits[:30]:
            print(f"    ⚠️  {rel}: {snippet}")
        if len(content_hits) > 30:
            print(f"    … 共 {len(content_hits)} 处")
        fail = True
    else:
        print("    ✅ 无加密敏感内容引用")

    # ── 3. (可选) 与开源仓库对比 ───────────────────────────────────
    if args.remote:
        remote = Path(args.remote)
        print(f"🔍 对比开源仓库: {remote}")
        if not remote.is_dir():
            print(f"  ❌ 目录不存在: {remote}")
            return 1
        lf, rf = list_files(local), list_files(remote)
        only_local = sorted(set(lf) - set(rf))
        only_remote = sorted(set(rf) - set(lf))
        print(f"  ── 仅存在于本地 (需确认是否应排除或推送) ({len(only_local)}) ──")
        for f in only_local:
            tag = "🚫 加密白名单" if is_sensitive_path(f) else "❓ 待确认"
            print(f"    {tag}: {f}")
            if not is_sensitive_path(f):
                fail = True
        print(f"  ── 仅存在于开源仓库 ({len(only_remote)}) ──")
        for f in only_remote:
            print(f"    ✅ {f}")
        print("  ── 共有文件差异 ──")
        diff_cnt = 0
        for f in sorted(set(lf) & set(rf)):
            if lf[f] != rf[f]:
                diff_cnt += 1
                print(f"    ⚠️  内容不同: {f}")
        if diff_cnt == 0:
            print("    ✅ 共有文件全部一致")
        else:
            print(f"    ⚠️  {diff_cnt} 个共有文件内容不同 —— 请确认差异均为天气功能增强")
            fail = True

    print()
    if fail:
        print("❌ 检查未通过: 存在加密敏感项或未解释差异, 请清理后再推送开源仓库。")
        return 1
    print("✅ 检查通过: 本地目录不含加密敏感项, 与开源仓库状态一致, 可以推送。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
