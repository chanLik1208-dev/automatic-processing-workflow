import base64, json, re, urllib.error, urllib.request

SPEC = {
    "name": "github_repo",
    "description": "讀一個公開 GitHub repo 的概況：簡介、語言、README、根目錄檔案、最近的 commit。"
                   "要看某個檔案就再給 path。repo 可以是 owner/name 或完整網址。",
    "parameters": {"type": "object", "properties": {
        "repo": {"type": "string", "description": "例如 chanLik1208-dev/learnzen 或 https://github.com/chanLik1208-dev/learnzen"},
        "path": {"type": "string", "description": "選填：要讀的檔案或資料夾路徑，例如 server/index.js 或 src"}},
        "required": ["repo"]},
}

API = "https://api.github.com/repos/"


def _get(url, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": "ai-workflow", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    return body if raw else json.loads(body)


def run(repo, path=""):
    m = re.search(r"(?:github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/|$)", repo.strip())
    if not m:
        return "看不懂這個 repo 名稱，請用 owner/name"
    base = API + f"{m[1]}/{m[2]}"
    try:
        if path:
            data = _get(f"{base}/contents/{path.strip('/')}")
            if isinstance(data, list):
                return "\n".join(f"{'📁' if x['type'] == 'dir' else '📄'} {x['path']}" for x in data)
            text = base64.b64decode(data.get("content", "")).decode(errors="replace")
            return f"檔案 {data['path']}（{data['size']} bytes）\n---\n{text[:20000]}"
        info = _get(base)
        out = [f"repo：{info['full_name']}（{'私人' if info['private'] else '公開'}）",
               f"簡介：{info.get('description') or '（沒有）'}",
               f"主要語言：{info.get('language')}，⭐ {info['stargazers_count']}，預設分支 {info['default_branch']}",
               f"最後推送：{info['pushed_at']}"]
        try:
            langs = _get(f"{base}/languages")
            total = sum(langs.values()) or 1
            out.append("語言比例：" + "、".join(f"{k} {v/total:.0%}" for k, v in list(langs.items())[:6]))
        except urllib.error.HTTPError:
            pass
        root = _get(f"{base}/contents")
        out.append("根目錄：" + "  ".join(("📁" if x["type"] == "dir" else "") + x["name"] for x in root))
        commits = _get(f"{base}/commits?per_page=8")
        out.append("最近 commit：\n" + "\n".join(
            f"- {c['commit']['author']['date'][:10]} {c['commit']['message'].splitlines()[0][:100]}" for c in commits))
        try:
            readme = _get(f"{base}/readme")
            out.append("README：\n" + base64.b64decode(readme["content"]).decode(errors="replace")[:8000])
        except urllib.error.HTTPError:
            out.append("README：（沒有）")
        return "\n".join(out)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"找不到 {m[1]}/{m[2]}{'/' + path if path else ''}（不存在，或是私人 repo）"
        if e.code == 403:
            return "GitHub API 暫時限流（未登入每小時 60 次），等一下再試"
        raise
