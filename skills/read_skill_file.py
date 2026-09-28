import pathlib

SPEC = {
    "name": "read_skill_file",
    "description": "讀知識型 skill 資料夾裡的某個檔案，例如 references/feel.md 或 i18n/zh-TW/recipes.md。",
    "parameters": {"type": "object", "properties": {
        "skill": {"type": "string"},
        "path": {"type": "string", "description": "相對於 skill 資料夾的路徑"}},
        "required": ["skill", "path"]},
}

SKILLS = pathlib.Path(__file__).parent


def run(skill, path):
    names = {f.parent.name for f in SKILLS.glob("*/SKILL.md")}
    if skill not in names:
        return f"沒有這個 skill。可用的有：{sorted(names)}"
    base = (SKILLS / skill).resolve()
    f = (base / path).resolve()
    if base not in f.parents or not f.is_file():
        listing = sorted(str(p.relative_to(base)) for p in base.rglob("*.md") if ".git" not in p.parts)
        return f"找不到 {path}。這個 skill 裡有：{listing}"
    return f.read_text()
