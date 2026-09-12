from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


STATUSES = ("pending", "held", "linked", "resolved", "synced", "ignored")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(path: Path) -> None:
    with connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                source_file TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                UNIQUE(source, file_hash)
            );
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                description TEXT,
                amount TEXT,
                currency TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                payload TEXT NOT NULL,
                accounting_json TEXT,
                synced_at TEXT
            );
            CREATE TABLE IF NOT EXISTS accounts (
                name TEXT PRIMARY KEY,
                parent TEXT NOT NULL DEFAULT '',
                currency TEXT,
                description TEXT NOT NULL DEFAULT '',
                open_date TEXT NOT NULL DEFAULT '2025-08-01',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_groups (
                group_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'linked',
                description TEXT,
                created_at TEXT NOT NULL,
                synced_at TEXT
            );
            CREATE TABLE IF NOT EXISTS event_members (
                group_id INTEGER NOT NULL REFERENCES event_groups(group_id) ON DELETE CASCADE,
                record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
                PRIMARY KEY (group_id, record_id)
            );
            CREATE TABLE IF NOT EXISTS record_links (
                record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
                linked_record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
                PRIMARY KEY(record_id, linked_record_id),
                CHECK(record_id <> linked_record_id)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        account_columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
        if "open_date" not in account_columns:
            db.execute("ALTER TABLE accounts ADD COLUMN open_date TEXT NOT NULL DEFAULT '2025-08-01'")
        _repair_legacy_links(db)


def _repair_legacy_links(db: sqlite3.Connection) -> None:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='record_links'"
    ).fetchone()
    if not row or "records_legacy" not in (row[0] or ""):
        return
    old_rows = db.execute("SELECT record_id, linked_record_id FROM record_links").fetchall()
    db.execute("DROP TABLE record_links")
    db.execute(
        """CREATE TABLE record_links (
            record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
            linked_record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
            PRIMARY KEY(record_id, linked_record_id), CHECK(record_id <> linked_record_id)
        )"""
    )
    for old in old_rows:
        if db.execute("SELECT 1 FROM records WHERE record_id IN (?, ?)", old).fetchone():
            try:
                db.execute("INSERT INTO record_links VALUES (?, ?)", tuple(old))
            except sqlite3.IntegrityError:
                pass


def _json_default(value: Any) -> str:
    if isinstance(value, (Path, Decimal)):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Cannot encode {type(value).__name__}")


def serialize_record(record: Any) -> dict[str, Any]:
    return json.loads(json.dumps(record.__dict__, default=_json_default, ensure_ascii=False))


def add_records(path: Path, records: Iterable[Any]) -> tuple[int, int]:
    new_count = duplicate_count = 0
    with connect(path) as db:
        for record in records:
            payload = serialize_record(record)
            existing = db.execute(
                "SELECT 1 FROM records WHERE record_id = ?", (record.record_id,)
            ).fetchone()
            if existing:
                duplicate_count += 1
                continue
            source_id = payload.get("source_id")
            legacy_id = f"{record.source}:{source_id}" if source_id else None
            if legacy_id and db.execute("SELECT 1 FROM records WHERE record_id = ?", (legacy_id,)).fetchone():
                db.execute("UPDATE records SET record_id = ? WHERE record_id = ?", (record.record_id, legacy_id))
                new_count += 1
                continue
            db.execute(
                """INSERT INTO records
                (record_id, source, transaction_date, description, amount, currency, status, payload)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    record.record_id, record.source, record.date.isoformat(),
                    record.description, str(record.amount) if record.amount is not None else None,
                    record.currency, json.dumps(payload, ensure_ascii=False),
                ),
            )
            new_count += 1
    return new_count, duplicate_count


def list_records(path: Path, status: str | None = None, query: str = "") -> list[sqlite3.Row]:
    with connect(path) as db:
        clauses, values = [], []
        if status and status != "all":
            clauses.append("status = ?")
            values.append(status)
        if query:
            clauses.append("(record_id LIKE ? OR source LIKE ? OR description LIKE ? OR payload LIKE ?)")
            values.extend([f"%{query}%"] * 4)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return db.execute(
            f"SELECT * FROM records{where} ORDER BY transaction_date DESC, record_id", values
        ).fetchall()


def get_record(path: Path, record_id: str) -> sqlite3.Row | None:
    with connect(path) as db:
        return db.execute("SELECT * FROM records WHERE record_id = ?", (record_id,)).fetchone()


def update_record(path: Path, record_id: str, status: str, accounting: list[dict[str, str]] | None) -> None:
    if status not in STATUSES:
        raise ValueError("Unknown record status")
    if status == "resolved":
        row = get_record(path, record_id)
        if not row:
            raise ValueError("This record no longer exists.")
        validate_postings(path, accounting, row["transaction_date"])
    accounting_json = json.dumps(accounting, ensure_ascii=False) if accounting is not None else None
    with connect(path) as db:
        db.execute(
            "UPDATE records SET status = ?, accounting_json = ?, synced_at = NULL WHERE record_id = ?",
            (status, accounting_json, record_id),
        )


def append_accounting(path: Path, record_ids: list[str], posting_specs: list[dict[str, str]], status: str) -> None:
    if status not in STATUSES:
        raise ValueError("Unknown record status")
    if not record_ids:
        raise ValueError("Select at least one record")
    with connect(path) as db:
        for record_id in record_ids:
            row = db.execute("SELECT accounting_json FROM records WHERE record_id=?", (record_id,)).fetchone()
            if not row:
                raise ValueError(f"Record does not exist: {record_id}")
            if db.execute("SELECT 1 FROM event_members WHERE record_id=?", (record_id,)).fetchone():
                raise ValueError("Review linked events individually to keep their postings consistent.")
            existing = json.loads(row[0]) if row[0] else []
            if status in ('resolved', 'pending'):
                record = db.execute("SELECT amount, currency, transaction_date FROM records WHERE record_id=?", (record_id,)).fetchone()
                amount = Decimal(record[0]) if record[0] is not None else None
                postings = []
                for spec in posting_specs:
                    if amount is None:
                        raise ValueError(f"Record has no amount: {record_id}")
                    value = abs(amount) if spec.get("sign") == "positive" else -abs(amount)
                    postings.append({"account": spec["account"], "amount": str(value), "currency": spec.get("currency") or record[1] or ""})
                if status == 'resolved':
                    validate_postings(path, existing + postings, record[2])
                accounting_json = json.dumps(existing + postings, ensure_ascii=False)
            else:
                accounting_json = row[0]
            db.execute("UPDATE records SET accounting_json=?, status=?, synced_at=NULL WHERE record_id=?", (accounting_json, status, record_id))


def search_rows(path: Path, status: str | None = None, query: str = "") -> list[sqlite3.Row]:
    rows = list_records(path, status, query)
    seen: set[int] = set()
    result: list[sqlite3.Row] = []
    with connect(path) as db:
        for row in rows:
            group = db.execute("SELECT group_id FROM event_members WHERE record_id=?", (row["record_id"],)).fetchone()
            if group and group[0] in seen:
                continue
            if group:
                seen.add(group[0])
            result.append(row)
    return result


def review_rows(path: Path) -> list[sqlite3.Row]:
    rows = list_records(path, "all")
    seen_groups: set[int] = set()
    result: list[sqlite3.Row] = []
    with connect(path) as db:
        for row in rows:
            group = db.execute("SELECT group_id FROM event_members WHERE record_id=?", (row["record_id"],)).fetchone()
            if group:
                group_id = group[0]
                if group_id in seen_groups:
                    continue
                members = db.execute("SELECT status FROM records JOIN event_members USING(record_id) WHERE group_id=?", (group_id,)).fetchall()
                if not any(member[0] in ("pending", "linked") for member in members):
                    continue
                seen_groups.add(group_id)
                representative = db.execute("SELECT r.* FROM records r JOIN event_members m ON m.record_id=r.record_id WHERE m.group_id=? ORDER BY r.transaction_date, r.record_id LIMIT 1", (group_id,)).fetchone()
                result.append(representative)
            elif row["status"] == "pending":
                result.append(row)
    return result


def get_group_id(path: Path, record_id: str) -> int | None:
    with connect(path) as db:
        row = db.execute("SELECT group_id FROM event_members WHERE record_id=?", (record_id,)).fetchone()
        return row[0] if row else None


def update_group(path: Path, group_id: int, status: str, accounting: list[dict[str, str]] | None) -> None:
    if status not in STATUSES:
        raise ValueError("Unknown record status")
    if status == "resolved":
        members = get_group_members(path, group_id)
        if not members:
            raise ValueError("This event no longer exists.")
        validate_postings(path, accounting, min(r["transaction_date"] for r in members))
    accounting_json = json.dumps(accounting, ensure_ascii=False) if accounting is not None else None
    with connect(path) as db:
        db.execute("UPDATE event_groups SET status=?, synced_at=NULL WHERE group_id=?", (status, group_id))
        db.execute("UPDATE records SET status=?, accounting_json=?, synced_at=NULL WHERE record_id IN (SELECT record_id FROM event_members WHERE group_id=?)", (status, accounting_json, group_id))


def get_group_representative(path: Path, group_id: int) -> sqlite3.Row | None:
    with connect(path) as db:
        return db.execute("SELECT r.* FROM records r JOIN event_members m ON m.record_id=r.record_id WHERE m.group_id=? ORDER BY r.transaction_date, r.record_id LIMIT 1", (group_id,)).fetchone()


def set_synced(path: Path, record_ids: Iterable[str]) -> None:
    with connect(path) as db:
        now = datetime.now().isoformat(timespec="seconds")
        for record_id in record_ids:
            db.execute("UPDATE records SET status='synced', synced_at=? WHERE record_id=?", (now, record_id))


def get_accounts(path: Path) -> list[sqlite3.Row]:
    with connect(path) as db:
        return db.execute("SELECT * FROM accounts ORDER BY name").fetchall()


def save_account(path: Path, name: str, currency: str, description: str, open_date: str | None = None, original_name: str | None = None) -> None:
    name = ":".join(part.strip() for part in name.split(":") if part.strip())
    from .validation import ACCOUNT, CURRENCY
    if not ACCOUNT.fullmatch(name):
        raise ValueError("Use a Beancount account path such as Expenses:Food:Groceries.")
    currency = ",".join(part.strip().upper() for part in currency.split(",") if part.strip())
    if currency and any(not CURRENCY.fullmatch(part) for part in currency.split(",")):
        raise ValueError("Use currency codes separated by commas, such as USD,JPY.")
    open_date = open_date or datetime.now().date().isoformat()
    try:
        datetime.strptime(open_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Use a valid account opening date.")
    parent = name.rsplit(":", 1)[0] if ":" in name else ""
    with connect(path) as db:
        if original_name and original_name != name:
            if db.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
                raise ValueError("An account with this name already exists.")
            _require_unused_account(db, original_name)
            db.execute("UPDATE accounts SET name=?, parent=?, currency=?, description=?, open_date=? WHERE name=?", (name, parent, currency or None, description, open_date, original_name))
            db.execute("UPDATE accounts SET parent=? WHERE parent=?", (name, original_name))
        else:
            db.execute("INSERT INTO accounts(name,parent,currency,description,open_date,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET currency=excluded.currency, description=excluded.description, open_date=excluded.open_date", (name, parent, currency or None, description, open_date, datetime.now().isoformat(timespec="seconds")))


def delete_account(path: Path, name: str) -> None:
    with connect(path) as db:
        _require_unused_account(db, name)
        if db.execute("SELECT 1 FROM accounts WHERE parent=?", (name,)).fetchone():
            raise ValueError("Delete child accounts first")
        db.execute("DELETE FROM accounts WHERE name=?", (name,))


def link_records(path: Path, record_ids: list[str]) -> int:
    if len(record_ids) < 2 or len(set(record_ids)) != len(record_ids):
        raise ValueError("Select at least two different records")
    with connect(path) as db:
        missing = db.execute("SELECT COUNT(*) FROM records WHERE record_id IN (%s)" % ",".join("?" * len(record_ids)), record_ids).fetchone()[0]
        if missing != len(record_ids):
            raise ValueError("One or more records no longer exist")
        group_ids = {row[0] for row in db.execute("SELECT group_id FROM event_members WHERE record_id IN (%s)" % ",".join("?" * len(record_ids)), record_ids)}
        if len(group_ids) > 1:
            raise ValueError("These records belong to different events. Unlink them before combining.")
        existing = db.execute("SELECT group_id FROM event_members WHERE record_id IN (%s) ORDER BY group_id LIMIT 1" % ",".join("?" * len(record_ids)), record_ids).fetchone()
        group_id = existing[0] if existing else db.execute("INSERT INTO event_groups(status,created_at) VALUES('linked',?) RETURNING group_id", (datetime.now().isoformat(timespec="seconds"),)).fetchone()[0]
        for record_id in record_ids:
            db.execute("INSERT OR IGNORE INTO event_members VALUES (?,?)", (group_id, record_id))
            db.execute("UPDATE records SET status='linked', synced_at=NULL WHERE record_id=?", (record_id,))
        db.execute("UPDATE event_groups SET status='linked', synced_at=NULL WHERE group_id=?", (group_id,))
        db.execute("UPDATE records SET status='linked', accounting_json=NULL, synced_at=NULL WHERE record_id IN (SELECT record_id FROM event_members WHERE group_id=?)", (group_id,))
        for index, left in enumerate(record_ids):
            for right in record_ids[index + 1:]:
                db.execute("INSERT OR IGNORE INTO record_links VALUES (?,?)", (left, right))
        return group_id


def get_groups(path: Path) -> list[sqlite3.Row]:
    with connect(path) as db:
        return db.execute("SELECT * FROM event_groups ORDER BY group_id DESC").fetchall()


def get_group_members(path: Path, group_id: int) -> list[sqlite3.Row]:
    with connect(path) as db:
        return db.execute("SELECT r.* FROM records r JOIN event_members m ON m.record_id=r.record_id WHERE m.group_id=? ORDER BY r.transaction_date", (group_id,)).fetchall()


def unlink_group(path: Path, group_id: int) -> None:
    with connect(path) as db:
        db.execute("UPDATE records SET status='pending', accounting_json=NULL, synced_at=NULL WHERE record_id IN (SELECT record_id FROM event_members WHERE group_id=?)", (group_id,))
        db.execute("DELETE FROM record_links WHERE record_id IN (SELECT record_id FROM event_members WHERE group_id=?) OR linked_record_id IN (SELECT record_id FROM event_members WHERE group_id=?)", (group_id, group_id))
        db.execute("DELETE FROM event_groups WHERE group_id=?", (group_id,))


EDITOR_TABLES = (
    "imports",
    "records",
    "accounts",
    "event_groups",
    "event_members",
    "record_links",
    "settings",
)


def editor_table(path: Path, table: str) -> tuple[list[sqlite3.Row], list[str], list[str]]:
    if table not in EDITOR_TABLES:
        raise ValueError("Unknown database table")
    with connect(path) as db:
        columns = db.execute(f"PRAGMA table_info([{table}])").fetchall()
        names = [column[1] for column in columns]
        primary_keys = [column[1] for column in columns if column[5]]
        rows = db.execute(f"SELECT * FROM [{table}] ORDER BY rowid DESC" if table != "event_members" and table != "record_links" else f"SELECT * FROM [{table}]").fetchall()
    return rows, names, primary_keys


def editor_update(path: Path, table: str, original: dict[str, str], values: dict[str, str]) -> None:
    rows, columns, primary_keys = editor_table(path, table)
    if not primary_keys:
        raise ValueError("This table has no editable key")
    assignments = [column for column in columns if column not in primary_keys]
    with connect(path) as db:
        db.execute(
            f"UPDATE [{table}] SET " + ", ".join(f"[{column}]=?" for column in assignments) + " WHERE " + " AND ".join(f"[{column}]=?" for column in primary_keys),
            [values.get(column, "") or None for column in assignments] + [original.get(column, "") for column in primary_keys],
        )


def editor_insert(path: Path, table: str, values: dict[str, str]) -> None:
    _, columns, _ = editor_table(path, table)
    supplied = [column for column in columns if values.get(column, "") != ""]
    if not supplied:
        raise ValueError("Enter at least one value")
    with connect(path) as db:
        db.execute(
            f"INSERT INTO [{table}] (" + ",".join(f"[{column}]" for column in supplied) + ") VALUES (" + ",".join("?" for _ in supplied) + ")",
            [values[column] for column in supplied],
        )


def editor_delete(path: Path, table: str, keys: list[dict[str, str]]) -> int:
    _, _, primary_keys = editor_table(path, table)
    if not primary_keys:
        raise ValueError("This table has no editable key")
    deleted = 0
    with connect(path) as db:
        for key in keys:
            cursor = db.execute(
                f"DELETE FROM [{table}] WHERE " + " AND ".join(f"[{column}]=?" for column in primary_keys),
                [key.get(column, "") for column in primary_keys],
            )
            deleted += cursor.rowcount
    return deleted


def editor_duplicate(path: Path, table: str, keys: list[dict[str, str]]) -> int:
    rows, columns, primary_keys = editor_table(path, table)
    if len(primary_keys) != 1:
        raise ValueError("Rows with composite keys cannot be duplicated safely")
    primary_key = primary_keys[0]
    matches = [row for row in rows if str(row[primary_key]) in {key.get(primary_key, "") for key in keys}]
    duplicated = 0
    with connect(path) as db:
        for row in matches:
            values = {column: row[column] for column in columns}
            candidate = str(values[primary_key])
            column_info = db.execute(f"PRAGMA table_info([{table}])").fetchone()
            if column_info[2].upper() == "INTEGER":
                values.pop(primary_key)
            else:
                suffix = 1
                new_value = f"{candidate}:copy"
                while db.execute(f"SELECT 1 FROM [{table}] WHERE [{primary_key}]=?", (new_value,)).fetchone():
                    suffix += 1
                    new_value = f"{candidate}:copy{suffix}"
                values[primary_key] = new_value
            insert_columns = list(values)
            db.execute(f"INSERT INTO [{table}] (" + ",".join(f"[{column}]" for column in insert_columns) + ") VALUES (" + ",".join("?" for _ in insert_columns) + ")", [values[column] for column in insert_columns])
            duplicated += 1
    return duplicated


def validate_postings(path, postings, transaction_date=None):
    from .validation import validate
    validate(postings, {r['name']: r for r in get_accounts(path)}, transaction_date)


def _require_unused_account(db, name):
    for row in db.execute("SELECT accounting_json FROM records WHERE accounting_json IS NOT NULL"):
        if any(p.get("account") == name for p in json.loads(row[0] or "[]")):
            raise ValueError("This account is used by transactions. Update their postings first.")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='rules'").fetchone():
        for row in db.execute("SELECT definition FROM rules"):
            definition = json.loads(row[0])
            if name in (definition.get("source_account"), definition.get("target_account")):
                raise ValueError("This account is used by a rule. Update the rule first.")
