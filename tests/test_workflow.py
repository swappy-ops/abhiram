import concurrent.futures
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

import server


class ScriptedProvider:
    name = "scripted-test-provider"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, brief):
        self.calls += 1
        item = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


class ScriptedDelivery:
    name = "scripted-delivery"
    def __init__(self, responses):
        self.responses=list(responses); self.calls=0; self.keys=[]
    def deliver(self,campaign_id,version_id,idempotency_key,payload):
        self.calls+=1; self.keys.append(idempotency_key)
        item=self.responses[min(self.calls-1,len(self.responses)-1)]
        if isinstance(item,Exception): raise item
        return item


def draft_for(brief, *, headline=None, body=None):
    return {"headline": headline or (brief["project"] + ", " + brief["locality"])[:70],
            "body": body or ("Discover the thoughtfully planned homes in this welcoming neighbourhood. " * 9),
            "highlights": brief["highlights"],
            "disclaimer": f"RERA Reg. No. {brief['rera']}. Images are indicative. Subject to approvals and availability."}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.app = server.App(self.path)
        self.service = self.app.service
        self.users = {r["role"]: dict(r) for r in self.app.store.db.execute("SELECT * FROM users")}
        self.am = self.users["am"]
        self.admin = self.users["admin"]
        self.writer = self.users["cw"]
        self.client = self.users["client"]

    def tearDown(self):
        self.app.store.db.close()
        self.temp.cleanup()

    def brief(self, **extra):
        result = {"project": "Test Homes", "client": "Northgate Developers", "channel": "Portal listing",
                  "locality": "Sector 22, Noida", "rera": "UPRERA-TEST-1234",
                  "highlights": ["three-bedroom homes", "a landscaped garden", "nearby transit"],
                  "deadline": server.now_ms() + 5 * 86400000}
        result.update(extra)
        return result

    def create_review(self, provider=None):
        self.service.provider = provider or server.DraftProvider()
        return self.service.create(self.am, self.brief())

    def test_intake_routes_incomplete_and_complete_brief_to_review(self):
        c = self.service.create(self.am, self.brief(locality="", rera="", highlights=["one"]))
        self.assertEqual(c["state"], "needs_info")
        self.assertEqual({x["id"] for x in server.validate_brief(c)}, {"locality", "rera", "features"})
        c = self.service.update_brief(self.am, c["id"], {"locality": "Central Noida", "rera": "UPRERA-TEST-1234", "highlights": ["one", "two", "three"]})
        self.assertEqual(c["state"], "needs_info")
        c = self.service.action(self.am, c["id"], "supply_info", {"locality": c["locality"], "rera": c["rera"], "highlights": c["highlights"]})
        self.assertEqual(c["state"], "review")
        self.assertTrue(c["current_draft"]["valid"])
        self.assertEqual(c["current_draft"]["model"], "local-mock-v1")

    def test_duplicate_open_campaign_prevented_but_delivered_campaign_can_be_rebooked(self):
        c = self.create_review()
        with self.assertRaises(server.Problem) as raised:
            self.service.create(self.am, self.brief())
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.app.store.db.execute("SELECT kind FROM audit_events WHERE campaign_id=? ORDER BY id DESC",(c["id"],)).fetchone()["kind"],"denied")
        self.service.action(self.writer, c["id"], "approve_internal")
        c = self.service.action(self.client, c["id"], "client_approve")
        c = self.service.action(self.am, c["id"], "deliver")
        self.assertEqual(c["state"], "delivered")
        fresh = self.service.create(self.am, self.brief())
        self.assertNotEqual(fresh["id"], c["id"])

    def test_state_transition_and_permissions_are_server_enforced_and_audited(self):
        c = self.create_review()
        for role_actor in (self.client, self.am, self.admin):
            with self.subTest(role=role_actor["role"]):
                with self.assertRaises(server.Rejected):
                    self.service.action(role_actor, c["id"], "client_approve")
        audit = self.app.store.db.execute("SELECT count(*) n FROM audit_events WHERE campaign_id=? AND kind='denied'", (c["id"],)).fetchone()["n"]
        self.assertEqual(audit, 3)
        self.assertEqual(self.service.campaign_view(c["id"], self.am)["state"], "review")

    def test_internal_approval_then_client_approval_are_both_required_before_delivery(self):
        c = self.create_review()
        with self.assertRaises(server.Rejected):
            self.service.action(self.client, c["id"], "client_approve")
        with self.assertRaises(server.Rejected):
            self.service.action(self.am, c["id"], "deliver")
        c = self.service.action(self.writer, c["id"], "approve_internal")
        self.assertEqual(c["state"], "client")
        c = self.service.action(self.client, c["id"], "client_approve")
        self.assertEqual(c["state"], "ready")
        self.assertEqual(c["approved_version"], c["current_draft"]["version"])
        c = self.service.action(self.am, c["id"], "deliver")
        self.assertEqual(c["state"], "delivered")
        self.assertTrue(c["delivery_attempts"][0]["receipt"].startswith("local-"))
        with self.assertRaises(server.Rejected):
            self.service.action(self.am, c["id"], "deliver")
        self.assertEqual(self.service.campaign_view(c["id"], self.am)["state"], "delivered")

    def test_client_cannot_read_or_act_on_another_clients_campaign(self):
        c = self.create_review()
        other = dict(self.client, client_id="cli-halden")
        with self.assertRaises(server.Rejected):
            self.service.campaign_view(c["id"], other)
        self.assertEqual(self.app.store.db.execute("SELECT kind FROM audit_events WHERE campaign_id=? ORDER BY id DESC", (c["id"],)).fetchone()["kind"], "denied")

    def test_deterministic_copy_validation_explains_bad_and_prohibited_copy(self):
        brief = {"project":"Test", "locality":"Noida", "client_name":"Northgate", "rera":"UPRERA-TEST-1234", "highlights":["a","b","c"]}
        bad = draft_for(brief, headline="H"*71, body="Guaranteed risk-free returns.")
        checks = server.validate_draft(brief,bad)
        failures = {x["id"] for x in checks if not x["ok"]}
        self.assertTrue({"headline","body","prohibited"}.issubset(failures))
        self.assertTrue(all(x.get("fix") for x in checks if not x["ok"]))
        self.assertIn("RERA-TEST-1234", checks[2]["detail"])

    def test_retry_limit_and_repeated_identical_failure_escalation(self):
        self.service.provider = ScriptedProvider([TimeoutError("provider timeout")])
        c = self.service.create(self.am,self.brief())
        self.assertEqual(c["state"],"escalated")
        self.assertEqual(c["draft_attempts"],2)
        self.assertIn("Repeated provider failure",c["escalation_reason"])
        audits=self.app.store.db.execute("SELECT count(*) n FROM audit_events WHERE campaign_id=? AND message LIKE 'Draft attempt % failed:%'",(c["id"],)).fetchone()["n"]
        self.assertEqual(audits,2)

    def test_identical_validation_failure_escalates_before_attempt_limit(self):
        self.service.provider=ScriptedProvider([draft_for({"project":"Test Homes","locality":"Sector 22","client_name":"Northgate Developers","rera":"UPRERA-TEST-1234","highlights":["one","two","three"]},headline="H"*71)])
        c=self.service.create(self.am,self.brief())
        self.assertEqual(c["state"],"escalated")
        self.assertEqual(c["draft_attempts"],2)
        self.assertEqual(len(c["versions"]),2)
        self.assertIn("Repeated identical validation failure",c["escalation_reason"])

    def test_nonidentical_validation_failures_stop_at_configured_attempt_limit(self):
        brief={"project":"Test Homes","locality":"Sector 22","client_name":"Northgate Developers","rera":"UPRERA-TEST-1234","highlights":["one","two","three"]}
        goodbody="Discover the planned homes and amenities with the project team. "*9
        self.service.provider=ScriptedProvider([draft_for(brief,headline="H"*71),draft_for(brief,body="Too short."),draft_for(brief,body=goodbody+" Guaranteed returns.")])
        c=self.service.create(self.am,self.brief())
        self.assertEqual(c["state"],"escalated")
        self.assertEqual(c["draft_attempts"],server.MAX_ATTEMPTS)
        self.assertIn(f"after {server.MAX_ATTEMPTS} attempts",c["escalation_reason"])

    def test_recovery_resume_redraft_and_manual_takeover_paths(self):
        c=self.create_review()
        c=self.service.action(self.am,c["id"],"escalate",{"reason":"Client facts unclear"})
        self.assertEqual(c["previous_state"],"review")
        c=self.service.action(self.am,c["id"],"resume")
        self.assertEqual(c["state"],"review")
        c=self.service.action(self.am,c["id"],"escalate",{"reason":"Needs new copy"})
        c=self.service.action(self.am,c["id"],"rerun_ai")
        self.assertEqual(c["state"],"review")
        c=self.service.action(self.am,c["id"],"escalate",{"reason":"Manual recovery"})
        c=self.service.action(self.writer,c["id"],"take_over")
        self.assertEqual(c["state"],"review")
        self.assertEqual(c["current_draft"]["source"],"human")
        self.assertFalse(c["current_draft"]["valid"])

    def test_persistence_survives_store_recreation(self):
        c=self.create_review()
        cid=c["id"]
        self.app.login("am@example.test","Manager123!")
        self.app.store.db.close()
        reopened=server.App(self.path)
        try:
            persisted=reopened.service.campaign_view(cid,self.am)
            self.assertEqual(persisted["state"],"review")
            self.assertEqual(len(persisted["versions"]),1)
            self.assertTrue(persisted["audit"])
            session_count=reopened.store.db.execute("SELECT count(*) n FROM sessions").fetchone()["n"]
            self.assertEqual(session_count,1)
        finally:
            reopened.store.db.close()

    def test_repeated_concurrent_approval_and_delivery_do_not_duplicate(self):
        c=self.create_review()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda _: self.try_action(self.writer,c["id"],"approve_internal"),range(2)))
        self.assertEqual(sum(x=="ok" for x in outcomes),1)
        c=self.service.campaign_view(c["id"],self.am)
        c=self.service.action(self.client,c["id"],"client_approve")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda _: self.try_action(self.am,c["id"],"deliver"),range(2)))
        self.assertEqual(sum(x=="ok" for x in outcomes),1)
        count=self.app.store.db.execute("SELECT count(*) n FROM delivery_attempts WHERE campaign_id=? AND status='succeeded'",(c["id"],)).fetchone()["n"]
        self.assertEqual(count,1)

    def try_action(self,actor,cid,action):
        try:
            self.service.action(actor,cid,action)
            return "ok"
        except server.Problem:
            return "rejected"

    def test_activity_csv_has_correct_quoted_headers_and_rows(self):
        c=self.create_review()
        self.service.action(self.writer,c["id"],"approve_internal")
        import io,csv
        events=self.app.store.db.execute("SELECT * FROM audit_events WHERE campaign_id=? ORDER BY created_at DESC",(c["id"],)).fetchall()
        out=io.StringIO(); writer=csv.writer(out);writer.writerow(["time","campaign","actor","type","from","to","message"])
        for e in events: writer.writerow([e["created_at"],e["campaign_id"],e["actor_label"],e["kind"],e["from_state"] or "",e["to_state"] or "",e["message"]])
        rows=list(csv.reader(io.StringIO(out.getvalue())))
        self.assertEqual(rows[0],['time','campaign','actor','type','from','to','message'])
        self.assertTrue(any(row[1]==c["id"] and "approved" in row[-1].lower() for row in rows[1:]))
        self.assertEqual(server.csv_safe("=HYPERLINK('bad')"),"'=HYPERLINK('bad')")

    def test_seed_records_with_approval_states_have_persisted_valid_versions(self):
        for cid in ("CMP-101","CMP-102","CMP-103"):
            c=self.service.campaign_view(cid,self.admin)
            self.assertIsNotNone(c["current_draft"])
            self.assertTrue(c["current_draft"]["valid"],cid)
        client=self.service.campaign_view("CMP-103",self.client)
        self.assertEqual(client["state"],"ready")

    def test_malformed_and_rate_limited_provider_responses_escalate_safely(self):
        for provider in (ScriptedProvider([{}]),ScriptedProvider([urllib.error.HTTPError("https://example.invalid",429,"rate limited",{},None)])):
            with self.subTest(provider=provider):
                self.service.provider=provider
                c=self.service.create(self.am,self.brief(project="Failure " + str(id(provider))))
                self.assertEqual(c["state"],"escalated")
                self.assertIsNone(c["current_draft"])
                self.assertLessEqual(provider.calls,server.MAX_ATTEMPTS)
                self.assertTrue(any("failed" in e["message"].lower() for e in c["audit"]))

    def ready_campaign(self):
        c=self.create_review()
        c=self.service.action(self.writer,c["id"],"approve_internal")
        return self.service.action(self.client,c["id"],"client_approve")

    def test_delivery_timeout_records_bounded_attempts_and_allows_safe_retry(self):
        c=self.ready_campaign()
        self.service.delivery=ScriptedDelivery([TimeoutError("connection timed out")])
        with self.assertRaises(server.Rejected) as raised:
            self.service.action(self.am,c["id"],"deliver")
        self.assertEqual(raised.exception.status,502)
        unchanged=self.service.campaign_view(c["id"],self.am)
        self.assertEqual(unchanged["state"],"ready")
        self.assertEqual(len(unchanged["delivery_attempts"]),server.MAX_DELIVERY_ATTEMPTS)
        self.assertEqual(set(self.service.delivery.keys),{f"{c['id']}:v{c['approved_version']}"})
        self.service.delivery=server.LocalDeliveryProvider()
        delivered=self.service.action(self.am,c["id"],"deliver")
        self.assertEqual(delivered["state"],"delivered")
        self.assertEqual(len(delivered["delivery_attempts"]),server.MAX_DELIVERY_ATTEMPTS+1)

    def test_client_approver_provisioning_and_password_change(self):
        with self.assertRaises(server.Problem):
            self.service.provision_client(self.am,{"client":"New Dev","name":"Approver","email":"new@example.test","password":"long-initial-pass"})
        provisioned=self.service.provision_client(self.admin,{"client":"New Dev","name":"Approver","email":"new@example.test","password":"long-initial-pass"})
        user=dict(self.app.store.db.execute("SELECT * FROM users WHERE id=?",(provisioned["user"]["id"],)).fetchone())
        self.assertEqual(user["role"],"client")
        token,csrf=self.app.login("new@example.test","long-initial-pass")
        actor=self.app.identity(token)
        self.assertEqual(actor["client_id"],provisioned["client"]["id"])
        self.service.change_password(actor,"long-initial-pass","long-new-password",token)
        self.assertTrue(server.password_matches("long-new-password",self.app.store.db.execute("SELECT password_hash FROM users WHERE id=?",(actor["id"],)).fetchone()[0]))
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM sessions WHERE token_hash=?",(server.hashlib.sha256(token.encode()).hexdigest(),)).fetchone()[0],1)

    def test_http_auth_csrf_client_scope_health_and_csv_export(self):
        httpd=server.ThreadingHTTPServer(("127.0.0.1",0),server.make_handler(self.app))
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        base=f"http://127.0.0.1:{httpd.server_address[1]}"
        def client():
            jar=http.cookiejar.CookieJar()
            return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        def post(opener,path,data,csrf=None):
            headers={"Content-Type":"application/json"}
            if csrf: headers["X-CSRF-Token"]=csrf
            req=urllib.request.Request(base+path,data=json.dumps(data).encode(),headers=headers,method="POST")
            return opener.open(req)
        try:
            self.assertEqual(json.loads(urllib.request.urlopen(base+"/health").read())["status"],"ok")
            admin=client(); login=json.loads(post(admin,"/api/login",{"email":"am@example.test","password":"Manager123!"}).read());csrf=login["csrf"]
            me=json.loads(admin.open(base+"/api/me").read());self.assertEqual(me["csrf"],csrf)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                post(admin,"/api/campaigns",self.brief(project="No CSRF"))
            self.assertEqual(denied.exception.code,403)
            created=json.loads(post(admin,"/api/campaigns",self.brief(project="HTTP intake",locality="",rera="",highlights=[]),csrf).read())
            self.assertEqual(created["state"],"needs_info")
            csv_response=admin.open(base+"/api/export/activity.csv")
            self.assertIn("text/csv",csv_response.headers["Content-Type"])
            self.assertTrue(csv_response.read().decode("utf-8-sig").startswith("time,campaign,actor"))
            client_op=client(); client_login=json.loads(post(client_op,"/api/login",{"email":"client@example.test","password":"Client123!"}).read())
            scoped=json.loads(client_op.open(base+"/api/data").read())
            self.assertTrue(all(c["client_id"]==self.client["client_id"] for c in scoped["campaigns"]))
            with self.assertRaises(urllib.error.HTTPError) as cross:
                client_op.open(base+"/api/campaigns/CMP-104")
            self.assertEqual(cross.exception.code,403)
            self.assertTrue(client_login["csrf"])
        finally:
            httpd.shutdown();httpd.server_close();thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
