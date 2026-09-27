"""涉案卷宗：按发生时间印证证据，合并扫码来源，约束处置决定。

对应规则：
- 证书暂停、恢复、换绑只追加事件，早先的市场事实按当时状态评价；
- 批量扫码与跨地区补报按件合并来源，同一件货不重复计入；
- 证据按发生时间互相印证（生产、入库、供货、销售、抽样的先后）；
- 评估结论区分包装返工失误与多型号共用证书的系统性冒用；
- 门店只拿到自己应下架与通知的范围；
- 企业可补交整改材料，执法材料只增不删；
- 处罚或解除措施必须指向具体实物、证书版本、流向与消费者触达结果。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CERT_EVENT_KINDS = ("issued", "suspended", "restored", "rebound")
EVIDENCE_KINDS = ("warehousing", "sampling_photo", "interview")
DISPOSITION_KINDS = ("penalty", "lift-measures")


def _parse_time(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"时间必须带时区: {text}")
    return moment


@dataclass(frozen=True)
class CertificateEvent:
    """证书生命周期事件；issued/rebound 携带当时的有效产品范围。"""

    cert_id: str
    at: datetime
    kind: str
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class Batch:
    batch: str
    model: str
    packaging_version: str
    printed_cert: str
    produced_at: datetime
    quantity: int


@dataclass(frozen=True)
class Flow:
    ref: str
    batch: str
    to_store: str
    quantity: int
    shipped_at: datetime
    received_at: datetime


@dataclass(frozen=True)
class Sale:
    store: str
    batch: str
    quantity: int
    started_at: datetime
    ended_at: datetime


@dataclass(frozen=True)
class Evidence:
    kind: str
    at: datetime
    ref: str
    batch: str | None = None
    store: str | None = None
    note: str = ""


@dataclass(frozen=True)
class Scan:
    unit: str
    batch: str
    scanned_cert: str
    at: datetime
    source: str


@dataclass(frozen=True)
class Rectification:
    submitted_by: str
    at: datetime
    note: str
    attachments: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConsumerReach:
    store: str
    batch: str
    reached: int
    at: datetime


@dataclass(frozen=True)
class Disposition:
    """处罚或解除措施；四类指向缺一不可。"""

    kind: str
    batches: tuple[str, ...]
    cert_id: str
    cert_version: int
    flow_refs: tuple[str, ...]
    reach: tuple[ConsumerReach, ...]
    decided_at: datetime


@dataclass(frozen=True)
class MergedUnit:
    """同一件货在多个来源下的合并视图。"""

    unit: str
    batch: str
    scanned_cert: str
    sources: frozenset[str]
    first_seen: datetime
    sightings: int


@dataclass(frozen=True)
class Assessment:
    kind: str
    models: tuple[str, ...]
    packaging_versions: tuple[str, ...]
    units: int
    reason: str


@dataclass(frozen=True)
class StoreScope:
    """单个门店应下架与应通知的范围。"""

    store: str
    batch: str
    received: int
    sold: int
    to_delist: int
    to_notify: int
    reached: int

    @property
    def outstanding(self) -> int:
        return self.to_notify - self.reached


@dataclass
class CaseFile:
    case_id: str
    certificates: dict[str, tuple[CertificateEvent, ...]]
    batches: dict[str, Batch]
    flows: list[Flow]
    sales: list[Sale]
    evidence: list[Evidence]
    scans: list[Scan]
    rectifications: list[Rectification] = field(default_factory=list)
    notices: list[ConsumerReach] = field(default_factory=list)
    dispositions: list[Disposition] = field(default_factory=list)

    # ---- 证书生命周期：只追加事件，历史按当时状态评价 ----

    def certificate_status_at(self, cert_id: str, when: datetime) -> tuple[bool, frozenset[str]]:
        """证书在 when 时刻是否有效及其有效产品范围；只回看当时及之前的事件。"""
        active = False
        models: frozenset[str] = frozenset()
        for event in sorted(self.certificates.get(cert_id, ()), key=lambda e: e.at):
            if event.at > when:
                break
            if event.kind == "issued":
                active, models = True, frozenset(event.models)
            elif event.kind == "suspended":
                active = False
            elif event.kind == "restored":
                active = True
            elif event.kind == "rebound":
                models = frozenset(event.models)
        return active, models

    def certificate_version_at(self, cert_id: str, when: datetime) -> int:
        """证书在 when 时刻的版本号：issued 为第 1 版，每次 rebound 递增。"""
        version = 0
        for event in sorted(self.certificates.get(cert_id, ()), key=lambda e: e.at):
            if event.at > when:
                break
            if event.kind in ("issued", "rebound"):
                version += 1
        return version

    # ---- 扫码合并：同一件货只计一次，来源取并集 ----

    def merged_scans(self) -> dict[str, MergedUnit]:
        merged: dict[str, MergedUnit] = {}
        for scan in sorted(self.scans, key=lambda s: s.at):
            current = merged.get(scan.unit)
            if current is None:
                merged[scan.unit] = MergedUnit(
                    scan.unit, scan.batch, scan.scanned_cert, frozenset({scan.source}), scan.at, 1
                )
                continue
            if current.batch != scan.batch or current.scanned_cert != scan.scanned_cert:
                raise ValueError(f"同一货物的扫码记录互相冲突: {scan.unit}")
            merged[scan.unit] = MergedUnit(
                current.unit,
                current.batch,
                current.scanned_cert,
                current.sources | {scan.source},
                current.first_seen,
                current.sightings + 1,
            )
        return merged

    # ---- 冒用评估 ----

    def _mismatch_details(self) -> list[tuple[MergedUnit, Batch]]:
        details = []
        for unit in self.merged_scans().values():
            batch = self.batches.get(unit.batch)
            if batch is None:
                raise ValueError(f"扫码指向未知批次: {unit.batch}")
            _, models = self.certificate_status_at(unit.scanned_cert, unit.first_seen)
            if batch.model not in models:
                details.append((unit, batch))
        return details

    def mismatched_batches(self) -> set[str]:
        return {batch.batch for _, batch in self._mismatch_details()}

    def assess(self) -> Assessment:
        """区分包装返工失误与多型号共用证书的系统性冒用。"""
        details = self._mismatch_details()
        if not details:
            return Assessment("no-mismatch", (), (), 0, "扫码结果均在证书有效产品范围内")
        models = tuple(sorted({batch.model for _, batch in details}))
        versions = tuple(sorted({batch.packaging_version for _, batch in details}))
        certs = {unit.scanned_cert for unit, _ in details}
        units = len(details)
        if len(models) > 1 and len(certs) == 1:
            return Assessment(
                "systematic-misuse", models, versions, units, "多个型号共用同一证书编号，指向系统性冒用"
            )
        if len(models) == 1 and len(versions) == 1:
            return Assessment(
                "packaging-rework", models, versions, units, "问题集中于单一型号单一包装版本，指向包装返工失误"
            )
        return Assessment(
            "undetermined", models, versions, units, "现有证据不足以区分包装返工失误与系统性冒用"
        )

    def sales_blocked(self) -> bool:
        """评估未有结论或已确认冒用时不得继续销售，解除措施除外。"""
        if any(d.kind == "lift-measures" for d in self.dispositions):
            return False
        return self.assess().kind != "no-mismatch"

    # ---- 证据时间线互相印证 ----

    def verify_timeline(self) -> list[str]:
        problems: list[str] = []
        warehousing: dict[str, list[datetime]] = {}
        for item in self.evidence:
            if item.kind == "warehousing" and item.batch:
                warehousing.setdefault(item.batch, []).append(item.at)
        for batch in self.batches.values():
            for at in warehousing.get(batch.batch, []):
                if at < batch.produced_at:
                    problems.append(f"批次 {batch.batch} 入库记录早于生产时间")
        for flow in self.flows:
            batch = self.batches.get(flow.batch)
            if batch is None:
                problems.append(f"流向记录 {flow.ref} 指向未知批次 {flow.batch}")
                continue
            if flow.shipped_at < batch.produced_at:
                problems.append(f"批次 {flow.batch} 供货早于生产")
            if flow.received_at < flow.shipped_at:
                problems.append(f"批次 {flow.batch} 收货早于发货")
            if flow.batch in warehousing and flow.shipped_at < min(warehousing[flow.batch]):
                problems.append(f"批次 {flow.batch} 供货早于入库记录")
        for sale in self.sales:
            arrivals = [
                f.received_at
                for f in self.flows
                if f.batch == sale.batch and f.to_store == sale.store
            ]
            if not arrivals:
                problems.append(f"{sale.store} 销售 {sale.batch} 但无对应供货记录")
            elif sale.started_at < min(arrivals):
                problems.append(f"{sale.store} 销售 {sale.batch} 早于门店收货")
        for item in self.evidence:
            if item.kind == "sampling_photo" and item.batch in self.batches:
                if item.at < self.batches[item.batch].produced_at:
                    problems.append(f"抽样照片 {item.ref} 早于生产时间")
        return problems

    # ---- 门店范围：只含自己应下架与通知的数量 ----

    def store_scope(self, store: str) -> list[StoreScope]:
        affected = self.mismatched_batches()
        scopes = []
        for flow in self.flows:
            if flow.to_store != store or flow.batch not in affected:
                continue
            sold = sum(
                s.quantity for s in self.sales if s.store == store and s.batch == flow.batch
            )
            reached = sum(
                n.reached for n in self.notices if n.store == store and n.batch == flow.batch
            )
            scopes.append(
                StoreScope(
                    store, flow.batch, flow.quantity, sold, flow.quantity - sold, sold, reached
                )
            )
        return scopes

    # ---- 材料只增不删 ----

    def add_evidence(self, item: Evidence) -> None:
        self.evidence.append(item)

    def submit_rectification(self, item: Rectification) -> None:
        """企业可补交整改材料；只追加，不改写既有材料。"""
        self.rectifications.append(item)

    def remove_material(self, ref: str) -> None:
        """执法材料只增不删，企业补交整改也不能删除。"""
        raise PermissionError(f"执法材料不得删除: {ref}")

    # ---- 处置决定：必须指向具体实物、证书版本、流向与消费者触达结果 ----

    def issue_disposition(self, disp: Disposition) -> None:
        problems: list[str] = []
        if disp.kind not in DISPOSITION_KINDS:
            problems.append(f"未知处置类型: {disp.kind}")
        if not disp.batches:
            problems.append("处置必须指向具体实物批次")
        unknown = [b for b in disp.batches if b not in self.batches]
        if unknown:
            problems.append(f"处置指向未知批次: {sorted(unknown)}")
        if disp.cert_id not in self.certificates:
            problems.append(f"处置指向未知证书 {disp.cert_id}")
        elif disp.batches and not unknown:
            produced = min(self.batches[b].produced_at for b in disp.batches)
            expect = self.certificate_version_at(disp.cert_id, produced)
            if disp.cert_version != expect:
                problems.append(
                    f"证书版本与批次生产时不符：生产时为第 {expect} 版，处置写明第 {disp.cert_version} 版"
                )
        flows = {f.ref: f for f in self.flows}
        if not disp.flow_refs:
            problems.append("处置必须指向流向记录")
        for ref in disp.flow_refs:
            if ref not in flows:
                problems.append(f"未知流向记录 {ref}")
            elif flows[ref].batch not in disp.batches:
                problems.append(f"流向记录 {ref} 与处置批次不符")
        missing_flows = [f.ref for f in self.flows if f.batch in disp.batches and f.ref not in disp.flow_refs]
        if missing_flows:
            problems.append(f"处置未覆盖全部流向记录: {missing_flows}")
        if not disp.reach:
            problems.append("处置必须包含消费者触达结果")
        stores = {f.to_store for f in self.flows if f.batch in disp.batches}
        missing_stores = stores - {r.store for r in disp.reach}
        if missing_stores:
            problems.append(f"消费者触达结果缺少门店: {sorted(missing_stores)}")
        for reach in disp.reach:
            if reach.batch not in disp.batches:
                problems.append(f"触达结果指向处置外批次 {reach.batch}")
            sold = sum(
                s.quantity
                for s in self.sales
                if s.store == reach.store and s.batch == reach.batch
            )
            if reach.reached > sold:
                problems.append(f"{reach.store} 触达数量超过已售数量")
        if problems:
            raise ValueError("；".join(problems))
        self.dispositions.append(disp)


def load_case(path: Path) -> CaseFile:
    """读取卷宗并做基本校验；时间必须带时区，引用必须可解析。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"case_id", "certificates", "batches", "flows", "sales", "evidence", "scans"}
    if not isinstance(raw, dict) or not required.issubset(raw):
        raise ValueError("卷宗缺少必要字段")

    certificates: dict[str, tuple[CertificateEvent, ...]] = {}
    for entry in raw["certificates"]:
        events = []
        for item in entry["events"]:
            if item["kind"] not in CERT_EVENT_KINDS:
                raise ValueError(f"未知证书事件类型: {item['kind']}")
            events.append(
                CertificateEvent(
                    entry["cert_id"],
                    _parse_time(item["at"]),
                    item["kind"],
                    tuple(item.get("models", ())),
                )
            )
        certificates[entry["cert_id"]] = tuple(events)

    batches = {}
    for item in raw["batches"]:
        batch = Batch(
            item["batch"],
            item["model"],
            item["packaging_version"],
            item["printed_cert"],
            _parse_time(item["produced_at"]),
            int(item["quantity"]),
        )
        batches[batch.batch] = batch

    flows = [
        Flow(
            f["ref"],
            f["batch"],
            f["to_store"],
            int(f["quantity"]),
            _parse_time(f["shipped_at"]),
            _parse_time(f["received_at"]),
        )
        for f in raw["flows"]
    ]
    sales = [
        Sale(
            s["store"],
            s["batch"],
            int(s["quantity"]),
            _parse_time(s["started_at"]),
            _parse_time(s["ended_at"]),
        )
        for s in raw["sales"]
    ]
    evidence = []
    for item in raw["evidence"]:
        if item["kind"] not in EVIDENCE_KINDS:
            raise ValueError(f"未知证据类型: {item['kind']}")
        evidence.append(
            Evidence(
                item["kind"],
                _parse_time(item["at"]),
                item["ref"],
                item.get("batch"),
                item.get("store"),
                item.get("note", ""),
            )
        )
    scans = [
        Scan(s["unit"], s["batch"], s["scanned_cert"], _parse_time(s["at"]), s["source"])
        for s in raw["scans"]
    ]
    rectifications = [
        Rectification(
            r["submitted_by"],
            _parse_time(r["at"]),
            r["note"],
            tuple(r.get("attachments", ())),
        )
        for r in raw.get("rectifications", [])
    ]
    notices = [
        ConsumerReach(n["store"], n["batch"], int(n["reached"]), _parse_time(n["at"]))
        for n in raw.get("consumer_notices", [])
    ]

    case = CaseFile(
        raw["case_id"], certificates, batches, flows, sales, evidence, scans,
        rectifications, notices,
    )

    for scan in scans:
        if scan.batch not in batches:
            raise ValueError(f"扫码指向未知批次: {scan.batch}")
    for flow in flows:
        if flow.batch not in batches:
            raise ValueError(f"流向记录 {flow.ref} 指向未知批次 {flow.batch}")
    for sale in sales:
        if sale.batch not in batches:
            raise ValueError(f"销售记录指向未知批次 {sale.batch}")
    for notice in notices:
        if notice.batch not in batches:
            raise ValueError(f"通知记录指向未知批次 {notice.batch}")

    for item in raw.get("dispositions", []):
        disp = Disposition(
            item["kind"],
            tuple(item["batches"]),
            item["cert_id"],
            int(item["cert_version"]),
            tuple(item["flow_refs"]),
            tuple(
                ConsumerReach(r["store"], r["batch"], int(r["reached"]), _parse_time(r["at"]))
                for r in item["reach"]
            ),
            _parse_time(item["decided_at"]),
        )
        case.issue_disposition(disp)
    return case
