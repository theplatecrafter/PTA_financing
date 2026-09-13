"""Rename account references with rollback across SQLite and included ledger files."""
import glob
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from . import database

ACCOUNT_TOKEN = re.compile(r"(?<![\w:-])(?:Assets|Liabilities|Equity|Income|Expenses)(?::[\w-]+)+(?![\w:-])")
INCLUDE = re.compile(r'^\s*include\s+"([^"]+)"', re.MULTILINE)


def renamed(value, old, new):
    return new + value[len(old):] if value == old or value.startswith(old + ":") else value


def json_references(value, old, new):
    if isinstance(value, list):
        return [json_references(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: json_references(v, old, new) for k, v in value.items()}
    if isinstance(value, str):
        if value.startswith(("[", "{")):
            try:
                # Signatures are JSON strings inside the feature snapshot.
                parsed = json.loads(value)
                changed = json_references(parsed, old, new)
                if parsed != changed:
                    return json.dumps(sorted(changed) if isinstance(changed, list) else changed)
            except ValueError:
                pass
        return renamed(value, old, new)
    return value


def json_account_aliases(value, aliases, new):
    if isinstance(value, list):
        return [json_account_aliases(item, aliases, new) for item in value]
    if isinstance(value, dict):
        return {key: json_account_aliases(item, aliases, new) for key, item in value.items()}
    if isinstance(value, str):
        return new if value in aliases else value
    return value


def ledger_files(paths):
    found = {}
    def visit(path):
        path = Path(path).resolve()
        if path in found or not path.exists():
            return
        content = path.read_text(encoding="utf-8")
        found[path] = content
        for include in INCLUDE.findall(content):
            for child in glob.glob(str(path.parent / include)):
                visit(child)
    for path in paths:
        visit(path)
    return found


def replace_file(path, content):
    fd, staged = tempfile.mkstemp(prefix=".account-rename-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(staged, path.stat().st_mode)
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


@contextmanager
def merge_transaction(db, source, destination, new_name, currency, open_date, paths):
    """Merge two account names into one account, regardless of hierarchy."""
    originals = ledger_files(paths)
    aliases = {source, destination}
    declarations = re.compile(r"^(?P<indent>[ \t]*)(?P<date>\d{4}-\d{2}-\d{2})[ \t]+open[ \t]+(?P<account>\S+)(?P<rest>[^\n]*)(?:\n|$)", re.MULTILINE)
    changed = {}
    for path, content in originals.items():
        text = declarations.sub(lambda match: "" if match.group("account") in aliases else match.group(0), content)
        text = ACCOUNT_TOKEN.sub(lambda match: new_name if match[0] in aliases else match[0], text)
        if path.stem.startswith("accounts"):
            declaration = f"{open_date} open {new_name}" + (f" {currency}" if currency else "") + "\n"
            text = text.rstrip() + ("\n" if text.strip() else "") + declaration
        changed[path] = text
    changed = {p: text for p, text in changed.items() if text != originals[p]}
    written = []
    try:
        db.execute("BEGIN IMMEDIATE")
        names = {row[0] for row in db.execute("SELECT name FROM accounts")}
        if source not in names:
            raise ValueError("The source account no longer exists.")
        if destination not in names:
            raise ValueError("The destination account does not exist.")
        if new_name in names - aliases:
            raise ValueError("The new account name already exists.")
        db.execute("DELETE FROM accounts WHERE name IN (?,?)", (source, destination))
        parent = new_name.rsplit(":", 1)[0] if ":" in new_name else ""
        db.execute("INSERT INTO accounts(name,parent,currency,description,open_date,created_at) VALUES(?,?,?,?,?,datetime('now'))", (new_name, parent, currency or None, "", open_date))
        db.execute("UPDATE accounts SET parent=? WHERE parent IN (?,?)", (new_name, source, destination))
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, key, columns in [
            ("records", "record_id", ["accounting_json"]),
            ("event_groups", "group_id", ["accounting_json"]),
            ("rules", "id", ["definition"]),
            ("automation_log", "id", ["previous_accounting", "applied_accounting"]),
            ("decisions", "record_id", ["template", "features_json"]),
        ]:
            if table not in tables:
                continue
            available = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column not in available:
                    continue
                for row in db.execute(f"SELECT {key},{column} FROM {table} WHERE {column} IS NOT NULL").fetchall():
                    value = json.loads(row[1])
                    if column == "features_json":
                        updated = dict(value)
                        for part in ("layout", "signature"):
                            if part in updated:
                                updated[part] = json_account_aliases(updated[part], aliases, new_name)
                        if "layout" in updated:
                            updated["layout"] = sorted(updated["layout"])
                    elif table == "rules":
                        updated = dict(value)
                        for part in ("source_account", "target_account", "account"):
                            if updated.get(part):
                                updated[part] = new_name if updated[part] in aliases else updated[part]
                    else:
                        updated = json_account_aliases(value, aliases, new_name)
                        if column == "template" and isinstance(updated, list):
                            updated = sorted(updated)
                        elif column == "template" and isinstance(updated, dict) and "layout" in updated:
                            updated["layout"] = sorted(updated["layout"])
                    if updated != value:
                        db.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (json.dumps(updated, ensure_ascii=(column == "template")), row[0]))
        yield
        for path, text in changed.items():
            if path.read_text(encoding="utf-8") != originals[path]:
                raise ValueError(f"Ledger changed during merge: {path.name}. Please retry.")
            replace_file(path, text)
            written.append(path)
        db.commit()
    except BaseException:
        db.rollback()
        for path in reversed(written):
            replace_file(path, originals[path])
        raise


@contextmanager
def rename_transaction(db, old, new, paths):
    """Caller validates fields first; rollback files if any SQL/write/commit fails."""
    originals = ledger_files(paths)
    changed = {path: ACCOUNT_TOKEN.sub(lambda m: renamed(m[0], old, new), content)
               for path, content in originals.items()}
    changed = {p: text for p, text in changed.items() if text != originals[p]}
    written = []
    try:
        db.execute("BEGIN IMMEDIATE")
        ledger_names = {token for content in originals.values() for token in ACCOUNT_TOKEN.findall(content)}
        if any(n == new or n.startswith(new + ":") for n in ledger_names if not (n == old or n.startswith(old + ":"))):
            raise ValueError("The destination account already exists in a ledger file.")
        names = {row[0] for row in db.execute("SELECT name FROM accounts")}
        if old not in names:
            raise ValueError("The account no longer exists.")
        mapping = {n: renamed(n, old, new) for n in names if renamed(n, old, new) != n}
        if any(v in names and v not in mapping for v in mapping.values()):
            raise ValueError("An account or descendant with this name already exists.")
        if new.startswith(old + ":"):
            raise ValueError("An account cannot be moved inside itself.")
        # Primary keys have no foreign keys; account references live in postings JSON.
        for before, after in mapping.items():
            db.execute("UPDATE accounts SET name=?,parent=? WHERE name=?", (after, after.rsplit(":",1)[0], before))
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, key, columns in [
            ("records","record_id",["accounting_json"]),
            ("event_groups","group_id",["accounting_json"]),
            ("rules","id",["definition"]),
            ("automation_log","id",["previous_accounting","applied_accounting"]),
            ("decisions","record_id",["template","features_json"]),
        ]:
            if table not in tables:
                continue
            available = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column not in available:
                    continue
                for row in db.execute(f"SELECT {key},{column} FROM {table} WHERE {column} IS NOT NULL").fetchall():
                    value = json.loads(row[1])
                    if column == "features_json":
                        # Preserve raw observed features; change only account label metadata.
                        updated = dict(value)
                        for part in ("layout", "signature"):
                            if part in updated:
                                updated[part] = json_references(updated[part], old, new)
                        if "layout" in updated:
                            updated["layout"] = sorted(updated["layout"])
                    elif table == "rules":
                        updated = dict(value)
                        for part in ("source_account", "target_account", "account"):
                            if updated.get(part):
                                updated[part] = renamed(updated[part], old, new)
                    else:
                        updated = json_references(value, old, new)
                        if column == "template" and isinstance(updated, list):
                            updated = sorted(updated)
                        elif column == "template" and isinstance(updated, dict) and "layout" in updated:
                            updated["layout"] = sorted(updated["layout"])
                    if updated != value:
                        db.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (json.dumps(updated, ensure_ascii=(column == "template")), row[0]))
        yield
        account_file = next((path for path in changed if path.stem.startswith("accounts")), None)
        if account_file:
            account = db.execute("SELECT open_date,currency FROM accounts WHERE name=?", (new,)).fetchone()
            if account:
                declaration = re.compile(r"^[ \t]*\d{4}-\d{2}-\d{2}[ \t]+open[ \t]+(\S+)[^\n]*(?:\n|$)", re.MULTILINE)
                text = declaration.sub(lambda match: "" if match.group(1) in {old, new} else match.group(0), changed[account_file])
                line = f"{account[0]} open {new}" + (f" {account[1]}" if account[1] else "") + "\n"
                changed[account_file] = text.rstrip() + ("\n" if text.strip() else "") + line
        for path, text in changed.items():
            # Do not overwrite edits made while preparing the rename.
            if path.read_text(encoding="utf-8") != originals[path]:
                raise ValueError(f"Ledger changed during rename: {path.name}. Please retry.")
            replace_file(path, text)
            written.append(path)
        db.commit()
    except BaseException:
        db.rollback()
        for path in reversed(written):
            replace_file(path, originals[path])
        raise
