import os, shutil, subprocess, sys

SPEC = {
    "name": "system_status",
    "description": "回報這台電腦的磁碟剩餘、記憶體、負載，以及指定程序是否在跑。",
    "parameters": {"type": "object", "properties": {
        "processes": {"type": "array", "items": {"type": "string"},
                      "description": "要確認有沒有在跑的程序名稱關鍵字，例如 cloudflared"}}},
}


def _sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _memory():
    if sys.platform == "darwin":
        out = _sh("memory_pressure", "-Q").splitlines()
        return out[-1] if out else "?"
    if sys.platform == "win32":
        out = _sh("powershell", "-NoProfile", "-Command",
                  "$o=Get-CimInstance Win32_OperatingSystem;'{0:N1} GB 可用 / 共 {1:N1} GB' -f ($o.FreePhysicalMemory/1MB),($o.TotalVisibleMemorySize/1MB)")
        return out or "?"
    try:
        info = dict(l.split(":", 1) for l in open("/proc/meminfo", encoding="utf-8"))
        kb = lambda k: int(info[k].split()[0])
        return f"{kb('MemAvailable') / 1048576:.1f} GB 可用 / 共 {kb('MemTotal') / 1048576:.1f} GB"
    except (OSError, KeyError, ValueError):
        return "?"


def _running(name):
    if sys.platform == "win32":
        out = _sh("tasklist", "/FO", "CSV", "/NH")
        return [l.split(",")[1].strip('"') for l in out.splitlines() if name.lower() in l.lower()][:3]
    return _sh("pgrep", "-f", name).split()[:3]


def run(processes=None):
    d = shutil.disk_usage(os.path.abspath(os.sep))
    lines = [f"磁碟：剩 {d.free/1e9:.0f} GB / 共 {d.total/1e9:.0f} GB（已用 {d.used/d.total:.0%}）",
             f"記憶體：{_memory()}"]
    if hasattr(os, "getloadavg"):
        lines.append("負載：" + " ".join(f"{x:.2f}" for x in os.getloadavg()))
    for p in processes or []:
        pids = _running(p)
        lines.append(f"程序 {p}：{'在跑（pid ' + ','.join(pids) + '）' if pids else '沒有在跑'}")
    return "\n".join(lines)
