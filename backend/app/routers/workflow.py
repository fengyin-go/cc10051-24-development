"""抽取派发、清洗领用、结果回报和恢复的专用工作流接口。

通用 CRUD/动作接口仍然保留；这里提供语义更明确、可表达迟到结果和幂等续办的端点。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query

from app.services import cleanse_flow
from app.store import store

router = APIRouter(prefix="/api", tags=["抽取-清洗续办工作流"])


def _page(table: str, page: int, size: int) -> dict[str, Any]:
    rows = list(store.rows(table))
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start:start + size], "total": total, "page": page, "size": size}


def _filter_batches(
    *,
    source: str | None = None,
    entity: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    rows = list(store.rows("cleanse_batch"))
    if source:
        rows = [row for row in rows if source in str(row.get("源系统", "")) or source in str(row.get("source", ""))]
    if entity:
        rows = [row for row in rows if entity in str(row.get("实体编码", ""))]
    if status:
        if status in {"待清洗", "waiting", "WAITING"}:
            rows = [row for row in rows if row.get("state") == "waiting" or row.get("status") in {"等待清洗", "待清洗"}]
        else:
            rows = [row for row in rows if row.get("status") == status or row.get("state") == status]
    if keyword:
        needle = keyword.casefold()
        rows = [row for row in rows if needle in str(row.get("批次号", "")).casefold()
                or needle in str(row.get("抽取作业", "")).casefold()]
    return sorted(rows, key=lambda row: int(row.get("id", 0) or 0), reverse=True)


# ---------- 抽取作业侧：完成、派发、重试、状态找回 ----------

@router.post("/record_extract/complete")
def complete_extract_body(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    payload = payload or {}
    return cleanse_flow.complete_extraction(payload.get("entry_id") or payload.get("id"), payload)


@router.post("/record_extract/{entry_id}/complete")
def complete_extract(entry_id: int, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return cleanse_flow.complete_extraction(entry_id, payload or {})


@router.post("/record_extract/dispatch")
@router.post("/record_extract/dispatch_cleanse")
@router.post("/record_extract/dispatch-cleanse")
def dispatch_extract_body(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    payload = payload or {}
    return cleanse_flow.dispatch_extract(payload.get("entry_id") or payload.get("id"), payload)


@router.post("/record_extract/{entry_id}/dispatch")
@router.post("/record_extract/{entry_id}/dispatch-cleanse")
@router.post("/record_extract/{entry_id}/dispatch_cleanse")
def dispatch_extract(entry_id: int, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return cleanse_flow.dispatch_extract(entry_id, payload or {})


@router.post("/record_extract/retry")
def retry_extract_body(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    payload = payload or {}
    return cleanse_flow.retry_extract(payload.get("entry_id") or payload.get("id"), payload)


@router.post("/record_extract/{entry_id}/retry")
@router.post("/record_extract/{entry_id}/continue")
def retry_extract(entry_id: int, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    # continue 与 retry 在本工作流中都按稳定批次号幂等续办。
    return cleanse_flow.retry_extract(entry_id, payload or {})


@router.get("/record_extract/workflow")
def extract_workflow_by_query(
    entry_id: int | None = Query(default=None),
    batch_no: str | None = Query(default=None),
) -> dict[str, Any]:
    return cleanse_flow.get_workflow(entry_id=entry_id, batch_no=batch_no)


@router.get("/record_extract/{entry_id}/workflow")
@router.get("/record_extract/{entry_id}/status")
def extract_workflow(entry_id: int) -> dict[str, Any]:
    return cleanse_flow.get_workflow(entry_id=entry_id)


# ---------- 标准化清洗侧：待清洗批次、领用、执行、结果、恢复 ----------

@router.get("/std_cleanse/batches")
@router.get("/std_cleanse/waiting-batches")
@router.get("/std_cleanse/waiting_batches")
@router.get("/cleanse/batches")
@router.get("/cleanse-batches")
def list_cleanse_batches(
    source: str | None = Query(default=None),
    entity: str | None = Query(default=None, alias="entity_code"),
    entity_zh: str | None = Query(default=None, alias="实体编码"),
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    rows = _filter_batches(source=source, entity=entity or entity_zh, status=status, keyword=keyword)
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start:start + size], "total": total, "page": page, "size": size}


@router.get("/std_cleanse/batches/{batch_no}")
@router.get("/cleanse/batches/{batch_no}")
def get_cleanse_batch(batch_no: str) -> dict[str, Any]:
    result = cleanse_flow.get_workflow(batch_no=batch_no)
    if not result.get("batch"):
        result["ok"] = False
        result["message"] = f"等待清洗批次 {batch_no} 不存在"
    return result


@router.post("/std_cleanse/notify")
@router.post("/std_cleanse/notify-rule")
@router.post("/cleanse/notify")
def notify_rule(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    payload = payload or {}
    rule_id = payload.get("rule_id") or payload.get("id") or payload.get("规则ID")
    entity = payload.get("entity_code") or payload.get("实体编码")
    rule = store.find("std_cleanse", int(rule_id)) if rule_id not in (None, "") else None
    if rule is None and entity:
        rules = cleanse_flow.eligible_rules(entity)
        rule = rules[0] if rules else None
    if rule is None:
        return {"ok": False, "message": "找不到可通知的新启用规则"}
    result = cleanse_flow.notify_rule_enabled(rule)
    processed = []
    if payload.get("process") or payload.get("auto_claim") or payload.get("auto_execute"):
        for batch in result.get("batches", []):
            if batch.get("status") == cleanse_flow.WAITING and batch.get("claimable"):
                processed.append(cleanse_flow.execute_batch({"batch_no": batch.get("批次号")}))
    return {"ok": True, "message": "新规则通知已幂等登记", "processed": processed, **result}


@router.post("/std_cleanse/claim")
@router.post("/std_cleanse/claim_next")
@router.post("/std_cleanse/claim-next")
@router.post("/std_cleanse/acquire")
@router.post("/std_cleanse/lease")
@router.post("/std_cleanse/claim_batch")
@router.post("/std_cleanse/claim-batch")
@router.post("/std_cleanse/batches/claim")
@router.post("/cleanse/claim")
@router.post("/cleanse/claim_next")
@router.post("/cleanse/claim_batch")
def claim_cleanse(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return cleanse_flow.claim_batch(payload or {})


@router.post("/std_cleanse/batches/{batch_no}/claim")
@router.post("/std_cleanse/{batch_no}/claim")
def claim_cleanse_by_batch(batch_no: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    body = dict(payload or {})
    body.setdefault("batch_no", batch_no)
    return cleanse_flow.claim_batch(body)


@router.post("/std_cleanse/execute")
@router.post("/cleanse/execute")
def execute_cleanse(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return cleanse_flow.execute_batch(payload or {})


@router.post("/std_cleanse/batches/{batch_no}/execute")
@router.post("/std_cleanse/{batch_no}/execute")
def execute_cleanse_by_batch(batch_no: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    body = dict(payload or {})
    body.setdefault("batch_no", batch_no)
    return cleanse_flow.execute_batch(body)


@router.post("/std_cleanse/complete")
@router.post("/std_cleanse/complete_batch")
@router.post("/std_cleanse/complete-batch")
@router.post("/std_cleanse/confirm")
@router.post("/std_cleanse/result")
@router.post("/std_cleanse/batches/complete")
@router.post("/cleanse/complete")
def complete_cleanse(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return cleanse_flow.complete_batch(payload or {})


@router.post("/std_cleanse/batches/{batch_no}/complete")
@router.post("/std_cleanse/{batch_no}/complete")
def complete_cleanse_by_batch(batch_no: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    body = dict(payload or {})
    body.setdefault("batch_no", batch_no)
    return cleanse_flow.complete_batch(body)


def _is_failure_result(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("state") or "").lower()
    if status in {"清洗失败", "failed", "failure", "error"}:
        return True
    if payload.get("ok") is False or payload.get("success") is False:
        return True
    return bool(payload.get("error") or payload.get("failure_reason") or payload.get("失败原因"))


@router.post("/std_cleanse/dispatch-result")
@router.post("/std_cleanse/dispatch_result")
@router.post("/record_extract/dispatch-result")
@router.post("/record_extract/dispatch_result")
@router.post("/dispatch-result")
def receive_dispatch_result(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    body = payload or {}
    # 迟到的派发结果可能直接回报成功/失败；按同一批次号 upsert，不另开批次。
    return cleanse_flow.fail_batch(body) if _is_failure_result(body) else cleanse_flow.complete_batch(body)


@router.post("/std_cleanse/fail")
@router.post("/std_cleanse/failure")
@router.post("/std_cleanse/report-failure")
@router.post("/std_cleanse/batches/fail")
@router.post("/cleanse/fail")
def fail_cleanse(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return cleanse_flow.fail_batch(payload or {})


@router.post("/std_cleanse/batches/{batch_no}/fail")
@router.post("/std_cleanse/{batch_no}/fail")
def fail_cleanse_by_batch(batch_no: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    body = dict(payload or {})
    body.setdefault("batch_no", batch_no)
    return cleanse_flow.fail_batch(body)


@router.post("/std_cleanse/recover")
@router.post("/std_cleanse/recover_batch")
@router.post("/std_cleanse/recover-batch")
@router.post("/std_cleanse/restore")
@router.post("/std_cleanse/batches/recover")
@router.post("/cleanse/recover")
def recover_cleanse(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return cleanse_flow.recover_batch(payload or {})


@router.post("/std_cleanse/batches/{batch_no}/recover")
@router.post("/std_cleanse/{batch_no}/recover")
def recover_cleanse_by_batch(batch_no: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    body = dict(payload or {})
    body.setdefault("batch_no", batch_no)
    return cleanse_flow.recover_batch(body)


@router.get("/std_cleanse/records")
@router.get("/cleanse/records")
def list_cleanse_records(page: int = Query(default=1, ge=1), size: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    return _page("cleanse_record", page, size)


@router.get("/std_cleanse/records/{record_no}")
@router.get("/cleanse/records/{record_no}")
def get_cleanse_record(record_no: str) -> dict[str, Any]:
    record = cleanse_flow.find_record(record_no=record_no)
    if record is None:
        return {"ok": False, "message": f"清洗记录 {record_no} 不存在"}
    return {"ok": True, "record": record}
