"""SQLite 连接、建表初始化与底层读写工具。"""
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sample (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_code         TEXT NOT NULL UNIQUE,            -- 样本编号（业务唯一）
    collected_at        TEXT NOT NULL,                   -- 采集时间（UTC ISO8601）
    collector           TEXT NOT NULL,                   -- 采集人（初始保管人）
    seal_no             TEXT NOT NULL,                   -- 登记封签号
    content_hash        TEXT NOT NULL,                   -- 登记内容指纹（幂等/冲突判定）
    approved_storage_at TEXT,                            -- 入库核定时间；非空即历史锁定
    approved_by         TEXT,                            -- 核定操作人
    approve_note        TEXT,                            -- 核定备注
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custody_event (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id          INTEGER NOT NULL REFERENCES sample(id),
    event_id           TEXT NOT NULL UNIQUE,             -- 全局唯一事件号
    event_type         TEXT NOT NULL CHECK (event_type IN ('transfer','receive','seal_check','storage')),
    occurred_at        TEXT NOT NULL,                    -- 事件发生时间（UTC ISO8601）
    payload_json       TEXT NOT NULL,                    -- 类型相关字段
    content_hash       TEXT NOT NULL,                    -- 事件内容指纹（同事件号重试比对）
    created_at         TEXT NOT NULL,
    superseded_by      TEXT,                             -- 被哪条纠错作废/替换（NULL=当前有效版本）
    replaces_event_id  TEXT,                             -- 替换产生的新事件：指向被替换的原事件号
    batch_id           TEXT                              -- 批量移交提交时所属批次号（单事件上报为 NULL）
);

CREATE TABLE IF NOT EXISTS event_correction (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    correction_id     TEXT NOT NULL UNIQUE,              -- 全局唯一纠错号
    sample_id         INTEGER NOT NULL REFERENCES sample(id),
    target_event_id   TEXT NOT NULL,                     -- 被纠错的事件号（提交时为当前有效版本）
    action            TEXT NOT NULL CHECK (action IN ('void','replace')),
    operator          TEXT NOT NULL,                     -- 纠错操作人
    reason            TEXT NOT NULL,                     -- 纠错原因
    new_event_id      TEXT,                              -- replace 时的新事件号（void 为 NULL）
    new_event_row_id  INTEGER REFERENCES custody_event(id), -- replace 时新事件行
    request_hash      TEXT NOT NULL,                     -- 纠错请求指纹（同纠错号幂等/冲突判定）
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_batch (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT NOT NULL UNIQUE,                -- 全局唯一批次号（同一交接车次一次提交）
    occurred_at     TEXT NOT NULL,                       -- 统一移交时间（UTC ISO8601）
    from_custodian  TEXT NOT NULL,                       -- 交出人
    to_custodian    TEXT NOT NULL,                       -- 接收人
    item_count      INTEGER NOT NULL,                    -- 批内样本份数
    request_hash    TEXT NOT NULL,                       -- 整批内容指纹（同批次号幂等/冲突判定）
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_batch_item (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_pk     INTEGER NOT NULL REFERENCES transfer_batch(id),
    sample_id    INTEGER NOT NULL REFERENCES sample(id),
    sample_code  TEXT NOT NULL,                          -- 提交时样本编号（批次原始归属留痕）
    transfer_no  TEXT NOT NULL,                          -- 该份样本移交单号
    event_id     TEXT NOT NULL,                          -- 该份样本对应的既有 transfer 事件号
    event_row_id INTEGER NOT NULL REFERENCES custody_event(id),
    item_index   INTEGER NOT NULL                       -- 提交顺序（0 起）
);

-- 批量移交到达后的整批对账签收（接收方）：一个批次仅一次成功对账。
-- 接收方以全局唯一签收单号、签收人与统一签收时间，逐份提交覆盖原批次全部样本的
-- 对账清单：收妥项落为既有 receive 事件（与各样本新版本同事务提交），
-- 异常项仅留对账记录、不生成签收事件。失败整单拒绝且不留任何记录。
CREATE TABLE IF NOT EXISTS batch_reconciliation (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    reconciliation_no TEXT NOT NULL UNIQUE,              -- 全局唯一签收单号（同号重试幂等/异内容冲突）
    batch_pk          INTEGER NOT NULL UNIQUE REFERENCES transfer_batch(id), -- 一个批次只能成功对账一次
    receiver          TEXT NOT NULL,                    -- 签收人（须匹配该份在途移交的接收人）
    occurred_at       TEXT NOT NULL,                     -- 统一签收时间（UTC ISO8601）
    item_count        INTEGER NOT NULL,                 -- 清单份数（=原批次份数）
    received_count    INTEGER NOT NULL,                 -- 收妥份数
    exception_count   INTEGER NOT NULL,                 -- 异常份数
    request_hash      TEXT NOT NULL,                    -- 对账请求指纹（同签收单号幂等/冲突判定）
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_reconciliation_item (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    reconciliation_pk INTEGER NOT NULL REFERENCES batch_reconciliation(id),
    batch_item_pk     INTEGER NOT NULL REFERENCES transfer_batch_item(id),
    item_index        INTEGER NOT NULL,                 -- 原批次明细顺序（0 起，按原批次顺序呈现）
    sample_id         INTEGER NOT NULL REFERENCES sample(id),
    sample_code       TEXT NOT NULL,                    -- 提交时样本编号（原始归属留痕）
    transfer_no       TEXT NOT NULL,                    -- 原批次该份移交单号（留痕，不改写）
    outcome           TEXT NOT NULL CHECK (outcome IN ('received','exception')),
    event_id          TEXT,                             -- 收妥项生成的既有 receive 事件号（异常项为 NULL）
    event_row_id      INTEGER REFERENCES custody_event(id), -- 收妥项 receive 事件行
    verified_seal_no  TEXT,                             -- 收妥项实核封签号（异常项为 NULL）
    reason            TEXT,                             -- 异常项原因（收妥项为 NULL）
    UNIQUE (reconciliation_pk, sample_code)
);

-- 样本保管链版本（质控复核用）：版本 0 为登记/升级基线快照，此后每次真正提交
-- （单事件、批量移交中该样本事件、作废/替换纠错、入库核定）追加一个不可变快照行。
-- 版本按提交先后（id/created_at）排列，补录事件的较早发生时间不改写旧版。
CREATE TABLE IF NOT EXISTS sample_version (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id     INTEGER NOT NULL REFERENCES sample(id),
    version       INTEGER NOT NULL,                     -- 样本内版本号，从 0 起严格递增
    change_type   TEXT NOT NULL CHECK (change_type IN
                    ('register','event','batch_transfer','correction','approval','baseline')),
    change_ref    TEXT,                                 -- 触发版本的业务号（事件号/批次号/纠错号；register/baseline 为 NULL）
    snapshot_json TEXT NOT NULL,                        -- 提交时点完整核验快照（结论/原因/保管人/时间线/纠错/核定）
    created_at    TEXT NOT NULL,
    UNIQUE (sample_id, version)
);

-- 案卷封存（质控）：同一抽检的多份样本保管链版本一次封存为不可变案卷。
-- 案卷永久保存所选版本的完整快照与按样本编号排序的清单，并持有可复算的
-- SHA-256 摘要；后续补录/纠错/核定不改变旧案卷。
CREATE TABLE IF NOT EXISTS dossier (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dossier_id    TEXT NOT NULL UNIQUE,                -- 全局唯一案卷号
    item_count    INTEGER NOT NULL,                    -- 卷内样本份数（>=2）
    digest        TEXT NOT NULL,                       -- 封存内容 SHA-256 摘要（可复算）
    request_hash  TEXT NOT NULL,                       -- 封存请求指纹（同案卷号幂等/冲突判定）
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dossier_item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dossier_pk    INTEGER NOT NULL REFERENCES dossier(id),
    sample_id     INTEGER NOT NULL REFERENCES sample(id),
    sample_code   TEXT NOT NULL,                       -- 封存时样本编号（清单留痕）
    version       INTEGER NOT NULL,                    -- 封存时选取的版本号（非负）
    item_index    INTEGER NOT NULL,                    -- 按样本编号排序后的序号（0 起）
    snapshot_json TEXT NOT NULL,                       -- 所选版本完整快照（永久保存）
    UNIQUE (dossier_pk, sample_code)
);

-- 案卷双人独立复核（质控）：一个案卷对应一个复核案；前两份为两名不同复核人的
-- 完整清单（seq 1/2，kind=review），有分歧的份项再由未参与前两次复核的第三人
-- 裁决（seq 3，kind=adjudication，仅含分歧份项）。意见提交后不可改。
CREATE TABLE IF NOT EXISTS review_case (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dossier_pk    INTEGER NOT NULL UNIQUE REFERENCES dossier(id),
    digest        TEXT NOT NULL,                       -- 首次提交时核对的案卷摘要副本
    final_json    TEXT,                                -- 案卷终局结论（全部份项终局后固化，null=未终局）
    finalized_at  TEXT,                                -- 案卷结论形成时间（UTC ISO8601）
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    case_pk       INTEGER NOT NULL REFERENCES review_case(id),
    review_no     TEXT NOT NULL UNIQUE,                -- 全局唯一复核单号（同号重试幂等/异内容冲突）
    seq           INTEGER NOT NULL,                    -- 提交次序：1/2 独立复核，3 第三人裁决
    kind          TEXT NOT NULL CHECK (kind IN ('review','adjudication')),
    reviewer      TEXT NOT NULL,                       -- 复核人标识（前两份须不同，第三人须未参与前两次）
    reviewer_person_id TEXT,                           -- 提交时解析的人员标识归属（NULL=未登记/升级前旧意见，不伪造）
    request_hash  TEXT NOT NULL,                       -- 请求指纹（同复核单号幂等/冲突判定）
    created_at    TEXT NOT NULL,
    UNIQUE (case_pk, seq),
    UNIQUE (case_pk, reviewer)                         -- 复核人在同一案卷内不得重复（前两名互异、第三人未参与前两次）
);

CREATE TABLE IF NOT EXISTS review_opinion (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    review_pk        INTEGER NOT NULL REFERENCES review(id),
    dossier_item_pk  INTEGER NOT NULL REFERENCES dossier_item(id),
    sample_code      TEXT NOT NULL,                    -- 卷内样本编号（须命中封存清单）
    item_index       INTEGER NOT NULL,                 -- 封存清单序号（0 起，按样本编号排序）
    passed           INTEGER NOT NULL,                 -- 该份结论：1=通过，0=不通过
    reason           TEXT,                             -- 不通过原因（通过为 NULL）
    UNIQUE (review_pk, sample_code)
);

-- 人员标识与历史别名（质控复核回避）：人员以全局唯一主标识登记，可登记/增补
-- 历史别名；主标识与全部别名构成全局唯一命名空间——别名不得归属多人、不得
-- 转移归属（不提供删除/转移）。增补别名须携带所见修订号（过期拒绝）。
CREATE TABLE IF NOT EXISTS person (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id     TEXT NOT NULL UNIQUE,                -- 人员标识（主标识，全局唯一）
    revision      INTEGER NOT NULL,                    -- 修订号：登记为 1，每次成功增补别名 +1
    request_hash  TEXT NOT NULL,                       -- 登记请求指纹（同标识幂等/冲突判定）
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_alias (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_pk   INTEGER NOT NULL REFERENCES person(id),
    alias       TEXT NOT NULL UNIQUE,                  -- 别名全局唯一：不得归属多人、不得转移
    created_at  TEXT NOT NULL
);

-- 运输测温证据（未核定样本）：每份样本一次性设定允许摄氏温度闭区间与正数最大测温
-- 间隔（秒），设定后不可改（同内容重试幂等、异内容冲突）；读数按全局唯一读数号记录
-- 设备号/发生时间/有限数值，采集前、重复发生时刻或同号异内容拒绝，同号同内容重试
-- 返回原记录。测温配置与读数不参与保管链核验结论、不产生保管链版本；核定后拒绝
-- 新配置与新读数（同号同内容重试与既有事件接口一致仍幂等返回）。
CREATE TABLE IF NOT EXISTS sample_temp_config (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id            INTEGER NOT NULL UNIQUE REFERENCES sample(id), -- 一次性设定（每份样本至多一条）
    min_celsius          REAL NOT NULL,                -- 允许温度闭区间下限（摄氏）
    max_celsius          REAL NOT NULL,                -- 允许温度闭区间上限（摄氏）
    max_interval_seconds INTEGER NOT NULL,             -- 正数最大测温间隔（秒）
    request_hash         TEXT NOT NULL,                -- 配置内容指纹（同内容重试幂等/异内容冲突）
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS temp_reading (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id     INTEGER NOT NULL REFERENCES sample(id),
    reading_id    TEXT NOT NULL UNIQUE,              -- 全局唯一读数号
    device_id     TEXT NOT NULL,                     -- 测温设备号
    occurred_at   TEXT NOT NULL,                     -- 读数发生时间（UTC ISO8601）
    value_celsius REAL NOT NULL,                     -- 摄氏温度读数（有限数值）
    content_hash  TEXT NOT NULL,                     -- 读数内容指纹（同读数号重试比对）
    created_at    TEXT NOT NULL,
    UNIQUE (sample_id, occurred_at)                  -- 同一样本同一发生时刻只允许一条读数
);

-- 测温设备校准证明：证明号全局唯一，按已有读数中出现过的设备号登记。有效期左闭右开
-- [valid_from, valid_to)；同设备未作废证明时段不得首尾之外重叠（相邻端点允许）。
-- 证明可晚于运输记录补录，查询时即时匹配读数；证明登记/作废不改动读数、测温查询、
-- 保管链版本或已封存案卷。
CREATE TABLE IF NOT EXISTS device_calibration (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_id  TEXT NOT NULL UNIQUE,            -- 全局唯一证明号（同号幂等/异内容冲突）
    device_id       TEXT NOT NULL,                   -- 测温设备号（须已有读数）
    valid_from      TEXT NOT NULL,                   -- 有效起始时刻（含，UTC ISO8601）
    valid_to        TEXT NOT NULL,                   -- 有效结束时刻（不含，UTC ISO8601）
    issued_by       TEXT NOT NULL,                   -- 签发人
    request_hash    TEXT NOT NULL,                   -- 登记请求指纹
    created_at      TEXT NOT NULL,
    voided_at       TEXT,                            -- 作废时间；NULL=有效
    void_reason     TEXT,                            -- 作废原因
    void_request_hash TEXT                           -- 作废请求指纹（同证明号作废幂等/冲突）
);

CREATE INDEX IF NOT EXISTS idx_event_sample_time ON custody_event(sample_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_version_sample ON sample_version(sample_id, version);
CREATE INDEX IF NOT EXISTS idx_review_case_dossier ON review_case(dossier_pk);
CREATE INDEX IF NOT EXISTS idx_review_opinion_review ON review_opinion(review_pk, item_index);
CREATE INDEX IF NOT EXISTS idx_person_alias_person ON person_alias(person_pk);
CREATE INDEX IF NOT EXISTS idx_temp_reading_sample_time ON temp_reading(sample_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_temp_reading_device_time ON temp_reading(device_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_calibration_device_active ON device_calibration(device_id, voided_at, valid_from, valid_to);

-- 可信测温复核异常处置（运输质控）：质控人对可信测温复核当前仍存在的超温点/缺测区间
-- 留存处置意见。每条处置记录提交时所见的稳定异常标识、证据摘要指纹与完整证据快照；
-- 异常项以稳定标识（oor:<读数号> / gap:<前后有证读数号>）追踪，同一异常同一证据摘要
-- 至多一条“有效”处置（部分唯一索引）。每次真正提交的读数/证明补录或作废、保管链
-- 事件（单事件/批量移交/整批对账收妥）与纠错使异常消失或摘要变化时，在提交事务内即
-- 对账标记失效（永久可查、不删除、不改写、证据恢复不复活），查询/提交时再兜底对账，
-- 新摘要可重新处置。处置只写本表，不改动原始读数、校准证明、测温复核结论、保管链
-- 版本与已封存案卷。
CREATE TABLE IF NOT EXISTS temp_anomaly_disposition (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    disposition_no  TEXT NOT NULL UNIQUE,            -- 全局唯一处置单号（同号同内容幂等/异内容冲突）
    sample_id       INTEGER NOT NULL REFERENCES sample(id),
    sample_code     TEXT NOT NULL,                   -- 提交时样本编号（留痕）
    anomaly_id      TEXT NOT NULL,                   -- 稳定异常标识：oor:<读数号> / gap:<前读数号>:<后读数号>
    anomaly_type    TEXT NOT NULL CHECK (anomaly_type IN ('out_of_range_point','gap_interval')),
    evidence_digest TEXT NOT NULL,                   -- 提交方所见证据摘要指纹（须与当前摘要一致）
    decision        TEXT NOT NULL CHECK (decision IN ('acknowledged','excluded')), -- 认可异常 / 排除异常
    reason          TEXT NOT NULL,                   -- 处置理由
    handler         TEXT NOT NULL,                   -- 处置人
    evidence_json   TEXT NOT NULL,                   -- 提交时所见证据完整快照（失效后仍可查）
    request_hash    TEXT NOT NULL,                   -- 请求指纹（同处置单号幂等/冲突判定）
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','invalidated')),
    invalidated_at  TEXT,                            -- 失效时间；NULL=有效
    invalidate_reason TEXT CHECK (invalidate_reason IS NULL
                    OR invalidate_reason IN ('anomaly_gone','evidence_changed')),
    current_evidence_digest TEXT,                   -- evidence_changed 时重算的当前摘要（其余为 NULL）
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_anomaly_disp_sample ON temp_anomaly_disposition(sample_id, status, id);
-- 同一异常项在同一证据摘要下至多一条有效处置；失效历史不受限（同一摘要重新出现可再处置）
CREATE UNIQUE INDEX IF NOT EXISTS idx_anomaly_disp_active_once
    ON temp_anomaly_disposition(anomaly_id, evidence_digest) WHERE status = 'active';
"""

# 依赖迁移新列的索引（须在旧库补列迁移完成后再创建）
POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_event_effective ON custody_event(sample_id, superseded_by, occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_correction_sample ON event_correction(sample_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_event_batch ON custody_event(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_batch_item_batch ON transfer_batch_item(batch_pk, item_index)",
    "CREATE INDEX IF NOT EXISTS idx_dossier_item_dossier ON dossier_item(dossier_pk, item_index)",
    "CREATE INDEX IF NOT EXISTS idx_recon_item_recon ON batch_reconciliation_item(reconciliation_pk, item_index)",
]

# 旧库增量补列
_MIGRATION_COLUMNS = {
    "custody_event": [
        ("superseded_by", "TEXT"),
        ("replaces_event_id", "TEXT"),
        ("batch_id", "TEXT"),
    ],
    "review": [
        ("reviewer_person_id", "TEXT"),                # 升级前旧意见保持 NULL，不伪造身份记录
    ],
}

# 启动升级版本号（写入 SQLite user_version，随升级事务一同提交/回滚）：
# 1 = 建表 + 旧库补列 + 索引 + 版本 0 基线 的整轮原子升级。
# 已完成的库（user_version >= SCHEMA_VERSION）启动时直接沿用，不重复执行。
SCHEMA_VERSION = 1


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_connection() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), timeout=config.DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    # WAL 提升读写并发，遇到锁立即报错由服务层转 409
    conn.execute("PRAGMA journal_mode=WAL");
    conn.execute("PRAGMA foreign_keys=ON");
    conn.execute("PRAGMA busy_timeout=%d" % int(config.DB_TIMEOUT * 1000))
    return conn


@contextmanager
def db_session(commit: bool = True, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """事务上下文。

    immediate=True 时以 BEGIN IMMEDIATE 开启，事务一开始即持有写锁，
    使所有写操作（事件上报 / 纠错 / 核定）在 SQLite 层严格串行：
    纠错与核定并发时后到者必然看到先到者已提交的最终状态。
    """
    conn = get_connection()
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """进程启动初始化：把建表、旧库补列、索引与版本 0 基线作为一次可恢复升级。

    * 原子：全部步骤在同一 `BEGIN IMMEDIATE` 事务内执行，任一步失败整体回滚，
      不留下部分升级结果（SQLite 的 DDL 与 user_version 均随事务回滚）；
      修复原因后重启即从头重试（各步骤幂等），升级可恢复。
    * 并发：同一持久库被两个服务进程同时启动时，先到进程持写锁完成升级，
      后到进程在 `UPGRADE_LOCK_TIMEOUT` 内等待锁释放，随后读到已提交的
      `user_version` 直接沿用已完成状态——每份旧样本的版本 0 基线只构建一次。
    * 失败即不服务：升级在限定时长内仍无法完成（锁长期被占、磁盘错误等）时
      抛出异常，启动流程（entrypoint / lifespan）随之失败，进程不对外服务。
    * 不重写历史：基线仅以升级时状态为尚无版本的旧样本补建版本 0，
      已有版本、案卷与复核记录一律不改写。
    """
    deadline = time.monotonic() + config.UPGRADE_LOCK_TIMEOUT
    waited = False
    while True:
        try:
            with db_session(immediate=True) as conn:
                current = conn.execute("PRAGMA user_version").fetchone()[0]
                if current >= SCHEMA_VERSION:
                    # 已完成（含并发场景下先到进程刚提交的结果），直接沿用
                    return
                created = _apply_upgrade(conn)
            if created:
                print(f"[init_db] 升级完成：为 {created} 份旧样本补建版本 0 基线", file=sys.stderr)
            if waited:
                print("[init_db] 已等待到其它进程释放升级锁，本轮升级完成", file=sys.stderr)
            return
        except sqlite3.OperationalError as exc:
            # 仅锁竞争可等待重试；其余错误（磁盘满、权限、数据异常等）立即失败
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            if not waited:
                waited = True
                print("[init_db] 另一进程正在执行启动升级，等待其完成 ...", file=sys.stderr)
            time.sleep(0.1)


def _split_statements(script: str) -> list[str]:
    """把建表脚本拆成单条语句（脚本内的字符串字面量与注释均不含 ASCII 分号）。

    不能用 executescript：它会先隐式提交，破坏升级事务的整体回滚能力。
    """
    return [s.strip() for s in script.split(";") if s.strip()]


def _apply_upgrade(conn: sqlite3.Connection) -> int:
    """在调用方持有的升级事务内执行整轮升级（任一步失败随事务整体回滚）。

    返回本次补建的版本 0 基线条数（供提交后记录日志）。
    """
    # 1) 建表与内置索引（IF NOT EXISTS，对新库建全量、对旧库补齐缺失）
    for stmt in _split_statements(SCHEMA):
        conn.execute(stmt)
    # 2) 旧库增量补列（逐列检查，已存在则跳过）
    for table, columns in _MIGRATION_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # 3) 依赖迁移新列的索引（须在补列之后创建）
    for stmt in POST_MIGRATION_INDEXES:
        conn.execute(stmt)
    # 4) 版本 0 基线：为尚无版本的旧样本以升级时状态各补建一条 baseline，
    #    不推断更早历史；已有版本/案卷/复核记录不改写。幂等，逐样本跳过已完成者。
    #    惰性导入避免 db ↔ service 的模块级循环依赖。
    from .versions import ensure_version_baselines

    created = ensure_version_baselines(conn)
    # 5) 全部步骤成功后记录升级版本号（随同事务提交/回滚）
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return created


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
