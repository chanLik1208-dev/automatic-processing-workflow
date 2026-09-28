"""Agent 執行引擎：讀 workflow → 跑 tool-calling 迴圈 → 每一步寫進 SQLite。"""
import datetime
import importlib.util
import shutil
import tempfile
import subprocess
import json
import os
import sys
import pathlib
import re
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.request

# APP_DIR：程式自己帶的檔案（打包後在唯讀的暫存資料夾）
# ROOT：使用者的資料（設定、工作流、skill、紀錄、報告），打包後放在各平台的使用者資料夾
APP_DIR = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).parent))


def _data_dir():
    if os.environ.get("AUTOWORKFLOW_HOME"):
        return pathlib.Path(os.environ["AUTOWORKFLOW_HOME"]).expanduser()
    if not getattr(sys, "frozen", False):
        return pathlib.Path(__file__).parent            # 用原始碼跑：資料就在專案資料夾
    home = pathlib.Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "AutoWorkflow"
    if sys.platform == "win32":
        return pathlib.Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "AutoWorkflow"
    return pathlib.Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "autoworkflow"


ROOT = _data_dir()
DB_PATH = ROOT / "data" / "runs.db"


def seed_data_dir():
    """第一次執行時把預設的設定、工作流、skill 放進資料夾。
    內建 skill 每次啟動都會更新成程式附的版本（使用者自己加的 skill 不動）；設定和工作流只補缺的，不覆蓋。"""
    for sub in ("data", "reports", "workflows", "skills"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)
    if APP_DIR.resolve() == ROOT.resolve():
        return
    src = APP_DIR / "defaults" if (APP_DIR / "defaults").is_dir() else APP_DIR
    if not (ROOT / "config.json").exists():
        shutil.copy2(src / "config.json", ROOT / "config.json")
    for f in (src / "workflows").glob("*.json"):
        if not (ROOT / "workflows" / f.name).exists():
            shutil.copy2(f, ROOT / "workflows" / f.name)
    for f in (APP_DIR / "skills").glob("*.py"):
        shutil.copy2(f, ROOT / "skills" / f.name)
_db_lock = threading.Lock()


def load_config():
    return json.loads((ROOT / "config.json").read_text())


# ---------- 資料庫 ----------

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    seed_data_dir()
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


FIELD_ORDER = ["title", "description", "provider", "model", "fallback", "fallback_model", "enabled", "schedule",
               "max_steps", "max_tokens", "skills", "system", "task"]


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
        if d.get("fallback_model"):
            wf["fallback_model"] = str(d["fallback_model"]).strip()
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
    if d.get("max_tokens"):
        wf["max_tokens"] = max(256, min(32768, int(d["max_tokens"])))
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
    if p.get("type") == "cli":
        return p
    if "api_key_env" in p:
        p["api_key"] = os.environ.get(p["api_key_env"], "")
        if not p["api_key"]:
            raise RuntimeError(f"{name}: 環境變數 {p['api_key_env']} 沒有設定")
    return p


class Cancelled(Exception):
    pass


class ModelBroken(Exception):
    """模型本身壞掉（數值崩潰等），不是網路問題；要給人看得懂的原因。"""


LMS_LOGS = pathlib.Path.home() / ".lmstudio" / "server-logs"
LMS_CLI = pathlib.Path.home() / ".lmstudio" / "bin" / ("lms.exe" if sys.platform == "win32" else "lms")


def _lms_log():
    try:
        return max(LMS_LOGS.glob("*/*.log"), key=lambda p: p.stat().st_mtime)
    except ValueError:
        return None


# ---------- LM Studio 原始輸出 ----------
# OpenAI 相容 API 給的是 LM Studio 處理過的內容（工具呼叫被拆開、無效 token 被丟掉）。
# `lms log stream --source model --filter output` 會在每次生成結束時給出模型寫的原文和停止原因，
# 錄下來跟我們收到的內容對照，LM Studio 吞掉了什麼就看得出來。
_raw_events, _raw_lock, _raw_started = [], threading.Lock(), False


def _raw_listener():
    while True:
        try:
            p = subprocess.Popen([str(LMS_CLI), "log", "stream", "--source", "model", "--filter", "output", "--json"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
            for line in p.stdout:
                if not line.startswith("{"):
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                d = e.get("data") or {}
                if d.get("type") == "llm.prediction.output":
                    with _raw_lock:
                        _raw_events.append({"ts": e.get("timestamp", 0) / 1000, "model": d.get("modelIdentifier"),
                                            "output": d.get("output", ""), "stats": d.get("stats") or {}})
                        del _raw_events[:-50]
        except Exception:
            traceback.print_exc()
        time.sleep(5)                            # lms 被關掉或 LM Studio 重開：過一下再接上


def _ensure_raw_listener():
    global _raw_started
    if not _raw_started and LMS_CLI.exists():
        _raw_started = True
        threading.Thread(target=_raw_listener, daemon=True).start()


def take_raw(model, since, wait=3.0):
    """拿走「這個模型、這個時間之後」的第一段原始輸出；事件在生成結束後才到，所以稍等一下。"""
    deadline = time.time() + wait
    while True:
        with _raw_lock:
            for i, e in enumerate(_raw_events):
                if e["model"] == model and e["ts"] >= since - 1:
                    return _raw_events.pop(i)
        if time.time() > deadline:
            return None
        time.sleep(0.2)


def _watch_lmstudio(live, stop, model):
    """LM Studio 遇到 NaN token 時會默默把 token 丟掉、不傳給我們，但會寫進它自己的 log。
    盯著 log：一出現就標記、切斷連線、把壞掉的模型卸載（它不會自己停，會一直空轉佔 GPU）。"""
    log = _lms_log()
    if not log:
        return
    pos = log.stat().st_size
    while not stop.wait(2):
        try:
            with open(log, "rb") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except OSError:
            return
        if b"received nan" in chunk:
            live["broken"] = ("模型發生數值崩潰（算出的機率變成 NaN），之後產生的全是無效 token，"
                              "LM Studio 把它們丟掉了，所以沒有任何內容出來。已經自動停止並卸載模型，下次會重新載入。")
            resp = live.get("_resp")
            if resp:
                try:
                    resp.close()
                except Exception:
                    pass
            subprocess.run([str(LMS_CLI), "unload", model], capture_output=True, timeout=60)
            return


_cancel = set()


def cancel(name):
    """標記要停止；引擎在下一輪開始前檢查（正在等模型回覆的那一輪會先跑完）。"""
    ids = [rid for rid, v in LIVE.items() if v["workflow"] == name]
    _cancel.update(ids)
    for rid in ids:                             # 正在等模型的話直接切斷連線，LM Studio 也會跟著停止生成
        proc = LIVE.get(rid, {}).get("_proc")
        if proc:
            proc.kill()
        resp = LIVE.get(rid, {}).get("_resp")
        if resp:
            try:
                resp.close()
            except Exception:
                pass
    return bool(ids)


# 執行中的 run 即時狀態（給監控頁看「現在在做什麼、在想什麼」），run 結束就移除
LIVE = {}


def chat(provider, model, messages, tools, live=None, timeout=300, max_tokens=4096):
    """串流呼叫，邊收邊更新 live；回傳格式跟非串流的 chat completion 一樣。"""
    p = provider_conf(provider)
    if p.get("type") == "cli":
        return chat_cli(p, model, messages, tools, live if live is not None else {})
    # 一定要有上限：模型偶爾會鬼打牆地一直生成，沒上限就會一路寫到 context 滿
    body = {"model": model or p["default_model"] or _loaded_model(p), "messages": messages, "temperature": 0.3,
            "max_tokens": max_tokens, "stream": True, "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        p["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {p['api_key']}"},
    )
    live = live if live is not None else {}
    live.pop("broken", None)
    reasoning, content, calls, usage, model_name, finish, done = "", "", {}, {}, body["model"], None, False
    stop_watch = threading.Event()
    local = "localhost" in p["base_url"] or "127.0.0.1" in p["base_url"]
    started = time.time()
    if local:
        _ensure_raw_listener()
        threading.Thread(target=_watch_lmstudio, args=(live, stop_watch, body["model"]), daemon=True).start()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            live["_resp"] = r
            for raw in r:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                chunk = json.loads(data)
                usage = chunk.get("usage") or usage
                model_name = chunk.get("model") or model_name
                for ch in chunk.get("choices") or []:
                    finish = ch.get("finish_reason") or finish
                    d = ch.get("delta") or {}
                    if d:
                        live["last_token"] = time.time()
                    r_part = d.get("reasoning_content") or d.get("reasoning")
                    if r_part:
                        reasoning += r_part
                        live.update(phase="thinking", reasoning=reasoning)
                    if d.get("content"):
                        content += d["content"]
                        # 有些模型把思考直接夾在 content 的 <think> 裡
                        if "<think>" in content and "</think>" not in content:
                            live.update(phase="thinking", reasoning=content.split("<think>", 1)[1])
                        elif not calls:   # 已經開始呼叫工具就停在「決定要…」，不要跟說明文字來回切換
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
    except Exception:
        if live.get("run_id") in _cancel:      # 連線是被「停止」切斷的
            raise Cancelled()
        if live.get("broken"):
            raise ModelBroken(live["broken"])
        raise
    finally:
        stop_watch.set()
        live.pop("_resp", None)
    if live.get("run_id") in _cancel:
        raise Cancelled()
    if live.get("broken"):
        raise ModelBroken(live["broken"])
    if local and (not content and not calls or not done and not finish):
        live["_raw"] = take_raw(body["model"], started, wait=1.5)
    if not content and not calls:
        raise ModelBroken("模型什麼都沒有回傳（可能剛崩潰或正在重新載入）。")
    if not done and not finish:
        # 連線在沒有結束訊號的情況下斷了：寫到一半的東西不能當成完成的結果
        raise ModelBroken(f"模型寫到一半連線就斷了（已寫 {len(content)} 字），內容不完整。常見原因是 LM Studio 崩潰或記憶體不足。")
    if finish == "length":
        content += f"\n\n[已達單輪輸出上限 {max_tokens} tokens，內容被截斷]"
    tool_calls = []
    for i, c in sorted(calls.items()):
        c["id"] = c["id"] or f"call_{i}"
        tool_calls.append(c)
    msg = {"role": "assistant", "content": content or None, "reasoning_content": reasoning}
    if local:
        msg["raw"] = take_raw(body["model"], started)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"model": model_name, "usage": usage, "choices": [{"message": msg}]}


# ---------- 訂閱型 CLI（用登入的帳號，不用 API key） ----------
# CLI 代理自己的工具全部關掉，只拿它當「會思考的模型」；要動手的事一律走我們的 skill，
# 這樣白名單、停止、監控都照樣有效。工具呼叫改用文字協定：要用工具時整段回覆只放一個 JSON。

TOOL_PROTOCOL = """

## 工具
你可以使用下面這些工具（JSON Schema）：
{specs}

要使用工具時，整個回覆只能是一個 JSON 物件，不要有其他文字、不要用 ``` 包起來：
{{"say": "一句話說明你要做什麼（可省略）", "tool_calls": [{{"name": "工具名稱", "arguments": {{...}}}}]}}
可以一次呼叫好幾個工具。工具的結果會在下一則訊息給你。
不需要再用工具、要給出最終答案時，直接用一般文字回覆，不要輸出 JSON。
你沒有其他任何工具或檔案存取能力，只能用上面列出的這些。"""


def _transcript(messages):
    """把 OpenAI 格式的對話攤平成一段文字（CLI 每次都是無狀態呼叫）。"""
    out = []
    for m in messages[1:]:
        if m["role"] == "user":
            out.append(f"## 使用者\n{m['content']}")
        elif m["role"] == "assistant":
            part = [m["content"]] if m.get("content") else []
            for c in m.get("tool_calls") or []:
                part.append(f"[呼叫工具] {c['function']['name']} {c['function']['arguments']}")
            out.append("## 你（先前的回覆）\n" + "\n".join(part))
        elif m["role"] == "tool":
            out.append(f"## 工具結果\n{m['content']}")
    out.append("## 現在輪到你回覆")
    return "\n\n".join(out)


def _parse_tool_json(text):
    """找回覆裡帶 tool_calls 的 JSON 物件；模型常會先講一句話再接 JSON，前面的話當作 say。"""
    t = re.sub(r"```(?:json)?", "", text)
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", t):
        try:
            d, _ = dec.raw_decode(t, m.start())
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("tool_calls"), list):
            before = t[:m.start()].strip()
            if before and not d.get("say"):
                d["say"] = before
            return d
    return None


CLI_ADAPTERS = {"claude"}                        # 實際測通過的；codex / gemini 等安裝後再接


def chat_cli(p, model, messages, tools, live, timeout=600):
    if p.get("adapter") not in CLI_ADAPTERS:
        raise RuntimeError(f"{p.get('label') or p['command']} 的串接還沒完成")
    system = messages[0]["content"]
    if tools:
        system += TOOL_PROTOCOL.format(specs=json.dumps([t["function"] for t in tools], ensure_ascii=False, indent=1))
    model = model or p["default_model"]
    cmd = [shutil.which(p["command"]) or os.path.expanduser(p["path"]),
           "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--setting-sources", "project",
           "--system-prompt", system, "--model", model]
    tmp = tempfile.mkdtemp(prefix="wf-cli-")         # 在空資料夾執行，不會讀到任何專案的 CLAUDE.md / 設定
    proc = subprocess.Popen(cmd, cwd=tmp, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8")
    live["_proc"] = proc
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    text, thinking, usage, result, err = "", "", {}, None, None
    try:
        proc.stdin.write(_transcript(messages))
        proc.stdin.close()
        for line in proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ev = e.get("event") or {}
            if ev.get("type") == "content_block_delta":
                d = ev.get("delta") or {}
                live["last_token"] = time.time()
                if d.get("type") == "thinking_delta":
                    thinking += d.get("thinking", "")
                    live.update(phase="thinking", reasoning=thinking)
                elif d.get("text"):
                    text += d["text"]
                    # 開始寫工具呼叫的 JSON 了，不要當成結果顯示
                    if '"tool_calls"' in text or text.lstrip().startswith("{"):
                        live.update(phase="deciding", tool={"name": "", "args": ""})
                    else:
                        live.update(phase="writing", content=text)
            elif e.get("type") == "result":
                usage, result = e.get("usage") or {}, e.get("result")
                if e.get("is_error"):
                    err = result or e.get("subtype")
        proc.wait()
    finally:
        killer.cancel()
        live.pop("_proc", None)
        shutil.rmtree(tmp, ignore_errors=True)
    if live.get("run_id") in _cancel:
        raise Cancelled()
    if err or proc.returncode:
        raise RuntimeError(f"{p['command']} 執行失敗：{err or proc.stderr.read()[-400:] or proc.returncode}")
    text = result if isinstance(result, str) and result else text
    parsed = _parse_tool_json(text) if tools else None
    msg = {"role": "assistant", "content": text, "reasoning_content": thinking}
    if parsed:
        msg["content"] = parsed.get("say") or None
        msg["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                              "function": {"name": c.get("name", ""), "arguments": json.dumps(c.get("arguments") or {}, ensure_ascii=False)}}
                             for i, c in enumerate(parsed["tool_calls"])]
    if not msg.get("content") and not msg.get("tool_calls"):
        raise ModelBroken(f"{p['command']} 沒有回傳任何內容。")
    return {"model": model, "choices": [{"message": msg}],
            "usage": {"prompt_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
                      "completion_tokens": usage.get("output_tokens", 0)}}


def _loaded_model(p):
    """沒指定模型時，問 LM Studio 目前載入了哪顆（跳過 embedding 模型）。"""
    base = p["base_url"].rstrip("/")
    try:
        with urllib.request.urlopen(base.replace("/v1", "") + "/api/v0/models", timeout=5) as r:
            ms = json.loads(r.read()).get("data", [])
        loaded = [m["id"] for m in ms if m.get("state") == "loaded" and m.get("type") != "embeddings"]
        if loaded:
            return loaded[0]
        raise RuntimeError("LM Studio 目前沒有載入任何模型，請先在 LM Studio 載入一個模型，或在工作流設定裡指定模型")
    except urllib.error.URLError:
        raise RuntimeError("連不上 LM Studio（請確認已開啟，並在 Developer 分頁啟動 server）")


def ping_provider(name):
    try:
        p = provider_conf(name)
        if p.get("type") == "cli":
            exe = shutil.which(p["command"]) or (os.path.expanduser(p["path"]) if p.get("path") else "")
            info = {"kind": "cli", "label": p.get("label"), "default_model": p["default_model"], "models": p.get("models", [])}
            if not exe or not os.path.exists(exe):
                plat = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
                return {**info, "ok": False, "missing": True, "error": f"沒有安裝 {p['command']}",
                        "install": (p.get("install") or {}).get(plat, []), "login": p.get("login", "")}
            if p.get("adapter") not in CLI_ADAPTERS:
                return {**info, "ok": False, "error": f"已安裝 {p['command']}，但串接還沒完成"}
            return {**info, "ok": True}
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
    task = wf["task"]
    if wf.get("description"):
        task = f"（這個工作流的說明：{wf['description']}）\n\n{task}"
    if extra_input:
        task += f"\n\n額外輸入：{extra_input}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]

    provider, model = wf.get("provider", "lmstudio"), wf.get("model")
    run_id = _exec("INSERT INTO runs (workflow, provider, model, trigger, status, started, input) VALUES (?,?,?,?,?,?,?)",
                   (name, provider, model or "", trigger, "running", time.time(), task))
    idx, tin, tout = 0, 0, 0
    live = LIVE[run_id] = {"run_id": run_id, "workflow": name, "title": wf.get("title", name),
                           "started": time.time(), "round": 0, "provider": provider, "model": model or ""}

    fails, first_err = 0, None
    try:
        for rnd in range(wf["max_steps"]):
            if run_id in _cancel:
                raise Cancelled()
            t0 = time.time()
            live.update(round=rnd + 1, phase="waiting", since=t0, reasoning="", content="", tool=None, last_token=None,
                        provider=provider, model=model or "")
            try:
                resp = chat(provider, model, messages, tools, live, max_tokens=wf.get("max_tokens", 4096))
            except Cancelled:
                raise
            except Exception as e:
                # 主 provider 掛了就換備援，同一個 run 裡接著跑
                fb = wf.get("fallback")
                first_err = first_err or str(e)
                if fb and fb != provider:
                    raw = live.pop("_raw", None)
                    add_step(run_id, idx, "error", provider, json.dumps({"raw": raw}, ensure_ascii=False) if raw else "",
                             f"{e}\n→ 改用備援 {fb}"); idx += 1
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
                     reasoning, json.dumps({"content": msg.get("content"), "tool_calls": calls, "raw": msg.get("raw")},
                                           ensure_ascii=False), ms)
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
                failed = result.startswith(("[skill 錯誤]", "找不到", "抓不到", "這個路徑不", "沒有這個", "只接受"))
                fails = fails + 1 if failed else 0
                if fails >= 3:
                    # 小模型碰到錯誤常會一直換個猜法重試；連錯三次就叫它停下來照實回報
                    result += ("\n\n[系統] 已經連續失敗 3 次。不要再猜網址或路徑，"
                               "直接回報你缺什麼資訊、哪裡失敗，然後結束。")
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result[:40000]})

        raise RuntimeError(f"超過 max_steps={wf['max_steps']} 還沒結束")
    except Cancelled:
        add_step(run_id, idx, "error", "cancelled", "", "使用者停止了這次執行")
        _exec("UPDATE runs SET status='cancelled', finished=?, error=?, tokens_in=?, tokens_out=? WHERE id=?",
              (time.time(), "使用者停止了這次執行", tin, tout, run_id))
        return run_id
    except Exception as e:
        msg = f"{first_err}；備援也失敗：{e}" if first_err and first_err != str(e) else str(e)
        add_step(run_id, idx, "error", "engine", traceback.format_exc()[-3000:], msg)
        _exec("UPDATE runs SET status='failed', finished=?, error=?, tokens_in=?, tokens_out=? WHERE id=?",
              (time.time(), msg, tin, tout, run_id))
        return run_id
    finally:
        LIVE.pop(run_id, None)
        _cancel.discard(run_id)


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
