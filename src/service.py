"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .ledger import LedgerRules
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, ledger: LedgerRules = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.ledger = ledger or LedgerRules()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def register_entry(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.ledger.role_can_entry(actor.role):
            raise PermissionDenied("角色无权登记纾困入账")
        entry = self.ledger.validate_entry(data or {})
        created = self.repository.add_fund_entry(record_id, entry, actor.user_id, self.ledger.LEDGER_STATES, "贷款未进入纾困执行期，不能登记入账")
        return self.ledger.entry_view(created)

    def initiate_deduction(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.ledger.role_can_deduct(actor.role):
            raise PermissionDenied("角色无权发起划扣")
        deduction = self.ledger.validate_deduction(data or {})
        return self.repository.insert_deduction(record_id, deduction, actor.user_id, self.ledger.LEDGER_STATES, "贷款未进入纾困执行期，不能发起划扣")

    def confirm_deduction(self, actor: Actor, record_id: int, deduction_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.ledger.role_can_decide(actor.role):
            raise PermissionDenied("角色无权确认划扣")
        return self.repository.confirm_deduction(record_id, deduction_id, actor.user_id, self.ledger.plan_confirmation)

    def reject_deduction(self, actor: Actor, record_id: int, deduction_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.ledger.role_can_decide(actor.role):
            raise PermissionDenied("角色无权驳回划扣")
        reason = text(data or {}, "reason")
        return self.repository.reject_deduction(record_id, deduction_id, actor.user_id, reason)

    def ledger_detail(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        entries = self.repository.list_fund_entries(record_id)
        deductions = self.repository.list_deductions(record_id)
        return {
            "record_id": record["id"],
            "reference": record["reference"],
            "state": record["state"],
            "version": record["version"],
            "entries": [self.ledger.entry_view(entry) for entry in entries],
            "deductions": deductions,
            "summary": self.ledger.summarize(record, entries, deductions),
        }

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
