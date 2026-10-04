"""端到端自测：可信测温复核异常处置（运输质控）。

运行：python -m tests.test_dispositions
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_dispositions.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()
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


def reg(code, collector="王采集", seal="SEAL-D", when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when, "collector": collector, "seal_no": seal
    })


def cfg(code, lo=2.0, hi=8.0, interval=1800):
    return client.post(f"{API}/temperature-configs", json={
        "sample_code": code, "min_celsius": lo, "max_celsius": hi,
        "max_interval_seconds": interval})


def rd(code, rid, when, value, dev="DEV-D-01"):
    return client.post(f"{API}/temperature-readings", json={
        "sample_code": code, "reading_id": rid, "device_id": dev,
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


def anomalies(code):
    return client.get(f"{API}/samples/{code}/temperature/anomalies")


def anomaly_of(v, aid):
    return next(x for x in v["items"] if x["anomaly_id"] == aid)


def ledger(code, status=None):
    q = f"?status={status}" if status else ""
    return client.get(f"{API}/samples/{code}/temperature/anomalies/dispositions{q}")


def versions(code):
    return client.get(f"{API}/samples/{code}/versions").json()


def dispose(code, no, aid, digest, decision="acknowledged", reason="认可该超温",
            handler="质控员赵", body_code=None):
    return client.post(
        f"{API}/samples/{code}/temperature/anomalies/dispositions",
        json={
            "disposition_no": no,
            "sample_code": body_code or code,
            "anomaly_id": aid,
            "expected_evidence_digest": digest,
            "decision": decision,
            "reason": reason,
            "handler": handler,
        })


# ---------------------------------------------------------------- 1. 查询：稳定标识/摘要/归属/读数与证明
print("1) 异常查询：稳定标识、证据摘要、时间范围、相关读数及校准证明、归属待核标记")
reg("DISP-1")
cfg("DISP-1")
ev("D1-EVT-T1", "transfer", "DISP-1", "2026-09-30T02:30:00Z",
   transfer_no="D1-TR-1", from_custodian="王采集", to_custodian="李运输")
ev("D1-EVT-R1", "receive", "DISP-1", "2026-09-30T04:30:00Z",
   transfer_no="D1-TR-1", receiver="李运输", seal_no="SEAL-D")
rd("DISP-1", "D1-RD-1", "2026-09-30T02:00:00Z", 5.0)
rd("DISP-1", "D1-RD-2", "2026-09-30T02:30:00Z", 9.5)   # 超温（恰在移交时刻，按已移交=在途）
rd("DISP-1", "D1-RD-3", "2026-09-30T06:30:00Z", 5.0)   # 与 D1-RD-2 相邻有证形成缺测
cert("D1-C1", "DEV-D-01", "2026-09-30T01:00:00Z", "2026-09-30T07:00:00Z")

v = anomalies("DISP-1").json()
check("可评估且两项异常",
      v["assessment_status"] == "assessable" and v["total"] == 2
      and v["pending_count"] == 2 and v["active_count"] == 0, str(v))
point_id, gap_id = "oor:D1-RD-2", "gap:D1-RD-2:D1-RD-3"
check("稳定标识（oor:读数号 / gap:前后读数号）",
      {x["anomaly_id"] for x in v["items"]} == {point_id, gap_id}, str(v["items"]))

p = anomaly_of(v, point_id)
check("超温点摘要/时间范围/方向限值",
      p["anomaly_type"] == "out_of_range_point"
      and "超温" in p["summary"] and p["time_range"]["start"] == p["time_range"]["end"] == "2026-09-30T02:30:00Z"
      and p["reading"]["direction"] == "above_max" and p["reading"]["limit_celsius"] == 8.0
      and p["reading"]["value_celsius"] == 9.5, str(p))
check("超温点含校准证明与在途归属（不臆定责任人）",
      p["reading"]["certificate_id"] == "D1-C1"
      and p["reading"]["certificate"]["certificate_id"] == "D1-C1"
      and p["reading"]["certificate"]["status"] == "active"
      and p["attribution"]["kind"] == "in_transit"
      and p["attribution_pending"] is False, str(p))
check("证据摘要为 64 位十六进制", len(p["evidence_digest"]) == 64
      and all(c in "0123456789abcdef" for c in p["evidence_digest"]))
check("初始未处置无有效意见、无失效历史",
      p["disposition_status"] == "pending" and p["disposition"] is None
      and p["invalidated_history"] == [])

g = anomaly_of(v, gap_id)
check("缺测区间端点/时长/间隔与跨移交签收拆分三段",
      g["anomaly_type"] == "gap_interval"
      and g["time_range"]["duration_seconds"] == 4 * 3600
      and g["time_range"]["max_interval_seconds"] == 1800
      and [s["attribution"]["kind"] for s in g["segments"]] == ["in_transit", "custodian"]
      and [r["reading_id"] for r in g["readings"]] == ["D1-RD-2", "D1-RD-3"], str(g))
check("缺测区间端点读数含各自校准证明",
      all(r["certificate_id"] == "D1-C1" and r["certificate"] is not None for r in g["readings"]),
      str(g["readings"]))

# ---------------------------------------------------------------- 2. 提交：认可/排除
print("2) 处置提交：认可 / 排除，仅接受当前存在且摘要一致的异常项")
r = dispose("DISP-1", "DISP-NO-1", point_id, p["evidence_digest"],
            decision="acknowledged", reason="认可超温，运输方在途温控失效", handler="质控员赵")
check("认可超温 201", r.status_code == 201 and r.json()["idempotent"] is False, r.text)
body = r.json()
check("响应处置单字段完整",
      body["disposition"]["disposition_no"] == "DISP-NO-1"
      and body["disposition"]["anomaly_id"] == point_id
      and body["disposition"]["decision"] == "acknowledged"
      and body["disposition"]["handler"] == "质控员赵"
      and body["disposition"]["status"] == "active"
      and body["disposition"]["invalidated_at"] is None
      and body["disposition"]["submitted_evidence"]["evidence_digest"] == p["evidence_digest"]
      and body["anomaly"]["anomaly_id"] == point_id, str(body))

v = anomalies("DISP-1").json()
p2 = anomaly_of(v, point_id)
check("查询反映有效处置，统计联动",
      p2["disposition_status"] == "active"
      and p2["disposition"]["disposition_no"] == "DISP-NO-1"
      and v["active_count"] == 1 and v["pending_count"] == 1, str(v))

r = dispose("DISP-1", "DISP-NO-2", gap_id, g["evidence_digest"],
            decision="excluded", reason="排除缺测：设备换电池停机，有纸质交接记录",
            handler="质控员钱")
check("排除缺测 201", r.status_code == 201 and r.json()["disposition"]["decision"] == "excluded", r.text)
v = anomalies("DISP-1").json()
check("两项均为有效处置", v["total"] == 2 and v["active_count"] == 2 and v["pending_count"] == 0, str(v))

# ---------------------------------------------------------------- 3. 同摘要只能一次；幂等与冲突
print("3) 同一异常同一摘要仅一次；同单号同内容幂等、异内容冲突")
r = dispose("DISP-1", "DISP-NO-3", point_id, p["evidence_digest"],
            decision="excluded", reason="重复处置试探")
check("重复处置 409 anomaly_already_disposed 且附原单号",
      r.status_code == 409 and r.json()["error"]["code"] == "anomaly_already_disposed"
      and r.json()["error"]["details"]["existing_disposition_no"] == "DISP-NO-1", r.text)

r = dispose("DISP-1", "DISP-NO-1", point_id, p["evidence_digest"],
            decision="acknowledged", reason="认可超温，运输方在途温控失效", handler="质控员赵")
check("同单号同内容重试 200 幂等返回原结果",
      r.status_code == 200 and r.json()["idempotent"] is True
      and r.json()["disposition"]["submitted_at"] == body["disposition"]["submitted_at"], r.text)

r = dispose("DISP-1", "DISP-NO-1", point_id, p["evidence_digest"],
            decision="excluded", reason="同单号改意见", handler="质控员赵")
check("同单号异内容 409 disposition_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "disposition_conflict", r.text)

r = dispose("DISP-1", "DISP-NO-1", point_id, g["evidence_digest"],
            reason="同单号换异常", handler="质控员赵")
check("同单号换异常标识也 409",
      r.status_code == 409 and r.json()["error"]["code"] == "disposition_conflict", r.text)

# ---------------------------------------------------------------- 4. 过期：异常消失 / 摘要不一致
print("4) 过期或消失的项整单拒绝且不留记录")
r = dispose("DISP-1", "DISP-NO-GHOST", "oor:D1-RD-3", "a" * 64,
            reason="处置不存在的异常", handler="质控员赵")
check("未知异常标识 422 anomaly_stale(anomaly_gone)，不留记录",
      r.status_code == 422 and r.json()["error"]["code"] == "anomaly_stale"
      and r.json()["error"]["details"]["reason"] == "anomaly_gone"
      and set(r.json()["error"]["details"]["current_anomaly_ids"]) == {point_id, gap_id}, r.text)
check("被拒单号不留痕",
      all(x["disposition_no"] != "DISP-NO-GHOST" for x in ledger("DISP-1").json()["items"]))

r = dispose("DISP-1", "DISP-NO-STALE", point_id, "f" * 64,
            reason="持过期摘要提交", handler="质控员赵")
check("摘要不一致 422 anomaly_stale(evidence_changed) 附当前摘要",
      r.status_code == 422 and r.json()["error"]["code"] == "anomaly_stale"
      and r.json()["error"]["details"]["reason"] == "evidence_changed"
      and r.json()["error"]["details"]["current_evidence_digest"] == p["evidence_digest"], r.text)
check("过期单不留痕",
      all(x["disposition_no"] != "DISP-NO-STALE" for x in ledger("DISP-1").json()["items"]))

r = client.post(f"{API}/samples/DISP-1/temperature/anomalies/dispositions", json={
    "disposition_no": "DISP-NO-BLANK", "sample_code": "DISP-1",
    "anomaly_id": point_id, "expected_evidence_digest": p["evidence_digest"],
    "decision": "acknowledged", "reason": "   ", "handler": "质控员赵"})
check("空白理由 422（载荷校验）", r.status_code == 422, r.text)

# ---------------------------------------------------------------- 5. 保管链纠错 → 归属变化：旧意见失效，新摘要可再处置
print("5) 保管链纠错使归属变化：旧意见标失效（永久可查），新摘要可重新处置")
before_version = versions("DISP-1")["current_version"]
r = client.post(f"{API}/corrections", json={
    "correction_id": "D1-FIX-T1",
    "sample_code": "DISP-1",
    "target_event_id": "D1-EVT-T1",
    "action": "replace",
    "operator": "链管理员",
    "reason": "移交时间误录，更正为 03:00",
    "new_event": {
        "event_id": "D1-EVT-T1B", "event_type": "transfer",
        "occurred_at": "2026-09-30T03:00:00Z",
        "transfer_no": "D1-TR-1", "from_custodian": "王采集", "to_custodian": "李运输",
    }})
check("纠错成功", r.status_code in (200, 201), r.text)

v = anomalies("DISP-1").json()
p3, g3 = anomaly_of(v, point_id), anomaly_of(v, gap_id)
check("异常仍在但摘要已变",
      p3["evidence_digest"] != p["evidence_digest"]
      and g3["evidence_digest"] != g["evidence_digest"], str(v))
check("两份旧意见均失效：evidence_changed 且记录当前摘要",
      len(p3["invalidated_history"]) == 1 and len(g3["invalidated_history"]) == 1
      and p3["invalidated_history"][0]["status"] == "invalidated"
      and p3["invalidated_history"][0]["invalidate_reason"] == "evidence_changed"
      and p3["invalidated_history"][0]["current_evidence_digest"] == p3["evidence_digest"]
      and p3["disposition_status"] == "pending", str(p3))
check("旧意见证据快照永久可查",
      p3["invalidated_history"][0]["submitted_evidence"]["anomaly_id"] == point_id
      and p3["invalidated_history"][0]["submitted_evidence"]["reading"]["value_celsius"] == 9.5)
check("失效后无有效处置计数", v["active_count"] == 0 and v["pending_count"] == 2
      and v["invalidated_history_count"] == 2, str(v))

r = dispose("DISP-1", "DISP-NO-1", point_id, p3["evidence_digest"],
            reason="复用旧单号", handler="质控员赵")
check("旧单号仍是异内容冲突（单号不可复用）",
      r.status_code == 409 and r.json()["error"]["code"] == "disposition_conflict", r.text)

r = dispose("DISP-1", "DISP-NO-4", point_id, p3["evidence_digest"],
            reason="按新归属重新认可", handler="质控员赵")
check("新摘要可重新处置 201", r.status_code == 201, r.text)
v = anomalies("DISP-1").json()
p4 = anomaly_of(v, point_id)
check("重新处置后：一项有效 + 一条失效历史",
      p4["disposition_status"] == "active"
      and p4["disposition"]["disposition_no"] == "DISP-NO-4"
      and len(p4["invalidated_history"]) == 1
      and p4["invalidated_history"][0]["disposition_no"] == "DISP-NO-1", str(p4))
check("处置查询不产生保管链版本（版本变化仅来自纠错）",
      versions("DISP-1")["current_version"] == before_version + 1)

# ---------------------------------------------------------------- 6. 异常消失：作废旧证明
print("6) 证明作废使超温点消失：旧处置 anomaly_gone，补证后可再处置")
reg("DISP-2")
cfg("DISP-2")
rd("DISP-2", "D2-RD-1", "2026-09-30T02:00:00Z", 9.0, "DEV-D-2")
cert("D2-C1", "DEV-D-2", "2026-09-30T01:00:00Z", "2026-09-30T03:00:00Z")
v = anomalies("DISP-2").json()
pid = "oor:D2-RD-1"
check("DISP-2 一项超温", v["total"] == 1 and {x["anomaly_id"] for x in v["items"]} == {pid}, str(v))
dig = anomaly_of(v, pid)["evidence_digest"]
r = dispose("DISP-2", "DISP-NO-D2-1", pid, dig, reason="认可", handler="质控员赵")
check("处置成功 201", r.status_code == 201, r.text)
void("D2-C1", "纸质证明复核不通过")
v = anomalies("DISP-2").json()
check("作废后无法评估、当前异常为空",
      v["assessment_status"] == "unable_to_assess" and v["total"] == 0
      and v["items"] == [], str(v))
lg = ledger("DISP-2").json()
row = lg["items"][0]
check("台账保留失效处置：anomaly_gone",
      lg["total"] == 1 and row["status"] == "invalidated"
      and row["invalidate_reason"] == "anomaly_gone"
      and row["invalidated_at"] is not None, str(lg))
check("台账 current 指明异常不存在",
      row["current"]["assessable"] is False or row["current"]["present"] is False, str(row))
r = dispose("DISP-2", "DISP-NO-D2-2", pid, dig, reason="失效后重试原摘要")
check("消失项提交 422 anomaly_stale(anomaly_gone)",
      r.status_code == 422 and r.json()["error"]["details"]["reason"] == "anomaly_gone", r.text)

# 补录新证明 → 超温点以新摘要重新出现，可再处置；原失效意见不复活
cert("D2-C2", "DEV-D-2", "2026-09-30T01:30:00Z", "2026-09-30T04:00:00Z", issuer="计量员乙")
v = anomalies("DISP-2").json()
pnew = anomaly_of(v, pid)
check("补证后异常重现且为新摘要",
      v["total"] == 1 and pnew["evidence_digest"] != dig, str(v))
check("原失效意见不复活：未处置 + 1 条失效历史",
      pnew["disposition_status"] == "pending"
      and len(pnew["invalidated_history"]) == 1
      and pnew["invalidated_history"][0]["disposition_no"] == "DISP-NO-D2-1", str(pnew))
r = dispose("DISP-2", "DISP-NO-D2-3", pid, pnew["evidence_digest"],
            reason="补证后重新认可", handler="质控员赵")
check("新摘要重新处置 201", r.status_code == 201, r.text)

# ---------------------------------------------------------------- 7. 读数补录拆分缺测：旧区间消失、新区间产生
print("7) 中间补录有证读数：原缺测消失（anomaly_gone），新缺测以新标识/摘要出现")
reg("DISP-3")
cfg("DISP-3", interval=1800)
rd("DISP-3", "D3-RD-1", "2026-09-30T02:00:00Z", 5.0, "DEV-D-3")
rd("DISP-3", "D3-RD-3", "2026-09-30T03:00:00Z", 5.0, "DEV-D-3")
cert("D3-C1", "DEV-D-3", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")
v = anomalies("DISP-3").json()
gid = "gap:D3-RD-1:D3-RD-3"
check("单一长缺测", v["total"] == 1 and v["items"][0]["anomaly_id"] == gid
      and v["items"][0]["time_range"]["duration_seconds"] == 3600, str(v))
gdig = v["items"][0]["evidence_digest"]
r = dispose("DISP-3", "DISP-NO-D3-1", gid, gdig, reason="认可缺测", handler="质控员赵")
check("缺测处置成功", r.status_code == 201, r.text)
# 中点补录读数（样本未核定）：两段各 1800 秒，恰好不超间隔
r = rd("DISP-3", "D3-RD-2", "2026-09-30T02:30:00Z", 5.0, "DEV-D-3")
check("补录读数成功", r.status_code in (200, 201), r.text)
v = anomalies("DISP-3").json()
check("补录后原缺测消失且无新缺测（两段均恰为最大间隔）",
      v["total"] == 0 and v["vanished_disposition_count"] == 1, str(v))
lg = ledger("DISP-3").json()
check("原缺测处置标记 anomaly_gone 并保留快照",
      lg["items"][0]["invalidate_reason"] == "anomaly_gone"
      and lg["items"][0]["submitted_evidence"]["time_range"]["duration_seconds"] == 3600,
      str(lg))

# ---------------------------------------------------------------- 8. 归属待核：不臆定责任人
print("8) 断链待核：异常标记 attribution_pending，处置可留存")
reg("DISP-4")
cfg("DISP-4")
ev("D4-EVT-T1", "transfer", "DISP-4", "2026-09-30T02:00:00Z",
   transfer_no="D4-TR-1", from_custodian="王采集", to_custodian="李运输")
ev("D4-EVT-T2", "transfer", "DISP-4", "2026-09-30T03:00:00Z",
   transfer_no="D4-TR-2", from_custodian="李运输", to_custodian="陈接收")  # 在途再移交→断链
rd("DISP-4", "D4-RD-1", "2026-09-30T04:00:00Z", 1.0, "DEV-D-4")
cert("D4-C1", "DEV-D-4", "2026-09-30T01:00:00Z", "2026-09-30T05:00:00Z")
v = anomalies("DISP-4").json()
p = v["items"][0]
check("超温点归属待核显式标记、不臆定责任人",
      p["anomaly_id"] == "oor:D4-RD-1"
      and p["attribution"]["kind"] == "pending_review"
      and p["attribution_pending"] is True
      and "待核" in p["summary"], str(p))
r = dispose("DISP-4", "DISP-NO-D4-1", p["anomaly_id"], p["evidence_digest"],
            reason="归属待核仍需留痕：先认可异常，责任待保管链查实", handler="质控员赵")
check("待核异常可处置 201", r.status_code == 201, r.text)

# ---------------------------------------------------------------- 9. 台账过滤、跨样本单号、样本一致性
print("9) 台账查询/过滤；跨样本单号冲突；路径与请求体不一致拒绝")
r = ledger("DISP-1")
check("DISP-1 台账三条（DISP-NO-1/2/4）",
      r.status_code == 200 and r.json()["total"] == 3
      and {x["disposition_no"] for x in r.json()["items"]} == {"DISP-NO-1", "DISP-NO-2", "DISP-NO-4"},
      r.text)
r = ledger("DISP-1", status="active")
check("?status=active 仅有效（超温重新处置有效，缺测已失效）",
      r.json()["total"] == 1 and all(x["status"] == "active" for x in r.json()["items"])
      and r.json()["items"][0]["disposition_no"] == "DISP-NO-4", r.text)
r = ledger("DISP-1", status="invalidated")
check("?status=invalidated 仅失效（最初两份）",
      r.json()["total"] == 2 and all(x["status"] == "invalidated" for x in r.json()["items"]), r.text)
r = ledger("DISP-1", status="bogus")
check("非法 status 422 invalid_status_filter",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_status_filter", r.text)

v = anomalies("DISP-2").json()
pnew = anomaly_of(v, "oor:D2-RD-1")
r = dispose("DISP-2", "DISP-NO-4", "oor:D2-RD-1", pnew["evidence_digest"],
            reason="跨样本复用他样本已用单号", handler="质控员赵")
check("跨样本复用处置单号 409 disposition_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "disposition_conflict", r.text)
r = dispose("DISP-2", "DISP-NO-X", "oor:D2-RD-1", pnew["evidence_digest"],
            reason="路径与体不一致", handler="质控员赵", body_code="DISP-1")
check("路径样本与请求体样本不一致 422 disposition_sample_mismatch",
      r.status_code == 422 and r.json()["error"]["code"] == "disposition_sample_mismatch", r.text)

# ---------------------------------------------------------------- 10. 未配置/未知样本
print("10) 未设定测温配置 / 未知样本")
reg("DISP-NOCFG")
r = anomalies("DISP-NOCFG")
check("未配置查询 404 temp_config_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "temp_config_not_found", r.text)
r = client.post(f"{API}/samples/DISP-NOCFG/temperature/anomalies/dispositions", json={
    "disposition_no": "DISP-NO-NC", "sample_code": "DISP-NOCFG",
    "anomaly_id": "oor:X", "expected_evidence_digest": "a" * 64,
    "decision": "acknowledged", "reason": "无配置试探", "handler": "质控员赵"})
check("未配置提交 404",
      r.status_code == 404 and r.json()["error"]["code"] == "temp_config_not_found", r.text)
check("未配置台账为空 200（历史仍可读）",
      ledger("DISP-NOCFG").status_code == 200 and ledger("DISP-NOCFG").json()["total"] == 0)
check("未知样本 404 sample_not_found",
      anomalies("NO-SUCH-SAMPLE").status_code == 404
      and anomalies("NO-SUCH-SAMPLE").json()["error"]["code"] == "sample_not_found")

# ---------------------------------------------------------------- 11. 核定后仍可处置；不改变原始证据/复核/版本/案卷
print("11) 核定样本仍可处置；处置不触碰冻结证据、版本与既有复核")
reg("DISP-5")
cfg("DISP-5")
ev("D5-EVT-T1", "transfer", "DISP-5", "2026-09-30T01:30:00Z",
   transfer_no="D5-TR", from_custodian="王采集", to_custodian="李运输")
ev("D5-EVT-R1", "receive", "DISP-5", "2026-09-30T02:00:00Z",
   transfer_no="D5-TR", receiver="李运输", seal_no="SEAL-D")
ev("D5-EVT-S1", "storage", "DISP-5", "2026-09-30T02:30:00Z", custodian="实验室")
rd("DISP-5", "D5-RD-1", "2026-09-30T02:45:00Z", 9.0, "DEV-D-5")
cert("D5-C1", "DEV-D-5", "2026-09-30T01:00:00Z", "2026-09-30T03:00:00Z")
client.post(f"{API}/samples/DISP-5/approve", json={"approver": "质量主管"})
approved_version = versions("DISP-5")["current_version"]
v = anomalies("DISP-5").json()
pid = "oor:D5-RD-1"
check("核定后异常可查询（入库后归属实验室）",
      v["total"] == 1 and anomaly_of(v, pid)["attribution"]["kind"] == "custodian"
      and anomaly_of(v, pid)["attribution"]["custodian"] == "实验室", str(v))
r = dispose("DISP-5", "DISP-NO-D5-1", pid, anomaly_of(v, pid)["evidence_digest"],
            reason="核定后质控留存认可意见", handler="质控员赵")
check("核定后处置 201", r.status_code == 201, r.text)
check("处置不产生保管链版本", versions("DISP-5")["current_version"] == approved_version)
trusted = client.get(f"{API}/samples/DISP-5/temperature/trusted").json()
check("既有可信复核不含处置字段（处置不污染复核）",
      "disposition" not in trusted
      and [x["reading_id"] for x in trusted["out_of_range_points"]] == ["D5-RD-1"], str(trusted))
r = rd("DISP-5", "D5-RD-LATE", "2026-09-30T01:50:00Z", 5.0, "DEV-D-5")
check("既有核定后新读数仍冻结", r.status_code == 409 and r.json()["error"]["code"] == "history_locked", r.text)

# 失效意见在核定样本上同样可查（作废旧证明 → anomaly_gone）
void("D5-C1", "核定后证明撤销演示")
lg = ledger("DISP-5").json()
check("核定样本旧处置随证明作废标记失效",
      lg["items"][0]["status"] == "invalidated"
      and lg["items"][0]["invalidate_reason"] == "anomaly_gone", str(lg))
check("版本仍不变", versions("DISP-5")["current_version"] == approved_version)

# ---------------------------------------------------------------- 12. 并发：同异常同摘要只一条有效
print("12) 并发：同异常同摘要两条不同单号仅一条成功")
reg("DISP-6")
cfg("DISP-6")
rd("DISP-6", "D6-RD-1", "2026-09-30T02:00:00Z", 9.0, "DEV-D-6")
cert("D6-C1", "DEV-D-6", "2026-09-30T01:00:00Z", "2026-09-30T03:00:00Z")
dig = anomalies("DISP-6").json()["items"][0]["evidence_digest"]
results: dict[str, int] = {}
barrier = threading.Barrier(2)


def post_one(tag, no):
    barrier.wait()
    results[tag] = dispose("DISP-6", no, "oor:D6-RD-1", dig,
                           reason=f"并发处置 {tag}", handler=f"质控员{tag}").status_code


t1 = threading.Thread(target=post_one, args=("A", "DISP-NO-D6-A"))
t2 = threading.Thread(target=post_one, args=("B", "DISP-NO-D6-B"))
t1.start(); t2.start(); t1.join(); t2.join()
check("同异常同摘要并发：一 201 一 409",
      sorted(results.values()) == [201, 409] or sorted(results.values()) == [409, 201],
      str(results))
lg = ledger("DISP-6", status="active").json()
check("仅一条有效处置落库", lg["total"] == 1, str(lg))

# 同单号同内容并发：一 201 一 200
reg("DISP-7")
cfg("DISP-7")
rd("DISP-7", "D7-RD-1", "2026-09-30T02:00:00Z", 9.0, "DEV-D-7")
cert("D7-C1", "DEV-D-7", "2026-09-30T01:00:00Z", "2026-09-30T03:00:00Z")
dig = anomalies("DISP-7").json()["items"][0]["evidence_digest"]
results.clear()
barrier = threading.Barrier(2)
t1 = threading.Thread(target=lambda: (barrier.wait(), results.__setitem__(
    "A", dispose("DISP-7", "DISP-NO-D7-X", "oor:D7-RD-1", dig,
                 reason="同内容并发", handler="质控员赵").status_code)))
t2 = threading.Thread(target=lambda: (barrier.wait(), results.__setitem__(
    "B", dispose("DISP-7", "DISP-NO-D7-X", "oor:D7-RD-1", dig,
                 reason="同内容并发", handler="质控员赵").status_code)))
t1.start(); t2.start(); t1.join(); t2.join()
check("同单号同内容并发：一 201 一 200", sorted(results.values()) == [200, 201], str(results))

# ---------------------------------------------------------------- 13. 幂等返回原结果（即便其后失效）
print("13) 旧意见失效后，同单号同内容重试仍 200 返回原结果（含失效状态）")
r = dispose("DISP-2", "DISP-NO-D2-1", "oor:D2-RD-1",
            ledger("DISP-2", "invalidated").json()["items"][0]["submitted_evidence_digest"],
            decision="acknowledged", reason="认可", handler="质控员赵")
check("失效后同单重试 200 且返回失效状态",
      r.status_code == 200 and r.json()["idempotent"] is True
      and r.json()["disposition"]["status"] == "invalidated"
      and r.json()["disposition"]["invalidate_reason"] == "anomaly_gone", r.text)

check("健康检查可用", client.get("/health").json()["status"] == "ok")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
