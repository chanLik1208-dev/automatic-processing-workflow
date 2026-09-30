import html, json, pathlib, re, urllib.request
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


def run(url, max_chars=None):
    max_chars = int(max_chars or _cfg("fetch.max_chars", 6000))
    if not url.lower().startswith(("http://", "https://")):
        return "只接受 http:// 或 https:// 網址"
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
        return (f"抓不到這個網頁的正文（標題：{title or '無'}）。這類頁面通常要執行 JavaScript 才會出現內容，"
                "請換一個來源，不要當作已經讀過。")
    head = [f"標題：{title}" if title else "", f"日期：{date}" if date else "",
            f"摘要：{desc}" if desc and desc.rstrip(".…")[:40] not in text else "",
            f"（已略過 {hidden} 個一般人看不到的區塊）" if hidden else ""]
    out = "\n".join(h for h in head if h)
    return (out + "\n---\n" + text if out else text)[:max_chars]
