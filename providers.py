#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
余额查询（多服务商 · 参照 cc-switch 的供应商模型）
=================================================

每个服务商一条配置，字段与 cc-switch 的 provider 概念对齐：

    id / label      标识与显示名
    template        查询模板（决定默认路径与取值字段）
    base_url        接口根地址
    path            查询路径（可覆盖模板默认）
    api_key         密钥
    json_path       取值路径（可覆盖模板默认）
    scale           数值换算系数（new-api 的 quota 单位是 1/500000 美元）
    currency        货币符号
    enabled         是否在面板中显示（可同时多个）

模板（对应 cc-switch 的 usage_script.templateType）：

    deepseek   DeepSeek 官方      GET /user/balance
    moonshot   Moonshot / Kimi    GET /v1/users/me/balance
    openai     OpenAI 兼容        GET /v1/dashboard/billing/subscription
    newapi     New API 中转站     GET /api/user/self（quota / 500000）
    linoroute  linoroute          GET /api/user/self（web access token）
    lmu        LMU                GET /v1/usage
    zhipu      智谱 GLM           现金余额接口
    custom     任意接口           路径与 json_path 自己填

模板只是默认值；每个服务商可以再填一段 **提取器代码**（Python），对应
cc-switch 的 `usage_script.extractor`。代码里定义 `extract(response)`，
返回 cc-switch 同名字段即可，翻译脚本时不用改结构：

    {"amount": 12.3}                       # 或 remaining
    {"currency": "¥", "detail": "套餐"}    # 或 unit / extra / plan
    {"error": "令牌无效"}                  # 或 invalidMessage

提取器在受限命名空间里执行（无 import / open），适合本机自用配置。
"""

from __future__ import annotations

import json
import textwrap
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

UA = "wb-token-pet/1.0"

# 提取器只能用到这些内置函数，挡掉 import / open 等副作用
_SAFE_BUILTINS: Dict[str, Any] = {
    "abs": abs, "bool": bool, "dict": dict, "enumerate": enumerate,
    "float": float, "int": int, "isinstance": isinstance, "len": len,
    "list": list, "max": max, "min": min, "range": range, "round": round,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple,
}

_EXTRACT_LINOROUTE = """\
def extract(r):
    if not r.get("success") or not isinstance(r.get("data"), dict):
        return {"error": r.get("message") or "查询失败"}
    d = r["data"]
    group = str(d.get("group") or "")
    return {
        "amount": d.get("quota", 0) / 500000,
        "used": d.get("used_quota", 0) / 500000,
        "total": (d.get("quota", 0) + d.get("used_quota", 0)) / 500000,
        "currency": "$",
        "plan": "" if group in ("", "default") else group,
    }
"""

_EXTRACT_LMU = """\
_UNITS = {"USD": "$", "CNY": "¥", "CHY": "¥"}

def extract(r):
    if r.get("isValid") is False:
        return {"error": r.get("message") or "查询失败，请检查 API Key"}
    amount = r.get("balance")
    if amount is None:
        amount = r.get("remaining")
    if amount is None:
        return {"error": "未识别的余额字段"}
    frozen = r.get("frozen_balance") or 0
    return {
        "amount": amount,
        "currency": _UNITS.get(str(r.get("unit") or "").upper(), "$"),
        "plan": r.get("planName") or "灵眸账户",
        "detail": ("冻结 %.2f" % frozen) if frozen else "",
    }
"""

_EXTRACT_ZHIPU = """\
def extract(r):
    if r.get("code") != 200:
        return {"error": "查询失败：%s %s" % (r.get("code"), r.get("msg") or "")}
    d = r.get("data") or {}
    amount = d.get("balance")
    if amount is None:
        return {"error": "未识别的余额字段"}
    credit = {"NOT_OPEN": "信用未开通", "ENABLE": "信用已开通",
              "DISABLE": "信用已关闭"}.get(d.get("creditStatus"), "")
    return {
        "amount": amount,
        "currency": "¥",
        "detail": credit,
    }
"""

TEMPLATES: Dict[str, Dict[str, Any]] = {
    "deepseek": {"path": "/user/balance", "json_path": "balance_infos.0.total_balance",
                 "scale": 1.0, "currency": "¥"},
    "moonshot": {"path": "/v1/users/me/balance", "json_path": "data.available_balance",
                 "scale": 1.0, "currency": "¥"},
    "openai": {"path": "/v1/dashboard/billing/subscription",
               "json_path": "hard_limit_usd", "scale": 1.0, "currency": "$"},
    "newapi": {"path": "/api/user/self", "json_path": "data.quota",
               "scale": 1 / 500000.0, "currency": "¥"},
    # OpenRouter 的余额 = 总充值 − 已用
    "openrouter": {"path": "/api/v1/credits", "json_path": "data.total_credits",
                   "subtract": "data.total_usage", "scale": 1.0, "currency": "$"},
    # New API 站点：余额要用控制台的 access token 查，sk- Key 只用于推理
    "linoroute": {"path": "/api/user/self", "json_path": "data.quota",
                  "scale": 1 / 500000.0, "currency": "$",
                  "extract": _EXTRACT_LINOROUTE},
    "lmu": {"path": "/v1/usage", "json_path": "balance",
            "scale": 1.0, "currency": "$", "extract": _EXTRACT_LMU},
    "zhipu": {"path": "/api/biz/account/query-customer-account-report",
              "json_path": "data.balance", "scale": 1.0, "currency": "¥",
              "extract": _EXTRACT_ZHIPU},
    "custom": {"path": "", "json_path": "", "scale": 1.0, "currency": "¥"},
}

_KIND_MAP = {"deepseek": "deepseek", "openai-compatible": "openai", "custom": "custom"}


def template_of(item: Dict[str, Any]) -> str:
    tpl = (item.get("template") or "").strip()
    if tpl in TEMPLATES:
        return tpl
    return _KIND_MAP.get((item.get("kind") or "").lower(), "custom")


def eff_path(item: Dict[str, Any]) -> str:
    return (item.get("path") or "").strip() or TEMPLATES[template_of(item)]["path"]


def eff_json_path(item: Dict[str, Any]) -> str:
    return (item.get("json_path") or "").strip() or TEMPLATES[template_of(item)]["json_path"]


def eff_scale(item: Dict[str, Any]) -> float:
    try:
        s = item.get("scale")
        if s is None or s == "":
            return float(TEMPLATES[template_of(item)]["scale"])
        return float(s)
    except Exception:
        return 1.0


def eff_currency(item: Dict[str, Any]) -> str:
    return (item.get("currency") or TEMPLATES[template_of(item)]["currency"] or "¥")


def eff_extract(item: Dict[str, Any]) -> str:
    """服务商自己的提取器优先，否则用模板内置的。"""
    code = str(item.get("extract") or "").strip()
    if code:
        return code
    return str(TEMPLATES[template_of(item)].get("extract") or "")


def eff_token(item: Dict[str, Any]) -> str:
    """查询余额用的凭据：New API 站点的 web access token 优先于推理用的 sk- Key。"""
    return ((item.get("access_token") or "").strip()
            or (item.get("api_key") or "").strip())


def _unit_symbol(unit: Any, fallback: str) -> str:
    s = str(unit or "").strip()
    return {"USD": "$", "CNY": "¥", "CHY": "¥", "RMB": "¥"}.get(s.upper(),
                                                               s or fallback)


def _run_extract(code: str, response: Any) -> Dict[str, Any]:
    """在受限命名空间里执行提取器；契约与 cc-switch 的 extractor 对齐。"""
    ns: Dict[str, Any] = {"__builtins__": dict(_SAFE_BUILTINS)}
    exec(compile(textwrap.dedent(code), "<extractor>", "exec"), ns)
    fn = ns.get("extract")
    if not callable(fn):
        raise ValueError("提取器需定义 extract(response) 函数")
    res = fn(response)
    if not isinstance(res, dict):
        raise ValueError("提取器需返回 dict")
    return res


def _join(base: str, path: str) -> str:
    """拼接 base 与 path，避免 /api 之类的重复段（如 base=.../api + path=/api/v1/...）。"""
    b = (base or "").rstrip("/")
    p = path or ""
    if not p.startswith("/"):
        p = "/" + p
    if b.endswith("/api") and p.startswith("/api/"):
        p = p[4:]
    return b + p


def _http_get(url: str, headers: Dict[str, str], proxy: str = "",
              timeout: float = 8.0) -> Any:
    req = urllib.request.Request(url, headers=headers, method="GET")
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", "replace"))


def _dig(obj: Any, path: str) -> Any:
    """取嵌套字段，支持 a.b.0.c 与 a.b[0].c。"""
    cur = obj
    for part in (path or "").replace("]", "").split("."):
        if not part:
            continue
        key, _, idx = part.partition("[")
        if key:
            if isinstance(cur, list):
                try:
                    cur = cur[int(key)]
                except Exception:
                    return None
            elif isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
        if idx:
            try:
                cur = cur[int(idx)]
            except Exception:
                return None
        if cur is None:
            return None
    return cur


def _num(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def query(item: Dict[str, Any], timeout: float = 8.0) -> Dict[str, Any]:
    """返回 {ok, id, label, amount, text, currency, detail, error, ts, template}"""
    label = item.get("label") or item.get("id") or "未知"
    cur_sym = eff_currency(item)
    out: Dict[str, Any] = {
        "ok": False, "id": item.get("id"), "label": label, "amount": None,
        "text": "", "currency": cur_sym, "detail": "", "error": "",
        "ts": int(time.time() * 1000), "template": template_of(item),
    }

    base = (item.get("base_url") or "").rstrip("/")
    out["base_url"] = base          # 品牌图标识别用（label 猜不出时看域名）
    key = eff_token(item)
    if not base:
        out["error"] = "未配置接口地址"
        return out
    path = eff_path(item)
    if not path:
        out["error"] = "未配置查询路径"
        return out

    headers = {"User-Agent": UA, "Accept": "application/json"}
    auth_mode = (item.get("auth") or "bearer").lower()
    if key:
        if auth_mode in ("x-api-key", "xapikey", "anthropic"):
            # Claude/Anthropic 兼容代理用 x-api-key，且多数要求 anthropic-version
            headers["x-api-key"] = key
            headers["anthropic-version"] = item.get("anthropic_version") or "2023-06-01"
            headers["Authorization"] = f"Bearer {key}"
        elif auth_mode == "both":
            headers["Authorization"] = f"Bearer {key}"
            headers["x-api-key"] = key
        elif auth_mode == "raw":
            headers["Authorization"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"

    tpl = template_of(item)
    uid = (item.get("user_id") or "").strip()
    if uid:
        # New API 系的余额接口要求请求者 id 与令牌一致（linoroute 也走这条）
        headers["New-Api-User"] = uid

    try:
        data = _http_get(_join(base, path), headers, item.get("proxy") or "", timeout)
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code}"
        return out
    except urllib.error.URLError as e:
        out["error"] = f"网络错误：{getattr(e, 'reason', e)}"
        return out
    except json.JSONDecodeError:
        out["error"] = "返回不是合法 JSON"
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    code = eff_extract(item)
    if code:
        try:
            res = _run_extract(code, data)
        except Exception as e:
            out["error"] = f"提取器错误：{type(e).__name__}: {e}"
            return out
        if res.get("ok") is False or res.get("isValid") is False:
            out["error"] = str(res.get("error") or res.get("invalidMessage")
                               or "查询失败")
            return out
        amount = _num(res.get("amount", res.get("remaining")))
        if amount is None:
            out["error"] = str(res.get("error") or "提取器未返回金额")
            return out
        parts: List[str] = []
        for k in ("plan", "planName", "detail", "extra"):
            v = str(res.get(k) or "").strip()
            if v and v not in parts:
                parts.append(v)
        out.update(ok=True, amount=amount, text=f"{amount:.2f}",
                   currency=_unit_symbol(res.get("currency") or res.get("unit"),
                                         cur_sym),
                   detail=" · ".join(parts))
        return out

    jp = eff_json_path(item)
    val = _num(_dig(data, jp)) if jp else None
    if val is None:
        for alt in ("total_balance", "balance", "data.available_balance",
                    "data.balance", "data.quota", "hard_limit_usd",
                    "balance_infos.0.total_balance"):
            val = _num(_dig(data, alt))
            if val is not None:
                break

    if val is None:
        out["error"] = "未识别的余额字段"
        return out

    # 某些服务商需要两个字段相减（如 OpenRouter：总充值 − 已用）
    sub_path = (item.get("subtract") or TEMPLATES[tpl].get("subtract") or "")
    if sub_path:
        sub = _num(_dig(data, sub_path))
        if sub is not None:
            val = val - sub

    val *= eff_scale(item)
    if tpl == "deepseek":
        out["detail"] = "可用" if data.get("is_available") else "不可用"

    out.update(ok=True, amount=val, text=f"{val:.2f}")
    return out


def query_all(items: List[Dict[str, Any]], timeout: float = 8.0) -> List[Dict[str, Any]]:
    """并发查询多个服务商。"""
    import threading

    results: List[Optional[Dict[str, Any]]] = [None] * len(items)

    def work(i: int, it: Dict[str, Any]):
        try:
            results[i] = query(it, timeout=timeout)
        except Exception as e:
            results[i] = {"ok": False, "id": it.get("id"), "label": it.get("label"),
                          "error": str(e), "currency": eff_currency(it),
                          "base_url": it.get("base_url") or "",
                          "ts": int(time.time() * 1000)}

    threads = []
    for i, it in enumerate(items):
        t = threading.Thread(target=work, args=(i, it), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout + 2)
    return [r for r in results if r is not None]


def enabled_items(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """面板要显示的余额：enabled 且已填 Key 与地址；受 max_shown 限制。"""
    pv = cfg.get("providers") or {}
    items = pv.get("items") or []
    out = [i for i in items
           if i.get("enabled") and eff_token(i)
           and (i.get("base_url") or "").strip()]
    limit = int(pv.get("max_shown") or 3)
    return out[:max(1, limit)]


def active_item(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    got = enabled_items(cfg)
    return got[0] if got else None


# ---------------------------------------------------------------- cc-switch 导入

CC_DB = "~/.cc-switch/cc-switch.db"


def read_ccswitch(path: str = "") -> List[Dict[str, Any]]:
    """读取 cc-switch 的 providers，转成候选余额配置（含 Key）。"""
    import os
    import sqlite3

    db = os.path.expanduser(path or CC_DB)
    if not os.path.exists(db):
        return []
    out: List[Dict[str, Any]] = []
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
            "SELECT id, app_type, name, settings_config, meta, website_url "
            "FROM providers ORDER BY app_type, sort_index"
        ):
            try:
                cfg = json.loads(r["settings_config"] or "{}")
            except Exception:
                cfg = {}
            env = {k: v for k, v in (cfg.get("env") or {}).items() if isinstance(v, str)}
            auth = cfg.get("auth") or {}
            base = (env.get("ANTHROPIC_BASE_URL") or env.get("OPENAI_BASE_URL")
                    or env.get("GEMINI_BASE_URL") or cfg.get("base_url") or "")
            key = (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
                   or auth.get("OPENAI_API_KEY") or env.get("OPENAI_API_KEY")
                   or env.get("GEMINI_API_KEY") or "")
            try:
                meta = json.loads(r["meta"] or "{}")
            except Exception:
                meta = {}
            script = meta.get("usage_script") or {}
            hint = (script.get("templateType") or "").lower()
            access_token = str(script.get("accessToken") or "").strip()
            user_id = str(script.get("userId") or "").strip()

            name = r["name"] or r["id"]
            low = (name + " " + base + " " + (r["website_url"] or "")).lower()
            # 先认这几家有专用提取器的站点：它们的余额路径与通用模板不同
            if "linoroute" in low:
                tpl = "linoroute"
            elif "lmuai" in low or "lmu" in low:
                tpl = "lmu"
            elif "zhipu" in low or "bigmodel" in low or "glm" in low or "智谱" in low:
                tpl = "zhipu"
            elif "deepseek" in low:
                tpl = "deepseek"
            elif "moonshot" in low or "kimi" in low:
                tpl = "moonshot"
            elif "openrouter" in low:
                tpl = "openrouter"
            elif hint == "newapi" or "newapi" in low:
                tpl = "newapi"
            elif "openai" in low:
                tpl = "openai"
            else:
                tpl = "custom"

            out.append({
                "source_id": f"{r['app_type']}:{r['id']}",
                "label": name,
                "app_type": r["app_type"],
                "base_url": base,
                "api_key_set": bool(key),
                "api_key": key,
                "access_token": access_token,
                "access_token_set": bool(access_token),
                "user_id": user_id,
                "template": tpl,
                "template_hint": hint,
                "has_usage_script": bool((script.get("code") or "").strip()),
                "website": r["website_url"] or "",
            })
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out
