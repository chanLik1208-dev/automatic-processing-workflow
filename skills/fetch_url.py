import html, json, os, pathlib, re, shutil, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from html.parser import HTMLParser

SPEC = {
    "name": "fetch_url",
    "description": "抓一個網頁，回傳標題、日期和正文（已去掉選單、頁首頁尾、側欄）。",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string"},
        "max_chars": {"type": "integer", "description": "最多回傳幾個字（不填就用設定值）"}},
        "required": ["url"]},
}

# 跟一般瀏覽器一樣的請求標頭。寫明「ai-workflow」或只送一個 User-Agent 的請求，
# 有些網站會直接拒絕（實測 Reuters 401、Medium 403），或給 AI 看不一樣的內容，研究結果就會偏掉
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/141.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh-HK;q=0.9,zh;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
}

# 這些區塊幾乎不會是正文
DROP = r"script|style|noscript|svg|iframe|form|nav|header|footer|aside|button|select|template"
BLOCK = r"p|div|li|h[1-6]|tr|section|article|blockquote|pre|figcaption|dd|dt"
SENTENCE = re.compile(r"[。！？；，、.!?;:：]")


def _cfg(path, default):
    """讀使用者設定（設定頁存的 config.json）。"""
    try:
        cur = json.loads((pathlib.Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
        for k in path.split("."):
            cur = cur[k]
        return cur
    except (OSError, ValueError, KeyError, TypeError):
        return default


def _browser_on():
    """設定裡打開了「用我的瀏覽器讀網頁」，而且這個工作流被允許用（權限）。"""
    if not _cfg("browser.enabled", False):
        return False
    eng = sys.modules.get("engine")
    return eng.run_permission("browser") if eng and hasattr(eng, "run_permission") else True


def _can_ask():
    """這次執行能不能請使用者在瀏覽器裡協助（ask_user_browser 有開放）。"""
    eng = sys.modules.get("engine")
    return bool(eng and hasattr(eng, "run_has_tool") and eng.run_has_tool("ask_user_browser"))


def _meta(page, *names):
    for n in names:
        m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]*content=["\']([^"\']+)', page, re.I) \
            or re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']{re.escape(n)}["\']', page, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def _largest(page, tag):
    """同名區塊可能有好幾個（例如側欄也用 <article>），挑文字最多的那個。"""
    blocks = re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", page, re.S | re.I)
    return max(blocks, key=lambda b: len(re.sub(r"<[^>]+>", "", b)), default="")


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])", re.I)


class _Visible(HTMLParser):
    """把一般人在瀏覽器裡看不到的部分拿掉（hidden、aria-hidden、display:none…）。
    有些網頁會把「只給 AI 看」的指令藏在這些地方，叫模型改結論、忽略資訊；人看不到，模型卻會照單全收。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out, self.skip, self.removed = [], 0, 0

    @staticmethod
    def _hidden(attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        return ("hidden" in a or a.get("aria-hidden", "").lower() == "true"
                or bool(HIDDEN_STYLE.search(a.get("style", ""))) or a.get("type", "").lower() == "hidden")

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            if not self.skip:
                self.out.append(self.get_starttag_text())
            return
        if self.skip or self._hidden(attrs):
            self.skip += 1
            self.removed += self.skip == 1
            return
        self.out.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if not self.skip:
            self.out.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.skip:
            self.skip -= 1
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)

    def handle_entityref(self, name):
        if not self.skip:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skip:
            self.out.append(f"&#{name};")


def _visible_only(page):
    p = _Visible()
    try:
        p.feed(page)
        p.close()
    except Exception:
        return page, 0                  # 解析不了就用原本的（寧可多讀，不要整頁讀不到）
    return "".join(p.out), p.removed


def _to_lines(fragment):
    fragment = re.sub(rf"(?is)<({DROP})\b.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?i)</t[dh]\s*>", " | ", fragment)   # 表格的格子用「|」隔開，整列留在同一行
    fragment = re.sub(rf"(?i)</?({BLOCK})\b[^>]*>|<br\s*/?>", "\n", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return [re.sub(r"[ \t　\xa0]+", " ", l).strip() for l in text.split("\n")]


# 很短但有資料的行：價格、數量和單位、日期、「欄位：值」、表格列（例如商品頁的「¥199.00」「顏色分類：黑色」「月銷 2000+」）
DATA = re.compile(r"[¥￥$€£₩]\s*\d|\d\s*(?:元|块|塊|%|％|折|cm|mm|kg|g|ml|L|吋|寸|碼|码|件|个|個|天|週|周|月|年|日|分|星|萬|万|千|\+)"
                  r"|\d{4}[-/.年]\d{1,2}|^[^\s：:|]{1,12}[：:]\s*\S|\S\s\|\s\S")
MENU = re.compile(r"^(首页|首頁|登录|登入|注册|註冊|购物车|購物車|收藏|分享|客服|返回|更多|下载|下載|关注|關注|搜索|搜尋)\b")


def _keep(line):
    # 選單項目通常很短、沒有標點；正文是有標點的句子。短的行只留帶著資料的（價格、規格、日期、表格）
    if len(line) >= 40 or (len(line) >= 12 and SENTENCE.search(line)):
        return True
    if len(line) >= 6 and re.search(r"[，。！？；]", line) and not MENU.search(line):
        return True                                       # 短評論（「很保暖，偏大一碼」）：選單幾乎不會有中文標點
    return 2 <= len(line) <= 200 and bool(DATA.search(line)) and not MENU.search(line)


def _pdf(data, max_chars):
    """PDF 要先轉成文字；直接當文字解碼只會得到一堆亂碼（%PDF-1.5 ...），模型卻以為讀過了。"""
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages, text = len(reader.pages), []
        for i, page in enumerate(reader.pages[:40]):
            t = (page.extract_text() or "").strip()
            if t:
                text.append(f"［第 {i + 1} 頁］\n{t}")
            if sum(map(len, text)) > max_chars:
                break
    except Exception as e:
        return f"這是 PDF，但讀不出文字（{type(e).__name__}）。請換一個來源，不要當作已經讀過。"
    body = "\n".join(text)
    if not body.strip():
        return "這是 PDF，但裡面沒有文字（可能是掃描的圖片）。請換一個來源，不要當作已經讀過。"
    return (f"（PDF，共 {pages} 頁）\n" + body)[:max_chars]


# ---------- 用使用者登入過的瀏覽器讀（要執行 JavaScript、要登入的網頁） ----------
# 設定頁「用我的瀏覽器讀網頁」打開、並按「登入瀏覽器」登入過需要的網站後才會用。
# 用的是這個程式專用的瀏覽器資料夾，不碰使用者平常的 Chrome（裡面有支付、信箱等登入）；
# 只用 --dump-dom 取得載入完的網頁內容：不能點、不能打字、不能付款。
PROFILE = pathlib.Path(__file__).parent.parent / "browser-profile"


def find_browser():
    custom = os.path.expanduser(_cfg("export.browser_path", "") or "")
    if custom and os.path.exists(custom):
        return custom
    if sys.platform == "darwin":
        cands = [f"/Applications/{a}.app/Contents/MacOS/{a}" for a in ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser")]
    elif sys.platform == "win32":
        pf = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        cands = [os.path.join(b, x) for b in pf if b for x in
                 (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe")]
    else:
        cands = [shutil.which(n) or "" for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge")]
    return next((c for c in cands if c and os.path.exists(c)), None)


def _profile_in_use():
    """登入視窗還開著嗎？同一個瀏覽器資料夾一次只能一個瀏覽器用：開著時背景讀取會被交給那個視窗、拿到空的。
    Chrome 用 SingletonLock（指向「主機名-行程編號」的捷徑）標記；行程還活著才算。"""
    lock = PROFILE / "SingletonLock"
    try:
        target = os.readlink(lock)
    except OSError:
        return False
    try:
        pid = int(target.rsplit("-", 1)[-1])
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:                     # 行程在、只是不是我們的
        return True
    except OSError:
        return False


# Chrome 自己的錯誤頁（打不開、憑證錯誤、斷線）：不是網站內容，不能當正文交給模型
CHROME_ERROR = re.compile(r'id="main-frame-error"|class="interstitial-wrapper"|id="error-code"|chrome-error://|class="neterror"|jstcache.*?net::ERR_', re.I | re.S)
NET_ERR = re.compile(r"net::(ERR_[A-Z_]+)|\b(ERR_[A-Z][A-Z_]{4,}|DNS_PROBE_[A-Z_]+)\b")


def _chrome_error(dom):
    if not CHROME_ERROR.search(dom[:200000]):
        return ""
    m = NET_ERR.search(dom)
    return _net_error((m.group(1) or m.group(2)) if m else "未知錯誤")


def _net_error(code):
    hint = {"ERR_NAME_NOT_RESOLVED": "網址的網域找不到", "DNS_PROBE_FINISHED_NXDOMAIN": "網址的網域找不到", "ERR_INTERNET_DISCONNECTED": "電腦沒有連上網路",
            "ERR_CONNECTION_REFUSED": "網站拒絕連線", "ERR_CONNECTION_TIMED_OUT": "連線逾時",
            "ERR_CERT_AUTHORITY_INVALID": "網站憑證不被信任（公司網路或防毒軟體攔截 HTTPS 時常見）",
            "ERR_CERT_COMMON_NAME_INVALID": "網站憑證跟網址不符", "ERR_PROXY_CONNECTION_FAILED": "代理伺服器連不上",
            "ERR_TOO_MANY_REDIRECTS": "網站一直轉址"}.get(code, "")
    return f"瀏覽器打不開這個網址（{code}{'：' + hint if hint else ''}）"


# 網站要求驗證或登入（滑塊、驗證碼、登入頁）：內容不是使用者要的，請使用者在登入視窗處理一次
WALL = re.compile(r"滑块|滑塊|拖动|拖動|验证码|驗證碼|安全验证|安全驗證|人机验证|captcha|are you a robot|verify you are human|"
                  r"unusual traffic|请登录|請登入|密码登录|密碼登入|扫码登录|掃碼登入|login\.taobao\.com|passport\.|/punish", re.I)


def _wall(text):
    """正文很短、又出現驗證或登入的字樣：判定為被擋在驗證／登入頁。"""
    return len(text) < 1500 and bool(WALL.search(text))


# ---------- DevTools：開瀏覽器、把整頁瀏覽一遍（捲到底讓延遲載入的評論、圖片都出來）、讀畫面上的文字 ----------
# 只做這幾件事：開網址、捲動、讀內容、關掉。不點擊、不輸入、不付款。DevTools 埠只聽本機（127.0.0.1）。

def cdp_json(port, path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def ws_call(ws_url, method, params=None, timeout=30):
    """極簡 WebSocket：送一個 DevTools 指令、收它的回覆。標準庫沒有 WebSocket，所以自己做握手和分框。"""
    import base64, socket, struct
    u = urllib.parse.urlparse(ws_url)
    s = socket.create_connection((u.hostname, u.port), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise RuntimeError("瀏覽器沒有接受連線")
            buf += chunk
        head, buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("瀏覽器拒絕連線")
        payload = json.dumps({"id": 1, "method": method, "params": params or {}}).encode()
        n = len(payload)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
                                if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

        def need(k):
            nonlocal buf
            while len(buf) < k:
                chunk = s.recv(1 << 20)
                if not chunk:
                    raise RuntimeError("連線中斷")
                buf += chunk
            out, buf = buf[:k], buf[k:]
            return out

        msg = b""
        while True:
            b0, b1 = need(2)
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", need(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", need(8))[0]
            data = need(ln)
            if b0 & 0x0F == 8:
                raise RuntimeError("瀏覽器關閉了連線")
            msg += data
            if b0 & 0x80:
                got = json.loads(msg)
                msg = b""
                if got.get("id") == 1:
                    return got
    finally:
        s.close()


SCROLL_JS = ("(async()=>{const h=()=>Math.min(document.documentElement.scrollHeight,30000);"
             "for(let y=0;y<h();y+=700){window.scrollTo(0,y);await new Promise(r=>setTimeout(r,200));}"
             "window.scrollTo(0,h());await new Promise(r=>setTimeout(r,1200));window.scrollTo(0,0);return true})()")


def read_rendered(ws_url, deadline):
    """等網頁載完 → 從頭捲到底再捲回來（延遲載入的評論、規格、圖片都載進來）→ 回傳 (網址, HTML, 畫面上的文字)。"""
    def ev(expr, timeout=15, wait=False):
        got = ws_call(ws_url, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": wait},
                      timeout=timeout)
        return ((got.get("result") or {}).get("result") or {}).get("value")
    # 跳轉中分頁是一份空的 about:blank，也算「載完」：要等到真的網頁（或 Chrome 的錯誤頁）載完
    while time.time() < deadline:
        try:
            if ev("(location.protocol.startsWith('http')||location.protocol==='chrome-error:')"
                  "&&document.readyState==='complete'", timeout=5):
                break
        except (OSError, RuntimeError):
            pass
        time.sleep(0.4)
    # 捲動事件要在畫面更新時才送出，背景分頁不更新畫面：先把分頁拉到前面，捲動才會觸發延遲載入
    try:
        ws_call(ws_url, "Page.bringToFront", timeout=5)
    except (OSError, RuntimeError):
        pass
    time.sleep(0.8)                                        # 讓網頁的腳本把內容畫出來
    try:
        ev(SCROLL_JS, timeout=max(5, min(60, deadline - time.time())), wait=True)
    except (OSError, RuntimeError):
        pass
    return (ev("location.href") or "", ev("document.documentElement.outerHTML") or "",
            ev("document.body ? document.body.innerText : ''") or "")


def _browser_read(url, limit=60):
    """用專用瀏覽器資料夾、看不見的視窗讀一頁：瀏覽一遍讓全部東西載入，再讀。回傳 (網址, HTML, 畫面上的文字, 錯誤訊息)。"""
    browser = find_browser()
    if not browser:
        return "", "", "", "找不到 Chrome / Edge，沒辦法用瀏覽器讀"
    PROFILE.mkdir(parents=True, exist_ok=True)
    if _profile_in_use():
        return "", "", "", "「登入視窗」還開著（同一個瀏覽器資料夾一次只能一個瀏覽器用）：登入完把那個視窗整個關掉（macOS 要按 ⌘Q）再試"
    port_file = PROFILE / "DevToolsActivePort"
    try:
        port_file.unlink()
    except OSError:
        pass
    # Linux 用 root 跑時（容器、CI）Chrome 不肯在沙盒裡啟動；一般使用者不會走到這裡
    root = ["--no-sandbox"] if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0 else []
    # 背景更新、同步、元件下載都關掉：只是讀一頁，不要順便去改 Chrome 本身（macOS 會當成「修改 App」來問）
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", *root,
           "--disable-background-networking", "--disable-component-update", "--disable-sync",
           f"--user-data-dir={PROFILE}", "--remote-debugging-port=0", "--window-size=1280,2000", "about:blank"]
    try:
        # 自己一個行程群組：結束時連 Chrome 的子行程（繪圖、網路）一起關掉，不會留在背景
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                start_new_session=sys.platform != "win32")
    except OSError as e:
        return "", "", "", f"瀏覽器開不起來：{e}"
    deadline = time.time() + limit
    port = None
    try:
        while time.time() < deadline and proc.poll() is None:
            try:
                port = int(port_file.read_text().split()[0])
                break
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        if not port:
            why = proc.stderr.read(4000).decode("utf-8", "replace") if proc.poll() is not None else ""
            last = [l for l in why.splitlines() if "ERROR" in l][-1:]
            return "", "", "", "瀏覽器沒有啟動" + (f"（{last[0][-160:]}）" if last else "")
        target = cdp_json(port, "/json/new?" + urllib.parse.quote(url, safe=""), method="PUT")
        final, dom, text = read_rendered(target["webSocketDebuggerUrl"], deadline)
    except Exception as e:
        return "", "", "", f"瀏覽器讀取失敗：{e}"
    finally:
        if port:
            try:
                ws_call(cdp_json(port, "/json/version")["webSocketDebuggerUrl"], "Browser.close", timeout=5)
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if proc.poll() is None:
            if sys.platform != "win32":
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    pass
            else:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            proc.wait()
    bad = _chrome_error(dom)
    if bad:
        return final, "", "", bad
    if not dom.strip():
        return final, "", "", f"瀏覽器讀了 {limit} 秒還沒讀完" if time.time() >= deadline else "瀏覽器沒有回傳內容"
    return final, dom, text, ""


def _browser_dom(url, limit=60):
    """回傳 (html, 錯誤訊息)：給需要 HTML 的地方用（例如搜尋結果頁的解析）。"""
    _, dom, _, err = _browser_read(url, limit)
    return dom, err


def open_login_window(url="about:blank"):
    """給使用者登入用：用同一個專用資料夾開一個一般的瀏覽器視窗。"""
    browser = find_browser()
    if not browser:
        raise RuntimeError("找不到 Chrome / Edge")
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([browser, f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check", url],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


NO_TEXT = "抓不到這個網頁的正文"


def run(url, max_chars=None):
    max_chars = int(max_chars or default_chars())
    if not url.lower().startswith(("http://", "https://")):
        return "只接受 http:// 或 https:// 網址"
    use_browser = _browser_on()
    try:
        out = _fetch(url, max_chars)
    except urllib.error.HTTPError as e:
        if not use_browser or e.code not in (401, 403):
            raise
        out = f"{NO_TEXT}（HTTP {e.code}）"
    if not use_browser or not out.startswith(NO_TEXT):
        return out
    # 一般讀法拿不到正文（要執行 JavaScript、要登入）：改用使用者登入過的瀏覽器，把整頁瀏覽一遍再讀畫面上的文字
    final, dom, shown, err = _browser_read(url)
    if err:
        return out + f"\n（也試了用你的瀏覽器讀：{err}）"
    got = from_rendered(shown, dom, max_chars, final or url) if shown.strip() else _from_html(dom, max_chars, final or url)
    if got.startswith(NO_TEXT):
        return got.replace("這類頁面通常要執行 JavaScript 才會出現內容", "用你的瀏覽器讀也沒有正文（可能要登入、被驗證擋住，或內容要點擊才出現）")
    if _wall(got):
        nxt = (f"下一步：這頁對任務重要的話，用 ask_user_browser 請使用者打開 {url}、登入或通過驗證後停在要讀的頁面，就能讀回來；"
               "使用者按跳過就不要再請他。" if _can_ask() else
               "請使用者到設定頁按「打開登入視窗」，在那個視窗處理後再執行一次。")
        return (f"{NO_TEXT}：網站要求驗證或登入（用你的瀏覽器讀到的是驗證／登入頁）。{nxt}"
                f"不要把這頁當成已經讀過。\n（讀到的內容：{got[:300]}）")
    return ("（用你登入的瀏覽器讀取）\n" + got)[:max_chars]


def _fetch(url, max_chars):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        ctype = r.headers.get("Content-Type", "")
        data = r.read(30 * 1024 * 1024)
        charset = r.headers.get_content_charset()
    if "pdf" in ctype.lower() or data[:5] == b"%PDF-":
        return _pdf(data, max_chars)
    kind = ctype.split(";")[0].strip().lower()
    if kind.startswith(("image/", "audio/", "video/", "font/")) or kind in ("application/zip", "application/octet-stream"):
        return f"這是 {kind} 檔案，不是文字，沒辦法讀內容。請換一個來源，不要當作已經讀過。"
    raw = data.decode(charset or "utf-8", errors="replace")
    if "html" not in ctype and not raw.lstrip().startswith("<"):
        return raw[:max_chars]          # JSON、純文字等直接回傳
    return _from_html(raw, max_chars, url)


IMG_SKIP = re.compile(r"logo|icon|avatar|sprite|pixel|spacer|blank|emoji|badge|tracking|beacon|/ads?/|banner|placeholder|loading", re.I)


URL_SIZE = re.compile(r"(?:-tps-|[_-])(\d{2,4})[-x](\d{2,4})(?=[._q]|$)")


def _images(fragment, base, limit=12):
    """正文裡的圖片（網址 + 說明），給模型挑著用 view_image 看。跳過圖示、logo、追蹤用的小圖、base64 內嵌圖。"""
    out, seen = [], set()
    for tag in re.findall(r"<img\b[^>]*>", fragment, re.I):
        attrs = dict((k.lower(), html.unescape(v)) for k, _, v in re.findall(r'([\w:-]+)\s*=\s*(["\'])(.*?)\2', tag, re.S))
        src = attrs.get("data-src") or attrs.get("data-original") or attrs.get("data-lazy-src") or attrs.get("src") or ""
        if not src and attrs.get("srcset"):
            src = attrs["srcset"].split(",")[0].strip().split(" ")[0]
        src = src.strip()
        if not src or src.startswith("data:") or src.lower().split("?")[0].endswith(".svg") or IMG_SKIP.search(src):
            continue
        try:
            if any(int(re.sub(r"\D", "", attrs.get(k, "999") or "999") or 999) < 80 for k in ("width", "height")):
                continue                                    # 太小的多半是圖示或追蹤點
        except ValueError:
            pass
        m = URL_SIZE.search(src)
        if m and min(int(m.group(1)), int(m.group(2))) < 200:
            continue                                        # 網址裡寫著尺寸、而且很小（例如淘寶的 -tps-172-108）：徽章、按鈕圖
        full = urllib.parse.urljoin(base, src) if base else src
        if full in seen or not full.lower().startswith(("http://", "https://")):
            continue
        seen.add(full)
        out.append((re.sub(r"\s+", " ", attrs.get("alt") or attrs.get("title") or "").strip()[:80], full))
        if len(out) >= limit:
            break
    return out


def default_chars():
    """這次讀網頁最多幾字：設定值；篇幅調到 xhigh / max 時主程式會放大（讀得更細）。"""
    eng = sys.modules.get("engine")
    got = getattr(getattr(eng, "_ctx", None), "fetch_chars", None) if eng else None
    return int(got or os.environ.get("AW_FETCH_CHARS") or _cfg("fetch.max_chars", 10000))


def _structured(raw):
    """網站自己提供的結構化資料（schema.org JSON-LD）：商品的價格、評分、評論數、品牌等，比從畫面上抓更準。"""
    out = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
            return
        if not isinstance(o, dict):
            return
        t = o.get("@type")
        t = " ".join(t) if isinstance(t, list) else str(t or "")
        if any(k in t for k in ("Product", "Offer", "AggregateRating", "Review", "Book", "Recipe", "Event", "Article")):
            parts = []
            for k in ("name", "brand", "sku", "price", "lowPrice", "highPrice", "priceCurrency", "availability",
                      "ratingValue", "reviewCount", "ratingCount", "datePublished", "author", "reviewBody", "description"):
                v = o.get(k)
                if isinstance(v, dict):
                    v = v.get("name") or v.get("ratingValue")
                if v not in (None, "", []) and not isinstance(v, (dict, list)):
                    parts.append(f"{k}={str(v).strip()[:300]}")
            if parts:
                out.append(f"{t}：" + "；".join(parts))
        for v in o.values():
            if isinstance(v, (dict, list)):
                walk(v)

    for block in re.findall(r"(?is)<script[^>]*application/ld\+json[^>]*>(.*?)</script>", raw)[:10]:
        try:
            walk(json.loads(html.unescape(block.strip())))
        except ValueError:
            continue
    return out[:15]


def _from_html(raw, max_chars, url=""):
    data = _structured(raw)
    raw, hidden = _visible_only(raw)

    title = _meta(raw, "og:title", "twitter:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        title = html.unescape(m.group(1)).strip() if m else ""
    date = _meta(raw, "article:published_time", "pubdate", "date", "og:updated_time")
    desc = _meta(raw, "og:description", "description")

    # 先找正文容器；找不到才用整個 body
    for tag in ("article", "main"):
        part = _largest(raw, tag)
        if len(re.sub(r"<[^>]+>", "", part)) > 300:
            body = part
            break
    else:
        m = re.search(r"<body\b[^>]*>(.*)</body>", raw, re.S | re.I)
        body = m.group(1) if m else raw

    seen, lines = set(), []
    for l in _to_lines(body):
        if _keep(l) and l not in seen:
            seen.add(l)
            lines.append(l)
    text = "\n".join(lines)
    if len(text) < 200:                  # 過濾太兇、幾乎什麼都沒留下時，退回寬鬆版本
        text = "\n".join(l for l in _to_lines(body) if len(l) >= 4)

    if not text.strip():
        return (f"{NO_TEXT}（標題：{title or '無'}）。這類頁面通常要執行 JavaScript 才會出現內容，"
                "請換一個來源，不要當作已經讀過。")
    head = [f"標題：{title}" if title else "", f"日期：{date}" if date else "",
            f"摘要：{desc}" if desc and desc.rstrip(".…")[:40] not in text else "",
            f"（已略過 {hidden} 個一般人看不到的區塊）" if hidden else "",
            ("網站提供的結構化資料：\n" + "\n".join(data)) if data else ""]
    out = "\n".join(h for h in head if h)
    tail = _image_tail(raw, body, url)
    return (out + "\n---\n" + text if out else text)[:max(max_chars - len(tail), 500)] + tail


def _image_tail(raw, body, url):
    """結果最後的圖片清單（頁面主圖 + 正文圖片；正文圖少時整頁補）。"""
    pics = _images(body, url)
    if len(pics) < 3:                                    # 正文區塊裡圖少（例如商品頁的主圖在別的區塊）：整頁再找一次
        seen = {u for _, u in pics}
        pics += [x for x in _images(raw, url) if x[1] not in seen][:12 - len(pics)]
    og = _meta(raw, "og:image")                         # 網站自己標的主圖（商品頁通常就是商品照）
    og = urllib.parse.urljoin(url, og) if og and url else og
    if og and og.lower().startswith(("http://", "https://")) and not IMG_SKIP.search(og) and all(u != og for _, u in pics):
        pics = [("頁面主圖", og)] + pics[:11]
    return ("\n\n圖片（商品實拍、尺寸表、圖表等常有文字裡沒有的資訊；跟任務有關的用 view_image 看，不要每張都看）：\n" +
            "\n".join(f"{i}. {alt or '（沒有說明）'} — {src}" for i, (alt, src) in enumerate(pics, 1))) if pics else ""


def from_rendered(text, raw, max_chars, url=""):
    """瀏覽器畫面上實際顯示的文字（innerText）＋原始 HTML（拿標題、結構化資料、圖片）。
    要執行腳本才畫出來的網頁（淘寶的價格、評論）用這個比從 HTML 重組文字完整：看得到的才會在 innerText 裡。"""
    data = _structured(raw)
    vis, _ = _visible_only(raw)
    title = _meta(vis, "og:title", "twitter:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", vis, re.S | re.I)
        title = html.unescape(m.group(1)).strip() if m else ""
    seen, lines = set(), []
    for l in (re.sub(r"[ \t　\xa0]+", " ", x).strip() for x in (text or "").split("\n")):
        # 畫面上的文字本來就是給人看的：除了選單字眼和太短的碎片，都留下（價格、評論、規格常常很短）
        if l and l not in seen and not MENU.search(l) and (len(l) >= 4 or DATA.search(l)):
            seen.add(l)
            lines.append(l)
    body = "\n".join(lines)
    if not body.strip():
        return f"{NO_TEXT}（標題：{title or '無'}）。畫面上沒有文字，請換一個來源，不要當作已經讀過。"
    head = "\n".join(h for h in (f"標題：{title}" if title else "",
                                  ("網站提供的結構化資料：\n" + "\n".join(data)) if data else "") if h)
    m = re.search(r"<body\b[^>]*>(.*)</body>", vis, re.S | re.I)
    tail = _image_tail(vis, m.group(1) if m else vis, url)
    return ((head + "\n---\n" if head else "") + body)[:max(max_chars - len(tail), 500)] + tail
