@echo off
REM 开发启动：127.0.0.1:8000 + 热重载；先确认 .venv 与 .env 存在
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" echo [ERROR] 未找到 .venv，请先执行 python -m venv .venv 并安装 requirements.txt & exit /b 1
if not exist ".env" echo [WARN] 未找到 .env，请从 .env.example 复制并填写 APP_JWT_SECRET，否则服务启动会直接失败
echo [INFO] 开发模式启动 http://127.0.0.1:8000
".venv\Scripts\python.exe" -m uvicorn src.app.main:app --host 127.0.0.1 --port 8000 --reload
