#!/bin/bash
# MLX Ops Panel —— macOS 桌面双击启动
# 双击后面板前台跑在这个终端窗口里：关掉窗口 = 停面板。
# 也可以不关窗口，直接在页面下方点「关闭面板并退出」。
# 本文件只做「启动 + 打开浏览器 + 已运行判断」，真正的启停逻辑在 panel.sh。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PANEL="$HERE/panel.sh"

# 端口要与 panel.sh 一致；config.env 里设了 PANEL_PORT 就以它为准。
if [ -f "$HERE/config.env" ]; then
  while IFS= read -r _line; do
    case "$_line" in PANEL_PORT=*) PANEL_PORT="${_line#PANEL_PORT=}" ;; esac
  done < "$HERE/config.env"
fi
PANEL_PORT="${PANEL_PORT%\"}"; PANEL_PORT="${PANEL_PORT#\"}"
PANEL_PORT="${PANEL_PORT%\'}"; PANEL_PORT="${PANEL_PORT#\'}"
PANEL_PORT="${PANEL_PORT:-8090}"
URL="http://127.0.0.1:$PANEL_PORT"

echo "=================================================="
echo "  端侧模型运维面板 · 双击启动"
echo "=================================================="

if [ ! -x "$PANEL" ]; then
  echo "✗ 找不到启动脚本：$PANEL"
  echo "  请确认仓库目录完整、panel.sh 有可执行权限。按回车键关闭本窗口…"
  read -r _
  exit 1
fi

# 已经在跑：不要重复起（重复起会因端口占用直接退出），直接打开页面
if lsof -nP -iTCP:"$PANEL_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "面板已经在运行 → $URL"
  [ "${PANEL_NO_OPEN:-0}" = "1" ] || open "$URL"
  echo
  echo "要停止：在页面下方点「关闭面板并退出」，或在终端执行"
  echo "  $PANEL stop"
  echo
  echo "本窗口 ${PANEL_HOLD:-5} 秒后自动关闭（面板不会受影响）。"
  sleep "${PANEL_HOLD:-5}"
  exit 0
fi

echo "正在启动面板，随后自动打开浏览器：$URL"
echo "停止方式：关掉本终端窗口，或在页面下方点「关闭面板并退出」。"
echo "（只停本面板端口 $PANEL_PORT；已有推理服务不受影响）"
echo

if [ "${PANEL_DRYRUN:-0}" = "1" ]; then
  echo "[DRYRUN] 将执行： exec $PANEL start"
  exit 0
fi

# 延迟 2 秒再开浏览器，等面板完成监听；后台子 shell 只负责这一件事
if [ "${PANEL_NO_OPEN:-0}" != "1" ]; then
  ( sleep 2; open "$URL" ) &
fi

exec "$PANEL" start
