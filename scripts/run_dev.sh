#!/usr/bin/env bash
# 开发启动：127.0.0.1:8000 + 热重载；先确认 .venv 与 .env 存在
set -u
cd "$(dirname "$0")/.." || exit 1
PY="./.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[ERROR] 未找到 .venv，请先执行 python -m venv .venv 并安装 requirements.txt"
  exit 1
fi
if [ ! -f ".env" ]; then
  echo "[WARN] 未找到 .env，请从 .env.example 复制并填写 APP_JWT_SECRET，否则服务启动会直接失败"
fi
echo "[INFO] 开发模式启动 http://127.0.0.1:8000"
exec "$PY" -m uvicorn src.app.main:app --host 127.0.0.1 --port 8000 --reload
