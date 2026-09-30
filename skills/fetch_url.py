import html, json, os, pathlib, re, shutil, subprocess, sys, threading, time, urllib.error, urllib.request
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
    fragment = re.sub(rf"(?i)</?({BLOCK})\b[^>]*>|<br\s*/?>", "\n", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return [re.sub(r"[ \t　\xa0]+", " ", l).strip() for l in text.split("\n")]


def _keep(line):
    # 選單項目通常很短、沒有標點；正文是有標點的句子
    return len(line) >= 40 or (len(line) >= 12 and SENTENCE.search(line))


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


def _browser_dom(url, limit=60):
    """回傳 (html, 錯誤訊息)。limit：最多等幾秒。"""
    browser = find_browser()
    if not browser:
        return "", "找不到 Chrome / Edge，沒辦法用瀏覽器讀"
    PROFILE.mkdir(parents=True, exist_ok=True)
    # Linux 用 root 跑時（容器、CI）Chrome 不肯在沙盒裡啟動；一般使用者不會走到這裡
    root = ["--no-sandbox"] if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0 else []
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", *root,
           f"--user-data-dir={PROFILE}", "--virtual-time-budget=10000", "--dump-dom", url]
    try:
        # 自己一個行程群組：結束時連 Chrome 的子行程（繪圖、網路）一起關掉，不會留在背景
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=sys.platform != "win32")
    except OSError as e:
        return "", f"瀏覽器開不起來：{e}"
    # macOS 上 Chrome 印完網頁常常不會自己結束（匯出 PDF 也有同樣情況）：
    # 邊讀邊收，看到 </html> 而且一秒多沒有新輸出，就當作讀完、把它關掉
    chunks, err, last = [], [], [time.time()]

    def pump(stream, into, mark):
        for b in iter(lambda: stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536), b""):
            into.append(b)
            if mark:
                last[0] = time.time()

    threads = [threading.Thread(target=pump, args=(proc.stdout, chunks, True), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err, False), daemon=True)]
    for t in threads:
        t.start()
    deadline = time.time() + limit
    while proc.poll() is None and time.time() < deadline:
        if b"</html>" in b"".join(chunks[-3:]).lower() and time.time() - last[0] > 1.2:
            break
        time.sleep(0.2)
    timed_out = proc.poll() is None and time.time() >= deadline
    if sys.platform != "win32":
        try:
            os.killpg(proc.pid, 9)           # 子行程也一起（主行程已經結束時，子行程可能還在）
        except OSError:
            pass
    elif proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    proc.wait()
    for t in threads:
        t.join(timeout=2)
    dom = b"".join(chunks).decode("utf-8", errors="replace")
    if not dom.strip():
        if timed_out:
            return "", f"瀏覽器讀了 {limit} 秒還沒讀完"
        # 同一個瀏覽器資料夾只能有一個瀏覽器在用：登入用的視窗還開著時，背景讀取會拿到空的
        why = [l for l in b"".join(err).decode("utf-8", errors="replace").splitlines() if "ERROR" in l or "rror:" in l][-1:]
        return "", "瀏覽器沒有回傳內容；如果「登入瀏覽器」的視窗還開著，先把它關掉再試" + (f"（{why[0][-160:]}）" if why else "")
    return dom, ""


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
    max_chars = int(max_chars or _cfg("fetch.max_chars", 6000))
    if not url.lower().startswith(("http://", "https://")):
        return "只接受 http:// 或 https:// 網址"
    use_browser = bool(_cfg("browser.enabled", False))
    try:
        out = _fetch(url, max_chars)
    except urllib.error.HTTPError as e:
        if not use_browser or e.code not in (401, 403):
            raise
        out = f"{NO_TEXT}（HTTP {e.code}）"
    if not use_browser or not out.startswith(NO_TEXT):
        return out
    # 一般讀法拿不到正文（要執行 JavaScript、要登入）：改用使用者登入過的瀏覽器
    dom, err = _browser_dom(url)
    if err:
        return out + f"\n（也試了用你的瀏覽器讀：{err}）"
    got = _from_html(dom, max_chars)
    if got.startswith(NO_TEXT):
        return got.replace("這類頁面通常要執行 JavaScript 才會出現內容", "用你的瀏覽器讀也沒有正文（可能要登入、被驗證擋住，或內容要點擊才出現）")
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
    return _from_html(raw, max_chars)


def _from_html(raw, max_chars):
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
            f"（已略過 {hidden} 個一般人看不到的區塊）" if hidden else ""]
    out = "\n".join(h for h in head if h)
    return (out + "\n---\n" + text if out else text)[:max_chars]
