"""發佈前檢查：python tests/run_checks.py [--model 本地模型ID] [--no-network]

在暫時的資料夾裡跑，不會動到你真正的設定和紀錄。需要模型的項目只用 LM Studio（不花訂閱額度）；
LM Studio 沒開、或沒給 --model，就略過那幾項並標成「略過」。"""
import argparse
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="wf-check-"))
os.environ["AUTOWORKFLOW_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):      # Windows 主控台不是 UTF-8 時也要印得出報告
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import engine          # noqa: E402
import server          # noqa: E402
import skill_admin     # noqa: E402

results = []


class Skip(Exception):
    """外部服務（例如搜尋引擎）擋掉了，不是我們的程式壞了。"""


def check(name, fn, skip=None):
    if skip:
        results.append(("略過", name, skip))
        return
    try:
        detail = fn()
        results.append(("通過", name, detail or ""))
    except Skip as e:
        results.append(("略過", name, str(e)))
    except AssertionError as e:
        results.append(("失敗", name, str(e)))
    except Exception:
        results.append(("失敗", name, traceback.format_exc().strip().splitlines()[-1]))


def call(method, path, body=None):
    r = server.call(method, path, body)
    return r["status"], r.get("json")


# ---------------------------------------------------------------- 設定與資料
def t_seed():
    wfs = engine.load_workflows()
    assert set(wfs) == {"deepseek-research", "tech-news", "workflow-builder"}, wfs
    assert not wfs["tech-news"]["enabled"], "有排程的範例應該預設停用"
    assert not engine.load_config()["providers"]["deepseek"].get("enabled", True), "DeepSeek 應預設停用"
    return f"{len(wfs)} 條範例工作流，排程都關著"


def t_settings_validation():
    s = call("GET", "/api/settings")[1]
    bad = [({"server": {"port": 80}}, "port"), ({"limits": {"max_steps": 0}}, "最多幾輪"),
           ({"readable_paths": ["~/**"]}, "白名單"), ({"limits": {"max_tokens": "abc"}}, "數字")]
    for patch, word in bad:
        st, r = call("POST", "/api/settings", {**s, **patch})
        assert st == 400 and word in r["error"], (patch, r)
    st, r = call("POST", "/api/settings", {**s, "search": {"region": "hk-tzh", "limit": 5}})
    assert st == 200 and engine.cfg("search.limit") == 5
    return "錯的值會被擋，對的值會存進去"


def t_api_key_masking():
    s = call("GET", "/api/settings")[1]
    s["providers"]["deepseek"]["new_key"] = "sk-check-000000000WXYZ"
    st, r = call("POST", "/api/settings", s)
    assert st == 200 and "sk-check" not in json.dumps(r), "回應裡不能有完整 key"
    assert r["settings"]["providers"]["deepseek"]["key_hint"].endswith("WXYZ）")
    if sys.platform != "win32":
        mode = oct((HOME / "config.json").stat().st_mode & 0o777)
        assert mode == "0o600", f"設定檔權限是 {mode}"
    s = call("GET", "/api/settings")[1]
    s["providers"]["deepseek"]["clear_key"] = True
    call("POST", "/api/settings", s)
    assert "api_key" not in engine.load_config()["providers"]["deepseek"]
    return "只回末四碼；檔案 600；可以清除"


# ---------------------------------------------------------------- 工作流
def t_workflow_crud():
    body = {"name": "check-wf", "title": "檢查", "provider": "lmstudio", "task": "t", "skills": ["notify"],
            "fallback": "claude", "fallback_model": "haiku", "max_tokens": 2048}
    assert call("POST", "/api/workflows/new", body)[0] == 200
    assert call("POST", "/api/workflows/new", body)[0] == 400, "重複代號應該被擋"
    w = next(x for x in call("GET", "/api/workflows")[1] if x["name"] == "check-wf")
    assert w["fallback_model"] == "haiku" and w["max_tokens"] == 2048, "列表要帶這兩個欄位，編輯才不會清掉"
    assert call("POST", "/api/workflows/check-wf/save", {**w, "title": "改名"})[0] == 200
    w2 = engine.load_workflows()["check-wf"]
    assert w2["title"] == "改名" and w2["fallback_model"] == "haiku" and w2["max_tokens"] == 2048
    assert call("POST", "/api/workflows/check-wf/save", {**w, "schedule": {"daily": "25:00"}})[0] == 400
    assert call("POST", "/api/workflows/check-wf/save", {**w, "skills": ["rm_rf"]})[0] == 400
    assert call("POST", "/api/workflows/check-wf/delete", {})[0] == 200
    assert "check-wf" not in engine.load_workflows() and list((HOME / "workflows" / ".trash").glob("check-wf-*"))
    return "新增 / 編輯保留欄位 / 驗證 / 刪除到 .trash"


def t_http_csrf():
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_port

    def post(ctype, origin):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/workflows/nope/run", data=b"{}", method="POST",
                                     headers={"Content-Type": ctype, **({"Origin": origin} if origin else {})})
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        assert post("text/plain", "https://evil.example") == 403
        assert post("application/json", "https://evil.example") == 403
        assert post("application/json", f"http://127.0.0.1:{port}") == 404
    finally:
        httpd.shutdown()
    return "跨站請求 403，同源正常"


# ---------------------------------------------------------------- skill
def t_skill_import():
    md = "---\nname: Check Skill\ndescription: >\n  檢查用。\n---\n# 檢查\n"
    names = skill_admin.import_skill({"type": "file", "filename": "c.md", "content": base64.b64encode(md.encode()).decode()})
    assert names == ["check-skill"]
    py = "SPEC={'name':'check_tool','description':'x','parameters':{'type':'object','properties':{}}}\ndef run():\n    return 'ok'\n"
    b = {"type": "file", "filename": "check_tool.py", "content": base64.b64encode(py.encode()).decode()}
    try:
        skill_admin.import_skill(b)
        raise AssertionError(".py 沒勾確認也匯入了")
    except ValueError:
        pass
    assert skill_admin.import_skill({**b, "confirm_code": True}) == ["check_tool"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil/SKILL.md", md)
    try:
        skill_admin.import_skill({"type": "file", "filename": "e.zip", "content": base64.b64encode(buf.getvalue()).decode()})
        raise AssertionError("zip slip 沒被擋")
    except ValueError:
        pass
    assert not (HOME.parent / "evil").exists()
    try:
        skill_admin.delete_skill("notify")
        raise AssertionError("內建 skill 被刪掉了")
    except ValueError:
        pass
    skill_admin.delete_skill("check-skill")
    skill_admin.delete_skill("check_tool")
    return "md / .py 需確認 / zip slip / 內建不可刪 / 刪除"


def t_skill_github(no_net):
    names = skill_admin.import_skill({"type": "github", "url": "https://github.com/chanLik1208-dev/Dynamization"})
    assert names and (HOME / "skills" / names[0] / "SKILL.md").exists()
    skill_admin.delete_skill(names[0])
    return names[0]


# ---------------------------------------------------------------- 引擎邏輯（不需要模型）
def t_tool_json_parse():
    ok = [('{"tool_calls":[{"name":"a","arguments":{}}]}', "a"), ('先說一句。\n{"say":"x","tool_calls":[{"name":"b","arguments":{}}]}', "b"),
          ('```json\n{"tool_calls":[{"name":"c","arguments":{}}]}\n```', "c")]
    for text, name in ok:
        d = engine._parse_tool_json(text)
        assert d and d["tool_calls"][0]["name"] == name, text
    assert engine._parse_tool_json('設定像 {"port": 1234} 這樣。') is None
    return "4 種寫法都判斷正確"


def t_auto_pick():
    engine.init_db()
    c = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
    c["auto"] = {"order": [{"provider": "claude"}, {"provider": "lmstudio"}], "cli_max_utilization": 0.9}
    engine.save_config(c)
    engine.record_limits("claude", {"status": "allowed_warning", "utilization": 0.99, "rateLimitType": "five_hour",
                                    "resetsAt": time.time() + 3600})
    try:
        p, _, note = engine.pick_auto()
        assert p != "claude" and "99%" in note, note
    except RuntimeError as e:
        assert "99%" in str(e), e                      # 沒有其他可用來源時，原因裡也要寫清楚
    engine.record_limits("claude", {"status": "allowed", "utilization": 0.99, "resetsAt": time.time() - 1})
    try:
        p, _, _ = engine.pick_auto()
    except RuntimeError:
        p = None
    return f"額度 99% 會跳過 Claude；重置後{'恢復' if p == 'claude' else '（Claude 不可用，略過恢復檢查）'}"


def t_scheduler_lock():
    engine.init_db()
    assert engine.acquire_scheduler_lock()
    code = ("import os,sys;sys.path.insert(0,r'%s');os.environ['AUTOWORKFLOW_HOME']=r'%s';import engine;"
            "print(engine.acquire_scheduler_lock())") % (ROOT, HOME)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout.strip()
    assert out == "False", f"第二個行程也拿到了鎖：{out}"
    return "第二個行程拿不到排程鎖"


# ---------------------------------------------------------------- 需要網路 / 本地模型
def t_fetch():
    sys.path.insert(0, str(HOME / "skills"))
    import fetch_url
    t = fetch_url.run("https://www.ctee.com.tw/news/20260820700837-430804")
    assert "開闔次選單" not in t and "OKX" in t, t[:200]
    assert fetch_url.run("file:///etc/hosts").startswith("只接受")
    return "抓網頁只留正文；擋 file://"


def t_search():
    sys.path.insert(0, str(HOME / "skills"))
    import web_search
    links = web_search._bing_url("https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly9sbXN0dWRpby5haS8&ntb=1")
    assert links == "https://lmstudio.ai/", links                    # Bing 轉址解碼（不需要網路）
    r = web_search.run("LM Studio")
    if "暫時擋了" in r:
        raise Skip("DuckDuckGo 和 Bing 都擋了這台機器的請求（外部服務，不是程式問題）")
    assert r.count("http") >= 3, r[:200]
    return "有結果（" + r.rsplit("搜尋引擎：", 1)[-1].rstrip("）") + "）"


def lm_ready(model):
    if not model:
        return "沒給 --model"
    if not engine.ping_provider("lmstudio").get("ok"):
        return "LM Studio 沒開"
    return None


def t_local_run(model):
    wf = {"title": "檢查", "provider": "lmstudio", "model": model, "skills": [], "system": "只回一句話。",
          "task": "回覆：檢查成功", "max_steps": 3}
    rid = engine.run_workflow("check-run", "manual", "", wf)
    with engine.db() as c:
        r = dict(c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone())
    assert r["status"] == "success" and r["output"], r
    return r["output"][:30]


def t_local_fallback(model):
    c = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
    c["local_fallback"] = {"enabled": True, "provider": "lmstudio", "model": model}
    engine.save_config(c)
    wf = {"title": "檢查", "provider": "deepseek", "skills": [], "system": "只回一句話。", "task": "回覆：備用成功", "max_steps": 3}
    rid = engine.run_workflow("check-lf", "manual", "", wf)
    with engine.db() as db:
        r = dict(db.execute("SELECT status, provider FROM runs WHERE id=?", (rid,)).fetchone())
    c["local_fallback"]["enabled"] = False
    engine.save_config(c)
    assert r["status"] == "success" and r["provider"] == "lmstudio", r
    return "DeepSeek 失敗後改用本地模型完成"


def t_cancel(model):
    wf = {"title": "檢查", "provider": "lmstudio", "model": model, "skills": [], "system": "", "max_steps": 3,
          "task": "寫一篇 1500 字的文章介紹 CSS。"}
    engine.start_async("check-cancel", "manual", "", wf)
    for _ in range(200):
        live = next((v for v in engine.LIVE.values() if v["workflow"] == "check-cancel"), None)
        if live and live.get("phase") == "writing":
            break
        time.sleep(0.2)
    t0 = time.time()
    engine.cancel("check-cancel")
    while any(v["workflow"] == "check-cancel" for v in engine.LIVE.values()):
        time.sleep(0.1)
        assert time.time() - t0 < 10, "10 秒內沒有停下來"
    with engine.db() as c:
        st = c.execute("SELECT status FROM runs WHERE workflow='check-cancel' ORDER BY id DESC").fetchone()[0]
    assert st == "cancelled", st
    return f"{time.time() - t0:.1f} 秒停下"


def t_cli(model):
    env = {**os.environ, "AUTOWORKFLOW_HOME": str(HOME)}
    out = subprocess.run([sys.executable, str(ROOT / "app.py"), "list"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert out.returncode == 0 and "研究助理" in out.stdout
    bad = subprocess.run([sys.executable, str(ROOT / "app.py"), "run", "nope"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert bad.returncode == 2
    if model:
        r = subprocess.run([sys.executable, str(ROOT / "app.py"), "run", "tech-news", "--provider", "lmstudio", "--model", model,
                            "--json", "-i", "（檢查：只要回一句「檢查成功」，不要用任何工具）"],
                           capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
        d = json.loads(r.stdout)
        assert r.returncode in (0, 1) and d["status"] in ("success", "failed"), r.stdout[-300:]
        return f"list / 錯誤代碼 2 / run --json（{d['status']}）"
    return "list / 錯誤代碼 2"


def t_export():
    html = "<!doctype html><html><head><meta charset='utf-8'></head><body><h1>檢查</h1><p>中文 <b>粗體</b></p></body></html>"
    data, ext = server.convert(html, "docx")
    assert data[:2] == b"PK" or ext == "doc", ext
    if server.find_browser():
        pdf, ext2 = server.convert(html, "pdf")
        assert pdf[:5] == b"%PDF-", pdf[:10]
        return f"Word（{ext}）、PDF 都能產生"
    return f"Word（{ext}）；找不到瀏覽器，PDF 略過"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="用來測試的 LM Studio 模型 ID（建議挑小的）")
    ap.add_argument("--no-network", action="store_true")
    a = ap.parse_args()
    engine.init_db()
    net = "指定了 --no-network" if a.no_network else None
    lm = lm_ready(a.model)

    check("第一次啟動的預設值", t_seed)
    check("設定驗證", t_settings_validation)
    check("API key 保護", t_api_key_masking)
    check("工作流新增 / 編輯 / 刪除", t_workflow_crud)
    check("擋跨站請求", t_http_csrf)
    check("skill 匯入與刪除", t_skill_import)
    check("從 GitHub 匯入 skill", lambda: t_skill_github(a.no_network), skip=net)
    check("工具呼叫 JSON 解析", t_tool_json_parse)
    check("自動模式", t_auto_pick)
    check("排程鎖", t_scheduler_lock)
    check("抓網頁", t_fetch, skip=net)
    check("搜尋", t_search, skip=net)
    check("匯出 Word / PDF", t_export)
    check("本地模型執行", lambda: t_local_run(a.model), skip=lm)
    check("本地備用", lambda: t_local_fallback(a.model), skip=lm)
    check("停止", lambda: t_cancel(a.model), skip=lm)
    check("命令列", lambda: t_cli(None if lm else a.model))

    w = max(len(n) for _, n, _ in results)
    for st, name, detail in results:
        mark = {"通過": "✓", "失敗": "✕", "略過": "–"}[st]
        print(f" {mark} {name.ljust(w)}  {detail}")
    bad = sum(1 for st, *_ in results if st == "失敗")
    print(f"\n{len(results)} 項：通過 {sum(1 for st, *_ in results if st == '通過')}、失敗 {bad}、"
          f"略過 {sum(1 for st, *_ in results if st == '略過')}　（暫存資料夾：{HOME}）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
