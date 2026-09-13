"""Source-specific, field-aware nearest-neighbor learning. No external services."""
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from . import database

VERSION = 2
IDENTIFIERS = {"record_id", "source_id", "source_file", "source_row"}
NUMBERS = {"amount", "balance", "payment_amount", "source_amount", "target_amount",
           "fee_amount", "exchange_rate", "installment_number"}
DATES = {"date", "conversion_date", "completed_at"}
CATEGORICAL = {"category", "payment_method", "payment_type", "currency",
               "source_currency", "target_currency", "fee_currency", "balance_currency"}
RAW_ID = re.compile(r"(^|[ _])(id|uuid|number|row|file)($|[ _])|取引番号|取引id", re.I)


def normalized(value):
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def text_tokens(value):
    text = normalized(value)
    tokens = set(re.findall(r"[^\W\d_]+", text, re.UNICODE))
    # Japanese descriptions have no word spaces: share character bigrams too.
    for segment in re.findall(r"[\u3040-\u30ff\u3400-\u9fff]+", text):
        tokens.update(segment[i:i+2] for i in range(len(segment)-1))
    return sorted(tokens)


def number(value):
    try:
        value = Decimal(str(value).replace(",", ""))
        return str(value) if value.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def extract_features(row):
    data = json.loads(row["payload"])
    data.update({k: row[k] for k in ("description", "amount", "currency")})
    data["date"] = row["transaction_date"]
    features = {}
    def add(key, kind, value):
        if value is not None and value != [] and value != "":
            features[key] = {"kind": kind, "value": value}
    def temporal(key, value):
        try:
            parsed = datetime.fromisoformat(str(value).replace("/", "-").replace("Z", "+00:00"))
            add(key + ".weekday", "category", str(parsed.weekday()))
            add(key + ".month", "category", str(parsed.month))
            add(key + ".monthday", "day", parsed.day)
            if key == "completed_at":
                add(key + ".hour", "hour", parsed.hour + parsed.minute / 60)
        except ValueError:
            pass
    for key, value in data.items():
        if key in IDENTIFIERS or key in ("source", "raw_data") or value in (None, "", [], {}):
            continue
        if key in DATES:
            temporal(key, value)
        elif key == "time":
            try:
                parts = str(value).split(":")
                hour, minute = int(parts[0]), int(parts[1])
                if 0 <= hour < 24 and 0 <= minute < 60:
                    add("time.hour", "hour", hour + minute / 60)
            except (ValueError, IndexError):
                pass
        elif key in NUMBERS:
            add(key, "number", number(value))
        elif key in CATEGORICAL:
            add(key, "category", normalized(value))
        else:
            add(key, "text", text_tokens(json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value))

    # Original columns stay namespaced. Do not count exact copies of normalized
    # fields again, or allow transaction IDs to become merchant evidence.
    normalized_values = {normalized(v) for k,v in data.items()
                         if k not in IDENTIFIERS and not isinstance(v, (dict,list)) and v is not None}
    def raw_fields(value, prefix="raw_data"):
        if not isinstance(value, dict):
            return
        for key, child in sorted(value.items()):
            name = prefix + "/" + key.replace("~", "~0").replace("/", "~1")
            if RAW_ID.search(normalized(key)) or child in (None, "", "-", [], {}):
                continue
            if isinstance(child, dict):
                raw_fields(child, name)
            elif normalized(child) not in normalized_values:
                numeric = number(child)
                if numeric is not None:
                    add(name, "number", numeric)
                else:
                    add(name, "text", text_tokens(child))
    raw_fields(data.get("raw_data", {}))
    return features


def canonical(postings):
    return json.dumps(sorted((p["account"], str(Decimal(p["amount"])), p["currency"]) for p in postings))


def make_snapshot(row, postings, balanced):
    return {"version": VERSION, "fields": extract_features(row),
            "signature": canonical(postings), "mode": "balanced" if balanced else "accounts",
            "layout": sorted((p["account"], p["currency"], 1 if Decimal(p["amount"]) > 0 else -1)
                             for p in postings)}


def base_weight(key):
    if key == "currency":
        return 0.0  # already an exact candidate filter
    if key.startswith("raw_data/"):
        return 1.0
    if key in ("description", "counterparty"):
        return 3.0
    if key in ("payment_method", "payment_type", "category"):
        return 2.5
    if key in ("note", "tags", "reference"):
        return 1.5
    if key in ("source_currency", "target_currency", "fee_currency"):
        return 1.5
    if key.startswith(("date.", "conversion_date.", "completed_at.", "time.")):
        return .35
    if key.startswith("balance"):
        return .1
    return 1.0


def field_similarity(left, right):
    if not left or not right or left["kind"] != right["kind"]:
        return 0.0
    a, b, kind = left["value"], right["value"], left["kind"]
    if kind == "text":
        a, b = set(a), set(b)
        return len(a & b) / len(a | b) if a or b else 0.0
    if kind == "category":
        return float(a == b)
    if kind in ("hour", "day"):
        period, tolerance = (24, 6) if kind == "hour" else (31, 8)
        distance = abs(float(a)-float(b))
        return max(0., 1. - min(distance, period-distance) / tolerance)
    if kind == "number":
        a, b = Decimal(a), Decimal(b)
        if a == b:
            return 1.
        if a*b < 0:
            return 0.
        return float(min(abs(a),abs(b)) / max(abs(a),abs(b)))
    return 0.


def fit_weights(examples):
    """Learn discriminative fields per source from within/between-label pairs."""
    keys = sorted({key for e in examples for key in e["snapshot"]["fields"]})
    weights = {key: base_weight(key) for key in keys}
    # Deterministic, bounded pair fitting, balanced across account templates.
    by_label = defaultdict(list)
    for example in examples:
        by_label[example["template"]].append(example)
    sample = [by_label[label][i] for i in range(30) for label in sorted(by_label) if i < len(by_label[label])][:120]
    if len(by_label) >= 2:
        within, between = defaultdict(list), defaultdict(list)
        for i, left in enumerate(sample):
            for right in sample[i+1:]:
                bucket = within if left["template"] == right["template"] else between
                a, b = left["snapshot"]["fields"], right["snapshot"]["fields"]
                for key in a.keys() | b.keys():
                    bucket[key].append(field_similarity(a.get(key), b.get(key)))
        for key in keys:
            if len(within[key]) >= 3 and len(between[key]) >= 3:
                separation = sum(within[key])/len(within[key]) - sum(between[key])/len(between[key])
                weights[key] *= .25 + 2 * max(0., separation)
    # A wide CSV cannot outweigh all normalized fields by column count alone.
    raw_total = sum(w for k,w in weights.items() if k.startswith("raw_data/"))
    if raw_total > 2.:
        for key in weights:
            if key.startswith("raw_data/"):
                weights[key] *= 2. / raw_total
    return weights


def compare(query, example, weights):
    shared = query.keys() | example.keys()
    weights = {k: weights.get(k, base_weight(k)) for k in shared}
    raw_total = sum(w for k,w in weights.items() if k.startswith("raw_data/"))
    if raw_total > 2.:
        for key in weights:
            if key.startswith("raw_data/"):
                weights[key] *= 2. / raw_total
    denominator = sum(weights.values())
    if not denominator:
        return 0., []
    contributions = [(k, weights.get(k, base_weight(k)) * field_similarity(query.get(k), example.get(k))) for k in shared]
    score = sum(value for _,value in contributions) / denominator
    # Dates, balances, amounts, currencies alone are insufficient account evidence.
    semantic = [k for k,value in contributions if value > 0 and
                (k in ("description","counterparty","payment_method","payment_type","category","note","tags","reference")
                 or (k.startswith("raw_data/") and query.get(k,{}).get("kind") == "text"))]
    if not semantic:
        return 0., []
    return score, [k for k,value in sorted(contributions, key=lambda pair: (-pair[1],pair[0])) if value > 0][:6]


def load_examples(path):
    """Only confirmed, unchanged decisions; no rules or automatic labels."""
    with database.connect(path) as db:
        rows = db.execute("""SELECT r.*, d.template AS learned_template, d.features_json,
            d.source AS learned_source, d.currency AS learned_currency, d.direction AS learned_direction
            FROM decisions d JOIN records r USING(record_id)
            WHERE r.status IN ('resolved','synced') AND NOT EXISTS
            (SELECT 1 FROM event_members m WHERE m.record_id=r.record_id)
            ORDER BY r.record_id""").fetchall()
    examples = []
    for row in rows:
        if not row["features_json"]:
            continue
        snapshot = json.loads(row["features_json"])
        postings = json.loads(row["accounting_json"] or "[]")
        if snapshot["signature"] != canonical(postings):
            continue
        if row["source"] != row["learned_source"] or row["currency"] != row["learned_currency"]:
            continue
        if direction(row) != row["learned_direction"]:
            continue
        # Parsed-data edits invalidate the old feature snapshot until rebuilding.
        if snapshot["fields"] != extract_features(row):
            continue
        examples.append({"id": row["record_id"], "source": row["source"], "currency": row["currency"],
                         "direction": direction(row), "template": row["learned_template"], "snapshot": snapshot})
    return examples


def direction(row):
    value = number(row["amount"])
    return "unknown" if value is None else ("positive" if Decimal(value) > 0 else "negative" if Decimal(value) < 0 else "zero")


def predict(path, row, examples, profiles=None):
    pool = [e for e in examples if e["source"] == row["source"]]
    candidates = [e for e in pool if e["currency"] == row["currency"] and
                  e["direction"] == direction(row) and e["id"] != row["record_id"]]
    weights = profiles[row["source"]] if profiles is not None and row["source"] in profiles else fit_weights(pool)
    query = extract_features(row)
    votes, support, evidence = defaultdict(float), Counter(), defaultdict(Counter)
    neighbors = []
    for example in candidates:
        score, fields = compare(query, example["snapshot"]["fields"], weights)
        if score >= .65:
            neighbors.append((score, example["template"], fields))
    # Vote in the nearest neighborhood, retaining ties so ambiguity cannot be
    # hidden by record order or an arbitrary top-k cutoff.
    nearest = max((score for score,_,_ in neighbors), default=0.)
    for score, label, fields in neighbors:
        if score >= nearest - .08:
            votes[label] += score
            support[label] += 1
            evidence[label].update(fields)
    if not votes:
        return None
    best = max(votes, key=votes.get)
    agreement = votes[best] / sum(votes.values())
    if support[best] < 3 or agreement < .8:
        return None
    snapshot = next(e["snapshot"] for e in candidates if e["template"] == best)
    from .automation import simple_record
    balanced = snapshot["mode"] == "balanced" and simple_record(row)
    postings = [dict(account=account, currency=currency,
                     amount=str(abs(Decimal(row["amount"])) * sign) if balanced else "")
                for account,currency,sign in snapshot["layout"]]
    # Cross-currency structures only transfer to records with the same currencies.
    currencies = {f["value"].upper() for k,f in query.items()
                  if k in ("currency","source_currency","target_currency","fee_currency")}
    if any(p["currency"] not in currencies for p in postings):
        return None
    if balanced:
        try:
            database.validate_postings(path, postings, row["transaction_date"])
        except ValueError:
            return None
    else:
        accounts = {r["name"]: r for r in database.get_accounts(path)}
        for posting in postings:
            account = accounts.get(posting["account"])
            if not account or account["open_date"] > row["transaction_date"]:
                return None
            if account["currency"] and posting["currency"] not in account["currency"].split(","):
                return None
    fields = [key for key,_ in evidence[best].most_common(5)]
    return {"kind": "learned", "postings": postings, "accounts_only": not balanced,
            "reason": f"Learned from {support[best]} similar confirmed decisions · {agreement:.0%} weighted agreement (not a probability)",
            "matched_fields": fields, "model_version": VERSION}


def training_summary(path):
    sources = defaultdict(list)
    for example in load_examples(path):
        sources[example["source"]].append(example)
    result = []
    for source, examples in sorted(sources.items()):
        weights = fit_weights(examples)
        result.append({"source": source, "count": len(examples),
                       "fields": sorted(weights, key=lambda key: (-weights[key], key))[:10]})
    return result
