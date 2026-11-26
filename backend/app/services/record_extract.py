"""记录抽取作业领域服务：管理按游标从源系统抽取主数据记录的作业调度与断点。

数据链路位置：主数据采集层，负责消费源系统登记与同步游标状态，
把抽取产物派发给标准化清洗模块。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services import cleanse_flow
from app.services.base import BaseModuleService
from app.store import store
from app.schemas import ActionPayload


class RecordExtractService(BaseModuleService):
    MODULE = "record_extract"
    ENTITY = "抽取作业"
    OPERATION_TERM = "抓取"
    LIST_FIELDS = ["作业名称", "源系统", "实体编码", "抽取范围", "游标位置"]
    REQUIRED_FIELDS = ["作业名称", "源系统", "实体编码"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "抽取失败"]
    ACTION_RULES = {"启动作业": "执行中", "重试抽取": "抽取失败"}
    NEGATIVE_ACTIONS = []

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        items, total = super().list_entries(**kwargs)
        for item in items:
            batch = cleanse_flow.find_batch(cleanse_flow.batch_no_of_extract(item))
            if batch is not None:
                item["workflow"] = {
                    "批次号": batch.get("批次号"),
                    "status": batch.get("status"),
                    "state": batch.get("state"),
                    "claimable": batch.get("claimable"),
                    "recoverable": batch.get("recoverable"),
                    "暂停原因": batch.get("暂停原因"),
                    "清洗记录号": batch.get("清洗记录号"),
                    "规则名称": batch.get("规则名称"),
                }
                item["清洗状态"] = batch.get("status")
                item["清洗批次号"] = batch.get("批次号")
                item["清洗记录号"] = batch.get("清洗记录号")
        return items, total

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is not None:
            # 保留派发/幂等测试可能直接登记的稳定批次号和行数等扩展字段。
            for key in ("批次号", "抽取批次号", "batch_no", "抽取行数", "记录数", "行数"):
                if values.get(key) not in (None, ""):
                    entry[key] = values[key]
        return entry, missing

    def run_action(
        self,
        entry_id: int,
        action: str,
        payload: ActionPayload | dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        raw_payload = self._payload_dict(payload)
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action in {"派发清洗", "派发", "继续办理", "续办", "派发并续办"}:
            result = cleanse_flow.dispatch_extract(entry_id, raw_payload)
            entry["workflow_result"] = self._action_data(result)
            return entry, str(result.get("message", "派发请求已处理"))

        if action == "完成抽取":
            result = cleanse_flow.complete_extraction(entry_id, raw_payload)
            entry["workflow_result"] = self._action_data(result)
            return entry, str(result.get("message", "抽取已完成并派发"))

        if action in {"恢复", "恢复批次", "找回批次", "恢复清洗"}:
            raw_payload.setdefault("batch_no", cleanse_flow.batch_no_of_extract(entry))
            result = cleanse_flow.recover_batch(raw_payload)
            entry["workflow_result"] = self._action_data(result)
            return entry, str(result.get("message", "批次恢复请求已处理"))

        # 已完成作业上的“重试抽取”是迟到的幂等续办/正式认领请求；
        # 未完成/失败作业仍保留通用状态流转骨架。
        if action == "重试抽取" and entry.get("status") == "已完成":
            result = cleanse_flow.retry_extract(entry_id, raw_payload)
            entry["workflow_result"] = self._action_data(result)
            return entry, str(result.get("message", "重试请求已处理"))

        return super().run_action(entry_id, action)

    @staticmethod
    def _action_data(result: dict[str, Any]) -> dict[str, Any]:
        """移除会与 entry 形成循环引用的完整作业对象。"""
        data = {key: value for key, value in result.items() if key not in {"entry", "job"}}
        nested = data.get("dispatch")
        if isinstance(nested, dict):
            data["dispatch"] = {key: value for key, value in nested.items() if key not in {"entry", "job"}}
        return data

    @staticmethod
    def _payload_dict(payload: ActionPayload | dict[str, Any] | None) -> dict[str, Any]:
        if payload is None:
            return {}
        if isinstance(payload, dict):
            return payload
        data = payload.model_dump(exclude_none=True)
        extra = getattr(payload, "model_extra", None) or {}
        data.update(extra)
        return data

    # ---- 业务扩展：记录抽取与清洗入链 ----

    def start_with_cursor(self, entry_id: int) -> dict[str, Any]:
        """启动抽取作业，按 sync_cursor 的游标值定位起始位置。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "抽取作业不存在"}
        sys_code = entry.get("源系统", "")
        cursor_row = next((c for c in store.rows("sync_cursor")
                           if c.get("源系统") == sys_code and c.get("status") == "正常"), None)
        cursor_value = str(cursor_row.get("游标值", "0")) if cursor_row else "0"
        entry["游标位置"] = cursor_value
        entry["status"] = "执行中"
        entry["pending"] = True
        return {"ok": True, "cursor": cursor_value}

    def complete_and_advance(
        self,
        entry_id: int,
        record_count: int = 0,
        rows: list[dict[str, Any]] | None = None,
        *,
        dispatch: bool = True,
    ) -> dict[str, Any]:
        """完成抽取、推进游标，并按同一批次号幂等派发给标准化清洗。"""
        payload: dict[str, Any] = {"record_count": record_count}
        if rows is not None:
            payload["rows"] = rows
        if dispatch:
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return {"ok": False, "message": "抽取作业不存在"}
            if entry.get("status") != "执行中":
                return {"ok": False, "message": "作业不在执行中状态"}
            result = cleanse_flow.complete_extraction(entry_id, payload)
            if not result.get("ok"):
                return result
            job = result["entry"]
            return {
                "ok": True,
                "records": record_count,
                "cursor": job.get("游标位置"),
                "batch": result.get("batch"),
                "dispatch": result,
            }

        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "抽取作业不存在"}
        entry["status"] = "已完成"
        entry["pending"] = False
        new_cursor = str(int(entry.get("游标位置", "0") or 0) + record_count)
        entry["游标位置"] = new_cursor
        for cur in store.rows("sync_cursor"):
            if cur.get("源系统") == entry.get("源系统"):
                cur["游标值"] = new_cursor
                cur["推进时间"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True, "records": record_count, "cursor": new_cursor}

    def fail_with_retry(self, entry_id: int, reason: str) -> dict[str, Any]:
        """标记抽取失败并写回游标回退标记。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "抽取作业不存在"}
        entry["status"] = "抽取失败"
        entry["abnormal"] = True
        for cur in store.rows("sync_cursor"):
            if cur.get("源系统") == entry.get("源系统"):
                cur["回退标记"] = "是"
                cur["status"] = "已回退"
        return {"ok": False, "message": f"抽取失败: {reason}", "retry_hint": "可执行重试抽取"}

    def dispatch_cleanse(self, entry_id: int) -> int:
        """把抽取结果登记为一个幂等等待清洗批次。

        历史实现按启用规则数量 fan-out；新约束要求一批抽取只由一条确定规则处理，
        因此这里最多登记一个批次，返回 1 表示批次已存在/已登记，0 表示作业不可派发。
        """
        result = cleanse_flow.dispatch_extract(entry_id)
        return 1 if result.get("ok") and result.get("batch") else 0

    def dispatch_cleanse_detail(self, entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """返回带批次、规则受理状态和记录的完整派发结果。"""
        return cleanse_flow.dispatch_extract(entry_id, payload or {})

    def continue_processing(self, entry_id: int, payload: dict[str, Any] | str | None = None) -> dict[str, Any]:
        """打开原作业续办或处理迟到重试：始终核对来源/实体并保持原批次号。"""
        if payload is None:
            body: dict[str, Any] = {}
        elif isinstance(payload, dict):
            body = payload
        else:
            body = {"reason": str(payload)}
        return cleanse_flow.retry_extract(entry_id, body)

    def retry_dispatch(self, entry_id: int, payload: dict[str, Any] | str | None = None) -> dict[str, Any]:
        return self.continue_processing(entry_id, payload)

    def retry(self, entry_id: int, payload: dict[str, Any] | str | None = None) -> dict[str, Any]:
        return self.continue_processing(entry_id, payload)

    def retry_extract(self, entry_id: int, payload: dict[str, Any] | str | None = None) -> dict[str, Any]:
        return self.continue_processing(entry_id, payload)

    def workflow_status(self, entry_id: int) -> dict[str, Any]:
        return cleanse_flow.get_workflow(entry_id=entry_id)

    def consume_source_config(self, entry_id: int) -> dict[str, Any] | None:
        """读取源系统登记信息，确认抽取目标可用。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        sys_code = entry.get("源系统", "")
        return next((s for s in store.rows("source_system")
                     if s.get("系统标识") == sys_code and s.get("status") == "已接入"), None)

    def batch_extract(self, sys_code: str, entity_code: str, ranges: list[str]) -> list[int]:
        """批量创建抽取作业，一次提交整组作业。"""
        jobs = store.rows(self.MODULE)
        created: list[int] = []
        for rng in ranges:
            job_id = max((int(r.get("id", 0)) for r in jobs), default=0) + 1
            jobs.append({
                "id": job_id,
                "作业名称": f"batch-{entity_code}-{job_id}",
                "源系统": sys_code,
                "实体编码": entity_code,
                "抽取范围": rng,
                "游标位置": "0",
                "status": "待执行",
                "pending": True,
                "abnormal": False,
            })
            created.append(job_id)
        return created
