#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多应用数据源（Qoder / Codex / Claude Code）
==========================================

`collector.py` 原本只认 WorkBuddy 一种来源。这里把"还有哪些应用在本地留了用量"
抽成独立的数据源层，让上层按 `src` 字段筛选，而不用关心各家格式差异。

四家在本机的实测情况（2026-10-21 勘察）：

    workbuddy  ~/.workbuddy            token + credit 都有，由 collector.py 自己解析
    qoder      ~/.qoder-cn             **token 恒为 0**（2108 个字段实例无一非零），
                                       只有 credits / original_credits / context_usage_ratio
    codex      ~/.codex                原生 rollout 含 token/耗时/TTFT；
                                       cc-switch 代理日志可补历史与首字延迟
    claude     ~/.claude               原生 projects JSONL 含真实 token；
                                       cc-switch 代理日志可补历史与首字延迟

一手数据（原生 rollout / 转录）优先，cc-switch 只补缺口，合并时按 request_id
与 (源, 会话, 输入, 输出) 去重。UI 侧靠 `meta[src]["note"]` 说明数据来源与
"为什么是空的"，避免把"没数据"误显示成"用了 0"。

Qoder 的两处数据是互补的，靠 `request_id` 关联（实测 179/179 全部对得上）：

    logs/sessions/**/segments/*.jsonl   请求耗时（started/completed 配对，312/314 成功）
                                        + 工具执行区间（400/401 可配对）+ 更长的历史
    projects/**/*.jsonl                 credits / context_usage_ratio / 子 agent 标记

只解析其中一处都会丢东西：logs 没有 credits，projects 没有耗时且只保留当前会话。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HOME = Path.home()

# ---------------------------------------------------------------- 数据源注册表

SOURCES: Dict[str, Dict[str, Any]] = {
    "workbuddy": {
        "label": "WorkBuddy",
        "roots": [HOME / ".workbuddy"],
        # token/credit/dur/tools 四项能力，UI 据此决定显示真值还是"—"
        "caps": {"tokens": True, "credits": True, "dur": True, "tools": True},
        "unit": "tokens",
    },
    "qoder": {
        "label": "Qoder",
        "roots": [HOME / ".qoder-cn", HOME / ".qoder"],
        "caps": {"tokens": False, "credits": True, "dur": True, "tools": True},
        "unit": "credits",
        "note": "本机 Qoder 不回传 token 数（projects 2108 个 token 字段实例、"
                "日志 1217 条 model.response.completed 全为 0），只有 credits；"
                "因此 token 与 tok/s 一律显示为「—」，不是 0。"
                " 内部模型代号与 BYOK 配置 UUID 已按 Qoder 客户端的模型目录还原为真实名称。",
    },
    "codex": {
        "label": "Codex",
        "roots": [HOME / ".codex"],
        "caps": {"tokens": False, "credits": False, "dur": True, "tools": False},
        "unit": "requests",
        "note": "原生 rollout（sessions/**/rollout-*.jsonl）含 token 与真实首字延迟；"
                "旧 .jsonl.zst 需 zstandard，未安装时跳过。"
                " sqlite 投影仍只有请求数，历史代理数据由 cc-switch 补充。",
    },
    "claude": {
        "label": "Claude Code",
        "roots": [HOME / ".claude"],
        "caps": {"tokens": True, "credits": False, "dur": False, "tools": False},
        "unit": "tokens",
        "note": "原生 projects JSONL 含真实 token；cc-switch 可补历史与首字延迟。",
    },
}

SOURCE_IDS = tuple(SOURCES.keys())


def _roots(src: str) -> List[Path]:
    return [r for r in SOURCES[src]["roots"] if r.exists()]


# ---------------------------------------------------------------- 时间解析

def iso_ms(s: Any) -> float:
    """
    ISO8601 → 毫秒时间戳。

    Qoder 的 logs 用带时区偏移的 `2026-10-19T14:47:59.471+08:00`，
    projects 用 UTC 的 `2026-10-20T16:00:08.895Z`，两种都要吃下。
    已经是数字的（WorkBuddy / Codex）直接放行。
    """
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        v = float(s)
        # 秒级时间戳补齐成毫秒（Codex 的 started_at 是秒）
        return v * 1000.0 if v < 1e12 else v
    try:
        txt = str(s).strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        return datetime.fromisoformat(txt).timestamp() * 1000.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------- 通用记录

def _rec(src: str, ts: float, sid: str, model: str = "", **kw) -> Dict[str, Any]:
    """统一记录结构：与 collector._parse_file 产出的字段对齐，外加多源字段。"""
    r = {
        "ts": ts, "sid": sid, "model": model or "",
        "in": 0, "out": 0, "cached": 0, "reasoning": 0,
        "credit": 0.0, "orig_credit": 0.0, "ctx": 0.0,
        "sub": 0, "dur": 0, "model_ms": 0,
        "src": src, "rid": "",
    }
    r.update(kw)
    return r


# ---------------------------------------------------------------- Qoder

def _qoder_paths() -> Tuple[List[Path], List[Path]]:
    """
    返回 (logs 段文件, projects 转录文件)。

    用**浅层**通配而不是 `**`：目录层级是固定的，浅层实测 0.023s vs 递归 0.048s。
    采集是每 5s 一轮的常驻开销，这点差别会直接反映在 CPU 上。
    """
    logs: List[Path] = []
    proj: List[Path] = []
    for root in _roots("qoder"):
        seg = root / "logs" / "sessions"
        if seg.exists():
            logs.extend(sorted(map(Path, glob.glob(
                str(seg / "*" / "*" / "segments" / "*.jsonl")))))
        pd = root / "projects"
        if pd.exists():
            proj.extend(sorted(map(Path, glob.glob(str(pd / "*" / "*.jsonl")))))
            proj.extend(sorted(map(Path, glob.glob(
                str(pd / "*" / "*" / "subagents" / "*.jsonl")))))
    return logs, proj


def _sess_from_log_path(p: Path) -> str:
    """logs/sessions/<workspace>/<session-uuid>/segments/<file>.jsonl → <session-uuid>"""
    parts = p.parts
    try:
        i = parts.index("sessions")
        return parts[i + 2]
    except (ValueError, IndexError):
        return p.stem


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                      re.I)

# Qoder 的 model 字段是内部代号：内置模型写短键（qfmodel 等），用户自建的
# BYOK 模型写配置 UUID。两类都要还原成真实模型名，否则「按模型」里会出现
# 一堆 Qfmodel / unknown-xxxxxxxx。兜底表对应 Qoder CN 1.1.69 的模型目录，
# 运行时会用客户端语言包与 main.sqlite 里的 BYOK 表覆盖/补充。
_QODER_MODEL_FALLBACK = {
    "qmodel": "Qwen3.7-Plus",
    "qmodel_latest": "Qwen3.7-Max",
    "qmodel_38max": "Qwen3.8-Max",
    "qfmodel": "Qwen3.8-Flash",
    "dmodel": "DeepSeek-V4-Pro",
    "dfmodel": "DeepSeek-V4-Flash",
    "gmodel": "GLM-5.3",
    "gfmodel": "GLM-5.3-Flash",
    "kmodel": "Kimi-K2.7-Code",
    "kmodel_latest": "Kimi-K3",
    "mmodel": "MiniMax-M3",
    "cmodel": "Cantus",
}

_MODEL_MAP_CACHE: Dict[str, Any] = {"sig": "", "checked": 0.0, "map": {}}
_INSTALL_DIR_CACHE: Dict[str, Any] = {"checked": 0.0, "dir": None}
_BYOK_LOG_CACHE: Dict[str, str] = {}
_MODEL_MAP_TTL = 60.0


def _canon_model(name: Any) -> str:
    """展示名 → 聚合用的规范 id（小写、空格转连字符），UI 侧再美化。"""
    return re.sub(r"\s+", "-", str(name or "").strip()).lower()


def _qoder_install_dir() -> Optional[Path]:
    """定位 Qoder CN 安装目录：优先从最近一次 run 的 argv[0] 反推。"""
    now = time.time()
    c = _INSTALL_DIR_CACHE
    if c["dir"] is not None or now - c["checked"] < 600:
        return c["dir"]

    cands: List[Path] = []
    env = os.environ.get("QODER_CN_HOME") or os.environ.get("QODER_HOME")
    if env:
        cands.append(Path(env))
    try:
        manifests = sorted((HOME / ".qoder-cn" / "logs" / "runs").glob("*/manifest.json"),
                           key=lambda q: q.stat().st_mtime, reverse=True)[:3]
    except OSError:
        manifests = []
    for m in manifests:
        try:
            argv = json.loads(m.read_text(encoding="utf-8", errors="ignore")).get("argv") or []
        except Exception:
            continue
        if argv:
            cands.append(Path(argv[0]).parent)
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES"),
                 os.environ.get("PROGRAMFILES(X86)")):
        if base:
            cands.append(Path(base) / "Programs" / "Qoder CN")
    for d in cands:
        if (d / "resources" / "dynamic-text" / "qoder-cn.json").exists():
            c.update(checked=now, dir=d)
            return d
    c.update(checked=now, dir=None)
    return None


def _qoder_locale_labels() -> Dict[str, str]:
    """客户端语言包里的 model.<key>.label 就是官方展示名。"""
    inst = _qoder_install_dir()
    if inst is None:
        return {}
    try:
        d = json.loads((inst / "resources" / "dynamic-text" / "qoder-cn.json")
                       .read_text(encoding="utf-8"))
    except Exception:
        return {}
    text = d.get("en") or d.get("zh") or {}
    out: Dict[str, str] = {}
    for k, v in text.items():
        if k.startswith("model.") and k.endswith(".label") and v:
            out[k[len("model."):-len(".label")].lower()] = _canon_model(v)
    return out


def _byok_display_name(model_key: Any) -> str:
    """BYOK 的 model_key（如 qwen3.8-max-pg / deepseek-v4.1-flash-1017-pg）→ 规范 id。"""
    s = str(model_key or "").strip()
    s = re.sub(r"-pg$", "", s, flags=re.I)      # 百炼私有实例后缀
    s = re.sub(r"-\d{4}$", "", s)               # 快照日期后缀，如 -1017
    return _canon_model(s)


def _qoder_byok_labels() -> Dict[str, str]:
    """main.sqlite 的 BYOK 表：配置 UUID → 真实模型名。"""
    appdata = Path(os.environ.get("APPDATA") or (HOME / "AppData" / "Roaming"))
    out: Dict[str, str] = {}
    for db in sorted(appdata.glob("com.qoder*/main.sqlite")):
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
            try:
                for cid, mkey in conn.execute(
                        "SELECT config_id, model_key FROM byok_model_capability_metadata"):
                    name = _byok_display_name(mkey)
                    if cid and name:
                        out[str(cid).strip().lower()] = name
            except sqlite3.Error:
                pass
            try:
                for pid, mkey, disp in conn.execute(
                        "SELECT profile_id, model_key, model_display_name "
                        "FROM byok_model_profiles"):
                    name = _canon_model(disp) or _byok_display_name(mkey)
                    if pid and name:
                        out[str(pid).strip().lower()] = name
            except sqlite3.Error:
                pass
        except Exception:
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return out


def _qoder_byok_from_logs(uuids: set) -> Dict[str, str]:
    """
    兜底：数据库被锁或版本差异时，从最近几次 run 的 qodercli.log 里读
    `[ByokConfig] {"config_key": <uuid>, "model": ...}`。只在确实遇到
    未识别 UUID 时调用，且结果按 UUID 缓存，稳态没有开销。
    """
    try:
        logs = sorted((HOME / ".qoder-cn" / "logs" / "runs").glob("*/qodercli.log"),
                      key=lambda q: q.stat().st_mtime, reverse=True)[:5]
    except OSError:
        return {}
    out: Dict[str, str] = {}
    for p in logs:
        try:
            with p.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if "[ByokConfig]" not in line or "config_key" not in line:
                        continue
                    try:
                        obj = json.loads(line[line.index("{"):])
                    except ValueError:
                        continue
                    cid = str(obj.get("config_key") or "").strip().lower()
                    if cid in uuids and cid not in out:
                        name = _byok_display_name(obj.get("model"))
                        if name:
                            out[cid] = name
        except OSError:
            continue
        if len(out) >= len(uuids):
            break
    return out


def _qoder_model_map() -> Dict[str, str]:
    """代号/UUID → 规范模型 id；语言包与 BYOK 表有变化时最多每 60s 重建一次。"""
    now = time.time()
    c = _MODEL_MAP_CACHE
    if c["map"] and now - c["checked"] < _MODEL_MAP_TTL:
        return c["map"]

    # 保留上一轮已知的 UUID 映射：Qoder 运行时 sqlite 偶发被锁时，
    # 读取失败不会把已识别的 BYOK 模型打回 unknown。
    m = dict(c["map"])
    m.update({k: _canon_model(v) for k, v in _QODER_MODEL_FALLBACK.items()})
    m.update(_qoder_locale_labels())
    m.update(_qoder_byok_labels())
    sig = hashlib.sha1(json.dumps(sorted(m.items())).encode("utf-8")).hexdigest()[:12]
    c.update(sig=sig, checked=now, map=m)
    return m


def _qoder_model_sig() -> str:
    """模型映射指纹；写进解析缓存条目，映射变了就重解析，历史数据也能纠正。"""
    _qoder_model_map()
    return str(_MODEL_MAP_CACHE["sig"])


def _norm_model(m: Any) -> str:
    """
    Qoder 的 model 字段还原：内部代号走语言包，BYOK UUID 走 main.sqlite，
    都查不到才退回 `unknown-xxxxxxxx`（源数据真的没给可读名字时）。
    """
    s = str(m or "").strip()
    if not s:
        return ""
    low = s.lower()
    hit = _qoder_model_map().get(low)
    if hit:
        return hit
    if _UUID_RE.match(s):
        if low not in _BYOK_LOG_CACHE:
            _BYOK_LOG_CACHE[low] = _qoder_byok_from_logs({low}).get(low, "")
            if _BYOK_LOG_CACHE[low]:
                _MODEL_MAP_CACHE["map"][low] = _BYOK_LOG_CACHE[low]
        if _BYOK_LOG_CACHE[low]:
            return _BYOK_LOG_CACHE[low]
        return "unknown-" + s[:8]
    return s


def _collect_qoder(old: Dict[str, Any]) -> Dict[str, Any]:
    logs, proj = _qoder_paths()
    new_files: Dict[str, Any] = {}
    records: List[Dict[str, Any]] = []
    calls: List[List[Any]] = []
    rets: List[List[Any]] = []
    dirty = False

    # 模型映射一变（新增 BYOK、客户端升级改名），已解析的数据也要重算，
    # 否则旧缓存里的 Qfmodel / unknown-xxxx 会一直留着。
    sig = _qoder_model_sig()

    # 请求耗时与工具区间来自 logs；credits 来自 projects。两者分别按文件缓存，
    # 最后在 request_id 上合并。
    starts: Dict[str, float] = {}
    start_models: Dict[str, str] = {}
    ends: Dict[str, Dict[str, Any]] = {}
    creds: Dict[str, Dict[str, Any]] = {}

    for p in logs:
        key = "qoder-log:" + str(p)
        hit = _cache_hit(old, p, key, sig)
        if hit is None:
            item = _parse_qoder_log(p)
            dirty = True
        else:
            item = hit
        new_files[key] = _entry(p, item, sig)
        starts.update(item.get("starts") or {})
        start_models.update(item.get("start_models") or {})
        for rid, e in (item.get("ends") or {}).items():
            ends.setdefault(rid, e)
        calls.extend([c, t, s] for c, t, s in (item.get("calls") or []))
        rets.extend([c, t] for c, t in (item.get("rets") or []))

    for p in proj:
        key = "qoder-proj:" + str(p)
        hit = _cache_hit(old, p, key, sig)
        if hit is None:
            item = _parse_qoder_project(p)
            dirty = True
        else:
            item = hit
        new_files[key] = _entry(p, item, sig)
        for rid, c in (item.get("creds") or {}).items():
            creds.setdefault(rid, c)

    seen: set = set()
    for rid, e in ends.items():
        if rid in seen:
            continue
        seen.add(rid)
        st = starts.get(rid)
        c = creds.get(rid) or {}
        records.append(_rec(
            "qoder", e["ts"], e["sid"],
            c.get("model") or e.get("model") or start_models.get(rid) or "",
            rid=rid,
            dur=int(max(0.0, e["ts"] - st)) if st else 0,
            credit=float(c.get("credit") or 0.0),
            orig_credit=float(c.get("orig_credit") or 0.0),
            ctx=float(c.get("ctx") or 0.0),
            sub=int(c.get("sub") or 0),
        ))
    # projects 里有、logs 里没有的请求（历史被滚动清掉时会出现）
    for rid, c in creds.items():
        if rid in seen:
            continue
        seen.add(rid)
        records.append(_rec(
            "qoder", c.get("ts") or 0.0, c.get("sid") or "", c.get("model") or "",
            rid=rid, credit=float(c.get("credit") or 0.0),
            orig_credit=float(c.get("orig_credit") or 0.0),
            ctx=float(c.get("ctx") or 0.0), sub=int(c.get("sub") or 0),
        ))

    return {"records": records, "calls": calls, "rets": rets,
            "files": new_files, "dirty": dirty}


def _parse_qoder_log(p: Path) -> Dict[str, Any]:
    starts: Dict[str, float] = {}
    start_models: Dict[str, str] = {}
    ends: Dict[str, Dict[str, Any]] = {}
    calls: List[List[Any]] = []
    rets: List[List[Any]] = []
    sid = _sess_from_log_path(p)
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                # 段文件里绝大多数是 hook.* 噪声，先按子串粗筛再解析 JSON
                if '"model.re' not in line and '"tool.' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                ts = iso_ms(d.get("ts"))
                if t == "model.request.started":
                    rid = d.get("request_id")
                    data = d.get("data") if isinstance(d.get("data"), dict) else {}
                    if rid and ts:
                        starts[rid] = ts
                        if data.get("model"):
                            start_models[rid] = _norm_model(data.get("model"))
                elif t == "model.response.completed":
                    rid = d.get("request_id")
                    data = d.get("data") if isinstance(d.get("data"), dict) else {}
                    if rid and ts:
                        ends[rid] = {"ts": ts, "sid": sid,
                                     "model": _norm_model(data.get("model"))}
                elif t == "tool.requested":
                    cid = d.get("tool_call_id")
                    if cid and ts:
                        calls.append([cid, ts, sid])
                elif t == "tool.execution.finished":
                    cid = d.get("tool_call_id")
                    if cid and ts:
                        rets.append([cid, ts])
    except OSError:
        pass
    return {"starts": starts, "start_models": start_models,
            "ends": ends, "calls": calls, "rets": rets}


def _parse_qoder_project(p: Path) -> Dict[str, Any]:
    """projects 转录：只取 assistant 消息里的 usage（credits 与上下文占用在这里）。"""
    creds: Dict[str, Dict[str, Any]] = {}
    is_sub = 1 if "subagents" in p.parts else 0
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                m = d.get("message")
                if not isinstance(m, dict):
                    continue
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                rid = u.get("request_id") or m.get("id") or d.get("uuid")
                if not rid:
                    continue
                creds[str(rid)] = {
                    "ts": iso_ms(d.get("timestamp")),
                    "sid": d.get("sessionId") or "",
                    "model": _norm_model(m.get("model")),
                    "credit": u.get("credits") or 0.0,
                    "orig_credit": u.get("original_credits") or 0.0,
                    "ctx": u.get("context_usage_ratio") or 0.0,
                    "sub": 1 if (is_sub or d.get("isSidechain")) else 0,
                }
    except OSError:
        pass
    return {"creds": creds}


# ---------------------------------------------------------------- Codex

def _collect_codex(old: Dict[str, Any]) -> Dict[str, Any]:
    """
    Codex 有两条一手链路：
      · sessions/**/rollout-*.jsonl      真实 token + task_complete 的 duration/TTFT
      · thread_history_*.sqlite          只有请求数与耗时（旧投影）

    rollout 覆盖到的会话不再保留 sqlite 记录，避免同一 turn 被算两次。
    旧会话是 .jsonl.zst，没装 zstandard 时跳过（不引入硬依赖）。
    """
    out = {"records": [], "calls": [], "rets": [], "files": {}, "dirty": False}
    dbs = []
    for root in _roots("codex"):
        dbs.extend(sorted(root.glob("thread_history_*.sqlite")))
    sql_records: List[Dict[str, Any]] = []
    for db in dbs:
        key = "codex-db:" + str(db)
        hit = _cache_hit(old, db, key)
        if hit is None:
            item = _parse_codex_db(db)
            out["dirty"] = True
        else:
            item = hit
        out["files"][key] = _entry(db, item)
        sql_records.extend(item.get("records") or [])

    roll = _collect_codex_rollouts(old)
    out["files"].update(roll["files"])
    out["dirty"] = out["dirty"] or roll["dirty"]
    roll_records = roll["records"]
    covered = {r["sid"] for r in roll_records
               if (r.get("in") or 0) + (r.get("out") or 0) > 0}
    out["records"].extend(r for r in sql_records if r["sid"] not in covered)
    out["records"].extend(roll_records)
    return out


def _codex_rollout_paths() -> List[Path]:
    paths: List[Path] = []
    for root in _roots("codex"):
        base = root / "sessions"
        if base.exists():
            paths.extend(sorted(map(Path, base.glob("**/rollout-*.jsonl"))))
    return paths


def _collect_codex_rollouts(old: Dict[str, Any]) -> Dict[str, Any]:
    out = {"records": [], "files": {}, "dirty": False}
    for p in _codex_rollout_paths():
        key = "codex-rollout:" + str(p)
        hit = _cache_hit(old, p, key)
        if hit is None:
            item = _parse_codex_rollout(p)
            out["dirty"] = True
        else:
            item = hit
        out["files"][key] = _entry(p, item)
        out["records"].extend(item.get("records") or [])
    return out


def _parse_codex_rollout(p: Path) -> Dict[str, Any]:
    """
    rollout 是逐事件 JSONL。用量在 token_usage_record，耗时/TTFT 在
    event_msg.task_complete；模型名在 thread_settings_applied。

    一次 turn 只有一个 token_usage_record（缺失时宁可不造数），
    时间取记录自身的 timestamp，而不是文件 mtime。
    """
    recs: List[Dict[str, Any]] = []
    sid = p.stem
    model = ""
    turns: Dict[str, Dict[str, Any]] = {}
    usages: List[Tuple[Any, Dict[str, Any]]] = []
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if ("token_usage_record" not in line
                        and "task_complete" not in line
                        and "thread_settings_applied" not in line
                        and "session_meta" not in line):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                typ = d.get("type")
                pl = d.get("payload") or {}
                if typ == "session_meta":
                    sid = str(pl.get("session_id") or pl.get("id") or sid)
                elif typ == "event_msg":
                    pt = pl.get("type")
                    if pt == "thread_settings_applied":
                        m = (pl.get("thread_settings") or {}).get("model")
                        if m:
                            model = str(m)
                    elif pt == "task_complete":
                        tid = str(pl.get("turn_id") or pl.get("root_turn_id") or "")
                        turns[tid] = pl
                elif typ == "token_usage_record":
                    usages.append((d.get("timestamp"), pl))
    except OSError:
        pass

    for ts_raw, pl in usages:
        u = pl.get("usage") or {}
        sid_r = str(pl.get("session_id") or pl.get("thread_id") or sid)
        tid = str(pl.get("turn_id") or "")
        tc = turns.get(tid) or {}
        ts = iso_ms(ts_raw)
        if not ts and tc.get("completed_at"):
            ts = iso_ms(tc.get("completed_at"))
        dur = int(tc.get("duration_ms") or 0)
        ttft = int(tc.get("time_to_first_token_ms") or 0)
        rec = _rec(
            "codex", ts, sid_r, _norm_model(model),
            rid=str(pl.get("response_id") or ""),
            **{"in": int(u.get("input_tokens") or 0),
               "out": int(u.get("output_tokens") or 0),
               "cached": int(u.get("cached_input_tokens") or 0),
               "cache_write": int(u.get("cache_write_input_tokens") or 0),
               "reasoning": int(u.get("reasoning_output_tokens") or 0),
               "dur": dur, "exact_ms": dur, "ttft": ttft,
               "data_src": "native"},
        )
        recs.append(rec)
    return {"records": recs}


def _parse_codex_db(db: Path) -> Dict[str, Any]:
    recs: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            for tid, turn, status, st, dur in conn.execute(
                "SELECT thread_id, turn_id, status, started_at, duration_ms "
                "FROM thread_turns"
            ):
                recs.append(_rec(
                    "codex", iso_ms(st), str(tid), "",
                    rid=str(turn or ""), dur=int(dur or 0),
                    status=str(status or ""),
                ))
        finally:
            conn.close()
    except Exception:
        pass
    return {"records": recs}


# ---------------------------------------------------------------- Claude Code

def _collect_claude(old: Dict[str, Any]) -> Dict[str, Any]:
    """
    Claude Code CLI 的标准转录（projects/<encoded-cwd>/<uuid>.jsonl）带**真实 token**，
    格式与 Qoder 的 projects 同构，所以共用解析器。

    本机当前为空：桌面版把会话释放掉了，projects 下只剩
    `*.desktop-released.json` 墓碑（{"reason":"delete"}），sessions/ 是空目录。
    解析器照写，日后有转录就会自动出数。
    """
    out = {"records": [], "calls": [], "rets": [], "files": {}, "dirty": False}
    paths: List[Path] = []
    for root in _roots("claude"):
        pd = root / "projects"
        if pd.exists():
            paths.extend(sorted(map(Path, glob.glob(str(pd / "*" / "*.jsonl")))))
    for p in paths:
        key = "claude:" + str(p)
        hit = _cache_hit(old, p, key)
        if hit is None:
            item = _parse_anthropic_jsonl(p, "claude")
            out["dirty"] = True
        else:
            item = hit
        out["files"][key] = _entry(p, item)
        out["records"].extend(item.get("records") or [])
    return out


def _parse_anthropic_jsonl(p: Path, src: str) -> Dict[str, Any]:
    """Anthropic 标准转录：message.usage 里 input/output/cache_* 都是真值。"""
    recs: List[Dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                m = d.get("message")
                if not isinstance(m, dict):
                    continue
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                rid = str(u.get("request_id") or m.get("id") or d.get("uuid") or "")
                recs.append(_rec(
                    src, iso_ms(d.get("timestamp")), str(d.get("sessionId") or p.stem),
                    _norm_model(m.get("model")), rid=rid,
                    **{"in": int(u.get("input_tokens") or 0),
                       "out": int(u.get("output_tokens") or 0),
                       "cached": int(u.get("cache_read_input_tokens") or 0),
                       "reasoning": 0,
                       "credit": float(u.get("credits") or 0.0),
                       "orig_credit": float(u.get("original_credits") or 0.0),
                       "ctx": float(u.get("context_usage_ratio") or 0.0),
                       "sub": 1 if d.get("isSidechain") else 0},
                ))
    except OSError:
        pass
    return {"records": recs}


# ---------------------------------------------------------------- cc-switch 回退

CC_DB = HOME / ".cc-switch" / "cc-switch.db"


def _cc_key_rid(rid: Any) -> str:
    s = str(rid or "").strip().lower()
    if s.startswith("session:"):
        s = s[len("session:"):]
    return s


_CC_CACHE: Dict[str, Any] = {"stamp": None, "ts": 0.0, "recs": []}


def _collect_ccswitch() -> List[Dict[str, Any]]:
    """
    只读 cc-switch 的代理请求日志，作为 Claude/Codex 的历史与 TTFT 补充。

    一手数据（原生转录/rollout）永远优先：合并阶段会把已有记录剔掉，
    只把 cc-switch 的 first_token_ms 补到一手记录上。
    """
    if not CC_DB.exists():
        return []
    try:
        st = CC_DB.stat()
    except OSError:
        return []
    stamp = (st.st_mtime, st.st_size)
    now = time.time()
    # 桌宠/仪表盘每 1~3 秒轮询一次；这个库很少变，5 秒内直接用上次结果
    if _CC_CACHE["stamp"] == stamp and now - float(_CC_CACHE["ts"]) < 5.0:
        return _CC_CACHE["recs"]
    recs: List[Dict[str, Any]] = []
    conn = None
    try:
        conn = sqlite3.connect(f"file:{CC_DB}?mode=ro", uri=True, timeout=1)
        rows = conn.execute(
            "SELECT request_id, app_type, model, request_model, input_tokens, "
            "output_tokens, cache_read_tokens, latency_ms, first_token_ms, "
            "duration_ms, created_at, session_id, data_source, status_code, "
            "error_message FROM proxy_request_logs ORDER BY created_at"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    for row in rows:
        (rid, app, model, rmodel, inp, out, cached, latency, first_token,
         duration, created, sid, data_source, status, err) = row
        app_l = str(app or "").lower()
        if app_l.startswith("claude"):
            src = "claude"
        elif app_l.startswith("codex"):
            src = "codex"
        else:
            continue
        try:
            inp, out = int(inp or 0), int(out or 0)
            cached = int(cached or 0)
            first_token = int(first_token or 0)
            status = int(status or 0)
        except Exception:
            continue
        if err or status >= 400:
            continue
        # 代理健康检查：1 个输入 token、几字节输出，混进用量会把速率拉平。
        if inp + out < 20:
            continue
        dur = int(duration or latency or 0)
        ts = iso_ms(created)
        recs.append(_rec(
            src, ts, str(sid or ""), _norm_model(model or rmodel),
            rid=str(rid or ""),
            **{"in": inp, "out": out, "cached": cached,
               "dur": dur, "exact_ms": dur, "ttft": first_token,
               "data_src": "ccswitch"},
        ))
    _CC_CACHE.update(stamp=stamp, ts=now, recs=recs)
    return recs


def _merge_ccswitch(native: List[Dict[str, Any]],
                    extra: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """一手优先：按 request_id 或 (源, 会话, 输入, 输出) 去重并补 TTFT。"""
    if not extra:
        return native
    by_rid: Dict[str, Dict[str, Any]] = {}
    by_sig: Dict[Tuple[str, str, int, int], Dict[str, Any]] = {}
    for r in native:
        if r.get("src") not in ("claude", "codex"):
            continue
        r.setdefault("data_src", "native")
        rid = _cc_key_rid(r.get("rid"))
        if rid:
            by_rid.setdefault(rid, r)
        sig = (r.get("src"), str(r.get("sid") or ""),
               int(r.get("in") or 0), int(r.get("out") or 0))
        by_sig.setdefault(sig, r)

    out = list(native)
    seen: set = set()
    for r in extra:
        rid = _cc_key_rid(r.get("rid"))
        sig = (r.get("src"), str(r.get("sid") or ""),
               int(r.get("in") or 0), int(r.get("out") or 0))
        hit = by_rid.get(rid) if rid else None
        if hit is None:
            hit = by_sig.get(sig)
        if hit is not None:
            if not hit.get("ttft") and r.get("ttft"):
                hit["ttft"] = r["ttft"]
            continue
        dedup = rid or sig
        if dedup in seen:
            continue
        seen.add(dedup)
        out.append(r)
    return out


# ---------------------------------------------------------------- 缓存工具

def _cache_hit(old: Dict[str, Any], p: Path, key: str,
               sig: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """(mtime, size) 指纹命中则返回缓存条目，否则 None 表示需要重解析。"""
    hit = old.get(key)
    if not hit:
        return None
    if sig is not None and hit.get("msig") != sig:
        return None
    try:
        st = p.stat()
    except OSError:
        return None
    if hit.get("mtime") == st.st_mtime and hit.get("size") == st.st_size:
        return hit
    return None


def _entry(p: Path, item: Dict[str, Any], sig: Optional[str] = None) -> Dict[str, Any]:
    try:
        st = p.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        mtime, size = 0.0, 0
    e = {"mtime": mtime, "size": size}
    if sig is not None:
        e["msig"] = sig
    e.update(item)
    return e


# ---------------------------------------------------------------- 汇总入口

_COLLECTORS = {
    "qoder": _collect_qoder,
    "codex": _collect_codex,
    "claude": _collect_claude,
}


def collect(old_files: Dict[str, Any]) -> Dict[str, Any]:
    """
    采集 workbuddy 以外的所有源。

    返回 {records, calls, rets, files, dirty}，字段含义与 collector.collect_requests
    内部一致，可直接并入。
    """
    out: Dict[str, Any] = {"records": [], "calls": [], "rets": [],
                           "files": {}, "dirty": False}
    for src, fn in _COLLECTORS.items():
        try:
            r = fn(old_files)
        except Exception:
            continue
        out["records"].extend(r.get("records") or [])
        out["calls"].extend(r.get("calls") or [])
        out["rets"].extend(r.get("rets") or [])
        out["files"].update(r.get("files") or {})
        out["dirty"] = out["dirty"] or bool(r.get("dirty"))

    # 原生数据优先；cc-switch 只补一手没覆盖到的历史与 first_token_ms。
    out["records"] = _merge_ccswitch(out["records"], _collect_ccswitch())
    return out


def probe(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    给 UI 的数据源清单：能力、命中记录数、时间跨度，以及"为什么是空的"。

    note 只在真的有话要说时给出（无数据、口径缺失），有数据就留空，
    免得正常源也挂一堆解释。
    """
    out: List[Dict[str, Any]] = []
    for sid, spec in SOURCES.items():
        rs = [r for r in records if r.get("src") == sid]
        ts = [r["ts"] for r in rs if r.get("ts")]
        detected = any(_roots(sid))
        # note 有两种：**口径说明**（有数据时也要显示，比如 Qoder 为什么 token 是「—」）
        # 和**空数据原因**（只在真的一条都没有时追加）。原来写成"有数据就清空 note"，
        # 结果把最该看到的口径说明给吞了。
        note = spec.get("note", "")
        if not rs:
            extra = _empty_reason(sid, detected)
            note = (note + " " + extra).strip() if note else extra
        caps = dict(spec["caps"])
        # 能力位按真实记录判定：Codex 可能只有一部分 rollout 带 token，
        # 不能因为"理论上支持"就把一屏 0 当成有数据。
        if sid in ("codex", "claude"):
            caps["tokens"] = caps["tokens"] or any(
                (r.get("in") or 0) + (r.get("out") or 0) for r in rs)
        cc = sum(1 for r in rs if r.get("data_src") == "ccswitch")
        if cc:
            note = (note + f" 其中 {cc} 条历史/延迟数据来自 cc-switch。").strip()
        item: Dict[str, Any] = {
            "id": sid,
            "label": spec["label"],
            "unit": spec["unit"],
            "caps": caps,
            "detected": detected,
            "records": len(rs),
            "sessions": len({r["sid"] for r in rs if r.get("sid")}),
            "credits": round(sum(float(r.get("credit") or 0) for r in rs), 4),
            "input": sum(int(r.get("in") or 0) for r in rs),
            "output": sum(int(r.get("out") or 0) for r in rs),
            "first_ts": min(ts) if ts else 0,
            "last_ts": max(ts) if ts else 0,
            "ccswitch_records": cc,
            "note": note,
        }
        out.append(item)
    return out


def _empty_reason(sid: str, detected: bool) -> str:
    if not detected:
        return "未检测到该应用的本地数据目录（可能没装，或装在别处）。"
    if sid == "codex":
        zst = []
        for root in _roots("codex"):
            zst.extend(root.glob("sessions/**/*.jsonl.zst"))
        if zst:
            return ("本机无数据：sqlite 投影里没有 usage 记录，原始 rollout 是 "
                    f"{len(zst)} 个 .jsonl.zst，而未安装 zstandard 无法解压。"
                    "ChatGPT 桌面版的用量在服务端，本地不落盘。")
        return "本机无数据：ChatGPT 桌面版的用量在服务端，本地不落盘。"
    if sid == "claude":
        tomb = []
        for root in _roots("claude"):
            tomb.extend((root / "projects").glob("*/*.desktop-released.json"))
        if tomb:
            return (f"本机无数据：{len(tomb)} 个会话已被应用主动释放"
                    "（projects 下只剩 reason=delete 的墓碑文件，sessions/ 为空）。"
                    "若改用 Claude Code CLI，转录会带真实 token 并自动出现在这里。")
        return "本机无数据：未找到任何会话转录。"
    return "本机无数据。"
