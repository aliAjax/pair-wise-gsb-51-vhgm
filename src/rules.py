"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
import re
from datetime import date
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'assess': {'intake_officer'}, 'approve': {'underwriter'}, 'activate': {'servicer'}, 'cure': {'servicer'}, 'default': {'servicer'}}
TRANSITIONS = {'assess': {'submitted': 'assessed'}, 'approve': {'assessed': 'approved'}, 'activate': {'approved': 'active'}, 'cure': {'active': 'cured'}, 'default': {'active': 'defaulted'}}

# 纾困资金台账：来源、用途与角色
FUND_SOURCES = ["living_allowance", "disaster_payout", "insurance", "household_payment"]
FUND_PURPOSES = ["current_arrears", "principal_reduction"]
SOURCE_PURPOSES = {
    "living_allowance": {"current_arrears"},
    "disaster_payout": {"principal_reduction"},
    "insurance": {"current_arrears", "principal_reduction"},
    "household_payment": {"current_arrears"},
}
LEDGER_STATE = "active"
ENTRY_ROLES = {'servicer'}
DEDUCTION_INITIATE_ROLES = {'servicer'}
DEDUCTION_REVIEW_ROLES = {'underwriter'}
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        income = number(p, "monthly_income", 1)
        number(p, "monthly_expenses", 0)
        payment = number(p, "monthly_payment", 0)
        number(p, "arrears", 0)
        number(p, "hardship_factor", 0, 1)
        if p.get("principal_outstanding") is None:
            p["principal_outstanding"] = 0.0
        else:
            number(p, "principal_outstanding", 0)
        choice(p, "program_type", ["deferral", "reduction", "restructure"])
        integer(p, "requested_months", 1, 24)
        if p["monthly_expenses"] >= income:
            raise ValidationError("支出不能达到或超过收入")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        income = float(p["monthly_income"])
        disposable = income - float(p["monthly_expenses"])
        ratio = float(p["monthly_payment"]) / income
        months = min(int(p["requested_months"]), 12)
        if p["program_type"] == "deferral":
            proposed = 0.0
        elif p["program_type"] == "reduction":
            proposed = max(0.0, float(p["monthly_payment"]) - disposable * 0.4)
        else:
            proposed = max(float(p["monthly_payment"]) * 0.7, disposable * 0.25)
        p["disposable_income"] = round(disposable, 2)
        p["housing_ratio"] = round(ratio, 3)
        p["eligible_months"] = months
        p["proposed_payment"] = round(proposed, 2)
        p["risk_score"] = round(min(100.0, ratio * 60 + float(p["hardship_factor"]) * 40), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "approved", "assessed"} and item["payload"].get("borrower_id") == payload.get("borrower_id"):
                raise Conflict("该借款人已有处理中纾困申请")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "assess":
            changes["assessment_note"] = text(data, "assessment_note")
            changes["eligibility"] = bool(float(p["housing_ratio"]) <= 0.8 and float(p["arrears"]) <= float(p["monthly_payment"]) * 6)
            summary = "偿付能力评估完成"
        elif action == "approve":
            exception = boolean(data, "exception_approved")
            if not p.get("eligibility") and not exception:
                raise ValidationError("不符合纾困资格且无例外批准")
            changes["approved_program"] = p["program_type"]
            changes["approved_months"] = int(p["eligible_months"])
            changes["approved_payment"] = float(p["proposed_payment"])
            changes["exception_approved"] = exception
            summary = "纾困方案批准"
        elif action == "activate":
            if not boolean(data, "borrower_ack"):
                raise ValidationError("借款人尚未确认方案")
            changes["borrower_ack"] = True
            summary = "纾困方案生效"
        elif action == "cure":
            if not boolean(data, "arrears_cleared"):
                raise ValidationError("欠款尚未清偿")
            changes["arrears_cleared"] = True
            summary = "贷款恢复正常"
        elif action == "default":
            changes["default_reason"] = text(data, "default_reason")
            summary = "纾困方案违约"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def role_can_register_entry(self, role: str) -> bool:
        return role == "admin" or role in ENTRY_ROLES

    def role_can_initiate_deduction(self, role: str) -> bool:
        return role == "admin" or role in DEDUCTION_INITIATE_ROLES

    def role_can_review_deduction(self, role: str) -> bool:
        return role == "admin" or role in DEDUCTION_REVIEW_ROLES

    def require_ledger_open(self, record: Dict[str, Any]) -> None:
        if record["state"] != LEDGER_STATE:
            raise Conflict("当前状态不允许操作资金台账")

    def validate_fund_entry(self, data: Dict[str, Any]) -> Dict[str, Any]:
        source = choice(data, "source", FUND_SOURCES)
        purpose = choice(data, "purpose", FUND_PURPOSES)
        if purpose not in SOURCE_PURPOSES[source]:
            raise ValidationError("该来源资金不能指定此用途")
        amount = round(number(data, "amount", 0.01), 2)
        received_at = text(data, "received_at")
        if not DATE_RE.match(received_at):
            raise ValidationError("received_at格式应为YYYY-MM-DD")
        try:
            date.fromisoformat(received_at)
        except ValueError as exc:
            raise ValidationError("received_at不是有效日期") from exc
        return {"source": source, "purpose": purpose, "amount": amount, "received_at": received_at, "note": optional_text(data, "note")}

    def validate_deduction_request(self, data: Dict[str, Any]) -> Dict[str, Any]:
        entry_id = integer(data, "entry_id", 1)
        amount = round(number(data, "amount", 0.01), 2)
        target_period = optional_text(data, "target_period")
        if target_period and not PERIOD_RE.match(target_period):
            raise ValidationError("target_period格式应为YYYY-MM")
        return {"entry_id": entry_id, "amount": amount, "target_period": target_period}

    def check_deduction_request(self, record: Dict[str, Any], entry: Dict[str, Any], request: Dict[str, Any]) -> str:
        kind = entry["purpose"]
        payload = record["payload"]
        if request["amount"] > float(entry["available_amount"]) + 1e-9:
            raise ValidationError("划扣金额超过该笔入账可用金额")
        if kind == "current_arrears":
            if not request["target_period"]:
                raise ValidationError("补当期欠款必须指定target_period")
            if request["target_period"] in payload.get("paid_periods", []):
                raise Conflict("该期已扣款，不能重复划扣")
            if request["amount"] > float(payload.get("arrears", 0.0)) + 1e-9:
                raise ValidationError("划扣金额超过剩余欠款")
        elif request["amount"] > float(payload.get("principal_outstanding", 0.0)) + 1e-9:
            raise ValidationError("划扣金额超过剩余本金")
        return kind

    def apply_confirmed_deduction(self, payload: Dict[str, Any], deduction: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        amount = round(float(deduction["amount"]), 2)
        if deduction["kind"] == "current_arrears":
            period = deduction["target_period"]
            paid = list(p.get("paid_periods", []))
            if period in paid:
                raise Conflict("该期已扣款，不能重复划扣")
            arrears = round(float(p.get("arrears", 0.0)), 2)
            if amount > arrears + 1e-9:
                raise ValidationError("划扣金额超过剩余欠款")
            p["arrears"] = round(arrears - amount, 2)
            paid.append(period)
            p["paid_periods"] = sorted(paid)
        else:
            principal = round(float(p.get("principal_outstanding", 0.0)), 2)
            if amount > principal + 1e-9:
                raise ValidationError("划扣金额超过剩余本金")
            p["principal_outstanding"] = round(principal - amount, 2)
        return p

    def ledger_summary(self, payload: Dict[str, Any], entries: List[Dict[str, Any]], deductions: List[Dict[str, Any]]) -> Dict[str, Any]:
        next_due = round(float(payload.get("approved_payment") or payload.get("monthly_payment") or 0.0), 2)
        for_periods = round(sum(float(e["available_amount"]) for e in entries if e["purpose"] == "current_arrears"), 2)
        for_principal = round(sum(float(e["available_amount"]) for e in entries if e["purpose"] == "principal_reduction"), 2)
        confirmed = [d for d in deductions if d["status"] == "confirmed"]
        last = max((d["confirmed_at"] for d in confirmed), default="")
        gap = round(max(0.0, next_due - for_periods), 2)
        return {
            "next_period_due": next_due,
            "available_for_periods": for_periods,
            "available_for_principal": for_principal,
            "total_available": round(for_periods + for_principal, 2),
            "gap": gap,
            "sufficient": gap <= 0.0,
            "last_deduction_at": last[:10],
            "arrears_remaining": round(float(payload.get("arrears", 0.0)), 2),
            "principal_outstanding": round(float(payload.get("principal_outstanding", 0.0)), 2),
            "pending_deductions": sum(1 for d in deductions if d["status"] == "pending"),
        }
