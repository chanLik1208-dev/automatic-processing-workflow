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
import updater


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
                "max_tokens": wf.get("max_tokens"), "fallback_model": wf.get("fallback_model"),
                "depth": wf.get("depth", engine.DEPTH_DEFAULT),
                "provider": wf.get("provider"), "model": wf.get("model"), "fallback": wf.get("fallback"),
                "schedule": wf.get("schedule"), "skills": wf.get("skills", []), "enabled": wf["enabled"],
                "running": name in engine._running,
                "last": dict(last) if last else None,
                "total": stats[0], "ok": stats[1] or 0, "failed": stats[2] or 0,
                "next_due": due.timestamp() if due else None,
                "permissions": engine.permission_view(name, wf),
            })
    return out


MAX_IMAGES, MAX_IMAGE_BYTES = 8, 10 * 1024 * 1024


def save_attachments(body):
    """介面送來的附件 → 引擎要的格式。圖片是 base64，存進資料夾的 uploads/（之後重新生成還用得到）。"""
    att = {"folder": str(body.get("folder") or "").strip(), "images": []}
    imgs = body.get("images") or []
    if len(imgs) > MAX_IMAGES:
        raise ValueError(f"一次最多附 {MAX_IMAGES} 張圖片")
    if imgs:
        d = engine.ROOT / "uploads" / time.strftime("%Y%m%d-%H%M%S")
        d.mkdir(parents=True, exist_ok=True)
        for i, im in enumerate(imgs):
            name = pathlib.Path(str(im.get("name") or f"image{i}.png")).name
            ext = pathlib.Path(name).suffix.lower()
            if ext not in engine.IMAGE_TYPES:
                raise ValueError(f"不支援的圖片格式：{name}（可以用 png、jpg、webp、gif）")
            try:
                data = base64.b64decode(str(im.get("data") or "").split(",", 1)[-1], validate=True)
            except ValueError:
                raise ValueError(f"圖片資料壞掉了：{name}")
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError(f"圖片太大：{name}（每張最多 10 MB）")
            f = d / f"{i + 1:02d}-{name}"
            f.write_bytes(data)
            att["images"].append(str(f))
    return att


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
    # ignore_cleanup_errors：Chrome 被關掉後，子程序可能還在寫暫存資料夾；PDF 已經讀出來了，清不乾淨不能讓匯出失敗
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
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
        profile = tempfile.mkdtemp(prefix="wf-chrome-")          # 瀏覽器設定檔另外放，事後盡量清掉
        p = subprocess.Popen([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                              f"--user-data-dir={profile}", f"--print-to-pdf={out}", src.as_uri()],
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
            data = out.read_bytes()
        finally:
            p.kill()
            p.wait()
            shutil.rmtree(profile, ignore_errors=True)
        return data, "pdf"


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
        if u.path == "/api/depth":                          # 滑桿的五個等級（名稱、說明），介面照這個畫
            return self.send([{"level": k, "label": v["label"], "hint": v["hint"]} for k, v in engine.DEPTH.items()])
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
            conf = engine.load_config()
            return self.send({"today": engine.usage(), "week": engine.usage(since=week), "cli_limits": engine.cli_limits(),
                              "today_models": engine.usage_by_model(),
                              # 訂閱額度（整理好的）：整個來源看最兇的共用視窗；每個 (來源, 模型) 另外算，包含只算那個模型的視窗
                              "quota": {n: engine.quota_summary(n) for n in conf["providers"]},
                              "quota_models": {f"{n}/{m}": engine.quota_summary(n, m) for n, p in conf["providers"].items()
                                               for m in dict.fromkeys([p.get("default_model") or ""] + (p.get("models") or []))}})
        if u.path == "/api/update":
            return self.send(updater.status())
        if u.path == "/api/instance":
            return self.send({"data_dir": str(engine.ROOT.resolve())})
        if u.path == "/api/settings":
            return self.send(settings_api.masked())
        if u.path == "/api/providers":
            st = {n: engine.ping_provider(n) for n, p in engine.load_config()["providers"].items() if p.get("enabled", True)}
            engine.sync_auto_models(st)                       # 訂閱多了新模型：自動加進自動模式（各自一列）
            return self.send(st)
        if u.path == "/api/skills":
            return self.send(skill_admin.list_skills())
        if u.path == "/api/runs":
            wf = q.get("workflow", [None])[0]
            with engine.db() as c:
                sql = "SELECT id, workflow, provider, model, trigger, status, started, finished, error, " \
                      "tokens_in, tokens_out, depth FROM runs"
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
            run = dict(run)
            # 完整對話可能很大，不傳給介面；只告訴它能不能「繼續」，附件和接續關係另外整理
            run["can_continue"] = bool(run.pop("messages", None))
            try:
                params = json.loads(run.pop("params", None) or "{}")
            except ValueError:
                params = {}
            att = params.get("attachments") or {}
            run["attachments"] = {"folder": att.get("folder") or "", "images": [os.path.basename(x) for x in att.get("images") or []]}
            run["parent"] = params.get("parent")
            return self.send({"run": run, "steps": [dict(s) for s in steps]})
        self.send({"error": "not found"}, 404)

    def route_post(self, raw_path, body):
        try:
            return self._route_post(raw_path, body)
        except engine.NeedPermission as e:
            # 手動執行前還有沒問過的權限：介面跳出詢問，答完再執行一次
            return self.send({"error": str(e), "need_permissions": {"workflow": e.name, "items": e.items}}, 428)

    def _route_post(self, raw_path, body):
        u = urlparse(raw_path)
        parts = u.path.strip("/").split("/")
        if parts == ["api", "permissions"]:
            name = str(body.get("workflow") or "")
            if name not in engine.load_workflows() and not name.startswith("skill:"):
                return self.send({"error": "no such workflow"}, 404)
            try:
                notes = engine.set_permissions(name, body.get("decisions") or {})
            except ValueError as e:
                return self.send({"error": str(e)}, 400)
            return self.send({"ok": True, "notes": notes})
        if parts == ["api", "system", "check-folder"]:
            # 附資料夾的當下就去讀一次：macOS 會在這時候跳出權限視窗；被擋就告訴使用者去哪裡打開
            path = os.path.realpath(os.path.expanduser(str(body.get("path") or "").strip()))
            if not os.path.isdir(path):
                return self.send({"ok": False, "error": f"找不到這個資料夾：{path}", "pane": ""})
            err = engine.probe_folder(path)
            pane = "files" if err and sys.platform in engine.SETTINGS_PANES else ""
            return self.send({"ok": not err, "error": err or "", "pane": pane, "path": path})
        if parts == ["api", "system", "open-settings"]:
            try:
                return self.send({"ok": engine.open_system_settings(str(body.get("pane") or ""))})
            except Exception as e:
                return self.send({"error": str(e)}, 400)
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] in ("continue", "regenerate"):
            if not parts[2].isdigit():
                return self.send({"error": "紀錄編號不對"}, 400)
            try:
                if parts[3] == "continue":
                    ok = engine.continue_run(int(parts[2]), body.get("input", ""), save_attachments(body),
                                             body.get("provider"), body.get("model"), body.get("depth"))
                else:
                    ok = engine.regenerate_run(int(parts[2]))
            except ValueError as e:
                return self.send({"error": str(e)}, 400)
            return self.send({"started": ok} if ok else {"error": "這條工作流正在執行，等它跑完再試"}, 200 if ok else 409)
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "delete":
            if not parts[2].isdigit():
                return self.send({"error": "紀錄編號不對"}, 400)
            try:
                return self.send({"ok": True, "moved_reports": engine.delete_run(int(parts[2]))})
            except ValueError as e:
                return self.send({"error": str(e)}, 409)
        if parts == ["api", "browser", "login"]:
            # 「用我的瀏覽器讀網頁」用的專用瀏覽器資料夾（skills/fetch_url.py 讀網頁時用同一個）
            browser = find_browser()
            if not browser:
                return self.send({"error": "找不到 Chrome / Edge；可以在「PDF 用的瀏覽器」填路徑"}, 400)
            profile = engine.ROOT / "browser-profile"
            profile.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([browser, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                              "about:blank"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self.send({"ok": True})
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
        if parts[:2] == ["api", "update"]:
            try:
                if parts == ["api", "update", "check"]:
                    return self.send(updater.check())
                if parts == ["api", "update", "download"]:
                    return self.send(updater.stage())
                if parts == ["api", "update", "restart"]:
                    # 換上新版並重新開啟：先回應，再讓程式結束（小腳本會等它結束才替換）
                    updater.apply_and_restart(sys.argv[1:])
                    threading.Timer(0.8, lambda: os._exit(0)).start()
                    return self.send({"ok": True, "restarting": True})
            except Exception as e:
                return self.send({"error": str(e)}, 400)
            return self.send({"error": "not found"}, 404)
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
            except engine.NeedPermission:
                raise                                       # 交給外層回 428，介面先問權限
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
                wf = engine.with_model(name, body.get("provider"), body.get("model"))   # 這次臨時換模型（可省略）
                return self.send({"started": engine.start_async(name, "manual", body.get("input", ""), wf, depth=body.get("depth"),
                                                                attachments=save_attachments(body))})
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
