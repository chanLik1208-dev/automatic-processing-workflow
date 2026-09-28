import time, urllib.error, urllib.request

SPEC = {
    "name": "http_check",
    "description": "檢查一個網址能不能連上，回傳 HTTP 狀態碼、延遲、回應開頭。",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string"},
        "expect": {"type": "string", "description": "回應裡應該出現的字（選填）"}},
        "required": ["url"]},
}


def run(url, expect=""):
    if not url.lower().startswith(("http://", "https://")):
        return "只接受 http:// 或 https:// 網址"
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ai-workflow-healthcheck"})
        with urllib.request.urlopen(req, timeout=10) as r:
            code, body = r.status, r.read(2000).decode(errors="replace")
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read(500).decode(errors="replace")
    except Exception as e:
        return f"DOWN {url}：{type(e).__name__}: {e}（{int((time.time()-t0)*1000)} ms）"
    ms = int((time.time() - t0) * 1000)
    ok = 200 <= code < 400 and (not expect or expect in body)
    miss = f"，但找不到「{expect}」" if expect and expect not in body else ""
    return f"{'OK' if ok else 'PROBLEM'} {url}：HTTP {code}，{ms} ms{miss}\n回應開頭：{body[:200]}"
