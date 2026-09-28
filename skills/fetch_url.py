import html, re, urllib.request

SPEC = {
    "name": "fetch_url",
    "description": "抓一個網頁，回傳去掉 HTML 標籤後的純文字。",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string"},
        "max_chars": {"type": "integer", "description": "最多回傳幾個字，預設 6000"}},
        "required": ["url"]},
}


def run(url, max_chars=6000):
    if not url.lower().startswith(("http://", "https://")):
        return "只接受 http:// 或 https:// 網址"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ai-workflow"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text).strip()[:max_chars]
