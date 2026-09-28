"""Skill 管理：列出、匯入（zip / SKILL.md / GitHub / .py）、刪除、試用，以及對話式構建器。"""
import ast
import base64
import datetime
import io
import json
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import engine

MAX_ZIP = 20 * 1024 * 1024
# 明確列出內建工具；不能用「程式資料夾裡有哪些 .py」判斷，因為用原始碼跑時資料夾和程式是同一個，
# 使用者自己匯入的也會被算成內建而刪不掉
BUILTIN_PY = {"create_workflow", "fetch_url", "github_repo", "http_check", "notify", "read_rss", "read_skill_file",
              "save_report", "system_status", "tail_file", "use_skill", "web_search"}


def skills_dir():
    return engine.ROOT / "skills"


def slug(s):
    s = re.sub(r"[^a-z0-9-]+", "-", (s or "").lower()).strip("-")
    return s[:48] or "skill"


def frontmatter(text):
    """讀 SKILL.md 開頭的 name / description（支援 description: > 這種多行寫法）。"""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out, lines, i = {}, m.group(1).splitlines(), 0
    while i < len(lines):
        km = re.match(r"^([A-Za-z_-]+):\s*(.*)$", lines[i])
        if km:
            key, val = km.group(1), km.group(2).strip()
            if val in (">", "|", ">-", "|-"):
                buf = []
                while i + 1 < len(lines) and (lines[i + 1].startswith(" ") or not lines[i + 1].strip()):
                    i += 1
                    buf.append(lines[i].strip())
                val = " ".join(b for b in buf if b)
            out[key] = val.strip("\"'")
        i += 1
    return out


def list_skills():
    out = []
    for f in sorted(skills_dir().glob("*.py")):
        try:
            tree = ast.parse(f.read_text())
            spec = next((ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                         and any(getattr(t, "id", "") == "SPEC" for t in n.targets)), {})
        except Exception:
            spec = {}
        out.append({"name": f.stem, "kind": "tool", "description": spec.get("description", ""),
                    "source": "builtin" if f.stem in BUILTIN_PY else "imported"})
    for f in sorted(skills_dir().glob("*/SKILL.md")):
        fm = frontmatter(f.read_text(errors="replace"))
        files = sum(1 for p in f.parent.rglob("*") if p.is_file() and ".git" not in p.parts)
        out.append({"name": f.parent.name, "kind": "knowledge", "title": fm.get("name", f.parent.name),
                    "description": fm.get("description", ""), "files": files,
                    "source": "linked" if f.parent.is_symlink() else "imported"})
    return out


# ---------- 匯入 ----------

def _install_folder(src: Path, name_hint=""):
    """把一個含 SKILL.md 的資料夾裝進 skills/<name>/。"""
    fm = frontmatter((src / "SKILL.md").read_text(errors="replace"))
    if not fm.get("description"):
        raise ValueError(f"{name_hint or src.name} 的 SKILL.md 開頭缺少 description（格式：--- name: … description: … ---）")
    name = slug(fm.get("name") or name_hint or src.name)
    if name in BUILTIN_PY:
        name += "-skill"
    dest = skills_dir() / name
    if dest.exists():
        raise ValueError(f"已經有叫 {name} 的 skill，先刪掉舊的再匯入")
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", ".DS_Store"))
    return name


def _install_zip(data: bytes, name_hint=""):
    if len(data) > MAX_ZIP:
        raise ValueError("檔案超過 20 MB")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for m in z.infolist():
                target = (root / m.filename).resolve()
                if root not in target.parents and target != root:      # 擋掉 ../ 路徑（zip slip）
                    raise ValueError("壓縮檔裡有不安全的路徑")
            z.extractall(root)
        found = sorted(root.rglob("SKILL.md"), key=lambda p: len(p.parts))
        if not found:
            raise ValueError("壓縮檔裡找不到 SKILL.md")
        names = []
        for f in found[:20]:
            if any(f.parent == g.parent or g.parent in f.parents for g in found if g is not f and len(g.parts) < len(f.parts)):
                continue                                          # 巢狀的 SKILL.md 屬於外層那個 skill
            names.append(_install_folder(f.parent, name_hint if len(found) == 1 else ""))
        return names


def import_skill(body):
    kind = body.get("type")
    if kind == "github":
        m = re.match(r"https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/tree/([^/]+)(?:/(.*))?)?/?$",
                     (body.get("url") or "").strip())
        if not m:
            raise ValueError("請貼 GitHub repo 網址，例如 https://github.com/owner/repo")
        owner, repo, branch, sub = m.groups()
        if not branch:
            with urllib.request.urlopen(urllib.request.Request(
                    f"https://api.github.com/repos/{owner}/{repo}", headers={"User-Agent": "ai-workflow"}), timeout=20) as r:
                branch = json.loads(r.read())["default_branch"]
        url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ai-workflow"}), timeout=60) as r:
            data = r.read(MAX_ZIP + 1)
        if len(data) > MAX_ZIP:
            raise ValueError("repo 壓縮檔超過 20 MB")
        if sub:                                                   # 只要 repo 裡某個子資料夾
            with tempfile.TemporaryDirectory() as d:
                zipfile.ZipFile(io.BytesIO(data)).extractall(d)
                top = next(Path(d).iterdir())
                target = (top / sub).resolve()
                if top.resolve() not in target.parents or not (target / "SKILL.md").exists():
                    raise ValueError(f"{sub} 底下沒有 SKILL.md")
                return [_install_folder(target)]
        return _install_zip(data, repo)

    if kind == "file":
        fname = body.get("filename") or ""
        data = base64.b64decode(body.get("content") or "")
        if fname.lower().endswith(".zip"):
            return _install_zip(data, Path(fname).stem)
        if fname.lower().endswith(".md"):
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "SKILL.md").write_bytes(data)
                return [_install_folder(Path(d), Path(fname).stem)]
        if fname.lower().endswith(".py"):
            if not body.get("confirm_code"):
                raise ValueError("匯入 .py 需要先確認：這段程式會在你的電腦上以你的權限執行")
            src = data.decode("utf-8", errors="replace")
            tree = ast.parse(src)                                 # 只做靜態檢查，不執行
            has_spec = any(isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "SPEC" for t in n.targets) for n in tree.body)
            has_run = any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in tree.body)
            if not (has_spec and has_run):
                raise ValueError("工具型 skill 需要有 SPEC（OpenAI function 格式）和 run() 函式")
            name = slug(Path(fname).stem).replace("-", "_")
            if name in BUILTIN_PY or (skills_dir() / f"{name}.py").exists():
                raise ValueError(f"已經有叫 {name} 的工具")
            (skills_dir() / f"{name}.py").write_text(src)
            return [name]
        raise ValueError("支援 .zip、.md（SKILL.md）和 .py")
    raise ValueError("不認得的匯入方式")


def delete_skill(name):
    if name in BUILTIN_PY:
        raise ValueError("內建的 skill 不能刪")
    trash = skills_dir() / ".trash"
    trash.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder, py = skills_dir() / name, skills_dir() / f"{name}.py"
    if folder.is_symlink():
        folder.unlink()                                           # 連結只拿掉連結本身，不動原本的資料夾
        return "已移除連結"
    for p in (folder, py):
        if p.exists():
            shutil.move(str(p), str(trash / f"{p.name}-{stamp}"))
            return str(trash)
    raise ValueError(f"找不到 skill {name}")


# ---------- 試用 ----------

def adhoc_workflow(name, provider, model=""):
    info = next((s for s in list_skills() if s["name"] == name), None)
    if not info:
        raise ValueError(f"找不到 skill {name}")
    if info["kind"] == "knowledge":
        skills = ["read_skill_file", "web_search", "fetch_url", "save_report"]
        body = re.sub(r"^---.*?---\s*", "", (skills_dir() / name / "SKILL.md").read_text(errors="replace"), flags=re.S)
        # 直接把 skill 的內容放進系統提示，不靠模型記得先去載入（小模型常常只說「我去載入」卻沒做）
        system = (f"你是自動執行任務的 agent。這次要嚴格照下面這個 skill（{name}）的指示做事；"
                  f"它提到的其他檔案用 read_skill_file（skill 名稱填 {name}）讀。\n\n=== skill：{name} ===\n{body[:30000]}")
    else:
        skills = [name]
        system = "你是自動執行任務的 agent，一律用繁體中文（台灣）。"
    wf = {"title": f"試用：{info.get('title') or name}", "provider": provider, "skills": skills,
          "system": system, "task": "完成使用者的要求。", "max_steps": 12, "enabled": False}
    if model:
        wf["model"] = model
    return wf


# ---------- 對話構建器 ----------

BUILDER_SYSTEM = """你是 skill 設計師，一律用繁體中文（台灣），幫使用者把一個想法寫成 SKILL.md。

SKILL.md 的格式：
---
name: 英文小寫加連字號，例如 meeting-notes
description: 一兩句話：這個 skill 做什麼，以及「什麼時候該用它」（寫出會觸發它的情境和關鍵字）
---
# 標題
接著是給 AI 看的指示：目的、步驟、規則（要做／不要做）、輸出格式、一兩個簡短範例。

做法：
- 需求不清楚時，一次最多問 2 個關鍵問題；夠清楚就直接寫，不要一直問。
- 每次回覆先用一兩句話講你改了什麼或還想確認什麼，然後附上「目前完整的草稿」，放在 ```skill 開頭、``` 結尾的區塊裡。
- 使用者要改的時候，改完整份重新附上，不要只給片段。
- 指示要具體、可以照著做；寫明輸入是什麼、輸出長什麼樣。整份控制在 200 行內。"""


def extract_draft(text):
    blocks = re.findall(r"```(?:skill|markdown|md)?\s*\n(---\s*\n.*?)```", text, re.S)
    return blocks[-1].strip() + "\n" if blocks else None


def builder_chat(body):
    provider = body.get("provider") or "claude"
    msgs = [{"role": "system", "content": BUILDER_SYSTEM}]
    for m in (body.get("messages") or [])[-30:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": str(m["content"])[:20000]})
    resp = engine.chat(provider, body.get("model") or None, msgs, None, {}, max_tokens=6000)
    reply = resp["choices"][0]["message"].get("content") or ""
    reply = engine.strip_think(reply)
    return {"reply": reply, "draft": extract_draft(reply)}


def builder_save(body):
    text = body.get("draft") or ""
    fm = frontmatter(text)
    if not fm.get("name") or not fm.get("description"):
        raise ValueError("草稿開頭要有 name 和 description")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "SKILL.md").write_text(text)
        return _install_folder(Path(d))
