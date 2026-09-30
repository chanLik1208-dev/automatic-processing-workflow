"""原生視窗：用系統內建的 WebView 顯示介面，介面和 Python 直接互相呼叫，不開任何 port。"""
import base64
import threading
import webbrowser

import webview

import engine
import server


def _own_page():
    """pywebview 會把 js_api 提供給視窗裡「任何」頁面。萬一視窗被導到外部網站，那個網站就能呼叫這些函式，
    所以每次呼叫都確認現在顯示的還是我們自己的介面（用 html 字串載入的頁面，網址不會是 http/https/file）。"""
    try:
        url = webview.windows[0].get_current_url() or ""
    except Exception:
        return False
    return not url.lower().startswith(("http:", "https:", "file:"))


class Bridge:
    """介面呼叫得到的 Python 函式（window.pywebview.api.*）。"""

    def request(self, method, path, body=None):
        if not _own_page():
            return {"status": 403, "json": {"error": "拒絕：呼叫不是來自本程式的介面"}}
        return server.call(method, path, body)

    def open_url(self, url):
        if _own_page() and str(url).lower().startswith(("http://", "https://")):
            webbrowser.open(url)

    def pick_folder(self):
        """「＋」選單的「選擇資料夾…」：系統的選資料夾視窗，回傳路徑（取消回傳空字串）。"""
        if not _own_page():
            return ""
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        res = webview.windows[0].create_file_dialog(kind)
        if not res:
            return ""
        return res if isinstance(res, str) else res[0]

    def save_file(self, name, b64):
        if not _own_page():
            return None
        kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
        res = webview.windows[0].create_file_dialog(kind, save_filename=name)
        if not res:
            return None
        path = res if isinstance(res, str) else res[0]
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return path


def run():
    engine.init_db()
    threading.Thread(target=engine.scheduler_loop, daemon=True).start()
    html = (engine.APP_DIR / "dashboard.html").read_text(encoding="utf-8")
    html = html.replace("<head>", "<head><script>window.__NATIVE__ = true</script>", 1)
    webview.create_window("自動化工作流", html=html, js_api=Bridge(), width=1320, height=900, min_size=(900, 600))
    webview.start()
