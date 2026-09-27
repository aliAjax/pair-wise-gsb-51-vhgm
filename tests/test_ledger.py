import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9, 'principal_outstanding': 300000.0}
SERVICER = Actor("clerk", "servicer")
REVIEWER = Actor("boss", "underwriter")
VIEWER = Actor("viewer", "underwriter")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _active_record(self, reference="MORT-LEDGER-1"):
        record = self.service.create(Actor("creator", "intake_officer"), reference, dict(CREATE_DATA))
        record = self.service.act(Actor("op", "intake_officer"), record["id"], record["version"], "assess", {"assessment_note": "收入波动"})
        record = self.service.act(Actor("op", "underwriter"), record["id"], record["version"], "approve", {"exception_approved": False})
        record = self.service.act(Actor("op", "servicer"), record["id"], record["version"], "activate", {"borrower_ack": True})
        return record

    def _entry(self, record_id, source="living_allowance", purpose="current_arrears", amount=5000.0, received_at="2026-09-01"):
        return self.service.register_entry(SERVICER, record_id, {"source": source, "purpose": purpose, "amount": amount, "received_at": received_at})

    def _initiate(self, record_id, entry_id, amount, target_period="2026-09"):
        data = {"entry_id": entry_id, "amount": amount}
        if target_period:
            data["target_period"] = target_period
        return self.service.initiate_deduction(SERVICER, record_id, data)

    def test_register_entry_validates_source_purpose(self):
        record = self._active_record()
        entry = self._entry(record["id"])
        self.assertEqual(entry["available_amount"], 5000.0)
        self.assertEqual(entry["received_at"], "2026-09-01")
        with self.assertRaises(ValidationError):
            self._entry(record["id"], source="disaster_payout", purpose="current_arrears")
        with self.assertRaises(ValidationError):
            self._entry(record["id"], source="living_allowance", purpose="principal_reduction")
        with self.assertRaises(ValidationError):
            self._entry(record["id"], received_at="2026-13-40")

    def test_ledger_requires_active_state(self):
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-LEDGER-0", dict(CREATE_DATA))
        with self.assertRaises(Conflict):
            self._entry(record["id"])

    def test_pending_deduction_changes_nothing_until_confirmed(self):
        record = self._active_record()
        entry = self._entry(record["id"], amount=5000.0)
        deduction = self._initiate(record["id"], entry["id"], 3000.0)
        self.assertEqual(deduction["status"], "pending")
        ledger = self.service.get_ledger(VIEWER, record["id"])
        self.assertEqual(ledger["summary"]["available_for_periods"], 5000.0)
        self.assertEqual(ledger["summary"]["arrears_remaining"], 12000.0)
        self.assertEqual(ledger["summary"]["pending_deductions"], 1)
        confirmed = self.service.confirm_deduction(REVIEWER, record["id"], deduction["id"], record["version"])
        self.assertEqual(confirmed["status"], "confirmed")
        ledger = self.service.get_ledger(VIEWER, record["id"])
        self.assertEqual(ledger["summary"]["available_for_periods"], 2000.0)
        self.assertEqual(ledger["summary"]["arrears_remaining"], 9000.0)
        self.assertEqual(ledger["summary"]["pending_deductions"], 0)
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(ledger["summary"]["last_deduction_at"], today)
        updated = self.service.get_record(VIEWER, record["id"])
        self.assertEqual(updated["payload"]["paid_periods"], ["2026-09"])
        self.assertEqual(updated["version"], record["version"] + 1)
        actions = [event["action"] for event in self.service.timeline(VIEWER, record["id"])]
        self.assertEqual(actions[-3:], ["fund_entry_registered", "deduction_initiated", "deduction_confirmed"])

    def test_same_money_cannot_be_used_twice(self):
        record = self._active_record()
        entry = self._entry(record["id"], amount=5000.0)
        first = self._initiate(record["id"], entry["id"], 3000.0)
        with self.assertRaises(Conflict):
            self._initiate(record["id"], entry["id"], 1000.0)
        self.service.confirm_deduction(REVIEWER, record["id"], first["id"], record["version"])
        with self.assertRaises(Conflict):
            self.service.confirm_deduction(REVIEWER, record["id"], first["id"], record["version"] + 1)
        other = self._entry(record["id"], source="insurance", amount=2000.0)
        with self.assertRaises(Conflict):
            self._initiate(record["id"], other["id"], 1000.0)

    def test_available_balance_checked_again_at_confirm(self):
        record = self._active_record()
        entry = self._entry(record["id"], amount=5000.0)
        first = self._initiate(record["id"], entry["id"], 4000.0, "2026-09")
        second = self._initiate(record["id"], entry["id"], 4000.0, "2026-10")
        self.service.confirm_deduction(REVIEWER, record["id"], first["id"], record["version"])
        with self.assertRaises(Conflict):
            self.service.confirm_deduction(REVIEWER, record["id"], second["id"], record["version"] + 1)
        with self.assertRaises(ValidationError):
            self._initiate(record["id"], entry["id"], 99999.0, "2026-11")

    def test_principal_reduction(self):
        record = self._active_record()
        entry = self._entry(record["id"], source="disaster_payout", purpose="principal_reduction", amount=50000.0)
        deduction = self._initiate(record["id"], entry["id"], 20000.0, target_period="")
        self.assertEqual(deduction["kind"], "principal_reduction")
        self.service.confirm_deduction(REVIEWER, record["id"], deduction["id"], record["version"])
        ledger = self.service.get_ledger(VIEWER, record["id"])
        self.assertEqual(ledger["summary"]["principal_outstanding"], 280000.0)
        self.assertEqual(ledger["summary"]["available_for_principal"], 30000.0)
        self.assertEqual(ledger["summary"]["arrears_remaining"], 12000.0)

    def test_gap_reflects_next_period_shortfall(self):
        record = self._active_record()
        ledger = self.service.get_ledger(VIEWER, record["id"])
        self.assertEqual(ledger["summary"]["next_period_due"], 3400.0)
        self.assertEqual(ledger["summary"]["gap"], 3400.0)
        self.assertFalse(ledger["summary"]["sufficient"])
        self.assertEqual(ledger["summary"]["last_deduction_at"], "")
        self._entry(record["id"], source="household_payment", amount=1000.0)
        self._entry(record["id"], source="disaster_payout", purpose="principal_reduction", amount=90000.0)
        ledger = self.service.get_ledger(VIEWER, record["id"])
        self.assertEqual(ledger["summary"]["gap"], 2400.0)
        self._entry(record["id"], source="insurance", amount=2400.0)
        ledger = self.service.get_ledger(VIEWER, record["id"])
        self.assertEqual(ledger["summary"]["gap"], 0.0)
        self.assertTrue(ledger["summary"]["sufficient"])

    def test_permissions_and_self_approval(self):
        record = self._active_record()
        with self.assertRaises(PermissionDenied):
            self.service.register_entry(REVIEWER, record["id"], {"source": "insurance", "purpose": "current_arrears", "amount": 100.0, "received_at": "2026-09-01"})
        entry = self._entry(record["id"], amount=2000.0)
        deduction = self._initiate(record["id"], entry["id"], 1000.0)
        with self.assertRaises(PermissionDenied):
            self.service.confirm_deduction(Actor("other", "servicer"), record["id"], deduction["id"], record["version"])
        with self.assertRaises(PermissionDenied):
            self.service.get_ledger(Actor("x", "outsider"), record["id"])
        own = self.service.initiate_deduction(Actor("root", "admin"), record["id"], {"entry_id": entry["id"], "amount": 500.0, "target_period": "2026-10"})
        with self.assertRaises(PermissionDenied):
            self.service.confirm_deduction(Actor("root", "admin"), record["id"], own["id"], record["version"])

    def test_reject_releases_period(self):
        record = self._active_record()
        entry = self._entry(record["id"], amount=3000.0)
        deduction = self._initiate(record["id"], entry["id"], 1500.0)
        rejected = self.service.reject_deduction(REVIEWER, record["id"], deduction["id"], "金额有误")
        self.assertEqual(rejected["status"], "rejected")
        ledger = self.service.get_ledger(VIEWER, record["id"])
        self.assertEqual(ledger["summary"]["available_for_periods"], 3000.0)
        self.assertEqual(ledger["summary"]["pending_deductions"], 0)
        again = self._initiate(record["id"], entry["id"], 1500.0)
        self.assertEqual(again["status"], "pending")
        with self.assertRaises(Conflict):
            self.service.reject_deduction(REVIEWER, record["id"], deduction["id"], "重复驳回")

    def test_confirm_version_conflict(self):
        record = self._active_record()
        entry = self._entry(record["id"], amount=3000.0)
        deduction = self._initiate(record["id"], entry["id"], 1000.0)
        with self.assertRaises(Conflict):
            self.service.confirm_deduction(REVIEWER, record["id"], deduction["id"], record["version"] + 5)
