import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import date
from decimal import Decimal

_test_saving = Path(__file__).resolve().parents[1] / "saving" / "tests"
_test_saving.mkdir(parents=True, exist_ok=True)
_boot = tempfile.TemporaryDirectory(dir=_test_saving)
os.environ["IMPORTER_DB_PATH"] = str(Path(_boot.name) / "boot.db")
from importer import database, automation, ledger, categorization, learning
from importer.app import create_app
from importer.sumitomo_credit_card import parse_foreign_currency

class Workflows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_test_saving)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.db"
        self.app = create_app(self.path)
        self.app.config.update(TESTING=True, ACCOUNTS_PATH=Path(self.tmp.name)/"accounts.bean",
                               POSTS_PATH=Path(self.tmp.name)/"posts.bean")
        self.app.config["ACCOUNTS_PATH"].write_text("")
        self.app.config["POSTS_PATH"].write_text("")
        self.client = self.app.test_client()
        for name in ["Assets:Bank", "Expenses:Food", "Expenses:Other"]:
            database.save_account(self.path, name, "USD", "", "2020-01-01")

    def record(self, rid="bank:1", amount="-25", **payload):
        record = SimpleNamespace(record_id=rid, source="bank", date=date(2026,9,1),
                                 description="Market groceries", amount=Decimal(amount),
                                 currency="USD", **payload)
        database.add_records(self.path, [record])
        return database.get_record(self.path, rid)

    def rule(self, **changes):
        values = dict(name="Groceries", priority="100", enabled="on", mode="automatic",
                      source="bank", currency="USD", field="description", operator="contains",
                      pattern="market", direction="negative", minimum="", maximum="",
                      source_account="Assets:Bank", target_account="Expenses:Food",
                      source_sign="negative")
        values.update(changes)
        automation.save_rule(self.path, values)

    def postings(self):
        return [dict(account="Assets:Bank", amount="-25", currency="USD"),
                dict(account="Expenses:Food", amount="25", currency="USD")]

    def test_pages_and_empty_draft(self):
        self.record()
        database.update_record(self.path, "bank:1", "pending", [])
        for tab in ["stats","review","search","rules","accounts","sync","manual","files","database"]:
            with self.subTest(tab=tab):
                response = self.client.get("/", query_string=dict(tab=tab,record="bank:1"))
                self.assertEqual(response.status_code,200)
        self.assertIn(b'name="posting_amount"', self.client.get("/?tab=review").data)
        self.assertEqual(self.client.post("/records/missing/save").status_code,404)

    def test_sumitomo_credit_card_foreign_currency_date(self):
        self.assertEqual(
            parse_foreign_currency(
                "7590.00\u3000JPY\u30001.0000\u300009 05",
                date(2025, 9, 2),
            ),
            (Decimal("7590.00"), "JPY", Decimal("1.0000"), date(2025, 9, 5)),
        )

    def test_search_and_database_editor_are_paged(self):
        for index in range(105):
            self.record(f"bulk:{index}")
        first_search = self.client.get("/?tab=search&page=1")
        second_search = self.client.get("/?tab=search&page=2")
        self.assertEqual(first_search.status_code, 200)
        self.assertEqual(second_search.status_code, 200)
        self.assertIn(b"Page 1 of 2", first_search.data)
        self.assertIn(b"Page 2 of 2", second_search.data)
        first_database = self.client.get("/?tab=database&table=records&page=1")
        second_database = self.client.get("/?tab=database&table=records&page=2")
        self.assertEqual(first_database.status_code, 200)
        self.assertEqual(second_database.status_code, 200)
        self.assertIn(b"Page 1 of 2", first_database.data)
        self.assertIn(b"Page 2 of 2", second_database.data)

    def test_priority_confirmation_and_undo(self):
        self.record()
        self.rule(mode="suggest",priority="1")
        self.rule()
        self.assertEqual(automation.apply(self.path),0)
        self.assertEqual(automation.suggestion(self.path,self.record())["kind"],"rule")
        with database.connect(self.path) as db:
            db.execute("UPDATE rules SET enabled=0 WHERE priority=1")
        self.assertEqual(automation.apply(self.path),1)
        self.assertEqual(automation.apply(self.path),0)
        automation.undo(self.path,1)
        self.assertEqual(database.get_record(self.path,"bank:1")["status"],"pending")
        self.assertEqual(automation.apply(self.path),1)
        database.set_synced(self.path,["bank:1"])
        with self.assertRaises(ValueError):
            automation.undo(self.path,2)

    def test_complex_and_drafts_excluded(self):
        self.rule()
        self.record("fee",fee_amount="1")
        self.record("fx",source_currency="USD",target_currency="JPY")
        self.record("zero",amount="0")
        self.record("draft")
        database.update_record(self.path,"draft","pending",self.postings())
        self.record("one");self.record("two")
        database.link_records(self.path,["one","two"])
        self.assertEqual(automation.preview(self.path),[])

    def test_learning_threshold_and_corrections(self):
        for i in range(3):
            row=self.record(str(i))
            database.update_record(self.path,str(i),"resolved",self.postings())
            automation.learn(self.path,row,self.postings())
            candidate=self.record("candidate")
            if i<2: self.assertIsNone(automation.suggestion(self.path,candidate))
        self.assertEqual(automation.suggestion(self.path,candidate)["kind"],"learned")
        changed=self.postings();changed[1]["account"]="Expenses:Other"
        database.update_record(self.path,"0","resolved",changed)
        automation.learn(self.path,database.get_record(self.path,"0"),changed)
        self.assertIsNone(automation.suggestion(self.path,candidate))
        self.assertEqual(automation.apply(self.path),0)

    def test_validation_and_bulk_rollback(self):
        self.record()
        bad=self.postings();bad[1]["amount"]="24"
        with self.assertRaises(ValueError):
            database.update_record(self.path,"bank:1","resolved",bad)
        self.record("old")
        with database.connect(self.path) as db:
            db.execute("UPDATE records SET transaction_date='2019-01-01' WHERE record_id='old'")
        specs=[dict(account="Assets:Bank",sign="negative"),dict(account="Expenses:Food",sign="positive")]
        with self.assertRaises(ValueError):
            database.append_accounting(self.path,["bank:1","old"],specs,"resolved")
        self.assertEqual(database.get_record(self.path,"bank:1")["status"],"pending")

    def test_export_idempotence_and_handwritten_content(self):
        self.record()
        database.update_record(self.path,"bank:1","resolved",self.postings())
        accounts=self.app.config["ACCOUNTS_PATH"]; posts=self.app.config["POSTS_PATH"]
        accounts.write_text('2020-01-01 open Assets:Handwritten USD\n')
        posts.write_text('; my handwritten notes\n')
        ledger.export_all(self.path,accounts,posts)
        first=posts.read_text()
        ledger.export_all(self.path,accounts,posts)
        self.assertEqual(first,posts.read_text())
        self.assertIn("Assets:Handwritten",accounts.read_text())
        self.assertIn("; my handwritten notes",posts.read_text())
        from beancount import loader
        _,errors,_=loader.load_string(accounts.read_text()+posts.read_text())
        self.assertEqual(errors,[])
        database.update_record(self.path,"bank:1","pending",self.postings())
        ledger.export_all(self.path,accounts,posts)
        self.assertNotIn("source_record:",posts.read_text())

    def test_group_export_once(self):
        self.record("one");self.record("two")
        group=database.link_records(self.path,["one","two"])
        database.update_group(self.path,group,"resolved",self.postings())
        accounts=self.app.config["ACCOUNTS_PATH"];posts=self.app.config["POSTS_PATH"]
        ledger.export_all(self.path,accounts,posts)
        first=posts.read_text()
        ledger.export_all(self.path,accounts,posts)
        self.assertEqual(first,posts.read_text())
        self.assertEqual(first.count("; importer:group:"),1)
        self.assertNotIn("; importer:record:",first)

    def test_account_validation_and_references(self):
        with self.assertRaises(ValueError):
            database.save_account(self.path,"groceries","USD","","2020-01-01")
        self.record()
        database.update_record(self.path,"bank:1","resolved",self.postings())
        with self.assertRaises(ValueError):
            database.delete_account(self.path,"Assets:Bank")


    def test_group_unlink_replaces_export_and_sync_respects_drafts(self):
        self.record("one");self.record("two")
        group=database.link_records(self.path,["one","two"])
        database.update_group(self.path,group,"resolved",self.postings())
        accounts=self.app.config["ACCOUNTS_PATH"];posts=self.app.config["POSTS_PATH"]
        ledger.export_all(self.path,accounts,posts)
        self.assertEqual(ledger.import_beancount(self.path,accounts,posts)[1],2)
        database.unlink_group(self.path,group)
        ledger.import_beancount(self.path,accounts,posts)
        self.assertEqual(database.get_record(self.path,"one")["status"],"pending")
        database.update_record(self.path,"one","resolved",self.postings())
        ledger.export_all(self.path,accounts,posts)
        self.assertNotIn("; importer:group:",posts.read_text())
        self.assertEqual(posts.read_text().count("; importer:record:"),1)

    def test_create_rule_from_transaction(self):
        self.record()
        database.update_record(self.path,"bank:1","resolved",self.postings())
        response=self.client.get("/?tab=rules&from_record=bank:1")
        self.assertEqual(response.status_code,200)
        self.assertIn(b'value="Market groceries"',response.data)
        self.assertNotIn(b'name="id"',response.data)
        self.assertIn(b'value="suggest" selected',response.data)

    def test_http_save_and_invalid_posting(self):
        self.record()
        response=self.client.post("/records/bank:1/save",data={
            "status":"resolved", "posting_account":["Assets:Bank","Expenses:Food"],
            "posting_amount":["-25","24"],"posting_currency":["USD","USD"]},follow_redirects=True)
        self.assertIn(b"must balance",response.data)
        self.assertEqual(database.get_record(self.path,"bank:1")["status"],"pending")
        response=self.client.post("/records/bank:1/save",data={
            "status":"resolved", "posting_account":["Assets:Bank","Expenses:Food"],
            "posting_amount":["-25","25"],"posting_currency":["USD","USD"]},follow_redirects=True)
        self.assertEqual(response.status_code,200)
        self.assertEqual(database.get_record(self.path,"bank:1")["status"],"resolved")
        with database.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],1)

    def test_legacy_export_markers_migrate_without_duplicates(self):
        self.record()
        database.update_record(self.path,"bank:1","resolved",self.postings())
        posts=self.app.config["POSTS_PATH"]
        posts.write_text(ledger.render_transaction("bank:1","2026-09-01","Market",self.postings()))
        ledger.export_all(self.path,self.app.config["ACCOUNTS_PATH"],posts)
        self.assertEqual(posts.read_text().count("; importer:end"),1)


    def test_multiple_filters_and_dynamic_parsed_columns(self):
        from werkzeug.datastructures import MultiDict
        row = self.record(payment_method="card", raw_data={"Bank/Code": "ABC", "特記事項": "支払い"})
        available = dict(automation.available_fields(self.path))
        self.assertIn("payment_method",available)
        self.assertIn("fee_amount",available)
        self.assertIn("/raw_data/Bank~1Code",available)
        self.rule()
        original=automation.rules(self.path)[0]
        data=MultiDict({**original["definition"], "name":"Combined", "enabled":"on", "mode":"automatic",
                        "priority":"1","match_mode":"all"})
        data.setlist("condition_field",["description","payment_method","/raw_data/Bank~1Code","amount"])
        data.setlist("condition_operator",["contains","equals","equals","lt"])
        data.setlist("condition_pattern",["market","CARD","ABC","-20"])
        automation.save_rule(self.path,data)
        rule=automation.rules(self.path)[0]
        self.assertTrue(automation.matches(rule,row))
        rule["definition"]["conditions"][1]["pattern"]="cash"
        self.assertFalse(automation.matches(rule,row))
        rule["definition"]["match_mode"]="any"
        self.assertTrue(automation.matches(rule,row))
        self.assertEqual(self.client.get("/?tab=rules").status_code,200)
        data.setlist("condition_pattern",["market","CARD","ABC","NaN"])
        with self.assertRaises(ValueError): automation.save_rule(self.path,data)

    def test_missing_zero_and_legacy_filters(self):
        row=self.record(fee_amount="0")
        self.assertTrue(automation.condition_matches(dict(field="fee_amount",operator="equals",pattern="0"),row))
        self.assertFalse(automation.condition_matches(dict(field="note",operator="not_equals",pattern="x"),row))
        self.assertTrue(automation.condition_matches(dict(field="note",operator="empty",pattern=""),row))
        self.rule()
        rule=automation.rules(self.path)[0]
        rule["definition"].pop("conditions")
        self.assertTrue(automation.matches(rule,row))

    def test_linked_quick_fee_preview_does_not_save(self):
        self.record("out",amount="-103.01"); self.record("in",amount="100")
        group=database.link_records(self.path,["out","in"])
        data=dict(source="Assets:Bank",target="Expenses:Food",fee="Expenses:Other",
                  source_amount="-103.01",target_amount="100",
                  source_currency="USD",target_currency="USD")
        result=self.client.post("/records/out/categorize-preview",data=data)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json["postings"][2]["amount"],"3.01")
        self.assertEqual(database.get_record(self.path,"out")["status"],"linked")
        self.assertIn(b"Quick categorize",self.client.get(f"/?tab=review&group={group}").data)
        data["target_currency"]="JPY"
        self.assertEqual(self.client.post("/records/out/categorize-preview",data=data).status_code,400)
        data["target_currency"]="USD";data["fee"]=""
        self.assertEqual(self.client.post("/records/out/categorize-preview",data=data).status_code,400)
        data["source_amount"]="NaN"
        self.assertEqual(self.client.post("/records/out/categorize-preview",data=data).status_code,400)

    def test_prediction_scan_is_fresh_read_only_and_rules_first(self):
        for i in range(3):
            row=self.record(str(i))
            database.update_record(self.path,str(i),"resolved",self.postings())
            automation.learn(self.path,row,self.postings())
        self.record("pending")
        result=automation.scan_pending(self.path)
        self.assertEqual(result["results"][0]["kind"],"learned")
        self.assertEqual(result["total"],1)
        self.assertEqual(database.get_record(self.path,"pending")["status"],"pending")
        self.assertIsNone(database.get_record(self.path,"pending")["accounting_json"])
        response=self.client.post("/predictions/run")
        self.assertEqual(response.status_code,200)
        self.assertIn(b"Learned suggestion",response.data)
        self.rule(mode="suggest")
        self.assertEqual(automation.scan_pending(self.path)["results"][0]["kind"],"learned")
        with database.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],3)

    def test_fava_mounted_under_same_application(self):
        main=Path(self.tmp.name)/"main.beancount"
        main.write_text('option "title" "Test reports"\noption "operating_currency" "USD"\n2020-01-01 open Assets:Bank USD\n')
        application=create_app(self.path,ledger_path=main)
        client=application.test_client()
        response=client.get("/?tab=reports")
        self.assertIn(b'src="/fava/"',response.data)
        response=client.get("/fava/",follow_redirects=True)
        self.assertEqual(response.status_code,200)
        self.assertIn(b"Test reports",response.data)
        self.assertIn(b"/fava/",response.data)


    def test_bulk_human_confirmation_adds_training_examples(self):
        self.record("one");self.record("two")
        response=self.client.post("/records/bulk-save",data={
            "selected_record_id":["one","two"], "status":"resolved",
            "posting_account":["Assets:Bank","Expenses:Food"],
            "posting_sign":["negative","positive"], "posting_currency":["USD","USD"]})
        self.assertEqual(response.status_code,302)
        with database.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],2)


    def confirm_example(self, rid, target="Expenses:Food", **payload):
        row=self.record(rid, **payload)
        postings=self.postings()
        postings[1]["account"]=target
        database.update_record(self.path,rid,"resolved",postings)
        automation.learn(self.path,row,postings)
        return database.get_record(self.path,rid)

    def test_payment_method_disambiguates_same_merchant(self):
        for i in range(3):
            self.confirm_example("card"+str(i),payment_method="card",counterparty="Same merchant")
            self.confirm_example("cash"+str(i),target="Expenses:Other",payment_method="cash",counterparty="Same merchant")
        row=self.record("query",payment_method="cash",counterparty="Same merchant")
        prediction=automation.suggestion(self.path,row)
        self.assertIsNotNone(prediction)
        self.assertEqual(prediction["postings"][1]["account"],"Expenses:Other")
        self.assertIn("payment_method",prediction["matched_fields"])

    def test_time_disambiguates_identical_descriptions(self):
        for i in range(3):
            self.confirm_example("morning"+str(i),time="08:30:00")
            self.confirm_example("evening"+str(i),target="Expenses:Other",time="21:00:00")
        prediction=automation.suggestion(self.path,self.record("query",time="21:15:00"))
        self.assertIsNotNone(prediction)
        self.assertEqual(prediction["postings"][1]["account"],"Expenses:Other")

    def test_features_include_context_but_not_provenance(self):
        row=self.record("query",counterparty="ＡＢＣ 商店",payment_method="card",time="19:15:00",
                        exchange_rate="150.25",source_amount="10",source_currency="USD",
                        target_amount="1502.5",target_currency="JPY",fee_amount="1",
                        source_id="unique-secret",source_file="statement.csv",source_row=20,
                        raw_data={"Transaction ID":"unique-secret","Channel":"mobile",
                                  "Description":"Market groceries","Method":"card"})
        fields=learning.extract_features(row)
        for key in ["description","counterparty","payment_method","time.hour","date.weekday",
                    "date.month","date.monthday","exchange_rate","source_amount","target_currency","fee_amount","raw_data/Channel"]:
            self.assertIn(key,fields)
        for key in ["record_id","source_id","source_file","source_row","raw_data/Transaction ID",
                    "raw_data/Description","raw_data/Method"]:
            self.assertNotIn(key,fields)
        self.assertNotIn("1234",learning.text_tokens("COFFEE 1234"))
        self.assertIn("coffee",learning.text_tokens("COFFEE 1234"))
        self.assertIn("商店",learning.text_tokens("ＡＢＣ 商店"))

    def test_legacy_examples_enriched_without_labeling_other_records(self):
        row=self.record("legacy",payment_method="card")
        database.update_record(self.path,"legacy","resolved",self.postings())
        template=json.dumps([["Assets:Bank",-1],["Expenses:Food",1]])
        with database.connect(self.path) as db:
            db.execute("INSERT INTO decisions(record_id,source,currency,direction,description,template) VALUES(?,?,?,?,?,?)",
                       ("legacy","bank","USD","negative","Market groceries",template))
        self.record("automatic")
        self.rule()
        automation.apply(self.path,{"automatic"})
        automation.initialize(self.path)
        examples=learning.load_examples(self.path)
        self.assertEqual(len(examples),1)
        self.assertIn("payment_method",examples[0]["snapshot"]["fields"])
        with database.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],1)

    def test_source_isolation_and_no_amount_only_guess(self):
        for i in range(3): self.confirm_example(str(i),payment_method="card")
        row=self.record("query",payment_method="card")
        with database.connect(self.path) as db:
            db.execute("UPDATE records SET source='other-bank' WHERE record_id='query'")
        self.assertIsNone(automation.suggestion(self.path,database.get_record(self.path,"query")))
        with database.connect(self.path) as db:
            db.execute("DELETE FROM decisions")
        for i in range(3):
            row=self.record("numeric"+str(i))
            with database.connect(self.path) as db:
                db.execute("UPDATE records SET description='' WHERE record_id=?",(row["record_id"],))
            row=database.get_record(self.path,row["record_id"])
            database.update_record(self.path,row["record_id"],"resolved",self.postings())
            automation.learn(self.path,row,self.postings())
        row=self.record("numericquery")
        with database.connect(self.path) as db: db.execute("UPDATE records SET description='' WHERE record_id='numericquery'")
        self.assertIsNone(automation.suggestion(self.path,database.get_record(self.path,"numericquery")))

    def test_complex_conversions_suggest_accounts_without_amounts(self):
        database.save_account(self.path,"Assets:Exchange","","","2020-01-01")
        database.save_account(self.path,"Assets:Yen","JPY","","2020-01-01")
        postings=[dict(account="Assets:Bank",amount="-25",currency="USD"),
                  dict(account="Assets:Exchange",amount="25",currency="USD"),
                  dict(account="Assets:Exchange",amount="-3750",currency="JPY"),
                  dict(account="Assets:Yen",amount="3750",currency="JPY")]
        for i in range(3):
            row=self.record("fx"+str(i),source_currency="USD",target_currency="JPY",exchange_rate="150",counterparty="Currency transfer")
            database.update_record(self.path,row["record_id"],"resolved",postings)
            automation.learn(self.path,row,postings)
        row=self.record("newfx",source_currency="USD",target_currency="JPY",exchange_rate="151",counterparty="Currency transfer")
        result=automation.suggestion(self.path,row)
        self.assertIsNotNone(result)
        self.assertTrue(result["accounts_only"])
        self.assertEqual(len(result["postings"]),4)
        self.assertTrue(all(p["amount"] == "" for p in result["postings"]))
        self.assertIn(b"Accounts only",self.client.get("/?tab=review&record=newfx").data)

    def test_rebuild_keeps_corrections_and_parsed_edits_explicit(self):
        for i in range(3): self.confirm_example(str(i),payment_method="card")
        with database.connect(self.path) as db:
            row=db.execute("SELECT payload FROM records WHERE record_id='0'").fetchone()
            payload=json.loads(row[0]);payload["payment_method"]="cash"
            db.execute("UPDATE records SET payload=? WHERE record_id='0'",(json.dumps(payload),))
        self.assertEqual(len(learning.load_examples(self.path)),2)
        self.assertEqual(automation.rebuild_learning(self.path),3)
        self.assertEqual(len(learning.load_examples(self.path)),3)
        self.assertEqual(self.client.post("/learning/rebuild").status_code,302)
        self.assertEqual(self.client.get("/?tab=predictions").status_code,200)

if __name__ == "__main__":
    unittest.main()
