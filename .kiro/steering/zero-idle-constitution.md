# The Zero-Idle Constitution

**Status:** RATIFIED · **Scope:** the entire `aegis-skeleton` repository and every
AWS resource it creates · **Enforcement:** automated (AST gate, MCP probe, pre-commit
hook) · **Amendments:** pull request + explicit rule-number citation.

> Aegis is a *micro-core*, not a service. Every rule below exists to protect one
> property: **an idle system costs $0.00 and still proves its own correctness.**
> Rules are numbered so review comments, commits and the MCP tooling can cite them
> (`Rule 3` wins an argument; "I prefer it this way" does not).

---

## Rule 1 — Zero-Idle: pay-per-request billing only

**MUST**
- DynamoDB tables are created with `BillingMode: PAY_PER_REQUEST`. No
  `ProvisionedThroughput` block may exist anywhere in the templates.
- The public door is an **HTTP API (API Gateway v2)** on the `$default` stage.
  HTTP APIs bill per request and have no idle hourly charge.
- Compute is request-driven only: Lambda invoked by API Gateway or SQS, nothing else.
- Storage is S3 (static site + build artifacts) with lifecycle rules, never a running host.
- Queues are standard SQS with a bounded `MessageRetentionPeriod` and a redrive
  policy to a dead-letter queue.

**MUST NOT**
- Provisioned capacity or provisioned concurrency (including autoscaled minimums > 0).
- NAT Gateway, ALB/NLB, bastion hosts, always-on EC2/ECS/Fargate tasks.
- RDS / Aurora / ElastiCache / OpenSearch — anything that bills per wall-clock hour.
- "Warm-up" pings, cron keep-alives, or synthetic traffic used to keep a pool hot.
- REST APIs with usage plans where an HTTP API satisfies the contract.

**Why:** the system is judged on its idle bill. A single NAT Gateway (~$32/mo) or one
provisioned RCU would destroy the model the entire repository demonstrates.

**Verification**
- `probe_production_pipeline` → `checks.dynamodb.tables[].billing_mode` must all be
  `PAY_PER_REQUEST` and `checks.dynamodb.violations` must be empty.
- Template review: `grep -n "ProvisionedThroughput" template.yaml` returns nothing.

---

## Rule 2 — ARM64 Lambda, single runtime, no surprises

**MUST**
- Every function inherits `Architectures: [arm64]` from `Globals.Function`; a function
  that needs a different architecture is a design error, not an exception.
- Runtime is `python3.12` (pinned), handler is a dotted path inside this repository.
- Baseline `MemorySize: 256`, `Timeout: 10`. Raising either requires a cost note in
  the pull request body (ARM64 price is memory × duration; both are budget inputs).
- Dependencies are pure-Python wheels available in the Lambda runtime, or vendored
  under the function's `CodeUri`.

**MUST NOT**
- x86-only native wheels, compiled extensions built on the developer's host,
  Lambda layers that silently shadow `src/`, or container images (they reintroduce
  registry idle cost).
- `os.system`, shelling out to the AWS CLI from Lambda, or bundling `boto3` into the
  deployment package (the runtime already ships it).

**Why:** Graviton gives ~20% better price/performance at identical concurrency
semantics, and a single pinned runtime keeps the reproducibility story honest.

**Verification**
- Template grep for `Architectures` under `Globals.Function` plus per-function overrides.
- `aws lambda get-function-configuration` (when credentials allow) must report
  `Architectures: ["arm64"]`.

## Rule 3 — Single-table DynamoDB, two keys, one TTL

**MUST**
- Exactly **one** state table per environment. New entities never get a new table.
- Key schema is exactly `PK` (HASH, String) and `SK` (RANGE, String). No other
  attribute may be part of the key.
- Composite key convention:
  - `PK = "<ENTITY>#<natural-id>"` (e.g. `RECON#stripe:ch_3P…`, `AUDIT#account:7059…`)
  - `SK = "<UTC ISO-8601>#<status>"` (e.g. `2026-11-12T23:47:24Z#MATCHED`)
- Every item carries a numeric `ttl` attribute (epoch seconds) and the table has
  `TimeToLiveSpecification` enabled on `ttl`, so history expires without a sweeper.
- Writes are idempotent: `PutItem` on a deterministic key, never `UpdateItem` with
  read-modify-write counters in the request path.
- Money is stored as a **string** (canonical decimal) or a DynamoDB `N` produced from
  `decimal.Decimal`; never as an IEEE-754 float (see Rule 4).

**MUST NOT**
- `Scan` in any request path (`POST /ingest`, SQS consumer, UI). Query by `PK`, or
  the access pattern is wrong.
- Secondary indexes without a written access pattern in the pull request. A GSI that
  is never queried is pure idle cost.
- Multiple tables ("one table per domain") — that is how single-table design dies.

**Why:** one table means one billing surface, one backup policy, one TTL sweep, and
keys designed from access patterns instead of entities.

**Verification**
- `probe_production_pipeline` reports `single_table_keys: true` and the `ttl` block.
- Repository tests assert the key builder never emits anything but `PK`/`SK`.

---

## Rule 4 — Decimal-only arithmetic. No floats. Ever.

**MUST**
- Every monetary, quantity or rate value is a `decimal.Decimal`, constructed from a
  string: `Decimal("1234.56")`, or `Decimal(str(value))` when the input is unknown.
- Rounding uses an explicit mode and an explicit quantum:
  `amount.quantize(CENT, rounding=ROUND_HALF_UP)` with `CENT = Decimal("0.01")`.
- Divisions operate on `Decimal` operands and are immediately quantized; a ratio is
  stored as a Decimal string, not a float.
- DynamoDB serialisation goes through the existing `_decimalize` helper; the browser
  view may use integer cents, but the authoritative math is Python `Decimal`.
- Aggregation sets its accumulator explicitly: `sum(rows, Decimal("0"))`.

**MUST NOT**
- Float literals (`0.1`, `1e-3`, `4.1667`), `float(...)`, `round(...)`, `numpy`/`pandas`
  money math, `statistics.mean` on amounts, or JSON numbers parsed straight into floats.
- Implicit float coercion via `math.*`, `%` on floats, or f-string formatting that
  re-rounds money before persistence.
- Comparing money with `==` on floats or with a tolerance expressed in floats.

**Why:** `0.1 + 0.2 != 0.3`. A reconciliation engine that drifts by one cent per
million rows is not an auditor; it is a liability. Rule 4 is the reason the canary
drift row (±$0.02) is detectable at all.

**Verification**
- `audit_ast_float_safety` must return `summary.errors == 0` and
  `summary.clean == true` for `src/`. The gate maps directly onto AST nodes:

  | Rule id | AST shape | Severity |
  | --- | --- | --- |
  | `FLOAT_LITERAL` | `ast.Constant` with a `float` value | error |
  | `FLOAT_COERCION` | `ast.Call` to `float(...)` | error |
  | `UNBOUNDED_ROUND` | `ast.Call` to `round(...)` | warning |
  | `FLOAT_DIVISION` | `ast.BinOp(Div)` with no `Decimal` operand | warning |

  Warnings are tolerated only where the operands are provably integral counters
  (latency in ms, row counts); errors are never tolerated.

## Rule 5 — Buffered ingress and an auditable envelope

**MUST**
- The only public mutation door is `POST /ingest` on the HTTP API. Its handler
  validates, then enqueues: API Gateway → `AegisIngestionQueue` → core Lambda →
  DynamoDB. No component writes state synchronously from the request path.
- Envelope shape is fixed: `{ "id": "...", "type": "...", "payload": { ... } }`.
  `id`, `type` and `payload` are all mandatory; a missing field is `400 INVALID_PAYLOAD`.
- Accepted work answers `202` with `{ "status": "QUEUED", "message_id": "<id>" }`.
  The client must display the status and the returned message id verbatim.
- Batch identifiers are client-generated and unique (`batch-<epoch-ms>`), which makes
  redelivery from SQS harmless.
- `payload.row_count` is **measured** from the parsed artifact, never hard-coded.

**MUST NOT**
- Bulk/batch endpoints that bypass the queue, or direct DynamoDB writes from the UI.
- Returning `200 OK` for queued work, or inventing a `message_id` the server did not send.
- Rendering rows the browser did not actually parse from an ingested artifact.
- Using the public endpoint as a liveness check *with a valid envelope* (that enqueues
  a real message and costs money). Use an intentionally incomplete envelope: it is
  rejected by validation before `sqs.send_message`.

**Why:** the queue absorbs the spike that would otherwise become a DynamoDB bill, and
the envelope is the only correlation key the whole pipeline shares.

**Verification**
- Live UI: Zone A must show `HTTP 202 ACCEPTED · MESSAGE-ID batch-…`.
- `probe_production_pipeline` → `checks.api_gateway.http_probe.status == 400` with
  `enqueued: false` proves the door is alive without enqueueing anything.

---

## Rule 6 — Static UI, zero idle compute

**MUST**
- The interface is a single static document (`frontend/index.html`) served from S3.
  No SSR, no request-time build step, no Node server, no client framework runtime.
- Fixture loads parse locally for the table view and send only metadata
  (`dataset`, `row_count`) to the API — artifacts never leave the browser.
- The deployed bundle is synced with `--delete` so the bucket cannot accumulate
  orphaned objects (orphans are storage cost with no reader).

**MUST NOT**
- Add analytics beacons, font CDNs, or third-party scripts that bill per request.
- Break the accessible, keyboard-operable contract of the existing surface
  (`role`, `aria-live`, focus states) while changing behaviour.

**Why:** a static document has no idle footprint; every render is paid for by the
person looking at it.

---

## Rule 7 — Observe without keeping anything warm

**MUST**
- Emit structured JSON logs (one line per event) from every Lambda; include the
  envelope `id` on every pipeline log line.
- Set explicit CloudWatch Logs retention on every log group. Unbounded retention is
  silent storage spend.
- Alarm on dead-letter-queue depth and on `5XX` rate; alarms are the only always-on
  telemetry the system is allowed to have.

**MUST NOT**
- Third-party APM agents, tracing exporters, or custom metrics whose cost scales with
  cardinality nobody reads.
- Logging full payment payloads, secrets, or PII.

---

## Enforcement matrix

| Gate | Command | Blocks |
| --- | --- | --- |
| AST float gate | `python3 mcp/aegis_mcp_server.py --audit` | any Rule 4 error |
| Protocol self-test | `python3 mcp/aegis_mcp_server.py --dry-run` | broken MCP registry |
| Cost/billing probe | `python3 mcp/aegis_mcp_server.py --probe` | provisioned capacity, dead door |
| Pre-commit hook | `.kiro/hooks/pre-commit` | unreviewed governance drift |
| Template review | `grep -n "ProvisionedThroughput\|Architectures" template.yaml` | Rules 1–2 |

Violations found by these gates are **defects**, not preferences: fix the code, do not
weaken the gate. If a gate is wrong, amend the rule in the same pull request that
changes it.

---

## Amendments

1. Open a pull request that quotes the rule number being changed and links the
   evidence that makes the current rule insufficient.
2. Update this file **and** the enforcing gate (`mcp/aegis_mcp_server.py` for Rules 1–4)
   in the same commit — a rule without an executable check is documentation, not law.
3. Two green gates are required before merge: `--dry-run` and `--audit`.

---

## Glossary

- **Zero-Idle** — idle spend is $0; cost is a function of requests, not of uptime.
- **Envelope** — the `{id, type, payload}` document accepted by `POST /ingest`.
- **Canary row** — the deliberately drifting GL row (±$0.02) that proves the
  reconciliation tolerance is live rather than hard-coded to always match.
- **Zombie resource** — a Terraform-planned resource with no live counterpart
  (orphaned gp3 volume, idle provisioned concurrency) that bills without serving.


