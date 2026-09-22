#!/bin/sh
# 容器启动入口：初始化 SQLite 数据库（幂等），然后启动 Web 服务。
set -e

echo "[entrypoint] 初始化数据库 ..."
python -c "from app.database import init_db; init_db(); print('[entrypoint] 数据库就绪')"

echo "[entrypoint] 启动服务，监听 0.0.0.0:8000 ..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
