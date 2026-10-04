#!/bin/sh
# 容器入口：初始化依赖（已在镜像内安装）、执行启动升级、按需写入演示数据，然后启动服务。
set -e

# 启动升级：建表、旧库补列、索引与版本 0 基线作为一次原子事务执行。
# 任一步失败整体回滚且本命令非零退出 → set -e 使容器启动失败，不对外服务；
# 修复原因后重启容器即可从头重试（升级可恢复，各步骤幂等）。
# 两个进程并发启动同一持久库时，先到进程完成升级，后到进程等待并沿用已完成状态。
echo "[entrypoint] 执行启动升级（建表/补列/索引/版本 0 基线，原子可恢复）..."
python -c "from app.db import init_db; init_db()"

if [ "${SEED_ON_START:-true}" = "true" ]; then
  echo "[entrypoint] 写入演示数据（库非空时自动跳过）..."
  python -m app.seed || echo "[entrypoint] 演示数据初始化跳过（可能已存在）"
fi

echo "[entrypoint] 启动 uvicorn: ${HOST:-0.0.0.0}:${PORT:-8080}"
exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8080}"
