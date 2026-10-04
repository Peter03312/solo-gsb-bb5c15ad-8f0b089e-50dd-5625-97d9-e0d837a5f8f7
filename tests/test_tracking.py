"""端到端自测：异常处置失效追踪修复（提交时对账）。

修复点：旧实现只在查询/提交处置时惰性对账，两次处置查询之间发生“证据变化后又恢复”
（如补录移交使归属变为在途、随后作废该移交）会漏记中间变化——最终摘要恢复原值，旧意见
仍被显示为有效。本轮在每次“真正提交”的读数、校准证明、有效保管链变化（单事件、批量
移交、整批对账签收、作废/替换纠错）的同一写事务内立即对账：异常消失或摘要改变即永久
标记失效（invalidated_by 记录触发变化），证据恢复不复活，当前异常显示未处置；失败操作
与同内容重试不误触发；并发按提交顺序判定。

运行：python -m tests.test_tracking
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_tracking.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db_session, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()
API = "/api/v1"
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def reg(code, collector="王采集", seal=None, when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when, "collector": collector,
        "seal_no": seal or f"SEAL-{code}"})


def cfg(code, lo=2.0, hi=8.0, interval=1800):
    return client.post(f"{API}/temperature-configs", json={
        "sample_code": code, "min_celsius": lo, "max_celsius": hi,
        "max_interval_seconds": interval})


def rd(code, rid, when, value, dev=None):
    return client.post(f"{API}/temperature-readings", json={
        "sample_code": code, "reading_id": rid, "device_id": dev or f"DEV-{code}",
        "occurred_at": when, "value_celsius": value})


def cert(cid, dev, start, end, issuer="计量员甲"):
    return client.post(f"{API}/temperature-calibrations", json={
        "certificate_id": cid, "device_id": dev, "valid_from": start,
        "valid_to": end, "issued_by": issuer})


def void(cid, reason="证明复核作废"):
    return client.post(f"{API}/temperature-calibrations/{cid}/void", json={"reason": reason})


def ev(event_id, etype, code, when, **kw):
    return client.post(f"{API}/events", json={
        "event_id": event_id, "event_type": etype, "sample_code": code,
        "occurred_at": when, **kw})


def correction(cid, code, target, action="void", operator="链管理员", reason="纠错", new_event=None):
    body = {"correction_id": cid, "sample_code": code, "target_event_id": target,
            "action": action, "operator": operator, "reason": reason}
    if new_event is not None:
        body["new_event"] = new_event
    return client.post(f"{API}/corrections", json=body)


def anomalies(code):
    return client.get(f"{API}/samples/{code}/temperature/anomalies")


def anomaly_of(v, aid):
    return next(x for x in v["items"] if x["anomaly_id"] == aid)


def ledger(code, status=None):
    q = f"?status={status}" if status else ""
    return client.get(f"{API}/samples/{code}/temperature/anomalies/dispositions{q}")


def first_active(code):
    items = ledger(code, "active").json()["items"]
    return items[0] if items else None


def first_invalidated(code):
    items = ledger(code, "invalidated").json()["items"]
    return items[0] if items else None


def dispose(code, no, aid, digest, decision="acknowledged", reason="认可该异常", handler="质控员赵"):
    return client.post(f"{API}/samples/{code}/temperature/anomalies/dispositions", json={
        "disposition_no": no, "sample_code": code, "anomaly_id": aid,
        "expected_evidence_digest": digest, "decision": decision,
        "reason": reason, "handler": handler})


# ================================================================ 1. 核心缺陷：补录→作废，证据恢复，旧意见不复活
print("1) 核心场景：补录移交（归属变在途）→作废该移交（摘要恢复原值），旧意见仍永久失效")
reg("TRK-1")
cfg("TRK-1")
# 入库事件先存在（无移交链时不改变 02:00 的保管归属；用于验证“无关事件提交不误触发”）
ev("T1-EVT-S1", "storage", "TRK-1", "2026-09-30T05:00:00Z", custodian="实验室")
rd("TRK-1", "T1-RD-1", "2026-09-30T02:00:00Z", 9.0)
cert("T1-C1", "DEV-TRK-1", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
v = anomalies("TRK-1").json()
pid = "oor:T1-RD-1"
d0 = anomaly_of(v, pid)["evidence_digest"]
check("初始归属为采集人（custodian 王采集）",
      anomaly_of(v, pid)["attribution"] == {"kind": "custodian", "custodian": "王采集"}, str(v))
r = dispose("TRK-1", "T1-DISP-1", pid, d0)
check("处置成功 201", r.status_code == 201, r.text)

# 同内容重试读数：200 幂等，不误触发失效
r = rd("TRK-1", "T1-RD-1", "2026-09-30T02:00:00Z", 9.0)
check("读数同内容重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
check("重试后处置仍有效", first_active("TRK-1") is not None
      and first_invalidated("TRK-1") is None)

# 失败操作：采集前读数 422、重复时刻读数 409 —— 均不误触发
r = rd("TRK-1", "T1-RD-BAD", "2026-09-30T00:30:00Z", 5.0)
check("采集前读数 422", r.status_code == 422 and r.json()["error"]["code"] == "reading_before_collection", r.text)
r = rd("TRK-1", "T1-RD-DUP", "2026-09-30T02:00:00Z", 4.0)
check("重复时刻读数 409", r.status_code == 409, r.text)
check("失败操作后处置仍有效", first_active("TRK-1") is not None
      and ledger("TRK-1").json()["total"] == 1)

# 补录移交：01:30 移交、02:30 签收 → 02:00 的读数落在在途区间（先只补移交）
r = ev("T1-EVT-TX", "transfer", "TRK-1", "2026-09-30T01:30:00Z",
       transfer_no="T1-TR-X", from_custodian="王采集", to_custodian="李运输")
check("补录移交 201", r.status_code == 201, r.text)
# —— 关键：不经过异常查询，直接查台账，失效应已在移交提交事务内落定 ——
row = first_invalidated("TRK-1")
check("移交提交后立即失效（无需查询触发）：evidence_changed",
      row is not None and row["status"] == "invalidated"
      and row["invalidate_reason"] == "evidence_changed"
      and row["invalidated_at"] is not None
      and row["current_evidence_digest"] is not None
      and row["current_evidence_digest"] != d0, str(row))
check("invalidated_by 记录触发变化（单事件 transfer，事件号留痕）",
      row["invalidated_by"] == {"kind": "custody_event", "ref": "T1-EVT-TX", "action": "transfer"},
      str(row["invalidated_by"]))
check("失效后无有效处置", first_active("TRK-1") is None)

r = ev("T1-EVT-RX", "receive", "TRK-1", "2026-09-30T02:30:00Z",
       transfer_no="T1-TR-X", receiver="李运输", seal_no="SEAL-TRK-1")
check("补录签收 201", r.status_code == 201, r.text)
v = anomalies("TRK-1").json()
check("读数归属已变为在途、摘要不同于初始",
      anomaly_of(v, pid)["attribution"]["kind"] == "in_transit"
      and anomaly_of(v, pid)["evidence_digest"] != d0, str(v))

# 作废补录的移交与签收（两次纠错）：保管链恢复，02:00 重归采集人，摘要恢复 d0
r = correction("T1-FIX-RX", "TRK-1", "T1-EVT-RX", reason="撤销补录签收")
check("作废旧签收成功", r.status_code in (200, 201), r.text)
r = correction("T1-FIX-TX", "TRK-1", "T1-EVT-TX", reason="撤销补录移交")
check("作废旧移交成功", r.status_code in (200, 201), r.text)
v = anomalies("TRK-1").json()
p_now = anomaly_of(v, pid)
check("证据恢复：归属回到采集人、摘要恢复原值",
      p_now["attribution"] == {"kind": "custodian", "custodian": "王采集"}
      and p_now["evidence_digest"] == d0, str(p_now))
check("旧意见不复活：当前异常显示未处置 + 1 条永久失效历史（触发来源仍是首次变化）",
      p_now["disposition_status"] == "pending" and p_now["disposition"] is None
      and len(p_now["invalidated_history"]) == 1
      and p_now["invalidated_history"][0]["disposition_no"] == "T1-DISP-1"
      and p_now["invalidated_history"][0]["invalidated_by"]["ref"] == "T1-EVT-TX", str(p_now))
lg = ledger("TRK-1").json()["items"][0]
check("台账 current：异常仍在且摘要已一致，但状态仍为失效",
      lg["current"]["present"] is True and lg["current"]["digest_matches"] is True
      and lg["status"] == "invalidated", str(lg))
# 证据恢复后可按（原）摘要重新处置：新单号 201
r = dispose("TRK-1", "T1-DISP-2", pid, d0, reason="证据恢复后重新认可")
check("恢复后新单号可重新处置 201", r.status_code == 201, r.text)
v = anomalies("TRK-1").json()
check("重新处置后有效，失效历史仍保留一条",
      anomaly_of(v, pid)["disposition_status"] == "active"
      and len(anomaly_of(v, pid)["invalidated_history"]) == 1, str(v))

# ================================================================ 2. 批量移交立即失效
print("2) 批量移交：批内每份的有效保管链变化在批次提交事务内逐份立即对账")
reg("TRK-2A")
cfg("TRK-2A")
rd("TRK-2A", "T2A-RD-1", "2026-09-30T03:00:00Z", 9.0, "DEV-TRK-2A")
cert("T2A-C1", "DEV-TRK-2A", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
d2a = anomalies("TRK-2A").json()["items"][0]["evidence_digest"]
dispose("TRK-2A", "T2A-DISP-1", "oor:T2A-RD-1", d2a)
reg("TRK-2B")
cfg("TRK-2B")
rd("TRK-2B", "T2B-RD-1", "2026-09-30T03:00:00Z", 9.0, "DEV-TRK-2B")
cert("T2B-C1", "DEV-TRK-2B", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
d2b = anomalies("TRK-2B").json()["items"][0]["evidence_digest"]
dispose("TRK-2B", "T2B-DISP-1", "oor:T2B-RD-1", d2b)

# 先试探一个整批失败（批内样本编号重复）→ 422，不误触发任何一份
r = client.post(f"{API}/transfer-batches", json={
    "batch_id": "T2-BAD-BATCH", "occurred_at": "2026-09-30T02:00:00Z",
    "from_custodian": "王采集", "to_custodian": "李运输",
    "items": [
        {"sample_code": "TRK-2A", "transfer_no": "TR-BAD-1", "event_id": "T2-EVT-BAD1"},
        {"sample_code": "TRK-2A", "transfer_no": "TR-BAD-2", "event_id": "T2-EVT-BAD2"},
    ]})
check("整批失败 422 且不留事件", r.status_code == 422, r.text)
check("整批失败两份处置均仍有效",
      first_active("TRK-2A") is not None and first_active("TRK-2B") is not None)

r = client.post(f"{API}/transfer-batches", json={
    "batch_id": "T2-BATCH-1", "occurred_at": "2026-09-30T02:00:00Z",
    "from_custodian": "王采集", "to_custodian": "李运输",
    "items": [
        {"sample_code": "TRK-2A", "transfer_no": "T2-TR-A", "event_id": "T2-EVT-TA"},
        {"sample_code": "TRK-2B", "transfer_no": "T2-TR-B", "event_id": "T2-EVT-TB"},
    ]})
check("批量移交成功 201", r.status_code == 201, r.text)
ra, rb = first_invalidated("TRK-2A"), first_invalidated("TRK-2B")
check("批内两份处置在批次提交后均立即失效",
      ra is not None and rb is not None
      and ra["invalidate_reason"] == rb["invalidate_reason"] == "evidence_changed",
      f"{ra} {rb}")
check("invalidated_by.action=batch_transfer 且 ref 为各自事件号",
      ra["invalidated_by"] == {"kind": "custody_event", "ref": "T2-EVT-TA", "action": "batch_transfer"}
      and rb["invalidated_by"] == {"kind": "custody_event", "ref": "T2-EVT-TB", "action": "batch_transfer"},
      f"{ra['invalidated_by']} {rb['invalidated_by']}")
# 同批次号同内容重试 200：不重复、不改写失效记录的触发来源与时间
inv_at = ra["invalidated_at"]
r = client.post(f"{API}/transfer-batches", json={
    "batch_id": "T2-BATCH-1", "occurred_at": "2026-09-30T02:00:00Z",
    "from_custodian": "王采集", "to_custodian": "李运输",
    "items": [
        {"sample_code": "TRK-2B", "transfer_no": "T2-TR-B", "event_id": "T2-EVT-TB"},
        {"sample_code": "TRK-2A", "transfer_no": "T2-TR-A", "event_id": "T2-EVT-TA"},
    ]})
check("批次同内容重试 200 幂等（顺序不敏感）", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
check("幂等重试不产生新失效、不改失效时间",
      ledger("TRK-2A", "invalidated").json()["total"] == 1
      and first_invalidated("TRK-2A")["invalidated_at"] == inv_at)

# ================================================================ 3. 整批对账签收：收妥触发、异常项不触发、整单失败回滚
print("3) 整批对账签收：收妥项立即失效；异常项不生成事件不触发；整单失败不留痕")
# 重建一个两份在途批次：B3 收妥（读数在签收后→归属由在途变保管人），B4 异常（读数仍在途）
reg("TRK-3A", seal="SEAL-TRK-3A")
reg("TRK-3B", seal="SEAL-TRK-3B")
cfg("TRK-3A")
cfg("TRK-3B")
r = client.post(f"{API}/transfer-batches", json={
    "batch_id": "T3-BATCH-1", "occurred_at": "2026-09-30T02:00:00Z",
    "from_custodian": "王采集", "to_custodian": "李运输",
    "items": [
        {"sample_code": "TRK-3A", "transfer_no": "T3-TR-A", "event_id": "T3-EVT-TA"},
        {"sample_code": "TRK-3B", "transfer_no": "T3-TR-B", "event_id": "T3-EVT-TB"},
    ]})
check("在途批次提交成功", r.status_code == 201, r.text)
# 读数在签收时刻之后：批次在途时归属 in_transit
rd("TRK-3A", "T3A-RD-1", "2026-09-30T05:00:00Z", 9.0, "DEV-T3A")
rd("TRK-3B", "T3B-RD-1", "2026-09-30T03:00:00Z", 9.0, "DEV-T3B")
cert("T3A-C1", "DEV-T3A", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
cert("T3B-C1", "DEV-T3B", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
check("3A 读数归属在途",
      anomalies("TRK-3A").json()["items"][0]["attribution"]["kind"] == "in_transit")
check("3B 读数归属在途",
      anomalies("TRK-3B").json()["items"][0]["attribution"]["kind"] == "in_transit")
dispose("TRK-3A", "T3A-DISP-1", "oor:T3A-RD-1",
        anomalies("TRK-3A").json()["items"][0]["evidence_digest"])
dispose("TRK-3B", "T3B-DISP-1", "oor:T3B-RD-1",
        anomalies("TRK-3B").json()["items"][0]["evidence_digest"])

# 整单失败：签收人不匹配 → 422 整单拒绝，两份处置均仍有效
r = client.post(f"{API}/transfer-batches/T3-BATCH-1/reconciliation", json={
    "reconciliation_no": "T3-REC-BAD", "receiver": "冒名者",
    "occurred_at": "2026-09-30T04:00:00Z",
    "items": [
        {"sample_code": "TRK-3A", "event_id": "T3-EVT-RA-BAD", "verified_seal_no": "SEAL-TRK-3A"},
        {"sample_code": "TRK-3B", "reason": "异常项"},
    ]})
check("收妥项不合格整单 422 拒绝", r.status_code == 422, r.text)
check("整单失败后两份处置仍有效（不误触发、不留对账记录）",
      first_active("TRK-3A") is not None and first_active("TRK-3B") is not None)
check("失败不留对账记录", client.get(
    f"{API}/transfer-batches/T3-BATCH-1/reconciliation").status_code == 404)

# 成功对账：3A 收妥（04:00 签收 → 05:00 读数归属变为保管人李运输），3B 异常（不生成事件）
r = client.post(f"{API}/transfer-batches/T3-BATCH-1/reconciliation", json={
    "reconciliation_no": "T3-REC-1", "receiver": "李运输",
    "occurred_at": "2026-09-30T04:00:00Z",
    "items": [
        {"sample_code": "TRK-3A", "event_id": "T3-EVT-RA", "verified_seal_no": "SEAL-TRK-3A"},
        {"sample_code": "TRK-3B", "reason": "到货封签破损，异常登记"},
    ]})
check("对账成功 201", r.status_code == 201, r.text)
ra = first_invalidated("TRK-3A")
check("收妥项处置立即失效：reconciliation_receive",
      ra is not None and ra["invalidate_reason"] == "evidence_changed"
      and ra["invalidated_by"] == {"kind": "custody_event", "ref": "T3-EVT-RA",
                                   "action": "reconciliation_receive"}, str(ra))
check("3A 读数归属变为保管人李运输",
      anomalies("TRK-3A").json()["items"][0]["attribution"]
      == {"kind": "custodian", "custodian": "李运输"})
check("异常项不生成签收事件：3B 归属仍在途、处置仍有效",
      anomalies("TRK-3B").json()["items"][0]["attribution"]["kind"] == "in_transit"
      and first_active("TRK-3B") is not None and first_invalidated("TRK-3B") is None)
# 同单号同内容重试幂等：失效记录不新增
check("对账同内容重试 200",
      client.post(f"{API}/transfer-batches/T3-BATCH-1/reconciliation", json={
          "reconciliation_no": "T3-REC-1", "receiver": "李运输",
          "occurred_at": "2026-09-30T04:00:00Z",
          "items": [
              {"sample_code": "TRK-3B", "reason": "到货封签破损，异常登记"},
              {"sample_code": "TRK-3A", "event_id": "T3-EVT-RA", "verified_seal_no": "SEAL-TRK-3A"},
          ]}).status_code == 200)
check("幂等重试后 3A 仍恰有一条失效、3B 一条有效",
      ledger("TRK-3A", "invalidated").json()["total"] == 1
      and ledger("TRK-3B", "active").json()["total"] == 1)

# ================================================================ 4. 读数补录：缺测立即消失（不经查询）
print("4) 中间补录有证读数：提交事务内立即 anomaly_gone；失败/重试不触发")
reg("TRK-4")
cfg("TRK-4", interval=1800)
rd("TRK-4", "T4-RD-1", "2026-09-30T02:00:00Z", 5.0)
rd("TRK-4", "T4-RD-3", "2026-09-30T03:00:00Z", 5.0)
cert("T4-C1", "DEV-TRK-4", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
gid = "gap:T4-RD-1:T4-RD-3"
gdig = anomaly_of(anomalies("TRK-4").json(), gid)["evidence_digest"]
dispose("TRK-4", "T4-DISP-1", gid, gdig, reason="认可长缺测")
# 失败读数：非法数值 422（NaN 无法 JSON，用重复时刻已在场景 1 覆盖；此处用采集前）
r = rd("TRK-4", "T4-RD-X", "2026-09-30T00:00:00Z", 5.0)
check("采集前读数 422", r.status_code == 422, r.text)
check("失败后缺测处置仍有效", first_active("TRK-4") is not None)
# 中点补录：两段各 1800 秒（恰等于限值，不报缺测）→ 原区间消失
r = rd("TRK-4", "T4-RD-2", "2026-09-30T02:30:00Z", 5.0)
check("中点补录读数 201", r.status_code == 201, r.text)
row = first_invalidated("TRK-4")
check("补录提交后缺测处置立即 anomaly_gone（无需查询触发）",
      row is not None and row["invalidate_reason"] == "anomaly_gone"
      and row["current_evidence_digest"] is None
      and row["invalidated_by"] == {"kind": "reading", "ref": "T4-RD-2",
                                    "action": "record_reading"}, str(row))
check("当前异常为空且统计 vanished=1",
      anomalies("TRK-4").json()["total"] == 0
      and anomalies("TRK-4").json()["vanished_disposition_count"] == 1)
# 同读数号同内容重试：仍是 200，不新增失效
r = rd("TRK-4", "T4-RD-2", "2026-09-30T02:30:00Z", 5.0)
check("补录读数同内容重试 200", r.status_code == 200 and r.json()["idempotent"] is True)
check("重试不新增失效记录", ledger("TRK-4", "invalidated").json()["total"] == 1)

# ================================================================ 5. 校准证明：作废立即失效、补证拆分立即失效
print("5) 校准证明补录/作废在提交事务内立即对账；失败与同内容重试不触发")
reg("TRK-5")
cfg("TRK-5")
rd("TRK-5", "T5-RD-1", "2026-09-30T02:00:00Z", 9.0)
cert("T5-C1", "DEV-TRK-5", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
dispose("TRK-5", "T5-DISP-1", "oor:T5-RD-1",
        anomalies("TRK-5").json()["items"][0]["evidence_digest"])
# 失败操作：未知设备补证 422、重叠证明 409 —— 不触发
r = cert("T5-C-BAD", "DEV-NO-READINGS", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
check("无读数设备补证 422", r.status_code == 422
      and r.json()["error"]["code"] == "calibration_device_without_readings", r.text)
r = cert("T5-C-OVL", "DEV-TRK-5", "2026-09-30T02:00:00Z", "2026-09-30T03:00:00Z")
check("重叠证明 409", r.status_code == 409
      and r.json()["error"]["code"] == "calibration_overlap", r.text)
# 同号同内容重试 200
r = cert("T5-C1", "DEV-TRK-5", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
check("证明同内容重试 200", r.status_code == 200 and r.json()["idempotent"] is True)
check("失败/重试后处置仍有效", first_active("TRK-5") is not None)
# 作废：超温点立即消失（无有证读数 → unable_to_assess）
r = void("T5-C1", "纸质证明复核不通过")
check("作废成功 201", r.status_code == 201, r.text)
row = first_invalidated("TRK-5")
check("作废提交后处置立即 anomaly_gone，invalidated_by 记录证明作废",
      row is not None and row["invalidate_reason"] == "anomaly_gone"
      and row["invalidated_by"] == {"kind": "calibration", "ref": "T5-C1", "action": "void"},
      str(row))
# 同证明同理由重试 200，不新增失效
r = void("T5-C1", "纸质证明复核不通过")
check("作废同理由重试 200", r.status_code == 200 and r.json()["idempotent"] is True)
check("作废重试不新增失效", ledger("TRK-5", "invalidated").json()["total"] == 1)

# 补证使旧缺测消失：A/C 先有证形成长缺测并已处置，中间 B 补证后拆分为相邻有证序列
reg("TRK-6")
cfg("TRK-6", interval=1800)
rd("TRK-6", "T6-RD-A", "2026-09-30T02:00:00Z", 5.0)
rd("TRK-6", "T6-RD-B", "2026-09-30T03:00:00Z", 9.0)   # 暂无证（忽略）
rd("TRK-6", "T6-RD-C", "2026-09-30T04:00:00Z", 5.0)
cert("T6-C1", "DEV-TRK-6", "2026-09-30T01:30:00Z", "2026-09-30T02:30:00Z")  # 覆盖 A
cert("T6-C2", "DEV-TRK-6", "2026-09-30T03:30:00Z", "2026-09-30T04:30:00Z")  # 覆盖 C（端点相邻）
gid6 = "gap:T6-RD-A:T6-RD-C"
check("仅 A/C 有证：单一 2h 缺测",
      anomalies("TRK-6").json()["total"] == 1
      and anomalies("TRK-6").json()["items"][0]["anomaly_id"] == gid6)
dispose("TRK-6", "T6-DISP-1", gid6,
        anomaly_of(anomalies("TRK-6").json(), gid6)["evidence_digest"], reason="认可缺测")
# B 补证：相邻有证读数变为 A-B-C，原 A:C 缺测消失（同时产生 oor:T6-RD-B 与两段 1h 缺测）
r = cert("T6-C3", "DEV-TRK-6", "2026-09-30T02:30:00Z", "2026-09-30T03:30:00Z")
check("中间证明补录成功（端点相邻不重叠）201", r.status_code == 201, r.text)
row = first_invalidated("TRK-6")
check("补证提交后原缺测处置立即 anomaly_gone，invalidated_by 记录补证",
      row is not None and row["anomaly_id"] == gid6
      and row["invalidate_reason"] == "anomaly_gone"
      and row["invalidated_by"] == {"kind": "calibration", "ref": "T6-C3", "action": "register"},
      str(row))
v = anomalies("TRK-6").json()
check("新异常以新标识呈现且未处置（超温点 + 两段新缺测）",
      v["total"] == 3 and "oor:T6-RD-B" in {x["anomaly_id"] for x in v["items"]}
      and all(x["disposition_status"] == "pending" for x in v["items"])
      and all(len(x["invalidated_history"]) == 0 for x in v["items"]), str(v))

# ================================================================ 6. 纠错：作废/替换立即失效
print("6) 保管链纠错（void/replace）提交事务内立即失效；幂等重试不触发")
reg("TRK-7")
cfg("TRK-7")
ev("T7-EVT-T1", "transfer", "TRK-7", "2026-09-30T01:30:00Z",
   transfer_no="T7-TR-1", from_custodian="王采集", to_custodian="李运输")
rd("TRK-7", "T7-RD-1", "2026-09-30T02:00:00Z", 9.0)
cert("T7-C1", "DEV-TRK-7", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
pid7 = "oor:T7-RD-1"
check("读数归属在途", anomaly_of(anomalies("TRK-7").json(), pid7)["attribution"]["kind"] == "in_transit")
dispose("TRK-7", "T7-DISP-1", pid7,
        anomaly_of(anomalies("TRK-7").json(), pid7)["evidence_digest"])
# replace 移交时刻：01:30 → 02:30，02:00 读数回到移交前=采集人保管
r = correction("T7-FIX-1", "TRK-7", "T7-EVT-T1", action="replace", reason="移交时刻误录更正",
               new_event={
                   "event_id": "T7-EVT-T1B", "event_type": "transfer",
                   "occurred_at": "2026-09-30T02:30:00Z",
                   "transfer_no": "T7-TR-1", "from_custodian": "王采集", "to_custodian": "李运输"})
check("替换纠错成功", r.status_code in (200, 201), r.text)
row = first_invalidated("TRK-7")
check("纠错提交后处置立即 evidence_changed，invalidated_by 记录纠错号与动作",
      row is not None and row["invalidate_reason"] == "evidence_changed"
      and row["invalidated_by"] == {"kind": "correction", "ref": "T7-FIX-1", "action": "replace"},
      str(row))
# 纠错同号同内容重试 200：不新增/不改写
r = correction("T7-FIX-1", "TRK-7", "T7-EVT-T1", action="replace", reason="移交时刻误录更正",
               new_event={
                   "event_id": "T7-EVT-T1B", "event_type": "transfer",
                   "occurred_at": "2026-09-30T02:30:00Z",
                   "transfer_no": "T7-TR-1", "from_custodian": "王采集", "to_custodian": "李运输"})
check("纠错同内容重试 200 幂等（纠错接口无顶层 idempotent 字段，以状态码判定）",
      r.status_code == 200, r.text)
check("重试后仍恰一条失效", ledger("TRK-7", "invalidated").json()["total"] == 1)
# 对已失效事件再纠错 → 409 event_not_effective，且不触碰处置表
r = correction("T7-FIX-2", "TRK-7", "T7-EVT-T1", reason="对失效事件再纠错试探")
check("对已失效事件纠错 409 event_not_effective",
      r.status_code == 409 and r.json()["error"]["code"] == "event_not_effective", r.text)
check("失败纠错不新增失效记录", ledger("TRK-7", "invalidated").json()["total"] == 1)

# ================================================================ 7. 并发：按提交顺序判定，绝不留下有效旧意见
print("7) 并发：证据变化与处置意见竞争，按已提交顺序判定（终态无有效旧意见）")
observed = {"stale_reject": 0, "then_invalidated": 0}


def run_concurrent_case(i):
    code = f"TRK-8-{i}"
    reg(code)
    cfg(code)
    rd(code, f"T8-RD-{i}", "2026-09-30T02:00:00Z", 9.0, f"DEV-{code}")
    cert(f"T8-C-{i}", f"DEV-{code}", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
    digest = anomalies(code).json()["items"][0]["evidence_digest"]
    pid = f"oor:T8-RD-{i}"
    outcome = {}
    barrier = threading.Barrier(2)

    def post_change():
        barrier.wait()
        outcome["change"] = ev(
            f"T8-EVT-T-{i}", "transfer", code, "2026-09-30T01:30:00Z",
            transfer_no=f"T8-TR-{i}", from_custodian="王采集", to_custodian="李运输").status_code

    def post_disposition():
        barrier.wait()
        outcome["disp"] = dispose(
            code, f"T8-DISP-{i}", pid, digest, reason=f"并发认可 {i}").status_code

    t1 = threading.Thread(target=post_change)
    t2 = threading.Thread(target=post_disposition)
    t1.start(); t2.start(); t1.join(); t2.join()
    return code, outcome


for i in range(8):
    code, outcome = run_concurrent_case(i)
    active = ledger(code, "active").json()["total"]
    invalidated = ledger(code, "invalidated").json()["total"]
    if outcome["disp"] == 422:
        observed["stale_reject"] += 1
        check(f"迭代{i}：变化先提交→持旧摘要处置 422 anomaly_stale，且无处置留痕",
              outcome["disp"] == 422 and active == 0 and invalidated == 0
              and outcome["change"] == 201, str(outcome))
    else:
        observed["then_invalidated"] += 1
        check(f"迭代{i}：处置先提交 201、变化后提交→事务内立即失效，终态无有效旧意见",
              outcome["disp"] == 201 and active == 0 and invalidated == 1
              and outcome["change"] == 201, str(outcome))
check("两种提交顺序均被实际观测到",
      observed["stale_reject"] > 0 and observed["then_invalidated"] > 0, str(observed))

# ================================================================ 8. 查询惰性兜底：存量异常对账仍工作（invalidated_by 为 NULL）
print("8) 惰性对账兜底：存量有效处置在查询时对账，失效留痕但 invalidated_by 为 NULL")
reg("TRK-9")
cfg("TRK-9")
rd("TRK-9", "T9-RD-1", "2026-09-30T02:00:00Z", 9.0)
cert("T9-C1", "DEV-TRK-9", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
pid9 = "oor:T9-RD-1"
dig9 = anomalies("TRK-9").json()["items"][0]["evidence_digest"]
dispose("TRK-9", "T9-DISP-1", pid9, dig9)
# 直接在库中把有效处置的证据摘要改成不匹配（模拟存量/漏网情况），再走查询惰性对账
with db_session(immediate=True) as conn:
    conn.execute(
        "UPDATE temp_anomaly_disposition SET evidence_digest = ?"
        " WHERE disposition_no = 'T9-DISP-1'", ("f" * 64,))
v = anomalies("TRK-9").json()
check("查询后旧意见被惰性对账标记失效，当前异常未处置",
      anomaly_of(v, pid9)["disposition_status"] == "pending"
      and len(anomaly_of(v, pid9)["invalidated_history"]) == 1, str(v))
row = ledger("TRK-9", "invalidated").json()["items"][0]
check("惰性失效 invalidate_reason=evidence_changed 且 invalidated_by 为 NULL",
      row["invalidate_reason"] == "evidence_changed" and row["invalidated_by"] is None, str(row))

# ================================================================ 9. 既有兼容：字段保留、原始证据/版本/案卷不改
print("9) 兼容性：异常查询/台账字段、幂等与过期摘要拒绝、版本与原始证据不变")
# 失效后持旧摘要提交仍 422 anomaly_stale（异常在但摘要一致？此处为 TRK-1 恢复后摘要一致的情形
# —— 已允许用新单号重新处置；对 TRK-7（摘要已变）持旧摘要提交必须 422）
r = dispose("TRK-7", "T7-DISP-NEW", "oor:T7-RD-1",
            ledger("TRK-7", "invalidated").json()["items"][0]["submitted_evidence_digest"],
            reason="持旧摘要再提交")
check("摘要已变后持旧摘要提交 422 anomaly_stale(evidence_changed)",
      r.status_code == 422 and r.json()["error"]["code"] == "anomaly_stale"
      and r.json()["error"]["details"]["reason"] == "evidence_changed", r.text)
# 同单号同内容重试失效单仍 200 返回原结果（含失效状态与触发来源）
r = client.post(f"{API}/samples/TRK-1/temperature/anomalies/dispositions", json={
    "disposition_no": "T1-DISP-1", "sample_code": "TRK-1", "anomaly_id": "oor:T1-RD-1",
    "expected_evidence_digest": d0, "decision": "acknowledged",
    "reason": "认可该异常", "handler": "质控员赵"})
check("失效单同号重试 200 返回失效原结果",
      r.status_code == 200 and r.json()["idempotent"] is True
      and r.json()["disposition"]["status"] == "invalidated"
      and r.json()["disposition"]["invalidated_by"]["ref"] == "T1-EVT-TX", r.text)
# 原始读数不改、不产生保管链版本（TRK-1 的版本只来自 1 移交 + 1 签收 + 2 纠错 + 1 入库）
chain = client.get(f"{API}/samples/TRK-1/chain").json()
check("原始证据未被改写：补录移交/签收经作废后出现在 superseded_events，读数值不变",
      {e["event_id"] for e in chain["superseded_events"]} == {"T1-EVT-TX", "T1-EVT-RX"}
      and len(chain["corrections"]) == 2
      and client.get(f"{API}/samples/TRK-1/temperature/trusted").json()
      ["reading_matches"][0]["value_celsius"] == 9.0, str(chain))
check("处置追踪不产生保管链版本（版本仅随 5 次真正保管提交）",
      client.get(f"{API}/samples/TRK-1/versions").json()["current_version"] == 5)
# 台账字段保留：含提交快照、失效原因/时间、current 与新增 invalidated_by
view = ledger("TRK-1").json()["items"][0]
for field in ("disposition_no", "anomaly_id", "decision", "reason", "handler",
              "submitted_at", "submitted_evidence_digest", "submitted_evidence",
              "status", "invalidated_at", "invalidate_reason",
              "current_evidence_digest", "invalidated_by", "current"):
    check(f"台账保留字段 {field}", field in view)

check("健康检查可用", client.get("/health").json()["status"] == "ok")
print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
