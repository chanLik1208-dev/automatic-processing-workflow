import html, json, pathlib, re, urllib.parse, urllib.request

SPEC = {
    "name": "web_search",
    "description": "上網搜尋，回傳前幾筆結果的標題、網址、摘要。不知道網址時先用這個，找到後再用 fetch_url 讀內文。",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "搜尋關鍵字"},
        "limit": {"type": "integer", "description": "幾筆，最多 15"}},
        "required": ["query"]},
}


def _cfg(path, default):
    """讀使用者設定（設定頁存的 config.json）。"""
    try:
        cur = json.loads((pathlib.Path(__file__).parent.parent / "config.json").read_text())
        for k in path.split("."):
            cur = cur[k]
        return cur
    except (OSError, ValueError, KeyError, TypeError):
        return default


def _clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _real_url(href):
    # DuckDuckGo 的結果連結是 //duckduckgo.com/l/?uddg=<真正網址>
    q = urllib.parse.urlparse(html.unescape(href)).query
    return urllib.parse.parse_qs(q).get("uddg", [html.unescape(href)])[0]


def run(query, limit=None):
    limit = max(1, min(15, int(limit or _cfg("search.limit", 8))))
    data = urllib.parse.urlencode({"q": query, "kl": _cfg("search.region", "tw-tzh")}).encode()
    req = urllib.request.Request("https://html.duckduckgo.com/html/", data=data, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/126 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", errors="replace")
    titles = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    out = []
    for i, (href, title) in enumerate(titles):
        url = _real_url(href)
        if "duckduckgo.com/y.js" in url:   # 廣告
            continue
        out.append(f"{len(out) + 1}. {_clean(title)}\n   {url}\n   {_clean(snippets[i]) if i < len(snippets) else ''}")
        if len(out) >= limit:
            break
    return "\n".join(out) or f"搜尋「{query}」沒有結果（也可能是搜尋引擎暫時擋了請求）"
