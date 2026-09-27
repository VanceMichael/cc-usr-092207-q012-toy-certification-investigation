"""读取并检查共享的领域资料。"""

import json
from datetime import date
from pathlib import Path

TIMELINE_KINDS = {
    "证书范围",
    "包装版本",
    "生产入库",
    "供货门店",
    "销售记录",
    "扫码上报",
    "抽样照片",
    "询问笔录",
    "跨地区补报",
    "整改补交",
    "处置决定",
}
REPORT_SOURCES = {"批量扫码", "跨地区补报"}
CERTIFICATE_STATUSES = {"有效", "暂停", "恢复", "换绑", "撤销"}
LOT_STATUSES = {"在库", "在架", "已售", "下架", "召回"}
DISPOSITION_KINDS = {"处罚", "解除措施"}


def load_domain(path: Path) -> dict:
    """返回字段完整且带版本的业务资料。"""
    return validate_domain(json.loads(path.read_text(encoding="utf-8")))


def validate_domain(value: dict) -> dict:
    """检查共享资料的结构与领域约束，返回原资料。"""
    required = {
        "domain",
        "version",
        "sample_id",
        "actors",
        "facts",
        "constraints",
        "certificates",
        "stores",
        "timeline",
        "lots",
        "reports",
        "certificate_status",
        "notices",
        "dispositions",
    }
    if not required.issubset(value):
        raise ValueError("共享资料缺少必要字段")
    if (
        value["version"] < 1
        or len(value["actors"]) < 2
        or len(value["facts"]) < 2
        or len(value["constraints"]) < 2
    ):
        raise ValueError("共享资料内容不完整")
    _check_parties(value)
    _check_certificates(value)
    _check_lots(value)
    _check_timeline(value)
    _check_reports(value)
    _check_certificate_status(value)
    _check_notices(value)
    _check_dispositions(value)
    return value


def merged_lot_ids(domain: dict) -> set:
    """合并各来源上报的批次，同一实物只计一次。"""
    merged = set()
    for report in domain["reports"]:
        merged.update(report["lot_ids"])
    return merged


def merged_quantity(domain: dict) -> int:
    """按合并后的批次统计实物数量，不重复计入。"""
    quantities = {lot["lot_id"]: lot["quantity"] for lot in domain["lots"]}
    return sum(quantities[lot_id] for lot_id in merged_lot_ids(domain))


def _check_parties(value: dict) -> None:
    actors = value["actors"]
    stores = value["stores"]
    if len(set(actors)) != len(actors) or not stores or len(set(stores)) != len(stores):
        raise ValueError("参与方或门店名单不完整")


def _check_certificates(value: dict) -> None:
    if not value["certificates"]:
        raise ValueError("证书记录不完整")
    seen = set()
    for cert in value["certificates"]:
        if not {"certificate_id", "version", "holder", "scope_models", "issued_at", "status"}.issubset(cert):
            raise ValueError("证书记录不完整")
        if (
            not cert["certificate_id"]
            or cert["certificate_id"] in seen
            or not cert["version"]
            or cert["holder"] not in value["actors"]
            or not cert["scope_models"]
            or not _is_date(cert["issued_at"])
            or cert["status"] not in CERTIFICATE_STATUSES
        ):
            raise ValueError("证书记录不完整")
        seen.add(cert["certificate_id"])


def _check_lots(value: dict) -> None:
    if not value["lots"]:
        raise ValueError("实物批次记录不完整")
    seen = set()
    for lot in value["lots"]:
        if not {"lot_id", "model", "packaging_version", "quantity", "holder", "status"}.issubset(lot):
            raise ValueError("实物批次记录不完整")
        if (
            not lot["lot_id"]
            or lot["lot_id"] in seen
            or not lot["model"]
            or not lot["packaging_version"]
            or not isinstance(lot["quantity"], int)
            or lot["quantity"] <= 0
            or lot["holder"] not in value["stores"]
            or lot["status"] not in LOT_STATUSES
        ):
            raise ValueError("实物批次记录不完整")
        seen.add(lot["lot_id"])


def _check_timeline(value: dict) -> None:
    if not value["timeline"]:
        raise ValueError("时间线事件记录不完整")
    lots = {lot["lot_id"] for lot in value["lots"]}
    events = {}
    for event in value["timeline"]:
        if not {"event_id", "occurred_at", "kind", "actor", "summary", "refs"}.issubset(event):
            raise ValueError("时间线事件记录不完整")
        event_id = event["event_id"]
        if (
            not event_id
            or event_id in events
            or not _is_date(event["occurred_at"])
            or event["kind"] not in TIMELINE_KINDS
            or event["actor"] not in value["actors"]
            or not event["summary"]
        ):
            raise ValueError("时间线事件记录不完整")
        if "quantity" in event and (not isinstance(event["quantity"], int) or event["quantity"] <= 0):
            raise ValueError("时间线事件数量无效")
        if not lots.issuperset(event.get("lot_ids", [])):
            raise ValueError("时间线事件引用了未知批次")
        events[event_id] = event
    for event in value["timeline"]:
        occurred = date.fromisoformat(event["occurred_at"])
        for ref in event["refs"]:
            if ref not in events:
                raise ValueError("时间线事件引用了未知事件")
            if date.fromisoformat(events[ref]["occurred_at"]) > occurred:
                raise ValueError("时间线事件引用了更晚发生的事件")


def _check_reports(value: dict) -> None:
    lots = {lot["lot_id"] for lot in value["lots"]}
    seen = set()
    for report in value["reports"]:
        if not {"report_id", "source", "reported_at", "lot_ids"}.issubset(report):
            raise ValueError("上报记录不完整")
        if (
            not report["report_id"]
            or report["report_id"] in seen
            or report["source"] not in REPORT_SOURCES
            or not _is_date(report["reported_at"])
            or not report["lot_ids"]
            or not lots.issuperset(report["lot_ids"])
        ):
            raise ValueError("上报记录不完整")
        seen.add(report["report_id"])


def _check_certificate_status(value: dict) -> None:
    certificates = {cert["certificate_id"] for cert in value["certificates"]}
    for record in value["certificate_status"]:
        if not {"certificate_id", "certificate_version", "changed_at", "status", "note"}.issubset(record):
            raise ValueError("证书状态记录不完整")
        if (
            record["certificate_id"] not in certificates
            or not record["certificate_version"]
            or not _is_date(record["changed_at"])
            or record["status"] not in CERTIFICATE_STATUSES
            or not record["note"]
        ):
            raise ValueError("证书状态记录不完整")


def _check_notices(value: dict) -> None:
    holders = {lot["lot_id"]: lot["holder"] for lot in value["lots"]}
    seen = set()
    for notice in value["notices"]:
        if not {"notice_id", "to", "sent_at", "action", "lot_ids"}.issubset(notice):
            raise ValueError("下架通知记录不完整")
        if (
            not notice["notice_id"]
            or notice["notice_id"] in seen
            or notice["to"] not in value["stores"]
            or not _is_date(notice["sent_at"])
            or not notice["action"]
            or not notice["lot_ids"]
        ):
            raise ValueError("下架通知记录不完整")
        seen.add(notice["notice_id"])
        for lot_id in notice["lot_ids"]:
            if lot_id not in holders:
                raise ValueError("下架通知引用了未知批次")
            if holders[lot_id] != notice["to"]:
                raise ValueError("下架通知超出门店自身范围")


def _check_dispositions(value: dict) -> None:
    lots = {lot["lot_id"] for lot in value["lots"]}
    events = {event["event_id"] for event in value["timeline"]}
    certificates = {cert["certificate_id"]: cert for cert in value["certificates"]}
    seen = set()
    for disposition in value["dispositions"]:
        if not {
            "disposition_id",
            "kind",
            "decided_at",
            "lot_ids",
            "certificate_id",
            "certificate_version",
            "flow_refs",
            "consumer_reach",
            "summary",
        }.issubset(disposition):
            raise ValueError("处置记录不完整")
        certificate_id = disposition["certificate_id"]
        if (
            not disposition["disposition_id"]
            or disposition["disposition_id"] in seen
            or disposition["kind"] not in DISPOSITION_KINDS
            or not _is_date(disposition["decided_at"])
            or not disposition["lot_ids"]
            or not lots.issuperset(disposition["lot_ids"])
            or certificate_id not in certificates
            or disposition["certificate_version"] != certificates[certificate_id]["version"]
            or not disposition["flow_refs"]
            or not events.issuperset(disposition["flow_refs"])
            or not disposition["consumer_reach"]
            or not disposition["summary"]
        ):
            raise ValueError("处置记录不完整")
        seen.add(disposition["disposition_id"])


def _is_date(text) -> bool:
    if not isinstance(text, str) or len(text) != 10 or text[4] != "-" or text[7] != "-":
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True
