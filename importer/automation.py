"""Local explainable rules and human-confirmed nearest-neighbor learning."""
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from . import database, learning


def initialize(path):
    with database.connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 100,
            enabled INTEGER NOT NULL DEFAULT 1, mode TEXT NOT NULL, definition TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_log (
            id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, rule_id INTEGER,
            rule_name TEXT NOT NULL, previous_accounting TEXT, applied_accounting TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, undone INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS decisions (
            record_id TEXT PRIMARY KEY, source TEXT NOT NULL, currency TEXT NOT NULL,
            direction TEXT NOT NULL, description TEXT NOT NULL, template TEXT NOT NULL);
        """)
        if "features_json" not in {r[1] for r in db.execute("PRAGMA table_info(decisions)")}:
            db.execute("ALTER TABLE decisions ADD COLUMN features_json TEXT")
    rebuild_learning(path, only_missing=True)


def rules(path):
    with database.connect(path) as db:
        return [dict(row, definition=json.loads(row['definition'])) for row in
                db.execute('SELECT * FROM rules ORDER BY priority, id')]



OPERATORS = {
    "contains": "Contains", "equals": "Equals", "starts": "Starts with",
    "not_contains": "Does not contain", "not_equals": "Does not equal",
    "gt": "Greater than (number)", "gte": "At least (number)",
    "lt": "Less than (number)", "lte": "At most (number)",
    "exists": "Has a value", "empty": "Is empty / missing",
}


def conditions(definition):
    """Read old single-filter definitions without changing saved rules."""
    return definition.get("conditions", [
        {key: definition.get(key, "") for key in ("field", "operator", "pattern")}
    ])


def parsed_values(row):
    values = json.loads(row["payload"])
    # These display columns are the canonical editable source values.
    values.update({key: row[key] for key in ("record_id", "source", "description", "amount", "currency")})
    values["date"] = row["transaction_date"]
    values["transaction_date"] = row["transaction_date"]
    return values


def pointer_parts(field):
    return [part.replace("~1", "/").replace("~0", "~") for part in field[1:].split("/")]


def field_value(row, field):
    value = parsed_values(row)
    for part in pointer_parts(field) if field.startswith("/") else [field]:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def available_fields(path):
    from dataclasses import fields
    from .common import ParsedRecord
    names = {f.name for f in fields(ParsedRecord)} | {"transaction_date"}
    def nested(value, prefix=""):
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            pointer = prefix + "/" + key.replace("~", "~0").replace("/", "~1")
            if prefix:
                names.add(pointer)
            else:
                names.add(key)
            nested(child, pointer)
    with database.connect(path) as db:
        for row in db.execute("SELECT payload FROM records"):
            nested(json.loads(row[0]))
    # Keep saved fields editable even after their source records are removed.
    for rule in rules(path):
        names.update(c["field"] for c in conditions(rule["definition"]))
    return [(name, " → ".join(pointer_parts(name)) if name.startswith("/") else name)
            for name in sorted(names) if name]


def condition_matches(condition, row):
    value = field_value(row, condition["field"])
    empty = value is None or value == "" or value == [] or value == {}
    op, pattern = condition["operator"], condition.get("pattern", "")
    if op == "empty":
        return empty
    if op == "exists":
        return not empty
    if empty:
        return False
    if op in ("gt", "gte", "lt", "lte"):
        try:
            left, right = Decimal(str(value)), Decimal(pattern)
            if not left.is_finite() or not right.is_finite():
                return False
        except InvalidOperation:
            return False
        return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
    text, pattern = text.casefold(), pattern.casefold()
    return {"contains": lambda: pattern in text, "equals": lambda: pattern == text,
            "starts": lambda: text.startswith(pattern), "not_contains": lambda: pattern not in text,
            "not_equals": lambda: pattern != text}[op]()


def save_rule(path, form):
    definition = {key: form.get(key, '').strip() for key in (
        'source', 'currency', 'field', 'operator', 'pattern', 'direction',
        'minimum', 'maximum', 'source_account', 'target_account', 'source_sign')}
    name, mode = form.get('name', '').strip(), form.get('mode', 'suggest')
    if hasattr(form, "getlist") and "condition_field" in form:
        fields, operators, patterns = [form.getlist("condition_" + key) for key in ("field", "operator", "pattern")]
        if not (len(fields) == len(operators) == len(patterns)):
            raise ValueError("Complete every filter.")
        filters = [dict(field=f.strip(), operator=o, pattern=p.strip()) for f, o, p in zip(fields, operators, patterns)]
    else:
        filters = conditions(definition)
    allowed = dict(available_fields(path))
    if not name or not filters:
        raise ValueError("Give the rule a name and at least one filter.")
    for condition in filters:
        if condition["field"] not in allowed or condition["operator"] not in OPERATORS:
            raise ValueError("Choose a parsed field and a valid match type for every filter.")
        if condition["operator"] not in ("exists", "empty") and not condition["pattern"]:
            raise ValueError("Enter a matching value for every filter.")
        if condition["operator"] in ("gt", "gte", "lt", "lte"):
            try:
                if not Decimal(condition["pattern"]).is_finite():
                    raise InvalidOperation()
            except InvalidOperation:
                raise ValueError("Numeric comparisons need a finite number.")
    definition["conditions"] = filters
    definition["match_mode"] = form.get("match_mode", "all")
    if mode not in ("suggest", "automatic") or definition["match_mode"] not in ("all", "any"):
        raise ValueError("Choose a valid rule mode and filter combination.")
    if definition["direction"] not in ("any", "positive", "negative"):
        raise ValueError("Choose a valid amount direction.")
    if definition['source_sign'] not in ('positive', 'negative'):
        raise ValueError('Choose whether the source account increases or decreases.')
    accounts = {row['name'] for row in database.get_accounts(path)}
    if any(definition[k] not in accounts for k in ('source_account', 'target_account')):
        raise ValueError('Create both accounts in Accounts before saving a rule.')
    if definition['source_account'] == definition['target_account']:
        raise ValueError('Choose two different accounts.')
    try:
        priority = int(form.get('priority', '100'))
        bounds = [Decimal(definition[k]) if definition[k] else None for k in ('minimum', 'maximum')]
        if any(v is not None and (not v.is_finite() or v < 0) for v in bounds):
            raise ValueError()
        if all(v is not None for v in bounds) and bounds[0] > bounds[1]:
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise ValueError('Priority must be an integer; amount limits must be nonnegative and in order.')
    values = (name, priority, int(form.get('enabled') == 'on'), mode, json.dumps(definition))
    with database.connect(path) as db:
        if form.get('id'):
            db.execute('UPDATE rules SET name=?,priority=?,enabled=?,mode=?,definition=? WHERE id=?', (*values, int(form['id'])))
        else:
            db.execute('INSERT INTO rules(name,priority,enabled,mode,definition) VALUES(?,?,?,?,?)', values)


def simple_record(row):
    payload = json.loads(row['payload'])
    try:
        amount, fee = Decimal(row['amount'] or ''), Decimal(str(payload.get('fee_amount') or '0'))
        if not amount.is_finite() or amount == 0 or not fee.is_finite() or fee != 0:
            return False
    except InvalidOperation:
        return False
    currencies = {v for v in (row['currency'], payload.get('source_currency'), payload.get('target_currency')) if v}
    return bool(row['currency']) and len(currencies) == 1


def matches(rule, row):
    d = rule['definition']
    if not rule['enabled'] or not simple_record(row):
        return False
    if d['source'] and d['source'] != row['source'] or d['currency'] and d['currency'] != row['currency']:
        return False
    amount = Decimal(row['amount'])
    if d['direction'] == 'positive' and amount <= 0 or d['direction'] == 'negative' and amount >= 0:
        return False
    if d['minimum'] and abs(amount) < Decimal(d['minimum']) or d['maximum'] and abs(amount) > Decimal(d['maximum']):
        return False
    matched = [condition_matches(condition, row) for condition in conditions(d)]
    return bool(matched) and (any(matched) if d.get("match_mode", "all") == "any" else all(matched))


def rule_postings(rule, row):
    d = rule['definition']
    amount = abs(Decimal(row['amount'])) * (1 if d['source_sign'] == 'positive' else -1)
    return [{'account': d['source_account'], 'amount': str(amount), 'currency': row['currency']},
            {'account': d['target_account'], 'amount': str(-amount), 'currency': row['currency']}]


def preview(path, record_ids=None):
    result, configured = [], rules(path)
    with database.connect(path) as db:
        rows = db.execute("""SELECT * FROM records r WHERE status='pending'
            AND (accounting_json IS NULL OR accounting_json IN ('[]', ''))
            AND NOT EXISTS (SELECT 1 FROM event_members m WHERE m.record_id=r.record_id)
            ORDER BY transaction_date DESC, record_id""").fetchall()
    for row in rows:
        if record_ids is not None and row['record_id'] not in record_ids:
            continue
        matched = [rule for rule in configured if matches(rule, row)]
        if not matched:
            continue
        rule, error = matched[0], ''
        postings = rule_postings(rule, row)
        try:
            database.validate_postings(path, postings, row['transaction_date'])
        except ValueError as exc:
            error = str(exc)
        result.append({'record': row, 'rule': rule, 'postings': postings, 'error': error,
                       'also_matched': [r['name'] for r in matched[1:]]})
    return result


def apply(path, record_ids=None):
    count = 0
    for item in preview(path, record_ids):
        if item['rule']['mode'] != 'automatic' or item['error']:
            continue
        row, rule = item['record'], item['rule']
        encoded = json.dumps(item['postings'])
        with database.connect(path) as db:
            cursor = db.execute("""UPDATE records SET status='resolved', accounting_json=?, synced_at=NULL
                WHERE record_id=? AND status='pending' AND (accounting_json IS NULL OR accounting_json IN ('[]',''))
                AND NOT EXISTS(SELECT 1 FROM event_members WHERE record_id=records.record_id)""", (encoded, row['record_id']))
            if cursor.rowcount:
                db.execute('INSERT INTO automation_log(record_id,rule_id,rule_name,previous_accounting,applied_accounting) VALUES(?,?,?,?,?)',
                           (row['record_id'], rule['id'], rule['name'], row['accounting_json'], encoded))
                count += 1
    return count


def undo(path, log_id):
    with database.connect(path) as db:
        log = db.execute('SELECT * FROM automation_log WHERE id=? AND undone=0', (log_id,)).fetchone()
        if not log:
            raise ValueError('This action was already undone or no longer exists.')
        cursor = db.execute("""UPDATE records SET status='pending',accounting_json=?,synced_at=NULL
            WHERE record_id=? AND status='resolved' AND accounting_json=?
            AND NOT EXISTS(SELECT 1 FROM event_members WHERE record_id=records.record_id)""",
            (log['previous_accounting'], log['record_id'], log['applied_accounting']))
        if not cursor.rowcount:
            raise ValueError('Only unchanged, unexported automatic decisions can be undone here.')
        db.execute('UPDATE automation_log SET undone=1 WHERE id=?', (log_id,))


def learn(path, row, postings):
    """Save features from human-confirmed decisions, including complex layouts."""
    database.validate_postings(path, postings, row["transaction_date"])
    with database.connect(path) as db:
        db.execute("DELETE FROM decisions WHERE record_id=?", (row["record_id"],))
        if database.get_group_id(path, row["record_id"]):
            return
        balanced = simple_record(row) and len(postings) == 2 and all(
            p["currency"] == row["currency"] and abs(Decimal(p["amount"])) == abs(Decimal(row["amount"]))
            for p in postings)
        if len({p["account"] for p in postings}) < 2:
            return
        snapshot = learning.make_snapshot(row, postings, balanced)
        template = (sorted([(p["account"], 1 if Decimal(p["amount"]) > 0 else -1) for p in postings])
                    if balanced else {"mode": "accounts", "layout": snapshot["layout"]})
        payload = json.loads(row["payload"])
        description = str(payload.get("counterparty") or row["description"] or "")
        db.execute("""INSERT OR REPLACE INTO decisions
            (record_id,source,currency,direction,description,template,features_json)
            VALUES(?,?,?,?,?,?,?)""", (row["record_id"], row["source"], row["currency"] or "",
            learning.direction(row), description, json.dumps(template), json.dumps(snapshot, ensure_ascii=False)))


def rebuild_learning(path, only_missing=False):
    """Enrich existing human labels, never infer labels from resolved status alone."""
    with database.connect(path) as db:
        rows = db.execute("""SELECT r.*, d.template AS learned_template, d.features_json
            FROM decisions d JOIN records r USING(record_id)
            WHERE r.status IN ('resolved','synced') AND NOT EXISTS
            (SELECT 1 FROM event_members m WHERE m.record_id=r.record_id)""").fetchall()
    count = 0
    for row in rows:
        if only_missing and row["features_json"]:
            continue
        postings = json.loads(row["accounting_json"] or "[]")
        if row["features_json"]:
            if json.loads(row["features_json"])["signature"] != learning.canonical(postings):
                continue
        else:
            old_template = sorted([(p["account"], 1 if Decimal(p["amount"]) > 0 else -1) for p in postings])
            if json.dumps(old_template) != row["learned_template"]:
                continue
        try:
            learn(path, row, postings)
            count += 1
        except ValueError:
            continue
    return count


def suggestion(path, row, examples=None, profiles=None):
    if not row or row['status'] != 'pending' or (row['accounting_json'] and json.loads(row['accounting_json'])) or database.get_group_id(path, row['record_id']):
        return None
    for rule in rules(path):
        if matches(rule, row):
            postings = rule_postings(rule, row)
            try:
                database.validate_postings(path, postings, row['transaction_date'])
            except ValueError:
                return None
            return {'postings': postings, 'reason': 'Rule: ' + rule['name'], 'kind': 'rule'}
    with database.connect(path) as db:
        setting = db.execute("SELECT value FROM settings WHERE key='learning_enabled'").fetchone()
        if setting and setting[0] == "false":
            return None
    return learning.predict(path, row, examples if examples is not None else learning.load_examples(path), profiles)



def scan_pending(path):
    """Fresh, read-only predictions; existing drafts and linked events are untouched."""
    pending = database.list_records(path, "pending")
    results = []
    examples = learning.load_examples(path)
    profiles = {source: learning.fit_weights([e for e in examples if e["source"] == source])
                for source in {e["source"] for e in examples}}
    for row in pending:
        prediction = suggestion(path, row, examples, profiles)
        if prediction:
            results.append({"record": row, **prediction})
    return {"results": results, "total": len(pending), "skipped": len(pending) - len(results)}
