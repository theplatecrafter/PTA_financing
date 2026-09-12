from pathlib import Path
p = Path('importer/database.py')
s = p.read_text()
s += '''

def validate_postings(path, postings, transaction_date=None):
    from .validation import validate
    validate(postings, {r['name']: r for r in get_accounts(path)}, transaction_date)
'''
s = s.replace('    accounting_json = json.dumps(accounting, ensure_ascii=False) if accounting is not None else None\n    with connect(path) as db:', '''    if status == "resolved":
        validate_postings(path, accounting)
    accounting_json = json.dumps(accounting, ensure_ascii=False) if accounting is not None else None
    with connect(path) as db:''')
s = s.replace('            existing = json.loads(row[0]) if row[0] else []', '''            if db.execute("SELECT 1 FROM event_members WHERE record_id=?", (record_id,)).fetchone():
                raise ValueError("Review linked events individually to keep their postings consistent.")
            existing = json.loads(row[0]) if row[0] else []''')
s = s.replace('                accounting_json = json.dumps(existing + postings, ensure_ascii=False)', '''                if status == 'resolved':
                    validate_postings(path, existing + postings)
                accounting_json = json.dumps(existing + postings, ensure_ascii=False)''')
p.write_text(s)
p = Path('importer/app.py')
s = p.read_text().replace('from . import database, ledger', 'from . import database, ledger, automation')
s = s.replace('    database.initialize_database(app.config["DB_PATH"])', '    database.initialize_database(app.config["DB_PATH"])\n    automation.initialize(app.config["DB_PATH"])')
s = s.replace('request.args.get("tab", "search")', 'request.args.get("tab", "stats")')
s = s.replace('        return {"accounts": accounts,', '''        with database.connect(app.config["DB_PATH"]) as db:
            counts = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM records GROUP BY status")}
        return {"counts": counts, "accounts": accounts,''')
s = s.replace('        if tab == "files":', '''        if tab == "rules":
            configured = automation.rules(app.config["DB_PATH"])
            editing = next((r for r in configured if r['id'] == request.args.get('edit', type=int)), None)
            with database.connect(app.config["DB_PATH"]) as db:
                history = db.execute("SELECT * FROM automation_log ORDER BY id DESC LIMIT 30").fetchall()
                learned = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                setting = db.execute("SELECT value FROM settings WHERE key='learning_enabled'").fetchone()
            return render_template("rules.html", tab=tab, rules=configured, editing=editing,
                preview=automation.preview(app.config["DB_PATH"]), history=history, learned=learned,
                learning_enabled=not setting or setting[0] != 'false', parsers=PARSERS)
        if tab == "files":''')
s = s.replace('        return render_template("index.html", tab=tab,', '        return render_template("index.html", suggestion=automation.suggestion(app.config["DB_PATH"], selected), tab=tab,')
s = s.replace('        postings = _postings_from_form() if status in ("resolved", "pending") else None\n        try:', '        try:\n            postings = _postings_from_form() if status in ("resolved", "pending") else None', 1)
s = s.replace('                database.update_record(app.config["DB_PATH"], record_id, status, postings)', '''                if status == "resolved":
                    database.validate_postings(app.config["DB_PATH"], postings, database.get_record(app.config["DB_PATH"], record_id)["transaction_date"])
                database.update_record(app.config["DB_PATH"], record_id, status, postings)
                if status == "resolved":
                    automation.learn(app.config["DB_PATH"], database.get_record(app.config["DB_PATH"], record_id), postings)''')
s = s.replace('        except ValueError as error:\n            flash(str(error), "error")\n        return_tab', '''        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("index", tab=request.form.get("return_tab", "review"), record=record_id))
        return_tab''',1)
s = s.replace('        postings = _postings_from_form() if status in ("resolved", "pending") else None\n        database.update_group(app.config["DB_PATH"], group_id, status, postings)', '''        try:
            postings = _postings_from_form() if status in ("resolved", "pending") else None
            database.update_group(app.config["DB_PATH"], group_id, status, postings)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("index", tab="search", group=group_id))''')
s = s.replace('        selected = [record_id] + request.form.getlist("linked_record_id")', '        group_id = None\n        selected = [record_id] + [r for r in request.form.getlist("linked_record_id") if r]')
s = s.replace('        exported = ledger.export_all(app.config["DB_PATH"], app.config["ACCOUNTS_PATH"], app.config["POSTS_PATH"])', '''        try:
            exported = ledger.export_all(app.config["DB_PATH"], app.config["ACCOUNTS_PATH"], app.config["POSTS_PATH"])
        except ValueError as error:
            flash(f"Export stopped: {error}", "error")
            return redirect(url_for("index", tab="sync"))''')
s = s.replace('        temporary = ROOT / "state" / "uploads" / upload.filename\n        temporary.parent.mkdir(parents=True, exist_ok=True)', '''        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(suffix=".csv", delete=False) as temporary_file:
            temporary = Path(temporary_file.name)''')
s = s.replace('            new_count, duplicate_count = database.add_records(app.config["DB_PATH"], records)', '''            new_count, duplicate_count = database.add_records(app.config["DB_PATH"], records)
            applied = automation.apply(app.config["DB_PATH"], {r.record_id for r in records})
            flash(f"{applied} transaction(s) resolved by automatic rules. Review their history in Rules.", "success")''')
s = s.replace('            flash(f"Import failed: {error}", "error")\n        return redirect', '            flash(f"Import failed: {error}", "error")\n        finally:\n            temporary.unlink(missing_ok=True)\n        return redirect')
s = s.replace('    return app', '''
    @app.post("/rules/save")
    def save_rule():
        try:
            automation.save_rule(app.config["DB_PATH"], request.form)
            flash("Rule saved. Preview below before applying to pending transactions.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index", tab="rules"))

    @app.post("/rules/<int:rule_id>/toggle")
    def toggle_rule(rule_id):
        with database.connect(app.config["DB_PATH"]) as db:
            db.execute("UPDATE rules SET enabled=1-enabled WHERE id=?", (rule_id,))
        return redirect(url_for("index", tab="rules"))

    @app.post("/rules/<int:rule_id>/delete")
    def delete_rule(rule_id):
        with database.connect(app.config["DB_PATH"]) as db:
            db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        flash("Rule deleted. Existing decisions and history are preserved.", "success")
        return redirect(url_for("index", tab="rules"))

    @app.post("/rules/apply")
    def apply_rules():
        selected = set(request.form.getlist("record_id"))
        count = automation.apply(app.config["DB_PATH"], selected)
        flash(f"Resolved {count} selected transaction(s). Export when you are ready.", "success")
        return redirect(url_for("index", tab="rules"))

    @app.post("/rules/history/<int:log_id>/undo")
    def undo_rule(log_id):
        try:
            automation.undo(app.config["DB_PATH"], log_id)
            flash("Automatic decision undone. The transaction is pending again.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index", tab="rules"))

    @app.post("/learning")
    def learning():
        with database.connect(app.config["DB_PATH"]) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('learning_enabled',?)",
                       ('true' if request.form.get('enabled') else 'false',))
            if request.form.get('clear'):
                db.execute("DELETE FROM decisions")
        flash("Learning preferences saved.", "success")
        return redirect(url_for("index", tab="rules"))

    return app''')
s = s.replace('return [(account["name"], "  " * (account["name"].count(":")) + account["name"].split(":")[-1]) for account in accounts]', 'return [(account["name"], account["name"]) for account in accounts]')
s = s.replace('    return [{"account": account.strip(), "amount": amount.strip(), "currency": currency.strip()} for account, amount, currency in zip(accounts, amounts, currencies) if account.strip() and amount.strip() and currency.strip()]', '''    postings = []
    for account, amount, currency in zip(accounts, amounts, currencies):
        if not account.strip() and not amount.strip():
            continue
        if not all(v.strip() for v in (account, amount, currency)):
            raise ValueError("Complete the account, amount, and currency on each posting.")
        postings.append({"account": account.strip(), "amount": amount.strip(), "currency": currency.strip()})
    return postings''')
p.write_text(s)
p = Path('importer/ledger.py')
s = p.read_text().replace('    export_accounts(db_path, accounts_path)', '''    for row in database.list_records(db_path, "all"):
        if row["status"] in ("resolved", "synced"):
            try:
                database.validate_postings(db_path, __import__("json").loads(row["accounting_json"] or "[]"), row["transaction_date"])
            except ValueError as error:
                raise ValueError(f"{row['description'] or row['record_id']}: {error}")
    export_accounts(db_path, accounts_path)''')
# Match the actual existing record marker format and preserve group namespaces.
s = s.replace('(?:record|group):(?P<key>[^\n]+)', '(?P<key>[^\n]+)')
p.write_text(s)

