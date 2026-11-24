# -*- coding: utf-8 -*-
"""
玉衡5.0 · 跨系统的小工具（macOS / Windows / Linux 都能用）：找进程、关进程、后台启动、弹窗 + 提示音、打开文件夹、防睡眠、本机网卡地址。

  进程：优先用 psutil（三个系统都有）；没装 psutil 时 macOS / Linux 退回 ps + kill。
  弹窗：macOS 用系统弹窗 / 通知 + afplay；Windows 用置顶对话框（重要的）或系统通知（一般的）+ PowerShell 放 mp3；
        Linux 用 notify-send + paplay / aplay；都不行就只打印。
只读行情、不下单的规矩和这里无关：这里只管本机的进程和提示。
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
HERE = Path(__file__).resolve().parent
SOUND = HERE / "sounds" / "alert.mp3"

try:
    import psutil  # type: ignore
except Exception:                       # 没装：macOS / Linux 退回 ps
    psutil = None


# ================================================================ 进程

def _norm(a):
    return str(a).replace("\\", "/")


def list_procs():
    """[(pid, [命令行参数…])]，路径里的 \\ 统一换成 /。"""
    out = []
    if psutil is not None:
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                cl = p.info.get("cmdline") or []
            except Exception:
                continue
            if cl:
                out.append((p.info["pid"], [_norm(x) for x in cl]))
        return out
    if IS_WIN:
        return out
    r = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, cmd = line.partition(" ")
        if pid.isdigit():
            out.append((int(pid), cmd.split()))
    return out


def _is_python(a):
    return re.match(r"python[w0-9.]*(\.exe)?$", Path(a).name, re.I) is not None


def find_script(script, exclude=()):
    """正在用 Python 运行「程序/<script>」的进程（包括套在外面的 caffeinate / arch）。不含 exclude 里的 pid。"""
    tail = "程序/" + script
    me = {os.getpid()} | set(exclude)
    pids = []
    for pid, cl in list_procs():
        if pid in me:
            continue
        if any(_is_python(a) for a in cl) and any(a == tail or a.endswith("/" + tail) for a in cl):
            pids.append(pid)
    return pids


def find_all_program_procs(exclude=()):
    """所有在跑「程序/」下脚本的进程（常驻程序、工具任务、套在外面的 caffeinate）。打开控制台前清场用。"""
    me = {os.getpid()} | set(exclude)
    out = []
    for pid, cl in list_procs():
        if pid in me:
            continue
        joined = " ".join(cl)
        py_script = any(_is_python(a) for a in cl) and any(re.search(r"(^|/)程序/[^/]+\.py$", a) for a in cl)
        tool = "insert(0, '程序')" in joined
        caff = Path(cl[0]).name == "caffeinate" and "程序/" in joined
        if py_script or tool or caff:
            name = next((Path(a).name for a in reversed(cl) if a.endswith(".py")), str(pid))
            out.append((pid, name))
    return out


def alive(pid):
    if psutil is not None:
        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def terminate(pids, wait=4.0):
    """先礼后兵：先请它退出，等 wait 秒还在就强制结束。返回请求结束的个数。"""
    pids = list(pids)
    for pid in pids:
        try:
            if psutil is not None:
                psutil.Process(pid).terminate()
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    t0 = time.time()
    while time.time() - t0 < wait and any(alive(p) for p in pids):
        time.sleep(0.3)
    for pid in pids:
        if not alive(pid):
            continue
        try:
            if psutil is not None:
                psutil.Process(pid).kill()
            else:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except Exception:
            pass
    return len(pids)


def kill_tree(pid):
    """结束一个工具任务和它开的子进程（macOS / Linux 原来用 killpg）。"""
    if psutil is not None:
        try:
            p = psutil.Process(pid)
            kids = p.children(recursive=True)
            for x in kids + [p]:
                try:
                    x.terminate()
                except Exception:
                    pass
            return True
        except Exception:
            return False
    try:
        os.killpg(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


def detach_kwargs():
    """subprocess.Popen 的参数：后台启动、和控制台分开（Windows 不弹黑窗口）。"""
    if IS_WIN:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def _quiet():
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if IS_WIN else {}


# ================================================================ 打开、防睡眠、网卡

def open_path(p):
    p = str(p)
    if IS_MAC:
        subprocess.Popen(["open", p])
    elif IS_WIN:
        os.startfile(p)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def keep_awake(pid=None):
    """程序运行期间不让电脑睡眠。macOS：caffeinate；Windows：SetThreadExecutionState（在一个后台线程里每分钟续一次）。"""
    if IS_MAC:
        subprocess.Popen(["caffeinate", "-i", "-w", str(pid or os.getpid())])
    elif IS_WIN:
        def loop():
            import ctypes
            while True:
                try:
                    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                except Exception:
                    return
                time.sleep(60)
        threading.Thread(target=loop, daemon=True).start()


def caffeinate_prefix(heavy=True):
    """长任务前面加的防睡眠命令（只有 macOS 需要；Windows 由控制台统一防睡眠）。"""
    return ["caffeinate", "-i"] if IS_MAC and heavy else []


def local_ipv4():
    """本机所有 IPv4 地址（找 Tailscale 的 100.x 地址用）。"""
    ips = []
    if psutil is not None:
        try:
            for addrs in psutil.net_if_addrs().values():
                ips += [a.address for a in addrs if getattr(a, "family", None) == 2]
            return ips
        except Exception:
            pass
    try:
        if IS_WIN:
            r = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=5, **_quiet())
            return re.findall(r"(\d+\.\d+\.\d+\.\d+)", r.stdout)
        r = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
        return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout)
    except Exception:
        return ips


# ================================================================ 弹窗和提示音

def play_sound(path=None):
    snd = Path(path) if path else SOUND
    try:
        if IS_MAC:
            subprocess.Popen(["afplay", str(snd) if snd.exists() else "/System/Library/Sounds/Sosumi.aiff"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif IS_WIN:
            if snd.exists():                  # PowerShell 自带的媒体播放器能放 mp3
                ps = ("Add-Type -AssemblyName presentationCore; $p = New-Object System.Windows.Media.MediaPlayer; "
                      "$p.Open([uri]$env:YH_SND); $p.Play(); Start-Sleep -Seconds 5")
                subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                                 env=dict(os.environ, YH_SND=str(snd)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_quiet())
            else:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        else:
            for player in ("paplay", "aplay", "ffplay"):
                if subprocess.run(["which", player], capture_output=True).returncode == 0 and snd.exists():
                    args = [player, str(snd)] + (["-nodisp", "-autoexit"] if player == "ffplay" else [])
                    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
    except Exception:
        pass


def _win_toast(title, msg):
    """Windows 10 / 11 系统通知（右下角，不挡操作）。用 PowerShell 的身份发，不用装任何东西。"""
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x = $t.GetElementsByTagName('text'); $x.Item(0).AppendChild($t.CreateTextNode($env:YH_T)) > $null;"
        "$x.Item(1).AppendChild($t.CreateTextNode($env:YH_M)) > $null;"
        "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                     env=dict(os.environ, YH_T=title, YH_M=msg[:250]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_quiet())


def _win_box(title, msg):
    """Windows 置顶对话框（重要提醒：要点「确定」才消失）。在后台线程里弹，不卡程序。"""
    def show():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x00040000 | 0x00001000 | 0x30)   # 置顶 | 系统模态 | 警告图标
        except Exception:
            pass
    threading.Thread(target=show, daemon=True).start()


def popup(title, msg, critical=True, sound=None):
    """critical=True：要点掉的弹窗 + 提示音；False：通知（不挡操作）。sound=None 时：重要的响、一般的不响。"""
    sound = critical if sound is None else sound
    try:
        if IS_MAC:
            if critical:
                script = ['tell application "System Events"', "activate",
                          'display alert (item 1 of argv) message (item 2 of argv) as critical buttons {"知道了"} default button "知道了"',
                          "end tell"]
                args = ["osascript", "-e", "on run argv"] + sum([["-e", x] for x in script], []) + ["-e", "end run", title, msg]
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            t = msg.replace('"', "'")[:200]
            subprocess.Popen(["osascript", "-e", f'display notification "{t}" with title "{title}"'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif IS_WIN:
            if critical:
                _win_box(title, msg)
            else:
                _win_toast(title, msg)
        else:
            subprocess.Popen(["notify-send"] + (["-u", "critical"] if critical else []) + [title, msg],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    print(f"[{'弹窗' if critical else '通知'}] {title}：{msg}", flush=True)
    if sound:
        play_sound()
