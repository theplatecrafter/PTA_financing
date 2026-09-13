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
