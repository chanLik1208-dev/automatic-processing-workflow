import hashlib, importlib.util, pathlib, urllib.request

SPEC = {
    "name": "view_image",
    "description": "看一張網路上的圖片（例如 fetch_url 列出的圖片網址）：下載後讓你直接看到圖片內容。"
                   "只在圖片跟任務有關時用（例如商品實拍、圖表、截圖），不要每張都看；每次執行能看的張數有上限（設定頁可調）。",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string", "description": "圖片網址（http 或 https）"}},
        "required": ["url"]},
}

MARK = "[[AW_IMAGE]]"         # 主程式看到這個開頭，就把圖片接進對話給模型看（不是給模型讀的文字）
MAX_BYTES = 10 * 1024 * 1024
TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def _sniff(data):
    """看檔案開頭判斷格式（有些網站的 Content-Type 亂寫）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def _headers():
    """跟 fetch_url 一樣的瀏覽器標頭（不自稱 AI，免得被擋或拿到不同內容）。"""
    try:
        spec = importlib.util.spec_from_file_location("_aw_fetch_url", pathlib.Path(__file__).with_name("fetch_url.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return {**mod.BROWSER_HEADERS, "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8"}
    except Exception:
        return {"User-Agent": "Mozilla/5.0"}


def run(url):
    url = str(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.lower().startswith(("http://", "https://")):
        return "只接受 http:// 或 https:// 的圖片網址"
    req = urllib.request.Request(url, headers={**_headers(), "Referer": url})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(MAX_BYTES + 1)
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if len(data) > MAX_BYTES:
        return "圖片太大（超過 10 MB），不看這張。"
    kind = _sniff(data) or (ctype if ctype in TYPES else "")
    if not kind:
        return (f"這個網址不是模型看得懂的圖片（{ctype or '未知格式'}；只支援 png、jpg、webp、gif）。"
                "請換一張，不要當作已經看過。")
    folder = pathlib.Path(__file__).parent.parent / "uploads" / "web"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha1(url.encode()).hexdigest()[:16] + TYPES[kind])
    path.write_bytes(data)
    return f"{MARK}{path}\n{url}\n{len(data):,} bytes"
