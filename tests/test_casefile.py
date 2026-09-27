import json
import tempfile
import unittest
from pathlib import Path

from src.casefile import (
    Batch,
    CaseFile,
    CertificateEvent,
    ConsumerReach,
    Disposition,
    Evidence,
    Rectification,
    Scan,
    load_case,
)

FIXTURE = Path("fixtures/case-012.json")


def at(text):
    from datetime import datetime

    return datetime.fromisoformat(text)


def make_case(batches, scans, cert_models=("RC-01 遥控车",)):
    events = (
        CertificateEvent("CCC-X", at("2026-01-01T09:00:00+08:00"), "issued", tuple(cert_models)),
    )
    return CaseFile(
        case_id="test",
        certificates={"CCC-X": events},
        batches={b.batch: b for b in batches},
        flows=[],
        sales=[],
        evidence=[],
        scans=list(scans),
    )


class FixtureCaseTest(unittest.TestCase):
    def setUp(self):
        self.case = load_case(FIXTURE)

    def test_timeline_cross_checks_pass(self):
        self.assertEqual(self.case.verify_timeline(), [])

    def test_batch_scan_and_region_report_merge_without_double_counting(self):
        merged = self.case.merged_scans()
        self.assertEqual(len(self.case.scans), 5)
        self.assertEqual(len(merged), 4)
        unit = merged["U-0001"]
        self.assertEqual(unit.sightings, 2)
        self.assertEqual(unit.sources, {"城东门店现场批量扫码", "城西跨地区补报"})
        self.assertEqual(sum(m.sightings for m in merged.values()), 5)

    def test_assessment_flags_systematic_misuse(self):
        result = self.case.assess()
        self.assertEqual(result.kind, "systematic-misuse")
        self.assertEqual(result.models, ("DB-01 积木恐龙", "DB-02 积木飞船"))
        self.assertEqual(result.units, 4)
        self.assertTrue(self.case.sales_blocked())

    def test_certificate_history_not_rewritten_by_later_events(self):
        produced = self.case.batches["B-20260312-01"].produced_at
        active, models = self.case.certificate_status_at("CCC-2026-7788A", produced)
        self.assertTrue(active)
        self.assertEqual(models, {"RC-01 遥控车"})
        self.assertEqual(self.case.certificate_version_at("CCC-2026-7788A", produced), 1)
        during = at("2026-05-10T09:00:00+08:00")
        active_during, _ = self.case.certificate_status_at("CCC-2026-7788A", during)
        self.assertFalse(active_during)
        later = at("2026-09-01T09:00:00+08:00")
        self.assertEqual(self.case.certificate_version_at("CCC-2026-7788A", later), 2)
        _, later_models = self.case.certificate_status_at("CCC-2026-7788A", later)
        self.assertEqual(later_models, {"RC-01 遥控车", "RC-02 遥控车"})
        # 暂停、恢复、换绑之后，生产时刻的范围与版本不变
        active_again, models_again = self.case.certificate_status_at("CCC-2026-7788A", produced)
        self.assertTrue(active_again)
        self.assertEqual(models_again, {"RC-01 遥控车"})

    def test_store_scope_limited_to_own_store(self):
        scope = {s.batch: s for s in self.case.store_scope("城东玩具店")}
        self.assertEqual(set(scope), {"B-20260312-01", "B-20260420-02"})
        first = scope["B-20260312-01"]
        self.assertEqual((first.received, first.sold), (60, 25))
        self.assertEqual(first.to_delist, 35)
        self.assertEqual(first.to_notify, 25)
        self.assertEqual(first.reached, 25)
        self.assertEqual(first.outstanding, 0)
        second = scope["B-20260420-02"]
        self.assertEqual(second.to_delist, 30)
        self.assertEqual(second.outstanding, 2)
        west = {s.batch: s for s in self.case.store_scope("城西玩具店")}
        self.assertEqual(set(west), {"B-20260312-01"})
        self.assertEqual(west["B-20260312-01"].received, 40)

    def test_rectification_append_only_and_materials_not_removable(self):
        before = list(self.case.evidence)
        self.case.submit_rectification(
            Rectification("玩具生产企业", at("2026-09-10T09:00:00+08:00"), "补充说明", ("RECT-02",))
        )
        self.assertEqual(self.case.evidence, before)
        self.assertEqual(len(self.case.rectifications), 2)
        # 企业补交整改后仍无权删除执法材料
        with self.assertRaises(PermissionError):
            self.case.remove_material("EV-SP-01")

    def test_fixture_disposition_on_record(self):
        self.assertEqual(len(self.case.dispositions), 1)
        disp = self.case.dispositions[0]
        self.assertEqual(disp.kind, "penalty")
        self.assertEqual(disp.cert_version, 1)

    def test_disposition_must_cite_version_in_force_at_production(self):
        disp = Disposition(
            kind="penalty",
            batches=("B-20260312-01",),
            cert_id="CCC-2026-7788A",
            cert_version=2,
            flow_refs=("F-01", "F-02"),
            reach=(
                ConsumerReach("城东玩具店", "B-20260312-01", 25, at("2026-08-20T17:00:00+08:00")),
                ConsumerReach("城西玩具店", "B-20260312-01", 10, at("2026-08-22T17:00:00+08:00")),
            ),
            decided_at=at("2026-09-10T10:00:00+08:00"),
        )
        with self.assertRaisesRegex(ValueError, "证书版本"):
            self.case.issue_disposition(disp)

    def test_disposition_requires_concrete_references(self):
        with self.assertRaisesRegex(ValueError, "具体实物批次"):
            self.case.issue_disposition(
                Disposition("penalty", (), "CCC-2026-7788A", 1, (), (), at("2026-09-10T10:00:00+08:00"))
            )
        missing_reach = Disposition(
            kind="penalty",
            batches=("B-20260312-01", "B-20260420-02"),
            cert_id="CCC-2026-7788A",
            cert_version=1,
            flow_refs=("F-01", "F-02", "F-03"),
            reach=(
                ConsumerReach("城东玩具店", "B-20260312-01", 25, at("2026-08-20T17:00:00+08:00")),
            ),
            decided_at=at("2026-09-10T10:00:00+08:00"),
        )
        with self.assertRaisesRegex(ValueError, "缺少门店"):
            self.case.issue_disposition(missing_reach)
        over_reach = Disposition(
            kind="penalty",
            batches=("B-20260312-01",),
            cert_id="CCC-2026-7788A",
            cert_version=1,
            flow_refs=("F-01", "F-02"),
            reach=(
                ConsumerReach("城东玩具店", "B-20260312-01", 25, at("2026-08-20T17:00:00+08:00")),
                ConsumerReach("城西玩具店", "B-20260312-01", 99, at("2026-08-22T17:00:00+08:00")),
            ),
            decided_at=at("2026-09-10T10:00:00+08:00"),
        )
        with self.assertRaisesRegex(ValueError, "超过已售数量"):
            self.case.issue_disposition(over_reach)

    def test_valid_lift_measures_accepted_and_unblocks_sales(self):
        disp = Disposition(
            kind="lift-measures",
            batches=("B-20260312-01", "B-20260420-02"),
            cert_id="CCC-2026-7788A",
            cert_version=1,
            flow_refs=("F-01", "F-02", "F-03"),
            reach=(
                ConsumerReach("城东玩具店", "B-20260312-01", 25, at("2026-08-20T17:00:00+08:00")),
                ConsumerReach("城西玩具店", "B-20260312-01", 10, at("2026-08-22T17:00:00+08:00")),
                ConsumerReach("城东玩具店", "B-20260420-02", 30, at("2026-08-20T17:30:00+08:00")),
            ),
            decided_at=at("2026-09-10T10:00:00+08:00"),
        )
        self.case.issue_disposition(disp)
        self.assertFalse(self.case.sales_blocked())


class AssessmentTest(unittest.TestCase):
    def test_single_model_single_packaging_version_is_rework(self):
        case = make_case(
            [Batch("LOT-A", "DB-01 积木恐龙", "PV-1", "CCC-X", at("2026-02-01T08:00:00+08:00"), 100)],
            [Scan("U-1", "LOT-A", "CCC-X", at("2026-03-01T10:00:00+08:00"), "门店批量扫码")],
        )
        self.assertEqual(case.assess().kind, "packaging-rework")

    def test_matching_scans_are_clean(self):
        case = make_case(
            [Batch("LOT-A", "RC-01 遥控车", "PV-1", "CCC-X", at("2026-02-01T08:00:00+08:00"), 100)],
            [Scan("U-1", "LOT-A", "CCC-X", at("2026-03-01T10:00:00+08:00"), "门店批量扫码")],
        )
        self.assertEqual(case.assess().kind, "no-mismatch")
        self.assertFalse(case.sales_blocked())

    def test_single_model_multiple_packaging_versions_undetermined(self):
        case = make_case(
            [
                Batch("LOT-A", "DB-01 积木恐龙", "PV-1", "CCC-X", at("2026-02-01T08:00:00+08:00"), 100),
                Batch("LOT-B", "DB-01 积木恐龙", "PV-2", "CCC-X", at("2026-02-02T08:00:00+08:00"), 100),
            ],
            [
                Scan("U-1", "LOT-A", "CCC-X", at("2026-03-01T10:00:00+08:00"), "门店批量扫码"),
                Scan("U-2", "LOT-B", "CCC-X", at("2026-03-01T11:00:00+08:00"), "跨地区补报"),
            ],
        )
        self.assertEqual(case.assess().kind, "undetermined")
        self.assertTrue(case.sales_blocked())

    def test_conflicting_scan_records_rejected(self):
        case = make_case(
            [Batch("LOT-A", "DB-01 积木恐龙", "PV-1", "CCC-X", at("2026-02-01T08:00:00+08:00"), 10)],
            [
                Scan("U-1", "LOT-A", "CCC-X", at("2026-03-01T10:00:00+08:00"), "门店批量扫码"),
                Scan("U-1", "LOT-A", "CCC-Y", at("2026-03-02T10:00:00+08:00"), "跨地区补报"),
            ],
        )
        with self.assertRaisesRegex(ValueError, "互相冲突"):
            case.merged_scans()

    def test_timeline_flags_out_of_order_records(self):
        batch = Batch("LOT-A", "RC-01 遥控车", "PV-1", "CCC-X", at("2026-02-10T08:00:00+08:00"), 10)
        case = make_case([batch], [])
        case.evidence.append(
            Evidence("warehousing", at("2026-02-09T15:00:00+08:00"), "WH-1", batch="LOT-A")
        )
        problems = case.verify_timeline()
        self.assertTrue(any("入库" in p for p in problems))


class LoadCaseTest(unittest.TestCase):
    def write_tmp(self, payload):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        with handle:
            json.dump(payload, handle, ensure_ascii=False)
        return Path(handle.name)

    def test_missing_fields_rejected(self):
        with self.assertRaisesRegex(ValueError, "缺少必要字段"):
            load_case(self.write_tmp({"case_id": "x"}))

    def test_naive_time_rejected(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["batches"][0]["produced_at"] = "2026-03-12T14:00:00"
        with self.assertRaisesRegex(ValueError, "时区"):
            load_case(self.write_tmp(payload))

    def test_unknown_scan_batch_rejected(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["scans"][0]["batch"] = "LOT-UNKNOWN"
        with self.assertRaisesRegex(ValueError, "未知批次"):
            load_case(self.write_tmp(payload))


if __name__ == "__main__":
    unittest.main()
