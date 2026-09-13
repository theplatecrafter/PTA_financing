import json
import unittest
from unittest.mock import patch
from werkzeug.datastructures import MultiDict
import test_workflows as fixtures
from importer import automation, database, filtering, learning, ledger, account_rename


class ExpandedWorkflows(unittest.TestCase):
    setUp = fixtures.Workflows.setUp
    record = fixtures.Workflows.record
    rule = fixtures.Workflows.rule
    postings = fixtures.Workflows.postings
    confirm_example = fixtures.Workflows.confirm_example

    def test_shared_filters_and_unsaved_preview(self):
        self.record("negative", payment_method="cash", raw_data={"独自列":"ローソン"})
        self.record("positive", amount="25", payment_method="card")
        form = MultiDict([("condition_field","*"),("condition_operator","contains"),("condition_pattern","ローソン"),
                          ("condition_field","payment_method"),("condition_operator","equals"),("condition_pattern","cash"),
                          ("direction","negative")])
        self.assertEqual([r["record_id"] for r in filtering.search(self.path,form)],["negative"])
        response = self.client.post("/rules/filter-preview",data=form)
        self.assertEqual(response.json["total"],1)
        form.add("tab","search")
        page = self.client.get("/",query_string=form)
        self.assertEqual(page.status_code,200)
        self.assertIn(b"negative",page.data)
        form.setlist("condition_pattern",["unmatched","card"])
        form["match_mode"]="any";form["direction"]="positive"
        self.assertEqual([r["record_id"] for r in filtering.search(self.path,form)],["positive"])
        form.setlist("condition_field",["amount"])
        form.setlist("condition_operator",["gt"])
        form.setlist("condition_pattern",["NaN"])
        self.assertEqual(self.client.post("/rules/filter-preview",data=form).status_code,400)

    def test_posting_account_filter(self):
        self.record("categorized")
        database.update_record(self.path, "categorized", "resolved", self.postings())
        self.record("uncategorized")
        form = MultiDict({"posting_account": "Expenses:Food", "status": "all"})
        self.assertEqual([r["record_id"] for r in filtering.search(self.path, form)], ["categorized"])
        page = self.client.get("/", query_string={"tab": "search", **form.to_dict()})
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"categorized", page.data)
        self.assertNotIn(b"uncategorized", page.data)

    def test_preview_includes_complex_and_saved_records(self):
        self.record(fee_amount="2", source_currency="USD", target_currency="JPY")
        database.update_record(self.path,"bank:1","held",None)
        response=self.client.post("/rules/filter-preview",data={
            "status":"all","condition_field":"description","condition_operator":"contains","condition_pattern":"market"})
        self.assertEqual(response.json["total"],1)

    def test_account_rules_compose_without_resolving_or_training(self):
        row=self.record(payment_method="cash")
        self.rule(action="account",account="Assets:Bank",account_role="source",source_account="",target_account="",source_sign="")
        self.rule(action="account",account="Expenses:Food",account_role="target",source_account="",target_account="",source_sign="")
        self.rule(action="account",account="Expenses:Other",account_role="target",priority="200")
        hints=automation.account_hints(self.path,row)
        self.assertEqual(hints["target"]["account"],"Expenses:Food")
        result=automation.suggestion(self.path,row)
        self.assertEqual([p["account"] for p in result["postings"]],["Assets:Bank","Expenses:Food"])
        self.assertEqual(automation.apply(self.path),0)
        self.assertEqual(learning.load_examples(self.path),[])
        page=self.client.get("/?tab=review")
        self.assertIn(b"selected>Assets:Bank",page.data)
        with self.assertRaises(ValueError):
            database.delete_account(self.path,"Expenses:Food")

    def test_rule_can_match_without_filters(self):
        self.rule()
        with database.connect(self.path) as db:
            db.execute("DELETE FROM rules")
        automation.save_rule(self.path, MultiDict({
            "name": "All transactions", "mode": "suggest", "enabled": "on",
            "source": "", "currency": "", "direction": "any",
            "source_account": "Assets:Bank", "target_account": "Expenses:Food",
            "source_sign": "negative",
        }))
        saved = automation.rules(self.path)[0]
        self.assertEqual(saved["definition"]["conditions"], [])
        self.assertTrue(automation.matches(saved, self.record("match-all")))

    def test_prediction_queue_actions_and_incomplete_draft(self):
        for i in range(3):
            self.confirm_example("trained"+str(i))
        self.record("one")
        self.record("two")
        response=self.client.post("/predictions/run")
        self.assertIn(b'name="posting_account"',response.data)
        self.assertIn(b"selected>Expenses",response.data)
        self.assertNotIn(b"Quick categorize",response.data)
        self.assertIn(b">Next</a>",response.data)
        response=self.client.post("/records/one/save",data={
            "return_tab":"predictions","status":"pending",
            "posting_account":["Assets:Bank","Expenses:Food"],
            "posting_amount":["",""],"posting_currency":["USD","USD"]},follow_redirects=True)
        self.assertEqual(response.status_code,200)
        self.assertEqual(json.loads(database.get_record(self.path,"one")["accounting_json"])[0]["amount"],"")
        self.assertIn(b"/records/two/save",response.data)
        self.assertNotIn(b"/records/one/save",response.data)
        response=self.client.post("/records/two/save",data={"return_tab":"predictions","status":"held"},follow_redirects=True)
        self.assertIn(b"No suggestions to review",response.data)

    def ledger_setup(self):
        root=self.app.config["POSTS_PATH"].parent
        main=root/"main.bean"
        main.write_text('include "accounts.bean"\ninclude "posts.bean"\ninclude "extra.bean"\n')
        self.app.config["LEDGER_PATH"]=main
        extra=root/"extra.bean"
        extra.write_text('2020-01-01 open Assets:Bank:Saving USD\n2026-09-01 balance Assets:Bank 0 USD\n; Assets:Banking stays\n')
        return [main,self.app.config["ACCOUNTS_PATH"],self.app.config["POSTS_PATH"]],extra

    def test_used_account_rename_updates_every_reference_and_learning(self):
        paths,extra=self.ledger_setup()
        database.save_account(self.path,"Assets:Bank:Saving","USD","","2020-01-01")
        self.confirm_example("human")
        self.record("automatic")
        self.rule()
        automation.apply(self.path)
        ledger.export_all(self.path,self.app.config["ACCOUNTS_PATH"],self.app.config["POSTS_PATH"])
        database.save_account(self.path,"Assets:Wallet","USD","","2020-01-01","Assets:Bank",ledger_paths=paths)
        self.assertEqual(json.loads(database.get_record(self.path,"human")["accounting_json"])[0]["account"],"Assets:Wallet")
        self.assertEqual(len(learning.load_examples(self.path)),1)
        self.assertEqual(automation.rules(self.path)[0]["definition"]["source_account"],"Assets:Wallet")
        self.assertIn("Assets:Wallet:Saving",extra.read_text())
        self.assertIn("Assets:Banking stays",extra.read_text())
        for path in paths[1:]:
            self.assertNotIn("Assets:Bank",path.read_text())
        automation.undo(self.path,1)
        self.assertEqual(database.get_record(self.path,"automatic")["status"],"pending")
        self.assertIn("Assets:Wallet:Saving",{a["name"] for a in database.get_accounts(self.path)})

    def test_rename_collision_and_file_failure_roll_back(self):
        paths,_=self.ledger_setup()
        self.confirm_example("human")
        ledger.export_all(self.path,self.app.config["ACCOUNTS_PATH"],self.app.config["POSTS_PATH"])
        originals={p:p.read_text() for p in paths}
        with self.assertRaises(ValueError):
            database.save_account(self.path,"Expenses:Food","USD","","2020-01-01","Assets:Bank",ledger_paths=paths)
        real=account_rename.replace_file
        writes=[]
        def fail_second(path,text):
            writes.append(path)
            if len(writes)==2:
                raise OSError("test write failure")
            return real(path,text)
        with patch.object(account_rename,"replace_file",side_effect=fail_second),self.assertRaises(OSError):
            database.save_account(self.path,"Assets:Wallet","USD","","2020-01-01","Assets:Bank",ledger_paths=paths)
        self.assertEqual(json.loads(database.get_record(self.path,"human")["accounting_json"])[0]["account"],"Assets:Bank")
        self.assertEqual({p:p.read_text() for p in paths},originals)
        self.assertEqual(len(learning.load_examples(self.path)),1)

    def test_account_merge_updates_references_and_ledger(self):
        paths, extra = self.ledger_setup()
        database.save_account(self.path, "Assets:Wallet", "USD", "", "2020-01-01")
        database.save_account(self.path, "Assets:Bank:Saving", "USD", "", "2020-01-01")
        self.confirm_example("merged")
        self.rule()
        ledger.export_all(self.path, self.app.config["ACCOUNTS_PATH"], self.app.config["POSTS_PATH"])
        response = self.client.post("/accounts/merge", data={"source": "Assets:Bank", "destination": "Assets:Wallet", "name": "Assets:Wallet", "currency": "USD"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual({a["name"] for a in database.get_accounts(self.path)}, {"Assets:Wallet", "Assets:Bank:Saving", "Expenses:Food", "Expenses:Other"})
        self.assertEqual(json.loads(database.get_record(self.path, "merged")["accounting_json"])[0]["account"], "Assets:Wallet")
        self.assertEqual(automation.rules(self.path)[0]["definition"]["source_account"], "Assets:Wallet")
        self.assertNotIn("open Assets:Bank USD", extra.read_text())
        self.assertIn("Assets:Wallet", extra.read_text())
        self.assertIn("2020-01-01 open Assets:Wallet USD", self.app.config["ACCOUNTS_PATH"].read_text())

    def test_account_merge_allows_empty_currency(self):
        response = self.client.post("/accounts/merge", data={"source": "Assets:Bank", "destination": "Expenses:Food", "name": "Assets:Merged", "currency": ""})
        self.assertEqual(response.status_code, 302)
        merged = next(account for account in database.get_accounts(self.path) if account["name"] == "Assets:Merged")
        self.assertIsNone(merged["currency"])

    def test_prediction_error_preserves_edited_values(self):
        for i in range(3):
            self.confirm_example("trained"+str(i))
        self.record("pending")
        response=self.client.post("/records/pending/save",data={
            "return_tab":"predictions","status":"resolved",
            "posting_account":["Assets:Bank","Expenses:Other"],
            "posting_amount":["-25","17"],"posting_currency":["USD","USD"]})
        self.assertEqual(response.status_code,200)
        self.assertIn(b'value="17"',response.data)
        self.assertIn(b'value="Expenses:Other" selected',response.data)
        self.assertEqual(database.get_record(self.path,"pending")["status"],"pending")

    def test_synced_group_rename_and_rule_filter_preservation(self):
        paths,_=self.ledger_setup()
        self.record("a");self.record("b")
        group=database.link_records(self.path,["a","b"])
        database.update_group(self.path,group,"resolved",self.postings())
        self.rule(pattern="Assets:Bank")
        ledger.export_all(self.path,self.app.config["ACCOUNTS_PATH"],self.app.config["POSTS_PATH"])
        ledger.import_beancount(self.path,self.app.config["ACCOUNTS_PATH"],self.app.config["POSTS_PATH"])
        database.save_account(self.path,"Assets:Wallet","USD","","2020-01-01","Assets:Bank",ledger_paths=paths)
        self.assertEqual(database.get_record(self.path,"a")["status"],"synced")
        for member in database.get_group_members(self.path,group):
            self.assertEqual(json.loads(member["accounting_json"])[0]["account"],"Assets:Wallet")
        self.assertEqual(automation.rules(self.path)[0]["definition"]["conditions"][0]["pattern"],"Assets:Bank")
        self.assertNotIn("Assets:Bank",self.app.config["POSTS_PATH"].read_text())

    def test_any_field_negative_and_empty_filter_navigation(self):
        self.record("cash",payment_method="cash")
        self.record("card",payment_method="card")
        form=MultiDict(dict(condition_field="*",condition_operator="not_contains",condition_pattern="cash"))
        self.assertEqual([r["record_id"] for r in filtering.search(self.path,form)],["card"])
        from urllib.parse import urlencode, urlparse,parse_qs
        suffix=urlencode(dict(condition_field="*",condition_operator="contains",condition_pattern="",direction="negative"))
        response=self.client.post("/records/cash/save",data={
            "return_tab":"search","return_filters":suffix,"status":"pending",
            "posting_account":["Assets:Bank"],"posting_amount":[""],"posting_currency":["USD"]})
        self.assertEqual(parse_qs(urlparse(response.location).query,keep_blank_values=True)["condition_pattern"],[""])
