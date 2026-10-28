#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机桌面客户端（WorkBuddy / CodeBuddy）的进程管控：探测 / 退出 / 拉起。

为什么要这个模块
----------------
后台「注入本机客户端」的本意是：点一下，本机客户端立刻换成号池里那个账号，
所有窗口的会话都跟着同步。

但桌面端对登录态文件 `workbuddy-desktop.info` 是**启动时读一次**，之后常驻内存；
运行中它还会在刷新 token / 退出时把自己的会话**写回同一个文件**。所以：

    ❌ 先写文件，客户端还在跑   → 界面毫无变化，甚至隔一会儿被客户端旧会话覆盖
    ✅ 先退出客户端 → 等它彻底死透 → 写新凭证 → 再拉起

这个模块负责把上面这条正确顺序里的「进程侧」跑稳：找到客户端装在哪、有没有在跑、
优雅退出、确认真的死了、再拉起来并确认真的起来了。

任何一步失败都如实返回，交给调用方决定是继续还是回滚——不要静默假装成功。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

# 官方客户端的可能安装名（国内版优先，国际版与旧名兜底）
_APP_NAMES = (
    "WorkBuddy-CN.app",
    "WorkBuddy.app",
    "WorkBuddy CN.app",
    "CodeBuddy-CN.app",
    "CodeBuddy.app",
    "workbuddy.app",
)
_WIN_EXE_NAMES = ("WorkBuddy.exe", "WorkBuddy-CN.exe", "CodeBuddy.exe", "CodeBuddy-CN.exe")

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt" or sys.platform.startswith("win")


def _search_dirs() -> list[str]:
    home = os.path.expanduser("~")
    return ["/Applications", os.path.join(home, "Applications"),
            os.path.join(home, ".workbuddy"), os.getcwd()]


def app_install_path(override: str = "") -> str | None:
    """返回本机客户端 app 的安装路径；找不到返回 None。

    `override` 允许运维用环境变量 ADMIN_CLIENT_APP 指定（例如便携版装在别处）。
    """
    if override:
        p = os.path.expanduser(override)
        if os.path.exists(p):
            return p
    for d in _search_dirs():
        for name in _APP_NAMES:
            p = os.path.join(d, name)
            if os.path.isdir(p):
                return p
    if IS_WIN:
        for ev in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.getenv(ev)
            if not base:
                continue
            for name in _WIN_EXE_NAMES:
                p = os.path.join(base, "Programs", name)
                if os.path.isfile(p):
                    return p
    return None


def _ps_rows() -> list[tuple[int, str]]:
    """用 ps / tasklist 列出进程。"""
    if IS_WIN:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10)
        rows = []
        for line in out.stdout.splitlines():
            parts = [x.strip('"') for x in line.split('","')]
            # tasklist CSV 列序：Image Name, PID, Session Name, ...
            if len(parts) >= 2 and parts[0] in _WIN_EXE_NAMES:
                try:
                    rows.append((int(parts[1]), parts[0]))
                except ValueError:
                    continue
        return rows
    return _run_ps(["/bin/ps", "ax", "-o", "pid=,command="], full_cmd=True)


def _run_ps(args: list[str], full_cmd: bool = False) -> list[tuple[int, str]]:
    out = subprocess.run(args, capture_output=True, text=True, timeout=10)
    rows = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_s, _, rest = line.partition(" ")
        try:
            rows.append((int(pid_s), rest.strip()))
        except ValueError:
            continue
    return rows


def _pgrep_rows(pattern: str) -> list[tuple[int, str]]:
    """ps 不可用（某些沙箱会拦 /bin/ps）时的兜底。"""
    out = subprocess.run(["/usr/bin/pgrep", "-fl", pattern],
                         capture_output=True, text=True, timeout=10)
    rows = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_s, _, cmd = line.partition(" ")
        try:
            rows.append((int(pid_s), cmd))
        except ValueError:
            continue
    return rows


def _running_rows(pattern: str = "") -> list[tuple[int, str]]:
    """返回 [(pid, command)]——本机相关进程，读不到就退化为 pgrep / 空列表。"""
    try:
        rows = _ps_rows()
        if rows or not pattern:
            return rows
    except Exception:
        pass
    try:
        return _pgrep_rows(pattern) if pattern else []
    except Exception:
        return []


def client_pids(app_path: str | None = None) -> list[int]:
    """正在运行的客户端进程 pid 列表（不含自己）。"""
    if app_path:
        # macOS 的进程命令行里带完整 bundle 路径，用路径匹配最准
        needles = [app_path]
        if IS_MAC:
            needles.append(os.path.join(app_path, "Contents", "MacOS"))
        pattern = os.path.basename(app_path.rstrip("/"))
    else:
        needles = list(_APP_NAMES) + list(_WIN_EXE_NAMES)
        pattern = _APP_NAMES[0]
    rows = _running_rows(pattern)
    me = os.getpid()
    hits = []
    for pid, cmd in rows:
        if pid == me or pid in hits:
            continue
        if any(n and n in cmd for n in needles):
            hits.append(pid)
    return hits


def detect_client(app_path: str | None = None) -> dict[str, Any]:
    """探测本机客户端：装在哪、有没有在跑、跑的是哪些 pid。"""
    path = app_path or app_install_path(os.getenv("ADMIN_CLIENT_APP", ""))
    pids = client_pids(path) if path else []
    return {
        "app_path": path,
        "running": bool(pids),
        "pids": pids,
        "platform": "macos" if IS_MAC else ("windows" if IS_WIN else sys.platform),
    }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _zombie_pids() -> set[int]:
    """僵尸进程 pid 集合。

    子进程被杀但没被 wait 回收时，`os.kill(pid, 0)` 依然成功——照它判断会
    「明知道已经死了还在等」，直到超时。这里额外查一次进程状态把它剔掉。
    """
    zs: set[int] = set()
    try:
        out = subprocess.run(["/bin/ps", "ax", "-o", "pid=,stat="],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if parts[1].startswith("Z"):
                zs.add(pid)
    except Exception:
        # 查不到就当没有僵尸：最坏情况是走到 TERM/KILL 那一层，不会误判为「还在跑」
        pass
    return zs


def wait_exit(pids: list[int], timeout: float = 12.0, poll: float = 0.25) -> bool:
    """等这些 pid 全部消失。超时返回 False（表示还有活的）。"""
    deadline = time.time() + timeout
    while True:
        # 只有到最后一次探查才查僵尸，避免每次轮询都起一个 ps 进程
        left = [p for p in pids if _pid_alive(p)]
        if not left:
            return True
        if time.time() >= deadline:
            return not [p for p in left if p not in _zombie_pids()]
        time.sleep(poll)


def wait_running(app_path: str, timeout: float = 20.0, poll: float = 0.5) -> bool:
    """等客户端重新起来——轮询到 pid 出现为止。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client_pids(app_path):
            return True
        time.sleep(poll)
    return bool(client_pids(app_path))


def quit_client(app_path: str | None = None, pids: list[int] | None = None,
                graceful_timeout: float = 10.0) -> dict[str, Any]:
    """让客户端退出：先 Graceful（osascript / taskkill），不行再 TERM，最后 KILL。

    返回 {'was_running','exited','timeout'}，调用方按 `exited` 决定是否继续写文件。
    """
    info = detect_client(app_path)
    target_pids = list(pids or info["pids"])
    if not target_pids:
        return {"was_running": False, "exited": True, "timeout": False}
    path = info["app_path"] or app_path

    # ① 优雅退出：给客户端一个保存自身状态的机会（我们自己不依赖它写回）
    try:
        if IS_MAC and path:
            name = os.path.basename(path.rstrip("/"))
            if name.endswith(".app"):
                name = name[: -len(".app")]
            subprocess.run(["osascript", "-e", f'quit app "{name}"'],
                           capture_output=True, text=True, timeout=8)
        elif IS_WIN:
            subprocess.run(["taskkill", "/IM",
                            os.path.basename(path or _WIN_EXE_NAMES[0])],
                           capture_output=True, text=True, timeout=8)
        else:
            for p in target_pids:
                try:
                    os.kill(p, signal.SIGTERM)
                except OSError:
                    pass
    except Exception:
        pass

    if wait_exit(target_pids, timeout=graceful_timeout):
        return {"was_running": True, "exited": True, "timeout": False}

    # ② TERM → KILL：还在顽抗的，逐级加压
    zombies = _zombie_pids()
    alive = [p for p in target_pids if _pid_alive(p) and p not in zombies]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not alive:
            break
        for p in alive:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        if wait_exit(alive, timeout=5.0):
            alive = []
            break
        still = _zombie_pids()
        alive = [p for p in alive if _pid_alive(p) and p not in still]
    return {"was_running": True, "exited": not alive, "timeout": bool(alive)}


def _mac_executable(app_path: str) -> str | None:
    """The real binary inside an .app bundle, so we can pass argv to it.

    `open -a` cannot take arguments, and we need to hand Electron our DNS
    overrides - so resolve Contents/MacOS/<exe> and run that directly.
    """
    macos = os.path.join(app_path, "Contents", "MacOS")
    if not os.path.isdir(macos):
        return None
    plist = os.path.join(app_path, "Contents", "Info.plist")
    name = None
    if os.path.exists(plist):
        try:
            done = subprocess.run(
                ["/usr/bin/plutil", "-convert", "json", "-o", "-", "--", plist],
                capture_output=True, text=True, timeout=5)
            name = (json.loads(done.stdout) or {}).get("CFBundleExecutable")
        except Exception:
            name = None
    for candidate in (name,):
        if candidate:
            target = os.path.join(macos, candidate)
            if os.path.isfile(target) and os.access(target, os.X_OK):
                return target
    try:
        entries = sorted(os.listdir(macos))
    except Exception:
        return None
    for entry in entries:
        target = os.path.join(macos, entry)
        if os.path.isfile(target) and os.access(target, os.X_OK):
            return target
    return None


def launch_client(app_path: str | None = None, wait: bool = True,
                  timeout: float = 20.0, adapt_dns: bool = True) -> dict[str, Any]:
    """重新启动客户端，并（可选）等到它真的出现进程为止。

    adapt_dns: 若本机 DNS 把云端域名解析到不可达地址，就给这个进程塞一份
    Chromium 的 --host-resolver-rules（仅对该进程生效，不改系统任何配置），
    否则客户端的会话列表、模型列表都拉不下来。
    """
    path = app_path or app_install_path(os.getenv("ADMIN_CLIENT_APP", ""))
    if not path:
        return {"launched": False, "app_path": None, "reason": "找不到客户端安装位置"}

    extra: list[str] = []
    dns_detail: dict = {}
    if adapt_dns:
        try:
            from admin.network_adapt import electron_args
            extra, dns_detail = electron_args()
        except Exception as exc:          # 探测失败不该挡住拉起
            dns_detail = {"error": f"{type(exc).__name__}: {exc}"}

    launched = False
    try:
        if IS_MAC:
            exe = _mac_executable(path)
            if exe:
                argv = [exe] + extra
                subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True)
                launched = True
            else:
                launched = subprocess.run(["/usr/bin/open", "-a", path],
                                          capture_output=True, text=True,
                                          timeout=15).returncode == 0
        elif IS_WIN:
            subprocess.Popen([path] + extra, close_fds=True)
            launched = True
        else:
            if shutil.which(path):
                subprocess.Popen([path] + extra, close_fds=True)
                launched = True
    except Exception:
        launched = False
    running = wait_running(path, timeout=timeout) if (launched and wait) else False
    return {"launched": launched, "app_path": path, "running": running,
            "dns_overrides": dns_detail or None, "chromium_args": extra or None}
