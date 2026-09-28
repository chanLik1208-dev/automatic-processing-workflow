import json, pathlib, re

ROOT = pathlib.Path(__file__).parent.parent

SPEC = {
    "name": "create_workflow",
    "description": "建立一條新的 workflow（存到 workflows/）。新建的一律先停用排程，等使用者檢查過再開。",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "英文小寫加連字號，例如 disk-watch"},
        "description": {"type": "string", "description": "一句話說這條解決什麼問題"},
        "provider": {"type": "string", "enum": ["lmstudio", "deepseek"]},
        "skills": {"type": "array", "items": {"type": "string"}},
        "schedule": {"type": "object", "description": "{\"daily\": \"HH:MM\"} 或 {\"every_minutes\": N}，只能手動跑就給 {}"},
        "system": {"type": "string"},
        "task": {"type": "string", "description": "給執行 agent 的具體步驟，要寫清楚每一步用哪個 skill、什麼情況要 notify"}},
        "required": ["name", "description", "provider", "skills", "task"]},
}


def run(name, description, provider, skills, task, schedule=None, system=""):
    name = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    path = ROOT / "workflows" / f"{name}.json"
    if path.exists():
        return f"已經有叫 {name} 的 workflow，換個名字"
    have = {p.stem for p in (ROOT / "skills").glob("*.py")}
    missing = [s for s in skills if s not in have]
    if missing:
        return f"這些 skill 不存在：{missing}。可用的有：{sorted(have)}"
    if "create_workflow" in skills:
        return "新 workflow 不能再開放 create_workflow"
    wf = {"description": description, "provider": provider,
          "fallback": "deepseek" if provider == "lmstudio" else "lmstudio",
          "enabled": False, "schedule": schedule or None, "skills": skills,
          "system": system or "你是自動執行任務的 agent，一律用繁體中文（台灣）。", "task": task}
    path.write_text(json.dumps(wf, ensure_ascii=False, indent=2))
    return f"已建立 {path}（排程先停用）"
