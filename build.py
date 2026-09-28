"""打包成單一執行檔：python build.py（在哪個平台跑，就產生那個平台的版本）。
產物在 dist/，檔名帶平台與架構，例如 AutoWorkflow-macos-arm64。"""
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
os_name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine())
name = f"AutoWorkflow-{os_name}-{arch}"

if sys.version_info < (3, 13):
    sys.exit("需要 Python 3.13 以上（tail_file 用到 Path.full_match）")

shutil.rmtree(ROOT / "build", ignore_errors=True)
data = [("dashboard.html", "."), ("defaults", "defaults")] + [(str(p.relative_to(ROOT)), "skills") for p in (ROOT / "skills").glob("*.py")]
cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--console", "--noconfirm", "--clean",
       "--name", name, "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build")]
for src, dst in data:
    cmd += ["--add-data", f"{ROOT / src}:{dst}"]
cmd.append(str(ROOT / "app.py"))
subprocess.run(cmd, check=True)
print("完成：", next((ROOT / "dist").glob(name + "*")))
