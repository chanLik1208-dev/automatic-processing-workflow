"""監控面板 + API。啟動：python3 server.py"""
import json
import threading
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
                v = dict(v)
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
