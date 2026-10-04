"""运行配置：全部通过环境变量注入，提供默认值便于本地直接运行。"""
import os
from pathlib import Path

# 数据目录 / SQLite 文件位置（容器内默认 /data/chain.db，可挂载持久化）
DATA_DIR = Path(os.getenv("DATA_DIR", "/data" if os.getenv("RUNNING_IN_DOCKER") else str(Path(__file__).resolve().parent.parent / "data")))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "chain.db")))

# 服务监听地址与端口
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

# 启动时是否自动写入演示样本（VALID / 待补证 / 无效 各一条）
SEED_ON_START = os.getenv("SEED_ON_START", "true").lower() in ("1", "true", "yes")

# 入库核定后是否允许追加事件 / 修改样本。按需求恒为锁定，仅保留开关便于测试。
LOCK_AFTER_APPROVAL = os.getenv("LOCK_AFTER_APPROVAL", "true").lower() in ("1", "true", "yes")

# SQLite 写锁等待秒数
DB_TIMEOUT = float(os.getenv("DB_TIMEOUT", "10"))

# 启动升级等待秒数：并发启动时后到进程等待先到进程完成升级（建表/补列/索引/版本 0 基线）
# 的最长秒数；超时仍未完成则启动失败，进程不对外服务（修复原因后重启即可恢复重试）
UPGRADE_LOCK_TIMEOUT = float(os.getenv("UPGRADE_LOCK_TIMEOUT", "60"))
