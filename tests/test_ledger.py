import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {
    "monthly_income": 18000.0,
    "monthly_expenses": 9000.0,
    "monthly_payment": 7000.0,
    "arrears": 12000.0,
    "hardship_factor": 0.5,
    "program_type": "reduction",
    "requested_months": 9,
    "principal_outstanding": 200000.0,
}
FLOW = [
    ("assess", "intake_officer", {"assessment_note": "收入波动"}),
    ("approve", "underwriter", {"exception_approved": False}),
    ("activate", "servicer", {"borrower_ack": True}),
]
CREATOR = Actor("creator", "intake_officer")
SERVICER = Actor("specialist", "servicer")
APPROVER = Actor("approver", "underwriter")
VIEWER = Actor("viewer", "intake_officer")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def activate_record(self, reference="MORT-27001"):
        record = self.service.create(CREATOR, reference, CREATE_DATA)
        for action, role, data in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        return record

    def summary(self, record_id):
        return self.service.ledger_detail(VIEWER, record_id)["summary"]

    def test_entry_registration_and_gap(self):
        record = self.activate_record()
        entry = self.service.register_entry(SERVICER, record["id"], {"source": "living_allowance", "amount": 2000, "arrived_at": "2026-09-05"})
        self.assertEqual(entry["purpose"], "current_arrears")
        self.assertEqual(entry["available_amount"], 2000.0)
        summary = self.summary(record["id"])
        self.assertEqual(summary["total_received"], 2000.0)
        self.assertEqual(summary["total_available"], 2000.0)
        self.assertEqual(summary["available_by_purpose"]["current_arrears"], 2000.0)
        self.assertEqual(summary["next_installment_due"], 3400.0)
        self.assertEqual(summary["gap"], 1400.0)
        self.assertIsNone(summary["last_deduction_at"])

    def test_source_purpose_binding(self):
        record = self.activate_record()
        with self.assertRaises(ValidationError):
            self.service.register_entry(SERVICER, record["id"], {"source": "living_allowance", "amount": 100, "arrived_at": "2026-09-05", "purpose": "principal_reduction"})
        with self.assertRaises(ValidationError):
            self.service.register_entry(SERVICER, record["id"], {"source": "disaster_payout", "amount": 100, "arrived_at": "2026-09-05", "purpose": "current_arrears"})
        with self.assertRaises(ValidationError):
            self.service.register_entry(SERVICER, record["id"], {"source": "insurance", "amount": 100, "arrived_at": "2026-09-05"})
        disaster = self.service.register_entry(SERVICER, record["id"], {"source": "disaster_payout", "amount": 8000, "arrived_at": "2026-09-03"})
        self.assertEqual(disaster["purpose"], "principal_reduction")
        insurance = self.service.register_entry(SERVICER, record["id"], {"source": "insurance", "amount": 500, "arrived_at": "2026-09-04", "purpose": "current_arrears"})
        self.assertEqual(insurance["purpose"], "current_arrears")
        with self.assertRaises(ValidationError):
            self.service.register_entry(SERVICER, record["id"], {"source": "insurance", "amount": 100, "arrived_at": "2026-13-01", "purpose": "current_arrears"})

    def test_pending_deduction_keeps_balance_and_plan(self):
        record = self.activate_record()
        self.service.register_entry(SERVICER, record["id"], {"source": "living_allowance", "amount": 2000, "arrived_at": "2026-09-05"})
        deduction = self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 2000})
        self.assertEqual(deduction["status"], "pending")
        summary = self.summary(record["id"])
        self.assertEqual(summary["total_available"], 2000.0)
        self.assertEqual(summary["pending_amount"], 2000.0)
        self.assertEqual(summary["pending_deductions"], 1)
        self.assertEqual(summary["arrears_outstanding"], 12000.0)
        self.assertEqual(summary["gap"], 1400.0)
        self.assertIsNone(summary["last_deduction_at"])
        record = self.service.get_record(VIEWER, record["id"])
        self.assertEqual(record["payload"]["arrears"], 12000.0)

    def test_confirm_deduction_applies_allocation(self):
        record = self.activate_record()
        entry = self.service.register_entry(SERVICER, record["id"], {"source": "living_allowance", "amount": 2000, "arrived_at": "2026-09-05"})
        deduction = self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 2000})
        confirmed = self.service.confirm_deduction(APPROVER, record["id"], deduction["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["allocations"], [{"entry_id": entry["id"], "amount": 2000.0}])
        summary = self.summary(record["id"])
        self.assertEqual(summary["total_available"], 0.0)
        self.assertEqual(summary["total_used"], 2000.0)
        self.assertEqual(summary["arrears_outstanding"], 10000.0)
        self.assertEqual(summary["gap"], 3400.0)
        self.assertIsNotNone(summary["last_deduction_at"])
        record = self.service.get_record(VIEWER, record["id"])
        self.assertEqual(record["payload"]["arrears"], 10000.0)
        with self.assertRaises(Conflict):
            self.service.confirm_deduction(APPROVER, record["id"], deduction["id"])

    def test_double_spend_and_duplicate_period_rejected(self):
        record = self.activate_record()
        self.service.register_entry(SERVICER, record["id"], {"source": "family_repayment", "amount": 1500, "arrived_at": "2026-09-01", "purpose": "current_arrears"})
        first = self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 1500})
        with self.assertRaises(Conflict):
            self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 100})
        self.service.confirm_deduction(APPROVER, record["id"], first["id"])
        second = self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-10", "amount": 500})
        with self.assertRaises(Conflict):
            self.service.confirm_deduction(APPROVER, record["id"], second["id"])
        summary = self.summary(record["id"])
        self.assertEqual(summary["total_available"], 0.0)
        self.assertEqual(summary["pending_amount"], 500.0)
        detail = self.service.ledger_detail(VIEWER, record["id"])
        self.assertEqual(detail["entries"][0]["available_amount"], 0.0)

    def test_reject_keeps_balance_and_allows_retry(self):
        record = self.activate_record()
        self.service.register_entry(SERVICER, record["id"], {"source": "living_allowance", "amount": 3000, "arrived_at": "2026-09-05"})
        deduction = self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 3000})
        rejected = self.service.reject_deduction(APPROVER, record["id"], deduction["id"], {"reason": "金额存疑"})
        self.assertEqual(rejected["status"], "rejected")
        summary = self.summary(record["id"])
        self.assertEqual(summary["total_available"], 3000.0)
        self.assertEqual(summary["arrears_outstanding"], 12000.0)
        retry = self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 3000})
        confirmed = self.service.confirm_deduction(APPROVER, record["id"], retry["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(self.summary(record["id"])["total_available"], 0.0)

    def test_living_allowance_allocated_first(self):
        record = self.activate_record()
        family = self.service.register_entry(SERVICER, record["id"], {"source": "family_repayment", "amount": 3000, "arrived_at": "2026-08-15", "purpose": "current_arrears"})
        living = self.service.register_entry(SERVICER, record["id"], {"source": "living_allowance", "amount": 1000, "arrived_at": "2026-09-01"})
        deduction = self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 2000})
        confirmed = self.service.confirm_deduction(APPROVER, record["id"], deduction["id"])
        self.assertEqual(confirmed["allocations"], [{"entry_id": living["id"], "amount": 1000.0}, {"entry_id": family["id"], "amount": 1000.0}])
        detail = self.service.ledger_detail(VIEWER, record["id"])
        available = {entry["id"]: entry["available_amount"] for entry in detail["entries"]}
        self.assertEqual(available[living["id"]], 0.0)
        self.assertEqual(available[family["id"]], 2000.0)

    def test_disaster_payout_reduces_principal(self):
        record = self.activate_record()
        self.service.register_entry(SERVICER, record["id"], {"source": "disaster_payout", "amount": 8000, "arrived_at": "2026-09-03"})
        deduction = self.service.initiate_deduction(SERVICER, record["id"], {"target": "principal_reduction", "amount": 5000})
        confirmed = self.service.confirm_deduction(APPROVER, record["id"], deduction["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        summary = self.summary(record["id"])
        self.assertEqual(summary["principal_outstanding"], 195000.0)
        self.assertEqual(summary["arrears_outstanding"], 12000.0)
        self.assertEqual(summary["available_by_purpose"]["principal_reduction"], 3000.0)
        record = self.service.get_record(VIEWER, record["id"])
        self.assertEqual(record["payload"]["principal_outstanding"], 195000.0)

    def test_permissions(self):
        record = self.activate_record()
        with self.assertRaises(PermissionDenied):
            self.service.register_entry(CREATOR, record["id"], {"source": "living_allowance", "amount": 100, "arrived_at": "2026-09-05"})
        with self.assertRaises(PermissionDenied):
            self.service.initiate_deduction(APPROVER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 100})
        deduction = self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 100})
        with self.assertRaises(PermissionDenied):
            self.service.confirm_deduction(SERVICER, record["id"], deduction["id"])
        with self.assertRaises(PermissionDenied):
            self.service.reject_deduction(SERVICER, record["id"], deduction["id"], {"reason": "无权"})

    def test_ledger_requires_active_state(self):
        record = self.service.create(CREATOR, "MORT-27002", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.register_entry(SERVICER, record["id"], {"source": "living_allowance", "amount": 100, "arrived_at": "2026-09-05"})
        with self.assertRaises(Conflict):
            self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 100})

    def test_deduction_validation(self):
        record = self.activate_record()
        with self.assertRaises(ValidationError):
            self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "amount": 100})
        with self.assertRaises(ValidationError):
            self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-9", "amount": 100})
        with self.assertRaises(ValidationError):
            self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 0})

    def test_audit_trail_records_ledger_events(self):
        record = self.activate_record()
        self.service.register_entry(SERVICER, record["id"], {"source": "living_allowance", "amount": 2000, "arrived_at": "2026-09-05"})
        deduction = self.service.initiate_deduction(SERVICER, record["id"], {"target": "current_arrears", "period": "2026-09", "amount": 2000})
        self.service.confirm_deduction(APPROVER, record["id"], deduction["id"])
        actions = [event["action"] for event in self.service.timeline(VIEWER, record["id"])]
        self.assertIn("ledger_entry_registered", actions)
        self.assertIn("ledger_deduction_initiated", actions)
        self.assertIn("ledger_deduction_confirmed", actions)


if __name__ == "__main__":
    unittest.main()
