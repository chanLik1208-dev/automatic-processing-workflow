import pathlib, re

SPEC = {
    "name": "use_skill",
    "description": "載入一個知識型 skill 的 SKILL.md 全文。任務碰到該 skill 描述的領域時先呼叫它，再照裡面的指示用 read_skill_file 讀需要的章節。",
    "parameters": {"type": "object", "properties": {"skill": {"type": "string"}}, "required": ["skill"]},
}

SKILLS = pathlib.Path(__file__).parent


def catalog():
    """回傳 [(資料夾名, description)]，給引擎放進 system prompt。"""
    out = []
    for f in sorted(SKILLS.glob("*/SKILL.md")):
        m = re.search(r"^description:\s*(.+)$", f.read_text(), re.M)
        out.append((f.parent.name, (m.group(1) if m else "")[:400]))
    return out


def run(skill):
    f = SKILLS / skill / "SKILL.md"
    if skill not in {n for n, _ in catalog()}:
        return f"沒有這個 skill。可用的有：{[n for n, _ in catalog()]}"
    return re.sub(r"^---.*?---\s*", "", f.read_text(), flags=re.S)
