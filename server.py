"""監控面板 + API。啟動：python3 server.py"""
import base64
import json
import os
import sys
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import engine
import settings_api
import skill_admin


def workflows_view():
    out = []
    with engine.db() as c:
        for name, wf in engine.load_workflows().items():
            last = c.execute("SELECT id, status, started, finished, trigger FROM runs WHERE workflow=? "
                             "ORDER BY id DESC LIMIT 1", (name,)).fetchone()
            stats = c.execute("SELECT COUNT(*), SUM(status='success'), SUM(status='failed') FROM runs "
                              "WHERE workflow=?", (name,)).fetchone()
            due = engine.next_due(wf, engine.last_scheduled_start(name)) if wf["enabled"] else None
            out.append({
                "name": name, "title": wf.get("title", name), "description": wf.get("description", ""),
                "system": wf.get("system", ""), "task": wf.get("task", ""), "max_steps": wf.get("max_steps"),
                "provider": wf.get("provider"), "model": wf.get("model"), "fallback": wf.get("fallback"),
                "schedule": wf.get("schedule"), "skills": wf.get("skills", []), "enabled": wf["enabled"],
                "running": name in engine._running,
                "last": dict(last) if last else None,
                "total": stats[0], "ok": stats[1] or 0, "failed": stats[2] or 0,
                "next_due": due.timestamp() if due else None,
            })
    return out


def find_browser():
    """PDF 要靠 Chromium 系的瀏覽器印；設定裡有指定就用指定的，否則 Windows 用 Edge，macOS / Linux 找常見的幾個。"""
    custom = engine.cfg("export.browser_path", "")
    if custom and os.path.exists(os.path.expanduser(custom)):
        return os.path.expanduser(custom)
    if sys.platform == "darwin":
        cands = [f"/Applications/{a}.app/Contents/MacOS/{a}" for a in
                 ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser")]
    elif sys.platform == "win32":
        pf = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        cands = [os.path.join(b, p) for b in pf if b for p in
                 (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe")]
    else:
        cands = [shutil.which(n) or "" for n in
                 ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge")]
    return next((c for c in cands if c and os.path.exists(c)), None)


def convert(html, fmt):
    """把前端組好的完整 HTML 轉成 Word 或 PDF，回傳 (bytes, 副檔名)。"""
    with tempfile.TemporaryDirectory() as d:
        src = pathlib.Path(d) / "report.html"
        src.write_text(html, encoding="utf-8")
        out = pathlib.Path(d) / f"report.{fmt}"
        if fmt == "docx":
            if sys.platform == "darwin" and shutil.which("textutil"):
                subprocess.run(["textutil", "-convert", "docx", str(src), "-output", str(out)],
                               check=True, capture_output=True, timeout=60)
                return out.read_bytes(), "docx"
            soffice = shutil.which("soffice") or shutil.which("libreoffice")
            if soffice:
                subprocess.run([soffice, "--headless", "--convert-to", "docx", "--outdir", d, str(src)],
                               check=True, capture_output=True, timeout=120)
                return out.read_bytes(), "docx"
            # 沒有轉檔工具：輸出 Word 認得的 HTML 格式 .doc（Word、LibreOffice、Pages 都打得開）
            doc = html.replace("<html", '<html xmlns:o="urn:schemas-microsoft-com:office:office" '
                                       'xmlns:w="urn:schemas-microsoft-com:office:word"', 1)
            return doc.encode("utf-8"), "doc"
        browser = find_browser()
        if not browser:
            raise RuntimeError("找不到 Chrome / Edge / Chromium，沒辦法轉 PDF；可以改用「網頁」匯出再列印成 PDF")
        # Chrome headless 寫完 PDF 常常不會自己結束，所以等檔案寫好、大小穩定就直接關掉它
        p = subprocess.Popen([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                              f"--user-data-dir={d}/profile", f"--print-to-pdf={out}", src.as_uri()],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            last, deadline = -1, time.time() + 60
            while time.time() < deadline:
                size = out.stat().st_size if out.exists() else -1
                if size > 0 and size == last:
                    break
                if p.poll() is not None and size > 0:
                    break
                last = size
                time.sleep(0.5)
            else:
                raise RuntimeError("PDF 轉換逾時")
        finally:
            p.kill()
            p.wait()
        return out.read_bytes(), "pdf"


class Api:
    """所有路由。HTTP 伺服器（headless / 瀏覽器模式）和原生視窗的橋接都呼叫這裡，
    差別只在回應怎麼送出去（send / send_file 由子類別實作）。"""

    def send(self, obj, code=200, ctype="application/json"):
        raise NotImplementedError

    def send_file(self, data, headers):
        raise NotImplementedError

    def route_get(self, raw_path):
        u = urlparse(raw_path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self.send((engine.APP_DIR / "dashboard.html").read_bytes(), ctype="text/html")
        if u.path == "/api/workflows":
            return self.send(workflows_view())
        if u.path == "/api/live":
            out = []
            for v in list(engine.LIVE.values()):
                v = {k: x for k, x in v.items() if not k.startswith("_")}   # _resp 之類的內部物件不外露
                v["reasoning"] = (v.get("reasoning") or "")[-6000:]
                v["content"] = (v.get("content") or "")[-6000:]
                out.append(v)
            return self.send(out)
        if u.path == "/api/usage":
            week = time.time() - 7 * 86400
            return self.send({"today": engine.usage(), "week": engine.usage(since=week), "cli_limits": engine.cli_limits()})
        if u.path == "/api/instance":
            return self.send({"data_dir": str(engine.ROOT.resolve())})
        if u.path == "/api/settings":
            return self.send(settings_api.masked())
        if u.path == "/api/providers":
            return self.send({n: engine.ping_provider(n) for n, p in engine.load_config()["providers"].items()
                              if p.get("enabled", True)})
        if u.path == "/api/skills":
            return self.send(skill_admin.list_skills())
        if u.path == "/api/runs":
            wf = q.get("workflow", [None])[0]
            with engine.db() as c:
                sql = "SELECT id, workflow, provider, model, trigger, status, started, finished, error, " \
                      "tokens_in, tokens_out FROM runs"
                rows = c.execute(sql + (" WHERE workflow=?" if wf else "") + " ORDER BY id DESC LIMIT 50",
                                 (wf,) if wf else ()).fetchall()
            return self.send([dict(r) for r in rows])
        if u.path.startswith("/api/runs/"):
            rid = int(u.path.rsplit("/", 1)[1])
            with engine.db() as c:
                run = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
                steps = c.execute("SELECT * FROM steps WHERE run_id=? ORDER BY idx, id", (rid,)).fetchall()
            if not run:
                return self.send({"error": "not found"}, 404)
            return self.send({"run": dict(run), "steps": [dict(s) for s in steps]})
        self.send({"error": "not found"}, 404)

    def route_post(self, raw_path, body):
        u = urlparse(raw_path)
        parts = u.path.strip("/").split("/")
        if parts == ["api", "export"]:
            fmt, html = body.get("format"), body.get("html") or ""
            if fmt not in ("docx", "pdf") or not html or len(html) > 5_000_000:
                return self.send({"error": "格式不支援或內容太大"}, 400)
            try:
                data, ext = convert(html, fmt)
            except Exception as e:
                return self.send({"error": f"轉換失敗：{e}"}, 500)
            base = str(body.get("filename") or "report").rsplit(".", 1)[0]
            return self.send_file(data, {
                "X-Export-Ext": ext,
                "Content-Type": {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                 "doc": "application/msword", "pdf": "application/pdf"}[ext],
                "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(f'{base}.{ext}')}"})
        if parts == ["api", "settings"]:
            try:
                return self.send({"ok": True, "settings": settings_api.save(body)})
            except (ValueError, TypeError) as e:
                return self.send({"error": str(e)}, 400)
        if parts[:2] == ["api", "skills"] or parts[:2] == ["api", "builder"]:
            try:
                if parts == ["api", "skills", "import"]:
                    return self.send({"ok": True, "imported": skill_admin.import_skill(body)})
                if parts == ["api", "builder", "chat"]:
                    return self.send({"ok": True, **skill_admin.builder_chat(body)})
                if parts == ["api", "builder", "save"]:
                    return self.send({"ok": True, "name": skill_admin.builder_save(body)})
                if len(parts) == 4 and parts[1] == "skills" and parts[3] == "delete":
                    return self.send({"ok": True, "result": skill_admin.delete_skill(parts[2])})
                if len(parts) == 4 and parts[1] == "skills" and parts[3] == "run":
                    wf = skill_admin.adhoc_workflow(parts[2], body.get("provider") or "claude", body.get("model") or "")
                    name = f"skill:{parts[2]}"
                    return self.send({"ok": True, "started": engine.start_async(name, "manual", body.get("input", ""), wf),
                                      "workflow": name})
            except Exception as e:
                return self.send({"error": str(e)}, 400)
            return self.send({"error": "not found"}, 404)
        if parts[:2] != ["api", "workflows"] or len(parts) < 3:
            return self.send({"error": "not found"}, 404)
        name = parts[2]
        try:
            if len(parts) == 3 and name == "new":
                return self.send({"ok": True, "workflow": engine.save_workflow(body.get("name"), body, create=True)})
            if name not in engine.load_workflows():
                return self.send({"error": "no such workflow"}, 404)
            action = parts[3] if len(parts) == 4 else ""
            if action == "run":
                return self.send({"started": engine.start_async(name, "manual", body.get("input", ""))})
            if action == "stop":
                return self.send({"ok": engine.cancel(name)})
            if action == "save":
                return self.send({"ok": True, "workflow": engine.save_workflow(name, body)})
            if action == "enabled":
                wf = {**engine.load_workflows()[name], "enabled": bool(body.get("enabled"))}
                return self.send({"ok": True, "workflow": engine.save_workflow(name, wf)})
            if action == "delete":
                if name in engine._running:
                    return self.send({"error": "執行中的工作流不能刪"}, 409)
                return self.send({"ok": True, "moved_to": engine.trash_workflow(name)})
        except (ValueError, TypeError) as e:
            return self.send({"error": str(e)}, 400)
        self.send({"error": "not found"}, 404)


class H(Api, BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, data, headers):
        self.send_response(200)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.route_get(self.path)

    def do_POST(self):
        # 要求 JSON content-type（跨站請求會先被 CORS preflight 擋下），且 Origin 必須是自己
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if "application/json" not in (self.headers.get("Content-Type") or "") or \
                (origin and origin not in (f"http://{host}", f"https://{host}")):
            return self.send({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send({"error": "body 不是 JSON"}, 400)
        self.route_post(self.path, body)


class Captured(Api):
    """原生視窗用：不經過網路，直接把回應收下來交給介面。"""

    def __init__(self):
        self.result = None

    def send(self, obj, code=200, ctype="application/json"):
        if isinstance(obj, bytes):
            self.result = {"status": code, "ctype": ctype, "text": obj.decode("utf-8", "replace")}
        else:
            self.result = {"status": code, "ctype": ctype, "json": obj}

    def send_file(self, data, headers):
        self.result = {"status": 200, "ctype": headers.get("Content-Type"), "headers": headers,
                       "b64": base64.b64encode(data).decode()}


def call(method, path, body=None):
    c = Captured()
    try:
        c.route_get(path) if method == "GET" else c.route_post(path, body or {})
    except Exception as e:
        c.send({"error": f"內部錯誤：{e}"}, 500)
    return c.result or {"status": 500, "json": {"error": "沒有回應"}}


def serve(port=None, on_ready=None):
    engine.init_db()
    threading.Thread(target=engine.scheduler_loop, daemon=True).start()
    cfg = engine.load_config()["server"]
    httpd = ThreadingHTTPServer((cfg["host"], port or cfg["port"]), H)
    url = f"http://{cfg['host']}:{httpd.server_port}"
    print(f"監控面板：{url}\n資料夾：{engine.ROOT}")
    if on_ready:
        on_ready(url)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
