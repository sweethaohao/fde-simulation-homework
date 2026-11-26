#!/usr/bin/env bash
# 生产启动：先探 Ollama（退出码 2），再查密钥（退出码 3），最后 0.0.0.0:8000 双 worker
set -u
cd "$(dirname "$0")/.." || exit 1

# 1) 本地模型必须可达
if ! curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "[ERROR] 无法连接本机 Ollama：http://127.0.0.1:11434"
  echo "        请先启动 Ollama 服务，并确认 qwen3.5:9b 与 nomic-embed-text 已拉取"
  exit 2
fi

# 2) JWT 密钥必须有值（环境变量优先，其次 .env）
HAS_SECRET=0
if [ -n "${APP_JWT_SECRET:-}" ]; then HAS_SECRET=1; fi
if [ "$HAS_SECRET" -eq 0 ] && [ -f ".env" ] && grep -qE '^APP_JWT_SECRET=.+' ".env"; then HAS_SECRET=1; fi
if [ "$HAS_SECRET" -eq 0 ]; then
  echo "[ERROR] 未设置 APP_JWT_SECRET"
  echo "        请从 .env.example 复制为 .env，并填入至少 32 字符的随机密钥"
  exit 3
fi

# 3) 虚拟环境与数据库产物检查
PY="./.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[ERROR] 未找到 .venv，请先安装依赖"
  exit 1
fi
if [ ! -f "data/mvp.db" ]; then
  echo "[WARN] 未找到 data/mvp.db，首次部署请依次运行 gen_data.py、init_db.py、seed_graph.py"
fi

echo "[INFO] 生产模式启动 http://0.0.0.0:8000 （局域网请用本机 IP 访问）"
exec "$PY" -m uvicorn src.app.main:app --host 0.0.0.0 --port 8000 --workers 2
