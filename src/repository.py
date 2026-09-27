"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fund_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    amount REAL NOT NULL,
                    used_amount REAL NOT NULL DEFAULT 0,
                    arrived_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deductions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    target TEXT NOT NULL,
                    period TEXT NOT NULL DEFAULT '',
                    amount REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    note TEXT NOT NULL DEFAULT '',
                    initiated_by TEXT NOT NULL,
                    decided_by TEXT,
                    reject_reason TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS deduction_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deduction_id INTEGER NOT NULL REFERENCES deductions(id) ON DELETE CASCADE,
                    entry_id INTEGER NOT NULL REFERENCES fund_entries(id) ON DELETE CASCADE,
                    amount REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_entries_record ON fund_entries(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_deductions_record ON deductions(record_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_fund_entry(self, record_id: int, entry: Dict[str, Any], actor_id: str, required_states, state_error: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state,version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if row["state"] not in required_states:
                connection.rollback()
                raise Conflict(state_error)
            cursor = connection.execute(
                "INSERT INTO fund_entries(record_id,source,purpose,amount,used_amount,arrived_at,note,created_by,created_at) VALUES(?,?,?,?,0,?,?,?,?)",
                (record_id, entry["source"], entry["purpose"], entry["amount"], entry["arrived_at"], entry["note"], actor_id, now),
            )
            entry_id = int(cursor.lastrowid)
            version = int(row["version"]) + 1
            connection.execute("UPDATE records SET version=?,updated_by=?,updated_at=? WHERE id=?", (version, actor_id, now, record_id))
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "ledger_entry_registered", actor_id, version, json.dumps({"summary": "登记纾困入账", "entry_id": entry_id, "entry": entry}, ensure_ascii=False, sort_keys=True), now),
            )
            created = connection.execute("SELECT * FROM fund_entries WHERE id=?", (entry_id,)).fetchone()
            connection.commit()
        return dict(created)

    def list_fund_entries(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM fund_entries WHERE record_id=? ORDER BY arrived_at, id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def insert_deduction(self, record_id: int, deduction: Dict[str, Any], actor_id: str, required_states, state_error: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state,version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if row["state"] not in required_states:
                connection.rollback()
                raise Conflict(state_error)
            if deduction["period"]:
                duplicate = connection.execute(
                    "SELECT id FROM deductions WHERE record_id=? AND target=? AND period=? AND status IN ('pending','confirmed')",
                    (record_id, deduction["target"], deduction["period"]),
                ).fetchone()
                if duplicate is not None:
                    connection.rollback()
                    raise Conflict("该期次已存在划扣记录，不能重复发起")
            cursor = connection.execute(
                "INSERT INTO deductions(record_id,target,period,amount,status,note,initiated_by,created_at) VALUES(?,?,?,?,'pending',?,?,?)",
                (record_id, deduction["target"], deduction["period"], deduction["amount"], deduction["note"], actor_id, now),
            )
            deduction_id = int(cursor.lastrowid)
            version = int(row["version"]) + 1
            connection.execute("UPDATE records SET version=?,updated_by=?,updated_at=? WHERE id=?", (version, actor_id, now, record_id))
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "ledger_deduction_initiated", actor_id, version, json.dumps({"summary": "发起划扣申请", "deduction_id": deduction_id, "deduction": deduction}, ensure_ascii=False, sort_keys=True), now),
            )
            created = connection.execute("SELECT * FROM deductions WHERE id=?", (deduction_id,)).fetchone()
            connection.commit()
        result = dict(created)
        result["allocations"] = []
        return result

    def get_deduction(self, record_id: int, deduction_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM deductions WHERE id=? AND record_id=?", (deduction_id, record_id)).fetchone()
            if row is None:
                raise NotFound("划扣单不存在")
            allocations = connection.execute("SELECT entry_id,amount FROM deduction_allocations WHERE deduction_id=? ORDER BY id", (deduction_id,)).fetchall()
        item = dict(row)
        item["allocations"] = [dict(allocation) for allocation in allocations]
        return item

    def list_deductions(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            deductions = connection.execute("SELECT * FROM deductions WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
            allocations = connection.execute(
                "SELECT da.deduction_id,da.entry_id,da.amount FROM deduction_allocations da JOIN deductions d ON d.id=da.deduction_id WHERE d.record_id=? ORDER BY da.id",
                (record_id,),
            ).fetchall()
        by_deduction: Dict[int, List[Dict[str, Any]]] = {}
        for allocation in allocations:
            by_deduction.setdefault(int(allocation["deduction_id"]), []).append({"entry_id": allocation["entry_id"], "amount": allocation["amount"]})
        result = []
        for deduction in deductions:
            item = dict(deduction)
            item["allocations"] = by_deduction.get(item["id"], [])
            result.append(item)
        return result

    def confirm_deduction(self, record_id: int, deduction_id: int, actor_id: str, planner) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            deduction_row = connection.execute("SELECT * FROM deductions WHERE id=? AND record_id=?", (deduction_id, record_id)).fetchone()
            if deduction_row is None:
                connection.rollback()
                raise NotFound("划扣单不存在")
            if deduction_row["status"] != "pending":
                connection.rollback()
                raise Conflict("划扣单已处理，不能重复确认")
            entry_rows = connection.execute(
                "SELECT * FROM fund_entries WHERE record_id=? AND purpose=? ORDER BY arrived_at, id",
                (record_id, deduction_row["target"]),
            ).fetchall()
            record = self._row(record_row)
            plan = planner(record, dict(deduction_row), [dict(entry_row) for entry_row in entry_rows])
            for allocation in plan["allocations"]:
                current = next(entry_row for entry_row in entry_rows if entry_row["id"] == allocation["entry_id"])
                used = round(float(current["used_amount"]) + allocation["amount"], 2)
                connection.execute("UPDATE fund_entries SET used_amount=? WHERE id=?", (used, allocation["entry_id"]))
                connection.execute("INSERT INTO deduction_allocations(deduction_id,entry_id,amount) VALUES(?,?,?)", (deduction_id, allocation["entry_id"], allocation["amount"]))
            payload = dict(record["payload"])
            payload.update(plan["payload"])
            version = int(record_row["version"]) + 1
            connection.execute(
                "UPDATE records SET version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute("UPDATE deductions SET status='confirmed',decided_by=?,decided_at=? WHERE id=?", (actor_id, now, deduction_id))
            details = {"summary": "划扣确认入账", "deduction_id": deduction_id, "target": deduction_row["target"], "period": deduction_row["period"], "amount": deduction_row["amount"], "allocations": plan["allocations"]}
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "ledger_deduction_confirmed", actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_deduction(record_id, deduction_id)

    def reject_deduction(self, record_id: int, deduction_id: int, actor_id: str, reason: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            deduction_row = connection.execute("SELECT * FROM deductions WHERE id=? AND record_id=?", (deduction_id, record_id)).fetchone()
            if deduction_row is None:
                connection.rollback()
                raise NotFound("划扣单不存在")
            if deduction_row["status"] != "pending":
                connection.rollback()
                raise Conflict("划扣单已处理，不能驳回")
            version = int(record_row["version"]) + 1
            connection.execute("UPDATE records SET version=?,updated_by=?,updated_at=? WHERE id=?", (version, actor_id, now, record_id))
            connection.execute("UPDATE deductions SET status='rejected',decided_by=?,decided_at=?,reject_reason=? WHERE id=?", (actor_id, now, reason, deduction_id))
            details = {"summary": "划扣驳回", "deduction_id": deduction_id, "reason": reason}
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "ledger_deduction_rejected", actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_deduction(record_id, deduction_id)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
