"""可信测温复核异常处置（运输质控）。

质控人对“可信测温复核”当前仍存在的超温点与缺测区间留存处置意见（认可 / 排除）。

稳定标识（不随补录/纠错变化，仅用于追踪同一异常项）：
  * 超温点：oor:<读数号>（读数号全局唯一）；
  * 缺测区间：gap:<前一有证读数号>:<后一有证读数号>（端点读数号全局唯一）。

证据摘要（evidence_digest）：对异常项的全部证据要素做规范 JSON 的 SHA-256——
  超温点含读数（设备/时刻/数值/方向/限值）、覆盖该读数的校准证明号、依当前有效保管链
  重算的归属；缺测区间含端点读数、两端校准证明号、总时长/最大间隔、跨移交/签收时刻
  拆分后的逐段时间与归属。读数补录、证明补录或作废、保管链纠错使异常消失或任一要素
  变化时，摘要随之变化（或异常项消失）。

处置规则：
  * 提交必须回传查询时所见的稳定标识与证据摘要；仅接受当前仍存在且摘要一致的异常项；
    异常消失或摘要过期 → 422 anomaly_stale，整单拒绝且不留记录（不占处置单号）；
  * 同一异常项同一证据摘要只接受一次处置：重复提交 → 409 anomaly_already_disposed；
  * 同处置单号同内容重试 → 200 幂等返回原结果（即使原意见已失效）；异内容（含跨样本
    复用单号）→ 409 disposition_conflict；
  * 异常项或归属变化后，原处置意见在查询/提交时惰性对账标记为失效（anomaly_gone /
    evidence_changed），永久可查、不删除不改写；新摘要下可重新处置；
  * 处置只写 temp_anomaly_disposition 表：不改动原始读数、校准证明、既有可信复核结论、
    保管链版本与已封存案卷（也不产生保管链版本）。
"""
import json
import sqlite3
from typing import Any

from .calibrations import _certificates_by_device, _match_certificate, get_trusted_temperature_review
from .db import utcnow_iso
from .schemas import AnomalyDispositionIn
from .service import DomainError, _fingerprint, get_sample_or_404
from .temperature import _get_config_row

DECISION_ACK = "acknowledged"   # 认可异常
DECISION_EXCLUDE = "excluded"   # 排除异常

STATUS_ACTIVE = "active"
STATUS_INVALIDATED = "invalidated"

REASON_GONE = "anomaly_gone"
REASON_CHANGED = "evidence_changed"


# --------------------------------------------------------------------------- 指纹

def _request_fingerprint(data: AnomalyDispositionIn) -> str:
    return _fingerprint({
        "disposition_no": data.disposition_no,
        "sample_code": data.sample_code,
        "anomaly_id": data.anomaly_id,
        "expected_evidence_digest": data.expected_evidence_digest.strip().lower(),
        "decision": data.decision,
        "reason": data.reason.strip(),
        "handler": data.handler.strip(),
    })


# --------------------------------------------------------------------------- 当前异常项

def _certificate_brief(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "certificate_id": row["certificate_id"],
        "device_id": row["device_id"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "issued_by": row["issued_by"],
        "status": "void" if row["voided_at"] else "active",
        "voided_at": row["voided_at"],
    }


def _reading_brief(match: dict[str, Any], cert_row: sqlite3.Row | None) -> dict[str, Any]:
    return {
        "reading_id": match["reading_id"],
        "device_id": match["device_id"],
        "occurred_at": match["occurred_at"],
        "value_celsius": match["value_celsius"],
        "in_range": match["in_range"],
        "certificate_id": match["certificate_id"],
        "certificate": _certificate_brief(cert_row),
        "recorded_at": match["recorded_at"],
    }


def _point_canonical(point: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "out_of_range_point",
        "reading_id": point["reading_id"],
        "device_id": point["device_id"],
        "occurred_at": point["occurred_at"],
        "value_celsius": point["value_celsius"],
        "direction": point["direction"],
        "limit_celsius": point["limit_celsius"],
        "certificate_id": point["certificate_id"],
        "attribution": point["attribution"],
    }


def _point_summary(p: dict[str, Any]) -> str:
    if p["direction"] == "above_max":
        verb, bound = "高于上限", p["limit_celsius"]
    else:
        verb, bound = "低于下限", p["limit_celsius"]
    pending = "，归属待核（保管链断链，不臆定责任人）" if p["attribution"]["kind"] == "pending_review" else ""
    cert = f"，校准证明 {p['certificate_id']}" if p["certificate_id"] else ""
    return (f"超温点（{verb}）：读数 {p['reading_id']} 于 {p['occurred_at']} 测得 "
            f"{p['value_celsius']}℃，{verb} {bound}℃{cert}{pending}")


def _gap_canonical(gap: dict[str, Any], endpoint_certs: dict[str, str | None]) -> dict[str, Any]:
    return {
        "kind": "gap_interval",
        "before_reading_id": gap["before_reading_id"],
        "after_reading_id": gap["after_reading_id"],
        "start": gap["start"],
        "end": gap["end"],
        "duration_seconds": gap["duration_seconds"],
        "max_interval_seconds": gap["max_interval_seconds"],
        "before_certificate_id": endpoint_certs[gap["before_reading_id"]],
        "after_certificate_id": endpoint_certs[gap["after_reading_id"]],
        "segments": [
            {
                "start": s["start"],
                "end": s["end"],
                "duration_seconds": s["duration_seconds"],
                "attribution": s["attribution"],
            }
            for s in gap["segments"]
        ],
    }


def _gap_summary(g: dict[str, Any], endpoint_certs: dict[str, str | None]) -> str:
    pending = any(s["attribution"]["kind"] == "pending_review" for s in g["segments"])
    tail = "，其中存在归属待核段（保管链断链，不臆定责任人）" if pending else ""
    return (
        f"缺测区间：相邻有证读数 {g['before_reading_id']}"
        f"（证明 {endpoint_certs[g['before_reading_id']] or '无'}）与 {g['after_reading_id']}"
        f"（证明 {endpoint_certs[g['after_reading_id']] or '无'}）间隔 {g['duration_seconds']} 秒，"
        f"超过最大测温间隔 {g['max_interval_seconds']} 秒，跨移交/签收时刻拆分为 "
        f"{len(g['segments'])} 段{tail}"
    )


def build_current_anomalies(conn: sqlite3.Connection, sample: sqlite3.Row) -> dict[str, Any]:
    """按当前可信测温复核计算异常项（稳定标识 + 证据摘要 + 证据要素与归属）。

    返回 {"assessable": bool, "assessment_status": str, "unable_to_assess_reason": str|None,
          "anomalies": {anomaly_id: anomaly_view}}。未设定测温配置时抛 404 temp_config_not_found。
    计算只读原始证据（读数/证明/有效保管链事件），不写任何表。
    """
    review = get_trusted_temperature_review(conn, sample["sample_code"])
    if review["assessment_status"] != "assessable":
        return {
            "assessable": False,
            "assessment_status": review["assessment_status"],
            "unable_to_assess_reason": review["unable_to_assess_reason"],
            "anomalies": {},
        }

    match_by_reading = {m["reading_id"]: m for m in review["reading_matches"]}
    cert_rows_by_device = _certificates_by_device(
        conn, {m["device_id"] for m in review["reading_matches"]}
    )

    def cert_for(reading_id: str) -> sqlite3.Row | None:
        m = match_by_reading[reading_id]
        cert, _ = _match_certificate(cert_rows_by_device[m["device_id"]], m["occurred_at"])
        return cert

    anomalies: dict[str, dict[str, Any]] = {}

    for p in review["out_of_range_points"]:
        cert_row = cert_for(p["reading_id"])
        view = {
            "anomaly_id": f"oor:{p['reading_id']}",
            "anomaly_type": "out_of_range_point",
            "summary": _point_summary(p),
            "time_range": {"start": p["occurred_at"], "end": p["occurred_at"]},
            "reading": {
                "reading_id": p["reading_id"],
                "device_id": p["device_id"],
                "occurred_at": p["occurred_at"],
                "value_celsius": p["value_celsius"],
                "direction": p["direction"],
                "limit_celsius": p["limit_celsius"],
                "certificate_id": p["certificate_id"],
                "certificate": _certificate_brief(cert_row),
            },
            "attribution": p["attribution"],
            "attribution_pending": p["attribution"]["kind"] == "pending_review",
        }
        view["evidence_digest"] = _fingerprint(_point_canonical(p))
        anomalies[view["anomaly_id"]] = view

    for g in review["gap_intervals"]:
        endpoint_certs = {
            g["before_reading_id"]: match_by_reading[g["before_reading_id"]]["certificate_id"],
            g["after_reading_id"]: match_by_reading[g["after_reading_id"]]["certificate_id"],
        }
        readings = [
            _reading_brief(match_by_reading[g["before_reading_id"]], cert_for(g["before_reading_id"])),
            _reading_brief(match_by_reading[g["after_reading_id"]], cert_for(g["after_reading_id"])),
        ]
        view = {
            "anomaly_id": f"gap:{g['before_reading_id']}:{g['after_reading_id']}",
            "anomaly_type": "gap_interval",
            "summary": _gap_summary(g, endpoint_certs),
            "time_range": {
                "start": g["start"],
                "end": g["end"],
                "duration_seconds": g["duration_seconds"],
                "max_interval_seconds": g["max_interval_seconds"],
            },
            "readings": readings,
            "segments": g["segments"],
            "attribution_pending": any(
                s["attribution"]["kind"] == "pending_review" for s in g["segments"]
            ),
        }
        view["evidence_digest"] = _fingerprint(_gap_canonical(g, endpoint_certs))
        anomalies[view["anomaly_id"]] = view

    return {
        "assessable": True,
        "assessment_status": review["assessment_status"],
        "unable_to_assess_reason": None,
        "anomalies": anomalies,
    }


# --------------------------------------------------------------------------- 失效对账（惰性）

def _list_dispositions(conn: sqlite3.Connection, sample_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM temp_anomaly_disposition WHERE sample_id = ? ORDER BY id ASC",
        (sample_pk,),
    ).fetchall()


def reconcile_dispositions(conn: sqlite3.Connection, sample: sqlite3.Row) -> None:
    """按当前异常项对账该样本的全部有效处置意见（惰性失效标记）。

    * 当前异常集合中已无该标识 → invalidated / anomaly_gone；
    * 异常仍在但证据摘要变化（归属或证据要素变化）→ invalidated / evidence_changed，
      记录重算后的当前摘要供比对；
    * 已失效记录永久保留，不再复活（即使新证据恰好与旧摘要相同也不回改状态）。
    """
    current = build_current_anomalies(conn, sample)["anomalies"]
    now = utcnow_iso()
    for row in _list_dispositions(conn, sample["id"]):
        if row["status"] != STATUS_ACTIVE:
            continue
        cur = current.get(row["anomaly_id"])
        if cur is None:
            conn.execute(
                "UPDATE temp_anomaly_disposition SET status = ?, invalidated_at = ?,"
                " invalidate_reason = ?, current_evidence_digest = NULL WHERE id = ?",
                (STATUS_INVALIDATED, now, REASON_GONE, row["id"]),
            )
        elif cur["evidence_digest"] != row["evidence_digest"]:
            conn.execute(
                "UPDATE temp_anomaly_disposition SET status = ?, invalidated_at = ?,"
                " invalidate_reason = ?, current_evidence_digest = ? WHERE id = ?",
                (STATUS_INVALIDATED, now, REASON_CHANGED, cur["evidence_digest"], row["id"]),
            )


# --------------------------------------------------------------------------- 视图

def _disposition_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "disposition_no": row["disposition_no"],
        "anomaly_id": row["anomaly_id"],
        "anomaly_type": row["anomaly_type"],
        "decision": row["decision"],
        "reason": row["reason"],
        "handler": row["handler"],
        "submitted_at": row["created_at"],
        "submitted_evidence_digest": row["evidence_digest"],
        "status": row["status"],
        "invalidated_at": row["invalidated_at"],
        "invalidate_reason": row["invalidate_reason"],
        "current_evidence_digest": row["current_evidence_digest"],
        "submitted_evidence": json.loads(row["evidence_json"]),
    }


def _attach_state(view: dict[str, Any], active: sqlite3.Row | None,
                  history: list[sqlite3.Row]) -> dict[str, Any]:
    if active is not None:
        view["disposition_status"] = "active"
        view["disposition"] = _disposition_view(active)
    else:
        view["disposition_status"] = "pending"
        view["disposition"] = None
    view["invalidated_history"] = [_disposition_view(r) for r in history]
    return view


def list_anomaly_view(conn: sqlite3.Connection, sample_code: str) -> dict[str, Any]:
    """按样本查询当前异常项及处置状态（未处置 / 有效处置 / 失效历史三分）。

    提交式对账：查询本身不改动原始证据，但会把已过期的有效处置意见标记为失效
    （只更新处置表状态，旧意见永久留痕）。
    """
    sample = get_sample_or_404(conn, sample_code)
    reconcile_dispositions(conn, sample)
    built = build_current_anomalies(conn, sample)

    active_by_anomaly: dict[str, sqlite3.Row] = {}
    history_by_anomaly: dict[str, list[sqlite3.Row]] = {}
    vanished = 0
    for row in _list_dispositions(conn, sample["id"]):
        if row["status"] == STATUS_ACTIVE:
            active_by_anomaly[row["anomaly_id"]] = row
        else:
            if row["anomaly_id"] not in built["anomalies"]:
                vanished += 1
            history_by_anomaly.setdefault(row["anomaly_id"], []).append(row)

    items: list[dict[str, Any]] = []
    for view in built["anomalies"].values():
        items.append(_attach_state(
            view,
            active_by_anomaly.get(view["anomaly_id"]),
            history_by_anomaly.get(view["anomaly_id"], []),
        ))
    items.sort(key=lambda x: (x["time_range"]["start"], x["anomaly_id"]))

    return {
        "sample_code": sample["sample_code"],
        "assessment_status": built["assessment_status"],
        "unable_to_assess_reason": built["unable_to_assess_reason"],
        "total": len(items),
        "pending_count": sum(1 for i in items if i["disposition_status"] == "pending"),
        "active_count": sum(1 for i in items if i["disposition_status"] == "active"),
        "invalidated_history_count": sum(len(i["invalidated_history"]) for i in items),
        "vanished_disposition_count": vanished,
        "items": items,
    }


def list_disposition_ledger(
    conn: sqlite3.Connection, sample_code: str, status: str | None = None
) -> dict[str, Any]:
    """处置台账：该样本全部处置单（含异常已消失的失效记录）。

    ?status=active|invalidated 过滤；每条附提交时证据快照与该异常当前是否仍存在/摘要是否一致。
    """
    sample = get_sample_or_404(conn, sample_code)
    has_config = _get_config_row(conn, sample["id"]) is not None
    if has_config:
        reconcile_dispositions(conn, sample)
        current = build_current_anomalies(conn, sample)["anomalies"]
    else:
        # 未设定测温配置：无当前异常可对账，台账仍可读历史处置
        current = {}

    rows = _list_dispositions(conn, sample["id"])
    if status is not None:
        rows = [r for r in rows if r["status"] == status]

    items: list[dict[str, Any]] = []
    for row in rows:
        view = _disposition_view(row)
        if not has_config:
            view["current"] = {
                "assessable": False,
                "present": None,
                "evidence_digest": None,
                "digest_matches": None,
            }
        else:
            cur = current.get(row["anomaly_id"])
            view["current"] = {
                "assessable": True,
                "present": cur is not None,
                "evidence_digest": cur["evidence_digest"] if cur is not None else None,
                "digest_matches": cur is not None and cur["evidence_digest"] == row["evidence_digest"],
            }
        items.append(view)

    return {
        "sample_code": sample["sample_code"],
        "status_filter": status,
        "total": len(items),
        "active_count": sum(1 for i in items if i["status"] == STATUS_ACTIVE),
        "invalidated_count": sum(1 for i in items if i["status"] == STATUS_INVALIDATED),
        "items": items,
    }


# --------------------------------------------------------------------------- 提交

def _get_disposition_by_no(conn: sqlite3.Connection, disposition_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM temp_anomaly_disposition WHERE disposition_no = ?",
        (disposition_no,),
    ).fetchone()


def submit_disposition(
    conn: sqlite3.Connection, sample_code: str, data: AnomalyDispositionIn
) -> tuple[dict[str, Any], bool]:
    """提交一份异常处置单。返回 ({"disposition": 处置视图, "anomaly": 当前异常视图}, 是否幂等重试)。

    全部校验先于写入：异常消失 / 摘要过期 / 已处置 / 单号冲突均整单拒绝且不留记录。
    """
    if data.sample_code != sample_code:
        raise DomainError(
            422,
            "disposition_sample_mismatch",
            "处置单提交的样本编号与路径样本编号不一致（处置不得跨样本）",
            {"path_sample_code": sample_code, "body_sample_code": data.sample_code},
        )

    request_hash = _request_fingerprint(data)
    sample = get_sample_or_404(conn, data.sample_code)

    # 1) 同处置单号：先幂等/冲突判定（与既有单号接口一致，原提交不因事后证据变化而被判冲突）
    existing = _get_disposition_by_no(conn, data.disposition_no)
    if existing is not None:
        if existing["sample_id"] != sample["id"] or existing["request_hash"] != request_hash:
            raise DomainError(
                409,
                "disposition_conflict",
                f"处置单号 {data.disposition_no} 已使用但提交内容不一致（同单号异内容冲突），意见不可改",
                {"disposition_no": data.disposition_no,
                 "existing_request_hash": existing["request_hash"],
                 "incoming_request_hash": request_hash},
            )
        # 幂等重试返回原结果；先按当前证据对账状态（原意见可能已失效）
        reconcile_dispositions(conn, sample)
        existing = _get_disposition_by_no(conn, data.disposition_no)
        current = build_current_anomalies(conn, sample)["anomalies"]
        body = {"disposition": _disposition_view(existing),
                "anomaly": current.get(existing["anomaly_id"])}
        return body, True

    # 2) 按当前证据对账旧意见（可能把其他处置单标记失效），再判定本单
    reconcile_dispositions(conn, sample)
    built = build_current_anomalies(conn, sample)
    current = built["anomalies"]
    anomaly = current.get(data.anomaly_id)

    incoming_digest = data.expected_evidence_digest.strip().lower()
    if anomaly is None:
        raise DomainError(
            422,
            "anomaly_stale",
            f"异常项 {data.anomaly_id} 当前已不存在（读数补录、证明补录/作废或保管链纠错"
            "可使超温点/缺测区间消失），整单拒绝且不留记录；请重新查询当前异常项",
            {"sample_code": data.sample_code, "anomaly_id": data.anomaly_id,
             "reason": REASON_GONE, "submitted_evidence_digest": incoming_digest,
             "assessment_status": built["assessment_status"],
             "current_anomaly_ids": sorted(current)},
        )
    if anomaly["evidence_digest"] != incoming_digest:
        raise DomainError(
            422,
            "anomaly_stale",
            f"异常项 {data.anomaly_id} 仍存在但证据摘要已变化（读数补录、证明补录/作废或"
            "保管链纠错可改变证据或归属），整单拒绝且不留记录；请按新摘要重新提交",
            {"sample_code": data.sample_code, "anomaly_id": data.anomaly_id,
             "reason": REASON_CHANGED,
             "submitted_evidence_digest": incoming_digest,
             "current_evidence_digest": anomaly["evidence_digest"]},
        )

    active = conn.execute(
        "SELECT * FROM temp_anomaly_disposition"
        " WHERE anomaly_id = ? AND evidence_digest = ? AND status = ?",
        (data.anomaly_id, anomaly["evidence_digest"], STATUS_ACTIVE),
    ).fetchone()
    if active is not None:
        raise DomainError(
            409,
            "anomaly_already_disposed",
            f"异常项 {data.anomaly_id} 在同一证据摘要下已有有效处置单 {active['disposition_no']}"
            "（处置人 %s）；同一异常项同一证据摘要只接受一次处置" % active["handler"],
            {"sample_code": data.sample_code, "anomaly_id": data.anomaly_id,
             "evidence_digest": anomaly["evidence_digest"],
             "existing_disposition_no": active["disposition_no"],
             "existing_decision": active["decision"]},
        )

    created_at = utcnow_iso()
    snapshot = {k: v for k, v in anomaly.items()}
    try:
        cur = conn.execute(
            "INSERT INTO temp_anomaly_disposition"
            " (disposition_no, sample_id, sample_code, anomaly_id, anomaly_type, evidence_digest,"
            "  decision, reason, handler, evidence_json, request_hash, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (data.disposition_no, sample["id"], sample["sample_code"], data.anomaly_id,
             anomaly["anomaly_type"], anomaly["evidence_digest"], data.decision,
             data.reason.strip(), data.handler.strip(),
             json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
             request_hash, STATUS_ACTIVE, created_at),
        )
    except sqlite3.IntegrityError:
        raced = _get_disposition_by_no(conn, data.disposition_no)
        if raced is not None:
            if raced["request_hash"] == request_hash:
                body = {"disposition": _disposition_view(raced),
                        "anomaly": current.get(raced["anomaly_id"])}
                return body, True
            raise DomainError(
                409, "disposition_conflict",
                f"处置单号 {data.disposition_no} 并发写入且内容冲突",
                {"disposition_no": data.disposition_no},
            )
        # 部分唯一索引竞争：同一异常同一摘要的有效处置被并发抢先提交
        winner = conn.execute(
            "SELECT * FROM temp_anomaly_disposition"
            " WHERE anomaly_id = ? AND evidence_digest = ? AND status = ?",
            (data.anomaly_id, anomaly["evidence_digest"], STATUS_ACTIVE),
        ).fetchone()
        raise DomainError(
            409,
            "anomaly_already_disposed",
            f"异常项 {data.anomaly_id} 在同一证据摘要下的有效处置被并发提交"
            f"（{winner['disposition_no'] if winner else '未知单号'}），按已提交状态判定",
            {"sample_code": data.sample_code, "anomaly_id": data.anomaly_id,
             "existing_disposition_no": winner["disposition_no"] if winner else None},
        )

    row = conn.execute(
        "SELECT * FROM temp_anomaly_disposition WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return {"disposition": _disposition_view(row), "anomaly": anomaly}, False
