#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布前门禁：确认待公开的文件里没有本机专属信息、凭据或版本不一致。

用法：
    python3 scripts/prepublish_check.py
    make check

检查的是 **git 已跟踪的文件**（即真正会被 push 出去的内容），不是整个工作区。
启用 .githooks/pre-push 后，每次 push 前自动跑一遍，漏脱敏就推不出去。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 本文件内含检测规则字面量；config.example.env 是占位模板，含示例标签。
# 二者不参与「内容扫描」，但仍参与「文件是否被跟踪 / 必备文件是否齐全」检查。
CONTENT_SKIP = {"scripts/prepublish_check.py", "config.example.env"}

TEXT_EXT = {"", ".md", ".py", ".sh", ".command", ".env", ".txt", ".yaml",
            ".yml", ".json", ".toml", ".cfg", ".ini", ".gitignore"}

# 用拼接避免本文件自身出现被检测的字面串
PATTERNS = [
    ("本机绝对路径", re.compile(r"/" + "Users" + r"/[^\s\"'`,)]+")),
    ("Linux 家目录绝对路径", re.compile(r"/" + "home" + r"/[^\s\"'`,)]+")),
    ("Windows 用户目录", re.compile(r"[A-Za-z]:\\" + "Users" + r"\\", re.I)),
    ("邮箱地址", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("GitHub 凭据", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("云厂商密钥", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("API Key 形态", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("私钥内容", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Bearer 凭据", re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}")),
    ("具体 launchd 标签", re.compile(r"com\.[A-Za-z0-9_-]+\.mlx\.(server|proxy)")),
]

FORBIDDEN_TRACKED = [
    ("本机配置 config.env", lambda p: p in ("config.env", ".env")),
    ("运行日志 / 审计记录", lambda p: p.endswith((".jsonl", ".log"))),
    ("备份文件", lambda p: ".bak" in p or p.endswith(("~", ".orig", ".swp"))),
]

results: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    results.append((ok, msg))


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                             capture_output=True, check=True).stdout
        return sorted(p.decode("utf-8") for p in out.split(b"\0") if p)
    except Exception:                                          # noqa: BLE001
        # 还没 git init：退回工作区扫描，手动排除运行产物
        skip_dirs = {".git", "__pycache__", ".venv", "logs", "audit"}
        return sorted(
            str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
            if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip_dirs)
            and p.name != "config.env" and not p.name.endswith((".jsonl", ".log"))
        )


def main() -> int:
    files = tracked_files()
    check(bool(files), "能拿到待发布文件清单（%d 个）" % len(files))

    for rel in files:
        for label, pred in FORBIDDEN_TRACKED:
            if pred(rel):
                check(False, "不该被跟踪的文件：%s（%s）" % (rel, label))

    for rel in files:
        if rel in CONTENT_SKIP:
            continue
        path = ROOT / rel
        if path.suffix.lower() not in TEXT_EXT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pat in PATTERNS:
                if pat.search(line):
                    check(False, "%s:%d 命中「%s」：%s" %
                          (rel, lineno, label, line.strip()[:100]))

    for needed in ("README.md", "LICENSE", "CHANGELOG.md", "VERSION",
                   ".gitignore", "config.example.env", "ops_panel.py", "panel.sh"):
        check((ROOT / needed).is_file(), "必备文件存在：%s" % needed)

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    check(bool(re.fullmatch(r"\d+\.\d+\.\d+", version)),
          "VERSION 符合语义化版本：%s" % version)
    src = (ROOT / "ops_panel.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([^"]+)"', src, re.M)
    check(bool(m) and m.group(1) == version,
          "ops_panel.py __version__ 与 VERSION 一致（%s）" % (m.group(1) if m else "缺失"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    check("## [%s]" % version in changelog, "CHANGELOG 含 %s 条目" % version)
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    check("MIT License" in lic and "Copyright" in lic, "LICENSE 为完整 MIT 文本")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    check("config.env" in ignore and "*.jsonl" in ignore,
          ".gitignore 排除了本机配置与日志")

    bad = [msg for ok, msg in results if not ok]
    for ok, msg in results:
        if not ok:
            print("FAIL  " + msg)
    print("检查 %d 项，失败 %d 项。跳过内容扫描：%s" %
          (len(results), len(bad), ", ".join(sorted(CONTENT_SKIP))))
    if bad:
        print("\n请先处理以上问题再 push。详见 docs/MAINTAINING.md。", file=sys.stderr)
        return 1
    print("OK  可以发布。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
