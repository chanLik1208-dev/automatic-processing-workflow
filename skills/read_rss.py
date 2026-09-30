import re, urllib.request
import xml.etree.ElementTree as ET

# 跟一般瀏覽器一樣的請求標頭：寫明是 AI 工具的請求，有些網站會拒絕或給不一樣的內容
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/141.0.0.0 Safari/537.36",
           "Accept": "application/rss+xml,application/atom+xml,application/xml;q=0.9,text/xml;q=0.8,*/*;q=0.5",
           "Accept-Language": "zh-TW,zh-HK;q=0.9,zh;q=0.8,en;q=0.7"}

SPEC = {
    "name": "read_rss",
    "description": "讀 RSS / Atom feed，回傳最新幾則的標題、連結、摘要。",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string"},
        "limit": {"type": "integer", "description": "幾則，預設 10"}},
        "required": ["url"]},
}


def _t(el, *names):
    for n in names:
        x = el.find(n)
        if x is not None:
            return (x.text or x.get("href") or "").strip()
    return ""


def run(url, limit=10):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    ns = "{http://www.w3.org/2005/Atom}"
    items = root.findall(".//item") or root.findall(f".//{ns}entry")
    out = []
    for i, it in enumerate(items[:limit], 1):
        title = _t(it, "title", f"{ns}title")
        link = _t(it, "link", f"{ns}link")
        desc = re.sub(r"<[^>]+>", "", _t(it, "description", f"{ns}summary"))[:300]
        out.append(f"{i}. {title}\n   {link}\n   {desc}")
    return "\n".join(out) or "（feed 是空的）"
