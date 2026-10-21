#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自绘弹出菜单
============

Tk 的 `Menu` 在 Windows 上用的是系统菜单画法：蓝色选中条、灰白 3D 边框、行高与
字体都不受控，和桌宠面板 / 网页那套令牌完全对不上。这里改成 Canvas 自绘的弹出
面板，视觉规则与 `uitheme.py` / `dashboard.html` 保持一致：

    白底 #FFFFFF · 发丝描边 #E6E6EA · 圆角 12 · 下缘一道接触阴影
    行高 30、左右内边距 12、悬停底 #F1F1F4（圆角 7），悬停时整行文字转强调蓝
    对勾与子菜单箭头用折线画（不依赖符号字体，任何系统都不会缺字）
    当前值以浅灰小字右对齐（类似 macOS 菜单的 ⌘ 位）

用法：

    m = Menu(root)
    m.show([
        item("打开仪表盘", root.open_dashboard),
        sep(),
        item("查询来源", submenu=[item("全部", fn, checked=True), ...], hint="全部"),
    ], x, y)

spec 就是 dict 列表，用 item() / sep() 构造。
"""

from __future__ import annotations

import tkinter as tk
import traceback
from tkinter import font as tkfont

import uitheme as ut

# 以下都是 100% 缩放下的基准值，实际渲染按 DPI 系数等比放大
ROW_H = 30
SEP_H = 7
PAD_X = 12
PAD_Y = 6
CHK_W = 18
ARW_W = 13
HINT_GAP = 16
RADIUS = 12
MIN_W = 158
SHADOW = 4                # 下缘接触阴影：菜单是浮层，不投影会像贴在窗口上的一块


def item(label, command=None, *, checked=None, submenu=None, hint=None,
         enabled=True) -> dict:
    """一条命令项 / 勾选项 / 子菜单项。checked 只认 True 才会画对勾。"""
    return {"kind": "item", "label": label, "command": command, "checked": checked,
            "submenu": submenu, "hint": hint, "enabled": enabled}


def sep() -> dict:
    return {"kind": "sep"}


class Menu:
    """一次只弹一个菜单；show() 会先把上一个收掉。"""

    def __init__(self, root):
        self.root = root
        self.k = ut.dpi_scale(root)
        self.panels: list[_Panel] = []
        self._job = None

    # ---------------------------------------------------------------- 对外
    def show(self, spec, x, y):
        self.close()
        self.cancel_job()
        self.k = ut.dpi_scale(self.root)
        p = _Panel(self, spec, 0)
        self.panels.append(p)
        p.open_at(x, y)
        return p

    def close(self):
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        for p in reversed(self.panels):
            try:
                p.destroy()
            except Exception:
                pass
        self.panels = []

    # ---------------------------------------------------------------- 内部
    def close_below(self, panel):
        """收掉比 panel 更深的所有面板（例如把鼠标移回父级时收起子菜单）。"""
        deeper = [p for p in self.panels if p.depth > panel.depth]
        for p in reversed(deeper):
            try:
                p.destroy()
            except Exception:
                pass
            if p in self.panels:
                self.panels.remove(p)

    def pointer_inside(self) -> bool:
        try:
            px, py = self.root.winfo_pointerxy()
        except Exception:
            return False
        for p in self.panels:
            try:
                if not p.alive():
                    continue
                wx, wy = p.win.winfo_rootx(), p.win.winfo_rooty()
                if wx <= px < wx + p.w and wy <= py < wy + p.h:
                    return True
            except Exception:
                continue
        return False

    def cancel_job(self):
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def schedule_close(self, ms=260):
        """给焦点丢失一个宽限期：真正点到别的窗口才收，纯焦点抖动不收。"""
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
        self._job = self.root.after(ms, self._delayed_close)

    def schedule_prune(self, ms=170):
        """鼠标离开某个面板后，若没进入任何面板，就把子级收起来。"""
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
        self._job = self.root.after(ms, self._prune)

    def _delayed_close(self):
        self._job = None
        if not self.pointer_inside():
            self.close()

    def _prune(self):
        self._job = None
        if not self.panels or self.pointer_inside():
            return
        keep = [p for p in self.panels if p.depth == 0]
        for p in reversed(self.panels):
            if p.depth > 0:
                try:
                    p.destroy()
                except Exception:
                    pass
        self.panels = keep


class _Panel:
    """一个菜单面板；子菜单是另一个面板，彼此是独立的置顶窗口。"""

    def __init__(self, menu: Menu, spec, depth: int, parent=None, parent_row=None):
        self.menu = menu
        self.spec = spec
        self.depth = depth
        self.parent = parent
        self.parent_row = parent_row
        self.child = None
        self.k = menu.k
        self.w = 0
        self.h = 0
        self.rows: list[dict] = []
        self._hover = None
        self._fl = None
        self._fh = None

        self.win = tk.Toplevel(menu.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=ut.KEY)
        try:
            self.win.attributes("-transparentcolor", ut.KEY)
        except Exception:
            pass                        # 非 Windows 或旧 Tk：角上会有一点方，功能不受影响
        self.cv = tk.Canvas(self.win, bg=ut.KEY, highlightthickness=0, bd=0)
        self.cv.pack()

        self._layout()
        self.cv.bind("<Motion>", self._on_motion)
        self.cv.bind("<Button-1>", self._on_click)
        self.cv.bind("<Leave>", lambda e: self.menu.schedule_prune())
        self.win.bind("<Escape>", lambda e: self.menu.close())
        if depth == 0:
            # 点到别的窗口（包括任务栏、浏览器）时面板会失焦，这里收菜单
            self.win.bind("<FocusOut>", lambda e: self.menu.schedule_close())

    # ---------------------------------------------------------------- 布局
    def S(self, v) -> int:
        return int(round(v * self.k))

    def _layout(self):
        S = self.S
        self._fl = tkfont.Font(root=self.win, font=ut.font("row"))
        self._fh = tkfont.Font(root=self.win, font=ut.font("tiny"))
        row_h, sep_h = S(ROW_H), S(SEP_H)
        pad_x, pad_y = S(PAD_X), S(PAD_Y)
        chk_w, arw_w, hgap = S(CHK_W), S(ARW_W), S(HINT_GAP)

        y = pad_y
        rows = []
        width = S(MIN_W)
        for it in self.spec:
            if it["kind"] == "sep":
                rows.append({"kind": "sep", "y0": y, "y1": y + sep_h})
                y += sep_h
                continue
            need = pad_x + chk_w + self._fl.measure(it["label"]) + pad_x
            if it.get("hint"):
                need += hgap + self._fh.measure(it["hint"])
            if it.get("submenu"):
                need += arw_w + S(5)
            width = max(width, need)
            rows.append({"kind": "item", "item": it, "y0": y, "y1": y + row_h})
            y += row_h

        self.w = int(width)
        self.h = int(y + pad_y + S(SHADOW))
        self.rows = rows
        self._draw()

    def _draw(self):
        S = self.S
        cv = self.cv
        cv.delete("all")
        cv.configure(width=self.w, height=self.h)
        card_h = self.h - S(SHADOW)
        ut.contact_shadow(cv, S(1), S(1), self.w - S(1), card_h,
                          S(RADIUS), S(SHADOW))
        ut.round_rect(cv, S(1), S(1), self.w - S(1), card_h, S(RADIUS),
                      fill=ut.BG, outline=ut.BORDER)

        pad_x, chk_w, arw_w = S(PAD_X), S(CHK_W), S(ARW_W)
        x_text = pad_x + chk_w

        # 第一遍：命中区 + 悬停底。悬停底必须先画，文字后画，文字才永远在最上层。
        for i, r in enumerate(self.rows):
            if r["kind"] != "item":
                continue
            cv.create_rectangle(S(2), r["y0"], self.w - S(2), r["y1"],
                                fill=ut.BG, outline="")
            ut.round_rect(cv, S(5), r["y0"] + 1, self.w - S(5), r["y1"] - 1,
                          S(7), fill=ut.HOVER, tags=f"hv{i}")
            cv.itemconfig(f"hv{i}", state="hidden")

        # 第二遍：分隔线、对勾、文字、当前值、箭头
        for i, r in enumerate(self.rows):
            if r["kind"] == "sep":
                ym = (r["y0"] + r["y1"]) // 2
                cv.create_line(pad_x, ym, self.w - pad_x, ym, fill=ut.LINE)
                continue

            it = r["item"]
            yc = (r["y0"] + r["y1"]) // 2
            on = bool(it.get("enabled", True))
            fg = ut.TEXT if on else ut.TEXT_HINT
            # 整行（对勾 / 标签 / 当前值 / 箭头）共用一个标签，悬停时一次染色
            r["fg"] = fg
            tg = f"t{i}" if on else None

            if it.get("checked") is True:
                self._check(pad_x, yc, ut.ACCENT if on else ut.TEXT_HINT, tg)
            cv.create_text(x_text, yc, anchor="w", text=it["label"],
                           fill=fg, font=ut.font("row"), tags=tg or ())

            right = self.w - pad_x - (arw_w + S(5) if it.get("submenu") else 0)
            if it.get("hint"):
                cv.create_text(right, yc, anchor="e", text=str(it["hint"]),
                               fill=ut.TEXT_HINT, font=ut.font("tiny"),
                               tags=tg or ())
            if it.get("submenu"):
                self._arrow(self.w - pad_x - S(4), yc, ut.TEXT_FAINT, tg)

    def _check(self, x, yc, color, tags=None):
        """对勾用折线画：符号字体在各系统上字形差异太大。"""
        k = self.k
        s = 3.1 * k
        self.cv.create_line(x, yc, x + s, yc + s * 1.05,
                            x + s * 2.5, yc - s * 1.35,
                            fill=color, width=max(1.4, 1.55 * k),
                            capstyle="round", joinstyle="round",
                            tags=tags or ())

    def _arrow(self, x, yc, color, tags=None):
        k = self.k
        d = 3.3 * k
        self.cv.create_line(x - d, yc - d, x, yc, x - d, yc + d,
                            fill=color, width=max(1.2, 1.35 * k),
                            capstyle="round", joinstyle="round", tags=tags or ())

    # ---------------------------------------------------------------- 行为
    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except Exception:
            return False

    def open_at(self, x, y):
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x = max(4, min(int(x), sw - self.w - 4))
        y = max(4, min(int(y), sh - self.h - 4))
        self.win.geometry(f"{self.w}x{self.h}+{x}+{y}")
        self.win.deiconify()
        self.win.lift()
        # 只有顶层面板抢焦点。子面板若也抢，父面板会收到 FocusOut 而被收掉。
        if self.depth == 0:
            try:
                self.win.focus_force()
            except Exception:
                pass

    def _row_at(self, y):
        for i, r in enumerate(self.rows):
            if r["kind"] == "sep":
                continue
            if r["y0"] <= y < r["y1"]:
                return i
        return None

    def _set_hover(self, idx):
        if idx == self._hover:
            return
        self._hover = idx
        for i, r in enumerate(self.rows):
            if r["kind"] != "item":
                continue
            self.cv.itemconfig(f"hv{i}", state="normal" if i == idx else "hidden")
            if "fg" not in r:
                continue
            # 禁用项不参与染色：_draw 就没给它挂 t{i} 标签
            self.cv.itemconfig(f"t{i}",
                               fill=ut.ACCENT if i == idx else r["fg"])

    def _on_motion(self, ev):
        idx = self._row_at(ev.y)
        if idx is None:
            self._set_hover(None)      # 停在上下留白里：把上一条的高亮收掉
            return
        self._set_hover(idx)
        if self.rows[idx]["item"].get("submenu"):
            self._open_child(idx)
        else:
            if self.child is not None:
                self.menu.close_below(self)
                self.child = None

    def _open_child(self, idx):
        if self.child is not None and self.child.parent_row == idx and self.child.alive():
            return
        self.menu.close_below(self)
        self.menu.cancel_job()          # 别再被上一次的 FocusOut 误收
        self.child = None
        child = _Panel(self.menu, self.rows[idx]["item"]["submenu"],
                       self.depth + 1, parent=self, parent_row=idx)
        self.menu.panels.append(child)
        self.child = child

        # 子菜单第一行与父项对齐；右侧放不下就翻到左边
        r = self.rows[idx]
        cx = self.win.winfo_rootx() + self.w - self.S(3)
        cy = self.win.winfo_rooty() + r["y0"] - self.S(PAD_Y)
        if cx + child.w > self.win.winfo_screenwidth() - 4:
            cx = self.win.winfo_rootx() - child.w + self.S(3)
        child.open_at(cx, cy)

    def _on_click(self, ev):
        idx = self._row_at(ev.y)
        if idx is None:
            return
        it = self.rows[idx]["item"]
        if it.get("submenu"):
            self._open_child(idx)
            return
        if not it.get("enabled", True):
            return
        cmd = it.get("command")
        self.menu.close()
        if cmd is not None:
            try:
                cmd()
            except Exception:
                traceback.print_exc()

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass
