#!/bin/bash
# MLX Ops Panel —— 手动启动 / 状态 / 停止 / 重启
# 不注册 launchd，不改动任何已有推理服务配置。
# 用 pgrep + lsof 定位进程（ps 在部分 macOS 会话被系统拦截）。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

# 本机配置只放在 config.env（.gitignore 已排除），发布时永远带不出去。
# 刻意逐行解析而不是 source：不执行配置文件里的任意命令。
if [ -f "$HERE/config.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    case "$key" in [A-Za-z_][A-Za-z0-9_]*) ;; *) continue ;; esac
    val="${val#\"}"; val="${val%\"}"; val="${val#\'}"; val="${val%\'}"
    # 命令行/环境里已给的值优先，方便临时换端口做验证：
    #   PANEL_PORT=8092 ./panel.sh
    if ! printenv "$key" >/dev/null 2>&1; then
      export "$key=$val"
    fi
  done < "$HERE/config.env"
fi

LAB="${PANEL_LAB:-$HOME/mlx-lab}"
PORT="${PANEL_PORT:-8090}"
PY="${PANEL_PYTHON:-python3}"
SCRIPT="${PANEL_SCRIPT:-$HERE/ops_panel.py}"

# 命令行里必须真的出现 ops_panel.py，避免误杀 8090 上的其他进程
panel_pids() { pgrep -f 'ops_panel\.py' 2>/dev/null | sort -u; }
listen_pid() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1; }

cmd_start() {
  local p
  p="$(listen_pid)"
  if [ -n "$p" ]; then
    echo "端口 $PORT 已被 PID $p 占用；面板可能已在运行： http://127.0.0.1:$PORT"
    echo "查看状态： $0 status    重启： $0 restart"
    exit 1
  fi
  echo "面板地址 ： http://127.0.0.1:$PORT"
  echo "停止方式 ： $0 stop"
  echo "说明文档 ： $HERE/README.md"
  echo
  exec "$PY" "$SCRIPT"
}

cmd_status() {
  local p
  p="$(listen_pid)"
  if [ -z "$p" ]; then
    echo "面板未运行（$PORT 无监听）"
    return 1
  fi
  if ! panel_pids | grep -qx "$p"; then
    echo "⚠️ 端口 $PORT 被非面板进程 PID $p 占用，请手工核查： lsof -nP -iTCP:$PORT"
    return 1
  fi
  echo "面板运行中：PID $p | http://127.0.0.1:$PORT"
  curl -sS --noproxy '*' --max-time 3 -o /dev/null -w "首页 HTTP %{http_code}\n" \
    "http://127.0.0.1:$PORT/" 2>/dev/null || echo "首页探测失败（进程在但不响应）"
  echo "进程： $(pgrep -fl 'ops_panel\.py' 2>/dev/null | head -1)"
  return 0
}

cmd_stop() {
  local p leftover
  p="$(listen_pid)"
  if [ -z "$p" ]; then
    echo "面板未运行（$PORT 无监听）"
    leftover="$(panel_pids)"
    [ -n "$leftover" ] && echo "提示：仍有 ops_panel.py 残留进程 $leftover，可用 pgrep -fl ops_panel.py 核查"
    return 0
  fi
  if ! panel_pids | grep -qx "$p"; then
    echo "⚠️ 端口 $PORT 被非面板进程 PID $p 占用，为安全起见拒绝停止。"
    echo "   请手工核查： lsof -nP -iTCP:$PORT"
    return 1
  fi
  echo "正在停止面板 PID $p ..."
  kill -TERM "$p" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -z "$(listen_pid)" ] && break
    sleep 0.3
  done
  if [ -n "$(listen_pid)" ]; then
    echo "SIGTERM 未释放端口，改发 SIGKILL：$p"
    kill -KILL "$p" 2>/dev/null || true
    sleep 0.2
  fi
  if [ -z "$(listen_pid)" ]; then
    echo "已停止，端口 $PORT 已释放。已有推理服务不受影响。"
  else
    echo "⚠️ 端口 $PORT 仍有监听（PID $(listen_pid)），请手工核查。"
    return 1
  fi
}

case "${1:-start}" in
  start|foreground) cmd_start ;;
  status)           cmd_status ;;
  stop)             cmd_stop ;;
  restart)          cmd_stop || exit 1; cmd_start ;;
  *)
    echo "用法: $0 [start|status|stop|restart]"
    echo "  start    默认；前台启动，占住当前终端（关掉该终端即停）"
    echo "  status   查看面板是否在运行"
    echo "  stop     停止面板、释放 $PORT（只停面板，不动任何推理服务）"
    echo "  restart  先停，再前台启动"
    echo "本机配置： $HERE/config.env（可选，参考 config.example.env）"
    exit 2 ;;
esac
