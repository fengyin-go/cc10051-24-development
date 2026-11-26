"""抽取批次到标准化清洗之间的幂等工作流。

这里不依赖 HTTP，路由层和既有领域服务都调用本模块，保证：

* 无规则时只登记/找回等待清洗批次，不生成空清洗记录；
* 规则必须与抽取批次的实体编码精确匹配，来源和实体在重试/领用时核对；
* 一个批次只有一条主清洗记录，重复通知、迟到重试和失败恢复都不会复制完整确认；
* 领用请求把等待批次与实际抽取行关联，完成/失败时登记规则、行数和可继续结果。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.store import store

FLOW_LOCK = threading.RLock()

WAITING = "等待清洗"
PROCESSING = "清洗中"
DONE = "已完成"
FAILED = "清洗失败"

STATE_BY_STATUS = {
    WAITING: "waiting",
    PROCESSING: "processing",
    DONE: "completed",
    FAILED: "failed",
}

BATCH_KEYS = ("batch_no", "batchNo", "batch", "批次号", "抽取批次", "清洗批次", "批次编号")
EXTRACT_ID_KEYS = ("entry_id", "extract_id", "job_id", "抽取作业ID", "作业ID", "id")
SOURCE_KEYS = ("source", "source_system", "sourceSystem", "源系统", "源系统标识", "系统标识")
ENTITY_KEYS = ("entity", "entity_code", "entityCode", "实体编码", "实体代码")
REQUEST_ID_KEYS = ("request_id", "requestId", "idempotency_key", "idempotencyKey", "请求编号")
RULE_ID_KEYS = ("rule_id", "ruleId", "清洗规则ID", "规则ID")
RULE_NAME_KEYS = ("rule_name", "ruleName", "规则名称")
ROW_COUNT_KEYS = (
    "row_count", "rowCount", "record_count", "records", "processed_rows", "total_rows",
    "行数", "记录数", "抽取行数", "已处理行数",
)
REASON_KEYS = ("reason", "message", "failure_reason", "失败原因", "异常原因", "原因")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(table: str) -> list[dict[str, Any]]:
    return store.rows(table)


def _next_id(table: str) -> int:
    rows = _rows(table)
    return max((int(row.get("id", 0) or 0) for row in rows), default=0) + 1


def _pick(payload: dict[str, Any] | None, keys: tuple[str, ...]) -> Any:
    if not payload:
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    nested = payload.get("data")
    if isinstance(nested, dict):
        return _pick(nested, keys)
    return None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def state_of(status: str) -> str:
    return STATE_BY_STATUS.get(status, "unknown")


def batch_no_of_extract(job: dict[str, Any]) -> str:
    explicit = _pick(job, BATCH_KEYS)
    if explicit:
        return _text(explicit)
    # 本项目的示例作业以“作业名称”作为跨模块批次名；R1 是常见的人工批次号。
    return _text(job.get("作业名称")) or f"R{int(job.get('id', 0) or 0)}"


def batch_aliases_of_extract(job: dict[str, Any]) -> set[str]:
    job_id = job.get("id")
    aliases = {batch_no_of_extract(job), _text(job.get("作业名称")), _text(job.get("批次号"))}
    if job_id not in (None, ""):
        aliases.add(f"R{int(job_id or 0)}")
    return {alias for alias in aliases if alias}


def find_extract(
    payload: dict[str, Any] | None = None,
    *,
    entry_id: int | str | None = None,
    batch_no: str | None = None,
) -> dict[str, Any] | None:
    if entry_id is None and payload is not None:
        entry_id = _pick(payload, EXTRACT_ID_KEYS)
    if batch_no is None and payload is not None:
        batch_no = _text(_pick(payload, BATCH_KEYS))

    if entry_id not in (None, ""):
        try:
            job = store.find("record_extract", int(entry_id))
            if job is not None:
                return job
        except (TypeError, ValueError):
            candidate = next(
                (row for row in _rows("record_extract") if _text(row.get("id")) == _text(entry_id)),
                None,
            )
            if candidate is not None:
                return candidate

    if batch_no:
        for job in _rows("record_extract"):
            if batch_no in batch_aliases_of_extract(job):
                return job
        return None

    # 只有来源时不应该随意定位作业；调用方可以继续用来源找批次。
    return None


def find_batch(batch_no: str | None) -> dict[str, Any] | None:
    if not batch_no:
        return None
    batch_no = _text(batch_no)
    return next((row for row in _rows("cleanse_batch") if _text(row.get("批次号")) == batch_no
                 or _text(row.get("batch_no")) == batch_no
                 or batch_no in [_text(item) for item in row.get("aliases", [])]), None)


def find_record(batch_no: str | None = None, record_no: str | int | None = None) -> dict[str, Any] | None:
    records = _rows("cleanse_record")
    if record_no not in (None, ""):
        text = _text(record_no)
        found = next((row for row in records if _text(row.get("清洗记录号")) == text
                      or _text(row.get("record_no")) == text
                      or _text(row.get("id")) == text), None)
        if found is not None:
            return found
    if batch_no:
        batch_no = _text(batch_no)
        return next((row for row in records if _text(row.get("批次号")) == batch_no
                     or _text(row.get("batch_no")) == batch_no), None)
    return None


def eligible_rules(entity_code: str, rule_id: Any = None) -> list[dict[str, Any]]:
    entity_code = _text(entity_code)
    rules = [
        rule for rule in _rows("std_cleanse")
        if _text(rule.get("status")) == "已启用"
        and _text(rule.get("实体编码")) == entity_code
    ]
    if rule_id not in (None, ""):
        wanted = _text(rule_id)
        rules = [rule for rule in rules if _text(rule.get("id")) == wanted
                 or _text(rule.get("规则名称")) == wanted]
    # 确定性：同一实体存在多条启用规则时，始终选择 ID 最小的一条，不做随机派发。
    return sorted(rules, key=lambda rule: int(rule.get("id", 0) or 0))


def _attach_batch_to_rule(rule: dict[str, Any] | None, batch_no: str) -> None:
    if rule is None:
        return
    pending = rule.setdefault("待清洗批次", [])
    # 兼容既有 dup_detect 从规则行读取“待清洗批次”的锚点；幂等追加，重复通知不重复。
    if batch_no not in pending:
        pending.append(batch_no)


def _no_rule_reason(entity_code: str) -> str:
    return (
        f"暂停原因：实体「{entity_code}」当前没有实体编码精确匹配的已启用清洗规则；"
        "其他实体的已启用规则不能填补本批次空态。"
    )


def _rule_ready_reason(rule: dict[str, Any]) -> str:
    return f"规则「{rule.get('规则名称', rule.get('id'))}」已可受理，等待值班员或清洗 Worker 正式领用认领。"


def verify_scope(
    payload: dict[str, Any] | None,
    source: str | None = None,
    entity_code: str | None = None,
) -> dict[str, Any] | None:
    """返回 None 表示通过，否则返回可直接响应的 200/ok=false 业务错误。"""
    expected_source = _text(_pick(payload, SOURCE_KEYS))
    expected_entity = _text(_pick(payload, ENTITY_KEYS))
    if expected_source and source is not None and expected_source != _text(source):
        return {
            "ok": False,
            "code": "SOURCE_MISMATCH",
            "message": f"来源核对失败：请求来源 {expected_source} 与批次来源 {source} 不一致",
        }
    if expected_entity and entity_code is not None and expected_entity != _text(entity_code):
        return {
            "ok": False,
            "code": "ENTITY_MISMATCH",
            "message": f"实体核对失败：请求实体 {expected_entity} 与批次实体 {entity_code} 不一致",
        }
    return None


def _remember_request(batch: dict[str, Any], group: str, request_id: Any) -> bool:
    """记录幂等键。返回 True 表示这是重复请求。"""
    key = _text(request_id)
    if not key:
        return False
    bucket = batch.setdefault(group, [])
    if key in bucket:
        return True
    bucket.append(key)
    return False


def ensure_extract_rows(
    job: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """关联并返回某抽取作业的实际抽取行；首次调用时可接收 rows 或按行数登记。"""
    batch_no = batch_no_of_extract(job)
    job_id = int(job.get("id", 0) or 0)
    table = _rows("extract_row")
    existing = [
        row for row in table
        if int(row.get("extract_id", 0) or 0) == job_id
        or _text(row.get("批次号")) == batch_no
    ]
    if existing:
        return sorted(existing, key=lambda row: int(row.get("id", 0) or 0))

    raw_rows = _pick(payload, ("rows", "raw_rows", "extract_rows", "实际抽取行", "抽取行", "数据行"))
    source = job.get("源系统", "")
    entity = job.get("实体编码", "")
    created: list[dict[str, Any]] = []

    if isinstance(raw_rows, list) and raw_rows:
        for index, raw in enumerate(raw_rows, start=1):
            values = raw if isinstance(raw, dict) else {"value": raw}
            row = {
                "id": _next_id("extract_row") + index - 1,
                "extract_id": job_id,
                "批次号": batch_no,
                "源系统": values.get("源系统", source),
                "实体编码": values.get("实体编码", entity),
                "记录键": values.get("记录键") or values.get("id") or f"{batch_no}-ROW-{index}",
                "原始数据": values,
                "status": "已抽取",
                "pending": False,
                "abnormal": False,
                "extracted_at": now_iso(),
            }
            table.append(row)
            created.append(row)
        return created

    count = _int(_pick(payload, ROW_COUNT_KEYS), default=-1)
    if count < 0:
        count = _int(job.get("抽取行数") or job.get("记录数") or job.get("行数"), default=0)
    # 对没有显式行数据的旧示例作业保留可演示的实际行；真实链路会由完成接口写入 rows。
    if count <= 0 and job.get("status") == "已完成":
        count = 1

    for index in range(1, count + 1):
        row = {
            "id": _next_id("extract_row") + index - 1,
            "extract_id": job_id,
            "批次号": batch_no,
            "源系统": source,
            "实体编码": entity,
            "记录键": f"{batch_no}-ROW-{index}",
            "原始数据": {"序号": index, "源系统": source, "实体编码": entity},
            "status": "已抽取",
            "pending": False,
            "abnormal": False,
            "extracted_at": now_iso(),
        }
        table.append(row)
        created.append(row)
    return created


def _batch_summary(batch: dict[str, Any]) -> dict[str, Any]:
    """返回可安全放进通用 ActionResult.entry 的批次视图。"""
    return dict(batch)


def register_batch_for_extract(
    job: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Upsert R1 批次。返回 (batch, created)。"""
    batch_no = batch_no_of_extract(job)
    source = _text(job.get("源系统"))
    entity = _text(job.get("实体编码"))
    rows = ensure_extract_rows(job, payload)
    rule_candidates = eligible_rules(entity)
    rule = rule_candidates[0] if rule_candidates else None

    existing = find_batch(batch_no)
    if existing is not None:
        # 批次号是稳定幂等键；即使重复派发/通知，也只更新关联和规则可用性。
        existing.setdefault("extract_id", int(job.get("id", 0) or 0))
        existing["抽取作业ID"] = int(job.get("id", 0) or 0)
        existing["抽取作业"] = job.get("作业名称", "")
        existing["源系统"] = source or existing.get("源系统", "")
        existing["实体编码"] = entity or existing.get("实体编码", "")
        existing["行数"] = len(rows) or _int(existing.get("行数"), 0)
        existing["row_count"] = existing["行数"]
        if existing.get("status") == WAITING:
            if rule is not None:
                existing["available_rule_id"] = rule["id"]
                existing["规则ID"] = rule["id"]
                existing["规则名称"] = rule.get("规则名称", "")
                existing["claimable"] = True
                _attach_batch_to_rule(rule, _text(existing.get("批次号")))
                if not existing.get("pause_reason_locked"):
                    existing["暂停原因"] = _rule_ready_reason(rule)
            else:
                existing["available_rule_id"] = None
                existing["claimable"] = False
                existing["暂停原因"] = _no_rule_reason(entity)
        return existing, False

    row_count = len(rows)
    batch_id = _next_id("cleanse_batch")
    batch = {
        "id": batch_id,
        "state": state_of(WAITING),
        "status": WAITING,
        "pending": True,
        "abnormal": False,
        "批次号": batch_no,
        "batch_no": batch_no,
        "aliases": sorted(batch_aliases_of_extract(job) | {batch_no}),
        "extract_id": int(job.get("id", 0) or 0),
        "抽取作业ID": int(job.get("id", 0) or 0),
        "抽取作业": job.get("作业名称", ""),
        "源系统": source,
        "source": source,
        "实体编码": entity,
        "entity_code": entity,
        "行数": row_count,
        "row_count": row_count,
        "available_rule_id": rule.get("id") if rule else None,
        "规则ID": rule.get("id") if rule else None,
        "规则名称": rule.get("规则名称", "") if rule else "",
        "claimable": rule is not None,
        "recoverable": False,
        "暂停原因": _rule_ready_reason(rule) if rule else _no_rule_reason(entity),
        "pause_reason": "" if rule else "NO_ENABLED_RULE",
        "清洗记录号": "",
        "claim_token": "",
        "claimed_rule_id": None,
        "dispatch_count": 1,
        "claim_count": 0,
        "recovery_count": 0,
        "notification_count": 0,
        "dispatch_request_ids": [],
        "claim_request_ids": [],
        "confirmation_ids": [],
        "notified_rule_ids": [],
        "history": [{
            "at": now_iso(),
            "event": "registered",
            "message": "已把完成抽取的数据登记为等待清洗批次",
        }],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    _attach_batch_to_rule(rule, batch_no)
    _rows("cleanse_batch").append(batch)
    job["清洗批次号"] = batch_no
    job["cleanse_batch_no"] = batch_no
    job["清洗状态"] = WAITING
    return batch, True


def complete_extraction(
    entry_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """登记抽取完成和实际行，推进游标，并幂等派发清洗。"""
    payload = payload or {}
    with FLOW_LOCK:
        job = find_extract(payload, entry_id=entry_id)
        if job is None:
            return {"ok": False, "code": "EXTRACT_NOT_FOUND", "message": "抽取作业不存在"}

        mismatch = verify_scope(
            payload,
            source=_text(job.get("源系统")),
            entity_code=_text(job.get("实体编码")),
        )
        if mismatch:
            return mismatch

        count = _int(_pick(payload, ROW_COUNT_KEYS), default=0)
        raw_rows = _pick(payload, ("rows", "raw_rows", "extract_rows", "实际抽取行", "抽取行"))
        if count <= 0 and isinstance(raw_rows, list):
            count = len(raw_rows)
        if count > 0:
            job["抽取行数"] = count

        job["status"] = "已完成"
        job["pending"] = False
        job["abnormal"] = False
        job["completed_at"] = now_iso()

        current_cursor = _int(job.get("游标位置"), default=0)
        job["游标位置"] = str(current_cursor + max(count, 0))
        for cursor in _rows("sync_cursor"):
            if cursor.get("源系统") == job.get("源系统"):
                cursor["游标值"] = job["游标位置"]
                cursor["推进时间"] = now_iso()

        # 先确保行落库，再登记批次；这样报错/失败后 R1 与实际行都能找回。
        ensure_extract_rows(job, payload)
        return dispatch_extract(entry_id=int(job["id"]), payload=payload)


def dispatch_extract(
    entry_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """服务端判断规则能否受理，并幂等登记/返回等待清洗批次。"""
    payload = payload or {}
    with FLOW_LOCK:
        job = find_extract(payload, entry_id=entry_id)
        if job is None:
            return {"ok": False, "code": "EXTRACT_NOT_FOUND", "message": "抽取作业不存在"}

        mismatch = verify_scope(
            payload,
            source=_text(job.get("源系统")),
            entity_code=_text(job.get("实体编码")),
        )
        if mismatch:
            return mismatch

        requested_batch = _text(_pick(payload, BATCH_KEYS))
        if requested_batch:
            explicit_batch = _text(job.get("批次号") or job.get("抽取批次号") or job.get("batch_no"))
            if explicit_batch and requested_batch != explicit_batch:
                return {
                    "ok": False,
                    "code": "BATCH_MISMATCH",
                    "message": f"批次号核对失败：请求 {requested_batch} 与原批次 {explicit_batch} 不一致",
                }
            if not explicit_batch:
                # 调用方显式给出的 R1 是稳定幂等键；后续迟到/重试必须继续携带它。
                job["批次号"] = requested_batch

        if _text(job.get("status")) != "已完成":
            return {
                "ok": False,
                "code": "EXTRACT_NOT_COMPLETED",
                "message": f"抽取作业当前状态为 {job.get('status')}，完成后才能派发清洗",
                "entry": job,
            }

        request_id = _pick(payload, REQUEST_ID_KEYS)
        batch, created = register_batch_for_extract(job, payload)
        duplicate_request = _remember_request(batch, "dispatch_request_ids", request_id)
        if not created:
            batch["dispatch_count"] = _int(batch.get("dispatch_count"), 1) + (0 if duplicate_request else 1)
        batch["updated_at"] = now_iso()
        batch["history"].append({
            "at": now_iso(),
            "event": "dispatch",
            "duplicate": duplicate_request or not created,
            "request_id": _text(request_id),
            "message": "收到派发/重试请求" + ("（幂等重复）" if duplicate_request or not created else ""),
        })

        record = find_record(batch_no=batch["批次号"])
        rule = eligible_rules(batch["实体编码"])
        selected_rule = rule[0] if rule else None
        accepted = selected_rule is not None
        if record and record.get("status") == DONE:
            message = f"批次 {batch['批次号']} 已完成清洗，返回已有完整确认，不重复生成结果"
        elif accepted:
            message = f"批次 {batch['批次号']} 已登记，规则 {selected_rule.get('规则名称', selected_rule['id'])} 可受理，等待正式领用认领"
        else:
            message = f"批次 {batch['批次号']} 已保留为等待清洗状态；当前没有可受理的精确匹配规则，未生成空清洗输出"

        return {
            "ok": True,
            "accepted": accepted,
            "rule_accepted": accepted,
            "waiting": not accepted and not record,
            "created": created,
            "duplicate": (not created) or duplicate_request,
            "code": "RULE_READY" if accepted else "NO_ENABLED_RULE",
            "message": message,
            "batch": _batch_summary(batch),
            "entry": job,
            "rule": selected_rule,
            "record": record,
        }


def retry_extract(entry_id: int | None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """迟到的重试：核对来源/实体后按同一批次号幂等续办。

    如果已有可受理规则，重试请求可直接成为正式领用并完成内置清洗；
    如果规则仍缺失，则只刷新/找回等待批次。
    """
    payload = dict(payload or {})
    with FLOW_LOCK:
        job = find_extract(payload, entry_id=entry_id)
        if job is None:
            return {"ok": False, "message": "抽取作业不存在"}
        mismatch = verify_scope(payload, job.get("源系统", ""), job.get("实体编码", ""))
        if mismatch:
            return mismatch

        if _text(job.get("status")) == "抽取失败":
            job["status"] = "执行中"
            job["pending"] = True
            job["abnormal"] = False
            return {"ok": True, "message": "来源和实体核对通过，抽取作业已重新执行；完成后可继续派发", "entry": job}

        result = dispatch_extract(entry_id=int(job["id"]), payload=payload)
        if not result.get("ok"):
            return result

        batch = result["batch"]
        # 迟到的“重试”可以充当正式认领；若调用方只想登记，可传 auto_claim=false。
        auto_claim = payload.get("auto_claim", payload.get("autoClaim", True))
        if auto_claim is not False and batch.get("claimable") and batch.get("status") == WAITING:
            executed = execute_batch({
                **payload,
                "batch_no": batch["批次号"],
                "source": batch["源系统"],
                "entity_code": batch["实体编码"],
            })
            executed["late_retry"] = True
            return executed
        result["late_retry"] = True
        return result


def notify_rule_enabled(rule: dict[str, Any]) -> dict[str, Any]:
    """规则启用通知。通知只让等待批次变为可认领，不复制批次或清洗记录。"""
    entity = _text(rule.get("实体编码"))
    rule_id = rule.get("id")
    affected: list[dict[str, Any]] = []
    with FLOW_LOCK:
        for batch in _rows("cleanse_batch"):
            if _text(batch.get("实体编码")) != entity:
                continue
            if batch.get("status") not in (WAITING, FAILED):
                continue
            notice_key = f"rule:{rule_id}:enabled"
            already_notified = notice_key in batch.setdefault("notified_rule_ids", [])
            if not already_notified:
                batch["notified_rule_ids"].append(notice_key)
                batch["notification_count"] = _int(batch.get("notification_count"), 0) + 1
                batch.setdefault("history", []).append({
                    "at": now_iso(),
                    "event": "rule_enabled_notification",
                    "rule_id": rule_id,
                    "message": f"收到新规则启用通知：{rule.get('规则名称', rule_id)}",
                })
            if batch.get("status") == WAITING:
                batch["available_rule_id"] = rule_id
                batch["规则ID"] = rule_id
                batch["规则名称"] = rule.get("规则名称", "")
                batch["claimable"] = True
                _attach_batch_to_rule(rule, _text(batch.get("批次号")))
                batch["暂停原因"] = _rule_ready_reason(rule)
            affected.append(batch)
        return {"ok": True, "affected": len(affected), "batches": affected}


def find_and_verify_batch(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    batch_no = _text(_pick(payload, BATCH_KEYS))
    batch = find_batch(batch_no)

    if batch is None:
        source = _text(_pick(payload, SOURCE_KEYS))
        entity = _text(_pick(payload, ENTITY_KEYS))
        candidates = _rows("cleanse_batch")
        if source:
            candidates = [row for row in candidates if _text(row.get("源系统")) == source]
        if entity:
            candidates = [row for row in candidates if _text(row.get("实体编码")) == entity]
        if not batch_no and source:
            candidates = [row for row in candidates if row.get("status") in (WAITING, PROCESSING, FAILED)]
        candidates = sorted(candidates, key=lambda row: int(row.get("id", 0) or 0))
        if len(candidates) > 1 and not batch_no:
            return None, {
                "ok": False,
                "code": "AMBIGUOUS_BATCH",
                "message": "该来源存在多个清洗批次，请携带批次号后再领用",
            }
        if candidates:
            batch = candidates[0]

    if batch is None:
        return None, {"ok": False, "code": "BATCH_NOT_FOUND", "message": "找不到对应等待清洗批次"}

    mismatch = verify_scope(
        payload,
        source=_text(batch.get("源系统")),
        entity_code=_text(batch.get("实体编码")),
    )
    if mismatch:
        return None, mismatch
    return batch, None


def claim_batch(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    with FLOW_LOCK:
        batch, error = find_and_verify_batch(payload)
        if error:
            return error
        assert batch is not None

        request_id = _pick(payload, REQUEST_ID_KEYS)
        duplicate = _remember_request(batch, "claim_request_ids", request_id)

        if batch.get("status") == DONE:
            record = find_record(batch_no=batch["批次号"])
            return {
                "ok": True,
                "duplicate": True,
                "code": "ALREADY_COMPLETED",
                "message": f"批次 {batch['批次号']} 已有完整清洗确认，重复领用不产生新结果",
                "batch": batch,
                "record": record,
                "rows": [],
            }

        if batch.get("status") == PROCESSING:
            return {
                "ok": True,
                "duplicate": True,
                "code": "ALREADY_CLAIMED",
                "claim_token": batch.get("claim_token", ""),
                "message": "批次已被领用；重复请求返回原领用凭证，不重复处理",
                "batch": batch,
                "rows": ensure_extract_rows(find_extract(entry_id=batch.get("extract_id")) or {
                    "id": batch.get("extract_id"),
                    "源系统": batch.get("源系统"),
                    "实体编码": batch.get("实体编码"),
                    "作业名称": batch.get("抽取作业"),
                }, payload),
            }

        entity = _text(batch.get("实体编码"))
        requested_rule = _pick(payload, RULE_ID_KEYS)
        rules = eligible_rules(entity, requested_rule)
        if requested_rule and not rules:
            return {
                "ok": False,
                "code": "RULE_NOT_ELIGIBLE",
                "message": "指定规则不存在、未启用或实体编码不匹配，不能受理该批次",
                "batch": batch,
            }
        if not rules:
            batch["status"] = WAITING
            batch["state"] = state_of(WAITING)
            batch["claimable"] = False
            batch["暂停原因"] = _no_rule_reason(entity)
            return {"ok": False, "code": "NO_ENABLED_RULE", "message": "当前没有实体精确匹配的已启用清洗规则", "batch": batch}

        rule = rules[0]
        _attach_batch_to_rule(rule, _text(batch.get("批次号")))
        # 失败后的重新领用视为恢复，但仍保持同一个批次号和同一条清洗记录。
        recovered = batch.get("status") == FAILED
        if recovered:
            batch["recovery_count"] = _int(batch.get("recovery_count"), 0) + 1
        batch["status"] = PROCESSING
        batch["state"] = state_of(PROCESSING)
        batch["pending"] = True
        batch["abnormal"] = False
        batch["recoverable"] = False
        batch["claimed_rule_id"] = rule["id"]
        batch["规则ID"] = rule["id"]
        batch["available_rule_id"] = rule["id"]
        batch["规则名称"] = rule.get("规则名称", "")
        batch["claim_token"] = f"claim-{batch['批次号']}-{uuid.uuid4().hex[:8]}"
        batch["claimed_at"] = now_iso()
        batch["claim_count"] = _int(batch.get("claim_count"), 0) + 1
        batch["updated_at"] = now_iso()
        batch.setdefault("history", []).append({
            "at": now_iso(),
            "event": "claimed" if not recovered else "recovered_and_claimed",
            "rule_id": rule["id"],
            "request_id": _text(request_id),
            "duplicate": duplicate,
            "message": "正式领用批次并关联实际抽取行" + ("（失败后恢复）" if recovered else ""),
        })

        job = find_extract(entry_id=batch.get("extract_id"), batch_no=batch["批次号"])
        rows = ensure_extract_rows(job, payload) if job else []
        batch["行数"] = len(rows) or _int(batch.get("行数"), 0)
        batch["row_count"] = batch["行数"]
        return {
            "ok": True,
            "duplicate": duplicate,
            "claim_token": batch["claim_token"],
            "message": f"已领用 {batch['批次号']}，关联 {len(rows)} 行实际抽取数据",
            "batch": batch,
            "rule": rule,
            "rows": rows,
            "actual_rows": rows,
            "extract_rows": rows,
        }


def _continuation(batch: dict[str, Any], cleaned_count: int) -> dict[str, Any]:
    return {
        "continuable": True,
        "next_module": "dup_detect",
        "next_action": "派发重复检测",
        "batch_no": batch["批次号"],
        "entity_code": batch.get("实体编码", ""),
        "source": batch.get("源系统", ""),
        "cleaned_count": cleaned_count,
        "resume_url": f"/std_cleanse?batch={batch['批次号']}&record={batch.get('清洗记录号', '')}",
    }


def complete_batch(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    with FLOW_LOCK:
        batch, error = find_and_verify_batch(payload)
        if error:
            return error
        assert batch is not None

        existing = find_record(batch_no=batch["批次号"])
        if existing is not None and existing.get("status") == DONE:
            _remember_request(batch, "confirmation_ids", _pick(payload, REQUEST_ID_KEYS))
            return {
                "ok": True,
                "duplicate": True,
                "idempotent_replay": True,
                "code": "ALREADY_COMPLETED",
                "message": "同一 R1 批次已有一笔完整清洗确认，迟到结果未重复登记",
                "batch": batch,
                "record": existing,
            }

        request_id = _pick(payload, REQUEST_ID_KEYS)
        duplicate_confirmation = _remember_request(batch, "confirmation_ids", request_id)

        rule_id = batch.get("claimed_rule_id") or batch.get("available_rule_id") or _pick(payload, RULE_ID_KEYS)
        rules = eligible_rules(batch.get("实体编码"), rule_id)
        rule = rules[0] if rules else None
        if rule is None and rule_id:
            rule = next((item for item in _rows("std_cleanse") if _text(item.get("id")) == _text(rule_id)), None)
            if rule is not None and _text(rule.get("实体编码")) != _text(batch.get("实体编码")):
                return {"ok": False, "code": "ENTITY_MISMATCH", "message": "清洗规则实体与批次不一致"}
        if rule is None:
            rules = eligible_rules(batch.get("实体编码"))
            if not rules:
                return {"ok": False, "code": "NO_ENABLED_RULE", "message": "没有可用于完成清洗的确定规则", "batch": batch}
            rule = rules[0]

        job = find_extract(entry_id=batch.get("extract_id"), batch_no=batch["批次号"])
        rows = ensure_extract_rows(job, payload) if job else []
        row_count = _int(_pick(payload, ROW_COUNT_KEYS), default=len(rows) or _int(batch.get("行数"), 0))
        cleaned_value = _pick(payload, ("cleaned_count", "cleanedCount", "清洗后行数"))
        cleaned_rows_value = payload.get("cleaned_rows") or payload.get("cleanedRows")
        if isinstance(cleaned_rows_value, list):
            cleaned_count = len(cleaned_rows_value)
        elif cleaned_value not in (None, ""):
            cleaned_count = _int(cleaned_value, default=row_count)
        else:
            cleaned_count = row_count
        result_text = _text(payload.get("result") or payload.get("结果") or payload.get("清洗结果"))
        continuation_payload = payload.get("continuation") or payload.get("可继续结果")
        continuation = continuation_payload if isinstance(continuation_payload, dict) else _continuation(batch, cleaned_count)
        if not result_text:
            result_text = f"已按规则「{rule.get('规则名称', rule.get('id'))}」完整清洗 {cleaned_count}/{row_count} 行，可继续派发重复检测"

        record = existing or {
            "id": _next_id("cleanse_record"),
            "清洗记录号": f"CR-{batch['批次号']}",
            "record_no": f"CR-{batch['批次号']}",
            "批次号": batch["批次号"],
            "batch_no": batch["批次号"],
            "created_at": now_iso(),
        }
        record.update({
            "state": state_of(DONE),
            "status": DONE,
            "pending": False,
            "abnormal": False,
            "完整清洗确认": True,
            "confirmation_status": "已确认",
            "源系统": batch.get("源系统", ""),
            "source": batch.get("源系统", ""),
            "实体编码": batch.get("实体编码", ""),
            "entity_code": batch.get("实体编码", ""),
            "rule_id": rule.get("id"),
            "规则ID": rule.get("id"),
            "规则": rule.get("规则名称", ""),
            "规则名称": rule.get("规则名称", ""),
            "行数": row_count,
            "row_count": row_count,
            "清洗后行数": cleaned_count,
            "cleaned_count": cleaned_count,
            "结果": result_text,
            "continuation": continuation,
            "可继续结果": continuation,
            "confirmation_request_id": _text(request_id),
            "duplicate_confirmation": duplicate_confirmation,
            "completed_at": now_iso(),
            "updated_at": now_iso(),
        })
        record.setdefault("attempts", []).append({
            "at": now_iso(),
            "status": DONE,
            "rule_id": rule.get("id"),
            "row_count": row_count,
            "cleaned_count": cleaned_count,
            "request_id": _text(request_id),
        })
        if existing is None:
            _rows("cleanse_record").append(record)

        batch["status"] = DONE
        batch["state"] = state_of(DONE)
        batch["pending"] = False
        batch["abnormal"] = False
        batch["recoverable"] = False
        batch["行数"] = row_count
        batch["row_count"] = row_count
        batch["清洗记录号"] = record["清洗记录号"]
        batch["record_no"] = record["清洗记录号"]
        batch["结果"] = result_text
        batch["可继续结果"] = continuation
        batch["continuation"] = continuation
        batch["completed_at"] = now_iso()
        batch["updated_at"] = now_iso()
        batch.setdefault("history", []).append({
            "at": now_iso(),
            "event": "completed",
            "record_no": record["清洗记录号"],
            "row_count": row_count,
            "message": "登记唯一完整清洗确认",
        })

        if job is not None:
            job["清洗状态"] = DONE
            job["清洗记录号"] = record["清洗记录号"]
        rule["最近清洗批次"] = batch["批次号"]
        rule["最近清洗行数"] = row_count
        rule["最近清洗记录号"] = record["清洗记录号"]
        rule.setdefault("清洗记录号列表", [])
        if record["清洗记录号"] not in rule["清洗记录号列表"]:
            rule["清洗记录号列表"].append(record["清洗记录号"])

        return {
            "ok": True,
            "duplicate": False,
            "created": existing is None,
            "message": f"批次 {batch['批次号']} 清洗完成，已登记一笔完整确认",
            "batch": batch,
            "record": record,
            "rule": rule,
        }


def fail_batch(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    with FLOW_LOCK:
        batch, error = find_and_verify_batch(payload)
        if error:
            return error
        assert batch is not None
        if batch.get("status") == DONE:
            return {
                "ok": False,
                "code": "ALREADY_COMPLETED",
                "message": "批次已完成确认，迟到的失败结果不能覆盖完整清洗记录",
                "batch": batch,
                "record": find_record(batch_no=batch["批次号"]),
            }

        reason = _text(_pick(payload, REASON_KEYS)) or "清洗执行失败"
        rule_id = batch.get("claimed_rule_id") or batch.get("available_rule_id") or _pick(payload, RULE_ID_KEYS)
        rule = next((item for item in _rows("std_cleanse") if _text(item.get("id")) == _text(rule_id)), None)
        row_count = _int(_pick(payload, ROW_COUNT_KEYS), default=_int(batch.get("行数"), 0))
        continuation = {
            "continuable": True,
            "recover_action": "恢复批次并重新认领",
            "resume_batch": batch["批次号"],
            "last_rule_id": rule_id,
            "reason": reason,
        }

        record = find_record(batch_no=batch["批次号"]) or {
            "id": _next_id("cleanse_record"),
            "清洗记录号": f"CR-{batch['批次号']}",
            "record_no": f"CR-{batch['批次号']}",
            "批次号": batch["批次号"],
            "batch_no": batch["批次号"],
            "created_at": now_iso(),
        }
        record.update({
            "state": state_of(FAILED),
            "status": FAILED,
            "pending": True,
            "abnormal": True,
            "完整清洗确认": False,
            "源系统": batch.get("源系统", ""),
            "实体编码": batch.get("实体编码", ""),
            "rule_id": rule_id,
            "规则ID": rule_id,
            "规则名称": rule.get("规则名称", "") if rule else batch.get("规则名称", ""),
            "行数": row_count,
            "row_count": row_count,
            "失败原因": reason,
            "结果": f"清洗失败：{reason}；批次可恢复，原批次号 {batch['批次号']} 保留",
            "可继续结果": continuation,
            "continuation": continuation,
            "failed_at": now_iso(),
            "updated_at": now_iso(),
        })
        record.setdefault("attempts", []).append({
            "at": now_iso(),
            "status": FAILED,
            "rule_id": rule_id,
            "row_count": row_count,
            "reason": reason,
        })
        if "id" not in record:
            record["id"] = _next_id("cleanse_record")
        if not find_record(batch_no=batch["批次号"]):
            _rows("cleanse_record").append(record)

        batch["status"] = FAILED
        batch["state"] = state_of(FAILED)
        batch["pending"] = True
        batch["abnormal"] = True
        batch["recoverable"] = True
        batch["失败原因"] = reason
        batch["行数"] = row_count
        batch["row_count"] = row_count
        batch["清洗记录号"] = record["清洗记录号"]
        batch["可继续结果"] = continuation
        batch["暂停原因"] = f"清洗执行失败：{reason}。报错前的等待批次 {batch['批次号']} 仍可找回并恢复。"
        batch["updated_at"] = now_iso()
        batch.setdefault("history", []).append({
            "at": now_iso(),
            "event": "failed",
            "reason": reason,
            "message": "登记失败结果和可恢复续办信息",
        })
        return {"ok": False, "code": "CLEANSE_FAILED", "message": reason, "batch": batch, "record": record}


def recover_batch(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    with FLOW_LOCK:
        batch, error = find_and_verify_batch(payload)
        if error:
            return error
        assert batch is not None
        if batch.get("status") == DONE:
            return {"ok": True, "duplicate": True, "message": "批次已完成，无需恢复", "batch": batch,
                    "record": find_record(batch_no=batch["批次号"])}

        already_waiting = batch.get("status") == WAITING
        if batch.get("status") not in (FAILED, WAITING, PROCESSING):
            return {"ok": False, "message": f"批次状态 {batch.get('status')} 不允许恢复", "batch": batch}

        rules = eligible_rules(batch.get("实体编码"))
        rule = rules[0] if rules else None
        if not already_waiting:
            batch["recovery_count"] = _int(batch.get("recovery_count"), 0) + 1
        batch["status"] = WAITING
        batch["state"] = state_of(WAITING)
        batch["pending"] = True
        batch["abnormal"] = False
        batch["recoverable"] = False
        batch["claim_token"] = ""
        batch["claimable"] = rule is not None
        if rule:
            batch["available_rule_id"] = rule["id"]
            batch["规则ID"] = rule["id"]
            batch["规则名称"] = rule.get("规则名称", "")
            batch["暂停原因"] = f"已找回报错前的批次 {batch['批次号']}；规则已就绪，等待重新认领。"
        else:
            batch["暂停原因"] = _no_rule_reason(_text(batch.get("实体编码")))
        batch["updated_at"] = now_iso()
        batch.setdefault("history", []).append({
            "at": now_iso(),
            "event": "recovered",
            "message": "恢复等待清洗批次，批次号保持不变",
        })
        return {"ok": True, "duplicate": already_waiting, "message": f"等待清洗批次 {batch['批次号']} 已找回并恢复",
                "batch": batch}


def execute_batch(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """一键执行：领用 + 用实际抽取行跑确定性清洗 + 完成/失败登记。"""
    payload = dict(payload or {})
    claimed = claim_batch(payload)
    if not claimed.get("ok"):
        return claimed
    batch = claimed["batch"]
    rows = claimed.get("rows", [])
    rule = claimed.get("rule") or eligible_rules(batch.get("实体编码"))[0]

    fail_reason = _text(_pick(payload, REASON_KEYS))
    if payload.get("should_fail") or payload.get("simulate_failure") or payload.get("fail"):
        return fail_batch({
            **payload,
            "batch_no": batch["批次号"],
            "rule_id": rule.get("id") if rule else None,
            "row_count": len(rows),
            "reason": fail_reason or "规则执行异常（演练失败）",
        })

    expr = _text(rule.get("规则表达式") if rule else "")
    cleaned: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        raw = row.get("原始数据", {})
        values = {key: str(value).strip() for key, value in raw.items()} if isinstance(raw, dict) else {"value": str(raw)}
        # 实际行已有关联实体；只有完全没有实体信息的坏行才丢弃。
        if not row.get("实体编码") and not values.get("实体编码"):
            dropped += 1
            continue
        if "upper" in expr.lower() and values.get("实体编码"):
            values["实体编码"] = values["实体编码"].upper()
        cleaned.append(values)

    return complete_batch({
        **payload,
        "batch_no": batch["批次号"],
        "rule_id": rule.get("id") if rule else None,
        "row_count": len(rows),
        "cleaned_count": len(cleaned),
        "cleaned_rows": cleaned,
        "dropped": dropped,
    })


def get_workflow(*, entry_id: int | None = None, batch_no: str | None = None) -> dict[str, Any]:
    with FLOW_LOCK:
        job = find_extract(entry_id=entry_id, batch_no=batch_no)
        batch = find_batch(batch_no)
        if batch is None and job is not None:
            batch = find_batch(batch_no_of_extract(job))
        if batch_no and batch is None and job is None:
            return {"ok": False, "message": "找不到抽取作业或清洗批次"}
        record = find_record(batch_no=batch["批次号"]) if batch else None
        rows: list[dict[str, Any]] = []
        if batch:
            job_for_rows = job or {
                "id": batch.get("extract_id"),
                "源系统": batch.get("源系统"),
                "实体编码": batch.get("实体编码"),
                "作业名称": batch.get("抽取作业"),
            }
            rows = ensure_extract_rows(job_for_rows)  # type: ignore[arg-type]
        return {
            "ok": True,
            "job": job,
            "entry": job,
            "batch": batch,
            "record": record,
            "rule": next((r for r in _rows("std_cleanse") if batch and r.get("id") == batch.get("规则ID")), None),
            "rows": rows,
            "actual_rows": rows,
        }


def count_completed_records(batch_no: str) -> int:
    return sum(1 for record in _rows("cleanse_record")
               if _text(record.get("批次号")) == _text(batch_no) and record.get("status") == DONE)
