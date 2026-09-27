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
                    available_amount REAL NOT NULL,
                    received_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deductions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    entry_id INTEGER NOT NULL REFERENCES fund_entries(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    amount REAL NOT NULL,
                    target_period TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    initiated_by TEXT NOT NULL,
                    confirmed_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_entries_record ON fund_entries(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_deductions_record ON deductions(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_deductions_active_period ON deductions(entry_id, target_period) WHERE status IN ('pending','confirmed') AND kind='current_arrears';
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

    def add_fund_entry(self, record_id: int, entry: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            cursor = connection.execute(
                "INSERT INTO fund_entries(record_id,source,purpose,amount,available_amount,received_at,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (record_id, entry["source"], entry["purpose"], entry["amount"], entry["amount"], entry["received_at"], entry["note"], actor_id, now),
            )
            entry_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "fund_entry_registered", actor_id, int(record["version"]), json.dumps({"entry_id": entry_id, "source": entry["source"], "purpose": entry["purpose"], "amount": entry["amount"], "received_at": entry["received_at"]}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM fund_entries WHERE id=?", (entry_id,)).fetchone()
            connection.commit()
        return dict(row)

    def get_fund_entry(self, entry_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM fund_entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise NotFound("入账记录不存在")
        return dict(row)

    def list_fund_entries(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM fund_entries WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_deduction(self, deduction_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM deductions WHERE id=?", (deduction_id,)).fetchone()
        if row is None:
            raise NotFound("划扣单不存在")
        return dict(row)

    def list_deductions(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM deductions WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def initiate_deduction(self, record_id: int, entry_id: int, kind: str, amount: float, target_period: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            entry = connection.execute("SELECT * FROM fund_entries WHERE id=? AND record_id=?", (entry_id, record_id)).fetchone()
            if entry is None:
                connection.rollback()
                raise NotFound("入账记录不存在")
            if float(entry["available_amount"]) + 1e-9 < float(amount):
                connection.rollback()
                raise Conflict("入账可用余额不足")
            try:
                cursor = connection.execute(
                    "INSERT INTO deductions(record_id,entry_id,kind,amount,target_period,status,initiated_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (record_id, entry_id, kind, float(amount), target_period, "pending", actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("该笔入账对应期次已存在划扣") from exc
            deduction_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "deduction_initiated", actor_id, int(record["version"]), json.dumps({"deduction_id": deduction_id, "entry_id": entry_id, "kind": kind, "amount": float(amount), "target_period": target_period}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM deductions WHERE id=?", (deduction_id,)).fetchone()
            connection.commit()
        return dict(row)

    def confirm_deduction(self, record_id: int, deduction_id: int, expected_version: int, actor_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deduction = connection.execute("SELECT * FROM deductions WHERE id=? AND record_id=?", (deduction_id, record_id)).fetchone()
            if deduction is None:
                connection.rollback()
                raise NotFound("划扣单不存在")
            if deduction["status"] != "pending":
                connection.rollback()
                raise Conflict("划扣单已处理")
            entry = connection.execute("SELECT * FROM fund_entries WHERE id=?", (deduction["entry_id"],)).fetchone()
            if entry is None:
                connection.rollback()
                raise NotFound("入账记录不存在")
            if float(entry["available_amount"]) + 1e-9 < float(deduction["amount"]):
                connection.rollback()
                raise Conflict("入账可用余额不足")
            record = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(record["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            remaining = round(float(entry["available_amount"]) - float(deduction["amount"]), 2)
            connection.execute("UPDATE fund_entries SET available_amount=? WHERE id=?", (remaining, entry["id"]))
            connection.execute("UPDATE deductions SET status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=?", (actor_id, now, deduction_id))
            connection.execute(
                "UPDATE records SET version=?, payload=?, updated_by=?, updated_at=? WHERE id=?",
                (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "deduction_confirmed", actor_id, version, json.dumps({"deduction_id": deduction_id, "entry_id": deduction["entry_id"], "kind": deduction["kind"], "amount": float(deduction["amount"]), "target_period": deduction["target_period"]}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM deductions WHERE id=?", (deduction_id,)).fetchone()
            connection.commit()
        return dict(row)

    def reject_deduction(self, record_id: int, deduction_id: int, actor_id: str, reason: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deduction = connection.execute("SELECT * FROM deductions WHERE id=? AND record_id=?", (deduction_id, record_id)).fetchone()
            if deduction is None:
                connection.rollback()
                raise NotFound("划扣单不存在")
            if deduction["status"] != "pending":
                connection.rollback()
                raise Conflict("划扣单已处理")
            record = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            connection.execute("UPDATE deductions SET status='rejected', confirmed_by=?, confirmed_at=? WHERE id=?", (actor_id, now, deduction_id))
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "deduction_rejected", actor_id, int(record["version"]), json.dumps({"deduction_id": deduction_id, "reason": reason}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM deductions WHERE id=?", (deduction_id,)).fetchone()
            connection.commit()
        return dict(row)
