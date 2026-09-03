from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from datetime import date
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for

from . import database, ledger

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


def create_app(db_path: Path | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = "local-importer"
    app.config["DB_PATH"] = Path(db_path or ROOT / "state" / "importer.db")
    app.config["ACCOUNTS_PATH"] = ROOT / "accounts.beancount"
    app.config["POSTS_PATH"] = ROOT / "posts.beancount"
    database.initialize_database(app.config["DB_PATH"])

    @app.context_processor
    def shared_context():
        accounts = database.get_accounts(app.config["DB_PATH"])
        return {"accounts": accounts, "account_options": _account_options(accounts), "json": json, "row_keys": lambda row, keys: {key: row[key] for key in keys}, "manual_fields": MANUAL_FIELDS, "today": date.today().isoformat()}

    @app.get("/")
    def index():
        tab = request.args.get("tab", "search")
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
        records = database.search_rows(app.config["DB_PATH"], status, query) if tab == "search" else (database.review_rows(app.config["DB_PATH"]) if tab == "review" else database.list_records(app.config["DB_PATH"], status, query))
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
            try:
                rows, columns, primary_keys = database.editor_table(app.config["DB_PATH"], table)
            except ValueError:
                table = "records"
                rows, columns, primary_keys = database.editor_table(app.config["DB_PATH"], table)
            return render_template("database.html", tab=tab, table=table, tables=database.EDITOR_TABLES, rows=rows, columns=columns, primary_keys=primary_keys)
        return render_template("index.html", tab=tab, status=status, query=query, records=records, selected=selected, held=held, linked_members=linked_members, selected_group_id=selected_group_id, record_groups=record_groups, review_position=review_position, next_record_id=next_record_id, selected_group_id_query=request.args.get("group", ""), groups=database.get_groups(app.config["DB_PATH"]))

    @app.post("/records/<record_id>/save")
    def save_record(record_id: str):
        status = request.form.get("status", "resolved")
        postings = _postings_from_form() if status in ("resolved", "pending") else None
        try:
            group_id = database.get_group_id(app.config["DB_PATH"], record_id)
            if group_id:
                database.update_group(app.config["DB_PATH"], group_id, status, postings)
                flash(f"Event group #{group_id} saved as one database transaction.", "success")
            else:
                database.update_record(app.config["DB_PATH"], record_id, status, postings)
                flash("Record saved in the database.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return_tab = request.form.get("return_tab", "review")
        next_record = request.form.get("next_record") or (record_id if return_tab != "review" else "")
        return redirect(url_for("index", tab=return_tab, status=request.form.get("return_status", "all"), q=request.form.get("return_q", ""), record=next_record))

    @app.post("/groups/<int:group_id>/save")
    def save_group(group_id: int):
        status = request.form.get("status", "pending")
        postings = _postings_from_form() if status in ("resolved", "pending") else None
        database.update_group(app.config["DB_PATH"], group_id, status, postings)
        flash(f"Linked event group #{group_id} saved.", "success")
        return redirect(url_for("index", tab="search", status=request.form.get("return_status", "all"), q=request.form.get("return_q", ""), group=group_id))

    @app.post("/records/bulk-save")
    def save_records_bulk():
        record_ids = request.form.getlist("selected_record_id")
        status = request.form.get("status", "pending")
        postings = _bulk_postings_from_form()
        try:
            database.append_accounting(app.config["DB_PATH"], record_ids, postings, status)
            flash(f"Saved accounting postings for {len(record_ids)} record(s).", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index", tab="search", status=request.form.get("return_status", "all"), q=request.form.get("return_q", "")))

    @app.post("/records/<record_id>/keep")
    def keep_record(record_id: str):
        return redirect(url_for("index", tab=request.form.get("return_tab", "review"), status=request.form.get("return_status", "pending"), q=request.form.get("return_q", ""), record=request.form.get("next_record", record_id)))

    @app.post("/records/<record_id>/link")
    def link_record(record_id: str):
        selected = [record_id] + request.form.getlist("linked_record_id")
        try:
            group_id = database.link_records(app.config["DB_PATH"], selected)
            flash(f"Linked event group #{group_id} created. Source records were preserved.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index", tab=request.form.get("return_tab", "review"), status=request.form.get("return_status", "pending"), q=request.form.get("return_q", ""), group=group_id))

    @app.post("/groups/<int:group_id>/unlink")
    def unlink_group(group_id: int):
        database.unlink_group(app.config["DB_PATH"], group_id)
        flash(f"Event group #{group_id} unlinked; its source records are pending again.", "success")
        return redirect(url_for("index", tab="review"))

    @app.post("/accounts/save")
    def save_account():
        try:
            database.save_account(app.config["DB_PATH"], request.form["name"], request.form.get("currency", ""), request.form.get("description", ""), request.form.get("open_date"), request.form.get("original_name") or None)
            flash("Account saved.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index", tab="accounts"))

    @app.post("/accounts/delete")
    def remove_account():
        try:
            database.delete_account(app.config["DB_PATH"], request.form["name"])
            flash("Account deleted.", "success")
        except ValueError as error:
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
        return redirect(url_for("index", tab="database", table=table))

    @app.post("/database/<table>/add")
    def database_add(table: str):
        _, columns, _ = database.editor_table(app.config["DB_PATH"], table)
        try:
            database.editor_insert(app.config["DB_PATH"], table, {column: request.form.get(column, "") for column in columns})
            flash("Database row added.", "success")
        except (ValueError, sqlite3.Error) as error:
            flash(f"Could not add row: {error}", "error")
        return redirect(url_for("index", tab="database", table=table))

    @app.post("/database/<table>/delete")
    def database_delete(table: str):
        try:
            keys = [json.loads(value) for value in request.form.getlist("selected")]
            deleted = database.editor_delete(app.config["DB_PATH"], table, keys)
            flash(f"Deleted {deleted} database row(s).", "success")
        except (ValueError, sqlite3.Error) as error:
            flash(f"Could not delete rows: {error}", "error")
        return redirect(url_for("index", tab="database", table=table))

    @app.post("/database/<table>/duplicate")
    def database_duplicate(table: str):
        try:
            keys = [json.loads(value) for value in request.form.getlist("selected")]
            duplicated = database.editor_duplicate(app.config["DB_PATH"], table, keys)
            flash(f"Duplicated {duplicated} database row(s).", "success")
        except (ValueError, sqlite3.Error) as error:
            flash(f"Could not duplicate rows: {error}", "error")
        return redirect(url_for("index", tab="database", table=table))

    @app.post("/execute")
    def execute():
        exported = ledger.export_all(app.config["DB_PATH"], app.config["ACCOUNTS_PATH"], app.config["POSTS_PATH"])
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
        temporary = ROOT / "state" / "uploads" / upload.filename
        temporary.parent.mkdir(parents=True, exist_ok=True)
        upload.save(temporary)
        try:
            parser = importlib.import_module(f"importer.{PARSERS[source]}")
            file_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
            records = parser.parse(temporary)
            new_count, duplicate_count = database.add_records(app.config["DB_PATH"], records)
            with database.connect(app.config["DB_PATH"]) as db:
                db.execute("INSERT OR IGNORE INTO imports(source,source_file,file_hash,imported_at,record_count) VALUES(?,?,?,datetime('now'),?)", (source, upload.filename, file_hash, len(records)))
            flash(f"Imported {new_count} new record(s); {duplicate_count} exact duplicate(s) ignored.", "success")
        except Exception as error:
            flash(f"Import failed: {error}", "error")
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

    return app


def _account_options(accounts):
    return [(account["name"], "  " * (account["name"].count(":")) + account["name"].split(":")[-1]) for account in accounts]


def _postings_from_form() -> list[dict[str, str]]:
    accounts = request.form.getlist("posting_account")
    amounts = request.form.getlist("posting_amount")
    currencies = request.form.getlist("posting_currency")
    return [{"account": account.strip(), "amount": amount.strip(), "currency": currency.strip()} for account, amount, currency in zip(accounts, amounts, currencies) if account.strip() and amount.strip() and currency.strip()]


def _bulk_postings_from_form() -> list[dict[str, str]]:
    accounts = request.form.getlist("posting_account")
    signs = request.form.getlist("posting_sign")
    currencies = request.form.getlist("posting_currency")
    return [{"account": account.strip(), "sign": sign, "currency": currency.strip()} for account, sign, currency in zip(accounts, signs, currencies) if account.strip() and sign in ("positive", "negative")]


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
