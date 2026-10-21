#!/bin/bash
# Double-click in Finder: opens Terminal with the guided upload.
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HOME"
export XU_QUEST_RECEIPT=1   # Launch Quest: print a receipt after the upload
PY=""; for p in "$HERE/xsolla_upload.py" "$HERE/../xsolla_upload.py"; do [ -f "$p" ] && { PY="$p"; break; }; done
if [ -n "$PY" ] && command -v python3 >/dev/null 2>&1; then python3 "$PY"   # the bundled copy supports quest receipts
elif command -v xsolla-upload >/dev/null 2>&1; then xsolla-upload
elif [ -x "$HOME/.local/bin/xsolla-upload" ]; then "$HOME/.local/bin/xsolla-upload"
else echo "xsolla-upload not found. Keep this file inside the xsolla-upload folder, or install: curl -fsSL …/install.sh | bash"; fi
echo; read -n 1 -s -r -p "Press any key to close / Нажмите любую клавишу, чтобы закрыть"
