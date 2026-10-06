"""
tools.py —— Agent 能对这台电脑做的事情。

设计原则：
  1. 每个工具都是普通 Python 函数，最外层包一层 JSON Schema 暴露给模型。
  2. 所有破坏性动作先过 danger 检查，必要时挂起等手机端确认。
  3. 一切调用写进 data/audit.log，事后可查。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
SHOTS = DATA / "shots"
WORKSPACE = DATA / "workspace"
AUDIT = DATA / "audit.log"
OCR_PS1 = ROOT / "tools" / "ocr.ps1"

for _d in (DATA, UPLOADS, SHOTS, WORKSPACE):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 运行期状态

class Runtime:
    """跨模块共享的运行状态：急停开关、确认回调、事件发布、工作目录。"""

    def __init__(self) -> None:
        self.stop_flag = False
        self.work_dir: Path = WORKSPACE
        self.publish: Callable[[dict], None] = lambda ev: None
        self.request_confirm: Callable[[str, str], bool] = lambda kind, detail: False
        self.dry_run = False

    def reset_stop(self) -> None:
        self.stop_flag = False

    def check_stop(self) -> None:
        if self.stop_flag:
            raise InterruptedError("用户点了急停，任务已中止")

    def log(self, action: str, detail: Any, result: str = "ok") -> None:
        try:
            with AUDIT.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "action": action,
                    "detail": detail,
                    "result": result,
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass


RT = Runtime()

# ---------------------------------------------------------------- 危险判定

# 命中即需要手机端点头。宁可多问，不可误删。
DANGER_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+-rf\b", "递归强制删除"),
    (r"\brmdir\s+/s\b", "递归删目录"),
    (r"\bdel\s+/[sq]\b", "递归删文件"),
    (r"\bformat\b", "格式化磁盘"),
    (r"\bformat-volume\b", "格式化磁盘"),
    (r"\bdiskpart\b", "磁盘分区操作"),
    (r"\bshutdown\b", "关机/重启"),
    (r"\brestart-computer\b", "重启电脑"),
    (r"\bstop-computer\b", "关机"),
    (r"\bmkfs\b", "格式化文件系统"),
    (r"\bdd\s+if=", "裸设备写入"),
    (r"remove-item.*-recurse.*-force", "递归强删"),
    (r"reg\s+delete\b", "删除注册表"),
    (r"\btaskkill\b.*?/f\b", "强杀进程"),
    (r"set-mppreference.*disable", "关闭杀毒防护"),
    (r"\bnet\s+user\b.*?/add\b", "新建系统账号"),
    (r"vssadmin.*delete", "删除卷影副本"),
    (r"\bcipher\s+/w\b", "擦除磁盘剩余空间"),
]

# 这些目录之外写文件要问一声？不，太吵。只挡系统盘关键位置。
PROTECTED_PREFIXES = [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\Users\Default",
]


def _danger_reason(text: str) -> str | None:
    low = text.lower()
    for pat, why in DANGER_PATTERNS:
        if re.search(pat, low):
            return why
    return None


def _protected_reason(path: str) -> str | None:
    try:
        p = str(Path(path).resolve())
    except Exception:
        return None
    for pref in PROTECTED_PREFIXES:
        if p.lower().startswith(pref.lower()):
            return f"写系统目录 {pref}"
    return None


def _gate(kind: str, detail: str) -> None:
    """危险动作闸门：dry_run 直接拦，否则问手机端。"""
    RT.check_stop()
    if RT.dry_run:
        raise PermissionError(f"[演练模式] 拦下了危险动作：{detail}")
    RT.publish({"type": "confirm_pending", "kind": kind, "detail": detail})
    ok = RT.request_confirm(kind, detail)
    if not ok:
        RT.log("gate", detail, "denied")
        raise PermissionError("用户在手机上拒绝了这次操作")


# ---------------------------------------------------------------- 基础工具

def _truncate(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n\n...（中间省略 {len(text) - limit} 字）...\n\n{tail}"


def run_powershell(command: str, timeout: int = 60) -> str:
    """在当前用户的 Windows 上执行一条 PowerShell 命令。"""
    RT.check_stop()
    reason = _danger_reason(command)
    if reason:
        _gate("命令", f"{reason}：{command}")

    RT.publish({"type": "tool", "name": "run_powershell", "detail": command})
    RT.log("run_powershell", command)

    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            timeout=timeout,
            cwd=str(RT.work_dir),
        )
    except subprocess.TimeoutExpired:
        return f"[超时] 命令 {timeout} 秒内没跑完：{command}"

    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    parts = [f"[退出码] {proc.returncode}"]
    if out:
        parts.append(f"[输出]\n{_truncate(out)}")
    if err:
        parts.append(f"[报错]\n{_truncate(err)}")
    if not out and not err:
        parts.append("（没有任何输出）")
    return "\n".join(parts)


def _inside_workspace(path: Path) -> bool:
    try:
        path.resolve().relative_to(RT.work_dir.resolve())
        return True
    except Exception:
        return False


def read_file(path: str, max_chars: int = 20000) -> str:
    """读取一个文本文件的内容。"""
    RT.check_stop()
    p = Path(path).expanduser()
    if not p.exists():
        return f"[错误] 文件不存在：{p}"
    if p.is_dir():
        return f"[错误] {p} 是目录，请用 list_dir"
    try:
        raw = p.read_bytes()
    except Exception as e:
        return f"[错误] 读不了 {p}：{e}"
    if len(raw) > 2_000_000:
        return f"[错误] 文件太大（{len(raw)} 字节），超过 2MB 不读"
    text = raw.decode("utf-8", errors="replace")
    RT.log("read_file", str(p))
    return f"[{p} 共 {len(raw)} 字节]\n{_truncate(text, max_chars)}"


def write_file(path: str, content: str, append: bool = False) -> str:
    """把内容写入文件，自动创建上级目录。append=True 时追加。"""
    RT.check_stop()
    p = Path(path).expanduser()
    if not _inside_workspace(p):
        reason = _protected_reason(str(p)) or f"工作目录之外（{RT.work_dir}）"
        _gate("写文件", f"{reason}：{p}")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if append else "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return f"[错误] 写不了 {p}：{e}"
    RT.publish({"type": "tool", "name": "write_file", "detail": str(p)})
    RT.log("write_file", f"{p} ({len(content)} 字)")
    return f"[成功] {'追加' if append else '写入'} {len(content)} 字到 {p}"


def list_dir(path: str = "") -> str:
    """列出目录内容。不传路径就列工作目录。"""
    RT.check_stop()
    p = Path(path).expanduser() if path else RT.work_dir
    if not p.exists():
        return f"[错误] 目录不存在：{p}"
    if not p.is_dir():
        return f"[错误] {p} 不是目录"
    rows = []
    try:
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except Exception as e:
        return f"[错误] 打不开 {p}：{e}"
    for e in entries[:200]:
        try:
            size = "" if e.is_dir() else f"{e.stat().st_size:>10,}"
            rows.append(f"{'<目录>' if e.is_dir() else size}  {e.name}")
        except Exception:
            rows.append(f"{'?':>10}  {e.name}")
    RT.log("list_dir", str(p))
    return f"[{p} 共 {len(entries)} 项]\n" + "\n".join(rows)


def screenshot(region: str = "") -> str:
    """给电脑屏幕拍张照，返回图片路径（手机端会直接显示出来）。"""
    RT.check_stop()
    import pyautogui

    box = None
    if region:
        try:
            nums = [int(x) for x in re.findall(r"-?\d+", region)[:4]]
            if len(nums) == 4:
                box = tuple(nums)  # type: ignore[assignment]
        except Exception:
            box = None

    img = pyautogui.screenshot(region=box)
    name = f"shot_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}.png"
    path = SHOTS / name
    img.save(path)
    size = pyautogui.size()
    RT.publish({"type": "image", "url": f"/file/shots/{name}", "label": "电脑屏幕"})
    RT.log("screenshot", str(path))
    return (
        f"[截图已保存] {path}\n屏幕分辨率 {size.width}x{size.height}。"
        f"你可以用 ocr_image 读取上面的文字，或继续下一步操作。"
    )


CJK = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"


def _fix_cjk_spaces(text: str) -> str:
    """Windows OCR 会在汉字之间插空格，这里去掉。"""
    text = re.sub(rf"(?<=[{CJK}])\s+(?=[{CJK}])", "", text)
    text = re.sub(rf"(?<=[{CJK}])\s+(?=[，。！？、；：（）【】])", "", text)
    text = re.sub(rf"(?<=[，。！？、；：（）【】])\s+(?=[{CJK}])", "", text)
    return text


def ocr_image(path: str) -> str:
    """用 Windows 自带 OCR 识别图片里的文字（支持中文）。"""
    RT.check_stop()
    p = Path(path).expanduser()
    if not p.exists():
        return f"[错误] 图片不存在：{p}"
    if not OCR_PS1.exists():
        return "[错误] 缺少 tools/ocr.ps1，OCR 功能不可用"

    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(OCR_PS1), "-Path", str(p)],
            capture_output=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return "[错误] OCR 超过 90 秒没出结果"

    raw = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    line = raw.splitlines()[-1] if raw else ""
    try:
        payload = json.loads(line)
    except Exception:
        return f"[错误] OCR 输出无法解析：{_truncate(raw, 800)}"

    if not payload.get("ok"):
        return f"[错误] OCR 失败：{payload}"

    lines = [_fix_cjk_spaces(x) for x in payload.get("lines", [])]
    text = "\n".join(lines).strip()
    RT.log("ocr_image", str(p))
    if not text:
        return f"[OCR 完成] 这张图里没有识别到文字。（语言 {payload.get('language')}）"
    return f"[OCR 结果 · {payload.get('language')}]\n{text}"


# ---------------------------------------------------------------- 鼠标键盘

def _pyautogui():
    RT.check_stop()
    import pyautogui
    pyautogui.FAILSAFE = True   # 鼠标甩到左上角 = 立刻中止
    pyautogui.PAUSE = 0.15
    return pyautogui


def mouse(action: str, x: int = 0, y: int = 0, button: str = "left", clicks: int = 1,
          amount: int = 0) -> str:
    """
    操作鼠标。
    action: move / click / double_click / right_click / scroll
    """
    pg = _pyautogui()
    RT.publish({"type": "tool", "name": f"mouse.{action}", "detail": f"({x},{y})"})
    RT.log("mouse", f"{action} {x},{y} button={button}")

    if action == "move":
        pg.moveTo(x, y, duration=0.25)
        return f"[鼠标] 移动到 ({x}, {y})"
    if action == "click":
        pg.click(x, y, clicks=clicks, button=button, duration=0.2)
        return f"[鼠标] 在 ({x}, {y}) {'单击' if button == 'left' else button}"
    if action == "double_click":
        pg.doubleClick(x, y, button=button)
        return f"[鼠标] 在 ({x}, {y}) 双击"
    if action == "right_click":
        pg.rightClick(x, y)
        return f"[鼠标] 在 ({x}, {y}) 右键"
    if action == "scroll":
        pg.scroll(amount)
        return f"[鼠标] 滚轮 {amount}"
    return f"[错误] 不认识的鼠标动作：{action}"


def type_text(text: str, press_enter: bool = False) -> str:
    """
    在当前光标处输入文字。中文会走剪贴板粘贴，所以能打中文。
    """
    pg = _pyautogui()
    RT.publish({"type": "tool", "name": "type_text", "detail": text[:60]})
    RT.log("type_text", text[:200])

    if text.isascii():
        pg.typewrite(text, interval=0.02)
    else:
        # pyautogui 打不了中文，用剪贴板
        tmp = DATA / "_clip.txt"
        tmp.write_text(text, encoding="utf-8")
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"Get-Content -LiteralPath '{tmp}' -Raw -Encoding UTF8 | Set-Clipboard"],
            capture_output=True, timeout=20,
        )
        time.sleep(0.2)
        pg.hotkey("ctrl", "v")
        time.sleep(0.3)
        try:
            tmp.unlink()
        except Exception:
            pass

    if press_enter:
        pg.press("enter")
    return f"[键盘] 输入了 {len(text)} 个字符" + ("（并回车）" if press_enter else "")


def press_keys(keys: str) -> str:
    """按快捷键，比如 'ctrl+c'、'alt+tab'、'win+d'、'enter'。"""
    pg = _pyautogui()
    RT.check_stop()
    combo = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
    if not combo:
        return "[错误] 没给按键"
    RT.publish({"type": "tool", "name": "press_keys", "detail": keys})
    RT.log("press_keys", keys)
    if len(combo) == 1:
        pg.press(combo[0])
    else:
        pg.hotkey(*combo)
    return f"[键盘] 按下了 {keys}"


# ---------------------------------------------------------------- 程序与窗口

def open_app(target: str, args: str = "") -> str:
    """打开程序、文件或文件夹。target 可以是程序名（notepad）、完整路径或网址。"""
    RT.check_stop()
    RT.publish({"type": "tool", "name": "open_app", "detail": target})
    RT.log("open_app", target)

    if re.match(r"^https?://", target, re.I):
        return open_url(target)

    p = Path(target).expanduser()
    try:
        if p.exists():
            os.startfile(str(p))  # type: ignore[attr-defined]
            return f"[已打开] {p}"
    except Exception:
        pass

    try:
        cmd = f'Start-Process "{target}"'
        if args:
            cmd += f" -ArgumentList '{args}'"
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", cmd],
            capture_output=True, timeout=25,
        )
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return f"[失败] 打不开 {target}：{_truncate(err, 500)}"
        time.sleep(1.0)
        return f"[已打开] {target}"
    except Exception as e:
        return f"[失败] 打不开 {target}：{e}"


def open_url(url: str) -> str:
    """用默认浏览器打开一个网址。"""
    RT.check_stop()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    webbrowser.open(url)
    RT.log("open_url", url)
    return f"[已用浏览器打开] {url}"


def list_windows() -> str:
    """列出当前打开的窗口标题，用来判断现在屏幕上有什么。"""
    RT.check_stop()
    try:
        import pygetwindow as gw
    except Exception:
        return "[错误] 没有 pygetwindow，装一下：pip install pygetwindow"

    titles = []
    try:
        for w in gw.getAllWindows():
            t = (w.title or "").strip()
            if t:
                titles.append(f"{t}  [{w.width}x{w.height} @ {w.left},{w.top}]")
    except Exception as e:
        return f"[错误] 读不到窗口列表：{e}"
    RT.log("list_windows", len(titles))
    if not titles:
        return "（当前没有可见窗口）"
    return f"[共 {len(titles)} 个窗口]\n" + "\n".join(titles[:60])


def focus_window(title: str) -> str:
    """按标题关键词把某个窗口切到最前面。"""
    RT.check_stop()
    try:
        import pygetwindow as gw
    except Exception:
        return "[错误] 没有 pygetwindow"

    matches = [w for w in gw.getAllWindows() if title.lower() in (w.title or "").lower()]
    if not matches:
        return f"[没找到] 没有标题含「{title}」的窗口"
    w = matches[0]
    try:
        if w.isMinimized:
            w.restore()
        w.activate()
    except Exception as e:
        return f"[错误] 切不过去：{e}"
    RT.log("focus_window", w.title)
    time.sleep(0.6)
    return f"[已切到前台] {w.title}"


def get_screen_size() -> str:
    """获取屏幕分辨率，做坐标点击前先看这个。"""
    import pyautogui
    s = pyautogui.size()
    RT.log("get_screen_size", f"{s.width}x{s.height}")
    return f"[屏幕] {s.width} x {s.height}（坐标原点在左上角）"


def wait(seconds: float) -> str:
    """等一会儿，给软件反应时间。最多 30 秒。"""
    s = max(0.0, min(float(seconds), 30.0))
    RT.publish({"type": "tool", "name": "wait", "detail": f"{s}s"})
    end = time.time() + s
    while time.time() < end:
        RT.check_stop()
        time.sleep(0.2)
    return f"[等待] {s} 秒"


def remember(note: str) -> str:
    """把一条信息记进长期笔记 data/memory.md，以后每次任务开始都会带上。"""
    RT.check_stop()
    p = DATA / "memory.md"
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.now():%Y-%m-%d %H:%M} {note}\n")
    RT.log("remember", note)
    return f"[已记住] {note}"


def read_memory() -> str:
    """读一下之前记下的笔记。"""
    p = DATA / "memory.md"
    if not p.exists():
        return "（还没有任何笔记）"
    return _truncate(p.read_text(encoding="utf-8"), 4000)


# ---------------------------------------------------------------- 工具注册表

TOOLS: dict[str, dict] = {
    "run_powershell": {
        "fn": run_powershell,
        "desc": "在这台 Windows 电脑上执行 PowerShell 命令。装软件、查信息、批量处理文件都靠它。危险命令会先让用户在手机上确认。",
        "params": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 PowerShell 命令"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 60"},
            },
            "required": ["command"],
        },
    },
    "list_dir": {
        "fn": list_dir,
        "desc": "列出目录内容。不传 path 就列当前工作目录。",
        "params": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "目录路径"}},
        },
    },
    "read_file": {
        "fn": read_file,
        "desc": "读取文本文件内容，用来查看配置、日志、代码。",
        "params": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "max_chars": {"type": "integer", "description": "最多读多少字符，默认 20000"},
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "fn": write_file,
        "desc": "写文件（会覆盖原文件），自动创建上级目录。写工作目录以外的位置需要用户确认。",
        "params": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "写入内容"},
                "append": {"type": "boolean", "description": "true 表示追加而不是覆盖"},
            },
            "required": ["path", "content"],
        },
    },
    "screenshot": {
        "fn": screenshot,
        "desc": "给电脑屏幕截图并显示给用户。想知道屏幕上现在是什么样、或者要看操作结果时用它。",
        "params": {
            "type": "object",
            "properties": {
                "region": {"type": "string", "description": "可选，截图区域 'left,top,width,height'"},
            },
        },
    },
    "ocr_image": {
        "fn": ocr_image,
        "desc": "识别一张图片里的文字（中文英文都行）。用户从手机发来的照片、或刚截的图，都可以用它读出内容。",
        "params": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "图片完整路径"}},
            "required": ["path"],
        },
    },
    "mouse": {
        "fn": mouse,
        "desc": "操作鼠标：移动、点击、双击、右键、滚轮。坐标原点在屏幕左上角。点之前建议先 screenshot 看看位置。",
        "params": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["move", "click", "double_click", "right_click", "scroll"]},
                "x": {"type": "integer", "description": "横坐标"},
                "y": {"type": "integer", "description": "纵坐标"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer"},
                "amount": {"type": "integer", "description": "滚轮格数，正数向上"},
            },
            "required": ["action"],
        },
    },
    "type_text": {
        "fn": type_text,
        "desc": "在当前光标位置输入文字（中文也可以）。写入前请先用 mouse 点一下目标输入框。",
        "params": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的文字"},
                "press_enter": {"type": "boolean", "description": "输完是否回车"},
            },
            "required": ["text"],
        },
    },
    "press_keys": {
        "fn": press_keys,
        "desc": "按快捷键，例如 ctrl+c、ctrl+v、alt+tab、alt+f4、win+d、enter、esc。",
        "params": {
            "type": "object",
            "properties": {"keys": {"type": "string", "description": "按键组合，用 + 连接"}},
            "required": ["keys"],
        },
    },
    "open_app": {
        "fn": open_app,
        "desc": "打开程序、文件或文件夹。例如 notepad、calc、D:\\\\某个文件夹、或一个 exe 路径。",
        "params": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "程序名、路径或网址"},
                "args": {"type": "string", "description": "可选启动参数"},
            },
            "required": ["target"],
        },
    },
    "open_url": {
        "fn": open_url,
        "desc": "用默认浏览器打开网址。",
        "params": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    "list_windows": {
        "fn": list_windows,
        "desc": "列出当前所有窗口标题，用来判断桌面上开着什么。",
        "params": {"type": "object", "properties": {}},
    },
    "focus_window": {
        "fn": focus_window,
        "desc": "按标题关键词把窗口切到最前面。",
        "params": {
            "type": "object",
            "properties": {"title": {"type": "string", "description": "窗口标题的一部分"}},
            "required": ["title"],
        },
    },
    "get_screen_size": {
        "fn": get_screen_size,
        "desc": "获取屏幕分辨率。",
        "params": {"type": "object", "properties": {}},
    },
    "wait": {
        "fn": wait,
        "desc": "等待若干秒，给软件或网页加载时间。",
        "params": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        },
    },
    "remember": {
        "fn": remember,
        "desc": "把值得长期记住的信息写进笔记（比如常用路径、用户偏好），下次任务会自动带上。",
        "params": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    },
    "read_memory": {
        "fn": read_memory,
        "desc": "读取之前记下的长期笔记。",
        "params": {"type": "object", "properties": {}},
    },
}


def openai_tool_schemas() -> list[dict]:
    """转成 OpenAI / DeepSeek 的 tools 格式。"""
    out = []
    for name, spec in TOOLS.items():
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec["desc"],
                "parameters": spec["params"],
            },
        })
    return out


def call_tool(name: str, args: dict) -> str:
    """按名字调工具，永远返回字符串（错误也变成字符串回给模型）。"""
    spec = TOOLS.get(name)
    if not spec:
        return f"[错误] 没有这个工具：{name}"
    RT.check_stop()
    try:
        result = spec["fn"](**args)
        return str(result)
    except InterruptedError:
        raise
    except PermissionError as e:
        return f"[已拒绝] {e}"
    except TypeError as e:
        return f"[参数错误] 工具 {name} 的调用参数不对：{e}"
    except Exception as e:
        RT.log(name, args, f"error: {e}")
        return f"[工具出错] {name}: {type(e).__name__}: {e}"
