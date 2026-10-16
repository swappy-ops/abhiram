# Campaign Control Desk

A locally runnable real-estate campaign application and the first consumer of a reusable SQLite-backed Core runtime. The supplied `campaign-control-desk-upgraded.html` and `Campaign Control Desk.html` files are preserved unchanged; `index.html` is the served application entry point.

## Current architecture

- `core/contracts.py` defines capability contracts, a registry, and dependency-free input/output validation.
- `core/runtime.py` persists workflow runs, dependency-linked tasks, attempts, leases, audit events, context provenance, and usage attribution. It supports bounded retry, startup recovery, cancellation, pause, and resume.
- `applications/documentation.py` is a small application package with a three-step source → document → validation workflow. It uses the Core runtime and does not import Campaign Control Desk domain code. Its provider is deterministic/local.
- Campaign Control Desk now registers provider drafting and deterministic copy validation as two Core capabilities. Campaign tables remain authoritative for campaign state and immutable drafts; intake, application state transitions, internal/client approval, and local delivery still use the existing application service and are **not yet** Core workflow tasks.
- The Core operations view is available to workspace administrators and account managers in the app navigation. It shows persisted capability/task/workflow state and runs the independent documentation example.

The Core currently runs deterministic documentation capabilities synchronously in the HTTP request. Lease-based claims and persisted retry/recovery behavior are implemented, but there is no continuously running background scheduler. Capability timeout values are declared metadata, not yet forcibly interrupted by the runtime.

## Requirements and start

- Python 3.10 or newer (standard library only).
- No package install, Node build, container, queue, or external database is required.

From this directory:

```powershell
python server.py
```

Open <http://127.0.0.1:8000>. The server binds to localhost by default. `GET /health` is a local health/readiness check.

### Local seed accounts

These synthetic development accounts are seeded only into a new/empty database. Change a password after signing in. These are not production credentials.

| Role | Email | Initial password | Access |
|---|---|---|---|
| Agency administrator | `admin@example.test` | `Admin123!` | Workspace and user provisioning |
| Account manager | `am@example.test` | `Manager123!` | Briefs, recovery, delivery |
| Copywriter | `writer@example.test` | `Writer123!` | Draft editing and internal review |
| Northgate client approver | `client@example.test` | `Client123!` | Northgate campaigns only |

An administrator can provision additional client approver accounts from **Clients**. Passwords are stored as PBKDF2-HMAC-SHA256 hashes; session cookies are HttpOnly and SameSite=Strict; write APIs require a CSRF token. Client campaign reads and decisions are restricted server-side to the assigned client.

## Configuration

Copy `.env.example` values into the process environment as needed. The server does not load `.env` files automatically. `CCD_DB` defaults to `data/campaigns.sqlite3`.

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address; keep localhost for local use |
| `PORT` | `8000` | HTTP port |
| `CCD_DB` | `data/campaigns.sqlite3` | Persistent SQLite database path |
| `CCD_DRAFT_ATTEMPTS` | `3` | Bounded model/mock drafting attempts |
| `CCD_DELIVERY_ATTEMPTS` | `3` | Bounded delivery attempts, reusing one idempotency key |
| `OPENAI_API_KEY` | unset | Optional server-only OpenAI-compatible API credential |
| `CCD_MODEL` | `gpt-4o-mini` | Model name used with configured key |
| `CCD_OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base URL |
| `CCD_COOKIE_SECURE` | `0` | Set to `1` when HTTPS is provided by a trusted reverse proxy |

Without `OPENAI_API_KEY`, drafts use the clearly labelled deterministic `local-mock-v1` provider. With a key, the backend sends structured brief fields to the configured chat-completions endpoint with a 25-second timeout. Credentials never reach the browser. Provider failures, rate limits, malformed output and validation failures are bounded and audited; repeated identical failures escalate. Test with a provider that is approved for your data before sending real client briefs.

Delivery uses `local-test-adapter` by default. It accepts only client-approved content and stores attempts, payload references, a stable idempotency key and deterministic receipt in SQLite. It makes **no external publication call**. Do not describe it as a live CMS/portal connector.

## Database and backup

SQLite is created at first start. Migration v1 contains the existing Campaign Control Desk schema; additive migration v2 creates Core workspace/application/division/capability/workflow/task/attempt, policy, approval, integration, context, usage, side-effect, and Core audit tables. Applied migrations are recorded in `schema_migrations` and `PRAGMA user_version` is 2. Migration v2 does not rewrite campaign rows. Foreign keys and WAL are enabled; campaign state changes use `BEGIN IMMEDIATE` transactions.

The persistent model contains clients, users/roles, campaigns/brief fields, immutable draft versions and validation JSON, internal/client approvals, delivery attempts/receipts, sessions, and append-only audit events. Core workflow definitions, runs, dependency-linked tasks, leases, attempts, context references, usage, and audit events share the configured SQLite database. Timestamps are UTC epoch milliseconds. Both sets of records survive process restart.

Create a consistent online backup with:

```powershell
python backup.py
```

The script uses SQLite's backup API to write `backups/campaigns-<UTC timestamp>.sqlite3`. Before the Core migration, both discovered databases were backed up independently under `backups/pre-core-20261016T054334Z/`. Securely store and periodically restore-test backups. Activity can also be exported from the **Activity and audit** view as CSV. Approved content exports are available only after client approval.

## Test and check commands

```powershell
python -m unittest discover -s tests -v
python -m compileall -q server.py backup.py core applications tests
node --check app.js
```

There is no formatter, linter or type checker configured because this project has no third-party toolchain. HTTP-level tests cover Core authentication, operator scoping, and workflow execution. The Core view was manually exercised in the browser at desktop and mobile sizes, including running the documentation example and inspecting its saved artifact.

### Core documentation proof

Sign in as the seeded administrator or account manager, open **Core operations**, provide a document title, source label, and source text, then run the workflow. The result and its task/attempt/audit trace are stored in SQLite. This example does not call a model provider or external research service. Inputs and generated artifacts are retained in the database; no user-facing retention/deletion controls exist yet. Core endpoints are `GET /api/core/overview` and `POST /api/core/documentation`; write calls require the authenticated session's CSRF token.

## Workflow and validation boundaries

- States: Received, Needs info, AI drafting, Internal review, Client approval, Ready to deliver, Delivered, Escalated.
- Internal and client approval are separate and tied to the exact immutable draft version. Delivery checks the client approval and is idempotent.
- Administrators/account managers handle intake and recovery; copywriters and authorized account managers edit/review; client approvers are tenant-scoped.
- Brief checks require locality, an 8+ character RERA field, and at least three features. Copy checks cover headline length, 50–110 body words, RERA-string presence, prohibited assured/guaranteed/risk-free wording, and the required disclaimer.
- These rules are syntactic guards, not legal compliance. A RERA number is not verified against an authority, and property claims are not independently verified.
- Overdue work is escalated via the explicit deadline check. Escalated work can resume at a safe prior stage, be redrafted after brief validation, or be taken over manually.

## Limitations and deployment prerequisites

- Seed data and credentials are synthetic, local-development-only examples. Initial seeded users have fixed initial passwords; require secure account bootstrap/reset and remove/disable seed accounts before deployment with real data.
- No password reset/email workflow, MFA, rate limiting, lockout policy, user deactivation UI, or agency/client invite workflow beyond administrator provisioning is implemented.
- The built-in HTTP server is suitable for local development, not internet-facing production. Production needs a reviewed WSGI/ASGI deployment or reverse proxy, TLS, `CCD_COOKIE_SECURE=1`, secret management, request limits, backup retention, monitoring, and security review.
- SQLite supports a modest single-instance workload. WAL, foreign keys, locking, and transaction guards are configured; multi-instance deployment and high write concurrency need a shared database and a migration plan.
- Campaign draft-provider calls now execute between short campaign transactions as Core capability tasks. Campaign approvals and local delivery remain inside the application workflow; move any future external publishing call out of the SQLite transaction and add Core approval/side-effect reconciliation before configuring a live connector.
- Core task leases are local SQLite safeguards; there is no background worker, lease heartbeat, enforced capability timeout, distributed concurrency, or automated retry wake-up loop. The documentation example is deterministic and local.
- Core policy/approval/tool/side-effect tables establish storage contracts, but generic tool invocation, policy evaluation, human approval orchestration, and external-side-effect reconciliation are not yet implemented as runtime services.
- The documentation demo retains source text, task outputs, and audit history indefinitely. Retention, deletion, encrypted data storage, and sensitive-data controls need implementation before accepting confidential client material.
- `data/browser.sqlite3` was found to contain a separate dataset and WAL. It was backed up independently and was not merged into the configured `data/campaigns.sqlite3` authority. It remains schema version 1 unless selected explicitly with `CCD_DB`.
- Optional model integration requires a compatible provider, credential and privacy review. No model access is assumed. Model output may still hallucinate; deterministic checks cannot verify facts.
- Delivery is local-only; no CMS, property portal or client-notification connector is configured.
- No RERA registry, legal-review service, role-management UI for agency staff, background scheduler, or automatic deadline worker is present. Deadline checks run when an authorized user requests them.
- Audit rows are append-only by application convention, not tamper-evident against a database administrator. Production audit retention/immutability policy is still required.
