"""能力门禁：把「控制电脑」拆成可审计的分级权限。

为什么不做「完全无限制的控制」
────────────────────────────
无论怎么过滤命令行，最终都绕不开一个事实：**任何解释器都是万能逃逸口**
（`python -c`、`bash -c`、`node -e` 都能做任何事）。所以「用黑名单挡住危险命令」
本身不是安全边界，只是降低手滑概率的护栏。

真正能站得住的边界只有三条，本模块把它们做成硬性约束：

  1. **分级 + 强制确认**：有副作用的动作必须由人看过命令原文后回填一次性令牌；
  2. **全量审计**：每个动作连同分级理由一起落库，事后可查、可复盘；
  3. **默认拒绝破坏性**：tier 3 默认不放行，需要显式改策略才可能执行。

分级：
    tier 0  观察      只读命令（ls / cat / git status …），自动放行
    tier 1  工作区写  在工作区内创建文件（mkdir / touch / cp），记日志后放行
    tier 2  执行      可能产生外部影响（curl / git commit / 启动应用 / 跑脚本），需确认
    tier 3  破坏性    删除、覆盖、提权、装包、解释器内联代码，需确认 + 显式开启
    tier 4  禁止      提权与改系统本身（sudo / mkfs / dd / systemctl …），永不自动放行

⚠️ 边界声明：本模块**不是**沙箱。它能约束的是"经由本系统发起的动作"，
不能阻止用户自己在终端里做任何事，也不防恶意代码在进程内自行提权。
把不受信任的代码跑给它，等于放弃这层保护。
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path

# ── 分级常量 ────────────────────────────────────────────────
TIER_OBSERVE = 0
TIER_WORKSPACE = 1
TIER_EXECUTE = 2
TIER_DESTRUCTIVE = 3
TIER_FORBIDDEN = 4

TIER_LABEL = {
    TIER_OBSERVE: "观察（只读）",
    TIER_WORKSPACE: "工作区写入",
    TIER_EXECUTE: "执行（有外部影响）",
    TIER_DESTRUCTIVE: "破坏性",
    TIER_FORBIDDEN: "禁止",
}

# 只读命令：可自动放行
READ_ONLY_BIN = {
    "ls", "pwd", "cat", "head", "tail", "wc", "less", "more", "file", "stat",
    "which", "whereis", "type", "env", "printenv", "uname", "uptime", "whoami",
    "id", "date", "cal", "df", "du", "free", "ps", "pgrep", "top", "hostname",
    "echo", "printf", "tree", "jq", "diff", "cmp", "md5sum", "sha256sum",
    "nproc", "lscpu", "lsblk", "locale", "tty", "groups", "getent",
    "find", "grep", "sort", "uniq", "cut", "tr", "basename", "dirname", "seq",
}
# 刻意不放进只读集合：
#   sed   —— `-i` 会原地改写文件
#   awk   —— 内置 system() 可执行任意命令

# 工作区创建类：允许在工作区内执行
WORKSPACE_BIN = {"mkdir", "touch", "cp", "ln", "tar", "zip", "unzip", "tee"}

# 破坏性命令
DESTRUCTIVE_BIN = {
    "rm", "rmdir", "mv", "truncate", "shred", "dd", "chmod", "chown", "chgrp",
    "kill", "pkill", "killall", "xkill", "renice",
    "apt", "apt-get", "dpkg", "snap", "pip", "pip3", "conda", "npm", "pnpm",
    "yarn", "cargo", "gem", "go",
}

# 永远不放行：提权与系统级改动
FORBIDDEN_BIN = {
    "sudo", "su", "doas", "pkexec",
    "mkfs", "mkfs.ext4", "mkfs.xfs", "mkfs.vfat", "wipefs", "fdisk", "parted",
    "mount", "umount", "swapon", "swapoff",
    "insmod", "rmmod", "modprobe",
    "systemctl", "service", "init", "telinit", "reboot", "shutdown", "poweroff",
    "passwd", "chpasswd", "usermod", "useradd", "userdel", "groupadd", "visudo",
    "chroot", "unshare", "nsenter", "iptables", "nft", "ufw", "auditctl",
}

# 解释器：带内联代码参数时降级为破坏性（万能逃逸口）
INTERPRETERS = {
    "python", "python3", "bash", "sh", "zsh", "dash", "node", "deno", "perl",
    "ruby", "php", "lua", "Rscript",
}
INLINE_CODE_FLAGS = {"-c", "-e", "--eval", "-Command", "-EncodedCommand"}

# 只读命令里混入破坏性参数
DESTRUCTIVE_FLAGS = {
    "find": {"-delete", "-exec", "-execdir", "-ok"},
    "git": {"reset", "clean", "push", "filter-branch"},
    "tar": {"-x", "--extract", "-C"},
    "sed": {"-i", "--in-place"},          # 原地改写文件
    "sort": {"-o", "--output"},
}

# git 的只读子命令（不含 config —— 它可能写入）
GIT_READONLY_SUB = {
    "status", "log", "diff", "show", "branch", "remote", "tag", "rev-parse",
    "rev-list", "ls-files", "ls-tree", "shortlog", "blame", "describe", "whatchanged",
}

DEFAULT_POLICY_FILE = "policy.json"


@dataclass
class Policy:
    """一份可审计的策略。默认保守：破坏性关、执行需确认。"""

    allow_observe: bool = True
    allow_workspace_write: bool = True
    allow_execute: bool = False          # tier 2 是否免确认（默认否）
    allow_destructive: bool = False      # tier 3 是否可执行（默认否）
    allow_shell_wrapper: bool = False    # 是否允许 bash -c 这类包装
    workspace_jail: list[str] = field(default_factory=lambda: [str(Path.home())])
    timeout_seconds: int = 30
    max_output_chars: int = 20000

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def policy_path() -> Path:
    from .db import home_dir

    return home_dir() / DEFAULT_POLICY_FILE


def load_policy(create: bool = True) -> Policy:
    path = policy_path()
    if path.exists():
        try:
            return Policy.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            pass
    pol = Policy()
    if create:
        save_policy(pol)
    return pol


def save_policy(pol: Policy) -> Path:
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(pol.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


@dataclass
class Verdict:
    """分级结果。

    allowed 与 requires_confirm 必须分开：
        allowed=False          → 策略层面永不放行（FORBIDDEN，或该 tier 未启用）
        allowed=True, req=True → 需要人工回填一次性令牌
        allowed=True, req=False→ 直接执行并记账

    早期版本把两者压成一个布尔字段，取反后正好把 `rm` 放行了 —— 这种字段语义
    混淆在有副作用的代码里是致命的，所以显式拆成两维。
    """

    tier: int
    reason: str
    allowed: bool
    requires_confirm: bool

    @property
    def label(self) -> str:
        return TIER_LABEL.get(self.tier, f"未知({self.tier})")


def _is_inside(path: str, roots: list[str]) -> bool:
    try:
        target = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for root in roots:
        try:
            base = Path(root).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if target == base or base in target.parents:
            return True
    return False


def classify(argv: list[str], cwd: str | None = None, pol: Policy | None = None) -> Verdict:
    """给一条命令定级。返回分级与理由，不执行任何东西。"""
    pol = pol or load_policy(create=False)
    if not argv:
        return Verdict(TIER_FORBIDDEN, "空命令", False, False)

    binary = os.path.basename(argv[0])
    args = argv[1:]

    # 1) 永久禁止
    if binary in FORBIDDEN_BIN:
        return Verdict(
            TIER_FORBIDDEN,
            f"{binary} 涉及提权或系统级改动，本系统永不放行",
            allowed=False, requires_confirm=False,
        )

    # 2) 解释器内联代码：万能逃逸口
    if binary in INTERPRETERS and any(a in INLINE_CODE_FLAGS for a in args):
        if not pol.allow_shell_wrapper:
            return Verdict(
                TIER_FORBIDDEN,
                f"{binary} 内联代码等价于任意执行，默认禁止（改策略 allow_shell_wrapper 可开启）",
                allowed=False, requires_confirm=False,
            )
        return Verdict(
            TIER_DESTRUCTIVE, f"{binary} 内联代码",
            allowed=pol.allow_destructive, requires_confirm=True,
        )

    # 3) rm 的危险目标
    if binary == "rm":
        dangerous = [
            a for a in args
            if a in ("/", "/*", "~", "~/*", "*") or a.startswith(("/etc", "/usr", "/boot", "/var"))
        ]
        if dangerous:
            return Verdict(
                TIER_FORBIDDEN,
                f"rm 目标涉及系统路径或通配根：{dangerous[:3]}",
                allowed=False, requires_confirm=False,
            )

    # 4) 破坏性命令
    if binary in DESTRUCTIVE_BIN:
        return Verdict(
            TIER_DESTRUCTIVE,
            f"{binary} 属于删除/覆盖/装包/终止进程类",
            allowed=pol.allow_destructive, requires_confirm=True,
        )

    # 5) 只读命令里混入破坏性参数
    if binary in DESTRUCTIVE_FLAGS:
        hit = DESTRUCTIVE_FLAGS[binary] & set(args)
        if hit:
            return Verdict(
                TIER_DESTRUCTIVE,
                f"{binary} 带破坏性参数 {sorted(hit)}",
                allowed=pol.allow_destructive, requires_confirm=True,
            )

    # 5.5) git 只读子命令：避免每次查状态都要确认
    if binary == "git" and args and args[0] in GIT_READONLY_SUB:
        return Verdict(
            TIER_OBSERVE, f"git {args[0]} 为只读子命令",
            allowed=pol.allow_observe, requires_confirm=False,
        )

    # 6) 工作区创建类：必须落在 jail 内
    if binary in WORKSPACE_BIN:
        target = cwd or os.getcwd()
        if _is_inside(target, pol.workspace_jail):
            return Verdict(
                TIER_WORKSPACE,
                f"{binary} 作用于工作区内 {target}",
                allowed=pol.allow_workspace_write, requires_confirm=False,
            )
        return Verdict(
            TIER_EXECUTE,
            f"{binary} 作用于工作区外 {target}",
            allowed=True, requires_confirm=True,
        )

    # 7) 只读
    if binary in READ_ONLY_BIN:
        return Verdict(
            TIER_OBSERVE, f"{binary} 为只读命令",
            allowed=pol.allow_observe, requires_confirm=False,
        )

    # 8) 其余一律执行级
    return Verdict(
        TIER_EXECUTE, f"{binary} 未列入已知只读集合",
        allowed=True, requires_confirm=not pol.allow_execute,
    )


def confirm_token(action_id: int, command: str) -> str:
    """一次性确认令牌。

    绑定 action id + 命令原文 + 当天日期：命令被改过、或隔天再试，令牌即失效。
    这样"确认"就是针对具体这一条命令，而不是一个可复用的通行证。
    """
    payload = f"{action_id}\x00{command}\x00{date.today().isoformat()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def quote(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
