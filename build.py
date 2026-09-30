"""打包：python build.py（在哪個平台跑，就產生那個平台的版本）。

產物（dist/）：
  macOS    AutoWorkflow-macos-<arch>.dmg      裡面是 AutoWorkflow.app，拖進「應用程式」就能用
  Linux    AutoWorkflow-linux-<arch>.tar.gz   解壓縮後執行 ./AutoWorkflow（執行權限會保留）
  Windows  AutoWorkflow-windows-<arch>.exe    直接雙擊

另外寫出 dist/test-bin.txt（要做啟動測試的執行檔路徑）和 dist/upload.txt（要上傳的檔案），給 CI 用。"""
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent

# Windows 主控台預設不是 UTF-8（例如 cp1252），印中文會直接當掉；統一改成 UTF-8，印不出來的字元用替代符號
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

os_name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine())
full = f"AutoWorkflow-{os_name}-{arch}"
DIST, BUILD = ROOT / "dist", ROOT / "build"

if sys.version_info < (3, 13):
    sys.exit("需要 Python 3.13 以上（tail_file 用到 Path.full_match）")

shutil.rmtree(BUILD, ignore_errors=True)
shutil.rmtree(DIST, ignore_errors=True)
data = [("dashboard.html", "."), ("defaults", "defaults")] + [(str(p.relative_to(ROOT)), "skills") for p in (ROOT / "skills").glob("*.py")]
cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
       "--distpath", str(DIST), "--workpath", str(BUILD / "work"), "--specpath", str(BUILD)]
if os_name == "macos":
    # .app：雙擊開原生視窗，不會跳出終端機。onedir 是 PyInstaller 對 .app 建議的做法（啟動也比 onefile 快）
    cmd += ["--windowed", "--onedir", "--name", "AutoWorkflow", "--osx-bundle-identifier", "app.autoworkflow"]
else:
    cmd += ["--onefile", "--console", "--name", "AutoWorkflow" if os_name == "linux" else full]
for src, dst in data:
    cmd += ["--add-data", f"{ROOT / src}:{dst}"]
# 介面模組是在執行時才用名字載入的（app.py 的 __import__），PyInstaller 看不到：不寫明就不會打包，
# 結果 WebView 版在每台電腦上都「No module named 'native'」，默默改開 Qt 版
for mod in ("native", "qt_app", "mcp_skills"):
    cmd += ["--hidden-import", mod]
cmd.append(str(ROOT / "app.py"))
subprocess.run(cmd, check=True)

if os_name == "macos":
    app = DIST / "AutoWorkflow.app"
    test_bin = app / "Contents" / "MacOS" / "AutoWorkflow"
    with tempfile.TemporaryDirectory() as stage:
        shutil.copytree(app, Path(stage) / "AutoWorkflow.app", symlinks=True)
        os.symlink("/Applications", Path(stage) / "Applications")      # 打開 dmg 就能直接拖進「應用程式」
        # 雙擊就會複製到「應用程式」並移除隔離標記（腳本本身第一次仍要在系統設定允許一次）
        shutil.copy2(ROOT / "packaging" / "install-macos.command", Path(stage) / "安裝 AutoWorkflow.command")
        os.chmod(Path(stage) / "安裝 AutoWorkflow.command", 0o755)
        out = DIST / f"{full}.dmg"
        subprocess.run(["hdiutil", "create", "-volname", "AutoWorkflow", "-srcfolder", stage,
                        "-ov", "-format", "UDZO", str(out)], check=True, capture_output=True)
elif os_name == "linux":
    test_bin = DIST / "AutoWorkflow"
    out = DIST / f"{full}.tar.gz"
    with tarfile.open(out, "w:gz") as t:
        t.add(test_bin, arcname=f"{full}/AutoWorkflow")               # tar 會保留執行權限
else:
    test_bin = out = DIST / f"{full}.exe"

(DIST / "test-bin.txt").write_text(str(test_bin), encoding="utf-8")
(DIST / "upload.txt").write_text(str(out), encoding="utf-8")
print(f"完成：{out}（{out.stat().st_size / 1048576:.1f} MB）")
