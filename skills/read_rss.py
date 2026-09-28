import re, urllib.request
import xml.etree.ElementTree as ET

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
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ai-workflow"})
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
