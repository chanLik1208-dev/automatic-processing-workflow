import base64, html, http.cookiejar, importlib.util, json, pathlib, random, re, sys, threading, time, urllib.parse, urllib.request

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


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
           "Accept-Language": "zh-TW,zh-HK;q=0.9,zh;q=0.8,en;q=0.7"}


# 像同一個瀏覽器：保留搜尋引擎發的 cookie，而不是每次都像全新的陌生訪客
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
_pace = {"last": 0.0, "lock": threading.Lock()}
GAP = (2.0, 4.0)        # 兩次搜尋之間至少隔幾秒（隨機）；連續秒搜十幾次是被當成機器人的主因


def _wait_turn():
    """跟上一次搜尋保持間隔。模型常常一口氣丟好幾組關鍵字，這裡把它們排開。"""
    with _pace["lock"]:
        gap = random.uniform(*GAP) - (time.time() - _pace["last"])
        if gap > 0:
            time.sleep(gap)
        _pace["last"] = time.time()


def _open(req):
    with _OPENER.open(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def _clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _real_url(href):
    # DuckDuckGo 的結果連結是 //duckduckgo.com/l/?uddg=<真正網址>
    q = urllib.parse.urlparse(html.unescape(href)).query
    return urllib.parse.parse_qs(q).get("uddg", [html.unescape(href)])[0]


def _duckduckgo(query, limit):
    data = urllib.parse.urlencode({"q": query, "kl": _cfg("search.region", "tw-tzh")}).encode()
    req = urllib.request.Request("https://html.duckduckgo.com/html/", data=data, headers=HEADERS)
    return _duckduckgo_parse(query, _open(req), limit)


def _duckduckgo_parse(query, page, limit):
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


def _bing_query_url(query):
    region = _cfg("search.region", "tw-tzh")
    cc = {"tw": "TW", "hk": "HK", "us": "US", "cn": "CN", "jp": "JP"}.get(region.split("-")[0], "")
    return "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "zh-hant" if "tzh" in region else "", "cc": cc})


def _bing(query, limit):
    return _bing_parse(query, _open(urllib.request.Request(_bing_query_url(query), headers=HEADERS)), limit)


def _search_browser(query, limit):
    """用使用者自己的瀏覽器搜尋（設定「用我的瀏覽器讀網頁」打開時）：程式直接發的請求被當成機器人擋掉時，
    真的瀏覽器通常不會被擋。瀏覽器的部分跟 fetch_url 共用（同一個專用資料夾、只讀取頁面）。
    先試 DuckDuckGo 的純 HTML 版（幾乎沒有 JavaScript，幾秒就好），再試 Bing；每個都限時，不讓一次搜尋卡很久。"""
    browser_dom = _fetch_url_module()._browser_dom
    blocked, errs = [], []
    ddg = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query, "kl": _cfg("search.region", "tw-tzh")})
    for name, url, secs, parse in (("DuckDuckGo", ddg, 20, _duckduckgo_parse), ("Bing", _bing_query_url(query), 25, _bing_parse)):
        dom, err = browser_dom(url, secs)
        if err:
            errs.append(f"{name}：{err}")
            continue
        try:
            got = parse(query, dom, limit)
        except Blocked:
            blocked.append(name)
            continue
        if got:
            return got, f"{name}（用你的瀏覽器）"
    if blocked and not errs:
        raise Blocked("、".join(blocked))
    raise RuntimeError("；".join(errs + [f"{b}：也被擋" for b in blocked]) or "沒有結果")


def _fetch_url_module():
    """同一個資料夾裡的 fetch_url（skill 是用檔案路徑載入的，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("_aw_fetch_url", pathlib.Path(__file__).with_name("fetch_url.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bing_parse(query, page, limit):
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


def _search(query, limit):
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
    return results, engine, blocked


def run(query, limit=None):
    limit = max(1, min(15, int(limit or _cfg("search.limit", 8))))
    _wait_turn()
    results, engine, blocked = _search(query, limit)
    if not results and blocked:
        # 被擋通常是短時間的限流：停一下再試一次，還是被擋才照實回報
        time.sleep(random.uniform(8, 12))
        _pace["last"] = time.time()
        results, engine, blocked = _search(query, limit)
    browser_err, tried_browser = "", False
    if not results and blocked and _browser_on():
        tried_browser = True
        try:
            results, engine = _search_browser(query, limit)
        except Blocked as e:
            results, browser_err = [], f"{e} 也把瀏覽器當成機器人擋了"
        except Exception as e:
            browser_err = str(e)[:160]
    if not results and blocked:
        # 一定要講清楚是「被擋」，不然模型會把「搜不到」當成「沒有這回事」寫進結論
        return (f"[搜尋被擋] {'、'.join(blocked)} 把這次搜尋當成機器人擋了，這不代表沒有相關資料。"
                "不要據此下「查無資料」的結論；可以改用已知的網址直接讀，或在報告裡註明搜尋受阻。"
                + (f"（也試了用使用者的瀏覽器搜尋：{browser_err or '一樣沒有結果'}。）"
                   + (f"\n下一步：用 ask_user_browser 請使用者打開 {_bing_query_url(query)}、通過驗證，就能讀回搜尋結果"
                      "（reason 寫「搜尋引擎要驗證，請通過後停在搜尋結果頁」）。使用者按跳過就不要再請他，改用已知網址或照實註明。"
                      if _can_ask() else "")
                   if tried_browser
                   else "" if _browser_on() else
                   "（提示使用者：這個工作流沒有被允許「用你的瀏覽器讀網頁」，可以在工作流的權限裡允許）"
                   if _cfg("browser.enabled", False) else
                   "（提示使用者：設定頁打開「用我的瀏覽器讀網頁」後，被擋時會改用使用者自己的瀏覽器搜尋）"))
    if not results:
        return f"搜尋「{query}」沒有結果"
    return "\n".join(f"{i}. {t}\n   {u}\n   {sn}" for i, (t, u, sn) in enumerate(results, 1)) + f"\n（搜尋引擎：{engine}）"
