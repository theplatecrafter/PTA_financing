"""One literal filter engine for transaction search and unsaved rule previews."""
import json
from decimal import Decimal, InvalidOperation
from . import automation, database


def definition(form):
    fields = form.getlist("condition_field")
    ops = form.getlist("condition_operator")
    patterns = form.getlist("condition_pattern")
    if not len(fields) == len(ops) == len(patterns):
        raise ValueError("Complete every filter.")
    filters = []
    for field, op, pattern in zip(fields, ops, patterns):
        if op not in automation.OPERATORS:
            raise ValueError("Choose a valid match type.")
        if not pattern.strip() and op not in ("empty", "exists"):
            continue
        if op in ("gt", "gte", "lt", "lte"):
            try:
                if not Decimal(pattern).is_finite():
                    raise InvalidOperation()
            except InvalidOperation:
                raise ValueError("Numeric comparisons need a finite number.")
        filters.append(dict(field=field, operator=op, pattern=pattern.strip()))
    result = {k: form.get(k, "") for k in ("source", "currency", "posting_account", "minimum", "maximum")}
    result.update(conditions=filters, match_mode=form.get("match_mode", "all"),
                  direction=form.get("direction", "any"))
    if result["match_mode"] not in ("all", "any") or result["direction"] not in ("any", "positive", "negative"):
        raise ValueError("Choose a valid filter combination and amount direction.")
    try:
        bounds = [Decimal(result[k]) if result[k] else None for k in ("minimum", "maximum")]
        if any(x is not None and (not x.is_finite() or x < 0) for x in bounds):
            raise InvalidOperation()
        if all(x is not None for x in bounds) and bounds[0] > bounds[1]:
            raise InvalidOperation()
    except InvalidOperation:
        raise ValueError("Amount limits must be finite, nonnegative, and in order.")
    return result


def matches(d, row):
    if d.get("source") and d["source"] != row["source"] or d.get("currency") and d["currency"] != row["currency"]:
        return False
    if d.get("posting_account") and not any(p.get("account") == d["posting_account"] for p in json.loads(row["accounting_json"] or "[]")):
        return False
    if d.get("direction", "any") != "any" or d.get("minimum") or d.get("maximum"):
        try:
            amount = Decimal(row["amount"] or "")
            if not amount.is_finite():
                return False
            if d.get("direction") == "positive" and amount <= 0 or d.get("direction") == "negative" and amount >= 0:
                return False
            if d.get("minimum") and abs(amount) < Decimal(d["minimum"]) or d.get("maximum") and abs(amount) > Decimal(d["maximum"]):
                return False
        except InvalidOperation:
            return False
    results = [automation.condition_matches(c, row) for c in d.get("conditions", [])]
    return not results or (any(results) if d.get("match_mode") == "any" else all(results))


def search(path, form):
    d = definition(form)
    rows = database.list_records(path, form.get("status", "all"), form.get("q", ""))
    return [r for r in rows if matches(d, r)]


def search_page(path, form, page=1, page_size=100):
    d = definition(form)
    offset = (page - 1) * page_size
    matched = 0
    result = []
    for row in database.record_batches(path, form.get("status", "all"), form.get("q", ""), page_size):
        if not matches(d, row):
            continue
        if matched >= offset and len(result) < page_size:
            result.append(row)
        matched += 1
    return result, matched
