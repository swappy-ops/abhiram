"""Additive Core schema migration. Campaign tables are deliberately untouched."""
from __future__ import annotations

import sqlite3
import time


def now_ms() -> int:
    return int(time.time() * 1000)


def apply_v2(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone():
        db.execute("PRAGMA user_version=2")
        return
    db.executescript("""
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS workspaces(
      id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS applications(
      id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), slug TEXT NOT NULL,
      name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
      UNIQUE(workspace_id,slug));
    CREATE TABLE IF NOT EXISTS divisions(
      id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), slug TEXT NOT NULL,
      name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
      UNIQUE(workspace_id,slug));
    CREATE TABLE IF NOT EXISTS capabilities(
      id TEXT PRIMARY KEY, division_id TEXT NOT NULL REFERENCES divisions(id), purpose TEXT NOT NULL,
      task_types TEXT NOT NULL, input_schema TEXT NOT NULL, output_schema TEXT NOT NULL,
      permitted_tools TEXT NOT NULL DEFAULT '[]', provider TEXT NOT NULL, max_attempts INTEGER NOT NULL CHECK(max_attempts>0),
      timeout_ms INTEGER NOT NULL CHECK(timeout_ms>0), active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      registered_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS capability_configs(
      id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), capability_id TEXT NOT NULL REFERENCES capabilities(id),
      version INTEGER NOT NULL CHECK(version>0), config TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
      updated_at INTEGER NOT NULL, UNIQUE(workspace_id,capability_id,version));
    CREATE TABLE IF NOT EXISTS policy_definitions(
      id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), application_id TEXT REFERENCES applications(id),
      version INTEGER NOT NULL CHECK(version>0), name TEXT NOT NULL, rules TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      updated_at INTEGER NOT NULL, UNIQUE(workspace_id,application_id,name,version));
    CREATE TABLE IF NOT EXISTS workflow_definitions(
      id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), application_id TEXT NOT NULL REFERENCES applications(id),
      slug TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
      input_schema TEXT NOT NULL, definition TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      created_at INTEGER NOT NULL, UNIQUE(application_id,slug,version));
    CREATE TABLE IF NOT EXISTS workflow_runs(
      id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), application_id TEXT NOT NULL REFERENCES applications(id),
      definition_id TEXT NOT NULL REFERENCES workflow_definitions(id), status TEXT NOT NULL CHECK(status IN ('pending','running','blocked','paused','succeeded','failed','cancelled')),
      inputs TEXT NOT NULL, outputs TEXT, idempotency_key TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
      completed_at INTEGER, updated_at INTEGER NOT NULL, UNIQUE(application_id,idempotency_key));
    CREATE INDEX IF NOT EXISTS workflow_runs_status ON workflow_runs(status,updated_at DESC);
    CREATE TABLE IF NOT EXISTS tasks(
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES workspaces(id), application_id TEXT NOT NULL REFERENCES applications(id),
      capability_id TEXT NOT NULL REFERENCES capabilities(id), step_key TEXT NOT NULL, sequence_no INTEGER NOT NULL DEFAULT 0, task_type TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('pending','ready','running','blocked','retryable_failure','succeeded','failed','cancelled')),
      inputs TEXT NOT NULL, outputs TEXT, failure TEXT, available_at INTEGER NOT NULL, lease_owner TEXT, lease_until INTEGER,
      attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0), max_attempts INTEGER NOT NULL CHECK(max_attempts>0),
      created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER, updated_at INTEGER NOT NULL,
      UNIQUE(run_id,step_key));
    CREATE INDEX IF NOT EXISTS tasks_claim ON tasks(status,available_at,lease_until,created_at);
    CREATE INDEX IF NOT EXISTS tasks_run ON tasks(run_id,created_at);
    CREATE TABLE IF NOT EXISTS task_dependencies(
      task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      PRIMARY KEY(task_id,depends_on_task_id), CHECK(task_id!=depends_on_task_id));
    CREATE TABLE IF NOT EXISTS task_attempts(
      id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, attempt_no INTEGER NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('running','succeeded','retryable_failure','failed','cancelled','ambiguous')),
      lease_owner TEXT NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER, inputs TEXT NOT NULL,
      outputs TEXT, failure TEXT, UNIQUE(task_id,attempt_no));
    CREATE TABLE IF NOT EXISTS tool_invocations(
      id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), tool_id TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('read','write')),
      status TEXT NOT NULL, inputs TEXT NOT NULL, outputs TEXT, policy_decision_id TEXT, started_at INTEGER NOT NULL, finished_at INTEGER);
    CREATE TABLE IF NOT EXISTS context_refs(
      id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, source_type TEXT NOT NULL,
      source_id TEXT NOT NULL, version TEXT NOT NULL DEFAULT '', provenance TEXT NOT NULL, retrieved_at INTEGER NOT NULL,
      access_scope TEXT NOT NULL DEFAULT 'workspace');
    CREATE TABLE IF NOT EXISTS policy_decisions(
      id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), application_id TEXT REFERENCES applications(id),
      task_id TEXT REFERENCES tasks(id), policy_id TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('allow','deny','approval_required')),
      reason TEXT NOT NULL, facts TEXT NOT NULL DEFAULT '{}', decided_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS approval_requests(
      id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), application_id TEXT REFERENCES applications(id),
      run_id TEXT REFERENCES workflow_runs(id), task_id TEXT REFERENCES tasks(id), subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
      requested_by TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','cancelled')),
      reason TEXT NOT NULL DEFAULT '', payload_ref TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, resolved_at INTEGER);
    CREATE TABLE IF NOT EXISTS approval_decisions(
      id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES approval_requests(id), decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
      actor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS integration_configs(
      id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), application_id TEXT REFERENCES applications(id),
      integration_id TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('live','test','unconfigured','unavailable')),
      config TEXT NOT NULL DEFAULT '{}', health_status TEXT NOT NULL DEFAULT 'unknown', checked_at INTEGER,
      updated_at INTEGER NOT NULL, UNIQUE(workspace_id,application_id,integration_id));
    CREATE TABLE IF NOT EXISTS external_side_effects(
      id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), application_id TEXT REFERENCES applications(id),
      run_id TEXT REFERENCES workflow_runs(id), task_id TEXT REFERENCES tasks(id), integration_id TEXT NOT NULL,
      operation TEXT NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed','ambiguous')),
      request_ref TEXT NOT NULL DEFAULT '', result_ref TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL, UNIQUE(integration_id,idempotency_key));
    CREATE INDEX IF NOT EXISTS external_side_effect_status ON external_side_effects(status,updated_at);
    CREATE TABLE IF NOT EXISTS usage_records(
      id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), application_id TEXT REFERENCES applications(id),
      run_id TEXT REFERENCES workflow_runs(id), task_id TEXT REFERENCES tasks(id), provider TEXT NOT NULL,
      model TEXT NOT NULL DEFAULT '', input_tokens INTEGER, output_tokens INTEGER, cost_micros INTEGER, recorded_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS core_audit_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL REFERENCES workspaces(id), application_id TEXT REFERENCES applications(id),
      run_id TEXT REFERENCES workflow_runs(id), task_id TEXT REFERENCES tasks(id), kind TEXT NOT NULL, actor TEXT NOT NULL,
      message TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS core_audit_time ON core_audit_events(created_at DESC);
    CREATE INDEX IF NOT EXISTS core_audit_run ON core_audit_events(run_id,created_at);
    INSERT INTO schema_migrations(version,applied_at) VALUES(2,0);
    PRAGMA user_version=2;
    COMMIT;
    """)
    db.execute("UPDATE schema_migrations SET applied_at=? WHERE version=2", (now_ms(),))
