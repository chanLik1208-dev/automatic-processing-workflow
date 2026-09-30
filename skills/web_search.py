import base64, html, json, pathlib, re, urllib.parse, urllib.request

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
        cur = json.loads((pathlib.Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
        for k in path.split("."):
            cur = cur[k]
        return cur
    except (OSError, ValueError, KeyError, TypeError):
        return default


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
           "Accept-Language": "zh-TW,zh-HK;q=0.9,zh;q=0.8,en;q=0.7"}


def _clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _real_url(href):
    # DuckDuckGo 的結果連結是 //duckduckgo.com/l/?uddg=<真正網址>
    q = urllib.parse.urlparse(html.unescape(href)).query
    return urllib.parse.parse_qs(q).get("uddg", [html.unescape(href)])[0]


def _duckduckgo(query, limit):
    data = urllib.parse.urlencode({"q": query, "kl": _cfg("search.region", "tw-tzh")}).encode()
    req = urllib.request.Request("https://html.duckduckgo.com/html/", data=data, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", errors="replace")
    if "result__a" not in page and re.search(r"anomaly|challenge|bots", page, re.I):
        raise Blocked("DuckDuckGo")                    # 機器人驗證頁，不是真的沒結果
    titles = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    out = []
    for i, (href, title) in enumerate(titles):
        url = _real_url(href)
        if "duckduckgo.com/y.js" in url:   # 廣告
            continue
        out.append((_clean(title), url, _clean(snippets[i]) if i < len(snippets) else ""))
    return out[:limit]


class Blocked(Exception):
    """搜尋引擎把請求當成機器人擋了（驗證頁、或塞一堆無關的結果）。"""


def _terms(text):
    """比對相關性用的詞：英文單字、中文每兩個字一組。"""
    t = text.lower()
    words = set(re.findall(r"[a-z0-9]{3,}", t))
    for run in re.findall(r"[\u4e00-\u9fff]+", t):
        words |= {run[i:i + 2] for i in range(len(run) - 1)} or {run}
    return words


def _relevant(query, results):
    """Bing 被當成機器人時不會回錯誤，而是塞一堆跟關鍵字無關的結果（Costco、醫院掛號…）。
    標題和摘要跟關鍵字一個詞都對不上的就丟掉。"""
    q = _terms(re.sub(r'\b(OR|AND|site:\S+)\b|["()]', " ", query))
    return [r for r in results if not q or q & _terms(r[0] + " " + r[2])]


def _bing_url(href):
    # Bing 的連結是 /ck/a?...&u=a1<base64url 的真正網址>
    u = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query).get("u", [""])[0]
    if u.startswith("a1"):
        b = u[2:] + "=" * (-len(u[2:]) % 4)
        try:
            return base64.urlsafe_b64decode(b).decode("utf-8", errors="replace")
        except ValueError:
            pass
    return html.unescape(href)


def _bing(query, limit):
    region = _cfg("search.region", "tw-tzh")
    cc = {"tw": "TW", "hk": "HK", "us": "US", "cn": "CN", "jp": "JP"}.get(region.split("-")[0], "")
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "zh-hant" if "tzh" in region else "", "cc": cc})
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", errors="replace")
    out = []
    for it in re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', page, re.S):
        a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', it, re.S)
        p = re.search(r'<p[^>]*>(.*?)</p>', it, re.S)
        if a:
            snip = re.sub(r"\s*…?\s*(深入閱讀|閱讀更多|Read more)\s*$", "", _clean(p.group(1))) if p else ""
            out.append((_clean(a.group(2)), _bing_url(a.group(1)), snip))
    kept = _relevant(query, out)
    if out and not kept:
        raise Blocked("Bing")                           # 全部無關：當成被擋，不能把垃圾交給模型
    return kept[:limit]


def run(query, limit=None):
    limit = max(1, min(15, int(limit or _cfg("search.limit", 8))))
    results, engine, blocked = [], "", []
    # DuckDuckGo 偶爾會把請求當成機器人擋掉（短時間搜很多次、共用網路、雲端機器最常見）；擋掉就改用 Bing
    for name, fn in (("DuckDuckGo", _duckduckgo), ("Bing", _bing)):
        try:
            results = fn(query, limit)
        except Blocked:
            blocked.append(name)
            results = []
        except Exception:
            results = []
        if results:
            engine = name
            break
    if not results and blocked:
        # 一定要講清楚是「被擋」，不然模型會把「搜不到」當成「沒有這回事」寫進結論
        return (f"[搜尋被擋] {'、'.join(blocked)} 把這次搜尋當成機器人擋了，這不代表沒有相關資料。"
                "不要據此下「查無資料」的結論；可以改用已知的網址直接讀，或在報告裡註明搜尋受阻。")
    if not results:
        return f"搜尋「{query}」沒有結果"
    return "\n".join(f"{i}. {t}\n   {u}\n   {sn}" for i, (t, u, sn) in enumerate(results, 1)) + f"\n（搜尋引擎：{engine}）"
