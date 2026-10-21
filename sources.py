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
    codex      ~/.codex                sqlite 里仅 2 个 turn（止于 2026-09-21），无 usage
    claude     ~/.claude               本地会话已被应用删除（projects 下只剩墓碑文件）

后两家现在确实没有可展示的数据，但解析器照写：它们一旦开始落盘就会自动亮起来，
不用改代码。UI 侧靠 `meta[src]["note"]` 说明"为什么是空的"，避免把"没数据"
误显示成"用了 0"。

Qoder 的两处数据是互补的，靠 `request_id` 关联（实测 179/179 全部对得上）：

    logs/sessions/**/segments/*.jsonl   请求耗时（started/completed 配对，312/314 成功）
                                        + 工具执行区间（400/401 可配对）+ 更长的历史
    projects/**/*.jsonl                 credits / context_usage_ratio / 子 agent 标记

只解析其中一处都会丢东西：logs 没有 credits，projects 没有耗时且只保留当前会话。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
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
        "note": "本机 Qoder 不回传 token 数（全量扫描 2108 个 token 字段实例均为 0），"
                "只有 credits；因此 token 与 tok/s 一律显示为「—」，不是 0。"
                "另有部分请求的 model 字段被 Qoder 写成了 UUID，已归并为 unknown-xxxxxxxx。",
    },
    "codex": {
        "label": "Codex",
        "roots": [HOME / ".codex"],
        "caps": {"tokens": False, "credits": False, "dur": True, "tools": False},
        "unit": "requests",
        "note": "ChatGPT 桌面版的用量在服务端；本地 sqlite 投影不含 usage，"
                "因此只有请求数与耗时，token 与 credits 显示为「—」。",
    },
    "claude": {
        "label": "Claude Code",
        "roots": [HOME / ".claude"],
        "caps": {"tokens": True, "credits": False, "dur": False, "tools": False},
        "unit": "tokens",
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


def _norm_model(m: Any) -> str:
    """
    Qoder 的 logs 里有 24 条 `model.response.completed` 把 **UUID 写进了 model 字段**
    （实测 `555c7478-192c-4c37-94fc-3e1fb61dbf72`），不是解析错误，是源数据如此。
    裸 UUID 进"按模型"分组既难看又无法归并，按项目里已有的 `unknown` 约定缩写。
    """
    s = str(m or "").strip()
    if _UUID_RE.match(s):
        return "unknown-" + s[:8]
    return s


def _collect_qoder(old: Dict[str, Any]) -> Dict[str, Any]:
    logs, proj = _qoder_paths()
    new_files: Dict[str, Any] = {}
    records: List[Dict[str, Any]] = []
    calls: List[List[Any]] = []
    rets: List[List[Any]] = []
    dirty = False

    # 请求耗时与工具区间来自 logs；credits 来自 projects。两者分别按文件缓存，
    # 最后在 request_id 上合并。
    starts: Dict[str, float] = {}
    ends: Dict[str, Dict[str, Any]] = {}
    creds: Dict[str, Dict[str, Any]] = {}

    for p in logs:
        key = "qoder-log:" + str(p)
        hit = _cache_hit(old, p, key)
        if hit is None:
            item = _parse_qoder_log(p)
            dirty = True
        else:
            item = hit
        new_files[key] = _entry(p, item)
        starts.update(item.get("starts") or {})
        for rid, e in (item.get("ends") or {}).items():
            ends.setdefault(rid, e)
        calls.extend([c, t, s] for c, t, s in (item.get("calls") or []))
        rets.extend([c, t] for c, t in (item.get("rets") or []))

    for p in proj:
        key = "qoder-proj:" + str(p)
        hit = _cache_hit(old, p, key)
        if hit is None:
            item = _parse_qoder_project(p)
            dirty = True
        else:
            item = hit
        new_files[key] = _entry(p, item)
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
            "qoder", e["ts"], e["sid"], c.get("model") or e.get("model") or "",
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
                    if rid and ts:
                        starts[rid] = ts
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
    return {"starts": starts, "ends": ends, "calls": calls, "rets": rets}


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
    Codex 桌面版把会话投影进 sqlite。本机实测只有 2 个 turn（止于 2026-09-21），
    且 thread_items 里 0 条含 usage —— 所以能给出的只有请求数与耗时。

    另有 sessions/**/*.jsonl.zst 原始 rollout，但本机没装 zstandard，
    且 sqlite 投影已证明其中不含 usage，故不解压。
    """
    out = {"records": [], "calls": [], "rets": [], "files": {}, "dirty": False}
    dbs = []
    for root in _roots("codex"):
        dbs.extend(sorted(root.glob("thread_history_*.sqlite")))
    for db in dbs:
        key = "codex-db:" + str(db)
        hit = _cache_hit(old, db, key)
        if hit is None:
            item = _parse_codex_db(db)
            out["dirty"] = True
        else:
            item = hit
        out["files"][key] = _entry(db, item)
        out["records"].extend(item.get("records") or [])
    return out


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


# ---------------------------------------------------------------- 缓存工具

def _cache_hit(old: Dict[str, Any], p: Path, key: str) -> Optional[Dict[str, Any]]:
    """(mtime, size) 指纹命中则返回缓存条目，否则 None 表示需要重解析。"""
    hit = old.get(key)
    if not hit:
        return None
    try:
        st = p.stat()
    except OSError:
        return None
    if hit.get("mtime") == st.st_mtime and hit.get("size") == st.st_size:
        return hit
    return None


def _entry(p: Path, item: Dict[str, Any]) -> Dict[str, Any]:
    try:
        st = p.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        mtime, size = 0.0, 0
    e = {"mtime": mtime, "size": size}
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
        item: Dict[str, Any] = {
            "id": sid,
            "label": spec["label"],
            "unit": spec["unit"],
            "caps": dict(spec["caps"]),
            "detected": detected,
            "records": len(rs),
            "sessions": len({r["sid"] for r in rs if r.get("sid")}),
            "credits": round(sum(float(r.get("credit") or 0) for r in rs), 4),
            "input": sum(int(r.get("in") or 0) for r in rs),
            "output": sum(int(r.get("out") or 0) for r in rs),
            "first_ts": min(ts) if ts else 0,
            "last_ts": max(ts) if ts else 0,
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
