"""監控面板 + API。啟動：python3 server.py"""
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import engine


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


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def convert(html, fmt):
    """把前端組好的完整 HTML 轉成 docx（textutil）或 pdf（Chrome headless），回傳 bytes。"""
    with tempfile.TemporaryDirectory() as d:
        src = pathlib.Path(d) / "report.html"
        src.write_text(html, encoding="utf-8")
        out = pathlib.Path(d) / f"report.{fmt}"
        if fmt == "docx":
            subprocess.run(["textutil", "-convert", "docx", str(src), "-output", str(out)],
                           check=True, capture_output=True, timeout=60)
            return out.read_bytes()
        if not os.path.exists(CHROME):
            raise RuntimeError("找不到 Google Chrome，沒辦法轉 PDF；可以改用「列印 → 另存為 PDF」")
        # Chrome headless 寫完 PDF 常常不會自己結束，所以等檔案寫好、大小穩定就直接關掉它
        p = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
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
        return out.read_bytes()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self.send((engine.ROOT / "dashboard.html").read_bytes(), ctype="text/html")
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
        if u.path == "/api/providers":
            return self.send({n: engine.ping_provider(n) for n in engine.load_config()["providers"]})
        if u.path == "/api/skills":
            return self.send([m.SPEC for m in engine.load_skills().values()])
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

    def do_POST(self):
        # 要求 JSON content-type（跨站請求會先被 CORS preflight 擋下），且 Origin 必須是自己
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if "application/json" not in (self.headers.get("Content-Type") or "") or \
                (origin and origin not in (f"http://{host}", f"https://{host}")):
            return self.send({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send({"error": "body 不是 JSON"}, 400)
        if parts == ["api", "export"]:
            fmt, html = body.get("format"), body.get("html") or ""
            if fmt not in ("docx", "pdf") or not html or len(html) > 5_000_000:
                return self.send({"error": "格式不支援或內容太大"}, 400)
            try:
                data = convert(html, fmt)
            except Exception as e:
                return self.send({"error": f"轉換失敗：{e}"}, 500)
            name = urllib.parse.quote(str(body.get("filename") or f"report.{fmt}"))
            self.send_response(200)
            self.send_header("Content-Type", {"docx": "application/vnd.openxmlformats-officedocument."
                                                      "wordprocessingml.document", "pdf": "application/pdf"}[fmt])
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{name}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
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


if __name__ == "__main__":
    engine.init_db()
    threading.Thread(target=engine.scheduler_loop, daemon=True).start()
    cfg = engine.load_config()["server"]
    print(f"監控面板：http://{cfg['host']}:{cfg['port']}")
    ThreadingHTTPServer((cfg["host"], cfg["port"]), H).serve_forever()
