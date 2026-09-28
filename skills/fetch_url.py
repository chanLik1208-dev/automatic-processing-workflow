import html, json, pathlib, re, urllib.request

SPEC = {
    "name": "fetch_url",
    "description": "抓一個網頁，回傳標題、日期和正文（已去掉選單、頁首頁尾、側欄）。",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string"},
        "max_chars": {"type": "integer", "description": "最多回傳幾個字（不填就用設定值）"}},
        "required": ["url"]},
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


def _to_lines(fragment):
    fragment = re.sub(rf"(?is)<({DROP})\b.*?</\1>", " ", fragment)
    fragment = re.sub(rf"(?i)</?({BLOCK})\b[^>]*>|<br\s*/?>", "\n", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return [re.sub(r"[ \t　\xa0]+", " ", l).strip() for l in text.split("\n")]


def _keep(line):
    # 選單項目通常很短、沒有標點；正文是有標點的句子
    return len(line) >= 40 or (len(line) >= 12 and SENTENCE.search(line))


def run(url, max_chars=None):
    max_chars = int(max_chars or _cfg("fetch.max_chars", 6000))
    if not url.lower().startswith(("http://", "https://")):
        return "只接受 http:// 或 https:// 網址"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) ai-workflow"})
    with urllib.request.urlopen(req, timeout=20) as r:
        ctype = r.headers.get("Content-Type", "")
        raw = r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    if "html" not in ctype and not raw.lstrip().startswith("<"):
        return raw[:max_chars]          # JSON、純文字等直接回傳

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
            f"摘要：{desc}" if desc and desc.rstrip(".…")[:40] not in text else ""]
    out = "\n".join(h for h in head if h)
    return (out + "\n---\n" + text if out else text)[:max_chars]
