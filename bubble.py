#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""气泡窗口：圆角、无边框、不抢焦点、自动消失。"""

from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from typing import Callable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uitheme as ut  # noqa: E402

KEY = ut.KEY             # 色键，用于抠出圆角
BG = ut.BG
BORDER = ut.BORDER
FG = ut.TEXT
FG_SUB = ut.TEXT_SUB


def _force_topmost(win) -> None:
    try:
        hwnd = int(win.wm_frame(), 16)
        ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                                          0x0002 | 0x0001 | 0x0010)
    except Exception:
        pass




class Bubble:
    def __init__(self, master: tk.Misc):
        self.master = master
        self.win = tk.Toplevel(master)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=KEY)
        try:
            self.win.attributes("-transparentcolor", KEY)
        except Exception:
            pass
        self.cv = tk.Canvas(self.win, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack()
        ut.resolve_fonts(master)      # 与本机字体族对齐
        self._job = None
        self._visible = False
        self._on_click: Optional[Callable[[], None]] = None

    # ---------------------------------------------------------------- 显示
    def _fonts(self, f: dict):
        """三份实测字体：量宽和定行高都用它们，避免按磅数猜像素。"""
        try:
            return (tkfont.Font(master=self.master, font=f["bubble_big"]),
                    tkfont.Font(master=self.master, font=f["bubble"]),
                    tkfont.Font(master=self.master, font=f["bubble_s"]))
        except Exception:
            return None

    def _fit_width(self, rows, title: str, big, mid, small, pad: int, cap: int) -> int:
        """按最长一行文字量出所需宽度（含内边距），上限 cap。"""
        if big is None:
            return 0
        need = small.measure(title or "") if title else 0
        for left, right, _color in rows:
            if left:
                line = small.measure(left) + 10 + mid.measure(right)
            else:
                line = big.measure(right)
            need = max(need, line)
        return min(int(need) + pad * 2 + 2, cap)

    def show(self, rows: Sequence[Tuple[str, str, str]], x: int, y: int,
             anchor: str = "above", timeout: int = 5000,
             title: str = "", width: int = 210) -> None:
        """
        rows   : [(左标签, 右值, 颜色)]；`左标签为 None` 表示整行强调（大字号）
        x, y   : 宠物窗口左上角
        anchor : above / below / right
        """
        self.cv.delete("all")
        f = ut.FONTS or ut.resolve_fonts(self.master)
        big, mid, small = self._fonts(f) or (None, None, None)
        k = ut.dpi_scale(self.master)

        def S(v):
            return int(round(v * k))

        pad = S(12)
        # 宽度按最长一行实测文字定：固定 210 会把"今日积分 1,262 · 阈值 500"
        # 这类长句裁掉（以前只有短提示，看不出来）。
        w = max(S(width), self._fit_width(rows, title, big, mid, small, pad, S(460)))
        # 行高直接取字体行距：写死 28/20/16 在 150% 缩放下会被大字号压出叠行
        BIG_H = (big.metrics("linespace") if big else S(28)) + S(4)
        ROW_H = max(mid.metrics("linespace") if mid else S(18),
                    small.metrics("linespace") if small else S(16)) + S(5)
        TITLE_H = (small.metrics("linespace") if small else S(16)) + S(3)
        heights = [BIG_H if left is None else ROW_H for left, _, _ in rows]
        card_h = pad * 2 + (TITLE_H if title else 0) + sum(heights) + S(2)
        shadow = S(4)
        h = card_h + shadow

        # 与桌宠面板同一套层次：发丝描边 + 下缘一道接触阴影
        ut.contact_shadow(self.cv, S(1), S(1), w - S(1), card_h, S(ut.R_MD), shadow)
        ut.round_rect(self.cv, S(1), S(1), w - S(1), card_h, S(ut.R_MD),
                      fill=BG, outline=BORDER)

        cy = pad
        if title:
            self.cv.create_text(pad, cy + S(1), anchor="nw", text=title,
                                fill=ut.TEXT_FAINT, font=f["bubble_s"])
            cy += TITLE_H
        for (left, right, color), rh in zip(rows, heights):
            yc = cy + rh // 2          # 同行两个锚点都垂直居中，字号不同也不错位
            if left:
                self.cv.create_text(pad, yc, anchor="w", text=left,
                                    fill=FG_SUB, font=f["bubble_s"])
                self.cv.create_text(w - pad, yc, anchor="e", text=right,
                                    fill=color or FG, font=f["bubble"])
            else:
                self.cv.create_text(pad, yc, anchor="w", text=right,
                                    fill=color or FG, font=f["bubble_big"])
            cy += rh

        self.cv.configure(width=w, height=h)

        # 定位
        if anchor == "above":
            px = x + 12
            py = y - h - 6
        elif anchor == "below":
            px = x + 12
            py = y + 8
        else:  # right
            px = x + 200
            py = y + 10
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        px = max(4, min(px, sw - w - 4))
        py = max(4, min(py, sh - h - 4))
        self.win.geometry(f"{w}x{h}+{int(px)}+{int(py)}")
        self.win.deiconify()
        self.win.attributes("-topmost", True)
        _force_topmost(self.win)
        self._visible = True

        if self._job:
            try:
                self.master.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if timeout > 0:
            self._job = self.master.after(timeout, self.hide)

    def move(self, x: int, y: int, anchor: str = "above") -> None:
        if not self._visible:
            return
        w = self.cv.winfo_reqwidth()
        h = self.cv.winfo_reqheight()
        if anchor == "above":
            px, py = x + 12, y - h - 6
        else:
            px, py = x + 12, y + 8
        self.win.geometry(f"+{int(px)}+{int(py)}")

    def hide(self) -> None:
        self._visible = False
        try:
            self.win.withdraw()
        except Exception:
            pass

    def is_visible(self) -> bool:
        return self._visible
