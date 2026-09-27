import copy
import unittest
from pathlib import Path

from src.domain import load_domain, merged_lot_ids, merged_quantity, validate_domain

FIXTURE = Path("fixtures/domain.json")


class DomainTest(unittest.TestCase):
    def setUp(self):
        self.domain = load_domain(FIXTURE)

    def test_fixture_matches_domain(self):
        self.assertEqual(self.domain["domain"], "toy-certification-investigation")
        self.assertGreaterEqual(len(self.domain["constraints"]), 2)

    def test_timeline_cross_references_by_time(self):
        events = {event["event_id"]: event for event in self.domain["timeline"]}
        self.assertIn("evt-014", events)
        for event in self.domain["timeline"]:
            for ref in event["refs"]:
                self.assertLessEqual(events[ref]["occurred_at"], event["occurred_at"])

    def test_production_covers_supply(self):
        quantities = {event["event_id"]: event.get("quantity", 0) for event in self.domain["timeline"]}
        supplied = quantities["evt-005"] + quantities["evt-006"] + quantities["evt-007"]
        self.assertEqual(supplied, quantities["evt-004"])

    def test_reports_merge_without_double_count(self):
        naive = sum(len(report["lot_ids"]) for report in self.domain["reports"])
        self.assertGreater(naive, len(merged_lot_ids(self.domain)))
        self.assertEqual(merged_quantity(self.domain), 580)

    def test_certificate_status_kept_separate_from_timeline(self):
        kinds = {event["kind"] for event in self.domain["timeline"]}
        self.assertNotIn("证书状态", kinds)
        statuses = self.domain["certificate_status"]
        self.assertTrue(statuses)
        for record in statuses:
            self.assertEqual(record["certificate_id"], "C2026-0110")

    def test_notices_stay_within_store_scope(self):
        holders = {lot["lot_id"]: lot["holder"] for lot in self.domain["lots"]}
        for notice in self.domain["notices"]:
            for lot_id in notice["lot_ids"]:
                self.assertEqual(holders[lot_id], notice["to"])

    def test_disposition_points_to_concrete_targets(self):
        disposition = self.domain["dispositions"][0]
        self.assertEqual(disposition["certificate_version"], "V3")
        self.assertEqual(len(disposition["lot_ids"]), 4)
        self.assertTrue(disposition["flow_refs"])
        self.assertIn("96", disposition["consumer_reach"])

    def test_loader_rejects_unknown_timeline_ref(self):
        broken = copy.deepcopy(self.domain)
        broken["timeline"][0]["refs"] = ["evt-999"]
        with self.assertRaises(ValueError):
            validate_domain(broken)

    def test_loader_rejects_ref_to_later_event(self):
        broken = copy.deepcopy(self.domain)
        broken["timeline"][0]["refs"] = ["evt-014"]
        with self.assertRaises(ValueError):
            validate_domain(broken)

    def test_loader_rejects_notice_outside_store_scope(self):
        broken = copy.deepcopy(self.domain)
        broken["notices"][1]["lot_ids"].append("lot-a")
        with self.assertRaises(ValueError):
            validate_domain(broken)

    def test_loader_rejects_unknown_report_lot(self):
        broken = copy.deepcopy(self.domain)
        broken["reports"][0]["lot_ids"].append("lot-999")
        with self.assertRaises(ValueError):
            validate_domain(broken)

    def test_loader_rejects_disposition_without_consumer_reach(self):
        broken = copy.deepcopy(self.domain)
        broken["dispositions"][0]["consumer_reach"] = ""
        with self.assertRaises(ValueError):
            validate_domain(broken)

    def test_loader_rejects_disposition_with_wrong_certificate_version(self):
        broken = copy.deepcopy(self.domain)
        broken["dispositions"][0]["certificate_version"] = "V9"
        with self.assertRaises(ValueError):
            validate_domain(broken)


if __name__ == "__main__":
    unittest.main()
