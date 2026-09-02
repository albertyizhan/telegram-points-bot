#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "未找到 .venv，请先按 README 完成安装。" >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "未找到 .env，请先复制 .env.example 并填写配置。" >&2
  exit 1
fi

set -a
. "$ROOT/.env"
set +a
exec .venv/bin/python bot.py
