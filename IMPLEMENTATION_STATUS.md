# Core / Brain — implementation status

## Verified repository assessment

- This directory is a Python standard-library application, not a Git checkout. There is no package/build manifest. The runtime entry point is `server.py`; browser assets are `index.html` and `app.js`; the existing workflow tests are under `tests/`.
- The two supplied HTML reference files, `Campaign Control Desk.html` and `campaign-control-desk-upgraded.html`, were not edited.
- The existing campaign path is implemented in `Service.create` / `Service.action` / `Service._generate`: intake and completeness checks → draft provider → deterministic copy checks → internal approval → client approval of the exact immutable version → local delivery adapter → campaign audit. Existing state/role/approval/idempotency guards are server-side.
- Drafting is either the deterministic `local-mock-v1` adapter or an optional OpenAI-compatible HTTP request configured by environment variables. It is not a callable OpenCode CLI subscription. Delivery is `local-test-adapter`; it does not publish externally. RERA field/copy presence is not registry verification. No independent QA capability or external approval connector exists.
- Campaign draft generation and deterministic copy validation now run as two persisted Core capability tasks between short campaign transactions. Campaign rows remain authoritative for brief state, draft versions, approvals, and delivery status.
- Before changes, `python -m unittest discover -s tests -v` ran **18 tests, all passing**.

## Database authority and preservation

The configured database is `data/campaigns.sqlite3` (`CCD_DB` default). A separate `data/browser.sqlite3` also exists; source configuration does not select it. Both had schema version 1, active WAL sidecars, and valid SQLite integrity checks when inspected. Their data differed, so they were not merged.

| Database | Clients | Users | Campaigns | Drafts | Approvals | Deliveries | Audit events | Sessions | Backup |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `campaigns.sqlite3` | 3 | 4 | 6 | 4 | 3 | 0 | 9 | 1 | `backups/pre-core-20261016T054334Z/campaigns.sqlite3` |
| `browser.sqlite3` | 3 | 4 | 6 | 4 | 5 | 1 | 13 | 2 | `backups/pre-core-20261016T054334Z/browser.sqlite3` |

Both backups were made with SQLite's online backup API, including committed WAL content, and passed `PRAGMA integrity_check`. Only `campaigns.sqlite3` is selected for the Core migration; `browser.sqlite3` remains unchanged unless explicitly selected via `CCD_DB`.

Migration verification was run against the configured database after taking the backup: `PRAGMA user_version=2`, `PRAGMA integrity_check=ok`, and all eight campaign-domain table counts (clients, users, campaigns, drafts, approvals, deliveries, campaign audit, sessions) exactly matched the backup. A manual documentation workflow run was then retained in Core for inspection; it did not modify campaign-domain records. The separate browser database remained at `user_version=1`.

## Implemented architecture

- `core/contracts.py`: typed capability metadata, deterministic registry/selection, bounded execution metadata, and a dependency-free JSON-schema subset validator.
- `core/migrations.py`: additive migration v2. It adds workspace, application, division, capability/configuration, workflow definition/run, task/dependency/attempt, tool invocation, context reference, policy/decision, approval, integration, usage, external-side-effect, and Core audit tables. Existing campaign tables/rows are not transformed.
- `core/runtime.py`: persistent workflow/task creation, dependency resolution, SQLite task leases, task attempt history, bounded retry metadata, output validation, status aggregation, startup recovery that marks interrupted attempts ambiguous, cancel/pause/resume, audit, provenance references, and usage attribution where available.
- `applications/documentation.py`: an independent deterministic documentation app with source retrieval → Markdown generation → provenance validation. It uses the shared Core contracts/runtime and does not import campaign-domain code.
- `applications/campaign_control_desk.py`: registers the campaign provider and deterministic copy validator as composable Core capabilities. Each bounded drafting attempt has a Core workflow/task/attempt trace.
- `server.py` / `app.js` / `index.html`: authenticated Core API endpoints and a distinct Core operations area for operators. The Core view shows actual registry, task, integration, audit, workflow state, and generated artifact results.
- Campaign Control Desk remains backward-compatible. Intake, application state transitions, internal/client approvals, and delivery remain in the application service. They are not yet orchestrated as Core workflow tasks; no mirrored Core state is presented as authoritative.

The model and publishing adapters remain unchanged. The documentation example is deterministic/local and is not evidence of a live provider, external research, or publication integration.

## Phase tracking

- [x] Phase 1 — source/test/database audit and architecture assessment.
- [x] Phase 2 — Core contracts and additive, versioned schema boundary established.
- [x] Phase 3 — durable local task engine foundation (dependencies, claims, attempts, retry state, recovery, cancel/pause/resume).
- [x] Phase 4 — deterministic capability execution and structured input/output checks.
- [ ] Phase 5 — move Campaign Control Desk execution onto Core; preserve application-owned campaign rules and approvals. **Partially complete:** draft generation and validation use Core; intake, approval gates, publishing, delivery verification, and lifecycle orchestration remain in the application service.
- [x] Phase 6 — independent documentation workflow proof using the shared Core.
- [ ] Phase 7 — complete migration verification, browser validation, operations/security hardening, and handover. **Partially complete:** additive migration and local browser proof verified; restore drill, full security hardening, and campaign/Core integration remain.

## Test results

On the final working tree:

- `python -m unittest discover -s tests -v` — **30 passed, 0 failed** (12 Core tests plus all 18 original campaign tests).
- `python -m compileall -q server.py backup.py core applications tests` — **passed**.
- `node --check app.js` — **passed**.
- Manual browser check — logged in, created a complete campaign brief, verified it routed to review through persisted Core generation/validation tasks, opened the Core operations view, ran the independent documentation flow, and inspected the saved artifact. Mobile layout was checked after responsive-width adjustments.
- Existing campaign tests still cover the full prior workflow, including approval and local delivery boundaries. No new external/live-provider behavior was tested because none is configured.

## Run and backup commands

From this directory, with Python 3.10+:

```powershell
python server.py
python -m unittest discover -s tests -v
python -m compileall -q server.py backup.py core applications tests
node --check app.js
python backup.py
```

The server defaults to `http://127.0.0.1:8000`; it uses `data/campaigns.sqlite3`. `CCD_DB` selects a different SQLite file. `OPENAI_API_KEY` optionally configures the existing campaign draft adapter; without it drafting uses `local-mock-v1`. Campaign delivery remains a local test adapter and does not publish externally.

## Known gaps and risks

- The Campaign Control Desk lifecycle, approvals, campaign policy, and delivery are not yet orchestrated by Core. Draft provider calls have moved outside campaign write transactions; any live publishing connector must also execute outside transactions and add idempotency/reconciliation.
- The Core documentation workflow currently executes synchronously in the HTTP request. There is no continuously running background worker, retry wake-up scheduler, lease heartbeat, forced timeout, or cross-process worker integration test.
- Core tables exist for policies, approvals, tool calls, and external side effects; generic policy evaluation, permission-bound tool dispatch, approval orchestration, side-effect reconciliation, and idempotency-aware external connectors are not implemented.
- The capability timeout is declared but not forcibly enforced. Retry backoff is persisted; an operator/request must re-run blocked work after it becomes eligible.
- Documentation source text and outputs persist indefinitely. Retention/deletion controls, encryption at rest, and sensitive-data handling are not implemented.
- No live QA/research/publishing connectors, independent QA gate, external legal verification, or RERA registry lookup are configured.
- Authentication has no MFA, throttling/lockout, reset flow, or production-grade user lifecycle. The stdlib HTTP server and local seeded credentials are for development, not internet-facing use. Production requires a security review, TLS/reverse proxy, secrets management, request limits, backups/restore drills, monitoring, retention controls, and tamper-evident audit strategy.
- SQLite is appropriate for this local single-instance stage; distributed concurrency or multi-instance deployment requires a reviewed storage/locking plan.

## Next highest-value steps

1. Model campaign intake, approval gates, and delivery verification as Core workflow tasks without duplicating authoritative campaign state.
2. Add explicit campaign QA, approval, and publishing gates on Core policy/tool interfaces, with ambiguous external-side-effect reconciliation.
3. Add a bounded background worker with enforced timeouts, lease renewal, retry wake-up, graceful shutdown, and crash/concurrency tests.
4. Add retention/deletion controls and a browser-level test for the Core operations interface.
5. Re-run migration verification on a restore copy and document a restore drill before production data use.
