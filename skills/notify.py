import json, subprocess

SPEC = {
    "name": "notify",
    "description": "在 Mac 右上角跳一則通知，用來回報結果給使用者。",
    "parameters": {"type": "object", "properties": {
        "title": {"type": "string"},
        "message": {"type": "string", "description": "一兩句話就好"}},
        "required": ["title", "message"]},
}


def run(title, message):
    script = f"display notification {json.dumps(message[:200], ensure_ascii=False)} with title {json.dumps(title[:60], ensure_ascii=False)}"
    subprocess.run(["osascript", "-e", script], check=True, timeout=10)
    return "已送出通知"
