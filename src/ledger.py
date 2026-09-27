"""纾困资金台账规则：入账登记、来源用途约束、划扣分配与缺口汇总。"""
import re
from datetime import datetime
from typing import Any, Dict, List

from .domain import Conflict, ValidationError, choice, number, optional_text, text


class LedgerRules:
    SOURCES = ("living_allowance", "disaster_payout", "insurance", "family_repayment")
    PURPOSES = ("current_arrears", "principal_reduction")
    # 生活补助先补当期欠款，灾害赔付用于冲减本金：来源与用途固定绑定
    REQUIRED_PURPOSE = {"living_allowance": "current_arrears", "disaster_payout": "principal_reduction"}
    PURPOSE_MESSAGES = {"living_allowance": "生活补助只能用于补当期欠款", "disaster_payout": "灾害赔付只能用于冲减本金"}
    # 确认划扣时优先动用的来源，其余入账按到账日先后分配
    SOURCE_PRIORITY = {"current_arrears": {"living_allowance": 0}, "principal_reduction": {"disaster_payout": 0}}
    ENTRY_ROLES = {"servicer"}
    DEDUCT_ROLES = {"servicer"}
    DECIDE_ROLES = {"underwriter"}
    LEDGER_STATES = frozenset({"active"})
    DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")

    def role_can_entry(self, role: str) -> bool:
        return role == "admin" or role in self.ENTRY_ROLES

    def role_can_deduct(self, role: str) -> bool:
        return role == "admin" or role in self.DEDUCT_ROLES

    def role_can_decide(self, role: str) -> bool:
        return role == "admin" or role in self.DECIDE_ROLES

    @staticmethod
    def _check_format(value: str, pattern: Any, fmt: str, key: str) -> None:
        if not pattern.match(value):
            raise ValidationError("%s格式应为%s" % (key, fmt))
        try:
            datetime.strptime(value, fmt)
        except ValueError as exc:
            raise ValidationError("%s不是有效日期" % key) from exc

    @staticmethod
    def _positive_amount(data: Dict[str, Any]) -> float:
        amount = round(number(data, "amount", 0), 2)
        if amount <= 0:
            raise ValidationError("amount必须大于0")
        return amount

    def validate_entry(self, data: Dict[str, Any]) -> Dict[str, Any]:
        source = choice(data, "source", list(self.SOURCES))
        amount = self._positive_amount(data)
        arrived_at = text(data, "arrived_at")
        self._check_format(arrived_at, self.DATE_RE, "%Y-%m-%d", "arrived_at")
        purpose = optional_text(data, "purpose")
        required = self.REQUIRED_PURPOSE.get(source)
        if required is not None:
            if purpose and purpose != required:
                raise ValidationError(self.PURPOSE_MESSAGES[source])
            purpose = required
        else:
            if not purpose:
                raise ValidationError("purpose不能为空（current_arrears/principal_reduction）")
            if purpose not in self.PURPOSES:
                raise ValidationError("purpose只能是current_arrears/principal_reduction")
        return {"source": source, "purpose": purpose, "amount": amount, "arrived_at": arrived_at, "note": optional_text(data, "note")}

    def validate_deduction(self, data: Dict[str, Any]) -> Dict[str, Any]:
        target = choice(data, "target", list(self.PURPOSES))
        amount = self._positive_amount(data)
        period = optional_text(data, "period")
        if target == "current_arrears" and not period:
            raise ValidationError("period不能为空")
        if period:
            self._check_format(period, self.PERIOD_RE, "%Y-%m", "period")
        return {"target": target, "period": period, "amount": amount, "note": optional_text(data, "note")}

    def order_entries(self, entries: List[Dict[str, Any]], target: str) -> List[Dict[str, Any]]:
        priority = self.SOURCE_PRIORITY.get(target, {})
        return sorted(entries, key=lambda entry: (priority.get(entry["source"], 1), entry["arrived_at"], entry["id"]))

    def plan_allocation(self, entries: List[Dict[str, Any]], amount: float) -> List[Dict[str, Any]]:
        remaining = round(amount, 2)
        allocations: List[Dict[str, Any]] = []
        for entry in entries:
            available = round(float(entry["amount"]) - float(entry["used_amount"]), 2)
            if available <= 0:
                continue
            take = round(min(available, remaining), 2)
            allocations.append({"entry_id": entry["id"], "amount": take})
            remaining = round(remaining - take, 2)
            if remaining <= 0:
                break
        if remaining > 0:
            raise Conflict("台账可用余额不足，无法确认划扣")
        return allocations

    def plan_confirmation(self, record: Dict[str, Any], deduction: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        ordered = self.order_entries(entries, deduction["target"])
        allocations = self.plan_allocation(ordered, float(deduction["amount"]))
        payload_updates: Dict[str, Any] = {}
        if deduction["target"] == "current_arrears":
            arrears = float(record["payload"].get("arrears", 0) or 0)
            payload_updates["arrears"] = round(max(0.0, arrears - float(deduction["amount"])), 2)
        elif "principal_outstanding" in record["payload"]:
            principal = float(record["payload"].get("principal_outstanding") or 0)
            payload_updates["principal_outstanding"] = round(max(0.0, principal - float(deduction["amount"])), 2)
        return {"allocations": allocations, "payload": payload_updates}

    def entry_view(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(entry)
        item["available_amount"] = round(float(entry["amount"]) - float(entry["used_amount"]), 2)
        return item

    def summarize(self, record: Dict[str, Any], entries: List[Dict[str, Any]], deductions: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload = record["payload"]
        received_by_source: Dict[str, float] = {}
        available_by_purpose = {purpose: 0.0 for purpose in self.PURPOSES}
        total_received = 0.0
        total_used = 0.0
        for entry in entries:
            amount = float(entry["amount"])
            used = float(entry["used_amount"])
            total_received += amount
            total_used += used
            received_by_source[entry["source"]] = round(received_by_source.get(entry["source"], 0.0) + amount, 2)
            available_by_purpose[entry["purpose"]] = round(available_by_purpose[entry["purpose"]] + amount - used, 2)
        pending = [item for item in deductions if item["status"] == "pending"]
        confirmed = [item for item in deductions if item["status"] == "confirmed"]
        last_deduction_at = max((item["decided_at"] for item in confirmed if item["decided_at"]), default=None)
        next_due = float(payload.get("approved_payment", payload.get("monthly_payment", 0)) or 0)
        summary = {
            "total_received": round(total_received, 2),
            "total_used": round(total_used, 2),
            "total_available": round(total_received - total_used, 2),
            "received_by_source": received_by_source,
            "available_by_purpose": available_by_purpose,
            "pending_deductions": len(pending),
            "pending_amount": round(sum(float(item["amount"]) for item in pending), 2),
            "arrears_outstanding": round(float(payload.get("arrears", 0) or 0), 2),
            "next_installment_due": round(next_due, 2),
            "gap": round(max(0.0, next_due - available_by_purpose["current_arrears"]), 2),
            "last_deduction_at": last_deduction_at,
        }
        if "principal_outstanding" in payload:
            summary["principal_outstanding"] = round(float(payload["principal_outstanding"] or 0), 2)
        return summary
