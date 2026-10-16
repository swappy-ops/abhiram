"""Durable task/workflow runtime on the application's existing SQLite store."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from core.contracts import CapabilityRegistry, validate_schema


def now_ms() -> int:
    return int(time.time() * 1000)


def uid(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex


class CoreRuntime:
    WORKSPACE = "ws-local"
    APPLICATIONS = {"campaign-control-desk": "app-campaigns", "documentation-demo": "app-docs"}

    def __init__(self, store, capability_registry: CapabilityRegistry | None = None):
        self.store = store
        self.registry = capability_registry or CapabilityRegistry()
        from applications.documentation import capabilities, workflow_definition
        for capability in capabilities():
            self.registry.register(capability)
        self._seed_registry(workflow_definition())
        self.recover_expired_tasks()

    def _seed_registry(self, docs_definition: dict) -> None:
        t = now_ms()
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO workspaces(id,slug,name,created_at) VALUES(?,?,?,?)", (self.WORKSPACE, "local", "Local workspace", t))
            apps = [("app-campaigns", "campaign-control-desk", "Campaign Control Desk", "Real-estate campaign application."),
                    ("app-docs", "documentation-demo", "Documentation demo", "Independent proof of Core runtime reuse.")]
            for app_id, slug, name, description in apps:
                db.execute("INSERT OR IGNORE INTO applications(id,workspace_id,slug,name,description,created_at) VALUES(?,?,?,?,?,?)", (app_id, self.WORKSPACE, slug, name, description, t))
            divisions = [("div-marketing", "marketing", "Marketing", "Campaign and content operations."),
                         ("div-business-admin", "business-administration", "Business administration", "Documentation and administrative workflows.")]
            for div_id, slug, name, description in divisions:
                db.execute("INSERT OR IGNORE INTO divisions(id,workspace_id,slug,name,description,created_at) VALUES(?,?,?,?,?,?)", (div_id, self.WORKSPACE, slug, name, description, t))
            for capability in self.registry.all():
                row = db.execute("SELECT id FROM divisions WHERE slug=?", (capability.division,)).fetchone()
                db.execute("""INSERT OR IGNORE INTO capabilities(id,division_id,purpose,task_types,input_schema,output_schema,permitted_tools,provider,max_attempts,timeout_ms,registered_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (capability.id, row["id"], capability.purpose, json.dumps(capability.task_types), json.dumps(capability.input_schema), json.dumps(capability.output_schema), json.dumps(capability.permitted_tools), capability.provider, capability.max_attempts, capability.timeout_ms, t))
            db.execute("INSERT OR IGNORE INTO workflow_definitions(id,workspace_id,application_id,slug,version,name,description,input_schema,definition,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("wf-docs-v1", self.WORKSPACE, self.APPLICATIONS["documentation-demo"], docs_definition["slug"], docs_definition["version"], docs_definition["name"], docs_definition["description"], json.dumps(docs_definition["input_schema"]), json.dumps(docs_definition), t))
            db.execute("INSERT OR IGNORE INTO integration_configs(id,workspace_id,application_id,integration_id,mode,config,health_status,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       ("int-docs-local", self.WORKSPACE, self.APPLICATIONS["documentation-demo"], "documentation-provider", "test", "{}", "ready", t))
            db.execute("INSERT OR IGNORE INTO integration_configs(id,workspace_id,application_id,integration_id,mode,config,health_status,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       ("int-campaign-delivery", self.WORKSPACE, self.APPLICATIONS["campaign-control-desk"], "campaign-delivery", "test", json.dumps({"adapter": "local-test-adapter", "external_publication": False}), "ready", t))

    def register_package(self, application: str, capabilities: list, definition: dict) -> None:
        """Compose application capabilities/workflows into the shared runtime."""
        app_id = self.APPLICATIONS.get(application)
        if not app_id:
            raise ValueError("Unknown application")
        t = now_ms()
        with self.store.transaction() as db:
            for capability in capabilities:
                try:
                    registered = self.registry.get(capability.id)
                except KeyError:
                    self.registry.register(capability)
                    registered = capability
                if registered != capability:
                    raise ValueError(f"Capability id is already registered with a different contract: {capability.id}")
                division = db.execute("SELECT id FROM divisions WHERE slug=? AND workspace_id=?", (capability.division, self.WORKSPACE)).fetchone()
                if not division:
                    raise ValueError(f"Division is not registered: {capability.division}")
                db.execute("""INSERT INTO capabilities(id,division_id,purpose,task_types,input_schema,output_schema,permitted_tools,provider,max_attempts,timeout_ms,registered_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET purpose=excluded.purpose,task_types=excluded.task_types,input_schema=excluded.input_schema,
                    output_schema=excluded.output_schema,permitted_tools=excluded.permitted_tools,provider=excluded.provider,max_attempts=excluded.max_attempts,timeout_ms=excluded.timeout_ms,active=1""",
                           (capability.id, division["id"], capability.purpose, json.dumps(capability.task_types), json.dumps(capability.input_schema), json.dumps(capability.output_schema), json.dumps(capability.permitted_tools), capability.provider, capability.max_attempts, capability.timeout_ms, t))
            workflow_id = f"wf-{application}-{definition['slug']}-v{definition['version']}"
            db.execute("""INSERT OR IGNORE INTO workflow_definitions(id,workspace_id,application_id,slug,version,name,description,input_schema,definition,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (workflow_id, self.WORKSPACE, app_id, definition["slug"], definition["version"], definition["name"], definition["description"], json.dumps(definition["input_schema"]), json.dumps(definition), t))

    def _audit(self, db, kind: str, message: str, *, app_id: str | None = None, run_id: str | None = None, task_id: str | None = None, details: dict | None = None, actor: str = "core") -> None:
        db.execute("INSERT INTO core_audit_events(workspace_id,application_id,run_id,task_id,kind,actor,message,details,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (self.WORKSPACE, app_id, run_id, task_id, kind, actor, message[:500], json.dumps(details or {}, ensure_ascii=False)[:8000], now_ms()))

    def start_workflow(self, slug: str, inputs: dict, *, application: str = "documentation-demo", actor: str = "operator", idempotency_key: str | None = None) -> str:
        app_id = self.APPLICATIONS.get(application)
        if not app_id:
            raise ValueError("Unknown application")
        with self.store.transaction() as db:
            wf = db.execute("SELECT * FROM workflow_definitions WHERE application_id=? AND slug=? AND active=1 ORDER BY version DESC LIMIT 1", (app_id, slug)).fetchone()
            if not wf:
                raise ValueError("Workflow definition is unavailable")
            validate_schema(inputs, json.loads(wf["input_schema"]))
            if idempotency_key:
                prior = db.execute("SELECT id FROM workflow_runs WHERE application_id=? AND idempotency_key=?", (app_id, idempotency_key)).fetchone()
                if prior:
                    return prior["id"]
            definition = json.loads(wf["definition"])
            steps = definition["steps"]
            keys = {step["key"] for step in steps}
            if len(keys) != len(steps):
                raise ValueError("Workflow step keys must be unique")
            for step in steps:
                if any(dep not in keys for dep in step.get("depends_on", [])):
                    raise ValueError(f"Workflow has an unknown dependency in {step['key']}")
                if step["capability"] not in {cap.id for cap in self.registry.all()}:
                    raise ValueError(f"Capability is unavailable: {step['capability']}")
            # Reject cycles before any durable run/task rows are written.
            pending = {step["key"]: set(step.get("depends_on", [])) for step in steps}
            visited = set()
            while pending:
                ready_steps = {key for key, deps in pending.items() if deps.issubset(visited)}
                if not ready_steps:
                    raise ValueError("Workflow dependencies contain a cycle")
                visited.update(ready_steps)
                for key in ready_steps:
                    pending.pop(key)
            run_id = uid("run")
            t = now_ms()
            db.execute("INSERT INTO workflow_runs(id,workspace_id,application_id,definition_id,status,inputs,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (run_id, self.WORKSPACE, app_id, wf["id"], "pending", json.dumps(inputs, ensure_ascii=False), idempotency_key, t, t))
            task_ids = {}
            for sequence_no, step in enumerate(steps):
                capability = self.registry.get(step["capability"])
                if step["task_type"] not in capability.task_types:
                    raise ValueError(f"Capability {capability.id} does not support task type {step['task_type']}")
                task_id = uid("task")
                task_ids[step["key"]] = task_id
                initial = "ready" if not step.get("depends_on") else "blocked"
                db.execute("""INSERT INTO tasks(id,run_id,workspace_id,application_id,capability_id,step_key,sequence_no,task_type,status,inputs,available_at,max_attempts,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (task_id, run_id, self.WORKSPACE, app_id, capability.id, step["key"], sequence_no, step["task_type"], initial, "{}", t, capability.max_attempts, t, t))
            for step in steps:
                for dep in step.get("depends_on", []):
                    db.execute("INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES(?,?)", (task_ids[step["key"]], task_ids[dep]))
            self._audit(db, "workflow.created", "Workflow run created", app_id=app_id, run_id=run_id, actor=actor, details={"workflow": slug, "version": wf["version"]})
            return run_id

    def _claim(self, run_id: str, worker_id: str, lease_ms: int = 60_000) -> dict | None:
        t = now_ms()
        with self.store.transaction() as db:
            run = db.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] in ("paused", "cancelled", "failed", "succeeded"):
                return None
            db.execute("UPDATE tasks SET status='ready',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE run_id=? AND status='retryable_failure' AND available_at<=? AND attempts<max_attempts", (t, run_id, t))
            task = db.execute("""SELECT t.* FROM tasks t WHERE t.run_id=? AND t.status='ready' AND t.available_at<=?
                AND (t.lease_until IS NULL OR t.lease_until<?) ORDER BY t.created_at,t.id LIMIT 1""", (run_id, t, t)).fetchone()
            if not task:
                return None
            attempt_no = task["attempts"] + 1
            db.execute("UPDATE tasks SET status='running',attempts=?,lease_owner=?,lease_until=?,started_at=COALESCE(started_at,?),updated_at=? WHERE id=? AND status='ready'",
                       (attempt_no, worker_id, t + lease_ms, t, t, task["id"]))
            attempt_id = uid("attempt")
            db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,status,lease_owner,started_at,inputs) VALUES(?,?,?,?,?,?,?)",
                       (attempt_id, task["id"], attempt_no, "running", worker_id, t, task["inputs"]))
            db.execute("UPDATE workflow_runs SET status='running',started_at=COALESCE(started_at,?),updated_at=? WHERE id=?", (t, t, run_id))
            claimed = dict(task)
            claimed.update({"attempt_no": attempt_no, "attempt_id": attempt_id, "lease_owner": worker_id})
            self._audit(db, "task.claimed", "Capability task claimed", app_id=task["application_id"], run_id=run_id, task_id=task["id"], details={"attempt": attempt_no})
            return claimed

    def _execution_context(self, task: dict) -> tuple[dict, dict[str, Any]]:
        with self.store.lock:
            run = self.store.db.execute("SELECT inputs FROM workflow_runs WHERE id=?", (task["run_id"],)).fetchone()
            deps = self.store.db.execute("""SELECT prior.step_key,prior.outputs FROM task_dependencies d
                JOIN tasks prior ON prior.id=d.depends_on_task_id WHERE d.task_id=? ORDER BY prior.created_at""", (task["id"],)).fetchall()
            context = {"workflow": json.loads(run["inputs"]), "dependencies": {r["step_key"]: json.loads(r["outputs"] or "{}") for r in deps}}
            run_row = self.store.db.execute("SELECT application_id FROM workflow_runs WHERE id=?", (task["run_id"],)).fetchone()
        return context, dict(run_row)

    def _finish(self, task: dict, worker_id: str, result: dict | None, error: Exception | None) -> None:
        t = now_ms()
        with self.store.transaction() as db:
            current = db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            if not current or current["status"] != "running" or current["lease_owner"] != worker_id:
                return
            capability = self.registry.get(task["capability_id"])
            if error is None:
                validate_schema(result, capability.output_schema)
                serialized = json.dumps(result, ensure_ascii=False)
                db.execute("UPDATE tasks SET status='succeeded',outputs=?,failure=NULL,lease_owner=NULL,lease_until=NULL,completed_at=?,updated_at=? WHERE id=?", (serialized, t, t, task["id"]))
                db.execute("UPDATE task_attempts SET status='succeeded',outputs=?,finished_at=? WHERE id=?", (serialized, t, task["attempt_id"]))
                db.execute("INSERT INTO usage_records(id,workspace_id,application_id,run_id,task_id,provider,model,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                           (uid("usage"), self.WORKSPACE, current["application_id"], task["run_id"], task["id"], capability.provider, capability.metadata.get("model", ""), t))
                if capability.id == "docs.source.retrieve":
                    db.execute("INSERT INTO context_refs(id,task_id,source_type,source_id,version,provenance,retrieved_at,access_scope) VALUES(?,?,?,?,?,?,?,?)",
                               (uid("ctx"), task["id"], "operator_input", "source:" + str(result.get("source_label", ""))[:240], "", json.dumps({"origin": "operator-supplied", "trust": "untrusted"}), t, "workspace"))
                self._audit(db, "task.succeeded", "Capability task succeeded", app_id=current["application_id"], run_id=task["run_id"], task_id=task["id"], details={"capability": capability.id})
                dependents = db.execute("SELECT task_id FROM task_dependencies WHERE depends_on_task_id=?", (task["id"],)).fetchall()
                for dependent in dependents:
                    blocked = db.execute("SELECT status FROM tasks WHERE id=?", (dependent["task_id"],)).fetchone()
                    unresolved = db.execute("SELECT count(*) FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on_task_id WHERE d.task_id=? AND p.status!='succeeded'", (dependent["task_id"],)).fetchone()[0]
                    if blocked and blocked["status"] == "blocked" and not unresolved:
                        db.execute("UPDATE tasks SET status='ready',updated_at=? WHERE id=?", (t, dependent["task_id"]))
            else:
                message = f"{type(error).__name__}: {str(error)}"[:500]
                retry = current["attempts"] < current["max_attempts"]
                status = "retryable_failure" if retry else "failed"
                next_time = t + min(30_000, 500 * (2 ** max(0, current["attempts"] - 1))) if retry else t
                db.execute("UPDATE tasks SET status=?,failure=?,available_at=?,lease_owner=NULL,lease_until=NULL,completed_at=?,updated_at=? WHERE id=?", (status, message, next_time, None if retry else t, t, task["id"]))
                db.execute("UPDATE task_attempts SET status=?,failure=?,finished_at=? WHERE id=?", (status, message, t, task["attempt_id"]))
                self._audit(db, "task.failed", "Capability task failed", app_id=current["application_id"], run_id=task["run_id"], task_id=task["id"], details={"retryable": retry, "error": message})

    def execute_run(self, run_id: str, *, worker_id: str | None = None, max_tasks: int = 50) -> dict:
        worker_id = worker_id or uid("worker")
        for _ in range(max_tasks):
            claimed = self._claim(run_id, worker_id)
            if claimed is None:
                break
            try:
                context, _ = self._execution_context(claimed)
                capability = self.registry.get(claimed["capability_id"])
                validate_schema(context, capability.input_schema)
                output = capability.execute(context)
                validate_schema(output, capability.output_schema)
                self._finish(claimed, worker_id, output, None)
            except Exception as exc:
                self._finish(claimed, worker_id, None, exc)
            # Retry backoff is persisted; do not sleep while occupying this local worker.
            with self.store.lock:
                ready = self.store.db.execute("SELECT count(*) FROM tasks WHERE run_id=? AND status='ready' AND available_at<=?", (run_id, now_ms())).fetchone()[0]
            if not ready:
                break
        self._refresh_run_status(run_id)
        return self.run_view(run_id)

    def _refresh_run_status(self, run_id: str) -> None:
        t = now_ms()
        with self.store.transaction() as db:
            run = db.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] in ("paused", "cancelled"):
                return
            rows = db.execute("SELECT status,step_key,outputs FROM tasks WHERE run_id=?", (run_id,)).fetchall()
            statuses = {row["status"] for row in rows}
            failed = "failed" in statuses
            if failed:
                status = "failed"
            elif rows and all(row["status"] == "succeeded" for row in rows):
                status = "succeeded"
            elif "running" in statuses:
                status = "running"
            elif "retryable_failure" in statuses:
                status = "blocked"
            elif rows and all(row["status"] in ("succeeded", "cancelled") for row in rows):
                status = "cancelled"
            else:
                status = "blocked" if "blocked" in statuses else "pending"
            outputs = {row["step_key"]: json.loads(row["outputs"] or "{}") for row in rows if row["status"] == "succeeded"}
            completed = t if status in ("succeeded", "failed", "cancelled") else None
            db.execute("UPDATE workflow_runs SET status=?,outputs=?,completed_at=?,updated_at=? WHERE id=?", (status, json.dumps(outputs, ensure_ascii=False), completed, t, run_id))

    def run_view(self, run_id: str) -> dict:
        with self.store.lock:
            run = self.store.db.execute("""SELECT r.*,a.slug application_slug,w.slug workflow_slug,w.name workflow_name
                FROM workflow_runs r JOIN applications a ON a.id=r.application_id JOIN workflow_definitions w ON w.id=r.definition_id WHERE r.id=?""", (run_id,)).fetchone()
            if not run:
                raise ValueError("Workflow run not found")
            tasks = self.store.db.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY sequence_no", (run_id,)).fetchall()
            result = dict(run)
            for key in ("inputs", "outputs"):
                result[key] = json.loads(result[key]) if result[key] else None
            result["tasks"] = []
            for item in tasks:
                task = dict(item)
                for key in ("inputs", "outputs", "failure"):
                    task[key] = json.loads(task[key]) if key in ("inputs", "outputs") and task[key] else task[key]
                task["attempt_history"] = [dict(row) for row in self.store.db.execute("SELECT attempt_no,status,lease_owner,started_at,finished_at,failure FROM task_attempts WHERE task_id=? ORDER BY attempt_no", (task["id"],))]
                result["tasks"].append(task)
            return result

    def recover_expired_tasks(self) -> int:
        t = now_ms()
        recovered = 0
        with self.store.transaction() as db:
            rows = db.execute("SELECT * FROM tasks WHERE status='running' AND lease_until<?", (t,)).fetchall()
            for task in rows:
                status = "retryable_failure" if task["attempts"] < task["max_attempts"] else "failed"
                message = "Worker lease expired; execution outcome is unknown"[:500]
                db.execute("UPDATE tasks SET status=?,failure=?,lease_owner=NULL,lease_until=NULL,available_at=?,updated_at=? WHERE id=?", (status, message, t, t, task["id"]))
                db.execute("UPDATE task_attempts SET status='ambiguous',failure=?,finished_at=? WHERE task_id=? AND attempt_no=? AND status='running'", (message, t, task["id"], task["attempts"]))
                self._audit(db, "task.recovered", "Expired task lease recovered; side effects may be ambiguous", app_id=task["application_id"], run_id=task["run_id"], task_id=task["id"], details={"status": status})
                recovered += 1
            run_ids = {row["run_id"] for row in rows}
        for run_id in run_ids:
            self._refresh_run_status(run_id)
        return recovered

    def cancel_workflow(self, run_id: str, *, actor: str) -> dict:
        t = now_ms()
        with self.store.transaction() as db:
            run = db.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise ValueError("Workflow run not found")
            if run["status"] in ("succeeded", "failed", "cancelled"):
                raise ValueError("A completed workflow cannot be cancelled")
            db.execute("UPDATE workflow_runs SET status='cancelled',completed_at=?,updated_at=? WHERE id=?", (t, t, run_id))
            db.execute("UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_until=NULL,completed_at=?,updated_at=? WHERE run_id=? AND status IN ('pending','ready','blocked','retryable_failure')", (t, t, run_id))
            self._audit(db, "workflow.cancelled", "Workflow run cancelled by operator", app_id=run["application_id"], run_id=run_id, actor=actor)
        return self.run_view(run_id)

    def pause_workflow(self, run_id: str, *, actor: str) -> dict:
        t = now_ms()
        with self.store.transaction() as db:
            run = db.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] not in ("pending", "running", "blocked"):
                raise ValueError("Workflow cannot be paused in its current state")
            if db.execute("SELECT count(*) FROM tasks WHERE run_id=? AND status='running'", (run_id,)).fetchone()[0]:
                raise ValueError("Wait for running capabilities to finish before pausing")
            db.execute("UPDATE workflow_runs SET status='paused',updated_at=? WHERE id=?", (t, run_id))
            self._audit(db, "workflow.paused", "Workflow run paused by operator", app_id=run["application_id"], run_id=run_id, actor=actor)
        return self.run_view(run_id)

    def resume_workflow(self, run_id: str, *, actor: str) -> dict:
        t = now_ms()
        with self.store.transaction() as db:
            run = db.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] != "paused":
                raise ValueError("Only a paused workflow can be resumed")
            db.execute("UPDATE workflow_runs SET status='pending',updated_at=? WHERE id=?", (t, run_id))
            self._audit(db, "workflow.resumed", "Workflow run resumed by operator", app_id=run["application_id"], run_id=run_id, actor=actor)
        return self.execute_run(run_id, worker_id=actor)

    def overview(self, application: str = "documentation-demo") -> dict:
        app_id = self.APPLICATIONS[application]
        with self.store.lock:
            capabilities = self.store.db.execute("""SELECT c.id,c.purpose,c.task_types,c.input_schema,c.output_schema,c.permitted_tools,c.provider,c.max_attempts,c.timeout_ms,c.active,d.slug division
                FROM capabilities c JOIN divisions d ON d.id=c.division_id ORDER BY c.id""").fetchall()
            runs = self.store.db.execute("SELECT id FROM workflow_runs WHERE application_id=? ORDER BY created_at DESC LIMIT 20", (app_id,)).fetchall()
            counts = {r["status"]: r["count"] for r in self.store.db.execute("SELECT status,count(*) count FROM tasks WHERE application_id=? GROUP BY status", (app_id,))}
            campaign_id = self.APPLICATIONS["campaign-control-desk"]
            campaign_runs = self.store.db.execute("SELECT id FROM workflow_runs WHERE application_id=? ORDER BY created_at DESC LIMIT 20", (campaign_id,)).fetchall()
            campaign_counts = {r["status"]: r["count"] for r in self.store.db.execute("SELECT status,count(*) count FROM tasks WHERE application_id=? GROUP BY status", (campaign_id,))}
            campaign_audit = [dict(r) for r in self.store.db.execute("SELECT kind,actor,message,details,created_at FROM core_audit_events WHERE application_id=? ORDER BY id DESC LIMIT 50", (campaign_id,))]
            campaign_integrations = [dict(r) for r in self.store.db.execute("SELECT integration_id,mode,health_status,checked_at FROM integration_configs WHERE application_id=? ORDER BY integration_id", (campaign_id,))]
            integrations = [dict(r) for r in self.store.db.execute("SELECT integration_id,mode,health_status,checked_at FROM integration_configs WHERE application_id=? ORDER BY integration_id", (app_id,))]
            audit = [dict(r) for r in self.store.db.execute("SELECT kind,actor,message,details,created_at FROM core_audit_events WHERE application_id=? ORDER BY id DESC LIMIT 50", (app_id,))]
        caps = []
        for row in capabilities:
            item = dict(row)
            for key in ("task_types", "input_schema", "output_schema", "permitted_tools"):
                item[key] = json.loads(item[key])
            caps.append(item)
        return {"application": application, "capabilities": caps, "runs": [self.run_view(r["id"]) for r in runs], "task_counts": counts,
                "campaign_runs": [self.run_view(r["id"]) for r in campaign_runs], "campaign_task_counts": campaign_counts,
                "campaign_audit": campaign_audit, "campaign_integrations": campaign_integrations, "integrations": integrations, "audit": audit}
