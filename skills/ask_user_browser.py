import base64, importlib.util, json, os, pathlib, socket, struct, subprocess, sys, time, urllib.parse, urllib.request, uuid

SPEC = {
    "name": "ask_user_browser",
    "description": "請使用者幫忙打開一個網頁：在使用者的瀏覽器視窗打開網址，請他登入、通過驗證（滑塊、驗證碼）或點到要讀的那一頁，"
                   "他按「完成」後讀回那一頁的內容。只在 fetch_url / web_search 回報被驗證、登入頁或機器人檢查擋住，"
                   "而且這頁對任務真的重要時才用；使用者也可能按「跳過」或沒有回應，那就照實說明拿不到。",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string", "description": "要請使用者打開的網址（http 或 https）"},
        "reason": {"type": "string", "description": "一句話跟使用者說為什麼需要他、要他做什麼（例如「淘寶要求登入，請登入後停在商品頁」）。"
                                                     "需要評論的話請他先打開評論（例如「請點開『全部評價』再按完成」），程式不會替他點。"}},
        "required": ["url", "reason"]},
}

ROOT = pathlib.Path(__file__).parent.parent
ASKS = ROOT / "asks"
WAIT_MINUTES = 10


def _fetch_url():
    spec = importlib.util.spec_from_file_location("_aw_fetch_url", pathlib.Path(__file__).with_name("fetch_url.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_id():
    eng = sys.modules.get("engine")
    rid = getattr(getattr(eng, "_ctx", None), "run_id", None) if eng else None
    return rid or int(os.environ.get("AW_MCP_RUN") or 0)


def _notify(title, message):
    """系統通知（macOS 通知中心、Windows 右下角）：使用者切到別的 App 時也知道要回來處理。
    這是程式自己提醒使用者，不是模型在跳通知，所以不看工作流的「跳桌面通知」權限；設定頁的「桌面通知」關掉就不送。"""
    try:
        spec = importlib.util.spec_from_file_location("_aw_notify", pathlib.Path(__file__).with_name("notify.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.run(title, message)
    except Exception as e:                               # 通知送不出去不影響協助本身（畫面上照樣有卡片）
        return f"通知送不出去：{e}"


def _cancelled(run_id):
    eng = sys.modules.get("engine")
    return bool(eng and run_id in getattr(eng, "_cancel", ()))


# ---------- DevTools（只用來讀使用者停下來的那一頁、最後關掉視窗；不點、不打字）：跟 fetch_url 共用 ----------
_fu = _fetch_url()
_json, _ws_call = _fu.cdp_json, _fu.ws_call


def _close(fu, port):
    """關掉我們開的視窗，等它真的結束（資料夾放開）：之後的背景讀取、下一次協助才用得了同一個資料夾。"""
    try:
        _ws_call(_json(port, "/json/version")["webSocketDebuggerUrl"], "Browser.close", timeout=5)
    except Exception:
        pass
    for _ in range(50):
        if not fu._profile_in_use():
            return
        time.sleep(0.2)


def _launch(fu, url):
    """用專用瀏覽器資料夾開一個看得見的視窗，開 DevTools 埠（只聽本機）讀回內容。回傳埠號。"""
    browser = fu.find_browser()
    if not browser:
        raise RuntimeError("找不到 Chrome / Edge")
    fu.PROFILE.mkdir(parents=True, exist_ok=True)
    port_file = fu.PROFILE / "DevToolsActivePort"
    for _ in range(25):                                   # 上一個視窗剛關、還在結束中：等一下
        if not fu._profile_in_use() or port_file.exists():
            break
        time.sleep(0.2)
    if fu._profile_in_use():
        # 我們上次開的視窗還在：直接開新分頁；使用者自己開的登入視窗（沒有 DevTools 埠）就請他先關掉
        try:
            port = int(port_file.read_text().split()[0])
            _json(port, "/json/new?" + urllib.parse.quote(url, safe=""), method="PUT")
            return port
        except (OSError, ValueError, IndexError):
            raise RuntimeError("「登入視窗」還開著：請先把它整個關掉（macOS 按 ⌘Q），再請模型重試")
    try:
        port_file.unlink()
    except OSError:
        pass
    extra = ["--no-sandbox"] if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0 else []
    if os.environ.get("AW_TEST_HEADLESS"):               # 發佈前檢查：CI 沒有螢幕，用看不見的視窗代替
        extra.append("--headless=new")
    subprocess.Popen([browser, f"--user-data-dir={fu.PROFILE}", "--no-first-run", "--no-default-browser-check",
                      "--remote-debugging-port=0", *extra, url],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            return int(port_file.read_text().split()[0])
        except (OSError, ValueError, IndexError):
            time.sleep(0.2)
    raise RuntimeError("瀏覽器視窗開了，但讀不到它的位置（DevToolsActivePort）")


def _read_and_close(port, want):
    """讀使用者停下來的那一頁（優先挑跟要求的網址同網站的分頁），然後把視窗關掉，之後背景讀取才能用同一個資料夾。"""
    pages = [p for p in _json(port, "/json/list") if p.get("type") == "page" and p.get("url", "").startswith("http")]
    if not pages:
        raise RuntimeError("瀏覽器裡沒有開著的網頁")
    host = urllib.parse.urlparse(want).hostname or ""
    base = ".".join(host.split(".")[-2:])
    page = next((p for p in pages if base and base in (urllib.parse.urlparse(p["url"]).hostname or "")), pages[0])
    # 按「完成」時頁面可能還在載入：等它載完，從頭捲到底讓評論、規格都載進來，再讀畫面上的文字（最多等 60 秒）
    final, html, shown = _fu.read_rendered(page["webSocketDebuggerUrl"], time.time() + 60)
    return final or page["url"], html, shown


def run(url, reason=""):
    url = str(url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return "只接受 http:// 或 https:// 網址"
    fu = _fetch_url()
    rid = _run_id()
    try:
        port = _launch(fu, url)
    except RuntimeError as e:
        return f"[skill 錯誤] 沒辦法請使用者協助：{e}"
    ASKS.mkdir(parents=True, exist_ok=True)
    ask_id = uuid.uuid4().hex[:12]
    path = ASKS / f"{ask_id}.json"
    path.write_text(json.dumps({"id": ask_id, "run_id": rid, "url": url, "reason": str(reason or "")[:300],
                                "status": "waiting", "created": time.time()}, ensure_ascii=False), encoding="utf-8")
    _notify("AutoWorkflow 需要你協助", (str(reason or "") or "網站要求登入或驗證") + "。處理好後回到 AutoWorkflow 按「完成」。")
    status = "timeout"
    try:
        deadline = time.time() + WAIT_MINUTES * 60
        while time.time() < deadline:
            if _cancelled(rid):
                status = "cancelled"
                break
            try:
                status = json.loads(path.read_text(encoding="utf-8")).get("status", "waiting")
            except (OSError, ValueError):
                status = "waiting"
            if status != "waiting":
                break
            time.sleep(0.5)
        else:
            status = "timeout"
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if status != "done":
        _close(fu, port)
        why = {"skip": "使用者按了「跳過」", "timeout": f"使用者 {WAIT_MINUTES} 分鐘內沒有回應",
               "cancelled": "這次執行被停止了"}.get(status, "使用者沒有完成")
        return f"{fu.NO_TEXT}：{why}，沒有讀到 {url}。不要再請使用者協助同一頁，照實說明這頁拿不到。"
    try:
        final, html, shown = _read_and_close(port, url)
    except Exception as e:
        return f"[skill 錯誤] 使用者按了完成，但讀不到那一頁：{e}"
    finally:
        _close(fu, port)
    bad = fu._chrome_error(html)
    if bad:
        return f"{fu.NO_TEXT}：{bad}（使用者停在的頁面：{final}）"
    # 用畫面上實際顯示的文字（innerText）；拿不到才退回從 HTML 重組
    got = fu.from_rendered(shown, html, fu.default_chars(), final) if shown.strip() else fu._from_html(html, fu.default_chars(), final)
    if got.startswith(fu.NO_TEXT):
        return f"{got}\n（使用者協助打開的頁面：{final}）"
    return (f"（使用者在瀏覽器裡協助打開的頁面：{final}）\n" + got)[:fu.default_chars() + 200]
