"""Local explainable rules and human-confirmed nearest-neighbor learning."""
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from . import database


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


def rules(path):
    with database.connect(path) as db:
        return [dict(row, definition=json.loads(row['definition'])) for row in
                db.execute('SELECT * FROM rules ORDER BY priority, id')]


def save_rule(path, form):
    definition = {key: form.get(key, '').strip() for key in (
        'source', 'currency', 'field', 'operator', 'pattern', 'direction',
        'minimum', 'maximum', 'source_account', 'target_account', 'source_sign')}
    name, mode = form.get('name', '').strip(), form.get('mode', 'suggest')
    if not name or not definition['pattern']:
        raise ValueError('Give the rule a name and matching text.')
    if mode not in ('suggest', 'automatic') or definition['field'] not in ('description', 'counterparty', 'category'):
        raise ValueError('Choose a valid rule mode and matching field.')
    if definition['operator'] not in ('contains', 'equals', 'starts') or definition['direction'] not in ('any', 'positive', 'negative'):
        raise ValueError('Choose a valid match and amount direction.')
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
    value = str(row['description'] or '') if d['field'] == 'description' else str(json.loads(row['payload']).get(d['field']) or '')
    value, pattern = value.casefold(), d['pattern'].casefold()
    return {'contains': lambda: pattern in value, 'equals': lambda: pattern == value,
            'starts': lambda: value.startswith(pattern)}[d['operator']]()


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
    """Store the latest human decision once; never train on automatic output."""
    with database.connect(path) as db:
        db.execute('DELETE FROM decisions WHERE record_id=?', (row['record_id'],))
        if not simple_record(row) or len(postings) != 2 or database.get_group_id(path, row['record_id']):
            return
        amount = abs(Decimal(row['amount']))
        if any(p['currency'] != row['currency'] or abs(Decimal(p['amount'])) != amount for p in postings):
            return
        template = sorted([(p['account'], 1 if Decimal(p['amount']) > 0 else -1) for p in postings])
        if template[0][0] == template[1][0]:
            return
        payload = json.loads(row['payload'])
        description = str(payload.get('counterparty') or row['description'] or '')
        db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?)', (row['record_id'], row['source'], row['currency'],
                   'positive' if Decimal(row['amount']) > 0 else 'negative', description, json.dumps(template)))


def tokens(text):
    return set(re.findall(r'[^\W\d_]+', text.casefold(), re.UNICODE))


def suggestion(path, row):
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
    if not simple_record(row):
        return None
    with database.connect(path) as db:
        setting = db.execute("SELECT value FROM settings WHERE key='learning_enabled'").fetchone()
        if setting and setting[0] == 'false':
            return None
        examples = db.execute("""SELECT d.*, r.accounting_json AS current_accounting FROM decisions d JOIN records r USING(record_id)
            WHERE r.status IN ('resolved','synced') AND d.source=? AND d.currency=? AND d.direction=?
            AND d.record_id<>? AND NOT EXISTS(SELECT 1 FROM event_members m WHERE m.record_id=d.record_id)""",
            (row['source'], row['currency'], 'positive' if Decimal(row['amount']) > 0 else 'negative', row['record_id'])).fetchall()
    payload = json.loads(row['payload'])
    query = tokens(str(payload.get('counterparty') or row['description'] or ''))
    if not query:
        return None
    weights, support = defaultdict(float), Counter()
    for example in examples:
        current = json.loads(example['current_accounting'] or '[]')
        template = sorted([(p['account'], 1 if Decimal(p['amount']) > 0 else -1) for p in current])
        if json.dumps(template) != example['template']:
            continue
        other = tokens(example['description'])
        similarity = len(query & other) / len(query | other)
        if similarity >= .6:
            weights[example['template']] += similarity
            support[example['template']] += 1
    if not weights:
        return None
    best = max(weights, key=weights.get)
    agreement = weights[best] / sum(weights.values())
    if support[best] < 3 or agreement < .8:
        return None
    postings = [{'account': account, 'amount': str(abs(Decimal(row['amount'])) * sign), 'currency': row['currency']}
                for account, sign in json.loads(best)]
    try:
        database.validate_postings(path, postings, row['transaction_date'])
    except ValueError:
        return None
    return {'kind': 'learned', 'postings': postings,
            'reason': f"Learned from {support[best]} similar confirmed decisions · {agreement:.0%} weighted agreement (not a probability)"}

