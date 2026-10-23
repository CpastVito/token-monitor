#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Token 监测桌宠（鲸鱼娘）
==============================

折叠态是精灵动画，悬停/单击展开逐行数据面板。

形象    精灵表 9 动作 + 16 方向注视（assets/shenshen-atlas.png，CC BY-NC-SA 4.0）
状态    running（模型在跑）/ waiting（等待）/ review（一轮完成）/ failed（告警）/ idle
数据    有效上下行 t/s（已扣工具执行）、端到端、trace 基线、TTFT 估算、上下文、积分
交互    悬停/单击展开 · 双击仪表盘 · 拖动 · 右键菜单 · 全局热键 · 托盘
系统    开机自启 · 全屏自动隐藏 · 多服务商余额 · 阈值气泡与提示音
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import random
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
import tkinter.font as tkfont
from tkinter import (BooleanVar, Canvas, DoubleVar, Frame, Label,
                     PhotoImage, StringVar, Tk, Toplevel)

sys.path.insert(0, str(Path(__file__).resolve().parent))


# ---------------------------------------------------------------- 解释器兜底
def _ensure_deps() -> None:
    need = []
    try:
        import tkinter  # noqa: F401
    except ImportError:
        need.append("tkinter")
    try:
        import PIL  # noqa: F401
    except ImportError:
        need.append("Pillow")
    if not need:
        return
    for cand in [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python313/python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python312/python.exe",
    ]:
        if cand.exists() and str(cand) != sys.executable:
            os.execv(str(cand), [str(cand), str(Path(__file__).resolve())] + sys.argv[1:])
    sys.stderr.write(f"[wb-token-monitor] 缺少依赖：{', '.join(need)}\n")
    sys.exit(2)


_ensure_deps()

import collector  # noqa: E402
import petmenu  # noqa: E402
import petwin  # noqa: E402
import prefs  # noqa: E402
import providers  # noqa: E402
import petsprite  # noqa: E402
import win32ui  # noqa: E402
from bubble import Bubble  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "overlay-state.json"
LOGOS = BASE_DIR / "assets" / "logos"

# 数据源：右键菜单里"查哪个软件"的选项。顺序即菜单顺序，"全部"置顶。
_SOURCE_IDS = ("auto", "all") + tuple(collector.sources.SOURCE_IDS)
_SOURCE_LABELS = {"auto": "自动跟随", "all": "全部应用"}
_SOURCE_LABELS.update({k: v["label"] for k, v in collector.sources.SOURCES.items()})

# ---------------------------------------------------------------- 视觉令牌
# 与网页端共用同一套颜色/字阶（uitheme 对应 dashboard.html 的 :root 变量），
# 改一处两端同步，避免"网页一套皮肤、桌面另一套"。
import uitheme as ut  # noqa: E402

BG = ut.BG
BORDER = ut.BORDER
FG = ut.TEXT
FG_SUB = ut.TEXT_SUB
FG_FAINT = ut.TEXT_FAINT
FG_IN = ut.FG_IN
FG_OUT = ut.FG_OUT
FG_OK = ut.OK
FG_WARN = ut.BAD
BAR_BG = ut.BAR_BG
KEY = ut.KEY



def force_topmost(win) -> None:
    """
    把 tk 窗口强推到 HWND_TOPMOST，且不激活、不抢焦点。

    tk 的 attributes("-topmost", True) 在 overrideredirect 窗口上并不可靠
    （尤其 deiconify 之后可能被其它进程窗口盖住），直接调 Win32 更稳。
    """
    try:
        hwnd = int(win.wm_frame(), 16)
        ctypes.windll.user32.SetWindowPos(
            hwnd, -1, 0, 0, 0, 0,          # HWND_TOPMOST
            0x0002 | 0x0001 | 0x0010,      # NOMOVE | NOSIZE | NOACTIVATE
        )
    except Exception:
        pass


def _fmt(n) -> str:
    n = float(n or 0)
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}K"
    return f"{int(n):,}"


def _rate(n) -> str:
    n = float(n or 0)
    if n <= 0:
        return "—"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}K"
    if n >= 100:
        return f"{n:.0f}"
    return f"{n:.1f}"


def look_atlas(dx: float, dy: float, offset: float = 270.0) -> float:
    """
    鼠标相对桌宠中心的偏移 → 图集角度。

    图集第 9–10 行是 16 帧"绕竖轴转一圈"，实测朝向为：
        0° = 正脸、90° = 朝**屏幕右**、180° = 背面、270° = 朝**屏幕左**
    （早期注释按"90° 朝左"写，方向反了，于是左右镜像：指右边却朝左看。）

    只取水平分量 t = dx / √(dx² + (0.55·dy)²)：
    正上方/正下方都回到正脸，否则会露出 180° 的背面。
    offset 用来整体校准，默认 270 时 t=+1 → 90°（朝右）、t=-1 → 270°（朝左）。
    """
    t = dx / max(1.0, (dx * dx + (dy * 0.55) ** 2) ** 0.5)
    return (offset - 270.0 + 90.0 * t) % 360.0


def credit_band(credit: float, warn: float) -> int:
    """
    今日积分落在第几档：未达阈值（或阈值为 0 = 关闭）返回 0。

    提示按档触发而不是按时间：跨档才响一次（500 → 1000 → 1500 …），
    这样一整天最多响几次，而不是每 cooldown 一次。
    """
    if warn <= 0 or credit < warn:
        return 0
    return int(credit // warn)


def _ctx_color(p: float) -> str:
    """与网页端 ctxColor() 同一套阶梯：50% 琥珀、80% 红。

    注意这与告警阈值（petconfig 的 ctx_warn / ctx_crit）是两回事：
    颜色是视觉约定，告警阈值由用户配置。
    """
    if p >= 0.80:
        return ut.BAD
    if p >= 0.50:
        return ut.WARN
    return ut.OK


_SHORT = {"DeepSeek": "DS", "Moonshot": "Kimi", "New": "NewAPI",
          "OpenRouter": "OR", "OpenAI": "OpenAI", "自定义": "自定义"}


def _short_label(label) -> str:
    """把服务商名压成 3–6 字符，供面板同一行并排显示。"""
    if not label:
        return "?"
    first = str(label).replace("/", " ").split()
    key = first[0] if first else str(label)
    if key in _SHORT:
        return _SHORT[key]
    return key[:6]


def _load_brands() -> list:
    try:
        data = json.loads((LOGOS / "brands.json").read_text(encoding="utf-8"))
        return data.get("brands") or []
    except Exception:
        return []


_BRANDS = _load_brands()
_BRAND_NAMES = {b.get("name") for b in _BRANDS if b.get("name")} | {"custom"}


def _brand_for(item: dict) -> str:
    """按 label / id / base_url 认品牌，认不出再退回模板名，最后 custom。"""
    hay = " ".join(str(item.get(k) or "") for k in ("label", "id", "base_url")).lower()
    for b in _BRANDS:
        for k in (b.get("keys") or []):
            if str(k).lower() in hay:
                return str(b.get("name"))
    tpl = str(item.get("template") or "").lower()
    return tpl if tpl in _BRAND_NAMES else "custom"


def _pretty(m: str) -> str:
    if not m:
        return "—"
    segs = m.split("-")
    known = {"deepseek": "DeepSeek", "kimi": "Kimi", "glm": "GLM", "gemini": "Gemini",
             "hy": "HY", "space": "Space", "bunny": "Bunny", "qwen": "Qwen",
             "gpt": "GPT", "claude": "Claude", "minimax": "MiniMax"}
    head = segs[0].lower() if segs else ""
    if segs:
        segs[0] = known.get(head, segs[0][:1].upper() + segs[0][1:] if segs[0] else segs[0])
    for i in range(1, len(segs)):
        s = segs[i]
        if s and s[0].isalpha():
            segs[i] = s[0].upper() + s[1:]
    return "-".join(segs)


# ---------------------------------------------------------------- 数据采样
# 走 ProxyHandler({}) 绕过系统代理：本机装了代理时，127.0.0.1 的请求可能被转出去。
_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_live(source: str) -> dict | None:
    """
    服务在跑时直接取 /api/live。

    通常 server.py 与桌宠同时常驻（start.cmd / app.pyw 两个都会起服务），
    原来桌宠进程每 8 秒自己再做一遍全量扫描聚合，与服务完全重复 ——
    双倍的文件 glob、JSONL 指纹比对与 JSON 缓存读写。服务有 1.2s 响应缓存，
    一次本地 HTTP 往返的代价远低于一次采集。服务不在（或还没起来）时返回
    None，由调用方回落到本地采集。
    """
    try:
        url = f"http://127.0.0.1:{prefs.read_port()}/api/live?source={source}"
        with _HTTP_OPENER.open(url, timeout=1.5) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


class Sampler:
    def __init__(self, source: str = "auto"):
        self.lock = threading.Lock()
        self.source = source if source in _SOURCE_IDS else "auto"
        self.snap = {
            "model": "", "out_eff": 0.0, "in_eff": 0.0,
            "out_raw": 0.0, "in_raw": 0.0, "out_base": 0.0, "in_base": 0.0,
            "tool_ratio": 0.0, "pct": 0.0, "pct_model": 0.0,
            "used": 0, "size": 0, "size_model": 0,
            "sess_in": 0, "sess_out": 0, "credit": 0.0,
            "today_in": 0, "today_out": 0, "today_req": 0, "today_credit": 0.0,
            "ttft": 0.0, "turn": None, "running": [], "status": "",
            "active": False, "stale": True, "ts": 0,
            # 多源：当前查的是哪个应用，以及"有没有数可显示"。
            # has_* 是**数据驱动**而非能力驱动 —— Claude 虽然理论上能记 token，
            # 但本机一条记录都没有，此时也该显示「—」而不是 0。
            "src": self.source, "requested_src": self.source,
            "src_label": _SOURCE_LABELS.get(self.source, "全部"),
            "has_tokens": True, "has_credits": False, "unit": "tokens",
            "credit_per_min": 0.0, "credit_per_req": 0.0, "req_per_min": 0.0,
            "recent_req": 0, "ttft_est": True,
            "sess_req": 0, "src_note": "",
        }
        self._stop = False
        self._force = False
        self._srv_miss = 0
        self._srv_try_ts = 0.0

    def set_source(self, source: str) -> None:
        src = source if source in _SOURCE_IDS else "all"
        self.source = src
        self._force = True          # 让下一轮立刻做完整汇总，别等 tick%4
        # 标签先换掉：采样间隔 2s，否则切完来源标题还挂着旧应用名
        with self.lock:
            self.snap.update({"src": src, "requested_src": src,
                              "src_label": _SOURCE_LABELS.get(src, "全部应用")})

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        tick = 0
        while not self._stop:
            try:
                src = self.source
                data = _fetch_live(src)
                if data is not None:
                    # 服务在线：直接消费聚合结果，每轮都是新鲜的（服务端 TTL 1.2s）
                    self._srv_miss = 0
                    patch = self._patch_from_live(data, src)
                else:
                    # 服务不在：本地采集。db 每轮读（轻量），全量汇总每 4 轮一次
                    patch = self._collect_local(src, tick)
                    tick += 1
                    # 开机自启只拉起桌宠，服务没人管；连续几轮取不到数据就
                    # 自己把 server.py 拉起来，否则面板会一直停在本地兜底口径。
                    self._srv_miss += 1
                    if self._srv_miss >= 3:
                        self._maybe_start_server()
                if patch:
                    with self.lock:
                        self.snap.update(patch)
            except Exception as exc:
                if os.environ.get("WB_MONITOR_DEBUG"):
                    print("[sampler]", exc, file=sys.stderr)
            time.sleep(2.0)

    def _maybe_start_server(self) -> None:
        now = time.time()
        if now - self._srv_try_ts < 60:
            return
        self._srv_try_ts = now
        try:
            import socket
            with socket.create_connection(("127.0.0.1", prefs.read_port()),
                                          timeout=0.35):
                return
        except OSError:
            pass
        try:
            import subprocess
            exe = Path(sys.executable)
            pyw = exe.with_name("pythonw.exe")
            subprocess.Popen(
                [str(pyw if pyw.exists() else exe),
                 str(BASE_DIR / "server.py"), "--no-browser"],
                cwd=str(BASE_DIR),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
        except Exception:
            pass

    def _patch_from_live(self, data: dict, src: str) -> dict:
        """把 /api/live 的响应映射成面板快照（与本地采集口径完全一致）。"""
        lv = data.get("live") or {}
        rt = data.get("rates") or {}
        rc = data.get("recent") or {}
        bs = data.get("baseline") or {}
        td = data.get("today") or {}
        now_ms = time.time() * 1000
        model = lv.get("model") or ""
        hist = {b["model"]: b for b in (bs.get("by_model") or [])}
        h = hist.get(model) or {}
        rate_src = rc if (rc.get("samples") or 0) > 0 else rt
        bysrc = {b["id"]: b for b in (data.get("by_source") or [])}
        # auto/all 由服务端解析出当前真正活跃的源；单位跟着**实时会话**走，
        # 否则 all 模式下 Qoder 正在跑时，面板仍按 WorkBuddy 的 tokens 口径
        # 显示 0 t/s（看起来像没反应）。
        resolved = data.get("source") or ("all" if src in ("all", "auto") else src)
        units = data.get("units") or {}
        unit = lv.get("unit") or units.get("primary") or ""
        if unit not in ("tokens", "credits", "requests"):
            unit = "tokens"
        label = _SOURCE_LABELS.get(resolved, "全部")
        if src == "auto":
            label = "自动 · " + label
        # all 模式下 recent 混着别的源，必须用服务端解析出的实时会话速率；
        # 手动选源时用 recent 以与仪表盘口径保持一致（含"最近 N 次"回退）。
        use_lv = src == "all" and bool(lv) and unit == "tokens"
        upd = float(lv.get("updated_at") or 0)
        return {
            # 非 WorkBuddy 源没有 used/size 绝对值，pct 是 context_usage_ratio；
            # 面板按 size 是否为 0 区分两种画法，这里原样透传即可。
            "pct": float(lv.get("pct") or 0.0),
            "pct_model": float(lv.get("pct_model") or 0.0),
            "used": lv.get("used") or 0,
            "size": lv.get("size") or 0,
            "size_model": lv.get("size_model") or 0,
            "active": bool(upd) and (now_ms - upd) < 120_000,
            "status": lv.get("status") or "",
            "src": resolved,
            "requested_src": src,
            "src_label": label,
            "ts": int(now_ms),
            "model": model,
            "out_eff": (lv.get("out_tps", 0.0) if use_lv
                        else rate_src.get("out_tps", 0.0)),
            "in_eff": (lv.get("in_tps", 0.0) if use_lv
                       else rate_src.get("in_tps", 0.0)),
            "out_raw": rc.get("raw_out_tps") or rt.get("raw_out_tps", 0.0),
            "in_raw": rc.get("raw_in_tps") or rt.get("raw_in_tps", 0.0),
            "tool_ratio": rt.get("tool_ratio", 0.0),
            "out_base": h.get("out_tps", bs.get("out_tps", 0.0)),
            "in_base": h.get("in_tps", bs.get("in_tps", 0.0)),
            "sess_in": lv.get("session_input", 0),
            "sess_out": lv.get("session_output", 0),
            "sess_req": lv.get("requests", 0),
            "credit": lv.get("session_credit", 0.0),
            "today_in": td.get("input", 0),
            "today_out": td.get("output", 0),
            "today_req": td.get("requests", 0),
            "today_credit": td.get("credit", 0.0),
            "ttft": (lv.get("ttft") if lv.get("ttft") is not None
                     else (data.get("ttft") or {}).get("ttft_ms", 0.0)),
            "ttft_est": bool(lv.get("ttft_est")
                             if lv.get("ttft_est") is not None
                             else (data.get("ttft") or {}).get("estimated", True)),
            "turn": data.get("turn"),
            "running": data.get("running") or [],
            "unit": unit,
            "credit_per_min": float(lv.get("credit_per_min")
                                    or rate_src.get("credit_per_min") or 0.0),
            "credit_per_req": float(lv.get("credit_per_req")
                                    or rate_src.get("credit_per_req") or 0.0),
            "req_per_min": float(lv.get("req_per_min")
                                 or rate_src.get("req_per_min") or 0.0),
            "recent_req": int(lv.get("recent_requests")
                              or rate_src.get("requests") or 0),
            # "stale" 只表示近期没有任何请求；token 源没有 token 样本并不等于空闲
            "stale": (rc.get("requests") or 0) < 1,
            "has_tokens": unit == "tokens",
            "has_credits": unit == "credits",
            "src_note": (bysrc.get(resolved) or {}).get("note", "")
                        if resolved != "all" else "",
        }

    def _collect_local(self, src: str, tick: int) -> dict:
        """服务不可用时的本地采集路径（原实现）。"""
        # 快速轮次（每 2s）不重新汇总，但也不能把上一轮的状态清零：
        # 之前这里每轮写回 active=False / pct=0，只有每 4 轮才恢复一次，
        # 面板大部分时间因此显示「空闲 / 上下文 –」。
        with self.lock:
            prev = dict(self.snap)
        patch = {
            "pct": prev.get("pct", 0.0),
            "pct_model": prev.get("pct_model", 0.0),
            "used": prev.get("used", 0),
            "size": prev.get("size", 0),
            "size_model": prev.get("size_model", 0),
            "active": prev.get("active", False),
            "status": prev.get("status", ""),
            "ts": int(time.time() * 1000),
        }

        if tick % 4 == 0 or self._force:
            self._force = False
            s = collector.build_summary("today", with_sessions=False,
                                        source=src)
            rt = s.get("rates") or {}
            rc = s.get("recent") or {}
            bs = s.get("baseline") or {}
            lv = s.get("live") or {}
            td = s.get("today") or {}
            model = lv.get("model") or ""
            hist = {b["model"]: b for b in (bs.get("by_model") or [])}
            h = hist.get(model) or {}
            rate_src = rc if (rc.get("samples") or 0) > 0 else rt
            bysrc = {b["id"]: b for b in (s.get("by_source") or [])}
            resolved = s.get("source") or ("all" if src in ("all", "auto") else src)
            units = s.get("units") or {}
            unit = lv.get("unit") or units.get("primary") or ""
            if unit not in ("tokens", "credits", "requests"):
                unit = "tokens"
            label = _SOURCE_LABELS.get(resolved, "全部")
            if src == "auto":
                label = "自动 · " + label
            # 本地采集同样要报告"解析后的源"，否则 auto 在离线时标题会对不上
            patch.update(
                model=model,
                src=resolved,
                requested_src=src,
                src_label=label,
                unit=unit,
                out_eff=(lv.get("out_tps", 0.0) if (src == "all" and lv
                         and unit == "tokens") else rate_src.get("out_tps", 0.0)),
                in_eff=(lv.get("in_tps", 0.0) if (src == "all" and lv
                        and unit == "tokens") else rate_src.get("in_tps", 0.0)),
                out_raw=rc.get("raw_out_tps") or rt.get("raw_out_tps", 0.0),
                in_raw=rc.get("raw_in_tps") or rt.get("raw_in_tps", 0.0),
                tool_ratio=rt.get("tool_ratio", 0.0),
                out_base=h.get("out_tps", bs.get("out_tps", 0.0)),
                in_base=h.get("in_tps", bs.get("in_tps", 0.0)),
                sess_in=lv.get("session_input", 0),
                sess_out=lv.get("session_output", 0),
                sess_req=lv.get("requests", 0),
                credit=lv.get("session_credit", 0.0),
                today_in=td.get("input", 0),
                today_out=td.get("output", 0),
                today_req=td.get("requests", 0),
                today_credit=td.get("credit", 0.0),
                ttft=(lv.get("ttft") if lv.get("ttft") is not None
                      else (s.get("ttft") or {}).get("ttft_ms", 0.0)),
                ttft_est=bool(lv.get("ttft_est")
                              if lv.get("ttft_est") is not None
                              else (s.get("ttft") or {}).get("estimated", True)),
                turn=s.get("turn"),
                running=s.get("running") or [],
                credit_per_min=float(lv.get("credit_per_min")
                                     or rate_src.get("credit_per_min") or 0.0),
                credit_per_req=float(lv.get("credit_per_req")
                                     or rate_src.get("credit_per_req") or 0.0),
                req_per_min=float(lv.get("req_per_min")
                                  or rate_src.get("req_per_min") or 0.0),
                recent_req=int(lv.get("recent_requests")
                               or rate_src.get("requests") or 0),
                stale=(rc.get("requests") or 0) < 1,
                has_tokens=unit == "tokens",
                has_credits=unit == "credits",
                src_note=((bysrc.get(resolved) or {}).get("note", "")
                          if resolved != "all" else ""),
            )
            if lv:
                # 上下文/活跃状态跟着**解析后的实时源**走。all 模式下实时会话
                # 可能是 Qoder，不能拿 WorkBuddy 的 used/size 去冒充。
                upd = float(lv.get("updated_at") or 0)
                live_src = lv.get("src") or resolved
                patch.update(
                    pct=float(lv.get("pct") or 0.0),
                    pct_model=(float(lv.get("pct_model") or 0.0)
                               if live_src == "workbuddy" else 0.0),
                    used=(lv.get("used") or 0) if live_src == "workbuddy" else 0,
                    size=(lv.get("size") or 0) if live_src == "workbuddy" else 0,
                    size_model=((lv.get("size_model") or 0)
                                if live_src == "workbuddy" else 0),
                    active=bool(upd) and (time.time() * 1000 - upd) < 120_000,
                    status=lv.get("status") or "",
                )
        return patch

    def get(self):
        with self.lock:
            return dict(self.snap)

    def stop(self):
        self._stop = True


# ---------------------------------------------------------------- 数据面板
class Panel:
    ROWS = [
        ("out", "下行"), ("in", "上行"), ("raw", "端到端"), ("base", "生成基线"),
        ("ttft", "首字延迟"), ("ctx", "上下文"), ("sess", "本会话"),
        ("today", "今日"), ("bal", "余额"), ("multi", "并行"),
    ]

    def __init__(self, root: Tk, on_pin_toggle=None, on_balance_refresh=None):
        self.root = root
        self.win = Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=KEY)
        try:
            self.win.attributes("-transparentcolor", KEY)
        except Exception:
            pass
        self.on_pin_toggle = on_pin_toggle
        self.on_balance_refresh = on_balance_refresh
        self.pinned = False
        self._logo_cache = {}
        self._bal_sig = None
        self.bal_rows = 0
        self.bal_btn = None
        self.bal_refreshing = False
        self._last = None
        ut.resolve_fonts(self.root)      # 按本机字体族定字阶
        self._build()

    BAL_CARD_W = 0     # 0 = 占满整行
    BAL_CARD_H = 38    # 与网页端 .balitem 的 38px 图标区同高
    BAL_GAP = 7
    RATE_H = 30        # 主读数行（15pt 数字的实际行高 27 + 呼吸）
    ROW_H = 20         # 凹槽内的次级读数行
    WELL_PAD = 9       # 凹槽内边距：比面板 pad 再收一档，形成"嵌进去"的层次
    FORCE_SCALE = None # 调试用：强制 DPI 系数，None 表示按系统

    def _dpi_scale(self) -> float:
        """面板几何的缩放系数（1.0 = 100%）。量化到 1/4 档，避免奇怪的字距。"""
        if self.FORCE_SCALE:
            return float(self.FORCE_SCALE)
        try:
            k = self.win.winfo_fpixels("1i") / 96.0
        except Exception:
            k = 1.0
        return max(0.9, min(2.0, round(k * 4) / 4))

    def _unit_dx(self, text: str) -> int:
        """数值右侧要给单位留出的宽度（按当前字体实测）。"""
        try:
            return self._unit_font.measure(text) + self.unit_gap
        except Exception:
            return self.rate_dx

    def _build(self):
        self.bal_rows = max(0, int(getattr(self, "bal_rows", 0)))
        self.bal_btn = None
        c = Canvas(self.win, bg=KEY, highlightthickness=0, bd=0)
        c.pack()
        self.cv = c

        # 几何随 DPI 等比缩放：Tk 的字号按磅算（会随系统缩放变大），
        # 坐标却是像素；若只放大字不放大间距，125%/150% 下文字就压到行线上。
        k = self.k = self._dpi_scale()
        def S(v):
            return int(round(v * k))

        self.width = S(288)
        self.pad = S(14)
        self.shadow = S(5)
        # 上下文进度条：右侧留给百分比
        self.ctx_x1 = self.width - self.pad - S(11) - S(80)
        self.ctx_x2 = self.width - self.pad - S(11) - S(34)
        self.ctx_dy = S(9)
        f = ut.FONTS or ut.resolve_fonts(self.win)
        # 单位宽度按实际字形量，不能写死：req/min 比 t/s 宽得多，
        # 固定让位 22px 时 "req" 会压在数字上（截图里的 6.0 与 req 重叠）。
        try:
            self._unit_font = tkfont.Font(root=self.win, font=f["tiny"])
        except Exception:
            self._unit_font = None
        self.unit_gap = S(4)

        x0 = self.pad                                  # 内容左边界
        x1 = self.width - self.pad                     # 内容右边界
        wx0, wx1 = x0 - S(2), x1 + S(2)                # 凹槽比内容宽 2px
        ix0, ix1 = wx0 + S(self.WELL_PAD), wx1 - S(self.WELL_PAD)
        self.val_x1 = x1                               # 非凹槽行的数值右界
        self.rate_dx = S(22)                           # 有单位时数值让出的宽度
        self.rate_dy = S(1)

        self.items = {}
        self.row_y = {}
        y = self.pad + S(2)
        cy = y + S(10)                                 # 头部中线

        # ---- 头部：品牌方块 + 标题 + 状态点（与网页顶栏同一套语汇）
        td = S(20)
        ut.round_rect(c, x0, cy - td / 2, x0 + td, cy + td / 2, S(7),
                      fill=ut.TEXT, outline="", tags="brand")
        mark = ((.125, .646), (.317, .438), (.458, .579), (.65, .333), (.875, .667))
        c.create_line(*[v for fx, fy in mark
                        for v in (x0 + td * fx, cy - td / 2 + td * fy)],
                      fill=ut.HERO_OUT, width=max(2, S(2)), capstyle="round",
                      joinstyle="round", tags="brand")
        tx = x0 + td + S(8)
        c.create_text(tx, cy, anchor="w", text="Token 监测", fill=FG,
                      font=f["title"], tags="title")
        # 标题按像素截断，右边还要留给状态字 + 状态光晕
        self.title_max = x1 - tx - S(60)
        c.create_text(x1, cy, anchor="e", text="空闲", fill=FG_FAINT,
                      font=f["tiny"], tags="status")
        dx, dr = x1 - S(50), S(3.5)
        c.create_oval(dx - S(6.5), cy - S(6.5), dx + S(6.5), cy + S(6.5),
                      fill=ut.WELL, outline="", tags="halo")
        c.create_oval(dx - dr, cy - dr, dx + dr, cy + dr,
                      fill=FG_FAINT, outline="", tags="dot")
        y = cy + S(15)
        ut.hairline(c, x0, x1, y)
        y += S(12)

        # ---- 主读数：下行 / 上行。色块 + 大号等宽数字，单位与数字同一条基线
        # num 的行高 27、descent 5，所以数字基线在 y+23；单位按同一条基线摆，
        # 否则 8pt 的 "t/s" 会浮到数字上方，读成两行。
        for key, label, col in (("out", "下行 · 输出", FG_OUT),
                                ("in", "上行 · 输入", FG_IN)):
            sw = S(8)
            ut.round_rect(c, x0, y + S(11), x0 + sw, y + S(11) + sw, S(3),
                          fill=col, outline="", tags=f"swatch_{key}")
            c.create_text(x0 + sw + S(7), y + S(15), anchor="w", text=label,
                          fill=FG_SUB, font=f["row"], tags=f"lbl_{key}")
            self.items[key] = c.create_text(
                x1 - self.rate_dx, y + self.rate_dy, anchor="ne", text="—",
                fill=col, font=f["num"])
            c.create_text(x1, y + S(27), anchor="se", text="t/s",
                          fill=FG_FAINT, font=f["tiny"], tags=f"unit_{key}")
            self.row_y[key] = y
            y += S(self.RATE_H)

        # ---- 凹槽①：派生速率 + 上下文。整组下沉一档，读作"细节"而不是"读数"
        keys1 = [("raw", "端到端"), ("base", "生成基线"), ("ttft", "首字延迟")]
        wy = y + S(2)
        rowh = S(self.ROW_H)
        wh = S(self.WELL_PAD) * 2 + rowh * (len(keys1) + 1)
        ut.round_rect(c, wx0, wy, wx1, wy + wh, S(ut.R_WELL),
                      fill=ut.CARD, outline="", tags="well")
        yr = wy + S(self.WELL_PAD)
        for key, label in keys1:
            c.create_text(ix0, yr + S(4), anchor="w", text=label,
                          fill=FG_FAINT, font=f["tiny"], tags=f"lbl_{key}")
            self.items[key] = c.create_text(ix1, yr + S(2), anchor="ne",
                                            text="—", fill=FG, font=f["val"])
            self.row_y[key] = yr
            yr += rowh
            ut.hairline(c, ix0, ix1, yr, color=ut.WELL_LINE)
        c.create_text(ix0, yr + S(4), anchor="w", text="上下文",
                      fill=FG_FAINT, font=f["tiny"], tags="lbl_ctx")
        c.create_line(self.ctx_x1, yr + self.ctx_dy, self.ctx_x2, yr + self.ctx_dy,
                      fill=ut.TRACK, width=max(2, S(5)), capstyle="round")
        bar = c.create_line(self.ctx_x1, yr + self.ctx_dy, self.ctx_x1, yr + self.ctx_dy,
                            fill=FG_OK, width=max(2, S(5)), capstyle="round")
        txt = c.create_text(ix1, yr + S(2), anchor="ne", text="—",
                            fill=FG_SUB, font=f["val"], tags="v_ctx")
        self.items["ctx"] = (bar, txt)
        self.row_y["ctx"] = yr
        y = wy + wh + S(12)

        # ---- 分组线：以上是"实时"，以下是"累计"
        ut.hairline(c, x0, x1, y)
        y += S(12)

        keys2 = [("sess", "本会话"), ("today", "今日"), ("multi", "并行")]
        wy = y
        wh = S(self.WELL_PAD) * 2 + rowh * len(keys2)
        ut.round_rect(c, wx0, wy, wx1, wy + wh, S(ut.R_WELL),
                      fill=ut.CARD, outline="", tags="well")
        yr = wy + S(self.WELL_PAD)
        for i, (key, label) in enumerate(keys2):
            c.create_text(ix0, yr + S(4), anchor="w", text=label,
                          fill=FG_FAINT, font=f["tiny"], tags=f"lbl_{key}")
            self.items[key] = c.create_text(ix1, yr + S(2), anchor="ne", text="—",
                                            fill=FG_FAINT, font=f["val"])
            self.row_y[key] = yr
            yr += rowh
            if i < len(keys2) - 1:
                ut.hairline(c, ix0, ix1, yr, color=ut.WELL_LINE)
        y = wy + wh

        # ---- 余额卡：没有任何可显示的余额时，整块不占位
        self.row_y["bal"] = y
        if self.bal_rows:
            y += S(14)
            c.create_text(x0, y, anchor="nw", text="余额", fill=FG_FAINT,
                          font=f["tiny"], tags="lbl_bal")
            # 小刷新按钮：和「余额」同一行，靠右；点击只刷新余额，不改钉住状态
            bw, bh = S(52), S(20)
            bx0, by = x1 - bw, y - S(2)
            self.bal_btn = (bx0, by, x1, by + bh)
            refreshing = bool(getattr(self, "bal_refreshing", False))
            ut.round_rect(c, bx0, by, x1, by + bh, S(6),
                          fill=ut.CARD, outline="",
                          tags=("balrefresh", "balrefresh_bg"))
            c.create_text((bx0 + x1) / 2, by + bh / 2 - S(1),
                          text="刷新中" if refreshing else "刷新",
                          fill=FG_FAINT if refreshing else FG_IN,
                          font=f["tiny"], tags=("balrefresh", "balrefresh_t"))
            c.bind("<Button-1>", self._on_panel_click)
            c.bind("<Motion>", self._on_panel_motion)
            c.bind("<Leave>", self._on_panel_leave)
            y += S(16)
            self.row_y["bal"] = y
            y += (S(self.BAL_CARD_H) * self.bal_rows
                  + S(self.BAL_GAP) * (self.bal_rows - 1))

        # ---- 底部提示
        y += S(12)
        ut.hairline(c, x0, x1, y)
        y += S(8)
        self.hint = c.create_text(x0, y, anchor="nw",
                                  text="单击钉住 · 双击仪表盘 · 右键菜单",
                                  fill=ut.TEXT_HINT, font=f["tiny"])
        bottom = y + S(14)

        # 接触阴影 + 圆角底 + 发丝描边（最后画，再压到最底层）
        self.height = bottom + self.shadow
        ut.contact_shadow(c, S(1), S(1), self.width - S(1), bottom,
                          S(ut.R_CARD), self.shadow, tags="bg")
        ut.round_rect(c, S(1), S(1), self.width - S(1), bottom,
                      S(ut.R_CARD), fill=BG, outline=BORDER, tags="bg")
        c.tag_lower("bg")
        c.configure(width=self.width, height=self.height)

    # ---------------- 更新
    def _rebuild(self):
        for w in self.win.winfo_children():
            w.destroy()
        self._logo_cache = {}
        self._bal_sig = None        # 画布重建后必须重画余额卡
        self.items = {}
        self.row_y = {}
        self._build()

    def update(self, s: dict, cfg: dict, basis: str, balances):
        self._last = (s, cfg, basis, balances)
        c = self.cv
        # has_* 由 Sampler 按"当前源到底有没有数"给出，别用能力位去猜：
        # Claude 理论上能记 token，但本机一条记录都没有，这时也该显示「—」。
        has_tok = bool(s.get("has_tokens", True))
        has_cred = bool(s.get("has_credits"))
        src_label = s.get("src_label") or ""
        model = _pretty(s.get("model") or "")
        if s.get("src") and s.get("src") != "all" and model:
            head = f"{src_label}·{model}"
        else:
            head = model or src_label or "—"
        self._fit_title(head)
        status = ("运行中" if s.get("status") == "working"
                  else "活跃" if s.get("active") else "空闲")
        # 只让圆点带状态色，文字保持次级灰 —— 与网页顶栏"圆点 + 灰字"一致
        c.itemconfig("status", text=status, fill=FG_FAINT)
        live = status != "空闲"
        c.itemconfig("dot", fill=FG_OK if live else FG_FAINT)
        c.itemconfig("halo", fill=ut.TINT_OK if live else ut.WELL)

        unit = s.get("unit") or ("tokens" if has_tok else
                                 "credits" if has_cred else "")
        lbl_out, lbl_in = "下行 · 输出", "上行 · 输入"
        u_out = u_in = "t/s"
        if unit == "credits":
            lbl_out, lbl_in = "积分 · 每分钟", "请求 · 每分钟"
            u_out, u_in = "cr/min", "req/min"
            o = float(s.get("credit_per_min") or 0.0)
            i = float(s.get("req_per_min") or 0.0)
        elif unit == "requests":
            lbl_out, lbl_in = "请求 · 每分钟", "上行 · 输入"
            u_out = "req/min"
            o = float(s.get("req_per_min") or 0.0)
            i = 0.0
        elif unit == "tokens":
            if basis == "base":
                o, i = s["out_base"], s["in_base"]
            elif basis == "raw":
                o, i = s["out_raw"], s["in_raw"]
            else:
                o, i = s["out_eff"], s["in_eff"]
        else:
            o = i = 0.0

        c.itemconfig("lbl_out", text=lbl_out)
        c.itemconfig("lbl_in", text=lbl_in,
                     state="normal" if unit in ("tokens", "credits") else "hidden")
        c.itemconfig("unit_out", text=u_out)
        c.itemconfig("unit_in", text=u_in)

        if unit in ("tokens", "credits", "requests"):
            dx_o = self._unit_dx(u_out)
            dx_i = self._unit_dx(u_in)
            c.itemconfig(self.items["out"],
                         text=_rate(o), fill=FG_OUT if o else FG_FAINT)
            c.itemconfig(f"unit_out", state="normal")
            c.coords(self.items["out"], self.val_x1 - dx_o,
                     self.row_y["out"] + self.rate_dy)
            if unit in ("tokens", "credits"):
                c.itemconfig(self.items["in"],
                             text=_rate(i), fill=FG_IN if i else FG_FAINT)
                c.itemconfig("unit_in", state="normal")
                c.coords(self.items["in"], self.val_x1 - dx_i,
                         self.row_y["in"] + self.rate_dy)
            else:
                c.itemconfig(self.items["in"], text="—", fill=FG_FAINT)
                c.itemconfig("unit_in", state="hidden")
                c.coords(self.items["in"], self.val_x1,
                         self.row_y["in"] + self.rate_dy)

            if unit == "tokens":
                c.itemconfig("lbl_raw", text="端到端")
                c.itemconfig(self.items["raw"],
                             text=f"{_rate(s['out_raw'])} t/s · 工具 {s['tool_ratio'] * 100:.0f}%",
                             fill=FG)
                c.itemconfig("lbl_base", text="生成基线")
                c.itemconfig(self.items["base"],
                             text=f"{_rate(s['out_base'])} t/s", fill=FG)
            elif unit == "credits":
                c.itemconfig("lbl_raw", text="每次请求")
                c.itemconfig(self.items["raw"],
                             text=f"{float(s.get('credit_per_req') or 0):.2f} cr",
                             fill=FG)
                c.itemconfig("lbl_base", text="近期请求")
                c.itemconfig(self.items["base"],
                             text=f"{int(s.get('recent_req') or 0)} 次", fill=FG)
            else:
                c.itemconfig("lbl_raw", text="近期请求")
                c.itemconfig(self.items["raw"],
                             text=f"{int(s.get('recent_req') or 0)} 次", fill=FG)
                c.itemconfig("lbl_base", text="生成基线")
                c.itemconfig(self.items["base"], text="—", fill=FG_FAINT)
        else:
            # 没有可显示的单位时一律「—」：写成 0 t/s 会被读成"生成很慢"，
            # 而真相是"没有这项数据"。
            for k in ("out", "in", "raw", "base"):
                c.itemconfig(self.items[k], text="—", fill=FG_FAINT)
            c.itemconfig("lbl_raw", text="端到端")
            c.itemconfig("lbl_base", text="生成基线")
            # 单位一起收起，避免出现"— t/s"；数值回到右边缘，保证右边界对齐
            for k in ("out", "in"):
                c.itemconfig(f"unit_{k}", state="hidden")
                c.coords(self.items[k], self.val_x1, self.row_y[k] + self.rate_dy)

        ttft = s.get("ttft") or 0
        est = bool(s.get("ttft_est", True))
        c.itemconfig(self.items["ttft"],
                     text=(f"{'≈' if est else ''}"
                           + (f"{ttft / 1000:.1f}s" if ttft >= 1000
                              else f"{ttft:.0f}ms")) if ttft else "—")

        pct = float(s.get("pct") or 0)
        bar, txt = self.items["ctx"]
        ry = self.row_y["ctx"] + self.ctx_dy
        if pct > 0:
            # 只有比例、拿不到绝对容量（Qoder 的 context_usage_ratio）时，
            # 画不出"已用/总容量"的语义，但比例本身是真实的，照样画条 + 百分比。
            col = _ctx_color(pct)
            span = self.ctx_x2 - self.ctx_x1
            x2 = self.ctx_x1 + max(3, int(span * min(1.0, pct)))
            c.coords(bar, self.ctx_x1, ry, x2, ry)
            c.itemconfig(bar, fill=col, state="normal")
            c.itemconfig(txt, text=f"{pct * 100:.0f}%", fill=col)
        else:
            c.itemconfig(bar, state="hidden")
            c.itemconfig(txt, text="—", fill=FG_SUB)

        if has_tok:
            c.itemconfig(self.items["sess"],
                         text=f"↑{_fmt(s['sess_in'])} ↓{_fmt(s['sess_out'])}", fill=FG)
            c.itemconfig(self.items["today"],
                         text=f"↑{_fmt(s['today_in'])} ↓{_fmt(s['today_out'])}", fill=FG)
        elif has_cred:
            # 有 credits 没 token：显示请求数与积分，这才是该源的真实口径
            c.itemconfig(self.items["sess"],
                         text=f"{s.get('sess_req', 0)} 次 · {s.get('credit', 0.0):.1f} cr",
                         fill=FG)
            c.itemconfig(self.items["today"],
                         text=f"{s['today_req']} 次 · {s['today_credit']:.1f} cr", fill=FG)
        else:
            c.itemconfig(self.items["sess"],
                         text=f"{s.get('sess_req', 0)} 次请求", fill=FG)
            c.itemconfig(self.items["today"],
                         text=f"{s['today_req']} 次请求", fill=FG)

        # 余额：每个服务商一块 logo 小卡（横向排列）。
        # 卡片数量变化会重建画布，此时 c 已失效，必须立刻返回 ——
        # 递归那次 update 已经在新的画布上把剩下的行画完了。
        if self._render_balance_cards(c, balances or []):
            return

        run = s.get("running") or []
        c.itemconfig(self.items["multi"],
                     text=f"{len(run)} 个会话" if len(run) > 1 else "—",
                     fill=FG if len(run) > 1 else FG_FAINT)

        c.itemconfig(self.hint,
                     text="已钉住 · 单击取消" if self.pinned
                     else "单击钉住 · 双击仪表盘 · 右键菜单")

    def _fit_title(self, head: str) -> None:
        """
        按实际像素宽截断标题。

        各系统/各字号下同一串字符的宽度不同，按"字数"硬切会在 macOS 或
        高 DPI 上溢出、在窄字体上留白。这里用画布实测宽度，逐次收到放得下为止。
        """
        c = self.cv
        avail = self.title_max
        text = head
        c.itemconfig("title", text=text)
        for _ in range(20):
            bb = c.bbox("title")
            if not bb or (bb[2] - bb[0]) <= avail or len(text) <= 3:
                return
            text = text[:-2] + "…"
            c.itemconfig("title", text=text)

    # ---------------- 余额小卡
    def _logo_size(self) -> int:
        """按缩放选图标尺寸（底座 26px，图标留 3px 内边距，与网页 .logo 同比）。"""
        if self.k >= 1.45:
            return 32
        if self.k >= 1.2:
            return 24
        return 20

    def _logo(self, name: str):
        size = self._logo_size()
        name = name if name in _BRAND_NAMES else "custom"
        key = (name, size)
        if key in self._logo_cache:
            return self._logo_cache[key]
        p = LOGOS / f"{name}@{size}.png"
        if not p.exists():
            p = LOGOS / f"custom@{size}.png"
        if not p.exists():
            p = LOGOS / "custom@16.png"
        img = None
        if p.exists():
            try:
                img = PhotoImage(file=str(p))
            except Exception:
                img = None
        self._logo_cache[key] = img
        return img

    def _render_balance_cards(self, c, balances):
        """余额画成整行卡片（logo + 厂名 + 金额），数量变化时重建布局。"""
        rows = list(balances)
        need_rows = len(rows)
        if need_rows != self.bal_rows:
            self.bal_rows = need_rows
            last = self._last
            self._rebuild()
            if last:
                try:
                    self.update(*last)
                except Exception:
                    pass
            return True

        if not rows:
            c.delete("balchip")
            self._bal_sig = None
            return False

        sig = tuple((b.get("template") or "custom", b.get("label"), b.get("ok"),
                     b.get("text"), b.get("currency"), b.get("error")) for b in rows)
        if sig == self._bal_sig:
            return False
        self._bal_sig = sig
        c.delete("balchip")

        k = self.k
        def S(v):
            return int(round(v * k))
        ch, gap = S(self.BAL_CARD_H), S(self.BAL_GAP)
        cw = self.width - self.pad * 2
        x0 = self.pad
        y0 = self.row_y["bal"]

        for i, b in enumerate(rows):
            x = x0
            y = y0 + i * (ch + gap)
            # 与网页端 .balitem 同构：白底 + 发丝描边 + 11px 圆角，
            # 厂牌图标放进次级灰底座，避免"白 logo 贴白卡"糊成一片。
            ut.round_rect(c, x, y, x + cw, y + ch, S(ut.R_WELL - 1), fill=BG,
                          outline=BORDER, tags="balchip")
            td = S(26)
            tx, ty = x + S(6), y + (ch - td) // 2
            ut.round_rect(c, tx, ty, tx + td, ty + td, S(ut.R_PLATE), fill=ut.CARD,
                          outline="", tags="balchip")
            img = self._logo(_brand_for(b))
            if img is not None:
                # 按图片实际像素居中：各品牌 PNG 的透明留白不一样，
                # 按文件名标注的尺寸算会看起来偏上/偏左。
                iw, ih = img.width(), img.height()
                c.create_image(tx + (td - iw) // 2, ty + (td - ih) // 2,
                               anchor="nw", image=img, tags="balchip")
            name = str(b.get("label") or _short_label(b.get("label")))
            if len(name) > 12:
                name = name[:11] + "…"
            c.create_text(tx + td + S(9), y + ch / 2, anchor="w", text=name,
                          fill=FG_SUB, font=ut.font("row"), tags="balchip")
            ok = bool(b.get("ok"))
            txt = (f"{b.get('currency', '')}{b.get('text', '')}"
                   if ok else "查询失败")
            c.create_text(x + cw - S(11), y + ch / 2, anchor="e", text=txt,
                          fill=FG if ok else FG_WARN, font=ut.font("bal"),
                          tags="balchip")
        return False

    # ---------------- 余额刷新按钮
    def _refresh_hit(self, x: int, y: int) -> bool:
        if not self.bal_btn:
            return False
        x0, y0, x1, y1 = self.bal_btn
        return x0 <= x <= x1 and y0 <= y <= y1

    def _on_panel_motion(self, e):
        if self.bal_refreshing:
            self.cv.configure(cursor="watch")
            self.cv.itemconfig("balrefresh_bg", fill=ut.CARD)
        elif self._refresh_hit(e.x, e.y):
            self.cv.configure(cursor="hand2")
            self.cv.itemconfig("balrefresh_bg", fill=ut.TINT_IN)
        else:
            self.cv.configure(cursor="")
            self.cv.itemconfig("balrefresh_bg", fill=ut.CARD)

    def _on_panel_leave(self, _e):
        if not self.bal_btn:
            return
        self.cv.configure(cursor="")
        self.cv.itemconfig("balrefresh_bg", fill=ut.CARD)

    def _on_panel_click(self, e):
        if self.bal_refreshing or not self._refresh_hit(e.x, e.y):
            return
        if self.on_balance_refresh:
            self.on_balance_refresh()

    def set_balance_refreshing(self, on: bool):
        self.bal_refreshing = bool(on)
        if not self.bal_btn:
            return
        try:
            self.cv.itemconfig("balrefresh_t",
                               text="刷新中" if on else "刷新",
                               fill=FG_FAINT if on else FG_IN)
            self.cv.itemconfig("balrefresh_bg", fill=ut.CARD)
            self.cv.configure(cursor="watch" if on else "")
        except Exception:
            pass

    # ---------------- 显示控制
    def show_at(self, px: int, py: int, pet_w: int, pet_h: int):
        w, h = self.width, self.height
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x = px - w - 8
        if x < 4:
            x = px + pet_w + 8
        y = py + pet_h - h
        x = max(4, min(x, sw - w - 4))
        y = max(4, min(y, sh - h - 4))
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        self.win.deiconify()
        try:
            self.win.lift()      # 只提升 z 序；不要 focus_force，否则会抢走 WorkBuddy 输入焦点
        except Exception:
            pass
        force_topmost(self.win)

    def hide(self):
        self.win.withdraw()

    def visible(self) -> bool:
        return bool(self.win.winfo_ismapped())

    def contains(self, x: int, y: int) -> bool:
        """指针是否在面板窗口内（用于把面板本身也算作悬停区）。"""
        if not self.visible():
            return False
        try:
            wx, wy = self.win.winfo_rootx(), self.win.winfo_rooty()
            return wx <= x < wx + self.width and wy <= y < wy + self.height
        except Exception:
            return False


# ---------------------------------------------------------------- 主程序
class PetApp:
    # 所有自续期定时器的间隔统一在这里定义。
    # 曾经这些数字在 __init__ 和各自的回调里各写一份，_poll_hover 里还多起了
    # 一份 _look_loop/_dock_tick/_watch_config 链，而那三个回调自己也会续期，
    # 于是链数每 140ms +1 且永不停止 —— 实测 15 分钟就把一颗核吃满、拖动不跟手。
    TICK_MS = 1200        # 状态机刷新
    HOVER_MS = 100        # 悬停判定
    INPUT_MS = 30         # 鼠标事件派发；直接决定点击/拖动的"跟手"程度
    LOOK_MS = 70          # 注视鼠标
    DOCK_MS = 250         # 贴靠跟随
    CFG_MS = 2000         # 配置热重载
    FS_MS = 2500          # 全屏检测

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = Tk()
        self.root.withdraw()
        self.root.title("Token 监测")
        ut.resolve_fonts(self.root)     # 先定字阶，再建面板/气泡/菜单

        self.sprite = petsprite.Sprite(scale=cfg["pet"]["scale"], master=self.root)
        if not self.sprite.ok:
            raise RuntimeError("精灵表加载失败，请检查 assets/")

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        self.win = petwin.LayeredWindow(
            self.root, self.sprite.width, self.sprite.height, self._on_pet_event
        )
        self.player = petsprite.Player(self.root, self.sprite, self._on_frame)

        self.source = (cfg.get("source") or "all")
        if self.source not in _SOURCE_IDS:
            self.source = "all"
        self.sampler = Sampler(self.source)
        self.panel = Panel(self.root, on_balance_refresh=self.refresh_balances_now)
        self.bubble = Bubble(self.root)

        self.basis = cfg.get("basis", "eff")
        self.pinned = False
        self._hover = False
        self._hover_job = None
        self._frame_name = "idle"
        self._frame_idx = 0
        self._alpha_ts = 0
        self._balances = []
        self._balance_ts = 0
        self._bal_refreshing = False
        self._bal_refresh_result = None
        self._bal_refresh_deadline = 0.0
        self._alert_ts = {}
        self._credit_band = 0      # 今日积分已提示到第几档（防止每 5 分钟重复弹）
        self._last_turn_ts = 0
        self._look_ts = 0
        self._looking = False
        self._look_near = False     # 注视回滞状态
        self._look_t = None         # 平滑后的注视角度
        self._dir_idx = -1
        self._wander_defer_start = 0.0
        self._cfg_mtime = self._mtime()
        self._wander_job = None
        self._walk_dir = 0
        self._hidden_by_fs = False
        self._shown = True
        self._pin_job = None
        self._panel_pos = None
        self._closing = False
        self._pending_bubbles = []

        self.msgwin = win32ui.HiddenWindow(self.root, self._on_hotkey, self._on_tray)
        self.tray = None
        try:
            icon_img = self.sprite.frames("idle")[0][0]
            self.tray = win32ui.Tray(self.msgwin.hwnd, "Token 监测",
                                     icon_img, self._on_tray)
        except Exception:
            self.tray = None

        self.hotkey = None
        try:
            self.hotkey = win32ui.Hotkey(
                self.msgwin.hwnd, cfg["system"].get("hotkey", "ctrl+alt+w"),
                self._toggle_panel,
            )
        except Exception:
            self.hotkey = None

        self._build_menu()
        self._place_initial()
        self.player.play(petsprite.ANIM_IDLE)
        self.sampler.start()

        self.root.after(600, self._tick)
        self.root.after(self.INPUT_MS, self._poll_input)
        self.root.after(self.HOVER_MS, self._poll_hover)
        self.root.after(self.LOOK_MS, self._look_loop)
        self.root.after(self.DOCK_MS, self._dock_tick)
        self.root.after(self.CFG_MS, self._watch_config)
        self.root.after(self.FS_MS, self._fs_watch)
        self._schedule_wander()
        self.root.after(700, self._refresh_balance_async)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # ---------------------------------------------------------------- 位置
    def _place_initial(self):
        st = {}
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        x = st.get("x", self.cfg["pos"].get("x"))
        y = st.get("y", self.cfg["pos"].get("y"))
        w, h = self.sprite.width, self.sprite.height
        if x is None or y is None:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = sw - w - 60
            y = sh - h - 120
        self.win.move_to(*_clamp_to_virtual_screen(int(x), int(y), w, h))

    def _save_pos(self):
        x, y = self.win.position()
        try:
            STATE_FILE.write_text(json.dumps({"x": int(x), "y": int(y),
                                              "basis": self.basis}), encoding="utf-8")
        except Exception:
            pass

    # ---------------------------------------------------------------- 帧渲染
    def _on_frame(self, img, name, idx):
        self._frame_name, self._frame_idx = name, idx
        data, w, h = self.sprite.bgra(name, idx)
        if data:
            self.win.set_frame(data, w, h)

    def _show_still(self, degrees: float):
        data, w, h = self.sprite.bgra_dir(degrees)
        if data:
            self.win.set_frame(data, w, h)

    # ---------------------------------------------------------------- 状态机
    def _tick(self):
        try:
            self._apply_state()
        except Exception as exc:
            if os.environ.get("WB_MONITOR_DEBUG"):
                print("[tick]", exc, file=sys.stderr)
        self.root.after(self.TICK_MS, self._tick)

    def _apply_state(self):
        s = self.sampler.get()
        now = time.time()
        pct = float(s.get("pct") or 0)
        alerts = self.cfg.get("alerts") or {}
        crit = float(alerts.get("ctx_crit") or 0.95)
        warn = float(alerts.get("ctx_warn") or 0.80)

        # ---- 告警优先级最高
        if s.get("size") and pct >= crit:
            self.player.play(petsprite.ANIM_FAIL)
            self._alert("ctx_crit", f"上下文已用 {pct * 100:.0f}%，建议新开会话")
            return
        if s.get("size") and pct >= warn:
            self._alert("ctx_warn", f"上下文已达 {pct * 100:.0f}%")

        # 今日积分：只在"跨过新的阈值档"时提示一次（500 → 1000 → 1500 …）。
        # 原来只要超过阈值就每 cooldown_sec（默认 300 s）弹一次，一整天都在响。
        credit_warn = float(alerts.get("credit_warn") or 0)
        credit_now = float(s.get("today_credit") or 0)
        band = credit_band(credit_now, credit_warn)
        if band > 0:
            if band > self._credit_band:
                self._credit_band = band
                self._alert(
                    "credit",
                    f"今日积分 {credit_now:,.0f} · 阈值 {credit_warn:,.0f}",
                    title="积分提醒")
        else:
            self._credit_band = 0      # 跨天或调高阈值后重新计档

        # 漫步/注视时动画帧由各自的状态机独占；否则 WORK/WAIT/OK 会把
        # 走路帧或注视帧抢掉，看起来"走一半突然原地跑步"。
        if self._walk_dir or self._looking:
            return

        # ---- 一轮刚完成
        turn = s.get("turn") or {}
        tts = float(turn.get("ts") or 0)
        if tts and tts > self._last_turn_ts:
            self._last_turn_ts = tts
            if now * 1000 - tts < 6000:
                self.player.play(petsprite.ANIM_OK, once=True,
                                 on_done=lambda: self.player.play(petsprite.ANIM_IDLE))
                return

        # ---- 正在生成
        if s.get("status") == "working" or (s.get("active") and not s.get("stale")):
            if self.player.current() != petsprite.ANIM_WORK:
                self.player.play(petsprite.ANIM_WORK)
            return

        # ---- 等待（会话仍活跃但暂无请求）
        if s.get("active"):
            if self.player.current() != petsprite.ANIM_WAIT:
                self.player.play(petsprite.ANIM_WAIT)
            return

        # 面板可见时保持实时刷新（否则钉住后就定格了）
        if self.panel.visible():
            self._show_panel()

        # ---- 空闲：漫步中或正在注视则不动，其余回 idle
        if self._walk_dir or self._looking:
            return
        if self.player.current() != petsprite.ANIM_IDLE:
            self.player.play(petsprite.ANIM_IDLE)
        else:
            self.player.resume()

    # ---------------------------------------------------------------- 交互
    def _on_pet_event(self, kind: str, mx: int, my: int):
        if kind == "click":
            self._play_once(petsprite.ANIM_WAVE)
            self.pinned = not self.pinned
            self.panel.pinned = self.pinned
            if self.pinned:
                self._show_panel()
            else:
                self._maybe_hide_panel()
        elif kind == "dblclick":
            self._play_once(petsprite.ANIM_JUMP)
            self.open_dashboard()
        elif kind == "right":
            self._popup_menu(mx, my)
        elif kind == "drag":
            self._end_walk(save=False)
            self.panel.hide()
        elif kind == "drag_end":
            dock = self.cfg.get("dock") or {}
            if dock.get("enabled"):
                info = _dock_hwnd_rect(dock.get("target") or "workbuddy")
                if info:
                    _h, rect = info
                    px, py = self.win.position()
                    dock["margin_x"] = max(0, int(rect[2] - px - self.sprite.width))
                    dock["margin_y"] = max(0, int(rect[3] - py - self.sprite.height))
                    prefs.save(self.cfg)
            else:
                self._save_pos()
        elif kind == "press":
            pass

    def _play_once(self, name: str):
        self.player.play(name, once=True,
                         on_done=lambda: self.player.play(petsprite.ANIM_IDLE))

    def _poll_input(self):
        """唯一允许接触 Tk 的事件消费点（WndProc 只入队）。"""
        try:
            self.win.poll()
            self.msgwin.poll(self._on_hotkey, self._on_tray)
        except Exception:
            pass
        while self._pending_bubbles:
            try:
                rows, title, timeout = self._pending_bubbles.pop(0)
                x, y = self.win.position()
                self.bubble.show(rows, x, y, title=title, timeout=timeout)
            except Exception:
                pass
        self.root.after(self.INPUT_MS, self._poll_input)

    def _look_loop(self):
        try:
            self._update_look()
        except Exception:
            pass
        self.root.after(self.LOOK_MS, self._look_loop)

    # 靠近到多少像素内就"一定看过来"（生成中也看）。带 25% 回滞，避免边界抖动。
    LOOK_NEAR = 320.0
    LOOK_NEAR_EXIT = 1.25

    def _look_radius(self) -> float:
        pet = self.cfg.get("pet") or {}
        return min(float(pet.get("look_range") or 520), self.LOOK_NEAR)

    def _update_look(self):
        pet = self.cfg.get("pet") or {}
        if not pet.get("look_tracking", True) or self._walk_dir or self._hidden_by_fs:
            return self._stop_look()

        pos = _cursor_pos()
        if not pos:
            return self._stop_look()
        px, py = pos
        x, y = self.win.position()
        cx = x + self.sprite.width * 0.5
        cy = y + self.sprite.height * 0.45
        dx, dy = px - cx, py - cy
        dist = (dx * dx + dy * dy) ** 0.5

        # 回滞：进入 320px 开始看，离开 400px 才停
        near_r = self._look_radius()
        if dist <= near_r:
            self._look_near = True
        elif dist > near_r * self.LOOK_NEAR_EXIT:
            self._look_near = False

        # 原实现只要"有采样"就关掉注视，而 WorkBuddy 几乎一直有采样，
        # 这个功能形同不存在。现在只有**模型真的在生成**时才让动画优先，
        # 且鼠标贴近时始终看过去。
        if not self._look_near:
            if self.sampler.get().get("status") == "working":
                return self._stop_look()

        if dist > float(pet.get("look_range") or 520):
            return self._stop_look()

        # 图集只有 16 个水平旋转帧，没有抬头/低头，见 look_atlas 的说明。
        target = look_atlas(dx, dy, float(pet.get("look_offset", 270)))
        if self._look_t is None:
            self._look_t = target
        else:
            # 最短弧插值：鼠标从左边移到右边时不绕一整圈
            diff = ((target - self._look_t + 180.0) % 360.0) - 180.0
            self._look_t = (self._look_t + diff * 0.38) % 360.0

        n = max(1, self.sprite.dir_count())
        frame = 360.0 / n
        idx = int(round(self._look_t / frame)) % n
        self._looking = True
        if idx == self._dir_idx:      # 方向没变就不重绘
            return
        now = time.time()
        if now - self._look_ts < 0.09:  # 帧间去抖：鼠标微动不换帧
            return
        self._dir_idx = idx
        self._look_ts = now
        self.player.pause()
        data, w, h = self.sprite.bgra_dir(self._look_t)
        if data:
            self.win.set_frame(data, w, h)

    def _stop_look(self):
        if self._looking:
            self._looking = False
            self._dir_idx = -1
            self._look_t = None
            self._look_ts = 0
            self.player.resume()
        else:
            self._look_t = None

    def _set_shown(self, show: bool):
        """统一控制桌宠与面板的可见性（贴靠模式频繁调用）。"""
        if show and not self._shown:
            self._shown = True
            self.win.show()
        elif not show and self._shown:
            self._shown = False
            self.panel.hide()
            self.win.hide()

    def _set_dock_target(self, target: str, save: bool = True):
        dock = self.cfg.setdefault("dock", {})
        changed = dock.get("target") != target
        dock["target"] = target
        # 换目标后句柄粘滞缓存必须作废，否则继续粘在原应用窗口上
        _DOCK_CACHE["hwnd"] = None
        _DOCK_CACHE["target"] = target
        if save and changed:
            prefs.save(self.cfg)
        try:
            self.var_dock_target.set(target)   # 菜单按变量渲染，必须同步
        except Exception:
            pass
        if dock.get("enabled"):
            self._apply_dock(dock)

    def _toggle_dock(self):
        dock = self.cfg.setdefault("dock", {})
        dock["enabled"] = bool(self.var_dock.get())
        if dock["enabled"] and self.cfg["pet"].get("wander"):
            # 贴靠与漫步互斥：跟随时不能自己走开
            self.cfg["pet"]["wander"] = False
            try:
                self.var_wander.set(False)
            except Exception:
                pass
            self._end_walk(save=False)
        prefs.save(self.cfg)
        if dock["enabled"]:
            self._stop_look()
            self._apply_dock(dock)
        else:
            self._set_shown(True)
            self._save_pos()

    def _dock_tick(self):
        try:
            dock = self.cfg.get("dock") or {}
            if dock.get("enabled"):
                self._apply_dock(dock)
        except Exception:
            pass
        self.root.after(self.DOCK_MS, self._dock_tick)

    def _apply_dock(self, dock: dict):
        """贴靠：位置跟随窗口；找不到/最小化/全屏/非前台 时隐藏。"""
        info = _dock_hwnd_rect(dock.get("target") or "workbuddy")
        if not info:
            self._set_shown(False)
            return
        hwnd, rect = info
        if _is_minimized(hwnd):
            self._set_shown(False)
            return
        if dock.get("hide_fullscreen", True) and _rect_is_fullscreen(rect):
            self._set_shown(False)
            return
        if dock.get("hide_unfocused", True) and not _is_foreground(hwnd):
            self._set_shown(False)
            return

        w, h = self.sprite.width, self.sprite.height
        mx = int(dock.get("margin_x", 24))
        my = int(dock.get("margin_y", 14))
        x = int(rect[2] - w - mx)
        y = int(rect[3] - h - my)
        if dock.get("follow", True):
            if self.win.position() != (x, y):
                self.win.move_to(x, y)
        self._set_shown(True)
        if self.panel.visible() and self._panel_pos != (x, y):
            self._panel_pos = (x, y)
            self.panel.show_at(x, y, w, h)

    def _poll_hover(self):
        try:
            x, y = self.win.position()
            w, h = self.sprite.width, self.sprite.height
            px, py = self.root.winfo_pointerxy()
            inside_pet = (x <= px <= x + w) and (y <= py <= y + h)
            # 面板本身也算悬停区：从桌宠挪到面板上的刷新按钮时不会中途收起
            inside = inside_pet or self.panel.contains(px, py)
            if inside and not self._hover:
                self._hover = True
                if self._hover_job:
                    try:
                        self.root.after_cancel(self._hover_job)
                    except Exception:
                        pass
                    self._hover_job = None
                if inside_pet:
                    self._show_panel()
            elif not inside and self._hover:
                self._hover = False
                self._maybe_hide_panel()
        except Exception:
            pass
        self.root.after(self.HOVER_MS, self._poll_hover)

    def _show_panel(self):
        if self._hidden_by_fs or not self._shown:
            return
        s = self.sampler.get()
        try:
            self.panel.update(s, self.cfg, self.basis, self._balances)
        except Exception as exc:
            if os.environ.get("WB_MONITOR_DEBUG"):
                print("[panel]", exc, file=sys.stderr)
        x, y = self.win.position()
        self._panel_pos = (x, y)
        self.panel.show_at(x, y, self.sprite.width, self.sprite.height)

    def _maybe_hide_panel(self):
        if self.pinned:
            return
        if self._hover_job:
            try:
                self.root.after_cancel(self._hover_job)
            except Exception:
                pass
        self._hover_job = self.root.after(420, self.panel.hide)

    def _toggle_panel(self):
        """热键 / 托盘：显示或隐藏数据面板。"""
        if self.panel.visible():
            self.pinned = False
            self.panel.pinned = False
            self.panel.hide()
        else:
            self.pinned = True
            self.panel.pinned = True
            self._show_panel()

    # ---------------------------------------------------------------- 菜单
    def _build_menu(self):
        """
        只在这里建"状态变量"和菜单宿主，不再建 Tk 的 Menu 控件。

        原来用 tk.Menu 时，勾选态与标签要靠 Checkbutton/entryconfig 同步，
        热重载后容易和配置脱节（例如贴靠目标改了、菜单标签还是旧的）。
        现在菜单是纯渲染：每次右键都按当前变量/配置重新生成一份 spec。
        """
        self.basis_var = StringVar(value=self.basis)
        # 与 var_scale 同一个教训：必须共用一个变量。每项各建匿名 StringVar 的话
        # 既无法互斥，又会因无人持引用被 GC，子菜单永远不显示勾选态。
        self.var_source = StringVar(value=self.source)
        self.var_scale = DoubleVar(value=float(self.cfg["pet"]["scale"]))
        self.var_dock = BooleanVar(value=bool((self.cfg.get("dock") or {}).get("enabled")))
        self.var_dock_target = StringVar(
            value=(self.cfg.get("dock") or {}).get("target") or "workbuddy")
        self.var_wander = BooleanVar(value=self.cfg["pet"].get("wander", False))
        self.var_look = BooleanVar(value=self.cfg["pet"].get("look_tracking", True))
        self.var_sound = BooleanVar(value=self.cfg["alerts"].get("sound", True))
        self.var_auto = BooleanVar(value=win32ui.is_autostart(BASE_DIR))
        self.var_fs = BooleanVar(value=self.cfg["system"].get("hide_on_fullscreen", True))
        self.menu = petmenu.Menu(self.root)

    def _flip(self, var, fn):
        """
        勾选项的点击动作：先翻转变量，再调用原处理器。

        原生 tk.Menu 的 Checkbutton 会在回调前自动翻转关联变量，所以
        `_toggle_dock` 这类处理器都是"读变量 = 新状态"。换成自绘菜单后没有
        控件去翻转它，读到的永远是旧值 —— 表现就是点了没反应。这里补回这层语义。
        """
        def run():
            try:
                var.set(not bool(var.get()))
            except Exception:
                pass
            fn()
        return run

    def _menu_spec(self):
        """按当前状态生成菜单（勾选态、当前值提示都取自变量/配置）。"""
        mi, sp = petmenu.item, petmenu.sep
        target = (self.cfg.get("dock") or {}).get("target") or "workbuddy"
        target_label = _dock_target_meta(target)["label"]
        # 菜单右侧的"当前值"用短标签，完整名称留给子菜单，避免把菜单撑宽
        basis_label = {"eff": "有效", "raw": "端到端",
                       "base": "基线"}.get(self.basis_var.get(), "有效")
        scale = float(self.var_scale.get() or 1.0)

        basis_items = [
            mi("有效（扣工具）", lambda: self._pick_basis("eff"),
               checked=self.basis_var.get() == "eff"),
            mi("端到端", lambda: self._pick_basis("raw"),
               checked=self.basis_var.get() == "raw"),
            mi("生成基线", lambda: self._pick_basis("base"),
               checked=self.basis_var.get() == "base"),
        ]
        source_items = [
            mi(_SOURCE_LABELS.get(sid, sid), lambda v=sid: self.set_source(v),
               checked=self.source == sid)
            for sid in _SOURCE_IDS
        ]
        size_items = [
            mi(f"{int(round(sc * 100))}%", lambda v=sc: self.set_scale(v),
               checked=abs(scale - sc) < 1e-6)
            for sc in (0.5, 0.75, 1.0, 1.25, 1.5)
        ]
        dock_items = [
            mi(_dock_target_meta(tid)["label"], lambda v=tid: self._set_dock_target(v),
               checked=self.var_dock_target.get() == tid)
            for tid in _DOCK_TARGETS
        ]
        sys_items = [
            mi("开机自启", self._flip(self.var_auto, self._toggle_autostart),
               checked=bool(self.var_auto.get())),
            mi("全屏时自动隐藏", self._flip(self.var_fs, self._toggle_fs),
               checked=bool(self.var_fs.get())),
            sp(),
            mi("导出今日 CSV", self.export_csv),
        ]

        return [
            mi("打开仪表盘", self.open_dashboard),
            mi("查看余额", self.show_balance),
            sp(),
            mi("速率口径", submenu=basis_items, hint=basis_label),
            mi("查询来源", submenu=source_items,
               hint=_SOURCE_LABELS.get(self.source, self.source)),
            sp(),
            mi("宠物大小", submenu=size_items, hint=f"{int(round(scale * 100))}%"),
            mi("贴靠窗口并跟随", self._flip(self.var_dock, self._toggle_dock),
               checked=bool(self.var_dock.get()),
               hint=target_label),
            mi("贴靠目标", submenu=dock_items, hint=target_label),
            sp(),
            mi("随机漫步", self._flip(self.var_wander, self._toggle_wander),
               checked=bool(self.var_wander.get())),
            mi("注视鼠标", self._flip(self.var_look, self._toggle_look),
               checked=bool(self.var_look.get())),
            mi("提示音", self._flip(self.var_sound, self._toggle_sound),
               checked=bool(self.var_sound.get())),
            sp(),
            mi("系统", submenu=sys_items),
            sp(),
            mi("重置位置", self._reset_pos),
            mi("退出", self.quit),
        ]

    def _pick_basis(self, value: str):
        self.basis_var.set(value)
        self._on_basis()

    def _popup_menu(self, x, y):
        try:
            self.menu.show(self._menu_spec(), x, y)
        except Exception:
            import traceback
            traceback.print_exc()

    def _on_basis(self):
        self.basis = self.basis_var.get()
        self.cfg["basis"] = self.basis
        prefs.save(self.cfg)
        self._show_panel()

    def set_source(self, source: str, save: bool = True):
        """切换桌宠查哪个应用的用量（全部 / WorkBuddy / Qoder / Codex / Claude）。"""
        if source not in _SOURCE_IDS:
            source = "all"
        self.source = source
        self.var_source.set(source)
        self.sampler.set_source(source)
        self.cfg["source"] = source
        if save:
            prefs.save(self.cfg)
        self._show_panel()

    def _on_tray(self, kind):
        if kind in ("left", "dbl"):
            self._toggle_panel()
        else:
            try:
                x, y = self.root.winfo_pointerxy()
                self._popup_menu(x, y)
            except Exception:
                pass

    def _on_hotkey(self):
        self._toggle_panel()

    # ---------------------------------------------------------------- 设置项
    def _toggle_wander(self):
        on = bool(self.var_wander.get())
        self.cfg["pet"]["wander"] = on
        if not on:
            self._end_walk(save=False)
        prefs.save(self.cfg)
        self._schedule_wander()

    def _toggle_look(self):
        on = bool(self.var_look.get())
        self.cfg["pet"]["look_tracking"] = on
        if not on:
            self._stop_look()
        prefs.save(self.cfg)

    def _toggle_sound(self):
        self.cfg["alerts"]["sound"] = bool(self.var_sound.get())
        prefs.save(self.cfg)

    def _toggle_fs(self):
        self.cfg["system"]["hide_on_fullscreen"] = bool(self.var_fs.get())
        prefs.save(self.cfg)

    def _toggle_autostart(self):
        ok = win32ui.set_autostart(BASE_DIR, bool(self.var_auto.get()))
        if not ok:
            self.var_auto.set(not self.var_auto.get())

    def set_scale(self, scale: float, save: bool = True):
        self.cfg["pet"]["scale"] = float(scale)
        self.var_scale.set(float(scale))
        if save:
            prefs.save(self.cfg)
        self.sprite = petsprite.Sprite(scale=scale, master=self.root)
        self.player.sprite = self.sprite
        self.win.resize(self.sprite.width, self.sprite.height)
        self.player.play(petsprite.ANIM_IDLE)
        self._show_panel()

    # ---------------------------------------------------------------- 漫步
    def _schedule_wander(self, delay: int | None = None):
        if self._wander_job:
            try:
                self.root.after_cancel(self._wander_job)
            except Exception:
                pass
            self._wander_job = None
        if not self.cfg["pet"].get("wander"):
            return
        if (self.cfg.get("dock") or {}).get("enabled"):
            return
        # 间隔随机：10~30 秒，避免看起来像机械循环
        if delay is None:
            delay = random.randint(10_000, 30_000)
        self._wander_job = self.root.after(delay, self._wander_step)

    def _end_walk(self, save: bool = True):
        was_walking = bool(self._walk_dir)
        self._walk_dir = 0
        if was_walking:
            self.player.play(petsprite.ANIM_IDLE)
            if save:
                self._save_pos()
            self._schedule_wander()

    def _wander_step(self):
        self._wander_job = None
        pet = self.cfg["pet"]
        if not pet.get("wander"):
            return
        if (self.cfg.get("dock") or {}).get("enabled"):
            self._schedule_wander(random.randint(8_000, 15_000))
            return
        # 面板正开着（钉住/临时展示）或桌宠不可见时不走开
        if self.panel.visible() or self._hidden_by_fs or not self._shown:
            self._schedule_wander(5_000 + random.randint(0, 4_000))
            return

        s = self.sampler.get()
        if s.get("status") == "working":
            self._wander_defer_start = 0.0
            self._schedule_wander(random.randint(5_000, 9_000))
            return
        # "活跃但空闲"最多让路 30 秒；否则开着会话就永远不漫步了
        if s.get("active"):
            now = time.time()
            if not self._wander_defer_start:
                self._wander_defer_start = now
            if now - self._wander_defer_start < 30.0:
                self._schedule_wander(random.randint(4_000, 8_000))
                return
        self._wander_defer_start = 0.0

        w, h = self.sprite.width, self.sprite.height
        x, y = self.win.position()
        # 在当前显示器的工作区内走，任务栏/副屏边缘都不会踩空
        area = _work_area_at(int(x + w / 2), int(y + h / 2))
        if not area:
            self._schedule_wander()
            return
        margin = 24
        lo = area[0] + margin
        hi = area[2] - w - margin
        if hi <= lo:
            self._schedule_wander()
            return

        mouse = _cursor_pos()
        target = None
        for _ in range(12):
            dist = random.randint(90, 380)
            direction = random.choice((-1, 1))
            t = x + direction * dist
            if t < lo or t > hi:
                t = x - direction * dist
            t = max(lo, min(hi, t))
            if abs(t - x) < 60:
                continue
            if mouse:
                # 别把目标点选在鼠标脚下（水平走，只看横向是否挡路）
                if (abs((t + w / 2) - mouse[0]) < 110
                        and abs((y + h / 2) - mouse[1]) < h):
                    continue
            target = t
            break
        if target is None:
            self._schedule_wander(random.randint(6_000, 12_000))
            return

        move = target - x
        direction = 1 if move > 0 else -1
        speed = random.uniform(55.0, 125.0)         # px/s，长距离走快点
        dur = max(1.2, min(6.0, abs(move) / speed))
        anim = petsprite.ANIM_WALK_R if direction > 0 else petsprite.ANIM_WALK_L
        self._walk_dir = direction
        self._stop_look()
        self.panel.hide()
        self.player.play(anim)

        x0 = float(x)
        target_x = float(target)
        t0 = time.time()
        stalled = 0.0

        def step_move():
            if not self._walk_dir:
                return
            now = time.time()
            p = min(1.0, (now - t0) / dur)
            eased = 0.5 - 0.5 * math.cos(math.pi * p)   # 起步/收尾都减速
            nx = int(round(x0 + (target_x - x0) * eased))
            cx, cy = self.win.position()
            cur = _cursor_pos()
            if cur:
                d = ((cur[0] - (nx + w / 2)) ** 2
                     + (cur[1] - (cy + h / 2)) ** 2) ** 0.5
                if d < 80:
                    # 鼠标挡在面前：原地等一会儿，不从指针底下穿过去
                    stalled += 0.12
                    if stalled > 2.0:
                        self._end_walk()
                        return
                    self.root.after(120, step_move)
                    return
            self.win.move_to(nx, cy)
            if p >= 1.0:
                self._end_walk()
                return
            self.root.after(30, step_move)

        step_move()

    # ---------------------------------------------------------------- 全屏隐藏
    def _fs_watch(self):
        if self.cfg["system"].get("hide_on_fullscreen", True):
            fs = win32ui.is_fullscreen_foreground()
            if fs and not self._hidden_by_fs:
                self._hidden_by_fs = True
                self._end_walk(save=False)
                self._set_shown(False)
            elif not fs and self._hidden_by_fs:
                self._hidden_by_fs = False
                self._set_shown(True)
        self.root.after(self.FS_MS, self._fs_watch)

    # ---------------------------------------------------------------- 告警
    def _alert(self, key: str, text: str, title: str = "提示"):
        alerts = self.cfg.get("alerts") or {}
        cd = float(alerts.get("cooldown_sec") or 300)
        now = time.time()
        if now - self._alert_ts.get(key, 0) < cd:
            return
        self._alert_ts[key] = now
        if alerts.get("bubble", True):
            x, y = self.win.position()
            self.bubble.show([(None, text, FG_WARN)], x, y,
                             anchor="above", timeout=6000, title=title)
        if alerts.get("sound", True):
            try:
                import winsound
                winsound.MessageBeep(0x00000030)  # MB_ICONWARNING
            except Exception:
                pass

    # ---------------------------------------------------------------- 余额
    def _refresh_balance_async(self):
        """并发刷新所有"已启用且填了 Key"的服务商余额。"""
        items = providers.enabled_items(self.cfg)
        if items:
            def work():
                try:
                    self._balances = providers.query_all(items, timeout=8)
                    self._balance_ts = int(time.time() * 1000)
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()
        else:
            self._balances = []
        interval = float((self.cfg.get("providers") or {}).get("refresh_sec") or 1800)
        self.root.after(int(max(60, interval) * 1000), self._refresh_balance_async)

    def _flash_panel(self, sec: int = 8):
        """临时钉住面板若干秒（用于"查看余额"后直接把结果显示在同一块里）。"""
        self.pinned = True
        self.panel.pinned = True
        self._show_panel()
        if self._pin_job:
            try:
                self.root.after_cancel(self._pin_job)
            except Exception:
                pass
        self._pin_job = self.root.after(sec * 1000, self._unflash)

    def _unflash(self):
        if self._hover:
            return
        self.pinned = False
        self.panel.pinned = False
        self.panel.hide()

    def refresh_balances_now(self):
        """面板里的刷新按钮：后台查询，完成后在主线程重画；不改钉住状态。"""
        if self._bal_refreshing:
            return
        items = providers.enabled_items(self.cfg)
        if not items:
            self._alert("balance_none",
                        "尚未启用余额服务商（设置页可配置或从 cc-switch 导入）")
            return

        self._bal_refreshing = True
        self._bal_refresh_result = None
        self._bal_refresh_deadline = time.time() + 20
        self.panel.set_balance_refreshing(True)

        def work():
            res = []
            try:
                res = providers.query_all(items, timeout=9)
            except Exception:
                res = []
            self._bal_refresh_result = res

        threading.Thread(target=work, daemon=True).start()
        self.root.after(120, self._poll_balance_refresh)

    def _poll_balance_refresh(self):
        # 工作线程只赋值列表，Tk 调用全部留在主线程
        if (self._bal_refresh_result is None
                and time.time() < self._bal_refresh_deadline):
            self.root.after(120, self._poll_balance_refresh)
            return
        res = self._bal_refresh_result or []
        self._bal_refresh_result = None
        self._bal_refreshing = False
        self.panel.set_balance_refreshing(False)
        if res:
            self._balances = res
            self._balance_ts = int(time.time() * 1000)
            bad = [b for b in res if not b.get("ok")]
            if bad and len(bad) == len(res):
                self._pending_bubbles.append(
                    ([(None, "余额查询失败", FG_WARN),
                      (None, f"{bad[0].get('label', '')}：{bad[0].get('error', '')}",
                       FG_SUB)], "余额", 6000)
                )
        else:
            self._pending_bubbles.append(
                ([(None, "余额查询失败", FG_WARN)], "余额", 6000))
        if self.panel.visible():
            self._show_panel()

    def show_balance(self):
        """菜单「查看余额」：临时钉住面板，再走同一套刷新流程。"""
        items = providers.enabled_items(self.cfg)
        if not items:
            self._alert("balance_none",
                        "尚未启用余额服务商（设置页可配置或从 cc-switch 导入）")
            self._flash_panel(6)
            return

        self._flash_panel(10)
        self.refresh_balances_now()

    # ---------------------------------------------------------------- 其它动作
    def export_csv(self):
        try:
            path = collector.export_csv("today", source=self.source)
            x, y = self.win.position()
            self.bubble.show([(None, "已导出 CSV", FG),
                              (None, Path(path).name, FG_SUB)], x, y,
                             title="导出", timeout=5000)
        except Exception as e:
            x, y = self.win.position()
            self.bubble.show([(None, f"导出失败：{e}", FG_WARN)], x, y, timeout=5000)

    def open_dashboard(self):
        def go():
            if not _server_alive():
                import subprocess
                try:
                    subprocess.Popen([sys.executable, str(BASE_DIR / "server.py"),
                                      "--no-browser"],
                                     cwd=str(BASE_DIR),
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception:
                    pass
                # 等服务把实际端口写进 .cache/server-port（冲突时会顺延）
                for _ in range(40):
                    if _server_alive():
                        break
                    time.sleep(0.1)
            webbrowser.open(prefs.server_url())

        threading.Thread(target=go, daemon=True).start()

    def dock(self):
        target = (self.cfg.get("dock") or {}).get("target") or "workbuddy"
        rect = _dock_rect(target)
        w, h = self.sprite.width, self.sprite.height
        if rect:
            x = rect[2] - w - 40
            y = rect[3] - h - 60
        else:
            x = self.root.winfo_screenwidth() - w - 60
            y = self.root.winfo_screenheight() - h - 120
        self.win.move_to(int(x), int(y))
        self._save_pos()
        self._show_panel()

    @staticmethod
    def _mtime() -> float:
        try:
            return os.path.getmtime(prefs.CONFIG_FILE)
        except OSError:
            return 0.0

    def _watch_config(self):
        mt = self._mtime()
        if mt and mt != self._cfg_mtime:
            self._cfg_mtime = mt
            try:
                self._apply_cfg(prefs.load())
            except Exception:
                pass
        self.root.after(self.CFG_MS, self._watch_config)

    def _apply_cfg(self, new: dict):
        """网页设置保存后热生效：尺寸、漫步、注视、告警、热键、口径。"""
        old = self.cfg
        self.cfg = new
        op, np = old.get("pet") or {}, new.get("pet") or {}
        if abs(float(np.get("scale", 1)) - float(op.get("scale", 1))) > 1e-6:
            self.set_scale(float(np["scale"]), save=False)
        if bool(np.get("wander")) != bool(op.get("wander")):
            self.var_wander.set(bool(np.get("wander")))
            if np.get("wander"):
                self._schedule_wander()
            else:
                self._end_walk(save=False)
        if not np.get("look_tracking", True):
            self._stop_look()
        # 漫步与注视不再互斥：走路时注视自动让位，停下后恢复
        self.var_look.set(bool(np.get("look_tracking", True)))
        self.var_sound.set(bool((new.get("alerts") or {}).get("sound", True)))
        self.var_fs.set(bool((new.get("system") or {}).get("hide_on_fullscreen", True)))
        self.var_dock.set(bool((new.get("dock") or {}).get("enabled")))
        nt = (new.get("dock") or {}).get("target") or "workbuddy"
        if nt != ((old.get("dock") or {}).get("target") or "workbuddy"):
            self.var_dock_target.set(nt)
            self._set_dock_target(nt, save=False)
        if not (new.get("dock") or {}).get("enabled"):
            self._set_shown(True)
        elif self.cfg["pet"].get("wander"):
            self.cfg["pet"]["wander"] = False
            self.var_wander.set(False)
            self._end_walk(save=False)
            prefs.save(self.cfg)
        self.basis = new.get("basis", "eff")
        self.basis_var.set(self.basis)
        ns = new.get("source") or "all"
        if ns in _SOURCE_IDS and ns != self.source:
            self.set_source(ns, save=False)   # 网页端改的，配置已经存过，别再写一遍
        hk_new = (new.get("system") or {}).get("hotkey", "")
        hk_old = (old.get("system") or {}).get("hotkey", "")
        if hk_new and hk_new != hk_old and self.hotkey:
            if not self.hotkey.register(hk_new):
                self.hotkey.register(hk_old)
        self._refresh_balance_async()

    def _reset_pos(self):
        w, h = self.sprite.width, self.sprite.height
        x = self.root.winfo_screenwidth() - w - 60
        y = self.root.winfo_screenheight() - h - 120
        self.win.move_to(x, y)
        self._save_pos()

    def quit(self):
        if self._closing:
            return
        self._closing = True
        self._save_pos()
        self.sampler.stop()
        try:
            if self.hotkey:
                self.hotkey.unregister()
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.remove()
        except Exception:
            pass
        try:
            self.msgwin.destroy()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
        self.root.after(50, self.root.destroy)

    def run(self):
        self.root.mainloop()


def _server_alive() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", prefs.read_port()), timeout=0.4):
            return True
    except OSError:
        return False


_U = ctypes.windll.user32
_U.GetForegroundWindow.restype = wt.HWND
_U.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
_U.GetAncestor.restype = wt.HWND
_U.IsIconic.argtypes = [wt.HWND]
_U.IsIconic.restype = wt.BOOL
_U.MonitorFromRect.argtypes = [ctypes.POINTER(wt.RECT), wt.DWORD]
_U.MonitorFromRect.restype = wt.HMONITOR
_U.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.c_void_p]
_U.GetMonitorInfoW.restype = wt.BOOL
_U.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
_U.GetWindowLongW.restype = ctypes.c_long
_U.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_U.GetClassNameW.restype = ctypes.c_int
_U.IsWindowVisible.argtypes = [wt.HWND]
_U.IsWindowVisible.restype = wt.BOOL
_U.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_U.GetWindowRect.restype = wt.BOOL
_U.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_U.GetWindowThreadProcessId.restype = wt.DWORD
_U.GetSystemMetrics.argtypes = [ctypes.c_int]
_U.GetSystemMetrics.restype = ctypes.c_int
_U.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_U.GetCursorPos.restype = wt.BOOL
_U.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
_U.MonitorFromPoint.restype = wt.HMONITOR

# OpenProcess 必须声明 restype：默认按 c_int 返回，64 位下句柄会被截断，
# 随后 CloseHandle 拿到的是错值 —— 每轮窗口枚举都漏一个进程句柄。
_K = ctypes.windll.kernel32
_K.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_K.OpenProcess.restype = wt.HANDLE
_K.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                          ctypes.POINTER(wt.DWORD)]
_K.QueryFullProcessImageNameW.restype = wt.BOOL
_K.CloseHandle.argtypes = [wt.HANDLE]
_K.CloseHandle.restype = wt.BOOL

# 虚拟屏范围（多显示器 / 负坐标）
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def _cursor_pos():
    """当前鼠标位置（虚拟屏坐标）；GetCursorPos 比 Tk 的 pointerxy 更少延迟。"""
    try:
        p = wt.POINT()
        if _U.GetCursorPos(ctypes.byref(p)):
            return int(p.x), int(p.y)
    except Exception:
        pass
    return None


def _work_area_at(x: int, y: int):
    """坐标所在显示器的可用区域（已扣除任务栏）；失败时退回整个虚拟屏。"""
    try:
        hmon = _U.MonitorFromPoint(wt.POINT(int(x), int(y)), 2)
        if hmon:
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if _U.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcWork
                return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    try:
        vx = _U.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = _U.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = _U.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = _U.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if vw > 0 and vh > 0:
            return vx, vy, vx + vw, vy + vh
    except Exception:
        pass
    return None


def _clamp_to_virtual_screen(x: int, y: int, w: int, h: int):
    """
    把坐标夹回虚拟屏范围。

    overlay-state.json 存的是绝对坐标，拔掉副屏之后桌宠会永久留在不可见区域，
    右键菜单也点不到，只能手删状态文件。启动时兜一次底。
    """
    try:
        vx = _U.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = _U.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = _U.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = _U.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    except Exception:
        return x, y
    if vw <= 0 or vh <= 0:
        return x, y
    keep = 32                       # 至少留这么多像素在屏内，保证还能点到
    x = max(vx - w + keep, min(x, vx + vw - keep))
    y = max(vy, min(y, vy + vh - keep))
    return x, y


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


_DOCK_CACHE = {"target": None, "hwnd": None}

# 贴靠目标注册表：keys 与进程镜像文件名（小写 basename）做包含匹配。
# 未注册的目标会退化为「按目标名本身匹配」，方便以后直接配新应用。
_DOCK_TARGETS = {
    "workbuddy": {"label": "WorkBuddy", "keys": ("workbuddy",)},
    # 本机实测主程序为 D:\Programs\Qoder CN\Qoder CN.exe（国际版为 Qoder.exe）
    "qoder":     {"label": "Qoder",     "keys": ("qoder",)},
}


def _dock_target_meta(target: str) -> dict:
    meta = _DOCK_TARGETS.get(target)
    if meta:
        return meta
    return {"label": target or "workbuddy", "keys": ((target or "workbuddy"),)}


def _dock_hwnd_rect(target: str = "workbuddy"):
    """
    返回 (hwnd, (l, t, r, b)) 或 None。

    多个同应用窗口（如主窗 + 独立预览窗）时，若每次按"面积最大"选取会来回切换，
    导致桌宠在两个窗口间跳动。这里改为**句柄粘滞**：
    只要上次选中的窗口仍有效（未销毁、可见、尺寸像主窗），就继续用它。
    """
    try:
        meta = _dock_target_meta(target)
        keys = meta["keys"]
        if _DOCK_CACHE.get("target") != target:
            _DOCK_CACHE["target"] = target
            _DOCK_CACHE["hwnd"] = None
        best = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(h, l):
            if not _U.IsWindowVisible(h):
                return True
            r = wt.RECT()
            _U.GetWindowRect(h, ctypes.byref(r))
            w, hh = r.right - r.left, r.bottom - r.top
            if w < 700 or hh < 450:
                return True
            pid = wt.DWORD()
            _U.GetWindowThreadProcessId(h, ctypes.byref(pid))
            ph = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not ph:
                return True
            try:
                buf = ctypes.create_unicode_buffer(1024)
                sz = wt.DWORD(1024)
                ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    ph, 0, buf, ctypes.byref(sz))
                if ok:
                    base = buf.value.lower().rsplit("\\", 1)[-1]
                    if any(k in base for k in keys):
                        best.append((w * hh, h, (r.left, r.top, r.right, r.bottom)))
            finally:
                ctypes.windll.kernel32.CloseHandle(ph)
            return True

        _U.EnumWindows(cb, 0)
        if not best:
            _DOCK_CACHE["hwnd"] = None
            return None

        cached = _DOCK_CACHE.get("hwnd")
        if cached:
            for _area, h, rect in best:
                if h == cached:
                    return h, rect

        # 没有缓存（或缓存已失效）：优先当前前台的目标窗口，其次面积最大者
        fg = _U.GetForegroundWindow()
        pick = None
        for area, h, rect in best:
            if h == fg or _U.GetAncestor(fg, 2) == h:
                pick = (h, rect)
                break
        if pick is None:
            best.sort(reverse=True)
            pick = (best[0][1], best[0][2])
        _DOCK_CACHE["hwnd"] = pick[0]
        return pick
    except Exception:
        return None


def _workbuddy_hwnd_rect():
    """兼容旧调用：贴靠目标为 WorkBuddy。"""
    return _dock_hwnd_rect("workbuddy")


def _is_minimized(hwnd) -> bool:
    try:
        return bool(_U.IsIconic(hwnd))
    except Exception:
        return False


_IGNORE_CLASSES = {
    "#32768",                  # 系统菜单
    "tooltips_class32",        # 工具提示
    "SysShadow",
    "ForegroundStaging",
    "Windows.UI.Core.CoreWindow",          # 系统通知
    "XamlExplorerHostIslandWindow",
    "TopLevelWindowForOverflowXamlIsland", # 托盘溢出
    "TaskListThumbnailWnd",                # 任务栏预览
    "MultitaskingViewFrame",
}
_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_POPUP = 0x80000000
_WS_EX_NOACTIVATE = 0x08000000


def _fg_class_and_pid(h):
    try:
        buf = ctypes.create_unicode_buffer(256)
        _U.GetClassNameW(h, buf, 256)
        pid = wt.DWORD()
        _U.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return buf.value, int(pid.value)
    except Exception:
        return "", 0


def _is_ignorable_fg(h) -> bool:
    """
    前台窗口是否属于"可以忽视的小弹窗"：
      · 本程序自己的窗口（右键菜单 / 气泡 / 面板）
      · 系统菜单、tooltip、通知、托盘溢出等已知类
      · WS_EX_NOACTIVATE（从不真正取得焦点的浮窗）
      · 小尺寸 WS_POPUP（各种 toast / 悬浮提示）
    """
    try:
        cls, pid = _fg_class_and_pid(h)
        if pid == os.getpid():
            return True
        if cls in _IGNORE_CLASSES:
            return True
        ex = _U.GetWindowLongW(h, _GWL_EXSTYLE)
        if ex & _WS_EX_NOACTIVATE:
            return True
        style = _U.GetWindowLongW(h, _GWL_STYLE)
        if style & _WS_POPUP:
            r = wt.RECT()
            _U.GetWindowRect(h, ctypes.byref(r))
            if (r.right - r.left) < 560 and (r.bottom - r.top) < 460:
                return True
        return False
    except Exception:
        return False


def _is_foreground(hwnd) -> bool:
    """
    贴靠目标是否仍是"最上层"。

    比单纯比较前台窗口宽松：被菜单、tooltip、系统通知、本程序浮窗
    短暂抢走前台时，仍视为目标应用在前台，避免桌宠频繁闪隐。
    """
    try:
        fg = _U.GetForegroundWindow()
        if not fg:
            return False
        if fg == hwnd or _U.GetAncestor(fg, 2) == hwnd:   # GA_ROOT
            return True
        return _is_ignorable_fg(fg)
    except Exception:
        return True


def _rect_is_fullscreen(rect) -> bool:
    """给定窗口矩形是否铺满其所在显示器（判定全屏）。"""
    try:
        r = wt.RECT(*rect)
        hmon = _U.MonitorFromRect(ctypes.byref(r), 2)   # NEAREST
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _U.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False
        m = mi.rcMonitor
        return (r.left <= m.left + 1 and r.top <= m.top + 1 and
                r.right >= m.right - 1 and r.bottom >= m.bottom - 1)
    except Exception:
        return False


def _workbuddy_rect():
    info = _dock_hwnd_rect("workbuddy")
    return info[1] if info else None


def _dock_rect(target: str = "workbuddy"):
    info = _dock_hwnd_rect(target)
    return info[1] if info else None


# ---------------------------------------------------------------- 入口
_SINGLETON = None
_SINGLETON_PORT = 8799


def _single_instance() -> bool:
    global _SINGLETON
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _SINGLETON_PORT))
        s.listen(1)
        _SINGLETON = s
        return True
    except OSError:
        return False


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    if not _single_instance():
        sys.stderr.write("[wb-token-monitor] 桌宠已在运行，本次启动退出。\n")
        return

    cfg = prefs.load()
    if not (BASE_DIR / "petconfig.json").exists():
        prefs.save(cfg)
    PetApp(cfg).run()


if __name__ == "__main__":
    main()
