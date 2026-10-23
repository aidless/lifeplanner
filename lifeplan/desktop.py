"""桌面应用控制：启动应用、枚举窗口、模拟输入。

实现策略是**能做什么就诚实地做什么，做不到就给出可执行的补救指令**，
而不是假装支持然后静默失败。

当前环境的实测结论（Ubuntu 24.04 / Wayland）：
    gio          可用 → 能启动任意已安装应用
    dbus         可用 → 能查会话总线
    xdotool      缺失 → 窗口聚焦 / 键鼠模拟不可用
    wmctrl       缺失 → 窗口枚举不可用

因此：
    launch()          可用（经门禁，tier 2 需确认）
    list_apps()       可用
    list_windows()    不可用，返回明确原因 + 补救命令
    type_text() 等    不可用，同上

不擅自 apt install：装系统包会改动系统状态，属于用户的决定，不属于本次任务范围。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .terminal import RunResult, run

APP_DIRS = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path.home() / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    Path("/var/lib/snapd/desktop/applications"),
]

# 可用的窗口级控制后端
WINDOW_BACKENDS = {
    "xdotool": "sudo apt install xdotool",
    "wmctrl": "sudo apt install wmctrl",
}


@dataclass
class App:
    name: str
    exec_line: str
    desktop_file: str
    categories: str = ""
    terminal: bool = False

    @property
    def argv(self) -> list[str]:
        """把 Exec 拆成 argv，剥掉 %U %F 这类占位符。"""
        parts = [p for p in self.exec_line.split() if not p.startswith("%")]
        # 去掉 env VAR=val 前缀
        while parts and (parts[0] == "env" or "=" in parts[0] and parts[0].split("=")[0].isupper()):
            parts.pop(0)
        return parts


def _parse_desktop(path: Path) -> App | None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    name = exec_line = categories = ""
    terminal = False
    no_display = False
    in_entry = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_entry = s == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in s or s.startswith("#"):
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        if key == "Name" and not name:
            name = val.strip()
        elif key == "Exec" and not exec_line:
            exec_line = val.strip()
        elif key == "Categories":
            categories = val.strip()
        elif key == "NoDisplay":
            no_display = val.strip().lower() == "true"
        elif key == "Terminal":
            terminal = val.strip().lower() == "true"
    if not name or not exec_line or no_display:
        return None
    return App(name, exec_line, str(path), categories, terminal)


def list_apps(limit: int = 0) -> list[App]:
    """枚举已安装的图形应用（去重，按名称排序）。"""
    seen: dict[str, App] = {}
    for d in APP_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.desktop")):
            app = _parse_desktop(f)
            if app and app.name not in seen:
                seen[app.name] = app
    apps = sorted(seen.values(), key=lambda a: a.name.lower())
    return apps[:limit] if limit else apps


def find_app(query: str) -> App | None:
    q = query.strip().lower()
    apps = list_apps()
    # 先精确、再前缀、再子串
    for a in apps:
        if a.name.lower() == q:
            return a
    for a in apps:
        if a.name.lower().startswith(q):
            return a
    for a in apps:
        if q in a.name.lower() or q in Path(a.desktop_file).stem.lower():
            return a
    return None


def launch(query: str, token: str | None = None, conn=None) -> tuple[RunResult | None, str]:
    """启动应用。返回 (执行结果, 说明)。

    优先 `gio launch`（走桌面环境的标准启动路径，能正确处理单实例与启动通知）；
    失败则回退到解析 Exec 自行拉起。
    """
    app = find_app(query)
    if app is None:
        return None, f"未找到应用「{query}」。用 `lifeplan apps list` 查看可用应用。"

    if shutil.which("gio"):
        res = run(["gio", "launch", app.desktop_file], token=token,
                  rationale=f"启动应用 {app.name}", conn=conn)
        if res.ok:
            return res, f"已通过 gio 启动 {app.name}"
        # 门禁拦下时不要回退，避免绕过确认
        if res.status in ("blocked", "denied"):
            return res, f"启动 {app.name} 需确认"
        # 真正执行失败才回退
        argv = app.argv
        if not argv:
            return res, f"gio 启动失败，且无法解析 {app.name} 的 Exec"
        res2 = run(argv, token=token, rationale=f"回退直接启动 {app.name}", conn=conn)
        return res2, f"gio 失败，已回退到 Exec：{' '.join(argv[:3])}..."

    argv = app.argv
    if not argv:
        return None, f"无法解析 {app.name} 的 Exec 字段"
    res = run(argv, token=token, rationale=f"启动应用 {app.name}", conn=conn)
    return res, f"已通过 Exec 启动 {app.name}"


def window_backend() -> str | None:
    for tool in WINDOW_BACKENDS:
        if shutil.which(tool):
            return tool
    return None


def list_windows() -> tuple[list[dict], str]:
    """枚举窗口。返回 (窗口列表, 说明)。没有后端时返回空列表与补救指令。"""
    backend = window_backend()
    if backend is None:
        hints = "；".join(f"{k} → {v}" for k, v in WINDOW_BACKENDS.items())
        return [], f"未安装窗口控制后端，无法枚举窗口。可安装：{hints}"
    if backend == "wmctrl":
        res = run(["wmctrl", "-l"], rationale="枚举窗口")
        if not res.ok:
            return [], f"wmctrl 执行失败：{res.stderr.strip() or res.status}"
        wins = []
        for line in res.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 4:
                wins.append({"id": parts[0], "desktop": parts[1], "host": parts[2], "title": parts[3]})
        return wins, f"wmctrl 枚举到 {len(wins)} 个窗口"
    res = run(["xdotool", "search", "--name", ""], rationale="枚举窗口")
    if not res.ok:
        return [], f"xdotool 执行失败：{res.stderr.strip() or res.status}"
    ids = [i for i in res.stdout.split() if i.strip()]
    return [{"id": i, "desktop": "?", "host": "?", "title": "?"} for i in ids], \
        f"xdotool 枚举到 {len(ids)} 个窗口（无标题，建议装 wmctrl）"


def _require_xdotool() -> str | None:
    if shutil.which("xdotool"):
        return None
    return ("缺少 xdotool，键盘 / 鼠标模拟不可用。安装：sudo apt install xdotool "
            "（Wayland 会话下还需 ydotool 才能在原生 Wayland 窗口生效）")


def focus_window(window_id: str, token: str | None = None, conn=None) -> tuple[RunResult | None, str]:
    missing = _require_xdotool()
    if missing:
        return None, missing
    res = run(["xdotool", "windowactivate", window_id], token=token,
              rationale=f"聚焦窗口 {window_id}", conn=conn)
    return res, "已聚焦" if res.ok else f"聚焦失败：{res.stderr.strip() or res.status}"


def type_text(text: str, token: str | None = None, conn=None) -> tuple[RunResult | None, str]:
    """向当前焦点窗口输入文本。"""
    missing = _require_xdotool()
    if missing:
        return None, missing
    res = run(["xdotool", "type", "--delay", "20", text], token=token,
              rationale="模拟键盘输入", conn=conn)
    return res, "已输入" if res.ok else f"输入失败：{res.stderr.strip() or res.status}"


def send_key(combo: str, token: str | None = None, conn=None) -> tuple[RunResult | None, str]:
    """发送组合键，例如 ctrl+s。"""
    missing = _require_xdotool()
    if missing:
        return None, missing
    res = run(["xdotool", "key", combo], token=token,
              rationale=f"发送组合键 {combo}", conn=conn)
    return res, "已发送" if res.ok else f"发送失败：{res.stderr.strip() or res.status}"


def capabilities() -> dict[str, bool]:
    """本机实际可用的控制能力——用来避免"文档说有、实际没有"。"""
    return {
        "launch_app": bool(shutil.which("gio") or list_apps(1)),
        "list_apps": bool(list_apps(1)),
        "list_windows": window_backend() is not None,
        "focus_window": bool(shutil.which("xdotool")),
        "type_text": bool(shutil.which("xdotool")),
        "send_key": bool(shutil.which("xdotool")),
        "screenshot": any(shutil.which(t) for t in ("gnome-screenshot", "scrot", "import")),
    }


def capability_report() -> str:
    caps = capabilities()
    labels = {
        "launch_app": "启动应用",
        "list_apps": "枚举应用",
        "list_windows": "枚举窗口",
        "focus_window": "聚焦窗口",
        "type_text": "模拟输入",
        "send_key": "发送按键",
        "screenshot": "截屏",
    }
    lines = []
    for key, label in labels.items():
        ok = caps[key]
        lines.append(f"  {'✓' if ok else '✗'} {label:<8} {'可用' if ok else '不可用'}")
    if not caps["focus_window"]:
        lines.append("\n  补全键鼠控制：sudo apt install xdotool wmctrl")
    return "\n".join(lines)


def session_info() -> dict[str, str]:
    return {
        "session_type": os.environ.get("XDG_SESSION_TYPE", "unknown"),
        "display": os.environ.get("DISPLAY", ""),
        "wayland": os.environ.get("WAYLAND_DISPLAY", ""),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
    }
