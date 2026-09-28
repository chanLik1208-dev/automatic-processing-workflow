import json, shutil, subprocess, sys

SPEC = {
    "name": "notify",
    "description": "在桌面跳一則通知，用來回報結果給使用者。",
    "parameters": {"type": "object", "properties": {
        "title": {"type": "string"},
        "message": {"type": "string", "description": "一兩句話就好"}},
        "required": ["title", "message"]},
}


def run(title, message):
    title, message = title[:60], message[:200]
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(message, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"
        subprocess.run(["osascript", "-e", script], check=True, timeout=10)
    elif sys.platform == "win32":
        # 用系統內建的 Windows Forms 通知氣泡，不需要額外模組
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
              "$n.Visible=$true;$n.ShowBalloonTip(8000,$env:WF_T,$env:WF_M,'Info');Start-Sleep 8;$n.Dispose()")
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         env={**__import__("os").environ, "WF_T": title, "WF_M": message})
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", title, message], check=True, timeout=10)
    else:
        return "這台電腦沒有可用的通知工具（Linux 需要 notify-send），通知沒有送出"
    return "已送出通知"
