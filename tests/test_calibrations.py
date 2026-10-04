"""端到端自测：测温设备校准证明与可信测温复核。

运行：python -m tests.test_calibrations
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_calibrations.db")
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


def reg(code, collector="王采集", seal="SEAL-C", when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when, "collector": collector, "seal_no": seal
    })


def cfg(code, lo=2.0, hi=8.0, interval=1800):
    return client.post(f"{API}/temperature-configs", json={
        "sample_code": code, "min_celsius": lo, "max_celsius": hi,
        "max_interval_seconds": interval})


def rd(code, rid, when, value, dev="DEV-CAL-01"):
    return client.post(f"{API}/temperature-readings", json={
        "sample_code": code, "reading_id": rid, "device_id": dev,
        "occurred_at": when, "value_celsius": value})


def cert(cid, dev, start, end, issuer="计量员甲"):
    return client.post(f"{API}/temperature-calibrations", json={
        "certificate_id": cid, "device_id": dev, "valid_from": start,
        "valid_to": end, "issued_by": issuer})


def void(cid, reason="证明文件复核作废"):
    return client.post(f"{API}/temperature-calibrations/{cid}/void", json={"reason": reason})


def trusted(code):
    return client.get(f"{API}/samples/{code}/temperature/trusted")


def temperature(code):
    return client.get(f"{API}/samples/{code}/temperature")


def versions(code):
    return client.get(f"{API}/samples/{code}/versions").json()


def match(v, rid):
    return next(x for x in v["reading_matches"] if x["reading_id"] == rid)


# ---------------------------------------------------------------- 1. 登记前提与无效时段
print("1) 校准证明：只能围绕已有读数设备登记，无效时段拒绝")
reg("CAL-PRE")
cfg("CAL-PRE")
r = cert("CAL-BAD-DEVICE", "DEV-NEVER", "2026-09-30T00:00:00Z", "2026-09-30T23:00:00Z")
check("无读数设备 422 calibration_device_without_readings",
      r.status_code == 422 and r.json()["error"]["code"] == "calibration_device_without_readings", r.text)
check("失败不留证明", client.get(f"{API}/temperature-calibrations/CAL-BAD-DEVICE").status_code == 404)
rd("CAL-PRE", "CAL-RD-1", "2026-09-30T02:00:00Z", 5.0)
r = cert("CAL-EMPTY", "DEV-CAL-01", "2026-09-30T03:00:00Z", "2026-09-30T03:00:00Z")
check("起止相同 422（左闭右开空时段无效）", r.status_code == 422, r.text)
r = cert("CAL-REVERSED", "DEV-CAL-01", "2026-09-30T03:00:00Z", "2026-09-30T02:00:00Z")
check("起始晚于结束 422", r.status_code == 422, r.text)
r = client.post(f"{API}/temperature-calibrations", json={
    "certificate_id": "CAL-BLANK", "device_id": "DEV-CAL-01",
    "valid_from": "2026-09-30T01:00:00Z", "valid_to": "2026-09-30T02:00:00Z",
    "issued_by": " "})
check("空白签发人 422", r.status_code == 422, r.text)
check("无效请求均不留证明",
      client.get(f"{API}/temperature-calibrations/CAL-EMPTY").status_code == 404
      and client.get(f"{API}/temperature-calibrations/CAL-REVERSED").status_code == 404)

# ---------------------------------------------------------------- 2. 证明幂等、冲突、重叠与相邻
print("2) 校准证明：全局唯一号幂等/冲突，同设备有效区间不重叠，相邻允许")
r = cert("CAL-001", "DEV-CAL-01", "2026-09-30T01:00:00Z", "2026-09-30T03:00:00Z")
check("登记证明 201", r.status_code == 201 and r.json()["idempotent"] is False, r.text)
body = r.json()["certificate"]
check("证明响应字段与左闭右开",
      body["certificate_id"] == "CAL-001" and body["device_id"] == "DEV-CAL-01"
      and body["valid_from"] == "2026-09-30T01:00:00Z"
      and body["valid_to"] == "2026-09-30T03:00:00Z"
      and body["issued_by"] == "计量员甲" and body["status"] == "active"
      and body["voided_at"] is None, str(body))
r = cert("CAL-001", "DEV-CAL-01", "2026-09-30T03:00:00+02:00", "2026-09-30T05:00:00+02:00")
check("同号同内容（时区表示不同）200 返回原结果",
      r.status_code == 200 and r.json()["idempotent"] is True
      and r.json()["certificate"]["registered_at"] == body["registered_at"], r.text)
r = cert("CAL-001", "DEV-CAL-01", "2026-09-30T01:00:00Z", "2026-09-30T04:00:00Z")
check("同号异内容 409 calibration_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "calibration_conflict", r.text)
r = cert("CAL-002", "DEV-CAL-01", "2026-09-30T02:00:00Z", "2026-09-30T04:00:00Z")
check("有效时段重叠 409 calibration_overlap",
      r.status_code == 409 and r.json()["error"]["code"] == "calibration_overlap"
      and r.json()["error"]["details"]["overlapping_certificate_ids"] == ["CAL-001"], r.text)
check("重叠失败不留新证明", client.get(f"{API}/temperature-calibrations/CAL-002").status_code == 404)
r = cert("CAL-003", "DEV-CAL-01", "2026-09-30T03:00:00Z", "2026-09-30T05:00:00Z")
check("相邻端点不重叠（左闭右开）201", r.status_code == 201, r.text)
rd("CAL-PRE", "CAL-RD-OTHER", "2026-09-30T02:30:00Z", 4.0, "DEV-CAL-OTHER")
r = cert("CAL-OTHER-DEV", "DEV-CAL-OTHER", "2026-09-30T02:00:00Z", "2026-09-30T04:00:00Z")
check("不同设备可使用相同有效时段（证明号仍全局唯一）", r.status_code == 201, r.text)
r = client.get(f"{API}/temperature-calibrations?device_id=DEV-CAL-01")
check("按设备列出证明", r.status_code == 200 and r.json()["total"] == 2
      and [x["certificate_id"] for x in r.json()["items"]] == ["CAL-001", "CAL-003"], r.text)
r = client.get(f"{API}/temperature-calibrations/CAL-001")
check("按证明号查询", r.status_code == 200 and r.json()["certificate"]["certificate_id"] == "CAL-001", r.text)
r = client.get(f"{API}/temperature-calibrations/NO-SUCH-CAL")
check("未知证明查询 404", r.status_code == 404
      and r.json()["error"]["code"] == "calibration_not_found", r.text)

# ---------------------------------------------------------------- 3. 可信匹配：逐条证明号或缺证原因
print("3) 可信复核：逐条列证明号/缺证原因，左闭右开，无有证读数明确无法评估")
reg("CAL-MATCH")
cfg("CAL-MATCH")
# 设备 A：无证明、早于证明、证明内、结束点不含、晚于证明、证明空档
rd("CAL-MATCH", "M-RD-1", "2026-09-30T01:00:00Z", 5.0, "DEV-M-A")
rd("CAL-MATCH", "M-RD-2", "2026-09-30T02:00:00Z", 5.0, "DEV-M-A")
rd("CAL-MATCH", "M-RD-3", "2026-09-30T03:00:00Z", 1.0, "DEV-M-A")
rd("CAL-MATCH", "M-RD-4", "2026-09-30T04:00:00Z", 5.0, "DEV-M-A")
rd("CAL-MATCH", "M-RD-5", "2026-09-30T04:30:00Z", 5.0, "DEV-M-A")
rd("CAL-MATCH", "M-RD-8", "2026-09-30T06:00:00Z", 4.0, "DEV-M-A")
rd("CAL-MATCH", "M-RD-9", "2026-09-30T05:30:00Z", 4.0, "DEV-M-A")
cert("MC-A1", "DEV-M-A", "2026-09-30T02:00:00Z", "2026-09-30T04:00:00Z")
cert("MC-A2", "DEV-M-A", "2026-09-30T05:00:00Z", "2026-09-30T06:00:00Z")
# 设备 B：无任何证明（避开同样本同时刻唯一约束）
rd("CAL-MATCH", "M-RD-6", "2026-09-30T02:01:00Z", 5.0, "DEV-M-B")
# 设备 C：证明覆盖后作废
rd("CAL-MATCH", "M-RD-7", "2026-09-30T02:02:00Z", 9.5, "DEV-M-C")
cert("MC-C1", "DEV-M-C", "2026-09-30T01:00:00Z", "2026-09-30T03:00:00Z")
void("MC-C1", "纸质证明编号复核不通过")

v = trusted("CAL-MATCH").json()
check("读数总数与有证/缺证计数",
      v["reading_count"] == 9 and v["certified_reading_count"] == 3
      and v["uncertified_reading_count"] == 6, str(v))
check("有证读数可评估", v["assessment_status"] == "assessable" and v["unable_to_assess_reason"] is None, str(v))
check("起始点含在证明内",
      match(v, "M-RD-2")["certified"] is True and match(v, "M-RD-2")["certificate_id"] == "MC-A1")
check("结束点不含（左闭右开；随后证明开始前仍属空档）",
      match(v, "M-RD-4")["certified"] is False
      and match(v, "M-RD-4")["missing_reason"]["code"] == "calibration_gap", str(match(v, "M-RD-4")))
check("早于证明缺证原因", match(v, "M-RD-1")["missing_reason"]["code"] == "before_calibration_validity")
check("证明空档缺证原因", match(v, "M-RD-5")["missing_reason"]["code"] == "calibration_gap")
check("晚于最后证明缺证原因", match(v, "M-RD-8")["missing_reason"]["code"] == "after_calibration_validity")
check("无证明设备缺证原因", match(v, "M-RD-6")["missing_reason"]["code"] == "calibration_not_registered")
check("已作废证明缺证原因",
      match(v, "M-RD-7")["certified"] is False
      and match(v, "M-RD-7")["missing_reason"]["code"] == "calibration_voided"
      and match(v, "M-RD-7")["missing_reason"]["voided_certificate_ids"] == ["MC-C1"], str(match(v, "M-RD-7")))
check("仅用有证读数计算超温点",
      [x["reading_id"] for x in v["out_of_range_points"]] == ["M-RD-3"], str(v["out_of_range_points"]))
check("作废证明覆盖的超温读数不参与评估",
      all(x["reading_id"] != "M-RD-7" for x in v["out_of_range_points"]))

reg("CAL-NONE-CERT")
cfg("CAL-NONE-CERT")
rd("CAL-NONE-CERT", "N-RD-1", "2026-09-30T02:00:00Z", 9.0, "DEV-NO-CERT")
v = trusted("CAL-NONE-CERT").json()
check("没有有证读数明确标记无法评估",
      v["assessment_status"] == "unable_to_assess"
      and v["certified_reading_count"] == 0
      and "无法评估" in v["unable_to_assess_reason"]
      and v["out_of_range_points"] == [] and v["gap_intervals"] == [], str(v))
check("缺证逐条原因仍列出",
      v["reading_matches"][0]["missing_reason"]["code"] == "calibration_not_registered", str(v))

# ---------------------------------------------------------------- 4. 相邻有证读数缺测与保管链拆分
print("4) 可信缺测：只连接相邻有证读数，沿用保管链拆分与归属")
reg("CAL-GAP")
cfg("CAL-GAP", interval=1800)
client.post(f"{API}/events", json={
    "event_id": "CAL-EVT-T1", "event_type": "transfer", "sample_code": "CAL-GAP",
    "occurred_at": "2026-09-30T02:30:00Z", "transfer_no": "CAL-TR-1",
    "from_custodian": "王采集", "to_custodian": "李运输"})
client.post(f"{API}/events", json={
    "event_id": "CAL-EVT-R1", "event_type": "receive", "sample_code": "CAL-GAP",
    "occurred_at": "2026-09-30T04:30:00Z", "transfer_no": "CAL-TR-1",
    "receiver": "李运输", "seal_no": "SEAL-C"})
rd("CAL-GAP", "G-RD-1", "2026-09-30T01:30:00Z", 4.0, "DEV-GAP")
rd("CAL-GAP", "G-RD-X", "2026-09-30T02:30:00Z", 9.0, "DEV-GAP")
rd("CAL-GAP", "G-RD-2", "2026-09-30T05:30:00Z", 5.0, "DEV-GAP")
cert("GC-1", "DEV-GAP", "2026-09-30T01:00:00Z", "2026-09-30T02:00:00Z")
cert("GC-2", "DEV-GAP", "2026-09-30T05:00:00Z", "2026-09-30T06:00:00Z")
v = trusted("CAL-GAP").json()
check("无证读数不作为缺测端点",
      v["certified_reading_count"] == 2
      and len(v["gap_intervals"]) == 1
      and v["gap_intervals"][0]["before_reading_id"] == "G-RD-1"
      and v["gap_intervals"][0]["after_reading_id"] == "G-RD-2", str(v))
gap = v["gap_intervals"][0]
check("有证读数间跨移交/签收时刻拆分为三段",
      [s["attribution"]["kind"] for s in gap["segments"]] == ["custodian", "in_transit", "custodian"]
      and [s["start"] for s in gap["segments"]]
      == ["2026-09-30T01:30:00Z", "2026-09-30T02:30:00Z", "2026-09-30T04:30:00Z"], str(gap))
check("无证超温点不列入可信超温", v["out_of_range_points"] == [], str(v))

# ---------------------------------------------------------------- 5. 补录/作废即时重算，原查询和版本不变
print("5) 补录/作废证明后即时重算；不改原始读数、既有测温查询和保管链版本")
reg("CAL-LIVE")
cfg("CAL-LIVE")
client.post(f"{API}/events", json={
    "event_id": "LIVE-EVT-T1", "event_type": "transfer", "sample_code": "CAL-LIVE",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "LIVE-TR",
    "from_custodian": "王采集", "to_custodian": "李运输"})
rd("CAL-LIVE", "L-RD-1", "2026-09-30T01:30:00Z", 4.0, "DEV-LIVE")
rd("CAL-LIVE", "L-RD-2", "2026-09-30T03:30:00Z", 9.0, "DEV-LIVE")
before_versions = versions("CAL-LIVE")["current_version"]
before_old = temperature("CAL-LIVE").json()
v = trusted("CAL-LIVE").json()
check("补证前无法评估", v["assessment_status"] == "unable_to_assess", str(v))
cert("LIVE-C1", "DEV-LIVE", "2026-09-30T01:00:00Z", "2026-09-30T04:00:00Z")
v = trusted("CAL-LIVE").json()
check("补证后即时可评估且识别超温",
      v["assessment_status"] == "assessable"
      and [x["reading_id"] for x in v["out_of_range_points"]] == ["L-RD-2"]
      and v["out_of_range_points"][0]["attribution"]["kind"] == "in_transit", str(v))
check("证明操作不产生保管链版本", versions("CAL-LIVE")["current_version"] == before_versions)
after_old = temperature("CAL-LIVE").json()
check("既有测温查询仍使用全部原始读数",
      after_old["reading_count"] == before_old["reading_count"]
      and len(after_old["out_of_range_points"]) == len(before_old["out_of_range_points"])
      and len(after_old["gap_intervals"]) == len(before_old["gap_intervals"])
      and "certificate_id" not in after_old["readings"][0], str(after_old))
r = void("LIVE-C1", "证明撤销")
check("作废证明 201", r.status_code == 201 and r.json()["certificate"]["status"] == "void"
      and r.json()["certificate"]["void_reason"] == "证明撤销", r.text)
original_void_at = r.json()["certificate"]["voided_at"]
v = trusted("CAL-LIVE").json()
check("作废后查询即时变为无法评估",
      v["assessment_status"] == "unable_to_assess" and v["out_of_range_points"] == []
      and v["gap_intervals"] == [] and all(not x["certified"] for x in v["reading_matches"]), str(v))
check("作废证明不产生保管链版本", versions("CAL-LIVE")["current_version"] == before_versions)

# 同作废理由幂等，异理由冲突
r = void("LIVE-C1", "证明撤销")
check("同证明同作废理由重试 200 幂等",
      r.status_code == 200 and r.json()["idempotent"] is True
      and r.json()["certificate"]["voided_at"] == original_void_at,
      r.text)
r2 = void("LIVE-C1", "另一个作废理由")
check("异作废理由 409 calibration_void_conflict",
      r2.status_code == 409 and r2.json()["error"]["code"] == "calibration_void_conflict", r2.text)
r = client.get(f"{API}/temperature-calibrations/LIVE-C1")
check("冲突不改原作废记录",
      r.json()["certificate"]["void_reason"] == "证明撤销"
      and r.json()["certificate"]["voided_at"] == original_void_at, r.text)
r = client.post(f"{API}/temperature-calibrations/NO-SUCH/void", json={"reason": "未知"})
check("作废未知证明 404 且不留记录",
      r.status_code == 404 and r.json()["error"]["code"] == "calibration_not_found", r.text)
r = client.post(f"{API}/temperature-calibrations/LIVE-C1/void", json={"reason": "   "})
check("空白作废原因 422", r.status_code == 422, r.text)

# ---------------------------------------------------------------- 6. 作废后可登记重叠时段；相邻设备证明
print("6) 作废证明不再参与重叠，后续证明可覆盖同一时段")
reg("CAL-VOID-OVERLAP")
cfg("CAL-VOID-OVERLAP")
rd("CAL-VOID-OVERLAP", "V-RD-1", "2026-09-30T02:00:00Z", 5.0, "DEV-REPLACE")
cert("VOID-OLD", "DEV-REPLACE", "2026-09-30T01:00:00Z", "2026-09-30T05:00:00Z")
void("VOID-OLD", "旧证作废")
r = cert("VOID-NEW", "DEV-REPLACE", "2026-09-30T02:00:00Z", "2026-09-30T04:00:00Z")
check("与已作废证明重叠允许登记", r.status_code == 201, r.text)
r = cert("VOID-THIRD", "DEV-REPLACE", "2026-09-30T03:00:00Z", "2026-09-30T06:00:00Z")
check("仍不得与新的有效证明重叠",
      r.status_code == 409 and r.json()["error"]["code"] == "calibration_overlap", r.text)
v = trusted("CAL-VOID-OVERLAP").json()
check("读数匹配新证明而非旧作废证明",
      match(v, "V-RD-1")["certificate_id"] == "VOID-NEW", str(v))

# ---------------------------------------------------------------- 7. 核定后仍可补录/作废证明
print("7) 证明可晚于核定补录/作废，不触碰冻结样本、版本或案卷")
reg("CAL-APPROVED")
cfg("CAL-APPROVED")
client.post(f"{API}/events", json={
    "event_id": "AP-EVT-T1", "event_type": "transfer", "sample_code": "CAL-APPROVED",
    "occurred_at": "2026-09-30T01:30:00Z", "transfer_no": "AP-TR",
    "from_custodian": "王采集", "to_custodian": "李运输"})
client.post(f"{API}/events", json={
    "event_id": "AP-EVT-R1", "event_type": "receive", "sample_code": "CAL-APPROVED",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "AP-TR",
    "receiver": "李运输", "seal_no": "SEAL-C"})
client.post(f"{API}/events", json={
    "event_id": "AP-EVT-S1", "event_type": "storage", "sample_code": "CAL-APPROVED",
    "occurred_at": "2026-09-30T02:30:00Z", "custodian": "实验室"})
rd("CAL-APPROVED", "AP-RD-1", "2026-09-30T01:45:00Z", 9.0, "DEV-APPROVED")
client.post(f"{API}/samples/CAL-APPROVED/approve", json={"approver": "质量主管"})
approved_version = versions("CAL-APPROVED")["current_version"]
r = cert("AP-CAL-1", "DEV-APPROVED", "2026-09-30T01:00:00Z", "2026-09-30T03:00:00Z")
check("核定后可补录校准证明", r.status_code == 201, r.text)
v = trusted("CAL-APPROVED").json()
check("核定后可信查询可用且使用补证",
      v["assessment_status"] == "assessable"
      and v["out_of_range_points"][0]["certificate_id"] == "AP-CAL-1", str(v))
check("补证不改保管链版本", versions("CAL-APPROVED")["current_version"] == approved_version)
r = void("AP-CAL-1", "核定后补录撤销演示")
check("核定后可作废证明", r.status_code == 201, r.text)
check("作废不改保管链版本", versions("CAL-APPROVED")["current_version"] == approved_version)
r = rd("CAL-APPROVED", "AP-RD-LATE", "2026-09-30T01:50:00Z", 5.0, "DEV-APPROVED")
check("既有核定后新读数仍冻结", r.status_code == 409 and r.json()["error"]["code"] == "history_locked", r.text)

# ---------------------------------------------------------------- 8. 并发
print("8) 并发：同号同内容一建一幂等；重叠时段一胜一败")
reg("CAL-CONC")
cfg("CAL-CONC")
rd("CAL-CONC", "CC-RD-1", "2026-09-30T02:00:00Z", 5.0, "DEV-CONCURRENT")
results: dict[str, int] = {}
barrier = threading.Barrier(2)


def post_same_cert(tag):
    barrier.wait()
    results[tag] = cert("CC-CAL-1", "DEV-CONCURRENT",
                        "2026-09-30T01:00:00Z", "2026-09-30T03:00:00Z").status_code


t1 = threading.Thread(target=post_same_cert, args=("A",))
t2 = threading.Thread(target=post_same_cert, args=("B",))
t1.start(); t2.start(); t1.join(); t2.join()
check("同证明号同内容并发：一 201 一 200", sorted(results.values()) == [200, 201], str(results))

results.clear()
barrier = threading.Barrier(2)
t1 = threading.Thread(target=lambda: (barrier.wait(), results.__setitem__(
    "C", cert("CC-CAL-2", "DEV-CONCURRENT", "2026-09-30T02:00:00Z", "2026-09-30T04:00:00Z").status_code)))
t2 = threading.Thread(target=lambda: (barrier.wait(), results.__setitem__(
    "D", cert("CC-CAL-3", "DEV-CONCURRENT", "2026-09-30T03:30:00Z", "2026-09-30T05:00:00Z").status_code)))
t1.start(); t2.start(); t1.join(); t2.join()
check("同设备重叠证明并发：一 201 一 409", sorted(results.values()) == [201, 409], str(results))

# ---------------------------------------------------------------- 9. 全局列表与健康检查
print("9) 全局列表、健康检查")
r = client.get(f"{API}/temperature-calibrations")
items = r.json()["items"]
ids = {x["certificate_id"] for x in items}
check("全局列出全部证明（含已作废）",
      r.status_code == 200 and {"LIVE-C1", "MC-C1", "VOID-NEW", "AP-CAL-1"}.issubset(ids), r.text)
check("健康检查可用", client.get("/health").json()["status"] == "ok")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
