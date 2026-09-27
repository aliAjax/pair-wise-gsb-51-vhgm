"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, optional_text, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

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

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    def register_entry(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_register_entry(actor.role):
            raise PermissionDenied("角色无权登记入账")
        record = self.repository.get(record_id)
        self.rules.require_ledger_open(record)
        entry = self.rules.validate_fund_entry(data or {})
        return self.repository.add_fund_entry(record_id, entry, actor.user_id)

    def initiate_deduction(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_initiate_deduction(actor.role):
            raise PermissionDenied("角色无权发起划扣")
        record = self.repository.get(record_id)
        self.rules.require_ledger_open(record)
        request = self.rules.validate_deduction_request(data or {})
        entry = self.repository.get_fund_entry(request["entry_id"])
        if int(entry["record_id"]) != int(record_id):
            raise NotFound("入账记录不存在")
        kind = self.rules.check_deduction_request(record, entry, request)
        return self.repository.initiate_deduction(record_id, entry["id"], kind, request["amount"], request["target_period"], actor.user_id)

    def confirm_deduction(self, actor: Actor, record_id: int, deduction_id: int, expected_version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review_deduction(actor.role):
            raise PermissionDenied("角色无权审批划扣")
        record = self.repository.get(record_id)
        self.rules.require_ledger_open(record)
        deduction = self.repository.get_deduction(deduction_id)
        if int(deduction["record_id"]) != int(record_id):
            raise NotFound("划扣单不存在")
        if deduction["status"] != "pending":
            raise Conflict("划扣单已处理")
        if deduction["initiated_by"] == actor.user_id:
            raise PermissionDenied("发起人与审批人不能为同一人")
        new_payload = self.rules.apply_confirmed_deduction(record["payload"], deduction)
        return self.repository.confirm_deduction(record_id, deduction_id, int(expected_version), actor.user_id, new_payload)

    def reject_deduction(self, actor: Actor, record_id: int, deduction_id: int, reason: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review_deduction(actor.role):
            raise PermissionDenied("角色无权审批划扣")
        self.repository.get(record_id)
        deduction = self.repository.get_deduction(deduction_id)
        if int(deduction["record_id"]) != int(record_id):
            raise NotFound("划扣单不存在")
        reason = optional_text({"reason": reason}, "reason")
        return self.repository.reject_deduction(record_id, deduction_id, actor.user_id, reason)

    def get_ledger(self, actor: Actor, record_id: int) -> Dict[str, Any]:
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
            "entries": entries,
            "deductions": deductions,
            "summary": self.rules.ledger_summary(record["payload"], entries, deductions),
        }
