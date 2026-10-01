"""設定頁的讀寫：驗證每個欄位；API key 只寫不讀（回給瀏覽器的只有末四碼）。"""
import re

import engine

LABELS = {"tick_seconds": "排程檢查間隔", "server.port": "監控頁 port", "limits.max_tokens": "單輪最多輸出",
          "limits.request_timeout": "API 逾時", "limits.cli_timeout": "訂閱 CLI 逾時", "limits.max_steps": "最多幾輪",
          "limits.fail_streak": "連續失敗幾次叫它停", "limits.stall_seconds": "卡住提示", "search.limit": "搜尋筆數",
          "fetch.max_chars": "網頁最多讀幾字", "fetch.auto_images": "每頁自動看幾張圖"}
NUM = {  # 路徑: (最小, 最大)
    "tick_seconds": (5, 3600), "server.port": (1024, 65535),
    "limits.max_tokens": (256, 65536), "limits.request_timeout": (30, 3600), "limits.cli_timeout": (30, 7200),
    "limits.max_steps": (1, 60), "limits.fail_streak": (1, 20), "limits.stall_seconds": (10, 3600),
    "search.limit": (1, 15), "fetch.max_chars": (1000, 100000), "fetch.auto_images": (0, 6),
}
BOOL = ["lmstudio_guard.nan_watchdog", "lmstudio_guard.raw_capture", "notify.enabled", "update.auto_check", "update.auto_install", "search.native", "browser.enabled", "browser.ask_user"]
TEXT = {"language": 40, "search.region": 20, "export.browser_path": 500}


def _get(d, path):
    for k in path.split("."):
        d = d.get(k, {}) if isinstance(d, dict) else {}
    return d


def _set(d, path, v):
    *head, last = path.split(".")
    for k in head:
        d = d.setdefault(k, {})
    d[last] = v


def masked():
    c = engine.load_config()
    tok = (c.get("update") or {}).pop("github_token", "")
    c.setdefault("update", {})["token_hint"] = f"已設定（末四碼 {tok[-4:]}）" if tok else ""
    for name, p in c.get("providers", {}).items():
        key = p.pop("api_key", "")
        p["key_hint"] = f"已設定（末四碼 {key[-4:]}）" if key else ""
        p["key_from_env"] = bool(p.get("api_key_env") and engine.os.environ.get(p["api_key_env"]))
        p.pop("install", None)
    return c


def save(body):
    with engine.CONFIG_LOCK:                          # 跟「自動加入訂閱模型」用同一把鎖，免得互相蓋掉
        return _save(body)


def _save(body):
    raw = engine.json.loads((engine.ROOT / "config.json").read_text(encoding="utf-8"))
    for path, (lo, hi) in NUM.items():
        v = _get(body, path)
        if v != {}:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"「{LABELS.get(path, path)}」要填數字")
            if not lo <= v <= hi:
                raise ValueError(f"「{LABELS.get(path, path)}」要在 {lo}–{hi} 之間")
            _set(raw, path, v)
    for path in BOOL:
        v = _get(body, path)
        if v != {}:
            _set(raw, path, bool(v))
    for path, n in TEXT.items():
        v = _get(body, path)
        if v != {}:
            _set(raw, path, str(v).strip()[:n])
    if "readable_paths" in body:
        paths = [str(p).strip() for p in body["readable_paths"] if str(p).strip()]
        if any(p in ("/", "~", "~/**", "/**") or p.startswith("/**") for p in paths):
            raise ValueError("讀檔白名單不能開放整個磁碟或整個家目錄")
        raw["readable_paths"] = paths[:50]

    if "auto" in body:
        a = body["auto"] or {}
        order, seen = [], set()
        for it in a.get("order") or []:                     # 同一個來源可以出現多次，只要模型不同（Opus、Sonnet 各排各的）
            name, model = str(it.get("provider") or ""), str(it.get("model") or "").strip()[:120]
            if not name or (name, model) in seen:
                continue
            seen.add((name, model))
            order.append({"provider": name, "model": model,
                          "max_daily_tokens": max(0, int(it.get("max_daily_tokens") or 0)),
                          "max_daily_runs": max(0, int(it.get("max_daily_runs") or 0))})
        thr = float(a.get("cli_max_utilization", 0.9))
        if not 0.1 <= thr <= 1:
            raise ValueError("訂閱額度門檻要在 10%–100% 之間")
        # seen_models 要留著：使用者刪掉的自動加入模型，不會在下次偵測時又被加回來。
        # 但頁面打開「之後」才自動加進來的模型，使用者根本沒看到，不能當成他刪的：
        # 頁面會送回它載入時看過的 seen_models，不在裡面的新模型照原本的列補回最後面。
        cur = raw.get("auto") or {}
        seen = cur.get("seen_models", [])
        if isinstance(a.get("seen_models"), list):
            known, have = set(a["seen_models"]), {(it["provider"], it["model"]) for it in order}
            for it in cur.get("order", []):
                k = f"{it.get('provider')}/{it.get('model') or ''}"
                if k in seen and k not in known and (it.get("provider"), it.get("model") or "") not in have:
                    order.append(it)
        raw["auto"] = {"order": order, "cli_max_utilization": thr, "seen_models": seen}

    up = body.get("update") or {}
    if up.get("new_token"):
        raw.setdefault("update", {})["github_token"] = str(up["new_token"]).strip()[:200]
    elif up.get("clear_token"):
        raw.setdefault("update", {}).pop("github_token", None)
    if "local_fallback" in body:
        lf = body["local_fallback"] or {}
        raw["local_fallback"] = {"enabled": bool(lf.get("enabled")), "provider": str(lf.get("provider") or "lmstudio"),
                                 "model": str(lf.get("model") or "")[:120]}

    old = raw.get("providers", {})
    new = {}
    for name, p in (body.get("providers") or {}).items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", name):
            raise ValueError(f"模型來源代號 {name} 只能用英文小寫、數字、- 和 _")
        prev = dict(old.get(name, {}))
        kind = prev.get("type", p.get("type", "openai"))
        q = {**prev, "label": str(p.get("label") or prev.get("label") or name)[:40],
             "enabled": bool(p.get("enabled", True)), "default_model": str(p.get("default_model") or "")[:120]}
        if kind == "cli":
            q["type"] = "cli"
            eff = str(p.get("effort") or "")
            if eff not in ("", "low", "medium", "high", "xhigh"):
                raise ValueError(f"{q['label']} 的思考強度不認得：{eff}")
            q["effort"] = eff
            if p.get("path") is not None:
                q["path"] = str(p["path"]).strip()[:500]
        else:
            q.pop("type", None)
            url = str(p.get("base_url") or prev.get("base_url") or "").strip()
            if not re.match(r"^https?://", url):
                raise ValueError(f"{q['label']} 的網址要以 http:// 或 https:// 開頭")
            q["base_url"] = url.rstrip("/")
            q["needs_key"] = bool(p.get("needs_key", prev.get("needs_key") or prev.get("api_key_env")))
            if p.get("clear_key"):
                q.pop("api_key", None)
            elif p.get("new_key"):
                q["api_key"] = str(p["new_key"]).strip()
        new[name] = q
    if new:
        if not any(v.get("enabled", True) for v in new.values()):
            raise ValueError("至少要留一個啟用的模型來源")
        raw["providers"] = new
    known = set(raw.get("providers", {}))
    if raw.get("local_fallback", {}).get("enabled") and raw["local_fallback"]["provider"] not in known:
        raise ValueError("本地備用選的來源不在模型來源清單裡")
    for it in raw.get("auto", {}).get("order", []):
        if it["provider"] not in known:
            raise ValueError(f"自動模式裡的 {it['provider']} 不在模型來源清單裡")
    engine.save_config(raw)
    return masked()
