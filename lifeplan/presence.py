"""宿主感知：从本地信号推断「这台机器的主人在干什么」。

三条原则
────────
1. **只读**。只读取目录结构、git 配置、进程表、可选的历史记录，不修改任何东西。
2. **有证据**。每一条推断必须能指回具体路径 / 进程 / 命令；没有证据的猜测不写进结论。
   这是本项目「结论必须可核验」原则在感知层的延续。
3. **限界**。扫描有深度、数量、大小上限，跳过依赖目录与缓存；
   不递归 `~`，不发网络请求，不上传任何内容。

关于置信度
──────────
这里输出的是**启发式推断**，不是事实。`confidence` 表示"支持首要假设的证据权重
占全部证据的比例"，只能用来排序，不能当成概率。真实职业身份仍需人来确认。
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .db import utc_now

# 扫描上限：宁可少扫，不可卡死
MAX_PROJECTS = 400
MAX_DEPTH = 2
FILES_PER_PROJECT = 300
MAX_HISTORY = 600

SKIP_DIRS = {
    "__pycache__", "node_modules", "venv", ".venv", "env", "site-packages",
    ".git", ".cache", ".local", ".npm", ".cargo", "target", "build", "dist",
    ".mypy_cache", ".pytest_cache", ".tox", "htmlcov", ".idea", "__MACOSX",
}

PROJECT_MARKERS = {
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "requirements.txt",
    "setup.py", "setup.cfg", "Pipfile", "pom.xml", "build.gradle", "Makefile",
    "CMakeLists.txt", "composer.json", "Gemfile", ".git",
}

EXT_LANG = {
    ".py": "Python", ".ipynb": "Jupyter", ".js": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".jsx": "JavaScript", ".rs": "Rust", ".go": "Go",
    ".java": "Java", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".kt": "Kotlin",
    ".sh": "Shell", ".sql": "SQL", ".tex": "LaTeX", ".md": "Markdown",
    ".html": "HTML", ".css": "CSS", ".vue": "Vue", ".svelte": "Svelte",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".json": "JSON",
    ".csv": "Data", ".parquet": "Data", ".xlsx": "Spreadsheet",
}

# 工具 → 能力家族
TOOL_FAMILIES = {
    "python": ("Python 运行时", ["python3", "python", "pip", "pip3", "uv", "conda"]),
    "node": ("Node 生态", ["node", "npm", "pnpm", "yarn", "tsc"]),
    "ml": ("机器学习", ["nvidia-smi", "nvcc", "python3", "jupyter"]),
    "container": ("容器化", ["docker", "podman", "kubectl", "nerdctl"]),
    "git": ("版本控制", ["git", "gh", "git-lfs"]),
    "media": ("音视频处理", ["ffmpeg", "convert", "sox"]),
    "browser": ("桌面/浏览器", ["google-chrome", "chromium", "firefox", "gio"]),
    "db": ("数据库", ["psql", "mysql", "redis-cli", "sqlite3"]),
    "build": ("编译工具链", ["gcc", "g++", "clang", "make", "cmake", "cargo", "go"]),
    "security": ("安全与逆向", ["nmap", "gdb", "objdump", "radare2", "strings"]),
    "desktop_control": ("桌面自动化", ["xdotool", "wmctrl", "ydotool", "xrandr"]),
    "media_gen": ("生成式素材", ["ffmpeg", "magick", "inkscape"]),
}

PROCESS_FAMILY = [
    ("浏览器", ("chrome", "chromium", "firefox", "edge")),
    ("编辑器 / IDE", ("code", "cursor", "vim", "nvim", "emacs", "pycharm", "idea")),
    ("Python / 训练", ("python", "ipython", "jupyter", "torchrun")),
    ("Node 运行时", ("node", "npm", "pnpm", "vite", "next")),
    ("容器", ("docker", "containerd", "podman")),
    ("桌面环境", ("gnome-shell", "plasmashell", "Xwayland", "mutter", "kwin")),
    ("AI 助手", ("workbuddy", "claude", "codex", "ollama")),
]

# 推断规则：命中信号 → 假设
HYPOTHESIS_RULES = [
    (
        "机器学习 / 竞赛建模",
        [
            (r"\.ipynb$", 3.0, "存在 Jupyter notebook"),
            (r"kaggle|arc-agi|leaderboard|submission|contest", 3.0, "目录名指向数据竞赛"),
            (r"torch|tensorflow|sklearn|transformers|qlora|lora", 2.0, "路径或命令涉及训练框架"),
            (r"nvidia-smi|nvcc", 1.5, "具备 CUDA 工具链"),
        ],
    ),
    (
        "学术研究 / 论文产出",
        [
            (r"arxiv|preprint|paper|manuscript|tmlr|neurips|iclr", 3.0, "目录名指向论文投稿流程"),
            (r"\.tex$|\.bib$", 2.0, "存在 LaTeX / BibTeX 源文件"),
            (r"reviewer|review|rebuttal|calibrat", 2.0, "涉及审稿或校准研究"),
        ],
    ),
    (
        "求职 / 职业转型",
        [
            (r"resume|求职|job[-_]?search|boss[-_]?zhipin|interview|offer", 3.0, "目录名指向求职材料"),
            (r"投递|招聘|简历", 3.0, "中文求职相关命名"),
        ],
    ),
    (
        "AI 工具链 / 智能体工程",
        [
            (r"\.workbuddy|skills?$|agent|mcp|prompt", 2.5, "存在智能体 / 技能配置目录"),
            (r"llm|gpt|deepseek|qwen|gemini", 2.0, "路径涉及大模型"),
        ],
    ),
    (
        "全栈 / 后端工程",
        [
            (r"package\.json|pyproject\.toml|go\.mod|Cargo\.toml", 1.5, "存在项目清单文件"),
            (r"api|server|backend|frontend|web", 1.5, "目录名指向服务端或前端"),
            (r"docker", 1.0, "存在容器化配置"),
        ],
    ),
]

SECRET_HINT = re.compile(
    r"(password|passwd|token|secret|api[_-]?key|sk-[A-Za-z0-9]|credential|-----BEGIN)", re.I
)

# 信号来源的可信度：项目目录 > 历史命令 > 已装工具 > 运行进程
# 理由：装了 CUDA 只说明机器能跑训练，不说明主人真的在训练；
# 而一个叫 casmi26/ 的目录几乎不会骗人。
SOURCE_WEIGHT = {
    "project": 2.0,
    "history": 1.5,
    "tool": 0.5,
    "process": 0.3,
    "session": 0.2,
}


# ── 数据结构 ────────────────────────────────────────────────
@dataclass
class Signal:
    kind: str
    name: str
    value: str
    evidence: str
    weight: float = 1.0


@dataclass
class Hypothesis:
    label: str
    score: float
    evidence: list[str] = field(default_factory=list)


@dataclass
class HostProfile:
    hostname: str
    os_name: str
    session: str
    scanned_at: str
    signals: list[Signal] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "hostname": self.hostname,
                "os_name": self.os_name,
                "session": self.session,
                "scanned_at": self.scanned_at,
                "summary": self.summary,
                "confidence": self.confidence,
                "hypotheses": [
                    {"label": h.label, "score": h.score, "evidence": h.evidence[:8]}
                    for h in self.hypotheses
                ],
                "signals": [
                    {"kind": s.kind, "name": s.name, "value": s.value, "evidence": s.evidence}
                    for s in self.signals
                ],
                "notes": self.notes,
            },
            ensure_ascii=False,
            indent=2,
        )


# ── 各扫描器 ────────────────────────────────────────────────
def scan_session() -> list[Signal]:
    out = [
        Signal("session", "主机名", socket.gethostname(), "socket.gethostname()"),
        Signal("session", "系统", f"{platform.system()} {platform.release()}",
               "/etc/os-release + uname"),
        Signal("session", "CPU 核数", str(os.cpu_count() or 0), "os.cpu_count()"),
        Signal("session", "Shell", os.environ.get("SHELL", "?"), "$SHELL"),
    ]
    session_type = os.environ.get("XDG_SESSION_TYPE", "")
    if session_type:
        out.append(Signal("session", "会话类型", session_type, "$XDG_SESSION_TYPE"))
    if os.environ.get("WAYLAND_DISPLAY"):
        out.append(Signal("session", "显示服务", "Wayland", "$WAYLAND_DISPLAY"))
    elif os.environ.get("DISPLAY"):
        out.append(Signal("session", "显示服务", "X11", "$DISPLAY"))
    return out


def scan_tools() -> list[Signal]:
    import shutil

    out: list[Signal] = []
    for family, (label, tools) in TOOL_FAMILIES.items():
        found = [t for t in tools if shutil.which(t)]
        if found:
            out.append(
                Signal(
                    "tool", label, ", ".join(sorted(set(found))),
                    "which " + " / ".join(sorted(set(found)))[:160],
                    weight=float(len(found)),
                )
            )
    return out


def _read_rss_kb(pid: str) -> int:
    try:
        with open(f"/proc/{pid}/statm", "r", encoding="ascii", errors="ignore") as f:
            parts = f.read().split()
        return int(parts[1]) * 4 if len(parts) > 1 else 0  # pages → KB（4K 页）
    except (OSError, ValueError, IndexError):
        return 0


def scan_processes(top_n: int = 10) -> list[Signal]:
    procs = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm", "r", encoding="utf-8", errors="ignore") as f:
                comm = f.read().strip()
        except OSError:
            continue
        if not comm:
            continue
        procs.append((comm, _read_rss_kb(pid), pid))
    procs.sort(key=lambda x: -x[1])
    out: list[Signal] = []
    for comm, rss, pid in procs[:top_n]:
        fam = "其他"
        low = comm.lower()
        for label, keys in PROCESS_FAMILY:
            if any(k in low for k in keys):
                fam = label
                break
        out.append(
            Signal("process", comm, f"{fam}｜{rss // 1024} MiB",
                   f"/proc/{pid}（{comm}，{fam}）", weight=1.0)
        )
    return out


def _project_describe(path: Path, names: set[str]) -> dict:
    exts: Counter[str] = Counter()
    file_count = 0
    has_notebook = any(n.endswith(".ipynb") for n in names)
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in EXT_LANG:
                exts[ext] += 1
            file_count += 1
            if file_count >= FILES_PER_PROJECT:
                break
        if file_count >= FILES_PER_PROJECT:
            break

    remote = ""
    cfg = path / ".git" / "config"
    if cfg.exists():
        try:
            for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("url ="):
                    remote = line.split("=", 1)[1].strip()
                    break
        except OSError:
            pass

    try:
        last = max((os.path.getmtime(path / n) for n in names if (path / n).exists()), default=0)
    except OSError:
        last = 0

    return {
        "name": path.name,
        "path": str(path),
        "langs": [EXT_LANG[e] for e, _ in exts.most_common(3)],
        "n_files": file_count,
        "has_notebook": has_notebook,
        "is_git": (path / ".git").exists(),
        "remote": remote,
        "mtime": last,
    }


def scan_projects(root: Path) -> list[dict]:
    out: list[dict] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and len(out) < MAX_PROJECTS:
        cur, depth = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except (PermissionError, OSError):
            continue
        names = {e.name for e in entries}
        if names & PROJECT_MARKERS or any(n.endswith(".ipynb") for n in names):
            out.append(_project_describe(cur, names))
            continue
        if depth < MAX_DEPTH:
            for e in entries:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") \
                        and e.name not in SKIP_DIRS:
                    stack.append((Path(e.path), depth + 1))
    return out


def scan_history(paths: list[Path] | None = None, limit: int = MAX_HISTORY) -> list[Signal]:
    """读取 shell 历史，统计命令频次。含敏感词的整行打码。"""
    candidates = paths or [Path.home() / ".bash_history", Path.home() / ".zsh_history"]
    lines: list[str] = []
    used: list[str] = []
    for p in candidates:
        if not p.exists():
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                chunk = f.readlines()[-limit:]
            lines.extend(chunk)
            used.append(str(p))
        except OSError:
            continue
    if not lines:
        return []

    heads: Counter[str] = Counter()
    redacted = 0
    for line in lines:
        cmd = line.strip()
        if not cmd or cmd.startswith("#"):
            continue
        if SECRET_HINT.search(cmd):
            redacted += 1
            continue
        head = cmd.split()[0] if cmd.split() else ""
        if head and not head.startswith(("-", "/", ".")):
            heads[head] += 1

    out = [
        Signal("history", f"高频命令 {head}", str(n), " / ".join(used),
               weight=min(float(n), 8.0))
        for head, n in heads.most_common(8)
    ]
    if redacted:
        out.append(
            Signal("history", "已打码的敏感行", str(redacted),
                   f"含 password/token/key 等关键词，共 {redacted} 行未参与统计")
        )
    return out


# ── 推断 ────────────────────────────────────────────────────
def infer(signals: list[Signal]) -> list[Hypothesis]:
    """把信号映射成假设。

    计分方式：每条规则先看有哪些信号命中，按**来源可信度**加权求和，
    再乘以规则本身的权重。证据按可信度降序展示——先给最硬的证据。
    """
    hypotheses: list[Hypothesis] = []
    for label, rules in HYPOTHESIS_RULES:
        score = 0.0
        evidence: list[tuple[float, str]] = []
        for pattern, rule_weight, why in rules:
            matched = [
                s for s in signals
                if re.search(pattern, f"{s.name} {s.value} {s.evidence}", flags=re.I)
            ]
            if not matched:
                continue
            weight_sum = sum(SOURCE_WEIGHT.get(s.kind, 0.3) for s in matched)
            score += rule_weight * min(weight_sum, 4.0) / 2.0
            best = max(matched, key=lambda s: SOURCE_WEIGHT.get(s.kind, 0.3))
            evidence.append(
                (SOURCE_WEIGHT.get(best.kind, 0.3), f"{why} → {best.evidence[:110]}")
            )
        if score > 0:
            evidence.sort(key=lambda x: -x[0])
            hypotheses.append(
                Hypothesis(label, round(score, 2), [e for _, e in evidence][:5])
            )
    hypotheses.sort(key=lambda h: -h.score)
    return hypotheses


def build_profile(
    roots: list[Path] | None = None,
    include_history: bool = False,
) -> HostProfile:
    roots = roots or [Path.home()]
    signals: list[Signal] = []
    signals += scan_session()
    signals += scan_tools()
    signals += scan_processes()

    projects = scan_projects(roots[0])
    for p in projects:
        langs = "/".join(p["langs"]) or "未知"
        signals.append(
            Signal(
                "project", p["name"],
                f"{langs}｜{p['n_files']} 文件" + ("｜notebook" if p["has_notebook"] else ""),
                p["path"] + (f"｜{p['remote']}" if p["remote"] else ""),
                weight=2.0,
            )
        )

    if include_history:
        signals += scan_history()

    hypotheses = infer(signals)
    notes: list[str] = []

    total = sum(h.score for h in hypotheses) or 1.0
    confidence = round(hypotheses[0].score / total, 2) if hypotheses else 0.0

    if len(projects) >= MAX_PROJECTS:
        notes.append(f"项目数触达上限 {MAX_PROJECTS}，未覆盖全部目录")
    if not include_history:
        notes.append("未读取 shell 历史（加 --include-history 可纳入，敏感行会自动打码）")
    if len([p for p in projects]) < 3:
        notes.append("识别到的项目数偏少，推断置信度有限")

    top = hypotheses[0].label if hypotheses else "未识别出明确方向"
    second = hypotheses[1].label if len(hypotheses) > 1 else None
    langs = Counter(l for p in projects for l in p["langs"]).most_common(3)
    lang_txt = "、".join(f"{n}({c})" for n, c in langs) or "未知"

    summary = f"这台机器主要用于「{top}」"
    if second:
        summary += f"，其次涉及「{second}」"
    summary += f"。扫描到 {len(projects)} 个项目，主要语言栈 {lang_txt}。"

    return HostProfile(
        hostname=socket.gethostname(),
        os_name=f"{platform.system()} {platform.release()}",
        session=os.environ.get("XDG_SESSION_TYPE", "unknown"),
        scanned_at=utc_now(),
        signals=signals,
        hypotheses=hypotheses,
        summary=summary,
        confidence=confidence,
        notes=notes,
    )


# ── 持久化 ──────────────────────────────────────────────────
def save_profile(conn, profile: HostProfile) -> int:
    cur = conn.execute(
        """INSERT INTO host_profile(scanned_at, hostname, os_name, session, summary, confidence, raw_json)
           VALUES (?,?,?,?,?,?,?)""",
        (profile.scanned_at, profile.hostname, profile.os_name, profile.session,
         profile.summary, profile.confidence, profile.to_json()),
    )
    pid = int(cur.lastrowid)
    conn.executemany(
        """INSERT INTO host_signals(profile_id, kind, name, value, evidence, weight)
           VALUES (?,?,?,?,?,?)""",
        [(pid, s.kind, s.name, s.value[:300], s.evidence[:400], s.weight) for s in profile.signals],
    )
    conn.commit()
    return pid


def load_latest(conn) -> dict | None:
    cur = conn.execute("SELECT * FROM host_profile ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    data = dict(row)
    cur = conn.execute(
        "SELECT * FROM host_signals WHERE profile_id=? ORDER BY weight DESC, id",
        (data["id"],),
    )
    data["signals"] = [dict(r) for r in cur.fetchall()]
    try:
        data["parsed"] = json.loads(data["raw_json"])
    except (json.JSONDecodeError, TypeError):
        data["parsed"] = {}
    return data
