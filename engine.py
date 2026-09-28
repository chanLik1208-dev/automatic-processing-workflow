"""Agent 執行引擎：讀 workflow → 跑 tool-calling 迴圈 → 每一步寫進 SQLite。"""
import datetime
import importlib.util
import json
import os
import pathlib
import re
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).parent
DB_PATH = ROOT / "data" / "runs.db"
_db_lock = threading.Lock()


def load_config():
    return json.loads((ROOT / "config.json").read_text())


# ---------- 資料庫 ----------

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow TEXT, provider TEXT, model TEXT, trigger TEXT,
            status TEXT, started REAL, finished REAL,
            input TEXT, output TEXT, error TEXT,
            tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER, idx INTEGER, kind TEXT, name TEXT,
            input TEXT, output TEXT, ts REAL, ms INTEGER
        );
        """)
        # 程式重啟時，上次沒跑完的標成 interrupted
        c.execute("UPDATE runs SET status='interrupted' WHERE status='running'")


def _exec(sql, args=()):
    with _db_lock, db() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def add_step(run_id, idx, kind, name, inp, out, ms=0):
    _exec("INSERT INTO steps (run_id, idx, kind, name, input, output, ts, ms) VALUES (?,?,?,?,?,?,?,?)",
          (run_id, idx, kind, name, inp, out, time.time(), ms))


# ---------- Skills ----------

def load_skills():
    """skills/ 底下每個 .py 都要有 SPEC（OpenAI function schema）和 run(**kwargs) -> str。"""
    skills = {}
    for f in sorted((ROOT / "skills").glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"skills.{f.stem}", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        skills[mod.SPEC["name"]] = mod
    return skills


# ---------- Workflows ----------

def load_workflows():
    wfs = {}
    for f in sorted((ROOT / "workflows").glob("*.json")):
        try:
            wf = json.loads(f.read_text())
        except ValueError as e:
            print(f"略過格式錯誤的 {f.name}：{e}")
            continue
        wf.setdefault("name", f.stem)
        wf.setdefault("enabled", True)
        wf.setdefault("max_steps", 12)
        wfs[wf["name"]] = wf
    return wfs


FIELD_ORDER = ["title", "description", "provider", "model", "fallback", "enabled", "schedule",
               "max_steps", "skills", "system", "task"]


def validate_workflow(d):
    """GUI 送來的 workflow 設定：檢查、清理，回傳可以直接寫檔的 dict；有問題就 raise ValueError。"""
    providers = load_config()["providers"]
    skills = {p.stem for p in (ROOT / "skills").glob("*.py")}
    wf = {}
    wf["title"] = str(d.get("title") or "").strip()[:60]
    if not wf["title"]:
        raise ValueError("名稱不能是空的")
    wf["description"] = str(d.get("description") or "").strip()[:300]
    if d.get("provider") not in providers:
        raise ValueError(f"不認得的模型來源：{d.get('provider')}")
    wf["provider"] = d["provider"]
    if d.get("model"):
        wf["model"] = str(d["model"]).strip()
    if d.get("fallback"):
        if d["fallback"] not in providers or d["fallback"] == wf["provider"]:
            raise ValueError("備援必須是另一個模型來源")
        wf["fallback"] = d["fallback"]
    wf["enabled"] = bool(d.get("enabled", True))
    s = d.get("schedule") or {}
    if s.get("daily"):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(s["daily"])):
            raise ValueError("每天的時間要是 HH:MM")
        wf["schedule"] = {"daily": s["daily"]}
    elif s.get("every_minutes"):
        m = int(s["every_minutes"])
        if not 5 <= m <= 10080:
            raise ValueError("間隔要在 5 分鐘到 7 天之間")
        wf["schedule"] = {"every_minutes": m}
    wf["max_steps"] = max(1, min(40, int(d.get("max_steps") or 12)))
    bad = [x for x in d.get("skills") or [] if x not in skills]
    if bad:
        raise ValueError(f"沒有這些 skill：{bad}")
    wf["skills"] = list(dict.fromkeys(d.get("skills") or []))
    wf["system"] = str(d.get("system") or "").strip()
    wf["task"] = str(d.get("task") or "").strip()
    if not wf["task"]:
        raise ValueError("任務說明不能是空的")
    return {k: wf[k] for k in FIELD_ORDER if k in wf}


def save_workflow(name, d, create=False):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name or ""):
        raise ValueError("代號只能用英文小寫、數字和連字號")
    path = ROOT / "workflows" / f"{name}.json"
    if create and path.exists():
        raise ValueError(f"已經有代號 {name} 的工作流")
    if not create and not path.exists():
        raise ValueError(f"找不到工作流 {name}")
    wf = validate_workflow(d)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)
    return wf


def trash_workflow(name):
    """不直接刪：搬進 workflows/.trash/，要救回來把檔案搬回去就好。"""
    path = ROOT / "workflows" / f"{name}.json"
    if not path.exists():
        raise ValueError(f"找不到工作流 {name}")
    trash = ROOT / "workflows" / ".trash"
    trash.mkdir(exist_ok=True)
    dest = trash / f"{name}-{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
    path.replace(dest)
    return str(dest)


# ---------- LLM ----------

def provider_conf(name):
    p = dict(load_config()["providers"][name])
    if "api_key_env" in p:
        p["api_key"] = os.environ.get(p["api_key_env"], "")
        if not p["api_key"]:
            raise RuntimeError(f"{name}: 環境變數 {p['api_key_env']} 沒有設定")
    return p


# 執行中的 run 即時狀態（給監控頁看「現在在做什麼、在想什麼」），run 結束就移除
LIVE = {}


def chat(provider, model, messages, tools, live=None, timeout=300):
    """串流呼叫，邊收邊更新 live；回傳格式跟非串流的 chat completion 一樣。"""
    p = provider_conf(provider)
    body = {"model": model or p["default_model"], "messages": messages, "temperature": 0.3,
            "stream": True, "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        p["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {p['api_key']}"},
    )
    live = live if live is not None else {}
    reasoning, content, calls, usage, model_name = "", "", {}, {}, body["model"]
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                usage = chunk.get("usage") or usage
                model_name = chunk.get("model") or model_name
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    r_part = d.get("reasoning_content") or d.get("reasoning")
                    if r_part:
                        reasoning += r_part
                        live.update(phase="thinking", reasoning=reasoning)
                    if d.get("content"):
                        content += d["content"]
                        # 有些模型把思考直接夾在 content 的 <think> 裡
                        if "<think>" in content and "</think>" not in content:
                            live.update(phase="thinking", reasoning=content.split("<think>", 1)[1])
                        else:
                            live.update(phase="writing", content=strip_think(content))
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {
                            "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        slot["id"] = tc.get("id") or slot["id"]
                        f = tc.get("function") or {}
                        slot["function"]["name"] += f.get("name") or ""
                        slot["function"]["arguments"] += f.get("arguments") or ""
                        live.update(phase="deciding", tool={"name": slot["function"]["name"],
                                                            "args": slot["function"]["arguments"]})
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{provider} HTTP {e.code}: {e.read().decode(errors='replace')[:500]}")
    tool_calls = []
    for i, c in sorted(calls.items()):
        c["id"] = c["id"] or f"call_{i}"
        tool_calls.append(c)
    msg = {"role": "assistant", "content": content or None, "reasoning_content": reasoning}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"model": model_name, "usage": usage, "choices": [{"message": msg}]}


def ping_provider(name):
    try:
        p = provider_conf(name)
        req = urllib.request.Request(p["base_url"].rstrip("/") + "/models",
                                     headers={"Authorization": f"Bearer {p['api_key']}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            ids = [m["id"] for m in json.loads(r.read()).get("data", [])]
        return {"ok": True, "models": ids, "default_model": p["default_model"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


# ---------- Agent 迴圈 ----------

def run_workflow(name, trigger="manual", extra_input=""):
    wf = load_workflows()[name]
    skills = load_skills()
    allowed = [s for s in wf.get("skills", []) if s in skills]
    tools = [{"type": "function", "function": skills[s].SPEC} for s in allowed]

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    system = wf.get("system", "你是一個自動執行任務的 agent。") + f"\n\n現在時間：{now}"
    if "use_skill" in allowed:
        cat = "\n".join(f"- {n}：{d}" for n, d in skills["use_skill"].catalog())
        system += f"\n\n可用的知識型 skill（任務相關時先用 use_skill 載入）：\n{cat}"
    task = wf["task"] + (f"\n\n額外輸入：{extra_input}" if extra_input else "")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]

    provider, model = wf.get("provider", "lmstudio"), wf.get("model")
    run_id = _exec("INSERT INTO runs (workflow, provider, model, trigger, status, started, input) VALUES (?,?,?,?,?,?,?)",
                   (name, provider, model or "", trigger, "running", time.time(), task))
    idx, tin, tout = 0, 0, 0
    live = LIVE[run_id] = {"run_id": run_id, "workflow": name, "title": wf.get("title", name),
                           "started": time.time(), "round": 0, "provider": provider, "model": model or ""}

    try:
        for rnd in range(wf["max_steps"]):
            t0 = time.time()
            live.update(round=rnd + 1, phase="waiting", since=t0, reasoning="", content="", tool=None,
                        provider=provider, model=model or "")
            try:
                resp = chat(provider, model, messages, tools, live)
            except Exception as e:
                # 主 provider 掛了就換備援，同一個 run 裡接著跑
                fb = wf.get("fallback")
                if fb and fb != provider:
                    add_step(run_id, idx, "error", provider, "", f"{e}\n→ 改用備援 {fb}"); idx += 1
                    provider, model = fb, wf.get("fallback_model")
                    _exec("UPDATE runs SET provider=?, model=? WHERE id=?", (provider, model or "", run_id))
                    continue
                raise
            ms = int((time.time() - t0) * 1000)
            usage = resp.get("usage") or {}
            tin += usage.get("prompt_tokens", 0); tout += usage.get("completion_tokens", 0)
            msg = resp["choices"][0]["message"]
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            calls = msg.get("tool_calls") or []

            add_step(run_id, idx, "llm", resp.get("model", model or provider),
                     reasoning, json.dumps({"content": msg.get("content"), "tool_calls": calls}, ensure_ascii=False), ms)
            idx += 1

            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not calls:
                output = strip_think(msg.get("content"))
                _exec("UPDATE runs SET status='success', finished=?, output=?, tokens_in=?, tokens_out=? WHERE id=?",
                      (time.time(), output, tin, tout, run_id))
                return run_id

            for call in calls:
                fn = call["function"]["name"]
                t0 = time.time()
                live.update(phase="tool", since=t0, tool={"name": fn, "args": call["function"].get("arguments")})
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                    if fn not in allowed:
                        raise RuntimeError(f"這個 workflow 沒有開放 skill：{fn}")
                    result = str(skills[fn].run(**args))
                except Exception as e:
                    result = f"[skill 錯誤] {e}"
                add_step(run_id, idx, "tool", fn, call["function"].get("arguments"), result[:20000],
                         int((time.time() - t0) * 1000))
                idx += 1
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result[:40000]})

        raise RuntimeError(f"超過 max_steps={wf['max_steps']} 還沒結束")
    except Exception as e:
        add_step(run_id, idx, "error", "engine", "", traceback.format_exc()[-3000:])
        _exec("UPDATE runs SET status='failed', finished=?, error=?, tokens_in=?, tokens_out=? WHERE id=?",
              (time.time(), str(e), tin, tout, run_id))
        return run_id
    finally:
        LIVE.pop(run_id, None)


# ---------- 排程 ----------

def next_due(wf, last_started):
    """schedule 支援 {"every_minutes": N} 或 {"daily": "HH:MM"}；沒寫就只能手動跑。"""
    s = wf.get("schedule") or {}
    now = datetime.datetime.now()
    last = datetime.datetime.fromtimestamp(last_started) if last_started else None
    if "every_minutes" in s:
        return (last + datetime.timedelta(minutes=s["every_minutes"])) if last else now
    if "daily" in s:
        h, m = map(int, s["daily"].split(":"))
        today = now.replace(hour=h, minute=m, second=0, microsecond=0)
        slot = today if now >= today else today - datetime.timedelta(days=1)
        if last and last >= slot:
            return slot + datetime.timedelta(days=1)
        return slot if last else (today if now < today else today + datetime.timedelta(days=1))
    return None


def last_scheduled_start(name):
    with db() as c:
        r = c.execute("SELECT MAX(started) FROM runs WHERE workflow=? AND trigger='schedule'", (name,)).fetchone()
    return r[0]


_running = set()
_running_lock = threading.Lock()


def start_async(name, trigger="manual", extra_input=""):
    with _running_lock:
        if name in _running:
            return False
        _running.add(name)

    def job():
        try:
            run_workflow(name, trigger, extra_input)
        finally:
            _running.discard(name)
    threading.Thread(target=job, daemon=True).start()
    return True


def scheduler_loop():
    tick = load_config().get("tick_seconds", 30)
    while True:
        try:
            for name, wf in load_workflows().items():
                if not wf["enabled"]:
                    continue
                due = next_due(wf, last_scheduled_start(name))
                if due and due <= datetime.datetime.now():
                    start_async(name, "schedule")
        except Exception:
            traceback.print_exc()
        time.sleep(tick)
