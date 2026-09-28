import glob, json, os, pathlib, time

SPEC = {
    "name": "tail_file",
    "description": "讀白名單內的文字檔（例如訓練 log）的最後幾行，並告訴你多久沒更新。path 可以用萬用字元，會挑最新修改的那個。",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "例如 ~/satsuki_*.log"},
        "lines": {"type": "integer", "description": "預設 60"}},
        "required": ["path"]},
}

ROOT = pathlib.Path(__file__).parent.parent


def _allowed(f):
    pats = json.loads((ROOT / "config.json").read_text()).get("readable_paths", [])
    return any(f.full_match(os.path.expanduser(p)) for p in pats)  # * 不跨目錄，** 才會


def run(path, lines=60):
    matches = [pathlib.Path(p).resolve() for p in glob.glob(os.path.expanduser(path))]
    matches = [p for p in matches if p.is_file()]
    if not matches:
        return f"找不到符合 {path} 的檔案"
    matches = [p for p in matches if _allowed(p)]
    if not matches:
        return "這個路徑不在 config.json 的 readable_paths 白名單裡"
    f = max(matches, key=lambda p: p.stat().st_mtime)
    age = (time.time() - f.stat().st_mtime) / 60
    with open(f, "rb") as fh:
        fh.seek(0, 2); fh.seek(max(0, fh.tell() - 200_000))
        tail = fh.read().decode(errors="replace").replace("\r", "\n").splitlines()[-lines:]
    return f"檔案：{f}\n最後更新：{age:.0f} 分鐘前\n---\n" + "\n".join(tail)
