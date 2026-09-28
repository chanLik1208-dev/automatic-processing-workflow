import datetime, pathlib, re

SPEC = {
    "name": "save_report",
    "description": "把整理好的報告存成 Markdown 檔，回傳檔案路徑。",
    "parameters": {"type": "object", "properties": {
        "title": {"type": "string"},
        "content": {"type": "string", "description": "Markdown 內容"}},
        "required": ["title", "content"]},
}

REPORTS = pathlib.Path(__file__).parent.parent / "reports"


def run(title, content):
    slug = re.sub(r"[^\w一-鿿-]+", "-", title).strip("-")[:40] or "report"
    path = REPORTS / f"{datetime.datetime.now():%Y%m%d-%H%M}-{slug}.md"
    path.write_text(f"# {title}\n\n{content}\n", encoding="utf-8")
    return str(path)
