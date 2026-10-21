#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
精灵表引擎
==========

数据来自 assets/shenshen.manifest.json：
    1536×2288 RGBA，8 列 × 11 行，单格 192×208
    第 0–8 行：9 个动作（含每帧时长）
    第 9–10 行：16 个注视方向（各一帧）

对外提供：
    Sprite.frames(name)        -> [(PIL.Image, duration_ms)]
    Sprite.dir_frame(index)    -> PIL.Image          # 0..15，每 22.5°
    Sprite.bgra(name, idx)     -> (bytes, w, h)      # 预乘 alpha，供 UpdateLayeredWindow
    Sprite.tk(name, idx)       -> PhotoImage         # tkinter 回退模式用
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageChops, ImageTk

BASE_DIR = Path(__file__).resolve().parent
ASSETS = BASE_DIR / "assets"
ATLAS = ASSETS / "shenshen-atlas.png"
MANIFEST = ASSETS / "shenshen.manifest.json"

# 动作名 → 语义（本工具自用）
ANIM_IDLE = "idle"
ANIM_WORK = "running"          # 模型在输出
ANIM_WAIT = "waiting"          # 等待 / 空闲间隙
ANIM_OK = "review"             # 一轮完成
ANIM_FAIL = "failed"           # 异常 / 上下文告警
ANIM_WAVE = "waving"
ANIM_JUMP = "jumping"
ANIM_WALK_R = "running-right"
ANIM_WALK_L = "running-left"


class Sprite:
    def __init__(self, scale: float = 0.75, master=None):
        self.scale = float(scale or 1.0)
        self.master = master
        self.ok = False
        self._frames: Dict[str, List[Tuple[Image.Image, int]]] = {}
        self._dirs: List[Image.Image] = []
        self._bgra: Dict[Tuple[str, int], Tuple[bytes, int, int]] = {}
        self._tk: Dict[Tuple[str, int], object] = {}
        self._tk_dir: Dict[int, object] = {}
        self.width = 0
        self.height = 0
        try:
            self._load()
            self.ok = True
        except Exception:
            self.ok = False

    # ---------------------------------------------------------------- 加载
    def _load(self) -> None:
        meta = json.loads(MANIFEST.read_text(encoding="utf-8"))
        atlas = Image.open(ATLAS).convert("RGBA")
        a = meta["atlas"]
        cw, ch = int(a["cellWidth"]), int(a["cellHeight"])
        cols = int(a["columns"])

        tw = max(1, int(round(cw * self.scale)))
        th = max(1, int(round(ch * self.scale)))
        self.width, self.height = tw, th

        def cell(col: int, row: int) -> Image.Image:
            box = (col * cw, row * ch, (col + 1) * cw, (row + 1) * ch)
            return atlas.crop(box).resize((tw, th), Image.LANCZOS)

        for anim in meta.get("animations") or []:
            name = anim["id"]
            row = int(anim["row"])
            n = int(anim["frameCount"])
            durs = anim.get("frameDurationsMs") or [120] * n
            frames = []
            for i in range(n):
                frames.append((cell(i, row), int(durs[i] if i < len(durs) else 140)))
            self._frames[name] = frames

        for d in meta.get("lookDirections") or []:
            self._dirs.append(cell(int(d["column"]), int(d["row"])))
        # 按角度排序，保证索引 0 对应 0°
        dirs = meta.get("lookDirections") or []
        order = sorted(range(len(dirs)), key=lambda i: float(dirs[i]["degrees"]))
        self._dirs = [self._dirs[i] for i in order]

        # 关键动作必须存在
        for req in (ANIM_IDLE, ANIM_WORK):
            if req not in self._frames:
                raise RuntimeError(f"manifest 缺少动作 {req}")

    # ---------------------------------------------------------------- 取帧
    def has(self, name: str) -> bool:
        return name in self._frames

    def frames(self, name: str) -> List[Tuple[Image.Image, int]]:
        return self._frames.get(name) or self._frames.get(ANIM_IDLE) or []

    def duration(self, name: str) -> int:
        return sum(d for _, d in self.frames(name))

    def dir_count(self) -> int:
        return len(self._dirs)

    def dir_frame(self, index: int) -> Optional[Image.Image]:
        if not self._dirs:
            return None
        return self._dirs[index % len(self._dirs)]

    def dir_by_angle(self, degrees: float) -> Optional[Image.Image]:
        """按屏幕坐标角度取注视帧（0° = 正右方，顺时针）。"""
        if not self._dirs:
            return None
        step = 360.0 / len(self._dirs)
        idx = int(round((degrees % 360.0) / step)) % len(self._dirs)
        return self._dirs[idx]

    @staticmethod
    def angle_to(dx: float, dy: float) -> float:
        """屏幕角度：0°=正右，90°=正下（atan2 惯例）。"""
        return math.degrees(math.atan2(dy, dx)) % 360.0

    @staticmethod
    def atlas_deg(screen_deg: float, offset: float = 270.0) -> float:
        """
        屏幕角度 → 图集角度（完整俯仰，未做水平压缩）。

        实测该精灵表是"转盘"式，顺时针排列：0° 正面朝镜头、**90° 朝屏幕右**、
        180° 背面、**270° 朝屏幕左**。因此：
            屏幕右(0°)→90、屏幕下(90°)→180、屏幕左(180°)→270、屏幕上(270°)→0

        注：早期版本按"90° 朝左"反向换算，导致左右镜像。
        """
        return (float(screen_deg) + float(offset) - 180.0) % 360.0

    def dir_frame_for(self, screen_deg: float, offset: float = 270.0):
        return self.dir_by_angle(self.atlas_deg(screen_deg, offset))

    def bgra_for(self, screen_deg: float, offset: float = 270.0):
        return self.bgra_dir(self.atlas_deg(screen_deg, offset))

    # ---------------------------------------------------------------- 输出格式
    def bgra(self, name: str, idx: int) -> Tuple[bytes, int, int]:
        """预乘 alpha 的 BGRA 字节串（供分层窗口直接贴屏）。"""
        key = (name, idx)
        hit = self._bgra.get(key)
        if hit:
            return hit
        frames = self.frames(name)
        if not frames:
            return b"", self.width, self.height
        img = frames[idx % len(frames)][0]
        r, g, b, a = img.split()
        # 预乘：C' = C * A / 255，用 ImageChops 在 C 里完成
        pr = ImageChops.multiply(r, a)
        pg = ImageChops.multiply(g, a)
        pb = ImageChops.multiply(b, a)
        data = Image.merge("RGBA", (pr, pg, pb, a)).tobytes("raw", "BGRA")
        out = (data, img.width, img.height)
        self._bgra[key] = out
        return out

    def bgra_dir(self, degrees: float) -> Tuple[bytes, int, int]:
        img = self.dir_by_angle(degrees)
        if img is None:
            return self.bgra(ANIM_IDLE, 0)
        key = ("@dir", int(round(degrees / (360.0 / max(1, len(self._dirs))))))
        hit = self._bgra.get(key)
        if hit:
            return hit
        r, g, b, a = img.split()
        pr = ImageChops.multiply(r, a)
        pg = ImageChops.multiply(g, a)
        pb = ImageChops.multiply(b, a)
        data = Image.merge("RGBA", (pr, pg, pb, a)).tobytes("raw", "BGRA")
        out = (data, img.width, img.height)
        self._bgra[key] = out
        return out

    def alpha_at(self, name: str, idx: int, x: int, y: int) -> int:
        frames = self.frames(name)
        if not frames:
            return 255
        img = frames[idx % len(frames)][0]
        if 0 <= x < img.width and 0 <= y < img.height:
            return img.getpixel((x, y))[3]
        return 0

    def tk(self, name: str, idx: int):
        key = (name, idx)
        hit = self._tk.get(key)
        if hit is not None:
            return hit
        frames = self.frames(name)
        if not frames:
            return None
        img = ImageTk.PhotoImage(frames[idx % len(frames)][0], master=self.master)
        self._tk[key] = img
        return img

    def tk_dir(self, degrees: float):
        img = self.dir_by_angle(degrees)
        if img is None:
            return self.tk(ANIM_IDLE, 0)
        step = 360.0 / max(1, len(self._dirs))
        key = int(round(degrees / step)) % max(1, len(self._dirs))
        hit = self._tk_dir.get(key)
        if hit is not None:
            return hit
        ph = ImageTk.PhotoImage(self._dirs[key], master=self.master)
        self._tk_dir[key] = ph
        return ph


class Player:
    """
    帧调度器：按 manifest 的每帧时长推进，一次性动画结束后回调。

    on_frame(pil_image, name, idx) —— 由宿主决定怎么显示（分层窗口或 Label）
    """

    def __init__(self, master, sprite: Sprite, on_frame):
        self.master = master
        self.sprite = sprite
        self.on_frame = on_frame
        self.name = ANIM_IDLE
        self.idx = 0
        self.once = False
        self._done = None
        self._job = None
        self._paused = False
        self.time_scale = 1.0

    # ---------------- 控制
    def play(self, name: str, once: bool = False, on_done=None) -> None:
        if not self.sprite.ok:
            return
        if not self.sprite.has(name):
            name = ANIM_IDLE
        if self.name == name and not once and self._job:
            return
        self.name = name
        self.idx = 0
        self.once = once
        self._done = on_done
        self._paused = False
        self._cancel()
        self._step()

    def pause(self) -> None:
        self._paused = True
        self._cancel()

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            self._step()

    def current(self) -> str:
        return self.name

    # ---------------- 内部
    def _cancel(self) -> None:
        if self._job:
            try:
                self.master.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _step(self) -> None:
        if not self.sprite.ok or self._paused:
            return
        frames = self.sprite.frames(self.name)
        if not frames:
            return
        img, dur = frames[self.idx % len(frames)]
        try:
            self.on_frame(img, self.name, self.idx)
        except Exception:
            pass

        nxt = self.idx + 1
        if nxt >= len(frames):
            if self.once:
                cb, self._done = self._done, None
                if cb:
                    try:
                        cb()
                    except Exception:
                        pass
                return
            nxt = 0
        self.idx = nxt
        delay = max(40, int(dur * self.time_scale))
        self._job = self.master.after(delay, self._step)
