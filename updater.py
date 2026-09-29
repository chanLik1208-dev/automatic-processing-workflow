"""檢查更新、下載、換上新版本。

版本來源：這個 repo 的 GitHub Releases（v* 標籤）。私人 repo 未登入查不到，會改用 gh CLI 的登入或設定裡的 token。
換版本的方式：程式執行中不能覆蓋自己，所以先把新版放到暫存位置，再用一個獨立的小腳本
等目前的程式結束後替換、重新開啟。"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request

import engine
from version import VERSION

REPO = "chanLik1208-dev/automatic-processing-workflow"
FROZEN = getattr(sys, "frozen", False)
_state = {"checking": False, "latest": None, "error": None, "checked": None, "staged": None, "progress": None}
_lock = threading.Lock()


def _ver(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3])


def asset_name():
    import platform
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    return {"darwin": f"AutoWorkflow-macos-{arch}.dmg", "win32": "AutoWorkflow-windows-x64.exe"}.get(
        sys.platform, f"AutoWorkflow-linux-{arch}.tar.gz")


def _token():
    return engine.cfg("update.github_token", "") or os.environ.get("GITHUB_TOKEN", "")


def _gh(args):
    gh = shutil.which("gh")
    if not gh:
        return None
    r = subprocess.run([gh] + args, capture_output=True, text=True, encoding="utf-8", timeout=60)
    return r.stdout if r.returncode == 0 else None


def _api(path):
    """先用 token / 不登入查；私人 repo 查不到時改用 gh CLI（它有登入）。"""
    req = urllib.request.Request(f"https://api.github.com{path}", headers={
        "Accept": "application/vnd.github+json", "User-Agent": "AutoWorkflow",
        **({"Authorization": f"Bearer {_token()}"} if _token() else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            out = _gh(["api", path])
            if out:
                return json.loads(out)
            if e.code == 404:
                raise RuntimeError("查不到更新：這個 repo 是私人的。請安裝並登入 GitHub CLI（gh auth login），"
                                   "或在「設定 → 更新」填入 GitHub token。")
            if e.code == 403:
                raise RuntimeError("GitHub 暫時限制了查詢次數，過一陣子再試。")
        raise


def check():
    """查最新的正式版本；回傳狀態 dict。"""
    with _lock:
        if _state["checking"]:
            return status()
        _state.update(checking=True, error=None)
    try:
        rel = _api(f"/repos/{REPO}/releases/latest")
        tag = rel.get("tag_name", "")
        asset = next((a for a in rel.get("assets", []) if a["name"] == asset_name()), None)
        _state["latest"] = {"version": tag.lstrip("v"), "tag": tag, "notes": (rel.get("body") or "")[:4000],
                            "url": rel.get("html_url"), "published": rel.get("published_at"),
                            "asset": {"id": asset["id"], "name": asset["name"], "size": asset["size"],
                                      "digest": asset.get("digest")} if asset else None}
    except Exception as e:
        _state["error"] = str(e) if isinstance(e, RuntimeError) else f"檢查更新失敗：{e}"
    finally:
        _state.update(checking=False, checked=time.time())
    return status()


def status():
    latest = _state["latest"]
    newer = bool(latest and _ver(latest["version"]) > _ver(VERSION))
    return {"current": VERSION, "frozen": FROZEN, "platform_asset": asset_name(), "update_available": newer,
            **{k: _state[k] for k in ("checking", "latest", "error", "checked", "staged", "progress")},
            "can_install": newer and FROZEN and bool(latest and latest.get("asset")),
            "why_cannot": None if FROZEN else "用原始碼執行：請用 git pull 更新。"}


# ---------------------------------------------------------------- 下載
def _download(asset, dest):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/assets/{asset['id']}", headers={
        "Accept": "application/octet-stream", "User-Agent": "AutoWorkflow",
        **({"Authorization": f"Bearer {_token()}"} if _token() else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            got = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                got += len(chunk)
                _state["progress"] = round(got / max(asset["size"], 1) * 100)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403, 404) or not shutil.which("gh"):
            raise
        tmp = tempfile.mkdtemp()                             # 私人 repo：用 gh 的登入下載
        subprocess.run(["gh", "release", "download", _state["latest"]["tag"], "-R", REPO, "-p", asset["name"],
                        "-D", tmp, "--clobber"], check=True, capture_output=True, timeout=600)
        shutil.move(os.path.join(tmp, asset["name"]), dest)
    size = os.path.getsize(dest)
    if size != asset["size"]:
        raise RuntimeError(f"下載的檔案大小不對（{size} ≠ {asset['size']}），可能沒下載完整")
    if asset.get("digest", "").startswith("sha256:"):
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != asset["digest"].split(":", 1)[1]:
            raise RuntimeError("下載的檔案 SHA-256 對不上，已放棄安裝")


def _current_target():
    """要被換掉的東西：macOS 是整個 .app，其他是執行檔本身。"""
    exe = pathlib.Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for p in exe.parents:
            if p.suffix == ".app":
                return p
    return exe


def stage():
    """下載並準備好新版本（還沒替換）。回傳狀態。"""
    st = check() if not _state["latest"] else status()
    if not st["can_install"]:
        raise RuntimeError(st["why_cannot"] or st["error"] or "已經是最新版本")
    asset = _state["latest"]["asset"]
    work = pathlib.Path(tempfile.mkdtemp(prefix="aw-update-"))
    dl = work / asset["name"]
    _state["progress"] = 0
    _download(asset, dl)
    if sys.platform == "darwin":
        mnt = work / "mnt"
        mnt.mkdir()
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mnt), str(dl)],
                       check=True, capture_output=True, timeout=120)
        try:
            new = work / "AutoWorkflow.app"
            subprocess.run(["ditto", str(mnt / "AutoWorkflow.app"), str(new)], check=True, timeout=300)
        finally:
            subprocess.run(["hdiutil", "detach", "-quiet", str(mnt)], timeout=60)
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(new)], timeout=60)
    elif sys.platform == "win32":
        new = dl
    else:
        with tarfile.open(dl) as t:
            member = next(m for m in t.getmembers() if m.name.endswith("/AutoWorkflow"))
            member.name = "AutoWorkflow"
            t.extract(member, work, filter="data")
        new = work / "AutoWorkflow"
        new.chmod(0o755)
    _state["staged"] = {"path": str(new), "version": _state["latest"]["version"]}
    _state["progress"] = None
    return status()


def apply_and_restart(relaunch_args=None):
    """開一個獨立的小腳本：等這個程式結束 → 換上新版 → 重新開啟。呼叫後程式應該馬上結束。"""
    staged = _state.get("staged")
    if not staged:
        raise RuntimeError("還沒有下載好的新版本")
    target, new, pid = _current_target(), staged["path"], os.getpid()
    args = " ".join(f'"{a}"' for a in (relaunch_args or []))
    if sys.platform == "win32":
        script = pathlib.Path(tempfile.gettempdir()) / "autoworkflow-update.cmd"
        script.write_text(
            "@echo off\r\n"
            f":wait\r\ntasklist /FI \"PID eq {pid}\" | find \"{pid}\" >nul && (timeout /t 1 >nul & goto wait)\r\n"
            f"move /Y \"{new}\" \"{target}\" >nul\r\n"
            f"start \"\" \"{target}\" {args}\r\n", encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=0x08000000 | 0x00000008)   # 不開視窗、脫離本程式
    else:
        opener = f'open "{target}"' if sys.platform == "darwin" else f'"{target}" {args} >/dev/null 2>&1 &'
        script = pathlib.Path(tempfile.gettempdir()) / "autoworkflow-update.sh"
        script.write_text(
            "#!/bin/sh\n"
            f"while kill -0 {pid} 2>/dev/null; do sleep 0.5; done\n"
            f'rm -rf "{target}.old" && mv "{target}" "{target}.old" && mv "{new}" "{target}" && rm -rf "{target}.old"\n'
            f"{opener}\n", encoding="utf-8")
        script.chmod(0o755)
        subprocess.Popen(["/bin/sh", str(script)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


# ---------------------------------------------------------------- 背景自動檢查
def auto_loop(on_found=None):
    """每天檢查一次；開了「自動安裝」就先下載好，下次啟動時換上。"""
    time.sleep(20)                                        # 不要跟啟動搶資源
    while True:
        if engine.cfg("update.auto_check", True):
            st = check()
            if st["update_available"]:
                if on_found:
                    on_found(st)
                if engine.cfg("update.auto_install", False) and st["can_install"] and not _state.get("staged"):
                    try:
                        stage()
                        _pending_file().write_text(json.dumps(_state["staged"]), encoding="utf-8")
                    except Exception as e:
                        _state["error"] = f"自動下載更新失敗：{e}"
        time.sleep(24 * 3600)


def _pending_file():
    return engine.ROOT / "data" / "pending-update.json"


def apply_pending_on_start():
    """啟動時：上次背景已經下載好新版，就先換上再開。"""
    if not FROZEN or not _pending_file().exists():
        return False
    try:
        staged = json.loads(_pending_file().read_text(encoding="utf-8"))
        _pending_file().unlink()
        if _ver(staged["version"]) <= _ver(VERSION) or not os.path.exists(staged["path"]):
            return False
        _state["staged"] = staged
        apply_and_restart(sys.argv[1:])
        return True
    except Exception:
        return False
