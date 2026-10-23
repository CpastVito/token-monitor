#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地 token-monitor 仪表盘打成可公开托管的静态快照。

用法:
    python publish_snapshot.py            # 从 127.0.0.1:8973 抓取并生成 dist/

产物:
    dist/index.html      dashboard + fetch 快照垫片
    dist/snapshot/*.json 预先抓取的接口响应
    dist/logos/*         品牌图标
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_HTML = ROOT / "dashboard.html"
DIST = ROOT / "dist"
SNAP_DIR = DIST / "snapshot"
LOGOS_SRC = ROOT / "assets" / "logos"
LOGOS_DST = DIST / "logos"

PORT_FILE = ROOT / ".cache" / "server-port"
BASES = [f"http://127.0.0.1:{p}" for p in ("8973", "8971", "8972")]

SCOPES = ["today", "7d", "30d", "all"]
SOURCES = ["all", "auto", "workbuddy", "qoder", "codex", "claude", "codebuddy"]


def find_base() -> str:
    if PORT_FILE.exists():
        port = PORT_FILE.read_text(encoding="utf-8").strip()
        if port:
            cand = f"http://127.0.0.1:{port}"
            if cand not in BASES:
                BASES.insert(0, cand)
    for b in BASES:
        try:
            with urllib.request.urlopen(b + "/api/config", timeout=3) as r:
                if r.status == 200:
                    return b
        except Exception:
            continue
    sys.exit("找不到运行中的 server.py，请先启动服务再打包快照。")


def get_json(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- 脱敏
def sanitize_config(data: dict) -> dict:
    cfg = dict(data.get("config") or {})
    cfg.pop("pos", None)
    if isinstance(cfg.get("providers"), dict):
        cfg["providers"] = {"max_shown": 0, "refresh_sec": 0, "items": []}
    return {"ok": True, "config": cfg}


def sanitize_ccswitch(_data: dict) -> dict:
    # 含 base_url / user_id / key 指纹，公开快照一律不保留
    return {"ok": True, "items": []}


def sanitize_balance_all(_data: dict) -> dict:
    return {"ok": True, "results": []}


# ---------------------------------------------------------------- 抓取
def collect(base: str) -> dict[str, str]:
    """返回 { 接口路径+查询串 : 快照文件名 }"""
    manifest: dict[str, str] = {}
    SNAP_DIR.mkdir(parents=True, exist_ok=True)

    def dump(key: str, obj) -> None:
        name = "api-%02d.json" % (len(manifest) + 1)
        (SNAP_DIR / name).write_text(
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        manifest[key] = name

    for scope in SCOPES:
        for source in SOURCES:
            key = f"/api/summary?scope={scope}&source={urllib.parse.quote(source)}"
            try:
                dump(key, get_json(base, key))
                print(f"  ok  {key}")
            except Exception as e:  # 单个组合失败不影响整体
                print(f"  ERR {key}: {e}")

    dump("/api/config", sanitize_config(get_json(base, "/api/config")))
    dump("/api/ccswitch", sanitize_ccswitch(get_json(base, "/api/ccswitch")))
    dump("/api/balance/all?all=1", sanitize_balance_all(get_json(base, "/api/balance/all?all=1")))
    dump("/api/balance/all", sanitize_balance_all({"ok": True, "results": []}))
    return manifest


# ---------------------------------------------------------------- 垫片
SHIM = r"""
<style>
  /* 快照标识：沿用页面既有色板，不引入新色 */
  #snapBadge{
    display:inline-flex;align-items:center;flex:none;
    background:#FFF4E6;color:#B26A16;border:1px solid #F3DCC0;
    font-size:11.5px;font-weight:600;line-height:1;letter-spacing:0;
    padding:5px 9px;border-radius:999px;white-space:nowrap;
    font-variant-numeric:tabular-nums;
  }
  #snapFoot{margin-top:6px;color:#B26A16}
  @media (max-width:840px){ #snapBadge{display:none} }
</style>
<script>
/* 静态快照垫片：把 /api/* 请求重定向到 snapshot/*.json。
   数据在打包时抓取，页面不再联网取数，余额与账号信息已脱敏移除。 */
(function () {
  var SNAP = window.__SNAP__ || { map: {}, ts: 0 };
  var origin = window.fetch ? window.fetch.bind(window) : null;
  var snapBase = SNAP.base || "snapshot/";

  function pathKey(input) {
    var raw = typeof input === "string" ? input : (input && input.url) || "";
    try {
      var u = new URL(raw, location.href);
      return u.pathname + (u.search || "");
    } catch (e) {
      return raw;
    }
  }

  function json(obj, status) {
    return new Response(JSON.stringify(obj), {
      status: status || 200,
      headers: { "Content-Type": "application/json; charset=utf-8" }
    });
  }

  function shell(p) {
    if (p.indexOf("/api/balance/all") === 0) return { ok: true, results: [] };
    if (p.indexOf("/api/balance") === 0) return { ok: false, error: "快照版未包含余额数据" };
    if (p.indexOf("/api/ccswitch") === 0) return { ok: true, items: [] };
    if (p.indexOf("/api/config") === 0) return { ok: true, config: {} };
    return { ok: false, error: "快照版无此接口数据" };
  }

  window.fetch = function (input, init) {
    var k = pathKey(input);
    var i = k.indexOf("/api/");
    if (i < 0) return origin.apply(null, arguments);
    var p = k.slice(i);
    var file = SNAP.map[p];
    if (file) {
      return origin(snapBase + file, { cache: "force-cache" }).then(function (r) {
        if (!r.ok) throw new Error("snapshot " + r.status);
        return r;
      });
    }
    if (init && init.method && String(init.method).toUpperCase() !== "GET") {
      return Promise.resolve(json({ ok: false, error: "快照版为只读，设置不可保存" }));
    }
    return Promise.resolve(json(shell(p)));
  };

  /* 导出 CSV 在静态托管下没有后端，改为提示 */
  document.addEventListener("DOMContentLoaded", function () {
    var exp = document.getElementById("export");
    if (exp) {
      var clone = exp.cloneNode(true);
      exp.parentNode.replaceChild(clone, exp);
      clone.title = "快照版不支持导出";
      clone.style.opacity = ".45";
      clone.addEventListener("click", function (e) {
        e.preventDefault();
        var t = document.createElement("div");
        t.textContent = "快照版不支持导出 CSV";
        t.style.cssText = "position:fixed;left:50%;bottom:28px;transform:translateX(-50%);"
          + "background:#131418;color:#fff;font-size:13px;padding:8px 14px;border-radius:999px;"
          + "z-index:9999;opacity:0;transition:opacity .18s";
        document.body.appendChild(t);
        requestAnimationFrame(function () { t.style.opacity = "1"; });
        setTimeout(function () {
          t.style.opacity = "0";
          setTimeout(function () { t.remove(); }, 220);
        }, 1600);
      });
    }

    var d = new Date(SNAP.ts || Date.now());
    var pad = function (n) { return String(n).padStart(2, "0"); };
    var stamp = pad(d.getMonth() + 1) + "-" + pad(d.getDate())
      + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());

    /* 快照角标：明确标示这是冻结数据。窄屏与原版 .meta 同步隐藏，避免挤坏页头 */
    var meta = document.getElementById("updated");
    if (meta && !document.getElementById("snapBadge")) {
      var b = document.createElement("span");
      b.id = "snapBadge";
      b.textContent = "静态快照 · " + stamp;
      b.title = "数据为打包瞬间的冻结状态，不会自动更新";
      meta.parentNode.insertBefore(b, meta);
    }

    /* 页脚补一句口径说明，窄屏也能看到 */
    var foot = document.querySelector(".foot");
    if (foot && !document.getElementById("snapFoot")) {
      var p = document.createElement("div");
      p.id = "snapFoot";
      p.textContent = "本页是静态快照：数据截至 " + stamp + "，不会自动更新；"
        + "各服务商余额与账号信息已移除。";
      foot.appendChild(p);
    }
  });
})();
</script>
"""


def build_html(manifest: dict[str, str], ts_ms: int) -> str:
    html = SRC_HTML.read_text(encoding="utf-8")
    payload = {
        "base": "snapshot/",
        "ts": ts_ms,
        "map": manifest,
    }
    inject = (
        "<script>window.__SNAP__="
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + ";</script>\n"
        + SHIM
    )
    marker = '<meta name="viewport"'
    idx = html.index(marker)
    return html[:idx] + inject + html[idx:]


def main() -> None:
    base = find_base()
    print(f"服务地址: {base}")
    ts_ms = int(time.time() * 1000)

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    print("抓取接口数据…")
    manifest = collect(base)
    print(f"共 {len(manifest)} 个快照文件")

    (DIST / "index.html").write_text(build_html(manifest, ts_ms), encoding="utf-8")

    if LOGOS_SRC.exists():
        shutil.copytree(LOGOS_SRC, LOGOS_DST, dirs_exist_ok=True)
        print(f"复制品牌图标 {len(list(LOGOS_DST.iterdir()))} 个")

    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"产出: {DIST}  合计 {total / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    main()
