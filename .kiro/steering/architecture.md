# AegisSkeleton — Architecture Steering

> **Status:** ENFORCED (machine-checked)
> **Owner:** Skeleton Crew
> **Applies to:** `apps/**`, `src/**`, `frontend/**`, `mcp/**`, `scripts/**`
> **Spec source:** `.kiro/specs/core-architecture.spec.json`
> **IaC source of truth:** `template.yaml` (SAM / CloudFormation)
> **Mechanical enforcement:** `.kiro/hooks/pre-commit` → `AegisASTSmiteGuard`

This document is the binding governance contract for every derived Aegis
application (`GrimReconciler`, `PhantomGuard`, and any future spawn). When this
document and a pull request disagree, this document wins. When this document
and `template.yaml` disagree, **`template.yaml` wins and this document must be
amended in the same commit**.

---

## Rule 1 — Zero-Mock Rule (no counterfeit persistence)

**The rule.** No `localStorage`, no `sessionStorage`, no `IndexedDB`, and no
in-memory arrays / dicts / sets standing in for a database. A "database" that
vanishes when the process or tab exits is a *mirage*, not persistence. It
fabricates a passing demo and hides real integration failures.

Forbidden by category:

| Category | Forbidden | Why |
| --- | --- | --- |
| Browser persistence | `localStorage`, `sessionStorage`, `indexedDB` | Survives reloads, never leaves the device, cannot be reconciled. |
| Module/class-level mutable stores | `LEDGER = []`, `_CACHE = {}`, `class InMemoryDatabase:` | Process-local state masquerading as the system of record. |
| Function-local accumulators named as stores | `db = []` … `db.append(...)` | Same mirage, scoped to a single invocation. |
| Instance-level shadow stores | `self._store = {}` … `self._store[key] = value` | Hides a phantom table inside an object. |
| Local file/JSON/CSV treated as live state | — | Fixtures are test inputs only; runtime truth lives in DynamoDB. |

**The only authorized persistence layer** is `AegisStateTable` (DynamoDB,
on-demand, TTL-bounded) reached through `boto3`. Fixtures under `fixtures/` are
read-only reconciliation inputs for tests; writing runtime state back into them
is a Rule 1 violation.

**Enforcement.** `AegisASTSmiteGuard` in `.kiro/hooks/pre-commit` parses every
staged Python file with `ast` and rejects:

* `requests.get` / `requests.post` / any `requests`, `urllib`, `urllib2`,
  `urllib3`, `httpx`, `aiohttp`, `http.client`, `ftplib`, `telnetlib`,
  `smtplib`, `xmlrpc` usage — including aliased and dynamic imports.
* raw socket egress (`socket.socket`, `socket.create_connection`,
  `socket.socketpair`).
* store-shaped containers (`*_db`, `*_store`, `*_cache`, `*_ledger`,
  `*_registry`, `*_records`, `*_queue`, `*_table`, `*_session`, `*_dataset`,
  `*_mock`, `*_fake`, `*_stub`, `*_inmemory`) that are declared as list/dict/set
  literals or `list()`/`dict()`/`set()`/`collections.defaultdict()` and then
  mutated (`.append()`, `.update()`, `... [k] = v`, `del ...`, `.pop()`, …).
* mock-shaped storage classes and factories (`InMemoryDatabase`, `MockTable`,
  `FakeLedger`, `StubStore`, `build_mock_store`, …).

Exit code `1` blocks the commit. There is no waiver flag: fix the code.

---

## Rule 2 — Single-Table DynamoDB schema (`PK` / `SK`, strict TTLs)

One table. One key pair. One TTL attribute. Never a second table, never a GSI
sprinkle to work around access patterns.

`template.yaml` → `AegisStateTable`:

| Attribute | Contract |
| --- | --- |
| `BillingMode` | `PAY_PER_REQUEST` (no provisioned RCU/WCU, no idle capacity) |
| `PK` | `S`, `KeyType: HASH` — partition / aggregate root |
| `SK` | `S`, `KeyType: RANGE` — sort / entity discriminator |
| `ttl` | `TimeToLiveSpecification.AttributeName: ttl`, `Enabled: true` |

**Key conventions (must be reused by every app):**

| Aggregate | `PK` | `SK` | TTL budget |
| --- | --- | --- | --- |
| Core event ledger (`src/functions/app.py`) | `TENANT#SYSTEM` | `EVENT#{event_type}#{item_id}` | `86400` s (24 h) |
| Reconciliation ledger (`apps/grim-reconciler`) | `RECONCILE#LEDGER` | `TX#{transaction_id}` | `604800` s (7 d) |
| Security audit (`apps/phantom-guard`) | `GUARD#SECURITY_AUDIT` | `RESOURCE#{resource_id}` | `86400` s (24 h) |

**TTL invariants**

* `ttl` is an **integer epoch-seconds** value: `int(time.time()) + budget`.
* `ttl` is always present on write — an item without `ttl` is unbounded storage
  and therefore unbounded cost. That is a hard failure.
* TTL budgets are declared above; a new budget requires an amendment to this
  document in the same commit.
* DynamoDB TTL deletion is asynchronous (hours, not seconds). Dashboards must
  never assume an expired item is already gone.
* Monetary values are persisted as `str(Decimal)`, never as `float` (see Rule 4).

**Enforcement.** `mcp/finops_sentry.py::verify_compliance` refuses EC2/RDS in
`template.yaml`; reviewers reject any second `AWS::DynamoDB::Table`, and
`template.yaml` keys are the review checklist.

---

## Rule 3 — Serverless ARM64 Graviton, scale-to-zero, `$0.0000/hr` idle

**The rule.** Idle must cost exactly zero. Compute exists only while an event is
being processed, runs on Graviton (`arm64`), and dies immediately afterwards.

Contract inherited from `Globals.Function` in `template.yaml`:

| Setting | Value | Rationale |
| --- | --- | --- |
| `Architectures` | `arm64` (Graviton2) | ~20 % cheaper per GB-second than x86 at equal memory |
| `Runtime` | `python3.12` | Matches the verified interpreter baseline |
| `MemorySize` | `256` MB | Smallest size that keeps p95 under the 10 s budget |
| `Timeout` | `10` s | Upper bound on any single unit of work |

Queue contract (`AegisIngestionQueue`): `VisibilityTimeout: 30`,
`MessageRetentionPeriod: 345600` (4 days), redrive to `AegisDeadLetterQueue`
with `maxReceiveCount: 3`. All inbound work is buffered here first — never
invoked synchronously, never long-polled.

**Forbidden — these bill while idle:**

* `AWS::EC2::Instance`, `AWS::RDS::DBInstance`, `AWS::ECS::Service` with a
  desired count above zero, self-managed brokers, bastion hosts.
* Provisioned concurrency on Lambda, or provisioned DynamoDB capacity.
* Unattached EBS volumes (`gp3` included), idle Elastic IPs, NAT gateways,
  idle load balancers, snapshot sprawl.
* Any always-on scheduler that replaces event-driven invocation.

**Idle-cost ledger:** `$0.0000/hr` when no traffic arrives — DynamoDB is
pay-per-request, SQS is pay-per-message, Lambda bills only for invocations, and
the table's TTL reclaims cold data for free.

**Enforcement.** `fixtures/tfplan.json` is the regression input for this rule:
it intentionally contains an unattached 100 GB `gp3` volume and an idle
provisioned-concurrency config so the FinOps analyzer (Stage 2) has something
real to smite. A plan that reports such resources must fail the cost gate.

---

## Rule 4 — `decimal.Decimal` mandatory for all financial variances

**The rule.** Money never touches `float`. Every monetary value, fee, net,
variance and tolerance is parsed and compared as `decimal.Decimal`.

Why: IEEE-754 binary floats cannot represent base-10 cents.

```python
>>> 0.1 + 0.2 == 0.3
False
>>> 400.19 - 400.17
0.02000000000003865          # phantom variance
```

That is exactly how a reconciler invents a $0.02 discrepancy that never existed,
or worse, hides a real one inside noise. Binary floats in a ledger are a
correctness defect, not a style preference.

**Mandatory patterns**

```python
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")

def money(raw):
    """Parse external money into Decimal — never via float."""
    return Decimal(str(raw)).quantize(CENT, rounding=ROUND_HALF_UP)

gross = money("400.19")
fee   = money(gross * Decimal("0.029") + Decimal("0.30"))
net   = money(gross - fee)
variance = net - money("400.17")      # exact: Decimal('0.0200')
```

**Prohibited**

* `float(...)` on monetary strings; `round(x, 2)` on money.
* `abs(a - b) < 0.01` style epsilon tolerance — compare `Decimal` against an
  explicit `Decimal` tolerance instead.
* Persisting money as `float` in DynamoDB; store `str(Decimal)`.
* Silent `quantize` without an explicit `rounding=` mode.

**Fixture contract (`fixtures/`, generated by `scripts/generate_fixtures.py`):**

| File | Shape | Purpose |
| --- | --- | --- |
| `stripe_settlements.csv` | 100 rows, Stripe export columns, integer-cent native fields + `*_decimal` strings, UTC timestamps (`created` epoch + `created_utc` ISO-8601) | Authoritative settlement side |
| `internal_gl.csv` | 100 rows, EST timestamps (fixed `-05:00`), `source_ref` = Stripe `balance_transaction` | Internal books side |
| `internal_gl.csv` row index 42 | deterministic `-0.02` drift on `net_amount` | Proves the reconciler measures variance with `Decimal`, not `float` |
| `tfplan.json` | Terraform plan JSON (`format_version` 1.2) with unattached 100 GB `gp3` + idle provisioned concurrency | FinOps waste regression input |

Row index `42` (0-based, excluding the header) is the canary. A reconciler that
reports any variance other than exactly `Decimal("-0.02")` for that row — or
that reports noise on the other 99 rows — is non-compliant with Rule 4.

**Enforcement.** `AegisASTSmiteGuard` in `.kiro/hooks/pre-commit` (rule
`AEGIS-FLOAT`, method `_check_float_currency_math`) rejects `+ - * /` when a name
containing the token `amount`, `balance`, `cost`, `fee` or `price` meets a float
literal or a `float()` call. It covers plain expressions, augmented assignment
(`fee *= 0.029`), and float arithmetic assigned into a monetary name
(`fee = gross * 0.029 + 0.30`). Exit code `1` blocks the commit. Tests:
`tests/test_ast_float_guard.py`.

The check is syntactic, so these remain review items: a float held in a plain
variable (`rate = 0.029`), `round(amount, 2)` on its own, epsilon comparisons, and
`Decimal(0.1)` built from a float.

---

## Delivery gate

A change may merge only when all of the following hold:

1. `python3 .kiro/hooks/pre-commit` exits `0` (staged Python + web assets clean).
2. `python3 scripts/generate_fixtures.py` regenerates byte-identical fixtures
   (deterministic, stdlib-only, no network access).
3. `template.yaml` still declares exactly one DynamoDB table with `PK`/`SK` and
   an enabled `ttl`, `PAY_PER_REQUEST`, and `arm64` functions.
4. No monetary value is derived through `float`.

Install the hook locally (one-time, opt-in):

```bash
chmod +x .kiro/hooks/pre-commit
ln -sf ../../.kiro/hooks/pre-commit .git/hooks/pre-commit
```

## Amendment process

Rule changes require a single commit that updates, atomically:

* `.kiro/specs/core-architecture.spec.json` (intent),
* this file (contract + rationale),
* `template.yaml` (if infra is affected) — **read-only in Stage 1**,
* `.kiro/hooks/pre-commit` (mechanical enforcement),
* `scripts/generate_fixtures.py` (fixtures that prove the change).

A rule that is not enforced by the hook is advisory; advisory rules are not
steering.
