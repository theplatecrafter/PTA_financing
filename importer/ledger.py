from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Iterable

from . import database


OPEN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+open\s+(\S+)(?:\s+(\S+))?")
MARKER_RE = re.compile(r"^; importer:(?:record|group):(.+)$", re.MULTILINE)


def _posting_lines(postings: list[dict[str, str]]) -> list[str]:
    lines = []
    for posting in postings:
        account = posting.get("account", "").strip()
        amount = posting.get("amount", "").strip()
        currency = posting.get("currency", "").strip()
        if account and amount and currency:
            lines.append(f"    {account:<42} {amount} {currency}")
    return lines


def render_transaction(transaction_id: str, transaction_date: str, narration: str, postings: list[dict[str, str]], metadata: Iterable[tuple[str, str]] = ()) -> str:
    lines = [f"; importer:{transaction_id}", f'{transaction_date} * "{narration.replace(chr(34), chr(39))}"']
    for key, value in metadata:
        escaped = value.replace('"', "'")
        lines.append(f'    {key}: "{escaped}"')
    lines.extend(_posting_lines(postings))
    lines.append("; importer:end")
    return "\n".join(lines) + "\n"


def _replace_generated_blocks(text: str, blocks: dict[str, str]) -> str:
    pattern = re.compile(r"(?ms)^; importer:(?:record|group):(?P<key>[^\n]+)\n.*?^; importer:end\n")
    seen: set[str] = set()
    def replace(match: re.Match[str]) -> str:
        key = match.group("key")
        if key in blocks:
            seen.add(key)
            return blocks[key]
        return match.group(0)
    result = pattern.sub(replace, text)
    additions = [blocks[key] for key in blocks if key not in seen]
    if additions:
        result = result.rstrip() + "\n\n" + "\n".join(additions)
    return result if result.endswith("\n") else result + "\n"


def export_accounts(db_path: Path, accounts_path: Path) -> int:
    accounts = database.get_accounts(db_path)
    existing = accounts_path.read_text(encoding="utf-8") if accounts_path.exists() else ""
    lines = existing.splitlines()
    non_open = [line for line in lines if not OPEN_RE.match(line)]
    generated = []
    for account in accounts:
        currency = f" {account['currency']}" if account["currency"] else ""
        generated.append(f"{account['open_date']} open {account['name']}{currency}")
    content = "\n".join(generated + [line for line in non_open if line.strip()])
    accounts_path.write_text((content.rstrip() + "\n") if content else "", encoding="utf-8")
    return len(accounts)


def _record_block(row: object) -> tuple[str, str] | None:
    accounting = row["accounting_json"]
    if not accounting or row["status"] not in ("resolved", "synced"):
        return None
    postings = __import__("json").loads(accounting)
    if not postings:
        return None
    metadata = (("source_record", row["record_id"]),)
    return row["record_id"], render_transaction(row["record_id"], row["transaction_date"], row["description"] or row["record_id"], postings, metadata)


def export_posts(db_path: Path, posts_path: Path) -> list[str]:
    rows = database.list_records(db_path, "all")
    blocks = {}
    exported: list[str] = []
    grouped: set[str] = set()
    with database.connect(db_path) as db:
        group_rows = db.execute("SELECT DISTINCT group_id FROM event_members").fetchall()
    for group_row in group_rows:
        members = database.get_group_members(db_path, group_row[0])
        if not members or any(member["status"] not in ("resolved", "synced") or not member["accounting_json"] for member in members):
            continue
        first = members[0]
        metadata = [(f"source_record_{index}", member["record_id"]) for index, member in enumerate(members, 1)]
        postings = __import__("json").loads(first["accounting_json"])
        marker = f"group:{group_row[0]}"
        blocks[marker] = render_transaction(marker, first["transaction_date"], first["description"] or marker, postings, metadata)
        grouped.update(member["record_id"] for member in members)
        exported.extend(member["record_id"] for member in members)
    for row in rows:
        if row["record_id"] in grouped:
            continue
        rendered = _record_block(row)
        if rendered:
            blocks[rendered[0]] = rendered[1]
            exported.append(row["record_id"])
    existing = posts_path.read_text(encoding="utf-8") if posts_path.exists() else ""
    posts_path.write_text(_replace_generated_blocks(existing, blocks), encoding="utf-8")
    return exported


def export_all(db_path: Path, accounts_path: Path, posts_path: Path) -> list[str]:
    export_accounts(db_path, accounts_path)
    return export_posts(db_path, posts_path)


def import_beancount(db_path: Path, accounts_path: Path, posts_path: Path) -> tuple[int, int]:
    account_count = 0
    if accounts_path.exists():
        for line in accounts_path.read_text(encoding="utf-8").splitlines():
            match = OPEN_RE.match(line)
            if match:
                open_date, name, currency = match.groups()
                with database.connect(db_path) as db:
                    parent = name.rsplit(":", 1)[0] if ":" in name else ""
                    db.execute("INSERT INTO accounts(name,parent,currency,description,open_date,created_at) VALUES(?,?,?,?,?,datetime('now')) ON CONFLICT(name) DO UPDATE SET open_date=excluded.open_date, currency=COALESCE(excluded.currency, accounts.currency)", (name, parent, currency, "", open_date))
                account_count += 1
    record_ids: set[str] = set()
    if posts_path.exists():
        record_ids = set(MARKER_RE.findall(posts_path.read_text(encoding="utf-8")))
    with database.connect(db_path) as db:
        for record_id in record_ids:
            db.execute("UPDATE records SET status='synced', synced_at=COALESCE(synced_at, datetime('now')) WHERE record_id=?", (record_id,))
    return account_count, len(record_ids)
