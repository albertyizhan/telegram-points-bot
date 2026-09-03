#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
PID_FILE="$ROOT/.bot.pid"
LOG_FILE="${BOT_LOG:-$ROOT/bot.log}"

usage() {
  cat <<'EOF'
用法：./manage.sh <命令>

命令：
  start             后台启动
  stop              正常关闭
  stop --force      强制关闭卡住的进程
  restart           重启
  status            查看运行状态和最近日志
  run               前台运行（调试用）
  update            拉取 GitHub 更新、安装依赖并重启
EOF
}

load_env() {
  [[ -x "$PYTHON" ]] || { echo "未找到 .venv，请先完成安装。" >&2; exit 1; }
  [[ -f "$ROOT/.env" ]] || { echo "未找到 .env，请先复制 .env.example 并填写配置。" >&2; exit 1; }
  set -a
  . "$ROOT/.env"
  set +a
  [[ -n "${BOT_TOKEN:-}" ]] || { echo "缺少 BOT_TOKEN。" >&2; exit 1; }
  [[ -n "${OWNER_ID:-}" ]] || { echo "缺少 OWNER_ID。" >&2; exit 1; }
}

read_pid() {
  local pid=""
  [[ -s "$PID_FILE" ]] || return 1
  read -r pid < "$PID_FILE"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$pid"
}

is_bot_pid() {
  local command
  command=$(ps -p "$1" -o command= 2>/dev/null || true)
  case "$command" in *"$ROOT/bot.py"*) return 0;; esac
  return 1
}

running_pid() {
  local pid
  pid=$(read_pid) || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  is_bot_pid "$pid" || return 1
  printf '%s\n' "$pid"
}

clear_pid() { rm -f -- "$PID_FILE"; }

start_bot() {
  load_env
  local pid
  if pid=$(running_pid); then
    echo "机器人已经在运行（PID $pid）。"
    return 0
  fi
  clear_pid
  umask 077
  nohup "$PYTHON" "$ROOT/bot.py" >> "$LOG_FILE" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"
  sleep 1
  if running_pid >/dev/null; then
    echo "机器人已后台启动（PID $pid）。日志：$LOG_FILE"
  else
    echo "启动失败，最近日志：" >&2
    tail -n 30 "$LOG_FILE" 2>/dev/null || true
    clear_pid
    return 1
  fi
}

stop_bot() {
  local force=0 pid i
  [[ "${1:-}" == "--force" ]] && force=1
  pid=$(running_pid || true)
  if [[ -z "$pid" ]]; then
    clear_pid
    echo "机器人没有运行。"
    return 0
  fi
  if (( force )); then
    kill -KILL "$pid" 2>/dev/null || true
  else
    kill -TERM "$pid"
    for i in {1..20}; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "进程未在 10 秒内退出；如确认卡住，请执行 ./manage.sh stop --force。" >&2
      return 1
    fi
  fi
  clear_pid
  echo "机器人已关闭。"
}

status_bot() {
  local pid
  if pid=$(running_pid); then
    echo "运行中（PID $pid）"
    echo "日志：$LOG_FILE"
    tail -n 10 "$LOG_FILE" 2>/dev/null || true
  else
    clear_pid
    echo "未运行"
  fi
}

update_bot() {
  load_env
  local was_running=0 pid branch
  if pid=$(running_pid); then was_running=1; fi
  if ! git diff --quiet -- . ':(exclude)points.db-shm' ':(exclude)points.db-wal' || \
     ! git diff --cached --quiet -- . ':(exclude)points.db-shm' ':(exclude)points.db-wal'; then
    echo "检测到本地代码改动，已停止更新以避免覆盖。" >&2
    exit 1
  fi
  branch=$(git branch --show-current)
  [[ -n "$branch" ]] || { echo "无法确定当前 Git 分支。" >&2; exit 1; }
  (( was_running )) && stop_bot
  if ! git pull --ff-only origin "$branch"; then
    (( was_running )) && start_bot || true
    exit 1
  fi
  if ! "$PYTHON" -m pip install -r "$ROOT/requirements.txt"; then
    (( was_running )) && start_bot || true
    exit 1
  fi
  (( was_running )) && start_bot
  echo "更新完成。"
}

case "${1:-}" in
  start) shift; start_bot "$@";;
  stop) shift; stop_bot "$@";;
  restart) stop_bot; start_bot;;
  status) status_bot;;
  run) load_env; exec "$PYTHON" "$ROOT/bot.py";;
  update) update_bot;;
  -h|--help|help) usage;;
  *) usage; exit 2;;
esac
