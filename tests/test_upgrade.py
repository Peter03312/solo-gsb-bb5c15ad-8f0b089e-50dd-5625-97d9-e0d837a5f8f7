"""端到端自测：启动升级（建表 / 旧库补列 / 索引 / 版本 0 基线）的原子性、并发与故障恢复。

覆盖：
  * 旧库（缺新列、无版本表）一次原子升级：补列、补索引、每份旧样本仅一条 baseline 版本 0，
    基线只反映升级时有效保管链与核定状态（不推断更早历史），user_version 落档；
  * 升级任一步失败整体回滚：不留补列/新表/基线等部分结果，库保持旧态；修复后可恢复重试成功；
  * 并发启动（线程与多进程）：同一持久库只升级一次，每份旧样本只构建一次基线，
    后到进程/线程沿用已完成状态；
  * 升级锁等待超时：进程启动失败不对外服务（init_db 抛错、lifespan 启动失败）；
  * 已有版本、案卷、复核记录不得重写：重跑升级前后逐行一致，HTTP 查询响应含义不变；
  * 升级完成后样本/版本/案卷 HTTP 查询保持可调用（旧库升级后直接以 HTTP 验证）。

运行：python -m tests.test_upgrade
"""
import json
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import time

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_upgrade_main.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

import app.db as dbmod  # noqa: E402
import app.main as mainmod  # noqa: E402
import app.versions as versmod  # noqa: E402
from app import config  # noqa: E402
from app.db import SCHEMA_VERSION, db_session, init_db  # noqa: E402
from app.main import app  # noqa: E402

API = "/api/v1"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


# --------------------------------------------------------------------------- 旧库构造

# 升级前的旧库结构：无 superseded_by/replaces_event_id/batch_id 列，无版本/案卷/复核等后续表
LEGACY_SCHEMA = """
CREATE TABLE sample (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sample_code TEXT NOT NULL UNIQUE,
    collected_at TEXT NOT NULL, collector TEXT NOT NULL, seal_no TEXT NOT NULL,
    content_hash TEXT NOT NULL, approved_storage_at TEXT, approved_by TEXT,
    approve_note TEXT, created_at TEXT NOT NULL);
CREATE TABLE custody_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE event_correction (
    id INTEGER PRIMARY KEY AUTOINCREMENT, correction_id TEXT NOT NULL UNIQUE, sample_id INTEGER NOT NULL,
    target_event_id TEXT NOT NULL, action TEXT NOT NULL, operator TEXT NOT NULL, reason TEXT NOT NULL,
    new_event_id TEXT, new_event_row_id INTEGER, request_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE transfer_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL UNIQUE, occurred_at TEXT NOT NULL,
    from_custodian TEXT NOT NULL, to_custodian TEXT NOT NULL, item_count INTEGER NOT NULL,
    request_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE transfer_batch_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_pk INTEGER NOT NULL, sample_id INTEGER NOT NULL,
    sample_code TEXT NOT NULL, transfer_no TEXT NOT NULL, event_id TEXT NOT NULL,
    event_row_id INTEGER NOT NULL, item_index INTEGER NOT NULL);
"""

# OLD-A 完整有效链（移交→签收→封签检查→入库）；OLD-B 有效链且已核定
LEGACY_EVENTS = {
    "OLD-A": [
        ("EVT-LA-T1", "transfer", "2026-01-01T01:00:00Z",
         {"transfer_no": "TR-LA", "from_custodian": "甲", "to_custodian": "乙"}),
        ("EVT-LA-R1", "receive", "2026-01-01T02:00:00Z",
         {"transfer_no": "TR-LA", "receiver": "乙", "seal_no": "SL-A"}),
        ("EVT-LA-C1", "seal_check", "2026-01-01T02:30:00Z",
         {"checked_seal_no": "SL-A", "seal_intact": True}),
        ("EVT-LA-S1", "storage", "2026-01-01T03:00:00Z",
         {"custodian": "实验室", "storage_location": "冷库A-01"}),
    ],
    "OLD-B": [
        ("EVT-LB-T1", "transfer", "2026-02-01T01:00:00Z",
         {"transfer_no": "TR-LB", "from_custodian": "丙", "to_custodian": "丁"}),
        ("EVT-LB-R1", "receive", "2026-02-01T02:00:00Z",
         {"transfer_no": "TR-LB", "receiver": "丁", "seal_no": "SL-B"}),
        ("EVT-LB-S1", "storage", "2026-02-01T03:00:00Z",
         {"custodian": "实验室", "storage_location": "冷库B-02"}),
    ],
}


def build_legacy_db(path: str, samples: list[str] = ("OLD-A", "OLD-B")) -> None:
    """构造升级前旧库：旧结构、无版本表、无 user_version 落档。"""
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    for i, code in enumerate(samples, start=1):
        approved = ("2026-03-01T00:00:00Z", "主管", "准予入库") if code == "OLD-B" else (None, None, None)
        events = LEGACY_EVENTS.get(code, [])
        # 采集人 = 初始保管人，须与首笔移交的交出人一致（否则按规则断链判无效）
        collector = events[0][3]["from_custodian"] if events else "采集员" + code[-1]
        con.execute(
            "INSERT INTO sample (id, sample_code, collected_at, collector, seal_no, content_hash,"
            " approved_storage_at, approved_by, approve_note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (i, code, "2026-01-01T00:00:00Z", collector, f"SL-{code[-1]}",
             "h-" + code, approved[0], approved[1], approved[2], "2026-01-01T00:00:00Z"),
        )
        for event_id, etype, occurred, payload in events:
            con.execute(
                "INSERT INTO custody_event (sample_id, event_id, event_type, occurred_at, payload_json,"
                " content_hash, created_at) VALUES (?,?,?,?,?,?,?)",
                (i, event_id, etype, occurred, json.dumps(payload, ensure_ascii=False),
                 "eh-" + event_id, occurred),
            )
    con.commit()
    con.close()


def db_snapshot(path: str) -> dict:
    """读取库的结构与关键状态，用于断言升级是否留下部分结果。"""
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    evt_cols = [r[1] for r in con.execute("PRAGMA table_info(custody_event)")]
    user_version = con.execute("PRAGMA user_version").fetchone()[0]
    sample_count = con.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
    con.close()
    return {"tables": tables, "indexes": indexes, "evt_cols": evt_cols,
            "user_version": user_version, "sample_count": sample_count}


def table_rows(path: str, table: str) -> list:
    con = sqlite3.connect(path)
    rows = con.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    con.close()
    return rows


ORIG_DB_PATH = config.DB_PATH


def use_db(path: str):
    config.DB_PATH = type(config.DB_PATH)(path)


def restore_db():
    config.DB_PATH = ORIG_DB_PATH


# ---------------------------------------------------------------- 1. 旧库一次原子升级
print("1) 旧库升级：补列 + 索引 + 每份旧样本一条 baseline 版本 0（只反映升级时状态）")
legacy1 = os.path.join(_tmp, "legacy1.db")
build_legacy_db(legacy1)
before = db_snapshot(legacy1)
check("升级前旧库无版本表/无新列/未落档",
      "sample_version" not in before["tables"] and "superseded_by" not in before["evt_cols"]
      and before["user_version"] == 0)

use_db(legacy1)
try:
    init_db()
    after = db_snapshot(legacy1)
    check("补列完成（superseded_by/replaces_event_id/batch_id）",
          all(c in after["evt_cols"] for c in ("superseded_by", "replaces_event_id", "batch_id")))
    check("版本/案卷/复核等表已建",
          {"sample_version", "dossier", "dossier_item", "review_case", "review", "review_opinion"}
          <= after["tables"])
    check("迁移后索引已建", "idx_event_effective" in after["indexes"]
          and "idx_correction_sample" in after["indexes"])
    check("升级版本号已落档", after["user_version"] == SCHEMA_VERSION, str(after["user_version"]))
    with db_session() as conn:
        for code in ("OLD-A", "OLD-B"):
            rows = conn.execute(
                "SELECT sv.* FROM sample_version sv JOIN sample s ON s.id = sv.sample_id"
                " WHERE s.sample_code = ? ORDER BY sv.version", (code,)).fetchall()
            check(f"{code} 仅一条基线版本 0", len(rows) == 1 and rows[0]["version"] == 0
                  and rows[0]["change_type"] == "baseline" and rows[0]["change_ref"] is None)
            snap = json.loads(rows[0]["snapshot_json"])
            check(f"{code} 基线反映升级时有效链（valid）", snap["status"] == "valid", snap["status"])
            check(f"{code} 基线含当时有效事件时间线", len(snap["timeline"]) == len(LEGACY_EVENTS[code]))
        snap_b = json.loads(conn.execute(
            "SELECT sv.snapshot_json FROM sample_version sv JOIN sample s ON s.id = sv.sample_id"
            " WHERE s.sample_code = 'OLD-B'").fetchone()[0])
        check("OLD-B 基线保留升级时核定状态（不推断更早历史）",
              snap_b["approved_by"] == "主管" and snap_b["approval"]["approver"] == "主管")
        # 重复执行：幂等，不重复构建基线
        init_db()
        n = conn.execute("SELECT COUNT(*) FROM sample_version").fetchone()[0]
        check("重复升级幂等（基线不重复构建）", n == 2, str(n))
finally:
    restore_db()

# ---------------------------------------------------------------- 2. 升级失败整体回滚 + 可恢复
print("2) 升级任一步失败：整体回滚不留部分结果，修复后可恢复重试")
legacy2 = os.path.join(_tmp, "legacy2.db")
build_legacy_db(legacy2)
use_db(legacy2)
try:
    # 2a. 基线构建步骤失败
    real_ensure = versmod.ensure_version_baselines

    def boom_ensure(conn):
        raise RuntimeError("模拟基线构建失败（磁盘/数据异常）")

    versmod.ensure_version_baselines = boom_ensure
    try:
        init_db()
        check("基线失败时 init_db 抛出", False)
    except RuntimeError:
        check("基线失败时 init_db 抛出", True)
    finally:
        versmod.ensure_version_baselines = real_ensure
    st = db_snapshot(legacy2)
    check("基线失败不留部分结果（无补列/无版本表/未落档）",
          "superseded_by" not in st["evt_cols"] and "sample_version" not in st["tables"]
          and st["user_version"] == 0 and st["sample_count"] == 2, str(st))

    # 2b. 索引创建步骤失败
    real_indexes = dbmod.POST_MIGRATION_INDEXES
    dbmod.POST_MIGRATION_INDEXES = real_indexes + ["CREATE INDEX idx_boom ON no_such_table(x)"]
    try:
        init_db()
        check("索引失败时 init_db 抛出", False)
    except sqlite3.OperationalError:
        check("索引失败时 init_db 抛出", True)
    finally:
        dbmod.POST_MIGRATION_INDEXES = real_indexes
    st = db_snapshot(legacy2)
    check("索引失败不留部分结果（无补列/无版本表/未落档）",
          "superseded_by" not in st["evt_cols"] and "sample_version" not in st["tables"]
          and st["user_version"] == 0, str(st))

    # 2c. 修复后重试：升级可恢复，最终完成且结果完整
    init_db()
    st = db_snapshot(legacy2)
    check("修复后重试升级成功（可恢复）",
          "superseded_by" in st["evt_cols"] and "sample_version" in st["tables"]
          and st["user_version"] == SCHEMA_VERSION)
    with db_session() as conn:
        n = conn.execute("SELECT COUNT(*) FROM sample_version").fetchone()[0]
        check("恢复后每份旧样本恰好一条基线", n == 2, str(n))
finally:
    restore_db()

# ---------------------------------------------------------------- 3. 并发启动：基线只构建一次
print("3) 并发启动（线程）：同一持久库只升级一次，后到线程沿用已完成状态")
legacy3 = os.path.join(_tmp, "legacy3.db")
build_legacy_db(legacy3, samples=[f"OLD-C{i}" for i in range(5)])
use_db(legacy3)
try:
    real_ensure = versmod.ensure_version_baselines
    build_calls = {"n": 0}

    def slow_ensure(conn):
        # 持锁期间放慢，迫使并发线程在锁外等待；统计实际执行基线构建的次数
        build_calls["n"] += 1
        time.sleep(0.3)
        return real_ensure(conn)

    versmod.ensure_version_baselines = slow_ensure
    errors: list[BaseException] = []

    def worker():
        try:
            init_db()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    try:
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        versmod.ensure_version_baselines = real_ensure
    check("4 个并发线程全部升级成功", not errors, str(errors[:1]))
    check("基线构建只执行一次（后到线程沿用已完成状态）", build_calls["n"] == 1, str(build_calls))
    with db_session() as conn:
        rows = conn.execute(
            "SELECT s.sample_code, COUNT(sv.id) AS n FROM sample s"
            " LEFT JOIN sample_version sv ON sv.sample_id = s.id GROUP BY s.id").fetchall()
        check("每份旧样本恰好一条版本 0 基线",
              len(rows) == 5 and all(r["n"] == 1 for r in rows), str([dict(r) for r in rows]))
        uv = conn.execute("PRAGMA user_version").fetchone()[0]
        check("并发后升级版本号已落档", uv == SCHEMA_VERSION, str(uv))
finally:
    restore_db()

# 3b. 多进程并发启动（两个服务进程同时启动同一持久库）
print("3b) 并发启动（多进程）：两个进程同时升级同一持久库，基线仍只构建一次")
legacy3b = os.path.join(_tmp, "legacy3b.db")
build_legacy_db(legacy3b, samples=[f"OLD-P{i}" for i in range(3)])
use_db(legacy3b)
try:
    def proc_entry(db_path, q):
        try:
            from app import config as cfg
            cfg.DB_PATH = type(cfg.DB_PATH)(db_path)
            import app.versions as vm
            real = vm.ensure_version_baselines

            def slow(conn):
                time.sleep(0.3)
                return real(conn)

            vm.ensure_version_baselines = slow
            from app.db import init_db as _init
            _init()
            q.put("ok")
        except BaseException as e:  # noqa: BLE001
            q.put(f"error:{e!r}")

    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    procs = [ctx.Process(target=proc_entry, args=(legacy3b, q)) for _ in range(2)]
    for p in procs:
        p.start()
    results = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
    check("两个进程均升级成功", results == ["ok", "ok"], str(results))
    with db_session() as conn:
        rows = conn.execute(
            "SELECT s.sample_code, COUNT(sv.id) AS n FROM sample s"
            " LEFT JOIN sample_version sv ON sv.sample_id = s.id GROUP BY s.id").fetchall()
        check("多进程并发后每份旧样本仍只有一条基线",
              len(rows) == 3 and all(r["n"] == 1 for r in rows), str([dict(r) for r in rows]))
finally:
    restore_db()

# ---------------------------------------------------------------- 4. 升级锁超时：失败进程不服务
print("4) 升级锁被长期占用：限定时间内失败（进程不得对外服务），释放后可恢复")
legacy4 = os.path.join(_tmp, "legacy4.db")
build_legacy_db(legacy4)
use_db(legacy4)
orig_timeout, orig_lock_wait = config.DB_TIMEOUT, config.UPGRADE_LOCK_TIMEOUT
try:
    # 另一进程持写锁不放
    holder = sqlite3.connect(legacy4)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO transfer_batch (batch_id, occurred_at, from_custodian, to_custodian,"
                   " item_count, request_hash, created_at) VALUES ('HOLD','t','a','b',0,'h','t')")
    config.DB_TIMEOUT = 0.2          # 单次锁等待
    config.UPGRADE_LOCK_TIMEOUT = 0.6  # 升级总等待上限
    t0 = time.monotonic()
    try:
        init_db()
        check("锁占用时 init_db 超时失败", False)
    except sqlite3.OperationalError as e:
        elapsed = time.monotonic() - t0
        check("锁占用时 init_db 超时失败", "locked" in str(e).lower(), str(e))
        check("超时在限定时长内（不死等）", elapsed < 10, f"{elapsed:.1f}s")
    st = db_snapshot(legacy4)
    check("锁超时未留下部分升级结果", "superseded_by" not in st["evt_cols"]
          and st["user_version"] == 0)
    holder.rollback()
    holder.close()
    # 锁释放后重启即可恢复
    config.UPGRADE_LOCK_TIMEOUT = 5
    init_db()
    st = db_snapshot(legacy4)
    check("锁释放后升级恢复成功", st["user_version"] == SCHEMA_VERSION
          and "sample_version" in st["tables"])
finally:
    config.DB_TIMEOUT, config.UPGRADE_LOCK_TIMEOUT = orig_timeout, orig_lock_wait
    restore_db()

# 4b. 启动升级失败 → 应用启动失败，不对外服务
print("4b) 启动升级失败 → lifespan 启动失败，进程不对外服务")
real_init = mainmod.init_db


def boom_init():
    raise RuntimeError("模拟升级失败")


mainmod.init_db = boom_init
try:
    served = False
    try:
        with TestClient(app) as c:
            served = c.get("/health").status_code == 200
    except RuntimeError:
        pass
    check("升级失败时应用不对外服务（启动即失败）", not served)
finally:
    mainmod.init_db = real_init

# ---------------------------------------------------------------- 5. 已有版本/案卷/复核记录不得重写
print("5) 已有版本、案卷、复核记录：重跑升级一律不改写，HTTP 响应含义不变")
db5 = os.path.join(_tmp, "upgrade_rewrite.db")
use_db(db5)
try:
    with TestClient(app) as client:
        # 造数：两份样本（各一条有效链）、版本累积、案卷封存、双人复核终局
        for code, seal in (("S-UP-A", "SEAL-UP-A"), ("S-UP-B", "SEAL-UP-B")):
            r = client.post(f"{API}/samples", json={
                "sample_code": code, "collected_at": "2026-09-30T01:00:00Z",
                "collector": "王采集", "seal_no": seal})
            check(f"{code} 登记 201", r.status_code == 201)
        for code, seal in (("S-UP-A", "SEAL-UP-A"), ("S-UP-B", "SEAL-UP-B")):
            client.post(f"{API}/events", json={
                "event_id": f"EVT-{code}-T1", "event_type": "transfer", "sample_code": code,
                "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": f"TR-{code}",
                "from_custodian": "王采集", "to_custodian": "李运输"})
            client.post(f"{API}/events", json={
                "event_id": f"EVT-{code}-R1", "event_type": "receive", "sample_code": code,
                "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": f"TR-{code}",
                "receiver": "李运输", "seal_no": seal})
            client.post(f"{API}/events", json={
                "event_id": f"EVT-{code}-S1", "event_type": "storage", "sample_code": code,
                "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
        r = client.post(f"{API}/dossiers", json={
            "dossier_id": "D-UP-1",
            "items": [{"sample_code": "S-UP-A"}, {"sample_code": "S-UP-B"}]})
        check("案卷封存 201", r.status_code == 201, r.text)
        digest = client.get(f"{API}/dossiers/D-UP-1").json()["dossier"]["digest"]
        for no, reviewer in (("REV-UP-1", "质控员甲"), ("REV-UP-2", "质控员乙")):
            r = client.post(f"{API}/dossiers/D-UP-1/reviews", json={
                "review_no": no, "reviewer": reviewer, "expected_digest": digest,
                "items": [{"sample_code": "S-UP-A", "passed": True},
                          {"sample_code": "S-UP-B", "passed": True}]})
            check(f"{no} 提交 201", r.status_code == 201, r.text)
        check("复核终局", client.get(f"{API}/dossiers/D-UP-1/reviews").json()["stage"] == "completed")

        # 升级前快照：库行 + HTTP 响应
        tables = ["sample_version", "dossier", "dossier_item", "review_case", "review", "review_opinion"]
        rows_before = {t: table_rows(db5, t) for t in tables}
        http_before = {
            "versions_a": client.get(f"{API}/samples/S-UP-A/versions").json(),
            "version_a2": client.get(f"{API}/samples/S-UP-A/versions/2").json(),
            "latest_a": client.get(f"{API}/samples/S-UP-A/versions/latest").json(),
            "dossier": client.get(f"{API}/dossiers/D-UP-1").json(),
            "reviews": client.get(f"{API}/dossiers/D-UP-1/reviews").json(),
            "samples": client.get(f"{API}/samples").json(),
            "chain_a": client.get(f"{API}/samples/S-UP-A/chain").json(),
        }

    # 模拟修复前旧二进制留下的库：数据齐全但 user_version 未落档 → 触发整轮升级重跑
    with db_session(immediate=True) as conn:
        conn.execute("PRAGMA user_version = 0")
    init_db()
    with db_session() as conn:
        uv = conn.execute("PRAGMA user_version").fetchone()[0]
    check("重跑升级后版本号落档", uv == SCHEMA_VERSION, str(uv))
    rows_after = {t: table_rows(db5, t) for t in tables}
    check("已有版本记录逐行未改写", rows_after["sample_version"] == rows_before["sample_version"])
    check("案卷与清单逐行未改写",
          rows_after["dossier"] == rows_before["dossier"]
          and rows_after["dossier_item"] == rows_before["dossier_item"])
    check("复核案/提交/意见逐行未改写",
          rows_after["review_case"] == rows_before["review_case"]
          and rows_after["review"] == rows_before["review"]
          and rows_after["review_opinion"] == rows_before["review_opinion"])

    with TestClient(app) as client:
        http_after = {
            "versions_a": client.get(f"{API}/samples/S-UP-A/versions").json(),
            "version_a2": client.get(f"{API}/samples/S-UP-A/versions/2").json(),
            "latest_a": client.get(f"{API}/samples/S-UP-A/versions/latest").json(),
            "dossier": client.get(f"{API}/dossiers/D-UP-1").json(),
            "reviews": client.get(f"{API}/dossiers/D-UP-1/reviews").json(),
            "samples": client.get(f"{API}/samples").json(),
            "chain_a": client.get(f"{API}/samples/S-UP-A/chain").json(),
        }
    check("重跑升级后样本/版本/案卷/复核 HTTP 响应完全一致", http_after == http_before)
    with TestClient(app) as client:
        v = client.post(f"{API}/dossiers/D-UP-1/verify").json()
        check("案卷摘要核验仍一致", v["match"] is True)
finally:
    restore_db()

# ---------------------------------------------------------------- 6. 升级后旧库 HTTP 查询可调用
print("6) 旧库升级后：样本/版本/案卷 HTTP 查询保持可调用且响应含义不变")
use_db(legacy1)  # 第 1 节升级完成的旧库
try:
    with TestClient(app) as client:
        r = client.get(f"{API}/samples")
        statuses = {x["sample_code"]: x["status"] for x in r.json()["items"]}
        check("样本列表可查且结论不变", r.status_code == 200
              and statuses == {"OLD-A": "valid", "OLD-B": "valid"}, str(statuses))
        r = client.get(f"{API}/samples/OLD-A")
        check("单样本摘要附基线版本号", r.status_code == 200
              and r.json()["evaluation"]["current_version"] == 0)
        r = client.get(f"{API}/samples/OLD-A/chain")
        body = r.json()
        check("证据链可查（4 条有效事件、当前保管人实验室）",
              r.status_code == 200 and len(body["timeline"]) == 4
              and body["current_custodian"] == "实验室" and body["current_version"] == 0)
        r = client.get(f"{API}/samples/OLD-A/versions")
        items = r.json()["items"]
        check("版本索引：仅基线版本 0", r.status_code == 200 and r.json()["current_version"] == 0
              and len(items) == 1 and items[0]["change_type"] == "baseline")
        r0 = client.get(f"{API}/samples/OLD-A/versions/0")
        check("基线版本快照可查", r0.status_code == 200
              and r0.json()["version_snapshot"]["change_type"] == "baseline"
              and r0.json()["version_snapshot"]["status"] == "valid")
        check("latest 等价于版本 0",
              client.get(f"{API}/samples/OLD-A/versions/latest").json() == r0.json())
        r = client.get(f"{API}/samples/OLD-A/versions/1")
        check("不存在版本 404", r.status_code == 404
              and r.json()["error"]["code"] == "version_not_found")
        rb = client.get(f"{API}/samples/OLD-B/versions/0")
        check("OLD-B 基线快照含核定信息", rb.status_code == 200
              and rb.json()["version_snapshot"]["approval"]["approver"] == "主管")

        # 升级后写入路径不变：新事件使版本从基线递增
        r = client.post(f"{API}/events", json={
            "event_id": "EVT-LA-C2", "event_type": "seal_check", "sample_code": "OLD-A",
            "occurred_at": "2026-01-01T04:00:00Z", "checked_seal_no": "SL-A", "seal_intact": True})
        check("升级后新事件正常写入", r.status_code == 201
              and r.json()["evaluation"]["current_version"] == 1, r.text)
        r = client.get(f"{API}/samples/OLD-A/versions")
        check("版本自基线递增（baseline→event）",
              [i["change_type"] for i in r.json()["items"]] == ["baseline", "event"])

        # 升级后案卷封存/查询/核验可用
        r = client.post(f"{API}/dossiers", json={
            "dossier_id": "D-LEG-1",
            "items": [{"sample_code": "OLD-A"}, {"sample_code": "OLD-B"}]})
        check("旧库升级后案卷封存 201", r.status_code == 201, r.text)
        r = client.get(f"{API}/dossiers/D-LEG-1")
        check("案卷可查（两份、按编号排序）", r.status_code == 200
              and r.json()["dossier"]["item_count"] == 2
              and [i["sample_code"] for i in r.json()["dossier"]["items"]] == ["OLD-A", "OLD-B"])
        v = client.post(f"{API}/dossiers/D-LEG-1/verify").json()
        check("案卷摘要核验一致", v["match"] is True)
finally:
    restore_db()

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
