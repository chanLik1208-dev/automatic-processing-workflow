#!/bin/bash
# 安裝 AutoWorkflow：複製到「應用程式」，並移除 macOS 對下載檔加的隔離標記（com.apple.quarantine），
# 之後雙擊 app 就不會再被 Gatekeeper 擋。只會動 AutoWorkflow.app 這一個 app。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/AutoWorkflow.app"
DEST="${AW_DEST:-/Applications}/AutoWorkflow.app"

if [ ! -d "$SRC" ]; then
  echo "找不到 AutoWorkflow.app，請從 dmg 裡執行這個檔案。"; exit 1
fi
if pgrep -f "AutoWorkflow.app/Contents/MacOS/AutoWorkflow" > /dev/null; then
  echo "AutoWorkflow 正在執行，請先關掉它再安裝。"; exit 1
fi

echo "複製到 $DEST …"
rm -rf "$DEST"
cp -R "$SRC" "$DEST"
echo "移除隔離標記 …"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
echo "完成，正在開啟 AutoWorkflow。這個視窗可以關掉了。"
[ -z "$AW_NO_OPEN" ] && open "$DEST"
exit 0
