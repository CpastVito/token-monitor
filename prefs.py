#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置读写（JSON，热生效）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "petconfig.json"
# server.py 把实际监听的端口写在这里；端口冲突顺延后靠它发现真实端口
PORT_FILE = BASE_DIR / ".cache" / "server-port"
DEFAULT_PORT = 8971

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    # ---------------- 形象
    "pet": {
        "scale": 0.75,          # 0.5 / 0.75 / 1.0 / 1.25 / 1.5
        "alpha": 1.0,
        "wander": False,        # 随机漫步
        "look_tracking": True,  # 16 方向注视鼠标
        "look_offset": 270,     # 图集角度偏移（0°=正面朝镜头，270°=朝右）
        "look_range": 520,      # 注视范围（像素）
        "bubble_follow": True,
    },
    # ---------------- 位置
    "pos": {"x": None, "y": None},
    # ---------------- 速率口径
    "basis": "eff",             # eff | raw | base
    # ---------------- 数据源：桌宠查哪个应用的用量
    # all | workbuddy | qoder | codex | claude | codebuddy（与 collector.sources.SOURCE_IDS 对齐）
    "source": "all",
    # ---------------- 提醒
    "alerts": {
        "ctx_warn": 0.80,       # 上下文占用阈值
        "ctx_crit": 0.95,
        "credit_warn": 0.0,     # 今日积分阈值，0 = 关闭
        "sound": True,
        "bubble": True,
        "cooldown_sec": 300,
    },
    # ---------------- 余额（多服务商，参照 cc-switch）
    "providers": {
        "max_shown": 5,          # 面板最多同时显示几个余额
        "refresh_sec": 1800,
        "items": [
            {
                "id": "deepseek", "label": "DeepSeek", "template": "deepseek",
                "base_url": "https://api.deepseek.com", "path": "", "json_path": "",
                "api_key": "", "auth": "bearer", "scale": "", "currency": "¥",
                "user_id": "", "enabled": False,
            },
            {
                "id": "moonshot", "label": "Moonshot / Kimi", "template": "moonshot",
                "base_url": "https://api.moonshot.cn", "path": "", "json_path": "",
                "api_key": "", "auth": "bearer", "scale": "", "currency": "¥",
                "user_id": "", "enabled": False,
            },
            {
                "id": "newapi", "label": "New API 中转站", "template": "newapi",
                "base_url": "", "path": "", "json_path": "",
                "api_key": "", "auth": "bearer", "scale": "", "currency": "¥",
                "user_id": "", "enabled": False,
            },
            {
                "id": "openai", "label": "OpenAI 兼容", "template": "openai",
                "base_url": "", "path": "", "json_path": "",
                "api_key": "", "auth": "bearer", "scale": "", "currency": "$",
                "user_id": "", "enabled": False,
            },
            {
                "id": "custom", "label": "自定义", "template": "custom",
                "base_url": "", "path": "", "json_path": "",
                "api_key": "", "auth": "bearer", "scale": "", "currency": "¥",
                "user_id": "", "enabled": False,
            },
        ],
    },
    # ---------------- 贴靠 WorkBuddy / Qoder
    "dock": {
        "enabled": False,        # 贴靠并跟随目标应用窗口
        "target": "workbuddy",   # 贴靠目标：workbuddy | qoder（见 overlay._DOCK_TARGETS）
        "follow": True,          # 窗口移动/缩放时跟随
        "hide_unfocused": True,  # 目标应用不在前台时隐藏
        "hide_fullscreen": True, # 目标应用全屏时隐藏
        "margin_x": 24,          # 距窗口右边缘
        "margin_y": 14,          # 距窗口底边缘
    },
    # ---------------- 系统集成
    "system": {
        "autostart": False,
        "hide_on_fullscreen": True,
        "hotkey": "ctrl+alt+w",
        "tray": True,
    },
}


def _merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("1", "true", "on", "yes", "y")


def normalize(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    布尔归一化：早期界面把复选框读成了字符串 "on"，写进了配置，
    导致 `if item["enabled"]` 永远为真。这里统一转成真布尔。
    """
    pet = cfg.get("pet") or {}
    for k in ("wander", "look_tracking", "bubble_follow"):
        if k in pet:
            pet[k] = _truthy(pet[k])
    for k in ("sound", "bubble"):
        al = cfg.get("alerts") or {}
        if k in al:
            al[k] = _truthy(al[k])
    sy = cfg.get("system") or {}
    for k in ("autostart", "hide_on_fullscreen", "tray"):
        if k in sy:
            sy[k] = _truthy(sy[k])
    dk = cfg.get("dock") or {}
    for k in ("enabled", "follow", "hide_unfocused", "hide_fullscreen"):
        if k in dk:
            dk[k] = _truthy(dk[k])
    for it in ((cfg.get("providers") or {}).get("items") or []):
        if "enabled" in it:
            it["enabled"] = _truthy(it["enabled"])
    return cfg


def load() -> Dict[str, Any]:
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return normalize(_merge(DEFAULTS, raw))
    except Exception:
        return json.loads(json.dumps(DEFAULTS))


def save(cfg: Dict[str, Any]) -> None:
    normalize(cfg)
    try:
        CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# ---------------------------------------------------------------- 服务端口发现
# server.py 在端口被占用时会顺延，所以不能各处硬编码 8971。
# 启动后把真实端口写到 .cache/server-port，桌宠与启动器都从这里读。

def write_port(port: int) -> None:
    try:
        PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORT_FILE.write_text(str(int(port)), encoding="ascii")
    except OSError:
        pass


def read_port(default: int = DEFAULT_PORT) -> int:
    try:
        return int(PORT_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return default


def server_url(default: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{read_port(default)}/"
