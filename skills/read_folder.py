import os, pathlib, sys

SPEC = {
    "name": "read_folder",
    "description": "讀使用者這次執行附上的資料夾：path 留空或指向子資料夾就列出裡面的檔案，指向檔案就讀出文字內容。"
                   "只能讀附上的那個資料夾裡面的東西。",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "資料夾裡的相對路徑；留空 = 資料夾本身"}}},
}

TEXT_MAX = 60000              # 一次最多回傳幾個字（再多模型也讀不完）
LIST_MAX = 300                # 一次最多列幾個項目
SKIP = {".git", "node_modules", "__pycache__", ".venv", ".DS_Store"}


def _roots():
    """這次執行允許讀的資料夾：主程式在執行緒裡設定；ChatGPT 用的 MCP 伺服器是另一個行程，改從環境變數拿。"""
    eng = sys.modules.get("engine")
    got = eng.run_folders() if eng and hasattr(eng, "run_folders") else []
    return got or [p for p in os.environ.get("AW_FOLDERS", "").split(os.pathsep) if p]


def _cfg_max():
    try:
        import json
        c = json.loads((pathlib.Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
        return int(c.get("fetch", {}).get("max_chars", 6000)) * 4
    except Exception:
        return 24000


def run(path=""):
    roots = [os.path.realpath(r) for r in _roots()]
    if not roots:
        return "這次執行沒有附上資料夾，沒有東西可以讀。"
    root = roots[0]
    rel = str(path or "").strip().lstrip("/\\")
    target = os.path.realpath(os.path.join(root, rel)) if rel else root
    # 只能在附上的資料夾裡面（擋 ../、絕對路徑、指到外面的捷徑）
    if not any(target == r or target.startswith(r + os.sep) for r in roots):
        return f"只能讀附上的資料夾（{root}）裡面的檔案。"
    if not os.path.exists(target):
        return f"找不到：{rel or root}"
    if os.path.isdir(target):
        items = []
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP and not d.startswith("."))
            depth = os.path.relpath(dirpath, target).count(os.sep) + (dirpath != target)
            if depth > 2:                                   # 只往下看兩層，太深的請模型自己指定子資料夾
                dirnames[:] = []
            for f in sorted(filenames):
                if f in SKIP or f.startswith("."):
                    continue
                full = os.path.join(dirpath, f)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                items.append(f"{os.path.relpath(full, root)}（{size:,} bytes）")
                if len(items) >= LIST_MAX:
                    break
            if len(items) >= LIST_MAX:
                break
        head = f"資料夾：{os.path.relpath(target, root) if target != root else root}\n"
        more = f"\n（只列出前 {LIST_MAX} 個）" if len(items) >= LIST_MAX else ""
        return head + ("\n".join(items) or "（空的）") + more
    limit = min(_cfg_max(), TEXT_MAX)
    with open(target, "rb") as f:
        data = f.read(30 * 1024 * 1024)
    if data[:5] == b"%PDF-":                               # PDF 交給 fetch_url 的轉文字
        import importlib.util
        spec = importlib.util.spec_from_file_location("_aw_fetch_url", pathlib.Path(__file__).with_name("fetch_url.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._pdf(data, limit)
    if b"\x00" in data[:4096]:
        return f"{rel} 不是文字檔（可能是圖片或其他二進位檔），沒辦法讀內容。"
    text = data.decode("utf-8", errors="replace")
    cut = f"\n（檔案太長，只讀了前 {limit:,} 字，共 {len(text):,} 字）" if len(text) > limit else ""
    return f"檔案：{rel}\n---\n{text[:limit]}{cut}"
