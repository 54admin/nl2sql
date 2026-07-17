#!/usr/bin/env bash
# nl2sql AI问数 启动脚本
#   配置文件：config/application.yml（postgres 数据库连接 / redis / llm 兜底）
#   模型配置（apikey/model/base_url）：启动后 PUT /api/admin/llm-config 存数据库（优先于 yml）
#   接口文档：http://127.0.0.1:8000/docs
set -e
cd "$(dirname "$0")"

# 依赖兜底（没装就装）
python3 -c "import uvicorn, fastapi" 2>/dev/null || python3 -m pip install -q uvicorn fastapi

exec python3 -m uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
