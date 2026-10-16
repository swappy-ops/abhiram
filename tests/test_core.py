import concurrent.futures
import dataclasses
import json
import tempfile
import threading
import urllib.error
import urllib.request
import http.cookiejar
import unittest
from pathlib import Path

import server
from core.contracts import Capability, CapabilityRegistry, ContractError, validate_schema


class CoreRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "core.sqlite3"
        self.app = server.App(self.path)
        self.core = self.app.core

    def tearDown(self):
        self.app.store.db.close()
        self.temp.cleanup()

    def inputs(self):
        return {
            "title": "Operations handover",
            "source_label": "Operator-supplied notes, 2026-10-16",
            "source_text": "The local operations process records each request and its source. Operators verify important claims against the original. Draft artifacts remain local until a human reviews them.",
        }

    def test_v2_migration_is_additive_and_campaign_records_remain(self):
        self.assertEqual(self.app.store.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM campaigns").fetchone()[0], 5)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM clients").fetchone()[0], 3)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM schema_migrations WHERE version IN (1,2)").fetchone()[0], 2)
        self.assertTrue(self.app.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_side_effects'").fetchone())

    def test_startup_keeps_user_version_at_latest_applied_migration(self):
        self.app.store.db.execute("PRAGMA user_version=1")
        self.app.store.db.close()
        reopened = server.App(self.path)
        try:
            self.assertEqual(reopened.store.db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(reopened.store.db.execute("SELECT count(*) FROM schema_migrations WHERE version=2").fetchone()[0], 1)
        finally:
            reopened.store.db.close()

    def test_capability_registry_and_input_output_contract_validation(self):
        candidates = self.core.registry.select("source_retrieval")
        self.assertEqual([item.id for item in candidates], ["docs.source.retrieve"])
        registry = CapabilityRegistry()
        cap = Capability("test.cap", "test", ("test",), {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}, {"type": "object"}, lambda x: {}, "research")
        registry.register(cap)
        with self.assertRaises(ValueError):
            registry.register(cap)
        with self.assertRaises(ContractError):
            validate_schema({}, cap.input_schema)
        validate_schema({"name": "valid"}, cap.input_schema)

    def test_documentation_pipeline_runs_through_dependencies_and_audits(self):
        before_campaigns = self.app.store.db.execute("SELECT count(*) FROM campaigns").fetchone()[0]
        run_id = self.core.start_workflow("documentation-pipeline", self.inputs(), actor="operator-1")
        run = self.core.execute_run(run_id, worker_id="test-worker")
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual([t["status"] for t in run["tasks"]], ["succeeded"] * 3)
        self.assertEqual([t["attempts"] for t in run["tasks"]], [1, 1, 1])
        artifact = run["outputs"]["validate-document"]["artifact"]
        self.assertIn("# Operations handover", artifact["content"])
        self.assertTrue(run["outputs"]["validate-document"]["valid"])
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM task_dependencies").fetchone()[0], 2)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM usage_records WHERE run_id=?", (run_id,)).fetchone()[0], 3)
        ref = self.app.store.db.execute("SELECT source_type,provenance FROM context_refs WHERE task_id=(SELECT id FROM tasks WHERE run_id=? AND step_key='retrieve-source')", (run_id,)).fetchone()
        self.assertEqual(ref["source_type"], "operator_input")
        self.assertEqual(json.loads(ref["provenance"])["trust"], "untrusted")
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM campaigns").fetchone()[0], before_campaigns)

    def test_campaign_drafting_uses_core_generation_and_validation_without_open_transaction(self):
        app = self.app

        class TransactionCheckingProvider:
            name = "transaction-check-provider"

            def generate(self, brief):
                if app.store.db.in_transaction:
                    raise AssertionError("provider must not run inside a campaign database transaction")
                return server.DraftProvider().mock(brief)

        self.app.service.provider = TransactionCheckingProvider()
        actor = dict(self.app.store.db.execute("SELECT * FROM users WHERE role='am'").fetchone())
        created = self.app.service.create(actor, {
            "project": "Core Routed Homes", "client": "Northgate Developers", "channel": "Portal listing",
            "locality": "Sector 22, Noida", "rera": "UPRERA-CORE-3210",
            "highlights": ["three-bedroom homes", "a landscaped garden", "nearby transit"],
        })
        self.assertEqual(created["state"], "review")
        run = self.app.core.overview("documentation-demo")["campaign_runs"][0]
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["inputs"]["campaign_id"], created["id"])
        self.assertEqual([task["capability_id"] for task in run["tasks"]], ["campaign.copy.generate", "campaign.copy.validate"])
        self.assertTrue(run["outputs"]["validate-copy"]["valid"])

    def test_schema_validation_and_idempotent_creation(self):
        with self.assertRaises(ContractError):
            self.core.start_workflow("documentation-pipeline", {"title": "missing fields"})
        first = self.core.start_workflow("documentation-pipeline", self.inputs(), idempotency_key="docs-request-1")
        second = self.core.start_workflow("documentation-pipeline", self.inputs(), idempotency_key="docs-request-1")
        self.assertEqual(first, second)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM workflow_runs").fetchone()[0], 1)

    def test_workflow_persists_across_restart(self):
        run_id = self.core.start_workflow("documentation-pipeline", self.inputs())
        self.app.store.db.close()
        reopened = server.App(self.path)
        try:
            run = reopened.core.execute_run(run_id)
            self.assertEqual(run["status"], "succeeded")
            self.assertEqual(len(run["tasks"]), 3)
        finally:
            reopened.store.db.close()

    def test_concurrent_claims_do_not_claim_same_task(self):
        run_id = self.core.start_workflow("documentation-pipeline", self.inputs())
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda name: self.core._claim(run_id, name), ("worker-a", "worker-b")))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM task_attempts WHERE status='running'").fetchone()[0], 1)

    def test_pause_resume_and_cancel_are_persisted_operations(self):
        run_id = self.core.start_workflow("documentation-pipeline", self.inputs())
        paused = self.core.pause_workflow(run_id, actor="operator")
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(self.core.execute_run(run_id)["status"], "paused")
        resumed = self.core.resume_workflow(run_id, actor="operator")
        self.assertEqual(resumed["status"], "succeeded")
        pending_id = self.core.start_workflow("documentation-pipeline", self.inputs())
        cancelled = self.core.cancel_workflow(pending_id, actor="operator")
        self.assertEqual(cancelled["status"], "cancelled")

    def test_bounded_retry_can_resume_from_persisted_failure(self):
        original = self.core.registry.get("docs.source.retrieve")
        calls = []

        def fails_once(context):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("simulated local timeout")
            return original.execute(context)

        self.core.registry._items[original.id] = dataclasses.replace(original, execute=fails_once)
        run_id = self.core.start_workflow("documentation-pipeline", self.inputs())
        first = self.core.execute_run(run_id)
        self.assertEqual(first["status"], "blocked")
        task = next(item for item in first["tasks"] if item["step_key"] == "retrieve-source")
        self.assertEqual(task["status"], "retryable_failure")
        self.assertEqual(task["attempts"], 1)
        with self.app.store.transaction() as db:
            db.execute("UPDATE tasks SET available_at=0 WHERE id=?", (task["id"],))
        second = self.core.execute_run(run_id)
        self.assertEqual(second["status"], "succeeded")
        retry_task = next(item for item in second["tasks"] if item["step_key"] == "retrieve-source")
        self.assertEqual(retry_task["attempts"], 2)
        self.assertEqual(len(calls), 2)

    def test_lease_recovery_marks_attempt_ambiguous(self):
        run_id = self.core.start_workflow("documentation-pipeline", self.inputs())
        self.core._claim(run_id, "crashed-worker", lease_ms=1)
        with self.app.store.transaction() as db:
            db.execute("UPDATE tasks SET lease_until=0 WHERE run_id=? AND status='running'", (run_id,))
        self.assertEqual(self.core.recover_expired_tasks(), 1)
        attempt = self.app.store.db.execute("SELECT status,failure FROM task_attempts WHERE task_id=(SELECT id FROM tasks WHERE run_id=? LIMIT 1)", (run_id,)).fetchone()
        self.assertEqual(attempt["status"], "ambiguous")
        self.assertIn("outcome is unknown", attempt["failure"])

    def test_core_api_is_authenticated_csrf_protected_and_operator_scoped(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def client():
            jar = http.cookiejar.CookieJar()
            return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def post(opener, path, data, csrf=None):
            headers = {"Content-Type": "application/json"}
            if csrf:
                headers["X-CSRF-Token"] = csrf
            request = urllib.request.Request(base + path, data=json.dumps(data).encode(), headers=headers, method="POST")
            return opener.open(request)

        try:
            operator = client()
            login = json.loads(post(operator, "/api/login", {"email": "am@example.test", "password": "Manager123!"}).read())
            overview = json.loads(operator.open(base + "/api/core/overview").read())
            self.assertEqual(len(overview["capabilities"]), 5)
            run = json.loads(post(operator, "/api/core/documentation", self.inputs(), login["csrf"]).read())
            self.assertEqual(run["status"], "succeeded")

            client_user = client()
            post(client_user, "/api/login", {"email": "client@example.test", "password": "Client123!"})
            with self.assertRaises(urllib.error.HTTPError) as denied:
                client_user.open(base + "/api/core/overview")
            self.assertEqual(denied.exception.code, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
