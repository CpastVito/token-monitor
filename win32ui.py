#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 系统集成：开机自启 / 全局热键 / 系统托盘
================================================

全部用 ctypes + winreg 实现，不引入第三方依赖（pystray / keyboard 都不用）。

- 自启：写 `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`
- 热键：`RegisterHotKey` + 隐藏窗口接收 `WM_HOTKEY`（Tk 的消息泵会派发到本线程窗口）
- 托盘：`Shell_NotifyIconW` + 隐藏窗口回调，图标由精灵帧生成，右键菜单交给 tkinter 弹
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import winreg
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import petwin as pw

user32 = pw.user32
kernel32 = pw.kernel32
gdi32 = pw.gdi32
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_VALUE = "WorkBuddyTokenPet"

WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_TRAY = WM_APP + 1

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

# 托盘回调里的鼠标消息
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203


# ---------------------------------------------------------------- 开机自启

def _pythonw() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def autostart_command(base_dir: Path) -> str:
    return f'"{_pythonw()}" "{Path(base_dir) / "overlay.py"}" --autostart'


def is_autostart(base_dir: Path) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, APP_VALUE)
    except OSError:
        return False
    # base_dir 原来收了却不用，只判断值里有没有 "overlay.py" —— 于是本机存在两份
    # 副本时，从哪一份看都显示"已开启自启"，哪怕注册表指的是另一份（删掉那份之后
    # 自启就静默失效了）。这里按完整路径比对。
    target = str(Path(base_dir) / "overlay.py").lower()
    return target in str(val).lower()


def set_autostart(base_dir: Path, enable: bool) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, APP_VALUE, 0, winreg.REG_SZ,
                                  autostart_command(base_dir))
            else:
                try:
                    winreg.DeleteValue(k, APP_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 隐藏消息窗口

class HiddenWindow:
    """用于接收 WM_HOTKEY 与托盘回调的不可见窗口。"""

    _class_name = "WbTokenMsgWindow"
    _atom = 0

    def __init__(self, root, on_hotkey: Optional[Callable[[], None]] = None,
                 on_tray: Optional[Callable[[str], None]] = None):
        self.root = root
        self.on_hotkey = on_hotkey
        self.on_tray = on_tray
        self.hwnd = None
        self.queue = []
        self._wndproc = pw.WNDPROC(self._proc)
        self._create()

    def _create(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        if not HiddenWindow._atom:
            wc = pw.WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = self._class_name
            HiddenWindow._atom = user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(
            0, self._class_name, "WB Token", pw.WS_POPUP,
            0, 0, 1, 1, None, None, hinst, None,
        )

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY:
                self.queue.append(("hotkey", 0, 0))
                return 0
            if msg == WM_TRAY:
                code = int(lparam) & 0xFFFF
                kind = ("left" if code == WM_LBUTTONUP else
                        "right" if code == WM_RBUTTONUP else
                        "dbl" if code == WM_LBUTTONDBLCLK else "")
                if kind:
                    self.queue.append(("tray_" + kind, 0, 0))
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def poll(self, on_hotkey=None, on_tray=None) -> None:
        while self.queue:
            kind, _x, _y = self.queue.pop(0)
            try:
                if kind == "hotkey" and on_hotkey:
                    on_hotkey()
                elif kind.startswith("tray_") and on_tray:
                    on_tray(kind[5:])
            except Exception:
                pass

    def destroy(self) -> None:
        try:
            if self.hwnd:
                user32.DestroyWindow(self.hwnd)
                self.hwnd = None
        except Exception:
            pass


# ---------------------------------------------------------------- 全局热键

class Hotkey:
    """注册一个全局热键；解析形如 ctrl+alt+w 的字符串。"""

    def __init__(self, hwnd, spec: str, on_fire: Callable[[], None]):
        self.hwnd = hwnd
        self.on_fire = on_fire
        self.spec = spec
        self.ok = False
        self._id = 0xB001
        self.register(spec)

    @staticmethod
    def parse(spec: str) -> Tuple[int, int]:
        mods = 0
        vk = 0
        for part in (spec or "").lower().replace(" ", "").split("+"):
            if part in ("ctrl", "control"):
                mods |= MOD_CONTROL
            elif part == "alt":
                mods |= MOD_ALT
            elif part == "shift":
                mods |= MOD_SHIFT
            elif len(part) == 1:
                vk = ord(part.upper())
        return mods, vk

    def register(self, spec: str) -> bool:
        mods, vk = self.parse(spec)
        if not vk:
            return False
        try:
            self.unregister()
        except Exception:
            pass
        self.ok = bool(user32.RegisterHotKey(self.hwnd, self._id, mods, vk))
        return self.ok

    def unregister(self) -> None:
        try:
            user32.UnregisterHotKey(self.hwnd, self._id)
        except Exception:
            pass
        self.ok = False


# ---------------------------------------------------------------- 托盘

class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
        ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON),
    ]


def _hicon_from_pil(img, size: int = 32) -> int:
    """
    把 PIL 图像转成 HICON（CreateIconIndirect）。
    img 需带 alpha。
    """
    from PIL import Image

    im = img.convert("RGBA").resize((size, size))
    # 颜色位图：32bpp BGRA（不预乘，CreateIconIndirect 接受非预乘 ARGB）
    r, g, b, a = im.split()
    from PIL import ImageChops

    pb = ImageChops.multiply(b, a)
    pg = ImageChops.multiply(g, a)
    pr = ImageChops.multiply(r, a)
    color = Image.merge("RGBA", (pr, pg, pb, a))

    hdc = user32.GetDC(None)
    bmi = pw.BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(pw.BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bits = ctypes.c_void_p()
    hbm_color = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0,
                                       ctypes.byref(bits), None, 0)
    if hbm_color and bits:
        ctypes.memmove(bits, color.tobytes("raw", "BGRA"), size * size * 4)

    # 掩码位图：全 0（用 alpha 通道决定透明）
    hbm_mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    user32.ReleaseDC(None, hdc)

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD),
                    ("yHotspot", wt.DWORD), ("hbmMask", wt.HBITMAP),
                    ("hbmColor", wt.HBITMAP)]

    ii = ICONINFO(True, 0, 0, hbm_mask, hbm_color)
    user32.CreateIconIndirect.restype = wt.HICON
    hicon = user32.CreateIconIndirect(ctypes.byref(ii))
    if hbm_color:
        gdi32.DeleteObject(hbm_color)
    if hbm_mask:
        gdi32.DeleteObject(hbm_mask)
    return int(hicon or 0)


class Tray:
    """系统托盘图标 + 点击回调（菜单由调用方弹）。"""

    UID = 0x0B01

    def __init__(self, hwnd, tooltip: str, icon_image=None,
                 on_click: Optional[Callable[[str], None]] = None):
        self.hwnd = hwnd
        self.on_click = on_click
        self.ok = False
        self._hicon = _hicon_from_pil(icon_image) if icon_image is not None else 0
        self._add(tooltip)

    def _data(self, tooltip: str) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = self.UID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = (tooltip or "")[:127]
        return nid

    def _add(self, tooltip: str) -> None:
        try:
            nid = self._data(tooltip)
            self.ok = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        except Exception:
            self.ok = False

    def update(self, tooltip: str) -> None:
        try:
            nid = self._data(tooltip)
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def remove(self) -> None:
        try:
            if self.ok:
                nid = self._data("")
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass
        self.ok = False


# ---------------------------------------------------------------- 前台进程

def foreground_process_name() -> str:
    """当前前台窗口的进程映像名（如 Qoder CN.exe）。失败返回空串。"""
    try:
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.OpenProcess.restype = wt.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.CloseHandle.restype = wt.BOOL

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1]
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 全屏检测

def is_fullscreen_foreground() -> bool:
    """
    前台窗口是否全屏（用于 D4：看视频/全屏游戏时自动隐藏宠物）。
    判据：前台窗口矩形覆盖其所在显示器，且不是桌面/任务栏。
    """
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow"):
            return False
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        hmon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False
        m = mi.rcMonitor
        return (r.left <= m.left and r.top <= m.top and
                r.right >= m.right and r.bottom >= m.bottom)
    except Exception:
        return False
