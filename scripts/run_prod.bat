@echo off
REM 生产启动：按施工图第 14 节，先探 Ollama（退出码 2），再查密钥（退出码 3），最后 0.0.0.0:8000 双 worker
setlocal
cd /d "%~dp0.."

REM 1) 本地模型必须可达，否则建议链路会整体降级，生产不允许带病启动
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://127.0.0.1:11434/api/tags | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo [ERROR] 无法连接本机 Ollama：http://127.0.0.1:11434
  echo         请先运行 G:\Ollama\start-serve.bat，并确认 qwen3.5:9b 与 nomic-embed-text 已拉取
  exit /b 2
)

REM 2) JWT 密钥必须有值：环境变量优先，其次 .env（应用启动时也会再校验一次）
set "HAS_SECRET="
if defined APP_JWT_SECRET set "HAS_SECRET=1"
if not defined HAS_SECRET if exist ".env" findstr /r /c:"^APP_JWT_SECRET=." ".env" >nul 2>&1 && set "HAS_SECRET=1"
if not defined HAS_SECRET (
  echo [ERROR] 未设置 APP_JWT_SECRET
  echo         请从 .env.example 复制为 .env，并填入至少 32 字符的随机密钥
  exit /b 3
)

REM 3) 虚拟环境与数据库产物检查（数据库可复现，缺失时提示先建库）
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] 未找到 .venv，请先安装依赖
  exit /b 1
)
if not exist "data\mvp.db" (
  echo [WARN] 未找到 data\mvp.db，首次部署请依次运行 gen_data.py、init_db.py、seed_graph.py
)

echo [INFO] 生产模式启动 http://0.0.0.0:8000 （局域网请用本机 IP 访问）
".venv\Scripts\python.exe" -m uvicorn src.app.main:app --host 0.0.0.0 --port 8000 --workers 2
