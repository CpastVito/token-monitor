#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨端视觉令牌（Tk 界面 ⇄ dashboard.html）
========================================

桌面端的两个界面（桌宠面板、气泡）与网页端共用一套颜色/字阶，
避免出现"网页一套皮肤、桌面另一套皮肤"。

关于颜色：网页用 `rgba(0,0,0,.075)` 这类半透明发丝线，Tk 画布不吃 alpha，
这里按"叠在白底上"换算成等效实色（.075 黑 → #E6E6EA，.045 → #EFEFF2）。

关于字体：Tk 的字体族在不同系统上差异很大（Windows 有微软雅黑，macOS 只有
苹方）。`resolve_fonts()` 在 Tk() 建好之后按本机实际字体族挑一个，挑不到就
逐级回退，保证中文不落到衬线字体、等宽数字不落到比例字体。
"""

from __future__ import annotations

from tkinter import font as tkfont

# ---------------------------------------------------------------- 颜色
BG = "#FFFFFF"          # 卡片白（= --card）
CARD = "#FAFAFC"        # 次级面（= --card2）
WELL = "#F2F3F5"        # 凹槽/轨道底（= --well）
BORDER = "#E6E6EA"      # 外框（= --border）
LINE = "#EFEFF2"        # 内部分隔线（= --border2）
BORDER_HI = "#DADADF"   # 强调描边（= --border-hi）

TEXT = "#1D1D1F"        # 主文本（= --text）
TEXT_SUB = "#6E6E73"    # 次级（= --muted）
TEXT_FAINT = "#86868B"  # 三级（= --faint）
TEXT_HINT = "#A6A6AC"   # 底部操作提示，比三级更弱（= --hint）

ACCENT = "#0071E3"      # 强调蓝（= --accent）
FG_IN = "#0A6ED1"       # 输入（= --in）
FG_OUT = "#B45309"      # 输出（= --out）
FG_CACHE = "#0F766E"    # 缓存（= --cache）
FG_CREDIT = "#6D28D9"   # 积分（= --credit）
OK = "#0B8A5B"          # 正常（= --ok）
WARN = "#B45309"        # 预警（= --warn）
BAD = "#C4302B"         # 告警（= --bad）

# 语义色底：网页端是 rgba(色,.10)，Tk 画布不吃 alpha，按叠在白底上换算成实色
TINT_IN = "#E7F0FA"
TINT_OUT = "#F8EEE6"
TINT_CACHE = "#E7EDF0"
TINT_CREDIT = "#F0E9FB"
TINT_OK = "#E7F3EF"
TINT_BAD = "#F9EAEA"

BAR_BG = "#ECECEF"      # 进度条轨道
TRACK = "#E3E3E9"       # 凹槽里的进度条底（要比 BAR_BG 重一点才压得住灰底）
WELL_LINE = "#E9E9EE"   # 凹槽内的行分隔线
HOVER = "#F1F1F4"       # 悬停底（= 网页 .navtab:hover / .seg button:hover）

# 深色底专用：网页 HERO 的那组亮色，白底上看不清，只用在品牌方块这类深色元素上
HERO_BG = "#131418"     # = --hero
HERO_OUT = "#F6B26B"    # = .h-out .val b
HERO_IN = "#8FB6F5"     # = .h-in .val b

# Tk 没有 alpha，投影只能靠"下缘露出的两条实色边"模拟：贴近卡片的深、外扩的浅
SHADOW_NEAR = "#E1E1E7"
SHADOW_FAR = "#EDEDF1"

KEY = "#FE00FE"         # 色键：抠圆角用的洋红，不要用于可见元素

# ---------------------------------------------------------------- 圆角（与网页 --r-* 同一档）
R_SM = 10               # = --r-sm
R_MD = 13               # = --r-md
R_CARD = 16             # 面板/菜单外框
R_WELL = 11             # 分组凹槽
R_CHIP = 9              # 余额小卡
R_PLATE = 8             # logo 底座

# ---------------------------------------------------------------- 字体
_UI_STACK = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
             "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC",
             "Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", "Arial")
_MONO_STACK = ("Cascadia Mono", "Consolas", "SF Mono", "Menlo",
               "JetBrains Mono", "DejaVu Sans Mono", "Courier New")

FONTS: dict[str, tuple] = {}

# 字阶只在这里定义一次：_DEFAULT 和 resolve_fonts() 共用，
# 免得两处各写一份、改了一边后桌宠和气泡的字级对不上。
# 数值是"磅"，Tk 会随系统 DPI 自动放大；画布几何另外乘 dpi_scale()，两边同步。
_SIZES: dict[str, tuple] = {
    "title":      (10, "bold"),   # 面板标题
    "row":        (9,),           # 菜单项、读数标签
    "tiny":       (8,),           # 三级标签、单位
    "val":        (9,),           # 次级读数（等宽，数字不跳位）
    "num":        (15, "bold"),   # 主读数：与网页 HERO 的 46px 同一层级比例
    "bal":        (13, "bold"),   # 余额金额
    "bubble":     (9,),
    "bubble_b":   (9, "bold"),
    "bubble_s":   (8,),
    "bubble_big": (16, "bold"),   # 气泡主数字
}
_MONO_KEYS = ("val", "num", "bal", "bubble_big")


def _table(ui: str, mono: str) -> dict:
    return {k: ((mono if k in _MONO_KEYS else ui,) + v) for k, v in _SIZES.items()}


# 没调 resolve_fonts 时的兜底（Windows 常见组合）
_DEFAULT = _table("Microsoft YaHei UI", "Consolas")


def _first(available, stack):
    for name in stack:
        if name in available:
            return name
    return stack[-1]


def resolve_fonts(root=None, force: bool = False) -> dict:
    """按本机字体族生成字阶；重复调用直接返回缓存。"""
    if FONTS and not force:
        return FONTS
    if root is None:
        FONTS.update(_DEFAULT)
        return FONTS
    try:
        fams = set(tkfont.families(root))
    except Exception:
        fams = set()
    FONTS.update(_table(_first(fams, _UI_STACK), _first(fams, _MONO_STACK)))
    return FONTS


def font(key: str) -> tuple:
    return FONTS.get(key) or _DEFAULT.get(key) or _DEFAULT["row"]


def dpi_scale(win, force=None) -> float:
    """
    窗口几何的缩放系数（1.0 = 100%）。

    Tk 的字号按磅算、随系统缩放放大，但画布坐标是像素——只放大字号会让文字
    压到行线上。几何量统一乘这个系数，量化到 1/4 档，避免出现奇怪的字距。
    """
    if force:
        return float(force)
    try:
        k = win.winfo_fpixels("1i") / 96.0
    except Exception:
        k = 1.0
    return max(0.9, min(2.0, round(k * 4) / 4))


# ---------------------------------------------------------------- 绘制
def round_rect(cv, x1, y1, x2, y2, r, fill, outline="", tags=None):
    """
    圆角矩形：四个 90° 扇形 + 两个矩形拼出来。

    不用 `create_polygon(smooth=True)`：那种画法在小半径下四角会"鼓"出来，
    和网页端 `border-radius` 的观感差得明显。
    """
    tag = tags if tags is not None else ()
    cv.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="", tags=tag)
    cv.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline="", tags=tag)
    corners = ((x1, y1, 90), (x2 - 2 * r, y1, 0),
               (x1, y2 - 2 * r, 180), (x2 - 2 * r, y2 - 2 * r, 270))
    for sx, sy, start in corners:
        cv.create_arc(sx, sy, sx + 2 * r, sy + 2 * r, start=start, extent=90,
                      style="pieslice", fill=fill, outline="", tags=tag)
    if not outline:
        return
    cv.create_line(x1 + r, y1, x2 - r, y1, fill=outline, tags=tag)
    cv.create_line(x1 + r, y2, x2 - r, y2, fill=outline, tags=tag)
    cv.create_line(x1, y1 + r, x1, y2 - r, fill=outline, tags=tag)
    cv.create_line(x2, y1 + r, x2, y2 - r, fill=outline, tags=tag)
    for sx, sy, start in corners:
        cv.create_arc(sx, sy, sx + 2 * r, sy + 2 * r, start=start, extent=90,
                      style="arc", outline=outline, tags=tag)


def contact_shadow(cv, x1, y1, x2, y2, r, depth, tags=None):
    """
    接触阴影：先画这层，再在它上面画卡片本体。

    Tk 画布没有 alpha，网页端那套三层柔和投影在这里画不出来。退一步用
    "下缘露出的两条实色边"模拟：贴近卡片的一档深、外扩的一档浅，
    只往下探出 depth 像素，左右与顶部不外露，观感就是卡片压在桌面上。
    """
    tag = tags if tags is not None else ()
    near = max(1, int(round(depth * 0.5)))
    round_rect(cv, x1, y1 + 1, x2, y2 + depth, r, fill=SHADOW_FAR, outline="", tags=tag)
    round_rect(cv, x1, y1 + 1, x2, y2 + near, r, fill=SHADOW_NEAR, outline="", tags=tag)


def hairline(cv, x1, x2, y, color=None, tags=None):
    """1px 发丝分隔线。画布坐标是整数，+0.5 会让线变粗，这里直接按整数画。"""
    cv.create_line(x1, y, x2, y, fill=color or LINE, tags=tags if tags is not None else ())
