#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy Token 监测 - 本地服务
===============================

仅监听 127.0.0.1，不对外暴露。

路由：
    GET /                      仪表盘页面
    GET /api/summary?scope=     聚合数据（today | 7d | 30d | all）
    GET /api/live               轻量实时数据（悬浮胶囊轮询用）
    GET /api/health             健康检查

用法：
    python server.py [--port 8971] [--no-browser]
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import collector
import prefs
import providers

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD = BASE_DIR / "dashboard.html"

# POST 必须带这个头，否则一律 403（自定义头会触发浏览器预检，从而挡掉跨站请求）
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "wb-token-monitor"


# ---------------------------------------------------------------- 配置读写

def _mask_cfg(cfg: dict) -> dict:
    """返回给前端时隐藏明文 Key，只告知是否已设置。"""
    out = json.loads(json.dumps(cfg))
    pv = out.get("providers") or {}
    for it in pv.get("items") or []:
        key = (it.get("api_key") or "").strip()
        it["api_key_set"] = bool(key)
        it["api_key"] = ""
        tok = (it.get("access_token") or "").strip()
        it["access_token_set"] = bool(tok)
        it["access_token"] = ""
    return out


def _deep_merge(base: dict, patch: dict) -> dict:
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _merge_provider_keys(old_cfg: dict, patch: dict) -> dict:
    """patch 里 api_key 留空表示"不修改"，沿用旧值。"""
    old_items = {i.get("id"): i for i in
                 ((old_cfg.get("providers") or {}).get("items") or [])}
    for it in ((patch.get("providers") or {}).get("items") or []):
        prev = old_items.get(it.get("id")) or {}
        if not (it.get("api_key") or "").strip():
            it["api_key"] = prev.get("api_key", "")
        if not (it.get("access_token") or "").strip():
            it["access_token"] = prev.get("access_token", "")
        it.pop("api_key_set", None)
        it.pop("access_token_set", None)
    return patch

# ---------------------------------------------------------------- 响应缓存
# 汇总结果按 scope 缓存，避免前端高频轮询引发重复全量扫描
_CACHE_TTL = 1.2
_BAL_CACHE = {}
_BAL_LOCK = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def get_summary(scope: str, source: str = "all") -> dict:
    now = time.time()
    key = (scope, source)
    # 持锁计算：TTL 到期瞬间桌宠轮询（2s）与仪表盘轮询（3s）可能同时 miss，
    # 不串行化就会并发做两遍全量聚合（惊群）。本地单用户场景下串行代价可忽略。
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
        data = collector.build_summary(scope, source=source)
        _cache[key] = (now, data)
        return data


# dashboard.html 约 75KB，原来每次 GET / 都重新读盘；按 (mtime, size) 指纹缓存
_DASH: dict = {"stamp": None, "data": b""}


def _dashboard_bytes() -> bytes:
    try:
        st = DASHBOARD.stat()
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        return b""
    if _DASH["stamp"] != stamp:
        _DASH["stamp"] = stamp
        _DASH["data"] = DASHBOARD.read_bytes()
    return _DASH["data"]


# 源参数直接进 build_summary 并参与缓存键，必须白名单校验：
# 既防注入，也防一个拼错的 source 把缓存撑成一堆永不命中的条目。
_VALID_SOURCES = ("all",) + tuple(collector.sources.SOURCE_IDS)


def _valid_source(v: str) -> str:
    return v if v in _VALID_SOURCES else "all"


class Handler(BaseHTTPRequestHandler):
    server_version = "WbTokenMonitor/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 日志：静音，避免刷屏
    def log_message(self, fmt, *args):  # noqa: D401
        return

    # ---------------- 工具
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: dict) -> None:
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ---------------- 安全边界
    # 本服务只给同源的 dashboard.html 用，因此：
    #   1) 不发 Access-Control-Allow-Origin —— 任意网页都读不到用量/配置；
    #   2) 校验 Host —— 挡 DNS rebinding（把外部域名解析到 127.0.0.1）；
    #   3) POST 要求自定义头 —— 强制浏览器预检，挡 CSRF。否则恶意页面可用
    #      text/plain 发简单请求改 provider 的 base_url，而 api_key 留空时
    #      服务端会自动回填真实 Key，下次余额轮询就把 Key 发往攻击者地址。
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _csrf_ok(self) -> bool:
        return self.headers.get(CSRF_HEADER) == CSRF_VALUE

    # ---------------- 路由
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if not self._host_ok():
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return

        try:
            if path in ("/", "/index.html", "/dashboard.html"):
                body = _dashboard_bytes()
                if not body:
                    self._send(500, b"dashboard.html missing",
                               "text/plain; charset=utf-8")
                    return
                self._send(200, body, "text/html; charset=utf-8")
                return

            if path == "/api/summary":
                scope = (qs.get("scope") or ["today"])[0]
                if scope not in collector.SCOPES:
                    scope = "today"
                self._json(get_summary(scope, _valid_source(
                    (qs.get("source") or ["all"])[0])))
                return

            if path == "/api/live":
                data = get_summary("today", _valid_source(
                    (qs.get("source") or ["all"])[0]))
                # 桌宠的数据面板直接消费这份响应，字段需覆盖：
                # 速率三口径（rates/recent/baseline.by_model）、TTFT、本轮对比、
                # 正在运行的会话、今日累计、实时上下文与各源计数。
                self._json(
                    {
                        "generatedAt": data["generatedAt"],
                        "source": data.get("source", "all"),
                        "by_source": data.get("by_source") or [],
                        "live": data["live"],
                        "today": data["today"],
                        "totals": data["totals"],
                        "rates": data.get("rates"),
                        "recent": data.get("recent"),
                        "delta": data.get("delta"),
                        "ttft": data.get("ttft"),
                        "turn": data.get("turn"),
                        "running": data.get("running") or [],
                        "baseline": {
                            "out_tps": (data.get("baseline") or {}).get("out_tps"),
                            "in_tps": (data.get("baseline") or {}).get("in_tps"),
                            "samples": (data.get("baseline") or {}).get("samples"),
                            "by_model": (data.get("baseline") or {}).get("by_model") or [],
                        },
                    }
                )
                return

            if path == "/api/export.csv":
                scope = (qs.get("scope") or ["today"])[0]
                if scope not in collector.SCOPES:
                    scope = "today"
                fp = collector.export_csv(
                    scope, source=_valid_source((qs.get("source") or ["all"])[0]))
                data = Path(fp).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{Path(fp).name}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            if path.startswith("/logos/"):
                name = path[len("/logos/"):]
                # 只放行扁平文件名：挡掉 ".." 与绝对路径/子路径（Windows 下
                # Path 拼接触到一个绝对路径片段会丢弃前缀，造成目录逃逸）
                if (not name or ".." in name or "/" in name or "\\" in name
                        or ":" in name):
                    self._send(404, b"no logo", "text/plain; charset=utf-8")
                    return
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if ext not in ("png", "json"):
                    self._send(404, b"no logo", "text/plain; charset=utf-8")
                    return
                fp = BASE_DIR / "assets" / "logos" / name
                if fp.is_file():
                    ctype = ("image/png" if ext == "png"
                             else "application/json; charset=utf-8")
                    self._send(200, fp.read_bytes(), ctype)
                else:
                    self._send(404, b"no logo", "text/plain; charset=utf-8")
                return

            if path == "/api/config":
                self._json({"ok": True, "config": _mask_cfg(prefs.load()),
                            "config_file": str(prefs.CONFIG_FILE)})
                return

            if path == "/api/balance":
                pid = (qs.get("provider") or [""])[0]
                cfg = prefs.load()
                items = (cfg.get("providers") or {}).get("items") or []
                item = next((i for i in items if i.get("id") == pid), None)
                if item is None:
                    self._json({"ok": False, "error": "未找到该服务商"})
                    return
                if not providers.eff_token(item):
                    self._json({"ok": False, "error": "该服务商未填写 API Key 或访问令牌",
                                "label": item.get("label")})
                    return
                self._json(providers.query(item, timeout=10))
                return

            if path == "/api/balance/all":
                force = (qs.get("force") or ["0"])[0] in ("1", "true")
                now = time.time()
                with _BAL_LOCK:
                    if not force and _BAL_CACHE.get("ts", 0) and now - _BAL_CACHE["ts"] < 120:
                        self._json(_BAL_CACHE["data"])
                        return
                include_disabled = (qs.get("all") or ["0"])[0] in ("1", "true")
                cfg = prefs.load()
                items = (cfg.get("providers") or {}).get("items") or []
                todo = [i for i in items
                        if providers.eff_token(i)
                        and (i.get("base_url") or "").strip()
                        and (include_disabled or i.get("enabled"))]
                res = providers.query_all(todo, timeout=9)
                payload = {"ok": True, "results": res, "total": len(todo),
                           "ts": int(now * 1000)}
                with _BAL_LOCK:
                    _BAL_CACHE["ts"] = now
                    _BAL_CACHE["data"] = payload
                self._json(payload)
                return

            if path == "/api/ccswitch":
                rows = providers.read_ccswitch()
                for r in rows:
                    k = r.get("api_key") or ""
                    r["api_key_set"] = bool(k)
                    r["key_tail"] = k[-4:] if len(k) >= 8 else ""
                    r["api_key"] = ""
                    t = r.get("access_token") or ""
                    r["access_token_set"] = bool(t)
                    r["token_tail"] = t[-4:] if len(t) >= 8 else ""
                    r["access_token"] = ""
                self._json({"ok": True, "items": rows,
                            "db_found": bool(rows)})
                return

            if path == "/api/health":
                self._json({"ok": True, "ts": int(time.time() * 1000)})
                return

            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # 保证服务不因单次异常退出
            self._send(500, f"error: {exc}".encode("utf-8"),
                       "text/plain; charset=utf-8")


    def _import_ccswitch(self):
        """把 cc-switch 中勾选的供应商导入为余额配置（带 Key）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            req = json.loads(body.decode("utf-8"))
        except Exception as exc:
            self._send(400, f"bad json: {exc}".encode(), "text/plain; charset=utf-8")
            return
        want = set(req.get("source_ids") or [])
        rows = {r["source_id"]: r for r in providers.read_ccswitch()}
        cfg = prefs.load()
        pv = cfg.setdefault("providers", {})
        items = pv.setdefault("items", [])
        existing = {(i.get("label"), (i.get("base_url") or "").rstrip("/")) for i in items}

        imported = 0
        for sid in want:
            r = rows.get(sid)
            if not r or not (r.get("api_key") or r.get("access_token")):
                continue
            tpl0 = r.get("template") or "custom"
            default_base0 = {
                "deepseek": "https://api.deepseek.com",
                "moonshot": "https://api.moonshot.cn",
                "openai": "https://api.openai.com",
                "openrouter": "https://openrouter.ai/api",
                "linoroute": "https://linoroute.com",
                "lmu": "https://api.lmuai.ai",
                "zhipu": "https://open.bigmodel.cn",
            }.get(tpl0, "")
            eff_base = (r["base_url"] or default_base0 or "").rstrip("/")
            pair = (r["label"], eff_base)
            if pair in existing:
                # 已存在同一条目：补齐缺失的 Key / 访问令牌 / 模板（常见于预置模板）
                for old in items:
                    if (old.get("label"), (old.get("base_url") or "").rstrip("/")) == pair:
                        changed = False
                        if not (old.get("api_key") or "").strip() and r.get("api_key"):
                            old["api_key"] = r["api_key"]
                            changed = True
                        if not (old.get("access_token") or "").strip() and r.get("access_token"):
                            old["access_token"] = r["access_token"]
                            changed = True
                        if not (old.get("user_id") or "").strip() and r.get("user_id"):
                            old["user_id"] = r["user_id"]
                            changed = True
                        old_tpl = old.get("template") or ""
                        # 通用模板（custom / newapi）升级为有专用提取器的模板
                        if (old_tpl in ("", "custom", "newapi", None)
                                and tpl0 not in ("", "custom", "newapi")):
                            old["template"] = tpl0
                            old["currency"] = providers.eff_currency({"template": tpl0})
                            changed = True
                        elif tpl0 in providers.TEMPLATES and not (old.get("currency") or "").strip():
                            old["currency"] = providers.eff_currency({"template": tpl0})
                            changed = True
                        if not (old.get("extract") or "").strip():
                            code = providers.eff_extract({"template": old.get("template") or tpl0})
                            if code:
                                old["extract"] = code
                                changed = True
                        if changed:
                            old["enabled"] = True
                            imported += 1
                        break
                continue
            base_id = "cc" + "".join(ch for ch in r["label"].lower() if ch.isalnum())[:12] or "cc"
            pid = base_id
            n = 2
            while any(i.get("id") == pid for i in items):
                pid = f"{base_id}{n}"
                n += 1
            tpl = tpl0
            default_base = default_base0
            items.append({
                "id": pid,
                "label": r["label"],
                "template": tpl,
                "base_url": eff_base,
                "path": "",
                "json_path": "",
                "api_key": r.get("api_key") or "",
                "access_token": r.get("access_token") or "",
                "auth": "bearer",
                "scale": "",
                "currency": providers.eff_currency({"template": tpl}),
                "user_id": r.get("user_id") or "",
                "extract": providers.eff_extract({"template": tpl}),
                "enabled": True,
            })
            existing.add(pair)
            imported += 1

        if imported:
            prefs.save(cfg)
        self._json({"ok": True, "imported": imported,
                    "config": _mask_cfg(prefs.load())})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if not self._host_ok():
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        if not self._csrf_ok():
            self._send(403, b"missing X-Requested-With header",
                       "text/plain; charset=utf-8")
            return
        if parsed.path == "/api/ccswitch":
            self._import_ccswitch()
            return
        if parsed.path != "/api/config":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            patch = json.loads(body.decode("utf-8"))
        except Exception as exc:
            self._send(400, f"bad json: {exc}".encode(), "text/plain; charset=utf-8")
            return
        try:
            cfg = prefs.load()
            patch = _merge_provider_keys(cfg, patch)
            cfg = _deep_merge(cfg, patch)
            cfg.pop("version", None)
            prefs.save(cfg)
            with _BAL_LOCK:                 # 配置变了，余额缓存必须失效
                _BAL_CACHE.clear()
            self._json({"ok": True, "config": _mask_cfg(prefs.load())})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)})


def _log(msg: str) -> None:
    """pythonw 下 sys.stdout 为 None，print 会抛异常，这里兜住。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


class Server(ThreadingHTTPServer):
    """
    必须关掉 allow_reuse_address。

    Windows 上 SO_REUSEADDR 的语义与 POSIX 不同：设了它之后，绑定一个**已被
    占用**的端口也会成功。原实现先用带 SO_REUSEADDR 的临时 socket 去探测端口，
    于是探测永远通过、总是返回首选端口；第二个实例也照样"绑定成功"，却收不到
    任何连接 —— 变成一个既不服务、也不退出的孤儿进程，每启动一次多一个。
    """

    allow_reuse_address = False
    daemon_threads = True


def _start_server(preferred: int):
    """真正尝试绑定；端口被占用则向后顺延。返回 (服务实例, 实际端口)。"""
    last_err = None
    for p in range(preferred, preferred + 20):
        try:
            return Server(("127.0.0.1", p), Handler), p
        except OSError as exc:
            last_err = exc
            continue
    raise RuntimeError(f"no free port near {preferred}: {last_err}")


def main() -> None:
    ap = argparse.ArgumentParser(description="WorkBuddy Token 监测服务")
    ap.add_argument("--port", type=int, default=prefs.DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()

    httpd, port = _start_server(args.port)
    url = f"http://127.0.0.1:{port}/"
    prefs.write_port(port)          # 桌宠与启动器据此发现真实端口

    _log(f"[wb-token-monitor] 服务已启动: {url}")
    _log(f"[wb-token-monitor] 数据源: {collector.WB_HOME}")

    # 预热：首屏不必等待
    threading.Thread(target=lambda: get_summary("today"), daemon=True).start()

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _log("\n[wb-token-monitor] 已停止")
    finally:
        prefs.PORT_FILE.unlink(missing_ok=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
