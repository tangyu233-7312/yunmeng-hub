#!/usr/bin/env python
"""上传 GitHub 前的"纯净度"自检：确保个人使用痕迹与密钥不会进公开仓库。

==================== 为什么需要这个脚本 ====================
这个仓库是在**真实使用**中长出来的：里面混着贡献者本机的路径、真实账号名、
AI 丢过来的截图、以及运行期数据（向量库、日志、导出）。
发到公开仓库前必须逐项确认"哪些**绝对不能进**" —— 而"看一眼"是查不全的
（本项目刚踩过：一个 `.dsh-drop/` 个人截图已经被 git 跟踪了很久）。

==================== 三级结论 ====================
    BLOCKER  绝不能发布：真实密钥、`.env`、数据库转储、运行期数据、个人截图目录、
             **以及 `--name` 明确指定的个人标识（真名 / 账号名）**
    WARN     需要人确认：疑似密钥、个人标识（本机路径/账号名）、可疑二进制
    OK       通过

==================== 怎么判断"会不会被发布" ====================
    · 在 git 仓库里：以 `git ls-files`（**实际会被提交的文件**）为准，
      再用 `git check-ignore` 判断某个路径是否被忽略。
    · 不在 git 仓库里（比如刚 robocopy 出来的干净副本）：遍历文件系统，
      把"将要发布的目录"整体当作候选。

用法：
    python scripts/prepublish_check.py              # 检查当前目录
    python scripts/prepublish_check.py --root DIR    # 检查指定目录
    python scripts/prepublish_check.py --name 真名  # 追加"个人标识"关键词（可多次，命中即 BLOCKER）
退出码：0 = 没有 BLOCKER；1 = 有 BLOCKER（不要上传）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# ------------------------------------------------------------------
#  规则
# ------------------------------------------------------------------
#: 绝不能出现在公开仓库里的**路径**（相对仓库根）
FORBIDDEN_PATHS: tuple[tuple[str, str], ...] = (
    (".env", "环境变量文件（数据库口令 / JWT 密钥）"),
    ("data/chroma", "向量库实体（长期记忆内容）"),
    ("data/logs", "运行日志（含真实提示词与会话内容）"),
    ("data/exports", "数据导出"),
    (".dsh-drop", "「丢给 AI 的文件」暂存目录（个人截图/设计稿）"),
    (".venv", "本地虚拟环境"),
    ("node_modules", "前端依赖（本仓库零构建，不需要它）"),
)

#: 允许出现在仓库里的 `data/` 子路径（白名单，其余 data/ 都算运行期数据）
DATA_ALLOWLIST: tuple[str, ...] = ("data/test-cards/",)

#: 密钥/凭据形状
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI 风格密钥", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Anthropic 密钥", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("私钥文件头", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("带口令的连接串", re.compile(r"\b\w+://[^:/\s]+:[^@/\s]{6,}@")),
    ("明文密钥赋值", re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{12,}['\"]")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
)

#: 个人标识形状（贡献者本机信息）
PERSONAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Windows 用户目录", re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"'`)]+")),
    ("Unix 家目录", re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")),
    ("邮箱地址", re.compile(r"\b[\w.+-]+@[\w-]+\.[A-Za-z]{2,}\b")),
)

#: 数据库转储（schema.sql 是建表脚本，允许）
DB_DUMP_PATTERNS: tuple[str, ...] = ("*.sql.gz", "*.sqlite3", "*.db", "*.dump", "*.bak.sql")

#: 文本文件后缀（只在这些文件里扫内容）
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".css", ".html", ".json", ".md", ".txt", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".env", ".example", ".ps1", ".sh", ".sql", ".jsonl",
}
#: 单个文件最大扫描体积（超过就跳过，避免读进大二进制）
MAX_SCAN_BYTES = 2 * 1024 * 1024


def _git(root: Path, *args: str) -> str:
    """跑一条 git 命令并返回 stdout（不在仓库里就返回空串）。"""
    try:
        out = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def _git_ignored(root: Path, rel: str) -> bool:
    """`rel` 是否被 .gitignore 忽略。

    ★ 用 `git check-ignore -q` 的**返回码**判断（0 = 被忽略）：
      第一版我读的是 stdout，于是**所有被忽略的敏感路径都被报成"会被上传"** ——
      12 条 BLOCKER 里有 5 条是假警报。这种噪音会让人直接把检查器关掉。
    """
    try:
        out = subprocess.run(
            ["git", "check-ignore", "-q", rel],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


#: 占位/假值的特征词：真实密钥不会自称 test / fake / invalid
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "test", "fake", "dummy", "invalid", "example", "sample", "smoke",
    "placeholder", "changeme", "your-", "xxx", "secret-value", "abcdef",
    "123456", "not-a-real", "expired", "redacted",
)


def _looks_like_placeholder(hit: str) -> bool:
    """这一串是不是明显的"测试用假密钥"。

    ★ 为什么必须区分：测试里**故意**写着 `sk-definitely-invalid-key-for-testing`
      这类值（项目甚至有测试专门断言密钥不会被回显）。把它们当 BLOCKER，
      检查器就会永远红着，然后没人再看它。
    """
    low = hit.lower()
    return any(marker in low for marker in PLACEHOLDER_MARKERS)


def published_files(root: Path) -> tuple[list[Path], bool]:
    """返回 (会被发布的文件列表, 是否在 git 仓库里)。

    · git 仓库：`git ls-files` 是权威答案（只列**被跟踪**的文件）。
    · 非 git：遍历文件系统，跳过明显的本地目录（与 FORBIDDEN_PATHS 同名的一律跳过，
      它们的"存在"会被单独报出来）。
    """
    if (root / ".git").exists():
        tracked = [line for line in _git(root, "ls-files").splitlines() if line.strip()]
        return [root / p for p in tracked], True

    skip_dirs = {name.rstrip("/") for name, _ in FORBIDDEN_PATHS}
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in skip_dirs for part in rel.split("/")):
            continue
        files.append(path)
    return files, False


def scan(args: argparse.Namespace) -> dict:
    root = Path(args.root).resolve()
    files, in_git = published_files(root)
    blockers: list[str] = []
    warns: list[str] = []
    notes: list[str] = []

    notes.append(f"检查目录：{root}")
    notes.append(
        "判定依据：" + ("git ls-files（仓库里**实际会被提交**的文件）" if in_git else "文件系统遍历（非 git 目录）")
    )
    notes.append(f"候选文件数：{len(files)}")

    # ---- 1) 禁区路径：存在就要报（在 git 里再看它是否被忽略）----
    for rel, why in FORBIDDEN_PATHS:
        path = root / rel
        if not path.exists():
            continue
        ignored = _git_ignored(root, rel) if in_git else False
        if ignored:
            notes.append(f"本机存在但已被 .gitignore 忽略（不会上传）：{rel} —— {why}")
        else:
            blockers.append(f"**会被上传**的敏感路径：{rel} —— {why}")

    # ---- 2) 逐文件扫描 ----
    names = [n for n in (args.name or []) if n]
    for path in files:
        rel = path.relative_to(root).as_posix()

        if rel.startswith("data/") and not rel.startswith(DATA_ALLOWLIST):
            blockers.append(f"运行期数据进仓库：{rel}")
            continue
        if rel.endswith(".sql") and rel != "scripts/schema.sql":
            blockers.append(f"疑似数据库转储：{rel}")
            continue
        if any(path.match(p) for p in DB_DUMP_PATTERNS):
            blockers.append(f"疑似数据库转储：{rel}")
            continue

        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                warns.append(f"文件过大未扫描：{rel}")
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:  # pragma: no cover - 权限等
            warns.append(f"读取失败：{rel}（{exc}）")
            continue

        lines = text.splitlines()
        # ★ 测试范围（tests/ 与假模型/冒烟脚本）里的密钥形状降级为 WARN：
        #   那里本来就写着大量**故意的**假密钥（而且项目有测试专门断言密钥不被回显）。
        #   但**不忽略** —— 真有人把真密钥粘进测试里，它仍会以 WARN 出现在报告里等人看。
        is_test_scope = rel.startswith("tests/") or path.name in {"smoke_test.py", "conftest.py"}
        for label, pattern in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                line_no = text[: m.start()].count("\n") + 1
                snippet = (lines[line_no - 1].strip() if line_no <= len(lines) else "")[:80]
                if _looks_like_placeholder(m.group(0)) or _looks_like_placeholder(snippet):
                    notes.append(f"疑似占位值（不算 BLOCKER）：{rel}:{line_no} → {snippet}")
                    continue
                if is_test_scope:
                    warns.append(f"{label}（测试范围内，请人工确认是假值）：{rel}:{line_no} → {snippet}")
                    continue
                blockers.append(f"{label}：{rel}:{line_no} → {snippet}")
        for label, pattern in PERSONAL_PATTERNS:
            for m in pattern.finditer(text):
                hit = m.group(0)
                # 白名单：示例里常见的占位/无意义用户名不算个人标识
                if hit.lower().rstrip("/").endswith(("/user", "/username", "/<用户名>")):
                    continue
                if "@example.com" in hit or "@example.org" in hit:
                    continue
                line_no = text[: m.start()].count("\n") + 1
                warns.append(f"{label}：{rel}:{line_no} → {hit}")
        for name in names:
            if name in text:
                line_no = text.index(name)
                # ★ 为什么这里是 BLOCKER 而不是 WARN：
                #   `--name` 是用户**明确告诉我"这是我的真名"**的输入，它出现在待发布文件里
                #   就是"使用痕迹"，不是"需要人工确认的疑似"。以前按 WARN 报，
                #   结果一份带真名的文档在闸门里只是"提示"了一下就被忽略了
                #   （本轮真的发生过：`docs/next-electron-and-github.md` 里写着真账号名）。
                #   同理，这个关键词的**位置信息**不能进报告 —— 报告自己的措辞里也不能有这个真名。
                blockers.append(
                    f"出现指定关键词（真名等个人标识）：{rel}:{text[:line_no].count(chr(10)) + 1}"
                )

    return {
        "root": str(root),
        "in_git": in_git,
        "files": len(files),
        "blockers": blockers,
        "warns": warns,
        "notes": notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="上传 GitHub 前的纯净度自检")
    parser.add_argument("--root", default=".", help="要检查的目录（默认当前目录）")
    parser.add_argument("--name", action="append", default=[],
                        help="追加个人标识关键词（可多次）。命中即 BLOCKER —— 这是你亲口声明的真名/账号名")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    # ★ Windows 控制台默认是 GBK，`✓/✗` 这类符号会直接抛 UnicodeEncodeError
    #   （第一次跑就踩了）。这里强制 UTF-8；标记本身也已经换成 ASCII。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - 老环境
        pass

    result = scan(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 68)
        print("上传前纯净度自检")
        print("=" * 68)
        for line in result["notes"]:
            print("  ·", line)
        print()
        if result["blockers"]:
            print(f"[!] BLOCKER（{len(result['blockers'])} 项，**不要上传**）：")
            for item in result["blockers"]:
                print("   -", item)
        else:
            print("[OK] 没有 BLOCKER：没有会被上传的密钥 / 运行期数据 / 个人截图目录")
        print()
        if result["warns"]:
            print(f"[?] WARN（{len(result['warns'])} 项，需要人工确认）：")
            for item in result["warns"][:60]:
                print("   -", item)
            if len(result["warns"]) > 60:
                print(f"   …… 其余 {len(result['warns']) - 60} 项省略")
        else:
            print("[OK] 没有 WARN：未发现本机路径 / 账号名 / 邮箱")
        print()

    return 1 if result["blockers"] else 0


if __name__ == "__main__":
    sys.exit(main())
