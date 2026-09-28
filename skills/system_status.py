import shutil, subprocess

SPEC = {
    "name": "system_status",
    "description": "回報這台 Mac 的磁碟剩餘、記憶體壓力、負載，以及指定程序是否在跑。",
    "parameters": {"type": "object", "properties": {
        "processes": {"type": "array", "items": {"type": "string"},
                      "description": "要確認有沒有在跑的程序名稱關鍵字，例如 cloudflared"}}},
}


def _sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()


def run(processes=None):
    d = shutil.disk_usage("/")
    lines = [f"磁碟：剩 {d.free/1e9:.0f} GB / 共 {d.total/1e9:.0f} GB（已用 {d.used/d.total:.0%}）",
             f"負載：{_sh('sysctl', '-n', 'vm.loadavg')}",
             "記憶體：" + (_sh('memory_pressure', '-Q').splitlines() or ['?'])[-1]]
    for p in processes or []:
        pids = _sh("pgrep", "-f", p).split()
        lines.append(f"程序 {p}：{'在跑（pid ' + ','.join(pids[:3]) + '）' if pids else '沒有在跑'}")
    return "\n".join(lines)
