#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Win32 分层窗口（逐像素 alpha）
==============================

tkinter 只能做"色键透明"（`-transparentcolor`），边缘会出现彩色描边，
做桌宠不合格。这里直接用 `UpdateLayeredWindow` 贴逐像素 alpha 位图：
真正的不规则窗口，且**透明像素自动点击穿透**。

要点：
- 复用 tkinter 的消息循环：窗口在本线程创建，Tk 的 mainloop 会派发其消息；
- WndProc 内只入队，不直接调 tk 接口，避免重入；
- 半透明像素按阈值判定穿透（默认 alpha < 16 视为穿透）。

若系统不支持，构造时抛异常，由上层回退到 tkinter 色键模式。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from typing import Callable, List, Optional, Tuple

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
CS_DBLCLKS = 0x0008

SW_SHOWNOACTIVATE = 4
SW_HIDE = 0
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0
HTTRANSPARENT = -1

WM_NCHITTEST = 0x0084
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_APP = 0x8000

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)

# ---------------- 关键：显式声明 argtypes/restype，否则 64 位下 LPARAM 会溢出
user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.CreateWindowExW.restype = wt.HWND
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wt.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
    wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
    ctypes.POINTER(BLENDFUNCTION) if "BLENDFUNCTION" in globals() else ctypes.c_void_p,
    wt.DWORD,
]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.SetCapture.argtypes = [wt.HWND]
user32.ReleaseCapture.argtypes = []
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


# BLENDFUNCTION 定义之后再精确声明一次（覆盖前面的占位声明）
user32.UpdateLayeredWindow.argtypes = [
    wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
    wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wt.DWORD,
]
user32.UpdateLayeredWindow.restype = wt.BOOL


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class LayeredWindow:
    _class_atom = 0
    _class_name = "WbTokenPetLayered"

    def __init__(self, root, width: int, height: int,
                 on_event: Callable[[str, int, int], None]):
        self.root = root
        self.w = int(width)
        self.h = int(height)
        self.on_event = on_event
        self.events: List[Tuple[str, int, int]] = []
        self._alpha: bytes = b""
        self._cur_name = ""
        self._cur_idx = 0
        self._dragging = False
        self._drag_moved = False
        self._press_cursor = (0, 0)
        self._press_origin = (0, 0)
        self._hwnd: Optional[int] = None
        self._hdc_screen = None
        self._hdc_mem = None
        self._hbitmap = None
        self._bits = None
        self._old_bitmap = None
        self._wndproc = WNDPROC(self._wnd_proc)  # 必须保持引用，否则被回收
        self._create()

    # ---------------------------------------------------------------- 创建
    def _create(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        if not LayeredWindow._class_atom:
            wc = WNDCLASSW()
            wc.style = CS_DBLCLKS
            wc.lpfnWndProc = self._wndproc
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = None
            wc.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = self._class_name
            atom = user32.RegisterClassW(ctypes.byref(wc))
            if not atom:
                raise OSError(f"RegisterClassW failed: {ctypes.get_last_error()}")
            LayeredWindow._class_atom = atom

        ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
        self._hwnd = user32.CreateWindowExW(
            ex, self._class_name, "WB Pet", WS_POPUP,
            0, 0, self.w, self.h, None, None, hinst, None,
        )
        if not self._hwnd:
            raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")

        self._hdc_screen = user32.GetDC(None)
        self._hdc_mem = gdi32.CreateCompatibleDC(self._hdc_screen)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.w
        bmi.bmiHeader.biHeight = -self.h      # 负数 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0       # BI_RGB

        self._bits = ctypes.c_void_p()
        self._hbitmap = gdi32.CreateDIBSection(
            self._hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(self._bits), None, 0,
        )
        if not self._hbitmap:
            raise OSError(f"CreateDIBSection failed: {ctypes.get_last_error()}")
        self._old_bitmap = gdi32.SelectObject(self._hdc_mem, self._hbitmap)

        user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)

    # ---------------------------------------------------------------- 绘制
    def set_frame(self, bgra: bytes, w: int, h: int) -> None:
        if not self._hwnd or not bgra:
            return
        if (w, h) != (self.w, self.h):
            self.resize(w, h)
        if (w, h) != (self.w, self.h):
            return          # resize 失败：尺寸不一致，画了也会错位
        ctypes.memmove(self._bits, bgra, min(len(bgra), self.w * self.h * 4))
        self._alpha = bytes(bgra[3::4])
        self._paint()

    def _paint(self) -> None:
        size = wt.SIZE(self.w, self.h)
        src = wt.POINT(0, 0)
        pos = self.position()
        dst = wt.POINT(pos[0], pos[1])
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        ok = user32.UpdateLayeredWindow(
            self._hwnd, self._hdc_screen, ctypes.byref(dst), ctypes.byref(size),
            self._hdc_mem, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA,
        )
        if not ok and not self._alpha:
            pass  # 首次调用偶发失败，忽略

    def resize(self, w: int, h: int) -> None:
        if (w, h) == (self.w, self.h):
            return
        w, h = int(w), int(h)
        if w <= 0 or h <= 0:
            return

        # 先建新 DIB，成功了才拆旧的。原实现先删旧位图再建新的，一旦
        # CreateDIBSection 失败，_bits 就停在空指针上，下一帧 memmove 直接
        # 访问违例 —— pythonw 下进程无声死亡，用户看不到任何原因。
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h         # 负数 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0     # BI_RGB
        bits = ctypes.c_void_p()
        new_bitmap = gdi32.CreateDIBSection(
            self._hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0,
        )
        if not new_bitmap or not bits:
            return                          # 保持旧尺寸，下一帧会再试

        old = gdi32.SelectObject(self._hdc_mem, new_bitmap)
        if self._hbitmap:
            gdi32.DeleteObject(self._hbitmap)
        self._hbitmap = new_bitmap
        self._old_bitmap = old
        self._bits = bits
        self.w, self.h = w, h
        # 旧 alpha 缓冲是按旧 w/h 排的，留着会让 _alpha_at 读到错位字节，
        # 表现为宠物局部或整体误穿透。先作废，set_frame 紧接着会重填。
        self._alpha = b""

    # ---------------------------------------------------------------- 位置
    def position(self) -> Tuple[int, int]:
        if not self._hwnd:
            return (0, 0)
        r = wt.RECT()
        user32.GetWindowRect(self._hwnd, ctypes.byref(r))
        return (r.left, r.top)

    def move_to(self, x: int, y: int) -> None:
        if not self._hwnd:
            return
        user32.SetWindowPos(
            self._hwnd, None, int(x), int(y), 0, 0,
            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
        )

    def show(self) -> None:
        if self._hwnd:
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._paint()

    def hide(self) -> None:
        if self._hwnd:
            user32.ShowWindow(self._hwnd, SW_HIDE)

    def destroy(self) -> None:
        try:
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
            if self._hdc_mem and self._old_bitmap:
                gdi32.SelectObject(self._hdc_mem, self._old_bitmap)
            if self._hbitmap:
                gdi32.DeleteObject(self._hbitmap)
                self._hbitmap = None
            if self._hdc_mem:
                gdi32.DeleteDC(self._hdc_mem)
                self._hdc_mem = None
            if self._hdc_screen:
                user32.ReleaseDC(None, self._hdc_screen)
                self._hdc_screen = None
        except Exception:
            pass

    # ---------------------------------------------------------------- 消息
    def _alpha_at(self, x: int, y: int) -> int:
        if not self._alpha:
            return 255
        if 0 <= x < self.w and 0 <= y < self.h:
            return self._alpha[y * self.w + x]
        return 0

    def _queue(self, kind: str, x: int, y: int) -> None:
        # 只入队：绝不能在 WndProc 内调用 Tk（会与 Tk 的消息泵重入，导致 GIL 崩溃）
        self.events.append((kind, x, y))

    def poll(self, handler=None) -> None:
        """由 Tk 定时器调用，把队列里的事件一次性派发出去。"""
        h = handler or self.on_event
        while self.events:
            kind, x, y = self.events.pop(0)
            try:
                h(kind, x, y)
            except Exception:
                pass

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_NCHITTEST:
                # 透明像素点击穿透（分层窗口默认已做，这里再兜一层）
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                r = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                if self._alpha_at(pt.x - r.left, pt.y - r.top) < 16:
                    return HTTRANSPARENT
                return 1  # HTCLIENT

            if msg == WM_LBUTTONDOWN:
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                self._press_cursor = (pt.x, pt.y)
                self._press_origin = self.position()
                self._dragging = True
                self._drag_moved = False
                user32.SetCapture(hwnd)
                self._queue("press", pt.x, pt.y)
                return 0

            if msg == WM_MOUSEMOVE and self._dragging:
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                dx = pt.x - self._press_cursor[0]
                dy = pt.y - self._press_cursor[1]
                if abs(dx) > 3 or abs(dy) > 3:
                    self._drag_moved = True
                if self._drag_moved:
                    self.move_to(self._press_origin[0] + dx, self._press_origin[1] + dy)
                    self._queue("drag", pt.x, pt.y)
                return 0

            if msg == WM_LBUTTONUP:
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                self._dragging = False
                user32.ReleaseCapture()
                self._queue("drag_end" if self._drag_moved else "click", pt.x, pt.y)
                return 0

            if msg == WM_LBUTTONDBLCLK:
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                self._queue("dblclick", pt.x, pt.y)
                return 0

            if msg == WM_RBUTTONUP:
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                self._queue("right", pt.x, pt.y)
                return 0

            if msg == WM_CLOSE:
                self._queue("close", 0, 0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
