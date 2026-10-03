#!/bin/bash
# 一键启动:建虚拟环境 -> 装依赖 -> 启动服务
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r backend/requirements.txt
if [ -f .env ]; then
  set -a; source .env; set +a
fi
cd backend
exec ../.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 --reload

