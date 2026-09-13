from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode, parse_qsl

from flask import abort, Flask, flash, redirect, render_template, request, url_for, jsonify

from . import database, ledger, automation, categorization, filtering, learning as learning_model
from .storage import ensure_saving_dir

ROOT = Path(__file__).resolve().parent.parent
PARSERS = {
    "paypay": "paypay",
    "sdfcu": "sdfcu",
    "sumitomo": "sumitomo",
    "sumitomo_credit_card": "sumitomo_credit_card",
    "wise": "wise",
}
MANUAL_FIELDS = (
    ("date", "Date", "date"),
    ("description", "Description", "text"),
    ("amount", "Amount", "text"),
    ("currency", "Currency", "text"),
    ("counterparty", "Counterparty", "text"),
    ("category", "Category", "text"),
    ("payment_method", "Payment method", "text"),
    ("note", "Note", "text"),
    ("tags", "Tags (comma-separated)", "text"),
    ("reference", "Reference", "text"),
    ("balance", "Balance", "text"),
    ("balance_currency", "Balance currency", "text"),
    ("fee_amount", "Fee amount", "text"),
    ("fee_currency", "Fee currency", "text"),
    ("payment_type", "Payment type", "text"),
    ("installment_number", "Installment number", "number"),
    ("payment_amount", "Payment amount", "text"),
    ("source_amount", "Source amount", "text"),
    ("source_currency", "Source currency", "text"),
    ("target_amount", "Target amount", "text"),
    ("target_currency", "Target currency", "text"),
    ("exchange_rate", "Exchange rate", "text"),
    ("conversion_date", "Conversion date", "date"),
    ("time", "Time", "time"),
    ("completed_at", "Completed at", "datetime-local"),
)


def create_app(db_path: Path | None = None, ledger_path: Path | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = "local-importer"
    app.config["DB_PATH"] = Path(db_path or os.environ.get("IMPORTER_DB_PATH", ROOT / "state" / "importer.db"))
    app.config["ACCOUNTS_PATH"] = ROOT / "accounts.beancount"
    app.config["POSTS_PATH"] = ROOT / "posts.beancount"
    database.initialize_database(app.config["DB_PATH"])
    automation.initialize(app.config["DB_PATH"])
    app.config["LEDGER_PATH"] = Path(ledger_path or ROOT / "main.beancount")
    scan_cache = {"model": None, "rules": None}
    try:
        from fava.application import create_app as create_fava
        from werkzeug.middleware.dispatcher import DispatcherMiddleware
        reports = create_fava([app.config["LEDGER_PATH"]])
        app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/fava": reports})
        app.config["FAVA_AVAILABLE"] = True
    except ImportError:
        app.config["FAVA_AVAILABLE"] = False

    filter_keys = {"condition_field", "condition_operator", "condition_pattern", "match_mode", "direction", "minimum", "maximum", "source", "currency", "posting_account"}

    def filter_suffix(form):
        return urlencode([(k, v) for k, values in form.lists() if k in filter_keys for v in values])

    def saved_filter_suffix():
        return "&" + urlencode([(k,v) for k,v in parse_qsl(request.form.get("return_filters", ""), keep_blank_values=True) if k in filter_keys])

    def page_number(value):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 1

    def page_url(tab, page, **values):
        args = request.args.to_dict(flat=False)
        args.update({key: [str(value)] for key, value in values.items()})
        args["tab"] = [tab]
        args["page"] = [str(page)]
        return url_for("index", **args)

    def prediction_page(run=False, record_id=None, submitted=None):
        if run:
            scan_cache["model"] = automation.scan_pending(app.config["DB_PATH"])
        scan = scan_cache["model"]
        items = scan["results"] if scan else []
        position = next((i for i, item in enumerate(items) if item["record"]["record_id"] == (record_id or request.args.get("record"))), 0)
        item = items[position] if items else None
        if item and submitted is not None:
            item = dict(item, postings=submitted)
        with database.connect(app.config["DB_PATH"]) as db:
            setting = db.execute("SELECT value FROM settings WHERE key='learning_enabled'").fetchone()
        return render_template("predictions.html", tab="predictions", scan=scan,
            item=item, position=position,
            training=learning_model.training_summary(app.config["DB_PATH"]),
            learning_enabled=not setting or setting[0] != 'false')

    def remove_cached_record(record_id):
        for key in ("model", "rules"):
            scan = scan_cache[key]
            if not scan:
                continue
            scan["results"] = [item for item in scan["results"] if item["record"]["record_id"] != record_id]
            if "total" in scan:
                scan["total"] = max(0, scan["total"] - 1)

    @app.context_processor
    def shared_context():
        accounts = database.get_accounts(app.config["DB_PATH"])
        with database.connect(app.config["DB_PATH"]) as db:
            counts = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM records GROUP BY status")}
        return {"rule_conditions": automation.conditions, "counts": counts, "accounts": accounts, "account_options": _account_options(accounts), "json": json, "row_keys": lambda row, keys: {key: row[key] for key in keys}, "manual_fields": MANUAL_FIELDS, "today": date.today().isoformat()}

    @app.get("/")
    def index():
        tab = request.args.get("tab", "stats")
        if tab not in {"stats", "review", "search", "rules", "accounts", "sync", "manual", "files", "database", "reports", "predictions"}:
            abort(404)
        if tab == "reports":
            return render_template("reports.html", tab=tab, available=app.config["FAVA_AVAILABLE"])
        if tab == "predictions":
            return prediction_page(request.args.get("run") == "1")
        if tab == "rules":
            configured = automation.rules(app.config["DB_PATH"])
            if scan_cache["rules"] is None:
                scan_cache["rules"] = automation.scan_rules(app.config["DB_PATH"])
            rule_scan = scan_cache["rules"]
            rule_position = next((i for i, item in enumerate(rule_scan) if item["record"]["record_id"] == request.args.get("record")), 0)
            rule_item = rule_scan[rule_position] if rule_scan else None
            editing = next((r for r in configured if r['id'] == request.args.get('edit', type=int)), None)
            seed = database.get_record(app.config["DB_PATH"], request.args.get("from_record", ""))
            if not editing and seed and automation.simple_record(seed) and not database.get_group_id(app.config["DB_PATH"], seed["record_id"]):
                postings = json.loads(seed["accounting_json"] or "[]")
                if len(postings) == 2:
                    editing = dict(id=None, name=seed["description"], priority=100, enabled=True, mode="suggest", definition=dict(
                        source=seed["source"], currency=seed["currency"], field="description", operator="equals",
                        pattern=seed["description"], direction="positive" if Decimal(seed["amount"]) > 0 else "negative",
                        source_account=postings[0]["account"], target_account=postings[1]["account"],
                        source_sign="positive" if Decimal(postings[0]["amount"]) > 0 else "negative"))
            with database.connect(app.config["DB_PATH"]) as db:
                sources = sorted({r[0] for r in db.execute("SELECT DISTINCT source FROM records")} | set(PARSERS) | {"manual"})
                history = db.execute("SELECT * FROM automation_log ORDER BY id DESC LIMIT 30").fetchall()
                learned = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                setting = db.execute("SELECT value FROM settings WHERE key='learning_enabled'").fetchone()
            return render_template("rules.html", tab=tab, rules=configured, editing=editing,
                preview=rule_scan, rule_scan=rule_scan, rule_item=rule_item, rule_position=rule_position, history=history, learned=learned,
                learning_enabled=not setting or setting[0] != 'false', parsers=sources, match_fields=[("*", "Any field")] + automation.available_fields(app.config["DB_PATH"]), operators=automation.OPERATORS)
        if tab == "files":
            return render_template(
                "files.html",
                tab=tab,
                accounts_text=app.config["ACCOUNTS_PATH"].read_text(encoding="utf-8"),
                posts_text=app.config["POSTS_PATH"].read_text(encoding="utf-8"),
            )
        status = request.args.get("status", "all" if tab == "search" else "pending")
        if tab == "review":
            status = "pending"
        query = request.args.get("q", "")
        records = [] if tab == "search" else (database.review_rows(app.config["DB_PATH"]) if tab == "review" else database.list_records(app.config["DB_PATH"], status, query))
        filters = {}
        search_page_number = page_number(request.args.get("page"))
        search_page_count = 1
        if tab == "search":
            try:
                filters = filtering.definition(request.args)
                records, search_total = filtering.search_page(app.config["DB_PATH"], request.args, search_page_number)
                search_page_count = max(1, (search_total + 99) // 100)
                # Match individual source records before collapsing linked events.
                seen, filtered = set(), []
                for row in records:
                    key = database.get_group_id(app.config["DB_PATH"], row["record_id"]) or row["record_id"]
                    if key not in seen:
                        filtered.append(row)
                        seen.add(key)
                records = filtered
            except ValueError as error:
                records = []
                flash(str(error), "error")
        selected_id = request.args.get("record")
        selected_group_id = request.args.get("group", type=int)
        selected = database.get_group_representative(app.config["DB_PATH"], selected_group_id) if selected_group_id else (database.get_record(app.config["DB_PATH"], selected_id) if selected_id else (records[0] if records and tab == "review" else None))
        held = database.list_records(app.config["DB_PATH"], "held")
        review_position = (next((index for index, record in enumerate(records) if record["record_id"] == (selected["record_id"] if selected else None)), 0) + 1) if tab == "review" and records else 0
        next_record_id = records[review_position]["record_id"] if tab == "review" and review_position < len(records) else ""
        linked_members = []
        record_groups = {}
        if tab == "search":
            with database.connect(app.config["DB_PATH"]) as db:
                record_groups = {row["record_id"]: row["group_id"] for row in db.execute("SELECT record_id, group_id FROM event_members")}
        if selected and not selected_group_id:
            selected_group_id = record_groups.get(selected["record_id"]) if tab == "search" else database.get_group_id(app.config["DB_PATH"], selected["record_id"])
        if selected_group_id:
            linked_members = database.get_group_members(app.config["DB_PATH"], selected_group_id)
        elif tab == "review" and selected:
            selected_group_id = database.get_group_id(app.config["DB_PATH"], selected["record_id"])
            if selected_group_id:
                linked_members = database.get_group_members(app.config["DB_PATH"], selected_group_id)
        if tab == "database":
            table = request.args.get("table", "records")
            database_page = page_number(request.args.get("page"))
            try:
                rows, columns, primary_keys, database_total = database.editor_table(app.config["DB_PATH"], table, database_page)
            except ValueError:
                table = "records"
                rows, columns, primary_keys, database_total = database.editor_table(app.config["DB_PATH"], table, database_page)
            return render_template("database.html", tab=tab, table=table, tables=database.EDITOR_TABLES, rows=rows, columns=columns, primary_keys=primary_keys, page=database_page, page_count=max(1, (database_total + 99) // 100), page_url=lambda value: page_url("database", value, table=table))
        quick_hints = automation.account_hints(app.config["DB_PATH"], selected)
        if linked_members:
            for role, member in zip(("source","target"), linked_members):
                member_hints = automation.account_hints(app.config["DB_PATH"], member)
                if "source" in member_hints:
                    quick_hints[role] = member_hints["source"]
                if "fee" in member_hints:
                    quick_hints.setdefault("fee", member_hints["fee"])
        return render_template("index.html", filters=filters, filter_suffix=filter_suffix(request.args),
            match_fields=[("*", "Any field")] + automation.available_fields(app.config["DB_PATH"]),
            operators=automation.OPERATORS, hints=quick_hints, suggestion=automation.suggestion(app.config["DB_PATH"], selected), tab=tab, status=status, query=query, records=records, selected=selected, held=held, linked_members=linked_members, selected_group_id=selected_group_id, record_groups=record_groups, review_position=review_position, next_record_id=next_record_id, selected_group_id_query=request.args.get("group", ""), groups=database.get_groups(app.config["DB_PATH"]), page=search_page_number, page_count=search_page_count, page_url=lambda value: page_url("search", value))

    @app.post("/predictions/run")
    def run_predictions():
        return prediction_page(True)

    @app.post("/rules/filter-preview")
    def filter_preview():
        try:
            rows = filtering.search(app.config["DB_PATH"], request.form)
            return jsonify(total=len(rows), records=[
                {k: r[k] for k in ("record_id", "description", "source", "transaction_date", "amount", "currency", "status")}
                for r in rows])
        except ValueError as error:
            return jsonify(error=str(error)), 400

    @app.post("/records/<record_id>/categorize-preview")
    def categorize_preview(record_id):
        row = database.get_record(app.config["DB_PATH"], record_id)
        if not row:
            abort(404)
        members = database.get_group_members(app.config["DB_PATH"], database.get_group_id(app.config["DB_PATH"], record_id)) or [row]
        try:
            postings = categorization.quick_postings(app.config["DB_PATH"], request.form, min(r["transaction_date"] for r in members))
            return jsonify(postings=postings)
        except ValueError as error:
            return jsonify(error=str(error)), 400

    @app.post("/records/<record_id>/save")
    def save_record(record_id: str):
        if not database.get_record(app.config["DB_PATH"], record_id):
            abort(404)
        status = request.form.get("status", "resolved")
        try:
            postings = _postings_from_form() if status in ("resolved", "pending") else None
            group_id = database.get_group_id(app.config["DB_PATH"], record_id)
            if group_id:
                database.update_group(app.config["DB_PATH"], group_id, status, postings)
                flash(f"Event group #{group_id} saved as one database transaction.", "success")
            else:
                if status == "resolved":
                    database.validate_postings(app.config["DB_PATH"], postings, database.get_record(app.config["DB_PATH"], record_id)["transaction_date"])
                database.update_record(app.config["DB_PATH"], record_id, status, postings)
                if status == "resolved":
                    automation.learn(app.config["DB_PATH"], database.get_record(app.config["DB_PATH"], record_id), postings)
                flash("Record saved in the database.", "success")
            remove_cached_record(record_id)
        except ValueError as error:
            flash(str(error), "error")
            if request.form.get("return_tab") == "predictions":
                submitted = [dict(account=a,amount=m,currency=c) for a,m,c in zip(
                    request.form.getlist("posting_account"), request.form.getlist("posting_amount"),
                    request.form.getlist("posting_currency"))]
                return prediction_page(True, record_id, submitted)
            return redirect(url_for("index", tab=request.form.get("return_tab", "review"), record=record_id) + saved_filter_suffix())
        return_tab = request.form.get("return_tab", "review")
        if return_tab == "predictions":
            return redirect(url_for("index", tab="predictions", record=request.form.get("next_record","")))
        next_record = request.form.get("next_record") or (record_id if return_tab != "review" else "")
        return redirect(url_for("index", tab=return_tab, status=request.form.get("return_status", "all"), q=request.form.get("return_q", ""), record=next_record) + (saved_filter_suffix() if return_tab == "search" else ""))

    @app.post("/groups/<int:group_id>/save")
    def save_group(group_id: int):
        status = request.form.get("status", "pending")
        try:
            postings = _postings_from_form() if status in ("resolved", "pending") else None
            database.update_group(app.config["DB_PATH"], group_id, status, postings)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("index", tab="search", group=group_id))
        flash(f"Linked event group #{group_id} saved.", "success")
        return redirect(url_for("index", tab="search", status=request.form.get("return_status", "all"), q=request.form.get("return_q", ""), group=group_id) + saved_filter_suffix())

    @app.post("/records/bulk-save")
    def save_records_bulk():
        record_ids = request.form.getlist("selected_record_id")
        status = request.form.get("status", "pending")
        postings = _bulk_postings_from_form()
        try:
            database.append_accounting(app.config["DB_PATH"], record_ids, postings, status)
            if status == "resolved":
                for record_id in set(record_ids):
                    row = database.get_record(app.config["DB_PATH"], record_id)
                    automation.learn(app.config["DB_PATH"], row, json.loads(row["accounting_json"] or "[]"))
            flash(f"Saved accounting postings for {len(record_ids)} record(s).", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index", tab="search", status=request.form.get("return_status", "all"), q=request.form.get("return_q", "")) + saved_filter_suffix())

    @app.post("/records/<record_id>/keep")
    def keep_record(record_id: str):
        return redirect(url_for("index", tab=request.form.get("return_tab", "review"), status=request.form.get("return_status", "pending"), q=request.form.get("return_q", ""), record=request.form.get("next_record", record_id)))

    @app.post("/records/<record_id>/link")
    def link_record(record_id: str):
        group_id = None
        selected = [record_id] + [r for r in request.form.getlist("linked_record_id") if r]
        try:
            group_id = database.link_records(app.config["DB_PATH"], selected)
            flash(f"Linked event group #{group_id} created. Source records were preserved.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index", tab=request.form.get("return_tab", "review"), status=request.form.get("return_status", "pending"), q=request.form.get("return_q", ""), group=group_id) + saved_filter_suffix())

    @app.post("/groups/<int:group_id>/unlink")
    def unlink_group(group_id: int):
        database.unlink_group(app.config["DB_PATH"], group_id)
        flash(f"Event group #{group_id} unlinked; its source records are pending again.", "success")
        return redirect(url_for("index", tab="review"))

    @app.post("/accounts/save")
    def save_account():
        try:
            database.save_account(app.config["DB_PATH"], request.form["name"], request.form.get("currency", ""), request.form.get("description", ""), request.form.get("open_date"), request.form.get("original_name") or None, ledger_paths=[app.config["LEDGER_PATH"], app.config["ACCOUNTS_PATH"], app.config["POSTS_PATH"]])
            flash("Account saved.", "success")
        except (ValueError, OSError, sqlite3.Error) as error:
            flash(str(error), "error")
        return redirect(url_for("index", tab="accounts"))

    @app.post("/accounts/merge")
    def merge_accounts():
        try:
            database.merge_account(app.config["DB_PATH"], request.form["source"], request.form["destination"], request.form["name"], request.form.get("currency", ""), ledger_paths=[app.config["LEDGER_PATH"], app.config["ACCOUNTS_PATH"], app.config["POSTS_PATH"]])
            flash("Accounts merged.", "success")
        except (ValueError, OSError, sqlite3.Error) as error:
            flash(str(error), "error")
        return redirect(url_for("index", tab="accounts"))

    @app.post("/database/<table>/update")
    def database_update(table: str):
        _, columns, primary_keys = database.editor_table(app.config["DB_PATH"], table)
        original = {column: request.form.get(f"original_{column}", "") for column in primary_keys}
        values = {column: request.form.get(column, "") for column in columns}
        try:
            database.editor_update(app.config["DB_PATH"], table, original, values)
            flash("Database row updated.", "success")
        except (ValueError, sqlite3.Error) as error:
            flash(f"Could not update row: {error}", "error")
        return redirect(url_for("index", tab="database", table=table, page=page_number(request.form.get("page"))))

    @app.post("/database/<table>/add")
    def database_add(table: str):
        _, columns, _ = database.editor_table(app.config["DB_PATH"], table)
        try:
            database.editor_insert(app.config["DB_PATH"], table, {column: request.form.get(column, "") for column in columns})
            flash("Database row added.", "success")
        except (ValueError, sqlite3.Error) as error:
            flash(f"Could not add row: {error}", "error")
        return redirect(url_for("index", tab="database", table=table, page=page_number(request.form.get("page"))))

    @app.post("/database/<table>/delete")
    def database_delete(table: str):
        try:
            keys = [json.loads(value) for value in request.form.getlist("selected")]
            deleted = database.editor_delete(app.config["DB_PATH"], table, keys)
            flash(f"Deleted {deleted} database row(s).", "success")
        except (ValueError, sqlite3.Error) as error:
            flash(f"Could not delete rows: {error}", "error")
        return redirect(url_for("index", tab="database", table=table, page=page_number(request.form.get("page"))))

    @app.post("/database/<table>/duplicate")
    def database_duplicate(table: str):
        try:
            keys = [json.loads(value) for value in request.form.getlist("selected")]
            duplicated = database.editor_duplicate(app.config["DB_PATH"], table, keys)
            flash(f"Duplicated {duplicated} database row(s).", "success")
        except (ValueError, sqlite3.Error) as error:
            flash(f"Could not duplicate rows: {error}", "error")
        return redirect(url_for("index", tab="database", table=table, page=page_number(request.form.get("page"))))

    @app.post("/execute")
    def execute():
        try:
            exported = ledger.export_all(app.config["DB_PATH"], app.config["ACCOUNTS_PATH"], app.config["POSTS_PATH"])
        except ValueError as error:
            flash(f"Export stopped: {error}", "error")
            return redirect(url_for("index", tab="sync"))
        database.set_synced(app.config["DB_PATH"], exported)
        flash(f"Exported {len(exported)} database transaction(s) and updated account declarations.", "success")
        return redirect(url_for("index", tab="sync"))

    @app.post("/sync")
    def sync():
        accounts, records = ledger.import_beancount(app.config["DB_PATH"], app.config["ACCOUNTS_PATH"], app.config["POSTS_PATH"])
        flash(f"Imported {accounts} account declaration(s) and found {records} importer transaction marker(s).", "success")
        return redirect(url_for("index", tab="sync"))

    @app.post("/import")
    def import_csv():
        upload = request.files.get("file")
        source = request.form.get("source", "")
        if not upload or not upload.filename or source not in PARSERS:
            flash("Choose a CSV file and parser.", "error")
            return redirect(url_for("index", tab="sync"))
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(suffix=".csv", dir=ensure_saving_dir(), delete=False) as temporary_file:
            temporary = Path(temporary_file.name)
        upload.save(temporary)
        try:
            parser = importlib.import_module(f"importer.{PARSERS[source]}")
            file_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
            records = list(parser.parse(temporary))
            existing_ids = {r['record_id'] for r in database.list_records(app.config["DB_PATH"], "all")}
            new_count, duplicate_count = database.add_records(app.config["DB_PATH"], records)
            applied = automation.apply(app.config["DB_PATH"], {r.record_id for r in records} - existing_ids)
            flash(f"{applied} transaction(s) resolved by automatic rules. Review their history in Rules.", "success")
            with database.connect(app.config["DB_PATH"]) as db:
                db.execute("INSERT OR IGNORE INTO imports(source,source_file,file_hash,imported_at,record_count) VALUES(?,?,?,datetime('now'),?)", (source, upload.filename, file_hash, len(records)))
            flash(f"Imported {new_count} new record(s); {duplicate_count} exact duplicate(s) ignored.", "success")
        except Exception as error:
            flash(f"Import failed: {error}", "error")
        finally:
            temporary.unlink(missing_ok=True)
        scan_cache["model"] = None
        scan_cache["rules"] = None
        return redirect(url_for("index", tab="sync"))

    @app.post("/manual")
    def manual():
        from uuid import uuid4
        record_id = request.form.get("record_id") or f"manual:{uuid4()}"
        payload = {}
        payload = {
            "record_id": record_id,
            "source": "manual",
            "source_file": "manual",
            "source_row": 0,
            "source_id": None,
            "raw_data": {},
        }
        for field, _, field_type in MANUAL_FIELDS:
            value = request.form.get(field, "").strip()
            if field == "date":
                value = value or date.today().isoformat()
            if field == "tags":
                value = [tag.strip() for tag in value.split(",") if tag.strip()]
            elif not value:
                value = None
            payload[field] = value
        description = payload["description"] or record_id
        with database.connect(app.config["DB_PATH"]) as db:
            db.execute("INSERT INTO records(record_id,source,transaction_date,description,amount,currency,status,payload) VALUES(?,?,?,?,?,?, 'pending', ?)", (record_id, payload["source"], payload["date"], description, payload["amount"], payload["currency"], json.dumps(payload, ensure_ascii=False)))
        return redirect(url_for("index", tab="review", status="pending", record=record_id))

    @app.route("/files/edit", methods=["GET", "POST"])
    def edit_files():
        if request.method == "GET":
            return redirect(url_for("index", tab="files"))
        if request.method == "POST":
            for path, field in ((app.config["ACCOUNTS_PATH"], "accounts_text"), (app.config["POSTS_PATH"], "posts_text")):
                if field in request.form:
                    path.write_text(request.form[field], encoding="utf-8")
            flash("Beancount files saved directly.", "success")
        return redirect(url_for("index", tab="files"))


    @app.post("/rules/save")
    def save_rule():
        try:
            automation.save_rule(app.config["DB_PATH"], request.form)
            flash("Rule saved. Preview below before applying to pending transactions.", "success")
        except ValueError as error:
            flash(str(error), "error")
        scan_cache["rules"] = None
        return redirect(url_for("index", tab="rules"))

    @app.post("/rules/<int:rule_id>/toggle")
    def toggle_rule(rule_id):
        with database.connect(app.config["DB_PATH"]) as db:
            db.execute("UPDATE rules SET enabled=1-enabled WHERE id=?", (rule_id,))
        scan_cache["rules"] = None
        return redirect(url_for("index", tab="rules"))

    @app.post("/rules/<int:rule_id>/delete")
    def delete_rule(rule_id):
        with database.connect(app.config["DB_PATH"]) as db:
            db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        flash("Rule deleted. Existing decisions and history are preserved.", "success")
        scan_cache["rules"] = None
        return redirect(url_for("index", tab="rules"))

    @app.post("/rules/apply")
    def apply_rules():
        selected = set(request.form.getlist("record_id"))
        count = automation.apply(app.config["DB_PATH"], selected)
        scan_cache["rules"] = None
        flash(f"Resolved {count} selected transaction(s). Export when you are ready.", "success")
        return redirect(url_for("index", tab="rules"))

    @app.post("/rules/execute")
    def execute_rules():
        count = automation.apply(app.config["DB_PATH"])
        scan_cache["rules"] = None
        flash(f"Executed automatic rules on {count} transaction(s).", "success")
        return redirect(url_for("index", tab="rules"))

    @app.post("/rules/history/<int:log_id>/undo")
    def undo_rule(log_id):
        try:
            automation.undo(app.config["DB_PATH"], log_id)
            flash("Automatic decision undone. The transaction is pending again.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index", tab="rules"))

    @app.post("/learning/rebuild")
    def rebuild_learning():
        count = automation.rebuild_learning(app.config["DB_PATH"])
        scan_cache["model"] = None
        flash(f"Refreshed parsed-data features for {count} confirmed training example(s). Rules and automatic decisions were not used as labels.", "success")
        return redirect(url_for("index", tab="predictions"))

    @app.post("/learning")
    def learning():
        with database.connect(app.config["DB_PATH"]) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('learning_enabled',?)",
                       ('true' if request.form.get('enabled') else 'false',))
            if request.form.get('clear'):
                db.execute("DELETE FROM decisions")
        flash("Learning preferences saved.", "success")
        scan_cache["model"] = None
        return redirect(url_for("index", tab="predictions"))

    return app


def _account_options(accounts):
    return [(account["name"], account["name"]) for account in accounts]


def _postings_from_form() -> list[dict[str, str]]:
    accounts = request.form.getlist("posting_account")
    amounts = request.form.getlist("posting_amount")
    currencies = request.form.getlist("posting_currency")
    postings = []
    for account, amount, currency in zip(accounts, amounts, currencies):
        if not account.strip() and not amount.strip():
            continue
        if request.form.get("status") != "pending" and not all(v.strip() for v in (account, amount, currency)):
            raise ValueError("Complete the account, amount, and currency on each posting.")
        postings.append({"account": account.strip(), "amount": amount.strip(), "currency": currency.strip()})
    if not (len(accounts) == len(amounts) == len(currencies)):
        raise ValueError("Every posting needs an account, amount, and currency.")
    return postings


def _bulk_postings_from_form() -> list[dict[str, str]]:
    accounts = request.form.getlist("posting_account")
    signs = request.form.getlist("posting_sign")
    currencies = request.form.getlist("posting_currency")
    return [{"account": account.strip(), "sign": sign, "currency": currency.strip()} for account, sign, currency in zip(accounts, signs, currencies) if account.strip() and sign in ("positive", "negative")]


app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "50001")), debug=False)
