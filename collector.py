#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Token 用量采集器（多应用）
==========================

WorkBuddy 的数据来源（全部为本地只读文件）：

1. ~/.workbuddy/workbuddy.db
     - sessions       : 会话元数据（标题 / 模型 / 上下文窗口 / 活跃时间）
     - session_usage  : 实时上下文占用（used / size，size 是模型物理窗口）
2. ~/.workbuddy/projects/**/*.jsonl
     - providerData.rawUsage : 逐请求 prompt_tokens / completion_tokens
                               / prompt_cache_hit_tokens / credit(积分)
3. ~/.workbuddy/traces/**/trace_*.json
     - spans[type=generation] 的 duration(ms) → **真实生成耗时**
       与 JSONL 请求按 (sessionId, endedAt) 对齐，中位误差约 17ms，
       据此计算上下行 tokens/秒（而非按墙钟估算）。

其它应用（Qoder / Codex / Claude Code / CodeBuddy）由 `sources.py` 负责解析，
产出同构的记录后在这里并入，靠 `src` 字段区分。各家能给什么差别很大 ——
Qoder 有 credits 但 token 恒为 0，Codex 只有请求数与耗时，Claude 本机无数据 ——
因此每条记录都带 `src`，`build_summary` 会输出 `by_source` 能力清单，
UI 据此决定显示真值还是「—」，**不要把缺失当成 0 来展示**。

设计要点：
- 增量缓存：JSONL 与 trace 均以 (mtime, size) 为指纹，只重解析变化的文件。
- 去重：同一 (session, messageId) 只计一次（流式写入可能重复落盘）。
- 纯只读：数据库以 mode=ro 打开，不产生任何写入。

指标口径（与 ccusage / splitrail 对齐）：
- 上行速率 = prompt_tokens ÷ 生成耗时      → 预填充吞吐（prefill tok/s）
- 下行速率 = completion_tokens ÷ 生成耗时  → 解码吞吐（decode tok/s）
- 缓存命中率 = prompt_cache_hit_tokens ÷ prompt_tokens
- 燃烧率 = tokens ÷ 活跃分钟（各会话首尾跨度之和）
- 上下文占用 = used ÷ 有效窗口（sessions.context_window，与界面一致）
             另给物理窗口口径 used ÷ session_usage.size，见 ctx_gauges()
"""

from __future__ import annotations

import heapq
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Tuple

import sources

# ---------------------------------------------------------------- 路径配置

WB_HOME = Path(os.environ.get("WORKBUDDY_HOME") or (Path.home() / ".workbuddy"))
DB_PATH = WB_HOME / "workbuddy.db"
PROJECTS_DIR = WB_HOME / "projects"
TRACES_DIR = WB_HOME / "traces"

_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_FILE = _CACHE_DIR / "usage-cache.json"

_CACHE_VERSION = 7        # v7: 记录增加 ttft / exact_ms / data_src（一手 + 回退）
_lock = Lock()

SCOPES = ("today", "7d", "30d", "all")

# 请求与 generation span 的配对容差（毫秒）
MATCH_TOLERANCE_MS = 8000
# 实时速率窗口（毫秒）
LIVE_WINDOW_MS = 120_000

# 自动来源：最近活动窗口 / 切源最短保持 / 前台加权
_AUTO_WINDOW_MS = 120_000
_AUTO_HOLD_MS = 20_000
_AUTO_FG_BOOST_MS = 30_000
_AUTO_STATE: Dict[str, Any] = {"src": "", "switch_ts": 0.0}


def _foreground_src() -> str:
    """
    前台进程 → 数据源。只做加权，不做唯一判据：
    Claude Code / Codex 常跑在终端里，进程名是 WindowsTerminal，无法区分。
    """
    try:
        import win32ui
        name = win32ui.foreground_process_name().lower()
    except Exception:
        return ""
    for key, src in (("qoder", "qoder"), ("workbuddy", "workbuddy"),
                     ("claude", "claude"), ("codex", "codex")):
        if key in name:
            return src
    return ""


def resolve_auto_source(records: List[Dict[str, Any]]) -> str:
    """
    自动来源 = 最近 120 秒内有活动的源里最新者。

    同一轮里不切来切去：当前源仍有活动且挑战者没明显领先时保持；
    但用户切到的前台应用不受防抖约束，新请求一到就立刻跟随；
    所有源都空闲时回落到 all（聚合视图）。
    """
    now = time.time() * 1000.0
    last: Dict[str, float] = {}
    for r in records:
        src = r.get("src") or ""
        ts = float(r.get("ts") or 0)
        if src and ts > last.get(src, 0):
            last[src] = ts

    cand = {s: t for s, t in last.items() if now - t <= _AUTO_WINDOW_MS}
    if not cand:
        _AUTO_STATE["src"] = ""
        return "all"

    fg = _foreground_src()

    def score(item: Tuple[str, float]) -> float:
        src, ts = item
        return ts + (_AUTO_FG_BOOST_MS if src == fg else 0.0)

    chosen = max(cand.items(), key=score)[0]
    prev = str(_AUTO_STATE.get("src") or "")
    # 防抖只拦"后台应用刚好冒了个请求"的情况。用户已经主动切到某个应用时
    # 必须立刻跟过去，否则切完还要等十几秒才有反应，正是之前的体感来源。
    if prev and prev in cand and chosen != prev and chosen != fg:
        if (now - float(_AUTO_STATE.get("switch_ts") or 0) < _AUTO_HOLD_MS
                and last[prev] >= now - 30_000):
            chosen = prev
        elif last[prev] >= now - 60_000 and last[chosen] <= last[prev] + 10_000:
            chosen = prev
    if chosen != prev:
        _AUTO_STATE["switch_ts"] = now
    _AUTO_STATE["src"] = chosen
    return chosen


# ---------------------------------------------------------------- 工具函数

def fmt_tokens(n: float) -> str:
    """把 token 数格式化为紧凑可读形式：1,234 / 12.3K / 673.8M / 1.02B"""
    n = float(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(n):,}"


def fmt_rate(n: float) -> str:
    """速率格式化：42.3 / 1.2K / 43.1K / 1.2M（单位 tokens/秒）"""
    return fmt_tokens(n)


def _local_date(ts_ms: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts_ms / 1000.0))


def _local_hour(ts_ms: float) -> str:
    return time.strftime("%Y-%m-%d %H:00", time.localtime(ts_ms / 1000.0))


def _midnight(offset_days: int = 0) -> float:
    t = time.localtime()
    base = datetime(t.tm_year, t.tm_mon, t.tm_mday) + timedelta(days=offset_days)
    return base.timestamp() * 1000.0


def scope_range(scope: str) -> Tuple[float, float, Optional[Tuple[float, float]]]:
    """
    返回 (start_ms, end_ms, prev_range)。
    prev_range 为上一等长周期，用于环比（scope=all 时无环比）。
    """
    now = time.time() * 1000.0
    if scope == "today":
        start = _midnight(0)
        prev = (start - 86_400_000.0, start - 86_400_000.0 + (now - start))
        return start, now, prev
    if scope == "7d":
        start = _midnight(-6)
        prev = (start - 7 * 86_400_000.0, start)
        return start, now, prev
    if scope == "30d":
        start = _midnight(-29)
        prev = (start - 30 * 86_400_000.0, start)
        return start, now, prev
    return 0.0, now, None


# ---------------------------------------------------------------- JSONL 解析

def _session_id_from_path(p: Path) -> str:
    """
    从文件路径推导会话 ID。
    projects/<proj>/<uuid>.jsonl                          -> <uuid>
    projects/<proj>/<uuid>/subagents/agent-xxx.jsonl       -> <uuid>
    """
    parts = p.parts
    if "subagents" in parts:
        i = parts.index("subagents")
        if i >= 1:
            return parts[i - 1]
    return p.stem


def _parse_file(p: Path) -> Dict[str, Any]:
    """
    单次扫描 JSONL，抽取三样东西：

    recs  : 逐请求用量记录
    calls : 工具调用 [callId, ts, sessionId]（模型结束输出、开始执行工具的时刻）
    rets  : 工具结果 [callId, ts]（工具执行完成的时刻）

    → 两者配对得到"工具执行耗时"，用于把工具等待从速率分母里剔除。
    """
    recs: List[Dict[str, Any]] = []
    calls: List[List[Any]] = []
    rets: List[List[Any]] = []
    seen: set = set()
    sid_default = _session_id_from_path(p)
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                has_usage = "rawUsage" in line
                has_call = '"function_call' in line
                if not has_usage and not has_call:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue

                sid = d.get("sessionId") or sid_default
                ts = d.get("timestamp") or 0
                try:
                    ts = float(ts)
                except Exception:
                    ts = 0.0

                typ = d.get("type")
                cid = d.get("callId")

                if typ == "function_call" and cid and ts > 0:
                    # 注意：function_call 记录**同时携带 rawUsage**，
                    # 记录工具起点后必须继续往下走，不能 continue
                    calls.append([cid, ts, sid])
                elif typ == "function_call_result" and cid and ts > 0:
                    rets.append([cid, ts])
                    continue

                if not has_usage:
                    continue
                pd = d.get("providerData") or {}
                ru = pd.get("rawUsage")
                if not isinstance(ru, dict):
                    continue

                mid = pd.get("messageId") or d.get("id")
                key = mid if mid is not None else (d.get("id"), ts)
                if key in seen:
                    continue
                seen.add(key)

                norm = pd.get("usage") or {}
                ptd = ru.get("prompt_tokens_details") or {}

                inp = ru.get("prompt_tokens", norm.get("inputTokens", 0)) or 0
                out = ru.get("completion_tokens", norm.get("outputTokens", 0)) or 0
                cached = (
                    ru.get("prompt_cache_hit_tokens")
                    or ptd.get("cached_tokens")
                    or 0
                )
                reasoning = (
                    ru.get("completion_thinking_tokens")
                    or (ru.get("completion_tokens_details") or {}).get("reasoning_tokens")
                    or 0
                )
                credit = ru.get("credit")
                if credit is None:
                    credit = norm.get("credit")
                try:
                    credit = float(credit or 0)
                except Exception:
                    credit = 0.0

                recs.append(
                    {
                        "ts": ts,
                        "sid": sid,
                        "model": pd.get("model") or pd.get("requestModelName") or "",
                        "in": int(inp),
                        "out": int(out),
                        "cached": int(cached),
                        "reasoning": int(reasoning),
                        "credit": credit,
                        "orig_credit": 0.0,   # WorkBuddy 不区分原价/折后
                        "ctx": 0.0,           # 上下文占用走 db 的 session_usage
                        "sub": 1 if pd.get("isSubAgent") else 0,
                        "dur": 0,      # 生成耗时（trace 对齐后回填）
                        "model_ms": 0, # 模型在跑的时间（扣除工具执行后）
                        "src": "workbuddy",
                        "rid": str(mid or ""),
                    }
                )
    except OSError:
        return {"recs": [], "calls": [], "rets": []}
    return {"recs": recs, "calls": calls, "rets": rets}


# ---------------------------------------------------------------- trace 解析

def _iso_ms(s: str) -> float:
    """解析 2026-10-12T15:30:14.696Z → 毫秒时间戳。"""
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return 0.0
    return dt.replace(tzinfo=timezone.utc).timestamp() * 1000.0


def _parse_trace_file(p: Path) -> Dict[str, Any]:
    """
    抽取 trace 的 sessionId 与 generation span 耗时。
    返回 {"sid": str, "spans": [[ended_ms, duration_ms], ...]}
    """
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            doc = json.load(fh)
    except Exception:
        return {"sid": "", "spans": []}
    trace = doc.get("trace") or {}
    sid = trace.get("sessionId") or ""
    spans = []
    for s in doc.get("spans") or []:
        if s.get("type") != "generation":
            continue
        dur = s.get("duration")
        end = s.get("endedAt")
        if not dur or not end:
            continue
        end_ms = _iso_ms(end)
        if end_ms <= 0:
            continue
        spans.append([end_ms, float(dur)])
    return {"sid": sid, "spans": spans}


# ---------------------------------------------------------------- 缓存

# 进程内缓存：usage-cache.json 已涨到 2.8MB，原来每轮 collect 都要从磁盘
# 重新读 + json.load 一遍（实测占稳态采集耗时的大头），而 server（1.2s TTL）
# 与桌宠（秒级轮询）都在高频调 collect。这里按 (mtime, size) 指纹复用解析结果，
# 只有文件真的变了（本进程回写、或另一进程写了新版）才重新读盘。
_MEM_CACHE: Dict[str, Any] = {"mtime": None, "size": None, "data": None}


def _load_cache() -> Dict[str, Any]:
    try:
        st = _CACHE_FILE.stat()
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        stamp = (None, None)
    mem = _MEM_CACHE
    if mem["data"] is not None and stamp == (mem["mtime"], mem["size"]):
        return mem["data"]
    try:
        with _CACHE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("version") != _CACHE_VERSION:
            data = {"files": {}, "traces": {}}
        else:
            data = {"files": data.get("files") or {},
                    "traces": data.get("traces") or {}}
    except Exception:
        data = {"files": {}, "traces": {}}
    mem["mtime"], mem["size"] = stamp
    mem["data"] = data
    return data


def _save_cache(files: Dict[str, Any], traces: Dict[str, Any]) -> None:
    # tmp 名带进程号：server 与桌宠两个进程可能同时回写，
    # 同名 tmp 会互相覆盖/占用导致 replace 失败。
    tmp = _CACHE_FILE.with_name(f"{_CACHE_FILE.stem}.{os.getpid()}.tmp")
    payload = {"version": _CACHE_VERSION, "files": files, "traces": traces}
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception:
        return
    # Windows 上目标文件正被另一进程读取时 os.replace 会抛 PermissionError，稍候重试
    saved = False
    for _ in range(3):
        try:
            os.replace(tmp, _CACHE_FILE)
            saved = True
            break
        except PermissionError:
            time.sleep(0.05)
        except OSError:
            break
    if saved:
        # 回写成功即刷新内存指纹，下一轮 collect 不必再读盘
        try:
            st = _CACHE_FILE.stat()
            _MEM_CACHE.update(mtime=st.st_mtime, size=st.st_size,
                              data={"files": files, "traces": traces})
        except OSError:
            pass
    else:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# build_summary 要展示"扫了多少个会话文件"。它原来自己再 glob 一遍整棵 projects
# 树（1376 个目录），和 collect_requests 里那次完全重复。实测这棵树热缓存下走一趟
# 要 0.15s、冷缓存 0.44s，按 5s 的采样间隔就是 3%~17% 的常驻 CPU，白白翻倍。
_last_file_count = 0


def collect_requests(force: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, List[Tuple[float, float]]]]:
    """
    扫描全部会话文件，返回 (逐请求记录, 会话 -> 工具执行区间列表)。

    工具区间形如 (start_ms, duration_ms)，用于从速率分母中剔除工具等待。
    """
    global _last_file_count
    with _lock:
        cache = {"files": {}, "traces": {}} if force else _load_cache()
        old_files = cache["files"]
        old_traces = cache["traces"]

        new_files: Dict[str, Any] = {}
        new_traces: Dict[str, Any] = {}
        records: List[Dict[str, Any]] = []
        all_calls: List[List[Any]] = []
        all_rets: List[List[Any]] = []
        dirty = force                   # 只有真的重新解析过文件才需要回写缓存

        paths = sorted(PROJECTS_DIR.glob("**/*.jsonl")) if PROJECTS_DIR.exists() else []
        _last_file_count = len(paths)
        if paths:
            for p in paths:
                try:
                    st = p.stat()
                except OSError:
                    continue
                key = str(p)
                hit = old_files.get(key)
                if hit and hit.get("mtime") == st.st_mtime and hit.get("size") == st.st_size:
                    item = {
                        "recs": hit.get("records") or [],
                        "calls": hit.get("calls") or [],
                        "rets": hit.get("rets") or [],
                    }
                else:
                    item = _parse_file(p)
                    dirty = True
                new_files[key] = {
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                    "records": item["recs"],
                    "calls": item["calls"],
                    "rets": item["rets"],
                }
                records.extend(item["recs"])
                all_calls.extend(item["calls"])
                all_rets.extend(item["rets"])

        spans_by_sid: Dict[str, List[Tuple[float, float]]] = {}
        if TRACES_DIR.exists():
            for p in sorted(TRACES_DIR.glob("**/trace_*.json")):
                try:
                    st = p.stat()
                except OSError:
                    continue
                key = str(p)
                hit = old_traces.get(key)
                if hit and hit.get("mtime") == st.st_mtime and hit.get("size") == st.st_size:
                    spans = hit.get("spans") or []
                    sid = hit.get("sid") or ""
                else:
                    parsed = _parse_trace_file(p)
                    spans = parsed["spans"]
                    sid = parsed["sid"]
                    dirty = True
                new_traces[key] = {
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                    "spans": spans,
                    "sid": sid,
                }
                if sid and spans:
                    spans_by_sid.setdefault(sid, []).extend(
                        (float(a), float(b)) for a, b in spans
                    )

        # ---- 其它应用：Qoder / Codex / Claude Code / CodeBuddy（见 sources.py）
        # 与 WorkBuddy 共用同一份 (mtime, size) 指纹缓存，键加了 "qoder-log:" 之类
        # 的前缀避免与 WorkBuddy 的绝对路径键撞车。
        extra = sources.collect(old_files)
        new_files.update(extra["files"])
        records.extend(extra["records"])
        all_calls.extend(extra["calls"])
        all_rets.extend(extra["rets"])
        dirty = dirty or extra["dirty"]

        # 缓存有 2.8MB，json.dump 一次实测 ~0.48s，而绝大多数采样周期里什么都没变。
        # 只在真的重新解析过文件、或文件集合有增减时才回写。
        if dirty or set(new_files) != set(old_files) or set(new_traces) != set(old_traces):
            _save_cache(new_files, new_traces)

    # ---- 工具执行区间：callId 配对（实测配对率 99.7%）
    ret_map = {c: ts for c, ts in all_rets}
    tools_by_sid: Dict[str, List[Tuple[float, float]]] = {}
    for cid, ts, sid in all_calls:
        end = ret_map.get(cid)
        if end is None:
            continue
        dur = end - ts
        if dur <= 0 or dur > 3_600_000:
            continue
        tools_by_sid.setdefault(sid, []).append((ts, dur))
    for lst in tools_by_sid.values():
        lst.sort(key=lambda x: x[0])

    # ---- trace 生成耗时对齐
    for sid, lst in spans_by_sid.items():
        lst.sort(key=lambda x: x[0])

    for r in records:
        lst = spans_by_sid.get(r["sid"])
        if not lst or not r["ts"]:
            continue
        lo, hi = 0, len(lst) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if lst[mid][0] < r["ts"]:
                lo = mid + 1
            else:
                hi = mid
        best = None
        for idx in (lo - 1, lo):
            if 0 <= idx < len(lst):
                d = abs(lst[idx][0] - r["ts"])
                if best is None or d < best[0]:
                    best = (d, lst[idx][1])
        if best and best[0] <= MATCH_TOLERANCE_MS:
            r["dur"] = best[1]

    # ---- 扣除工具执行时间，得到"模型在跑的时间"
    _attach_model_ms(records, tools_by_sid)

    return records, tools_by_sid


def _attach_model_ms(records: List[Dict[str, Any]],
                     tools_by_sid: Dict[str, List[Tuple[float, float]]]) -> None:
    """
    对同一会话按时间排序，逐请求计算：

        请求间隔 Δt = 本次结束 - 上次结束
        工具耗时 T_tool = 落在 (上次结束, 本次结束] 区间内的工具执行时长之和
        模型时间 model_ms = Δt - T_tool   （下界 50ms，防止分母为 0）

    这样速率的分母只包含"模型真正在工作"的时间，排除工具执行与等待。
    """
    by_sid: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        if r["ts"]:
            by_sid.setdefault(r["sid"], []).append(r)

    for sid, lst in by_sid.items():
        lst.sort(key=lambda x: x["ts"])
        spans = tools_by_sid.get(sid) or []
        si = 0
        n = len(spans)
        prev = None
        for r in lst:
            ts = r["ts"]
            if prev is None:
                prev = ts
                continue
            dt = ts - prev
            win_start = prev
            prev = ts
            if dt <= 0 or dt > MAX_GAP_MS:
                continue

            # 指针前移：跳过已经完全落在窗口左侧的区间
            while si < n and spans[si][0] + spans[si][1] <= win_start:
                si += 1
            tool = 0.0
            j = si
            while j < n and spans[j][0] < ts:
                s, d = spans[j]
                a = s if s > win_start else win_start
                b = s + d
                if b > ts:
                    b = ts
                if b > a:
                    tool += b - a
                j += 1

            model_ms = dt - tool
            if model_ms < 50:
                model_ms = 50
            r["model_ms"] = model_ms
            r["gap_ms"] = dt
            r["tool_ms"] = tool

    # 一手 rollout / cc-switch 自带真实耗时，优先于按间隔推算（首条记录没有 prev）
    for r in records:
        exact = float(r.get("exact_ms") or 0)
        if exact > 0:
            r["model_ms"] = max(50.0, exact)


# ---------------------------------------------------------------- 数据库读取

def read_db() -> Dict[str, Any]:
    """读取会话与实时上下文占用。"""
    out: Dict[str, Any] = {"sessions": {}, "usage": {}}
    if not DB_PATH.exists():
        return out
    try:
        conn = sqlite3.connect(
            f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=3.0
        )
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute(
                "SELECT id, title, custom_title, model, context_window, "
                "created_at, updated_at, last_activity_at, status, cwd "
                "FROM sessions WHERE deleted_at IS NULL"
            ):
                out["sessions"][r["id"]] = {
                    "id": r["id"],
                    "title": r["custom_title"] or r["title"] or "(未命名会话)",
                    "model": r["model"] or "",
                    "context_window": r["context_window"] or 0,
                    "created_at": r["created_at"] or 0,
                    "updated_at": r["updated_at"] or 0,
                    "last_activity_at": r["last_activity_at"] or 0,
                    "status": r["status"] or "",
                    "cwd": r["cwd"] or "",
                }
            for r in conn.execute(
                "SELECT session_id, used, size, updated_at, credit_json FROM session_usage"
            ):
                out["usage"][r["session_id"]] = {
                    "used": r["used"] or 0,
                    "size": r["size"] or 0,
                    "updated_at": r["updated_at"] or 0,
                    "credit_json": r["credit_json"] or "",
                }
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    return out


# ---------------------------------------------------------------- 上下文双口径

def ctx_gauges(used: int, model_size: int, ctx_window: int) -> Dict[str, Any]:
    """
    上下文占用的双口径换算。

    主口径 size / pct 取 sessions.context_window，即客户端认定的**有效上下文窗口**：
    WorkBuddy 界面的百分比和自动压缩的触发判定都以它为基准，实测为模型物理上限
    的 60%（600K vs 1M）。

    副口径 size_model / pct_model 取 session_usage.size，即模型**物理输入上限**
    （~/.workbuddy/models.json 的 maxInputTokens）。

    混用两者会产生固定倍数偏差（600K vs 1M → 1.67 倍），因此分开暴露、由前端
    决定展示方式。model_size 与主口径相同时置 0，前端据此决定是否显示副标。
    """
    size = ctx_window or model_size or 0
    extra = model_size if (model_size and model_size != size) else 0
    return {
        "size": size,
        "pct": (used / size) if size else 0.0,
        "size_model": extra,
        "pct_model": (used / extra) if extra else 0.0,
    }


def latest_ctx_ratio(recs: List[Dict[str, Any]]) -> float:
    """
    非 WorkBuddy 源的「当前上下文」：取**最近一条带比值**的请求的 ctx（0~1）。

    这类客户端（Qoder 等）把 `context_usage_ratio` 直接写进每次请求的 usage，
    它本身已经是**有效窗口口径**——客户端也做自动压缩，压缩后的请求会把比值
    写低。因此当前占用必须取时间上最后一条，而不是窗口内最大值：

    取 max 会一直挂着压缩前的峰值，表现为"压缩完读数不降"，与 WorkBuddy 源
    （读 session_usage.used，压缩即回落）语义不一致。

    只接受 > 0 的样本：老记录可能根本没有该字段（落库时补的 0.0），若最后一条
    恰好缺字段就被误判成"上下文为空"。整段都没有比值时返回 0，前端显示「—」。
    """
    latest = 0.0
    latest_ts = None
    for r in recs:
        v = r.get("ctx") or 0.0
        if v <= 0:
            continue
        ts = r.get("ts") or 0
        if latest_ts is None or ts >= latest_ts:
            latest_ts = ts
            latest = float(v)
    return latest


# ---------------------------------------------------------------- 速率计算

def _pct(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


# 请求间隔超过该值视为"用户离开"，不计入速率
MAX_GAP_MS = 900_000


def decode_stats(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    真实生成速率（tokens/秒），来自 trace 的 generation span 耗时。

    注意：WorkBuddy 的 trace 在会话/工作流结束后才落盘，
    所以指标是**历史基线**，不是实时读数。
    只有成功对齐到 span（dur > 0）的记录参与计算，覆盖率单独返回。
    """
    total = len(recs)
    matched = [r for r in recs if r["dur"] > 0]
    if not matched:
        return {
            "out_tps": 0.0, "in_tps": 0.0, "e2e_tps": 0.0,
            "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0,
            "samples": 0, "total": total, "coverage": 0.0,
        }
    secs = sum(r["dur"] for r in matched) / 1000.0
    out = sum(r["out"] for r in matched)
    inp = sum(r["in"] for r in matched)
    durs = sorted(r["dur"] for r in matched)
    return {
        "out_tps": (out / secs) if secs else 0.0,
        "in_tps": (inp / secs) if secs else 0.0,
        "e2e_tps": ((out + inp) / secs) if secs else 0.0,
        "avg_ms": (sum(durs) / len(durs)) if durs else 0.0,
        "p50_ms": _pct(durs, 0.5),
        "p95_ms": _pct(durs, 0.95),
        "samples": len(matched),
        "total": total,
        "coverage": (len(matched) / total) if total else 0.0,
    }


def live_stats(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    上下行速率（tokens/秒）。

    分母优先使用 **model_ms**（已扣除工具执行时间的"模型在跑时间"），
    缺失时回落到请求间隔。同时返回未扣工具的端到端口径 raw_*，便于对比。
    """
    eff_out = eff_in = raw_out = raw_in = 0
    eff_secs = raw_secs = 0.0
    tool_ms = 0.0
    n_eff = n_raw = 0
    gaps: List[float] = []

    for r in recs:
        m = r.get("model_ms") or 0
        g = r.get("gap_ms") or 0
        if m > 0:
            eff_out += r["out"]
            eff_in += r["in"]
            eff_secs += m / 1000.0
            n_eff += 1
        if g > 0:
            raw_out += r["out"]
            raw_in += r["in"]
            raw_secs += g / 1000.0
            tool_ms += r.get("tool_ms") or 0
            gaps.append(g)
            n_raw += 1

    gaps.sort()
    ts = sorted(float(r.get("ts") or 0) for r in recs if r.get("ts"))
    span_min = ((ts[-1] - ts[0]) / 60_000.0) if len(ts) > 1 else 0.0
    # 单请求时至少按 1 分钟算，否则 credits/min 会被放大成 60 倍
    rate_min = max(span_min, 1.0) if recs else 0.0
    credits = sum(float(r.get("credit") or 0) for r in recs)
    credit_reqs = sum(1 for r in recs if float(r.get("credit") or 0) > 0)
    return {
        # 主口径：扣除工具执行/等待后的有效速率
        "out_tps": (eff_out / eff_secs) if eff_secs else 0.0,
        "in_tps": (eff_in / eff_secs) if eff_secs else 0.0,
        "e2e_tps": ((eff_out + eff_in) / eff_secs) if eff_secs else 0.0,
        "samples": n_eff,
        # 参考口径：端到端（含工具执行）
        "raw_out_tps": (raw_out / raw_secs) if raw_secs else 0.0,
        "raw_in_tps": (raw_in / raw_secs) if raw_secs else 0.0,
        "raw_samples": n_raw,
        "median_gap_ms": _pct(gaps, 0.5),
        "p95_gap_ms": _pct(gaps, 0.95),
        # 工具执行时间占请求间隔的比例，用于说明"排除了多少"
        "tool_ratio": (tool_ms / (raw_secs * 1000.0)) if raw_secs else 0.0,
        "model_secs": eff_secs,
        # 无 token 的源用这两项：credits/分钟、请求/分钟（墙钟口径）
        "credits": credits,
        "credit_per_min": (credits / rate_min) if rate_min else 0.0,
        "credit_per_req": (credits / credit_reqs) if credit_reqs else 0.0,
        "req_per_min": (len(recs) / rate_min) if rate_min else 0.0,
        "requests": len(recs),
    }


def _totals(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    inp = sum(r["in"] for r in recs)
    out = sum(r["out"] for r in recs)
    cached = sum(r["cached"] for r in recs)
    return {
        "requests": len(recs),
        "sub_requests": sum(r["sub"] for r in recs),
        "input": inp,
        "output": out,
        "total": inp + out,
        "cached": cached,
        "reasoning": sum(r["reasoning"] for r in recs),
        "cache_rate": (cached / inp) if inp else 0.0,
        "credit": sum(r["credit"] for r in recs),
        "avg_input_per_req": (inp / len(recs)) if recs else 0,
        "avg_output_per_req": (out / len(recs)) if recs else 0,
    }


def _delta(cur: float, prev: float) -> Optional[float]:
    """环比变化率；prev 为 0 时返回 None（避免 +∞）。"""
    if prev <= 0:
        return None
    return (cur - prev) / prev


def _active_minutes(recs: List[Dict[str, Any]]) -> float:
    """各会话首尾跨度之和（分钟），用于燃烧率；不含会话之间的空档。"""
    span: Dict[str, List[float]] = {}
    for r in recs:
        if not r["ts"]:
            continue
        s = span.setdefault(r["sid"], [r["ts"], r["ts"]])
        if r["ts"] < s[0]:
            s[0] = r["ts"]
        if r["ts"] > s[1]:
            s[1] = r["ts"]
    mins = sum((b - a) for a, b in span.values()) / 60_000.0
    return max(mins, 1.0 / 60.0)


# ---------------------------------------------------------------- 首字延迟估算

def ttft_stats(recs: List[Dict[str, Any]],
               out_tps_by_model: Dict[str, float]) -> Dict[str, Any]:
    """
    首字延迟（TTFT）估算。

    WorkBuddy 的本地记录不含流式时间戳，无法直接测 TTFT。
    这里用可分解关系做估算：

        模型时间 ≈ TTFT + 输出 tokens ÷ 该模型解码速度

    模型时间优先取 trace 的 generation span 耗时（真实），
    无 trace 时退化为"请求间隔 − 工具执行时间"（含排队，属上界）。
    解码速度取基线表。结果一律标注为"估算"，不与实测口径混用。
    """
    # 一手 TTFT 优先（Codex task_complete / cc-switch first_token_ms），
    # 有实测就不再混入估算值，避免两者口径互相污染。
    real = [float(r["ttft"]) for r in recs if float(r.get("ttft") or 0) > 0]
    if real:
        real.sort()
        return {
            "ttft_ms": sum(real) / len(real),
            "p50_ms": _pct(real, 0.5),
            "p95_ms": _pct(real, 0.95),
            "samples": len(real),
            "estimated": False,
        }

    vals: List[float] = []
    for r in recs:
        ms = r.get("dur") or 0
        if ms <= 0:
            ms = r.get("model_ms") or 0
        if ms <= 0 or not r["out"]:
            continue
        rate = out_tps_by_model.get(r["model"]) or 0.0
        if rate <= 0:
            continue
        decode_ms = (r["out"] / rate) * 1000.0
        est = ms - decode_ms
        if est < 0:
            continue
        if est > ms:           # 理论上不会发生，防御性处理
            est = ms
        vals.append(est)
    if not vals:
        return {"ttft_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "samples": 0,
                "estimated": True}
    vals.sort()
    return {
        "ttft_ms": sum(vals) / len(vals),
        "p50_ms": _pct(vals, 0.5),
        "p95_ms": _pct(vals, 0.95),
        "samples": len(vals),
        "estimated": True,
    }


# ---------------------------------------------------------------- 本轮 / 上轮

def turn_compare(recs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """最近两次请求的速率对比（用于胶囊上的涨跌箭头）。"""
    # Qoder 的 out 恒为 0，但仍要保留 turn.ts，桌宠才能在一轮完成时播 OK 动画；
    # 速率涨跌只在双方都有输出时才算（delta=None），不会被读成 0 t/s 的假跌。
    tail = [r for r in recs if (r.get("model_ms") or 0) > 0]
    if len(tail) < 2:
        return None
    tail.sort(key=lambda x: x["ts"])
    last, prev = tail[-1], tail[-2]
    lm, pm = last["model_ms"], prev["model_ms"]

    def tps(r, ms):
        return (r["out"] / (ms / 1000.0)) if ms else 0.0

    lo, po = tps(last, lm), tps(prev, pm)
    delta = ((lo - po) / po) if po > 0 else None
    return {
        "last_out_tps": lo,
        "prev_out_tps": po,
        "last_ms": lm,
        "prev_ms": pm,
        "last_out": last["out"],
        "last_model": last["model"],
        "delta": delta,
        "ts": last["ts"],
    }


# ---------------------------------------------------------------- CSV 导出

CSV_HEADER = [
    "source", "session_id", "session_title", "model", "unit", "data_source",
    "request_ts", "request_time",
    "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
    "cache_hit_rate", "reasoning_tokens", "credit", "original_credit",
    "context_ratio", "gap_ms", "tool_ms",
    "model_ms", "gen_ms_trace", "ttft_ms", "out_tps_effective", "in_tps_effective",
    "is_subagent",
]


def _record_unit(r: Dict[str, Any]) -> str:
    if (r.get("in") or 0) + (r.get("out") or 0) > 0:
        return "tokens"
    if float(r.get("credit") or 0) > 0:
        return "credit"
    return "requests"


def export_csv(scope: str = "all", path: Optional[str] = None,
               source: str = "all") -> str:
    """把逐请求明细导出为 CSV，返回文件路径。"""
    import csv

    if source not in sources.SOURCE_IDS + ("all", "auto"):
        source = "all"
    records, _ = collect_requests()
    if source == "auto":
        source = resolve_auto_source(records)
    if source != "all" and source in sources.SOURCE_IDS:
        records = [r for r in records if r.get("src") == source]
    db = read_db()
    titles = {k: v.get("title") for k, v in db["sessions"].items()}
    start, _now, _prev = scope_range(scope)
    rows = [r for r in records if r["ts"] >= start] if start else records
    rows.sort(key=lambda x: x["ts"])

    out = Path(path) if path else (
        Path(__file__).resolve().parent / "exports" /
        f"usage-{source}-{scope}-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for r in rows:
            mm = r.get("model_ms") or 0
            gm = r.get("gap_ms") or 0
            w.writerow([
                r.get("src") or "workbuddy",
                r["sid"],
                titles.get(r["sid"], ""),
                r["model"],
                _record_unit(r),
                r.get("data_src") or "local",
                int(r["ts"]),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"] / 1000.0)) if r["ts"] else "",
                r["in"], r["out"], r["in"] + r["out"], r["cached"],
                f"{(r['cached'] / r['in']):.4f}" if r["in"] else "0",
                r["reasoning"], f"{r['credit']:.4f}",
                f"{float(r.get('orig_credit') or 0):.4f}",
                f"{float(r.get('ctx') or 0):.6f}",
                int(gm), int(r.get("tool_ms") or 0), int(mm),
                int(r.get("dur") or 0),
                int(r.get("ttft") or 0),
                f"{(r['out'] / (mm / 1000.0)):.2f}" if mm else "",
                f"{(r['in'] / (mm / 1000.0)):.2f}" if mm else "",
                1 if r["sub"] else 0,
            ])
    return str(out)


# ---------------------------------------------------------------- 聚合

def build_summary(scope: str = "all", with_sessions: bool = True,
                  source: str = "all") -> Dict[str, Any]:
    """
    生成聚合结果。

    scope  : today | 7d | 30d | all
    source : all | auto | workbuddy | qoder | codex | claude | codebuddy

    auto 会先解析成"最近 120 秒内有活动的最新源"，再按该源筛选；
    因此 totals / rates / by_model / by_day / by_hour / minute_series /
    sessions 全部派生统计都自动跟着走。`by_source` 始终基于全量。
    """
    if scope not in SCOPES:
        scope = "all"
    requested_source = source if (source == "auto" or source == "all"
                                 or source in sources.SOURCE_IDS) else "all"

    all_records, _tools = collect_requests()
    # 仪表盘始终显示一个"自动"页签，即使当前手动选了别的源，也要知道自动会落到哪
    auto_source = resolve_auto_source(all_records)
    source = auto_source if requested_source == "auto" else requested_source
    records = (all_records if source == "all"
               else [r for r in all_records if r.get("src") == source])
    db = read_db()
    sessions_meta: Dict[str, Any] = db["sessions"]
    usage_meta: Dict[str, Any] = db["usage"]
    # WorkBuddy 的会话标题/上下文窗口只存在于它自己的 db，筛到别的源时不能用
    wb_only = source in ("all", "workbuddy")

    for r in records:
        if not r["model"]:
            m = (sessions_meta.get(r["sid"], {}).get("model")
                 if r.get("src") == "workbuddy" else None)
            r["model"] = m or "unknown"

    start, now, prev_range = scope_range(scope)
    filtered = [r for r in records if r["ts"] >= start] if start else records

    totals = _totals(filtered)

    # ---- 环比
    prev_totals = None
    delta = None
    if prev_range:
        pstart, pend = prev_range
        prev_recs = [r for r in records if pstart <= r["ts"] < pend]
        prev_totals = _totals(prev_recs)
        prev_totals["rates"] = live_stats(prev_recs)
        prev_live = live_stats(filtered)
        delta = {
            "input": _delta(totals["input"], prev_totals["input"]),
            "output": _delta(totals["output"], prev_totals["output"]),
            "requests": _delta(totals["requests"], prev_totals["requests"]),
            "credit": _delta(totals["credit"], prev_totals["credit"]),
            "cache_rate": _delta(totals["cache_rate"], prev_totals["cache_rate"]),
            "out_tps": _delta(prev_live["out_tps"], prev_totals["rates"]["out_tps"]),
        }

    # ---- 速率：区间整体（端到端）+ 真实生成基线（trace）+ 实时窗口
    rates = live_stats(filtered)
    rates["burn_per_min"] = totals["total"] / _active_minutes(filtered)
    rates["decode"] = decode_stats(filtered)

    # 生成速率基线：与 scope 无关，取全量已对齐数据，作为"模型真实解码速度"参考
    baseline = decode_stats(records)
    bmodel: Dict[str, Dict[str, Any]] = {}
    for r in records:
        if r["dur"] <= 0:
            continue
        a = bmodel.setdefault(
            r["model"], {"model": r["model"], "out": 0, "inp": 0, "secs": 0.0,
                         "durs": [], "n": 0, "srcs": {}, "sub": 0}
        )
        a["out"] += r["out"]
        a["inp"] += r["in"]
        a["secs"] += r["dur"] / 1000.0
        a["durs"].append(r["dur"])
        a["n"] += 1
        src = r.get("src") or ""
        if src:
            a["srcs"][src] = a["srcs"].get(src, 0) + 1
        a["sub"] += 1 if r.get("sub") else 0
    baseline["by_model"] = [
        {
            "model": a["model"],
            "out_tps": (a["out"] / a["secs"]) if a["secs"] else 0.0,
            "in_tps": (a["inp"] / a["secs"]) if a["secs"] else 0.0,
            "avg_ms": (sum(a["durs"]) / a["n"]) if a["n"] else 0.0,
            "p95_ms": _pct(sorted(a["durs"]), 0.95),
            "samples": a["n"],
            # 同一个模型可能被多个 agent（WorkBuddy/Qoder/Codex/Claude/CodeBuddy）调用，
            # 按调用次数给出，UI 在模型名后标注来源。
            "srcs": [{"id": k, "n": v} for k, v in
                     sorted(a["srcs"].items(), key=lambda kv: (-kv[1], kv[0]))],
            "sub_samples": a["sub"],
        }
        for a in bmodel.values()
    ]
    baseline["by_model"].sort(key=lambda x: -x["samples"])

    recent_recs = [r for r in records if r["ts"] >= now - LIVE_WINDOW_MS]
    recent = live_stats(recent_recs)
    recent["window_ms"] = LIVE_WINDOW_MS
    # 没有 token 的源（Qoder）samples 恒为 0，但请求是真实发生的；
    # 用请求数判断"近期是否有活动"，否则它会被永远当成 stale。
    recent["stale"] = recent["requests"] < 1
    if recent["stale"]:
        # 无近期请求时回落到最近 30 条，标注 recent.fallback
        # nlargest 只需 O(n log 30)，避免对全部记录做全排序
        tail = heapq.nlargest(30, records, key=lambda x: x["ts"] or 0)
        fb = live_stats(tail)
        recent.update(
            {
                "out_tps": fb["out_tps"],
                "in_tps": fb["in_tps"],
                "e2e_tps": fb["e2e_tps"],
                "samples": fb["samples"],
                "median_gap_ms": fb["median_gap_ms"],
                "credit_per_min": fb["credit_per_min"],
                "credit_per_req": fb["credit_per_req"],
                "req_per_min": fb["req_per_min"],
                "requests": fb["requests"],
                "fallback": True,
            }
        )

    # ---- 会话级聚合
    sess_agg: Dict[str, Dict[str, Any]] = {}
    for r in filtered:
        a = sess_agg.setdefault(
            r["sid"],
            {"input": 0, "output": 0, "credit": 0.0, "requests": 0, "cached": 0,
             "reasoning": 0, "last_ts": 0, "first_ts": r["ts"] or 0,
             "model": r["model"], "recs": []},
        )
        a["input"] += r["in"]
        a["output"] += r["out"]
        a["cached"] += r["cached"]
        a["reasoning"] += r["reasoning"]
        a["credit"] += r["credit"]
        a["requests"] += 1
        a["recs"].append(r)
        if r["ts"] > a["last_ts"]:
            a["last_ts"] = r["ts"]
            a["model"] = r["model"]
        if r["ts"] and r["ts"] < a["first_ts"]:
            a["first_ts"] = r["ts"]

    # ---- 按模型
    by_model_map: Dict[str, Dict[str, Any]] = {}
    for r in filtered:
        a = by_model_map.setdefault(
            r["model"],
            {"model": r["model"], "requests": 0, "input": 0, "output": 0,
             "cached": 0, "credit": 0.0, "recs": [], "srcs": {}, "sub": 0},
        )
        a["requests"] += 1
        a["input"] += r["in"]
        a["output"] += r["out"]
        a["cached"] += r["cached"]
        a["credit"] += r["credit"]
        a["recs"].append(r)
        src = r.get("src") or ""
        if src:
            a["srcs"][src] = a["srcs"].get(src, 0) + 1
        a["sub"] += 1 if r.get("sub") else 0

    models = []
    for m in by_model_map.values():
        recs_m = m.pop("recs")
        m["srcs"] = [{"id": k, "n": v} for k, v in
                     sorted(m.pop("srcs").items(), key=lambda kv: (-kv[1], kv[0]))]
        m["sub_samples"] = m.pop("sub")
        dec = decode_stats(recs_m)
        lv = live_stats(recs_m)
        m["cache_rate"] = (m["cached"] / m["input"]) if m["input"] else 0.0
        m["share"] = (m["input"] / totals["input"]) if totals["input"] else 0.0
        m["out_tps"] = lv["out_tps"]        # 端到端上下行（实时口径）
        m["in_tps"] = lv["in_tps"]
        m["decode_tps"] = dec["out_tps"]    # 真实解码速度（trace 基线）
        m["decode_ms"] = dec["avg_ms"]
        m["coverage"] = dec["coverage"]
        models.append(m)
    models.sort(key=lambda x: -x["input"])

    # ---- 按天 / 按小时（含分模型堆叠）
    by_day: Dict[str, Dict[str, Any]] = {}
    by_hour: Dict[str, Dict[str, Any]] = {}
    for r in filtered:
        dk = _local_date(r["ts"])
        d = by_day.setdefault(
            dk, {"date": dk, "input": 0, "output": 0, "requests": 0,
                 "credit": 0.0, "models": {}}
        )
        d["input"] += r["in"]
        d["output"] += r["out"]
        d["requests"] += 1
        d["credit"] += r["credit"]
        mm = d["models"].setdefault(r["model"], {"input": 0, "output": 0})
        mm["input"] += r["in"]
        mm["output"] += r["out"]

        hk = _local_hour(r["ts"])
        h = by_hour.setdefault(hk, {"hour": hk, "input": 0, "output": 0,
                                    "requests": 0, "credit": 0.0, "models": {}})
        h["input"] += r["in"]
        h["output"] += r["out"]
        h["requests"] += 1
        h["credit"] += r["credit"]
        hm = h["models"].setdefault(r["model"], {"input": 0, "output": 0})
        hm["input"] += r["in"]
        hm["output"] += r["out"]

    days = sorted(by_day.values(), key=lambda x: x["date"])
    hours = sorted(by_hour.values(), key=lambda x: x["hour"])[-48:]

    # ---- 会话列表
    sessions: List[Dict[str, Any]] = []
    if with_sessions:
        for sid, meta in (sessions_meta if wb_only else {}).items():
            u = usage_meta.get(sid) or {}
            a = sess_agg.get(sid) or {}
            used = u.get("used") or 0
            g = ctx_gauges(used, u.get("size") or 0,
                           meta.get("context_window") or 0)
            recs_s = a.get("recs") or []
            dec = decode_stats(recs_s)
            lv = live_stats(recs_s)
            sessions.append(
                {
                    "id": sid,
                    "title": meta.get("title"),
                    "src": "workbuddy",
                    "model": a.get("model") or meta.get("model"),
                    "used": used,
                    "size": g["size"],
                    "pct": g["pct"],
                    "size_model": g["size_model"],
                    "pct_model": g["pct_model"],
                    "requests": a.get("requests", 0),
                    "input": a.get("input", 0),
                    "output": a.get("output", 0),
                    "credit": a.get("credit", 0.0),
                    "out_tps": lv["out_tps"],
                    "in_tps": lv["in_tps"],
                    "decode_tps": dec["out_tps"],
                    "avg_ms": dec["avg_ms"],
                    "coverage": dec["coverage"],
                    "duration_ms": max(0, (a.get("last_ts") or 0) - (a.get("first_ts") or 0)),
                    "updated_at": u.get("updated_at") or meta.get("updated_at") or 0,
                    "status": meta.get("status"),
                }
            )
        # 其它源（Qoder / Codex / Claude / CodeBuddy）没有会话元数据库，直接从记录聚合出行。
        # 上下文占用用客户端自己写的比值（Qoder 的 context_usage_ratio，0~1），拿不到
        # used/size 绝对值；取最近一条而非峰值，压缩后才会跟着回落。
        have = {s["id"] for s in sessions}
        for sid, a in sess_agg.items():
            if sid in have:
                continue
            recs_s = a.get("recs") or []
            dec = decode_stats(recs_s)
            lv = live_stats(recs_s)
            ctx = latest_ctx_ratio(recs_s)
            sessions.append(
                {
                    "id": sid,
                    "title": None,
                    "src": (recs_s[0].get("src") if recs_s else "") or "",
                    "model": a.get("model") or "",
                    "used": 0,
                    "size": 0,
                    "pct": ctx,
                    "size_model": 0,
                    "pct_model": 0.0,
                    "requests": a.get("requests", 0),
                    "input": a.get("input", 0),
                    "output": a.get("output", 0),
                    "credit": a.get("credit", 0.0),
                    "out_tps": lv["out_tps"],
                    "in_tps": lv["in_tps"],
                    "decode_tps": dec["out_tps"],
                    "avg_ms": dec["avg_ms"],
                    "coverage": dec["coverage"],
                    "duration_ms": max(0, (a.get("last_ts") or 0) - (a.get("first_ts") or 0)),
                    "updated_at": a.get("last_ts") or 0,
                    "status": "",
                }
            )
        sessions.sort(key=lambda s: -(s["updated_at"] or 0))

    # ---- 实时会话
    # all 模式不能再无条件取 WorkBuddy：那会让 Qoder/Codex 正在跑时，
    # 面板仍停在 WorkBuddy 的旧会话，看起来像"没反应"。
    live: Optional[Dict[str, Any]] = None
    wb_sid, wb_ts = "", -1.0
    if source in ("all", "workbuddy") and usage_meta:
        sid_u = max(usage_meta, key=lambda k: usage_meta[k]["updated_at"])
        ts_u = float(usage_meta[sid_u].get("updated_at") or 0)
        meta_u = sessions_meta.get(sid_u) or {}
        # 上下文占用可能在请求结束后还更新一会儿；超过 15 分钟又没有 working
        # 状态就不再拿它当"实时"，改看记录里的最新会话。
        if (source == "workbuddy" or now - ts_u <= 15 * 60_000
                or meta_u.get("status") == "working"):
            wb_sid, wb_ts = sid_u, ts_u

    sess_sid, sess_ts = "", -1.0
    if sess_agg:
        sess_sid = max(sess_agg, key=lambda k: sess_agg[k].get("last_ts") or 0)
        sess_ts = float(sess_agg[sess_sid].get("last_ts") or 0)

    if wb_sid and wb_ts >= sess_ts:
        sid_live = wb_sid
        u = usage_meta[sid_live]
        meta = sessions_meta.get(sid_live) or {}
        a = sess_agg.get(sid_live) or {}
        g = ctx_gauges(u["used"], u["size"],
                       meta.get("context_window") or 0)
        # all 模式下 recent 混着别的源，WorkBuddy 的实时速率要单独算
        recs_s = a.get("recs") or []
        recs_rate = ([r for r in recent_recs if r.get("src") == "workbuddy"]
                     or recs_s)
        lv_recent = live_stats(recs_rate)
        live = {
            "session_id": sid_live,
            "title": meta.get("title"),
            "src": "workbuddy",
            "model": a.get("model") or meta.get("model") or "",
            "used": u["used"],
            "size": g["size"],
            "pct": g["pct"],
            "size_model": g["size_model"],
            "pct_model": g["pct_model"],
            "pct_source": "used",
            "session_input": a.get("input", 0),
            "session_output": a.get("output", 0),
            "session_credit": a.get("credit", 0.0),
            "requests": a.get("requests", 0),
            "out_tps": lv_recent["out_tps"],
            "in_tps": lv_recent["in_tps"],
            "credit_per_min": lv_recent.get("credit_per_min", 0.0),
            "credit_per_req": lv_recent.get("credit_per_req", 0.0),
            "req_per_min": lv_recent.get("req_per_min", 0.0),
            "recent_requests": lv_recent.get("requests", 0),
            "recent_samples": lv_recent.get("samples", 0),
            "unit": ("tokens"
                     if (a.get("input") or 0) + (a.get("output") or 0) > 0
                     else "credits" if (a.get("credit") or 0) > 0
                     else "requests"),
            "median_gap_ms": lv_recent.get("median_gap_ms", 0),
            "stale": lv_recent.get("requests", 0) < 1,
            "fallback": recent.get("fallback", False),
            "updated_at": u["updated_at"],
            "status": meta.get("status"),
        }
    elif sess_sid:
        # 非 WorkBuddy 源没有 session_usage 表，实时上下文占用改用记录里的
        # context_usage_ratio（Qoder 直接给 0~1 的比例）。used/size 拿不到就留 0，
        # UI 靠 pct_source 知道该只显示百分比、不要显示"x / y tokens"。
        # 同样取最近一条：客户端自动压缩后比值回落，不能用窗口峰值。
        a = sess_agg[sess_sid]
        recs_s = a.get("recs") or []
        ctx = latest_ctx_ratio(recs_s)
        live_src = (recs_s[0].get("src") if recs_s else "") or ""
        # 速率只取当前会话/当前源，别把 all 下其它应用的流量算进这一条读数
        recs_rate = [r for r in recent_recs if r.get("src") == live_src] or recs_s
        lv = live_stats(recs_rate)
        live = {
            "session_id": sess_sid,
            "title": None,
            "src": live_src,
            "model": a.get("model") or "",
            "used": 0,
            "size": 0,
            "pct": ctx,
            "pct_source": "ratio",
            "size_model": 0,
            "pct_model": 0.0,
            "session_input": a.get("input", 0),
            "session_output": a.get("output", 0),
            "session_credit": a.get("credit", 0.0),
            "requests": a.get("requests", 0),
            "out_tps": lv["out_tps"],
            "in_tps": lv["in_tps"],
            "credit_per_min": lv.get("credit_per_min", 0.0),
            "credit_per_req": lv.get("credit_per_req", 0.0),
            "req_per_min": lv.get("req_per_min", 0.0),
            "recent_requests": lv.get("requests", 0),
            "recent_samples": lv.get("samples", 0),
            "unit": ("tokens"
                     if (a.get("input") or 0) + (a.get("output") or 0) > 0
                     else "credits" if (a.get("credit") or 0) > 0
                     else "requests"),
            "median_gap_ms": lv.get("median_gap_ms", 0),
            "stale": lv.get("requests", 0) < 1,
            "fallback": False,
            "updated_at": a.get("last_ts") or 0,
            "status": "",
        }

    # ---- 首字延迟估算 / 本轮对比 / 正在运行的会话
    rate_map = {b["model"]: b["out_tps"] for b in baseline["by_model"]}
    ttft = ttft_stats(filtered, rate_map)
    if live is not None:
        # 首字延迟跟着实时会话走：全部来源下 Qoder 在跑时不能拿 Codex 的 TTFT
        # 冒充，反过来也一样。
        live_src = live.get("src") or ""
        recs_t = (filtered if live_src in ("", "all")
                  else [r for r in filtered if r.get("src") == live_src])
        lt = ttft_stats(recs_t, rate_map)
        live["ttft"] = lt["ttft_ms"]
        live["ttft_est"] = lt["estimated"]
    turn = turn_compare(recent_recs if recent_recs else records)

    running: List[Dict[str, Any]] = []
    for sid, a in sess_agg.items():
        meta = sessions_meta.get(sid) or {}
        u = usage_meta.get(sid) or {}
        last = a.get("last_ts") or 0
        if meta.get("status") != "working" and (now - last) > 900_000:
            continue
        lv = live_stats(a.get("recs") or [])
        g = ctx_gauges(u.get("used") or 0, u.get("size") or 0,
                       meta.get("context_window") or 0)
        running.append(
            {
                "id": sid,
                "title": meta.get("title") or "(未命名会话)",
                "model": a.get("model") or meta.get("model") or "",
                "out_tps": lv["out_tps"],
                "in_tps": lv["in_tps"],
                "pct": g["pct"],
                "pct_model": g["pct_model"],
                "requests": a.get("requests", 0),
                "last_ts": last,
                "status": meta.get("status") or "",
            }
        )
    running.sort(key=lambda x: -x["last_ts"])

    # ---- 今日（与 scope 无关）
    today_start = _midnight(0)
    today_recs = [r for r in records if r["ts"] >= today_start]
    today = _totals(today_recs)
    today["rates"] = live_stats(today_recs)
    today["decode"] = decode_stats(today_recs)

    # ---- 最近 60 分钟逐分钟吞吐（sparkline 用；tokens/60 = 该分钟平均 tok/s）
    now_ms = time.time() * 1000.0
    buckets: Dict[int, Dict[str, Any]] = {}
    for i in range(60):
        m = int((now_ms - (59 - i) * 60_000) // 60_000)
        buckets[m] = {"t": m * 60_000, "input": 0, "output": 0, "requests": 0}
    for r in records:
        if not r["ts"]:
            continue
        m = int(r["ts"] // 60_000)
        b = buckets.get(m)
        if b is None:
            continue
        b["input"] += r["in"]
        b["output"] += r["out"]
        b["requests"] += 1
    minute_series = []
    for b in buckets.values():
        minute_series.append(
            {
                "t": b["t"],
                "input": b["input"],
                "output": b["output"],
                "requests": b["requests"],
                "out_tps": b["output"] / 60.0,
                "in_tps": b["input"] / 60.0,
            }
        )

    return {
        "generatedAt": int(time.time() * 1000),
        "scope": scope,
        "totals": totals,
        "delta": delta,
        "prev": prev_totals,
        "rates": rates,
        "baseline": baseline,
        "recent": recent,
        "ttft": ttft,
        "turn": turn,
        "running": running[:8],
        "minute_series": minute_series,
        "today": today,
        "live": live,
        "by_model": models,
        "by_day": days,
        "by_hour": hours,
        "sessions": sessions[:120],
        "counts": {
            # files / sessions 是 WorkBuddy 侧的扫描诊断量，筛到别的源时给 0 或
            # 改用记录聚合出的会话数，不要把 WorkBuddy 的数字冒充成全局的。
            "files": _last_file_count if wb_only else 0,
            "sessions": len(sessions_meta) if wb_only else len(sess_agg),
            "traced": len({r["sid"] for r in records if r["dur"] > 0}),
        },
        "source": source,
        "requested_source": requested_source,
        "auto_source": auto_source,
        # 当前筛选结果里哪些单位真正有数据：UI 据此决定显示 t/s、cr/min 还是 req/min
        "units": {
            "tokens": bool(totals["input"] or totals["output"]),
            "credits": totals["credit"] > 0,
            "requests": totals["requests"] > 0,
            "primary": ("tokens" if (totals["input"] or totals["output"])
                        else "credits" if totals["credit"] > 0
                        else "requests" if totals["requests"] else ""),
        },
        # 始终基于全量记录，供筛选器显示各源计数与"为什么没数据"
        "by_source": sources.probe(all_records),
    }


def one_line(scope: str = "today") -> str:
    """单行紧凑文本，可用于命令行 / 状态栏。"""
    s = build_summary(scope=scope, with_sessions=False)
    t = s["totals"]
    live = s.get("live") or {}
    r = s.get("rates") or {}
    dec = s.get("baseline") or {}
    model = live.get("model") or "—"
    pct = live.get("pct") or 0
    pct_m = live.get("pct_model") or 0
    # 只在双口径确实不同时才打副标，避免老会话多出一段噪声
    ctx_s = f"| ctx {pct * 100:.1f}%"
    if pct_m:
        ctx_s += f" (物理 {pct_m * 100:.1f}%)"
    return (
        f"{model} | ↓{fmt_rate(r.get('out_tps', 0))}t/s ↑{fmt_rate(r.get('in_tps', 0))}t/s "
        f"| 基线 ↓{fmt_rate(dec.get('out_tps', 0))}t/s "
        f"{ctx_s} | ↑{fmt_tokens(t['input'])} ↓{fmt_tokens(t['output'])} "
        f"| {t['requests']} req | {t['credit']:.2f} credit"
    )


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(one_line(sys.argv[1] if len(sys.argv) > 1 else "today"))
