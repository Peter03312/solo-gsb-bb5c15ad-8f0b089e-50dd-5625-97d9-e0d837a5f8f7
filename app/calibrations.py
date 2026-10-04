"""测温设备校准证明登记、作废与可信测温复核。

校准证明是独立于样本保管链的设备证据：证明号全局唯一，只能围绕已有读数中出现过的
设备号登记；有效期使用左闭右开区间 [valid_from, valid_to)。同一设备未作废证明的有效
时段不得重叠（端点相邻允许），作废证明不再覆盖任何读数。证明可晚于运输记录补录，可信
复核在查询时即时匹配，不改动原始读数、既有测温查询、保管链版本或已封存案卷。
"""
import sqlite3
from typing import Any

from .db import utcnow_iso
from .schemas import CalibrationIn, CalibrationVoidIn
from .service import DomainError, _fingerprint, get_sample_or_404, to_utc_iso
from .temperature import (
    _attribution_at,
    _config_view,
    _custody_periods,
    _gap_intervals,
    _get_config_row,
)


# --------------------------------------------------------------------------- 视图与校验

def _calibration_fingerprint(data: CalibrationIn, valid_from: str, valid_to: str) -> str:
    return _fingerprint({
        "certificate_id": data.certificate_id,
        "device_id": data.device_id,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "issued_by": data.issued_by,
    })


def _void_fingerprint(certificate_id: str, reason: str) -> str:
    return _fingerprint({"certificate_id": certificate_id, "reason": reason})


def calibration_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "certificate_id": row["certificate_id"],
        "device_id": row["device_id"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "issued_by": row["issued_by"],
        "status": "void" if row["voided_at"] else "active",
        "registered_at": row["created_at"],
        "voided_at": row["voided_at"],
        "void_reason": row["void_reason"],
    }


def _get_certificate(conn: sqlite3.Connection, certificate_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM device_calibration WHERE certificate_id = ?",
        (certificate_id,),
    ).fetchone()


def get_certificate_or_404(conn: sqlite3.Connection, certificate_id: str) -> sqlite3.Row:
    row = _get_certificate(conn, certificate_id)
    if row is None:
        raise DomainError(
            404,
            "calibration_not_found",
            f"校准证明不存在: {certificate_id}",
            {"certificate_id": certificate_id},
        )
    return row


def _device_has_reading(conn: sqlite3.Connection, device_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM temp_reading WHERE device_id = ? LIMIT 1",
        (device_id,),
    ).fetchone() is not None


# --------------------------------------------------------------------------- 登记 / 作废

def register_calibration(
    conn: sqlite3.Connection, data: CalibrationIn
) -> tuple[dict[str, Any], bool]:
    """登记校准证明。返回 (证明视图, 是否同号同内容幂等重试)。"""
    valid_from = to_utc_iso(data.valid_from)
    valid_to = to_utc_iso(data.valid_to)
    h = _calibration_fingerprint(data, valid_from, valid_to)

    existing = _get_certificate(conn, data.certificate_id)
    if existing is not None:
        if existing["request_hash"] != h:
            raise DomainError(
                409,
                "calibration_conflict",
                f"校准证明号 {data.certificate_id} 已存在但提交内容不一致（同号异内容冲突）",
                {
                    "certificate_id": data.certificate_id,
                    "existing_request_hash": existing["request_hash"],
                    "incoming_request_hash": h,
                },
            )
        return calibration_view(existing), True

    # 幂等检查之后再做业务约束：失败不插入证明；BEGIN IMMEDIATE 保证重叠判定串行。
    if not _device_has_reading(conn, data.device_id):
        raise DomainError(
            422,
            "calibration_device_without_readings",
            f"设备 {data.device_id} 尚无测温读数，校准证明只能围绕已有读数的设备号补录",
            {"device_id": data.device_id},
        )

    overlapping = conn.execute(
        "SELECT certificate_id, valid_from, valid_to FROM device_calibration"
        " WHERE device_id = ? AND certificate_id <> ? AND voided_at IS NULL"
        " AND valid_from < ? AND valid_to > ?"
        " ORDER BY valid_from ASC, id ASC",
        (data.device_id, data.certificate_id, valid_to, valid_from),
    ).fetchall()
    if overlapping:
        raise DomainError(
            409,
            "calibration_overlap",
            f"设备 {data.device_id} 已有有效校准证明时段与新区间 [{valid_from}, {valid_to}) 重叠",
            {
                "device_id": data.device_id,
                "incoming": {"valid_from": valid_from, "valid_to": valid_to},
                "overlapping_certificate_ids": [r["certificate_id"] for r in overlapping],
            },
        )

    try:
        cur = conn.execute(
            "INSERT INTO device_calibration"
            " (certificate_id, device_id, valid_from, valid_to, issued_by, request_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (data.certificate_id, data.device_id, valid_from, valid_to,
             data.issued_by, h, utcnow_iso()),
        )
    except sqlite3.IntegrityError:
        raced = _get_certificate(conn, data.certificate_id)
        if raced is not None:
            if raced["request_hash"] == h:
                return calibration_view(raced), True
            raise DomainError(
                409,
                "calibration_conflict",
                f"校准证明号 {data.certificate_id} 并发写入且内容冲突",
            )
        raise
    row = conn.execute("SELECT * FROM device_calibration WHERE id = ?", (cur.lastrowid,)).fetchone()
    return calibration_view(row), False


def void_calibration(
    conn: sqlite3.Connection, certificate_id: str, data: CalibrationVoidIn
) -> tuple[dict[str, Any], bool]:
    """按证明号附理由作废。同号同理由重试返回原结果，不同理由冲突。"""
    existing = get_certificate_or_404(conn, certificate_id)
    h = _void_fingerprint(certificate_id, data.reason)

    if existing["voided_at"] is not None:
        if existing["void_request_hash"] != h:
            raise DomainError(
                409,
                "calibration_void_conflict",
                f"校准证明 {certificate_id} 已作废且本次理由与原作废请求不一致",
                {
                    "certificate_id": certificate_id,
                    "existing_void_reason": existing["void_reason"],
                    "incoming_reason": data.reason,
                    "existing_request_hash": existing["void_request_hash"],
                    "incoming_request_hash": h,
                },
            )
        return calibration_view(existing), True

    conn.execute(
        "UPDATE device_calibration SET voided_at = ?, void_reason = ?, void_request_hash = ?"
        " WHERE id = ? AND voided_at IS NULL",
        (utcnow_iso(), data.reason, h, existing["id"]),
    )
    row = get_certificate_or_404(conn, certificate_id)
    return calibration_view(row), False


def list_calibrations(conn: sqlite3.Connection, device_id: str | None = None) -> dict[str, Any]:
    if device_id is not None:
        rows = conn.execute(
            "SELECT * FROM device_calibration WHERE device_id = ? ORDER BY valid_from ASC, id ASC",
            (device_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM device_calibration ORDER BY device_id ASC, valid_from ASC, id ASC"
        ).fetchall()
    return {
        "total": len(rows),
        "device_id": device_id,
        "items": [calibration_view(r) for r in rows],
    }


# --------------------------------------------------------------------------- 可信复核

def _missing_certificate_reason(
    rows: list[sqlite3.Row], occurred_at: str
) -> dict[str, Any]:
    """设备存在证明但读数时刻无有效证明时，给出缺证原因。"""
    covering_void = [
        r for r in rows
        if r["voided_at"] is not None and r["valid_from"] <= occurred_at < r["valid_to"]
    ]
    active = [r for r in rows if r["voided_at"] is None]
    if covering_void:
        return {
            "code": "calibration_voided",
            "message": "覆盖该读数的校准证明已作废",
            "voided_certificate_ids": [r["certificate_id"] for r in covering_void],
        }

    considered = active or rows
    earliest = min(r["valid_from"] for r in considered)
    latest_end = max(r["valid_to"] for r in considered)
    if occurred_at < earliest:
        return {
            "code": "before_calibration_validity",
            "message": "读数早于该设备校准证明有效起始时刻",
            "earliest_valid_from": earliest,
        }
    if occurred_at >= latest_end:
        return {
            "code": "after_calibration_validity",
            "message": "读数不早于该设备校准证明有效结束时刻（结束时刻不含）",
            "latest_valid_to": latest_end,
        }
    return {
        "code": "calibration_gap",
        "message": "读数位于该设备相邻校准证明有效时段之间的空档",
    }


def _certificates_by_device(
    conn: sqlite3.Connection, device_ids: set[str]
) -> dict[str, list[sqlite3.Row]]:
    if not device_ids:
        return {}
    marks = ",".join("?" for _ in device_ids)
    rows = conn.execute(
        f"SELECT * FROM device_calibration WHERE device_id IN ({marks})"
        " ORDER BY valid_from ASC, id ASC",
        tuple(device_ids),
    ).fetchall()
    result: dict[str, list[sqlite3.Row]] = {d: [] for d in device_ids}
    for row in rows:
        result[row["device_id"]].append(row)
    return result


def _match_certificate(
    cert_rows: list[sqlite3.Row], occurred_at: str
) -> tuple[sqlite3.Row | None, dict[str, Any] | None]:
    active = [
        r for r in cert_rows
        if r["voided_at"] is None and r["valid_from"] <= occurred_at < r["valid_to"]
    ]
    if active:
        # 登记时已拒绝重叠；按最早起始稳定选取，避免异常数据导致结果不确定。
        return sorted(active, key=lambda r: (r["valid_from"], r["id"]))[0], None
    if not cert_rows:
        return None, {
            "code": "calibration_not_registered",
            "message": "该设备尚未登记校准证明",
        }
    return None, _missing_certificate_reason(cert_rows, occurred_at)


def get_trusted_temperature_review(
    conn: sqlite3.Connection, sample_code: str
) -> dict[str, Any]:
    """仅以有有效校准证明的读数评估超温点与相邻有证读数缺测区间。"""
    sample = get_sample_or_404(conn, sample_code)
    config = _get_config_row(conn, sample["id"])
    if config is None:
        raise DomainError(
            404,
            "temp_config_not_found",
            f"样本 {sample_code} 尚未设定测温配置",
            {"sample_code": sample_code},
        )

    readings = conn.execute(
        "SELECT * FROM temp_reading WHERE sample_id = ? ORDER BY occurred_at ASC, id ASC",
        (sample["id"],),
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM custody_event WHERE sample_id = ? AND superseded_by IS NULL"
        " ORDER BY occurred_at ASC, id ASC",
        (sample["id"],),
    ).fetchall()
    periods = _custody_periods(sample, events)
    certs_by_device = _certificates_by_device(conn, {r["device_id"] for r in readings})

    min_c = config["min_celsius"]
    max_c = config["max_celsius"]
    max_interval = config["max_interval_seconds"]

    reading_matches: list[dict[str, Any]] = []
    certified_rows: list[sqlite3.Row] = []
    for r in readings:
        cert, reason = _match_certificate(certs_by_device[r["device_id"]], r["occurred_at"])
        in_range = min_c <= r["value_celsius"] <= max_c
        item = {
            "reading_id": r["reading_id"],
            "device_id": r["device_id"],
            "occurred_at": r["occurred_at"],
            "value_celsius": r["value_celsius"],
            "in_range": in_range,
            "certified": cert is not None,
            "certificate_id": cert["certificate_id"] if cert is not None else None,
            "missing_reason": reason,
            "recorded_at": r["created_at"],
        }
        reading_matches.append(item)
        if cert is not None:
            certified_rows.append(r)

    out_of_range: list[dict[str, Any]] = []
    for r in certified_rows:
        value = r["value_celsius"]
        if min_c <= value <= max_c:
            continue
        cert, _ = _match_certificate(certs_by_device[r["device_id"]], r["occurred_at"])
        below = value < min_c
        out_of_range.append({
            "reading_id": r["reading_id"],
            "device_id": r["device_id"],
            "occurred_at": r["occurred_at"],
            "value_celsius": value,
            "certificate_id": cert["certificate_id"],
            "direction": "below_min" if below else "above_max",
            "limit_celsius": min_c if below else max_c,
            "attribution": _attribution_at(periods, r["occurred_at"]),
        })

    gaps = _gap_intervals(certified_rows, max_interval, events, periods)
    assessable = bool(certified_rows)

    return {
        "sample_code": sample["sample_code"],
        "config": _config_view(config, sample["sample_code"]),
        "reading_count": len(readings),
        "certified_reading_count": len(certified_rows),
        "uncertified_reading_count": len(readings) - len(certified_rows),
        "assessment_status": "assessable" if assessable else "unable_to_assess",
        "unable_to_assess_reason": (
            None if assessable else "没有任何读数被有效校准证明覆盖，无法评估超温点与缺测区间"
        ),
        "reading_matches": reading_matches,
        "out_of_range_points": out_of_range if assessable else [],
        "gap_intervals": gaps if assessable else [],
    }
