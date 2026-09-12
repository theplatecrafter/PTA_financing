import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import date
from decimal import Decimal

_boot = tempfile.TemporaryDirectory()
os.environ["IMPORTER_DB_PATH"] = str(Path(_boot.name) / "boot.db")
from importer import database, automation, ledger
from importer.app import create_app

class Workflows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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

if __name__ == "__main__":
    unittest.main()
