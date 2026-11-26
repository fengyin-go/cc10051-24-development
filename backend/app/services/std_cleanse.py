"""标准化清洗领域服务：管理主数据记录的清洗规则、阶段编排、异常处置与检测派发。

数据链路位置：主数据加工层第一环，负责消费抽取作业产物，
把清洗后的数据集派发给重复检测与质量检查。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services import cleanse_flow
from app.services.base import BaseModuleService
from app.store import store


class StdCleanseService(BaseModuleService):
    MODULE = "std_cleanse"
    ENTITY = "清洗规则"
    OPERATION_TERM = "清洗"
    LIST_FIELDS = ["规则名称", "实体编码", "清洗阶段", "规则表达式", "异常处理"]
    REQUIRED_FIELDS = ["规则名称", "实体编码", "清洗阶段"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用规则": "已启用", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def run_action(self, entry_id: int, action: str, payload: Any | None = None):
        entry, message = super().run_action(entry_id, action, payload)
        if entry is not None and action == "启用规则":
            # 只发“新规则已启用”的幂等通知；批次仍需正式领用，避免通知/重试竞态产生多笔结果。
            cleanse_flow.notify_rule_enabled(entry)
        return entry, message

    # ---- 业务扩展：标准化清洗与检测派发 ----

    def apply_rule(self, entry_id: int, raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
        """按规则表达式清洗原始行，输出去空标准化后的记录。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已启用":
            return {"ok": False, "message": "清洗规则未启用", "cleaned": []}
        cleaned: list[dict[str, Any]] = []
        dropped = 0
        expr = str(entry.get("规则表达式", ""))
        for row in raw_rows:
            values = {k: str(v).strip() for k, v in row.items()}
            if not values.get("实体编码"):
                dropped += 1
                continue
            if "upper" in expr:
                values["实体编码"] = values["实体编码"].upper()
            cleaned.append(values)
        return {"ok": True, "cleaned": cleaned, "dropped": dropped}

    def enqueue_detection(self, entry_id: int) -> dict[str, Any]:
        """把清洗结果排入重复检测队列，写入 dup_detect。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清洗规则不存在"}
        batches = store.rows("dup_detect")
        batch_id = max((int(r.get("id", 0)) for r in batches), default=0) + 1
        batches.append({
            "id": batch_id,
            "检测批次": f"dup-{entry.get('实体编码')}-{batch_id}",
            "实体编码": entry.get("实体编码", ""),
            "候选组数": "0",
            "相似度算法": "编辑距离",
            "置信区间": "[0.85,1.0]",
            "status": "检测中",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "batch_id": batch_id}

    def handle_anomaly(self, entry_id: int, reason: str) -> str:
        """处理清洗异常：按异常处理策略转人工或忽略。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return "清洗规则不存在"
        strategy = str(entry.get("异常处理", "转人工"))
        if strategy == "转人工":
            reviews = store.rows("merge_review")
            review_id = max((int(r.get("id", 0)) for r in reviews), default=0) + 1
            reviews.append({
                "id": review_id,
                "记录编号": f"rv-{entry_id}-{review_id}",
                "候选组": "",
                "复核结论": "清洗异常",
                "复核角色": "数据管理员",
                "复核时间": datetime.now(timezone.utc).isoformat(),
                "status": "待复核",
                "pending": True,
                "abnormal": True,
            })
            return f"异常已转人工复核: {reason}"
        entry["abnormal"] = False
        return f"异常已按策略忽略: {reason}"

    def consume_extract_output(self, entry_id: int) -> list[str]:
        """读取抽取作业产物，聚合待清洗批次。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        entity_code = entry.get("实体编码", "")
        return [j.get("作业名称", "") for j in store.rows("record_extract")
                if j.get("实体编码") == entity_code and j.get("status") == "已完成"]

    def produce_quality_input(self, entry_id: int, cleaned_count: int) -> int:
        """把清洗结果作为质量检查输入，写入 quality_result。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return 0
        results = store.rows("quality_result")
        report_id = max((int(r.get("id", 0)) for r in results), default=0) + 1
        results.append({
            "id": report_id,
            "报告批次": f"qr-{entry.get('实体编码')}-{report_id}",
            "实体编码": entry.get("实体编码", ""),
            "检查总数": str(cleaned_count),
            "命中数": "0",
            "评分结果": "pending",
            "status": "生成中",
            "pending": True,
            "abnormal": False,
        })
        return report_id

    def batch_enable(self, entity_code: str) -> int:
        """批量启用某实体下的全部草稿清洗规则，并幂等通知等待批次。"""
        enabled_rules = []
        for rule in store.rows(self.MODULE):
            if rule.get("实体编码") == entity_code and rule.get("status") == "草稿":
                rule["status"] = "已启用"
                rule["pending"] = True
                enabled_rules.append(rule)
        for rule in enabled_rules:
            cleanse_flow.notify_rule_enabled(rule)
        return len(enabled_rules)

    # ---- 抽取批次领用/结果/恢复的服务入口（HTTP 与直接单测共用） ----

    @staticmethod
    def _workflow_body(payload: Any | None, kwargs: dict[str, Any]) -> dict[str, Any]:
        if payload is None:
            body: dict[str, Any] = {}
        elif isinstance(payload, dict):
            body = dict(payload)
        else:
            # 兼容 claim_batch("R1") 这类位置参数调用。
            body = {"batch_no": str(payload)}
        body.update(kwargs)
        return body

    def claim_batch(self, payload: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        return cleanse_flow.claim_batch(self._workflow_body(payload, kwargs))

    def claim(self, payload: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.claim_batch(payload, **kwargs)

    def lease_batch(self, payload: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.claim_batch(payload, **kwargs)

    def execute_batch(self, payload: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        return cleanse_flow.execute_batch(self._workflow_body(payload, kwargs))

    def complete_batch(self, payload: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        return cleanse_flow.complete_batch(self._workflow_body(payload, kwargs))

    def confirm_batch(self, payload: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.complete_batch(payload, **kwargs)

    def fail_batch(self, payload: Any | None = None, reason: str | None = None, **kwargs: Any) -> dict[str, Any]:
        body = self._workflow_body(payload, kwargs)
        if reason:
            body["reason"] = reason
        return cleanse_flow.fail_batch(body)

    def report_failure(self, payload: Any | None = None, reason: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.fail_batch(payload, reason=reason, **kwargs)

    def recover_batch(self, payload: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        return cleanse_flow.recover_batch(self._workflow_body(payload, kwargs))

    def restore_batch(self, payload: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.recover_batch(payload, **kwargs)

    def get_batch(self, batch_no: str) -> dict[str, Any] | None:
        return cleanse_flow.find_batch(batch_no)

    def list_batches(
        self,
        source: str | None = None,
        entity_code: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows("cleanse_batch")
        if source:
            rows = [row for row in rows if source in str(row.get("源系统", ""))]
        if entity_code:
            rows = [row for row in rows if row.get("实体编码") == entity_code]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows

    def list_waiting_batches(self, source: str | None = None, entity_code: str | None = None) -> list[dict[str, Any]]:
        return self.list_batches(source=source, entity_code=entity_code, status=cleanse_flow.WAITING)


class CleanseBatchService(BaseModuleService):
    """等待/处理中/失败可恢复的清洗批次资源。"""

    MODULE = "cleanse_batch"
    ENTITY = "待清洗批次"
    LIST_FIELDS = ["批次号", "抽取作业", "源系统", "实体编码", "行数", "status", "暂停原因", "规则名称", "清洗记录号"]
    REQUIRED_FIELDS = ["批次号", "源系统", "实体编码"]
    STATUS_ORDER = ["等待清洗", "清洗中", "已完成", "清洗失败"]
    ACTION_RULES = {"恢复": "等待清洗", "领用": "清洗中"}

    def list_entries(self, **kwargs: Any):
        if kwargs.get("status") == "待清洗":
            kwargs["status"] = cleanse_flow.WAITING
        return super().list_entries(**kwargs)

    def run_action(self, entry_id: int, action: str, payload: Any | None = None):
        batch = store.find(self.MODULE, entry_id)
        if batch is None:
            return None, f"待清洗批次 {entry_id} 不存在"
        body = payload if isinstance(payload, dict) else (
            payload.model_dump(exclude_none=True) if payload is not None and hasattr(payload, "model_dump") else {}
        )
        body = {**body, "batch_no": batch.get("批次号")}
        if action in {"恢复", "恢复批次", "找回"}:
            result = cleanse_flow.recover_batch(body)
            return (result.get("batch") if result.get("ok") else None), str(result.get("message"))
        if action in {"领用", "认领", "正式认领"}:
            result = cleanse_flow.claim_batch(body)
            return (result.get("batch") if result.get("ok") else None), str(result.get("message"))
        if action in {"领用并清洗", "执行清洗"}:
            result = cleanse_flow.execute_batch(body)
            return (result.get("batch") if result.get("ok") else None), str(result.get("message"))
        return super().run_action(entry_id, action, payload)


class CleanseRecordService(BaseModuleService):
    """清洗完成/失败后登记的规则、行数和可继续结果。"""

    MODULE = "cleanse_record"
    ENTITY = "清洗记录"
    LIST_FIELDS = ["清洗记录号", "批次号", "源系统", "实体编码", "规则名称", "行数", "清洗后行数", "status", "结果"]
    REQUIRED_FIELDS = ["批次号", "实体编码"]
    STATUS_ORDER = ["清洗中", "已完成", "清洗失败"]
    ACTION_RULES: dict[str, str] = {}
