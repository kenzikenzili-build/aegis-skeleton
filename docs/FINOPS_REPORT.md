# Zero-Idle FinOps Report

**AegisSkeleton unit economics: provisioned servers vs. serverless containers vs. Zero-Idle**

> **Status: modeled, not measured.** Every figure below is computed from list prices and the stated workload assumptions. It has not been checked against a deployed stack or an AWS bill (`docs/MILESTONES.md` records no deployed API stack yet; Milestone 2 is open). Prices were gathered on 2026-11-06 from secondary price-tracking sites because the AWS pricing pages could not be opened from the authoring environment. Re-verify them before quoting any number externally. Section 2 lists what was and was not corroborated.

## Summary

| Monthly traffic | A: Provisioned (EC2 + RDS + NAT) | B: ECS Fargate | **C: AegisSkeleton Zero-Idle** |
| --- | --- | --- | --- |
| **Idle (0 req)** | $67.99 (about $75 with a $7 operations allowance) | $34.58 | **$0.00** (about $0.26 of SQS polling at most, see 4.1) |
| **Moderate (100,000 req)** | $67.99 | $34.72 | **$0.32** |
| **High (10,000,000 req)** | $67.99 | $47.97 | **$32.17** |

* **Idle is where the architecture wins outright.** A and B bill every hour of the month whether or not a request arrives. C bills nothing until an event is processed.
* **At 100,000 requests, A costs 211x and B costs 108x what C costs.** At 10 million requests, C costs 52.7% less than A and 32.9% less than B.
* **The advantage is not unlimited.** C's cost grows with every request ($3.22 per million) while A's is flat. They cross at about **21.1 million requests per month** (8.0 requests per second on average), and C and B cross at about 18.4 million. Above those volumes, a fully utilized provisioned server is cheaper per request, provided it can carry the load. Sections 5 and 8 cover this.
* **Unit cost:** one end-to-end event costs about **$0.032 per 10,000 events** ($0.32 per 100,000), before free tiers.

---

## 1. Scope and method

The question: what does it cost to run the AegisSkeleton workload (an event-driven ingest path that writes small records to a key-value store) under three architectures, across three traffic tiers?

**One "request"** is one `POST /ingest` carrying a domain event (`reconciliation` or `cloud_audit`), taken through to a persisted DynamoDB item. This matches the shipped code path: the domain app writes the item, then the core handler writes the same key again, so **a request costs 2 write units**. Items are assumed to be 1 KB or smaller (1 write unit per write).

**Common to all options** (so it cancels out of the comparison and is excluded): data transfer out, DNS, TLS certificates, CloudWatch Logs and metrics, KMS, support plans, taxes.

**Workload assumptions for Option C** (these drive its variable cost):

| Parameter | Value | Basis |
| --- | --- | --- |
| Ingest function | 50 ms at 256 MB per request | Validate and enqueue; an estimate, not a measurement |
| Core function | 300 ms at 256 MB per batch of 10 | Parse, route, two DynamoDB writes per message; an estimate |
| SQS requests per message | 1.2 | 1 send, plus one batched receive and one batched delete per 10 messages |
| Month | 730 hours = 2,628,000 seconds | Standard billing month |

Section 7 shows how far the conclusions move if these durations are wrong.

## 2. Pricing inputs and how well each was verified

All prices are us-east-1, on-demand, USD.

| Input | Value used | Evidence |
| --- | --- | --- |
| EC2 `t4g.small` | $0.0168/h | Three independent price trackers agree |
| EBS `gp3` | $0.08/GB-month | The repo's console uses this constant; not re-checked this session |
| RDS `db.t4g.micro` (single-AZ) | $0.016/h | Two trackers agree |
| RDS `gp3` storage | $0.115/GB-month | Consistent with a published example of $13.98/month for a micro plus 20 GiB ($11.68 + $2.30) |
| NAT Gateway | $0.045/h and $0.045/GB processed | Many sources agree |
| Public IPv4 address | $0.005/h each, attached or not | AWS announcement and AWS re:Post |
| Fargate vCPU / GB (x86) | $0.04048/h and $0.004445/h | Two sources agree |
| Fargate Arm | x86 rate times 0.8 | **Assumed 20 % discount; not re-verified** |
| Application Load Balancer | $0.0225/h plus $0.008 per LCU-hour | One source; **another lists $0.0252/h** (tested in Section 7) |
| API Gateway HTTP API | $1.00 per million (first 300 M) | Weak: one tracker plus a general statement |
| SQS standard | $0.40 per million | **Not verified this session** (AWS page blocked); standard published rate |
| Lambda arm64 | $0.20 per million requests, $0.0000133334 per GB-second | AWS-hosted article and a second source agree |
| DynamoDB on-demand | $0.625 per million write units | Two trackers agree; reflects the November 2024 price cut |
| Operations allowance (A only) | $7.00/month | **Assumption**, not a price: stands in for CloudWatch, backups and snapshots |

Free tiers are **excluded** from the main tables and tested in Section 7.

## 3. The three options

### Option A: Provisioned baseline (EC2 `t4g.small` + RDS `db.t4g.micro` + NAT Gateway)

A single-AZ stack: one instance runs the application, one managed database holds state, and a NAT gateway gives the private subnet outbound access. It is sized once and billed continuously.

| Fixed line item | USD / month |
| --- |---: |
| EC2 t4g.small, on-demand | $12.26 |
| EBS gp3, 20 GB | $1.60 |
| RDS db.t4g.micro, single-AZ | $11.68 |
| RDS gp3 storage, 20 GB | $2.30 |
| NAT Gateway, hourly | $32.85 |
| Public IPv4 x2 (EC2, NAT) | $7.30 |
| **Total, priced items** | **$67.99** |
| Operations allowance (assumption) | $7.00 |
| **Total with allowance** | **$74.99** |

Variable cost per request is taken as **$0**: capacity is pre-bought. (NAT data processing is $0.045 per GB and would add to this if the application sends traffic through the gateway.) The model also assumes one `t4g.small` carries every tier. At 10 million requests the average rate is 3.8 requests per second, but this report did not load-test the instance, and a burstable instance can exhaust its CPU credits under sustained load.

### Option B: ECS Fargate baseline

One always-on Fargate task (Arm, 0.25 vCPU, 0.5 GB) behind an Application Load Balancer, writing to the same DynamoDB on-demand table as C. There are no servers to patch, but the task and the load balancer bill continuously. Fargate has no scale-to-zero in this layout.

| Fixed line item | USD / month |
| --- |---: |
| Fargate task, Arm 0.25 vCPU / 0.5 GB | $7.21 |
| ALB, hourly | $16.43 |
| Public IPv4 x3 (ALB in 2 AZs, task) | $10.95 |
| **Total** | **$34.58** |

| Variable line item | USD per 1M requests |
| --- |---: |
| DynamoDB writes | $1.2500 |
| ALB LCU (new-connection bound) | $0.0889 |
| **Total** | **$1.3389** |

The ALB term assumes every request opens a new connection, which is the worst case for the LCU charge.

### Option C: AegisSkeleton Zero-Idle (HTTP API + SQS + Graviton Lambda + DynamoDB on-demand)

The template in this repository. Nothing is provisioned, so the fixed cost is **$0.00**.

| Variable line item | USD per 1M requests |
| --- |---: |
| API Gateway HTTP API | $1.0000 |
| SQS requests | $0.4800 |
| Ingest Lambda | $0.3667 |
| Core Lambda | $0.1200 |
| DynamoDB writes | $1.2500 |
| **Total** | **$3.2167** |

That is **$0.3217 per 100,000 requests** and $0.03217 per 10,000. For reference, `mcp/finops_sentry.py` reports $0.004 per 10,000 invocations; that figure matches the **ingest Lambda alone** ($0.00367 per 10,000), not the end-to-end path.

## 4. Results by traffic tier

| Tier | Requests / month | A: priced items | A: with $7 allowance | B: Fargate | **C: Zero-Idle** |
| --- |---: |---: |---: |---: |---: |
| **Idle** | 0 | $67.99 | $74.99 | $34.58 | **$0.00** |
| **Moderate** | 100,000 | $67.99 | $74.99 | $34.72 | **$0.32** |
| **High** | 10,000,000 | $67.99 | $74.99 | $47.97 | **$32.17** |

Effective cost per million requests (fixed cost spread over the traffic):

| Tier | A: priced items | A: with allowance | B | **C** |
| --- |---: |---: |---: |---: |
| Moderate (100,000 req) | $679.94 | $749.94 | $347.17 | $3.22 |
| High (10,000,000 req) | $6.80 | $7.50 | $4.80 | $3.22 |

Savings of C against each baseline:

| Tier | vs A | vs B |
| --- |---: |---: |
| Idle | $67.99/month ($815.93/year) | $34.58/month ($415.00/year) |
| Moderate (100k) | $67.67 (99.5%) | $34.40 (99.1%) |
| High (10M) | $35.83 (52.7%) | $15.81 (32.9%) |

### 4.1 Is the idle cost really zero?

Almost. C has no provisioned resource, so compute, API and database charges are $0 at idle, and DynamoDB TTL keeps stored data from accumulating. One idle-time charge remains. AWS documents that Lambda long-polls an SQS queue and that the empty receives are billable SQS requests. A third-party measurement reports up to five pollers, each polling about every 20 seconds; on that basis the idle queue sees about 657,000 receives a month, or **$0.26 if none of the SQS free tier is available** (1 million requests a month, per AWS guidance quoted in Appendix B). The poller count and interval are estimates, not AWS figures. Either way it is a per-request fee, not provisioned capacity.

## 5. Cost model and break-even formulas

Let `n` be requests per month. Every option has the form

```
C_X(n) = F_X + v_X * n          F = fixed monthly cost,  v = variable cost per request
```

**Option C's variable cost**, with its parameters (Section 1):

```
v_C = p_http + r*p_sqs + (p_req + t_i*g*p_gbs) + (p_req + t_c*g*p_gbs)/b + w*p_wru
```

| Symbol | Meaning | Value |
| --- | --- | --- |
| `p_http` | HTTP API, per request | $1.00 / 10^6 |
| `r`, `p_sqs` | SQS requests per message; price per request | 1.2; $0.40 / 10^6 |
| `p_req`, `p_gbs` | Lambda per request; per GB-second (arm64) | $0.20 / 10^6; $0.0000133334 |
| `t_i`, `t_c`, `g` | ingest seconds; core seconds per batch; memory in GB | 0.050; 0.300; 0.25 |
| `b` | messages per core invocation | 10 |
| `w`, `p_wru` | write units per request; price per write unit | 2; $0.625 / 10^6 |

Substituting gives `v_C = $0.000003217` per request, which is **$3.2167 per million**. Its fixed cost is `F_C = 0`.

**Break-even.** Option X (higher fixed cost, lower variable cost) becomes cheaper than option Y at

```
n*(X,Y) = (F_X - F_Y) / (v_Y - v_X)            average rate  lambda* = n* / 2,628,000 s
```

| Pair | Calculation | n* (requests / month) | Average rate |
| --- | --- |---: |---: |
| A vs C | $67.99 / $0.000003217 | **21,138,022** | 8.04 req/s |
| A (with allowance) vs C | $74.99 / $0.000003217 | **23,314,187** | 8.87 req/s |
| B vs C | $34.58 / ($0.000003217 - $0.000001339) | **18,416,980** | 7.01 req/s |

Below `n*`, C is the cheapest option; above it, the provisioned option is. Equivalently, the provisioned option wins only when it is busy enough that its fixed cost spread over `n` falls under C's flat `$3.22` per million.

## 6. Cost curves

Monthly cost against monthly requests (A uses priced items only). `*` marks overlap.

```
     $100 |
          |                                                          CCC
          |                                                       CCCC
          |                                                    CCCC
      $80 |                                                 CCCC
          |                                              CCCC       BBBB
          |AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA*****AAA********AAA
          |                                       CCC*BBBBBBBB
      $60 |                                   B****BBB
          |                           BBBBBB***C
          |                    BBBBBBBB  CCCC
          |            BBBBBBBBB      CCCC
      $40 |     BBBBBBBB           CCCC
          |BBBBBB               CCCC
          |                  CCCC
          |              CCCCC
      $20 |           CCCC
          |        CCCC
          |     CCCC
          |  CCCC
       $0 |CCC
          +-------------------------------------------------------------
           0M        5M        10M       15M       20M       25M       30M
          requests per month (millions)
```

Legend: `A` provisioned, priced items only ($67.99/month flat) · `B` Fargate · `C` AegisSkeleton Zero-Idle.

C starts at $0 and crosses B near 18M and A near 21M. The 10M tier sits left of both crossings.

Effective cost per million requests, log-log. A and B fall as their fixed cost is spread over more traffic; C is flat.

```
  $10,000 |
          |AAA
          |  AAA
          |BBB AAAA
          |  BBBB AAAA
          |     BBBB AAA
   $1,000 |        BBB AAAA
          |          BBBB AAAA
          |             BBBB AAA
          |                BBB AAAA
          |                  BBBB AAAA
          |                     BBBB AAA
     $100 |                        BBBBAAAA
          |                           BBB AAAA
          |                             BBBB AAA
          |                                BBBBAAAA
          |                                   BBBBAAAA
          |                                      BBBBAAA
      $10 |                                         BBB*AAA
          |                                            BBB*AAA
          |                                               BBB**A
          |CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC*****CCCCCCCCC
          |                                                       ****BBBBB
          |                                                          AAA  BB
       $1 |                                                            AA
          +-----------------------------------------------------------------
           10^4            10^5            10^6            10^7            10^8
          requests per month
```

Legend as above. The y-axis is dollars per million requests (fixed plus variable cost, divided by traffic), on a log scale.

At low volume the fixed cost dominates and C is orders of magnitude cheaper per request. C's flat line meets B's curve near 18M requests and A's near 21M, the same points as the break-even table.

## 7. Sensitivity: how robust is the conclusion?

Each row changes one assumption. "Break-even" is the monthly request volume at which the provisioned option becomes cheaper than C.

| Scenario | C per 1M req | C per 100k | A vs C break-even | B vs C break-even |
| --- | --- | --- | --- | --- |
| Base case | $3.22 | $0.322 | 21.1M | 18.4M |
| Remove the redundant domain write (1 write unit per request) | $2.59 | $0.259 | 26.2M | 27.6M |
| Lambda durations doubled (100 ms ingest, 600 ms core) | $3.48 | $0.348 | 19.5M | 16.1M |
| Add 1 KB of CloudWatch Logs per request at an assumed $0.50/GB | $3.72 | $0.372 | 18.3M | 14.5M |
| A bought on 1-year terms (assumes 28 % off the EC2 and RDS lines, the RDS figure one source lists) | $3.22 | $0.322 | 19.1M | 18.4M |
| B with the ALB at $0.0252/h, as one source lists it | $3.22 | $0.322 | 21.1M | 19.5M |
| B on x86 Fargate instead of Arm | $3.22 | $0.322 | 21.1M | 19.4M |

What the table shows:

* **The ranking at idle, 100k and 10M does not change in any row** (checked in the generating script). The A-versus-C break-even stays between 18.3M and 26.2M requests a month, above the High tier in every case.
* **The redundant write is C's biggest avoidable cost.** The domain app and the core handler write the same key, so each event pays for two write units. Writing once lowers C to $2.59 per million. That is a code change, not an architecture change.
* **Lambda duration matters less than expected.** Doubling both durations raises C by about $0.27 per million.
* **Free tiers help C at small scale.** With the monthly Lambda and SQS free tiers, C costs $0.23 at 100k requests (instead of $0.32) and $28.90 at 10M (instead of $32.17). An API Gateway free tier is not assumed: it is limited to new accounts and its current terms could not be verified. Neither A nor B is credited with a free tier either.
* **A is modeled single-AZ.** A highly available version needs a second instance, a second NAT gateway and a Multi-AZ database. The model gives A its cheaper, less available configuration, so it understates A's cost.

## 8. Where Zero-Idle does not win

This report should not be read as "serverless is always cheaper."

1. **Sustained high volume.** Beyond about 21M requests a month (8 requests per second on average), a busy provisioned server costs less per request. At 100 million requests a month, C would cost about $322 against A's $68, if one small instance could serve that traffic, which it likely could not without more nodes.
2. **Steady, predictable load.** Reserved or savings-plan pricing lowers A's fixed cost, which moves the break-even down (Section 7).
3. **Latency-critical paths.** Lambda cold starts and the SQS buffer add latency that a warm server does not have. This report prices cost only.
4. **Managed-service limits.** Rule 2 puts every reconciliation row under one partition key value, so very high write rates need a load test before relying on C.

The AegisSkeleton workloads (reconciliation batches and cloud audits) are bursty and low on average: even 10 million events a month is 3.8 per second. That puts them far to the left of the break-even, which is where the architecture is strongest.

## 9. Impact illustration (not a forecast)

* **The two derived apps.** Giving `grim-reconciler` and `phantom-guard` each their own Option A stack would cost $1,631.86 a year at idle before any traffic. Under C it is $0 at idle.
* **A fleet of 100 low-traffic services**, each at 100,000 requests a month: $6,799.40 a month under A (one stack per service) against $32.17 under C. Sharing one stack across services would narrow that gap, at the cost of isolation.

## 10. Limitations

* **Modeled, not measured.** No deployed stack exists yet; durations and request sizes are estimates.
* **Prices are from secondary sources** and a few are assumptions (Fargate Arm discount, SQS rate, the operations allowance). See Section 2.
* **Excluded from all options:** data transfer, CloudWatch, KMS, DNS, support, tax. CloudWatch Logs is sized in Section 7 only.
* **Security and protection are not priced.** The API defined in `template.yaml` has no authorizer or throttling configured. Adding either (or AWS WAF) adds a fixed or per-request charge to C that is not in these tables.
* **Capacity is assumed for A and B** at every tier; no load test was run.
* **us-east-1 only.** Other regions differ.

## Appendix A. Reproduce the numbers

Save as `finops_model.py` and run `python3 finops_model.py`. It uses `decimal.Decimal` throughout (Rule 4) and the standard library only.

```python
"""Zero-Idle FinOps model for AegisSkeleton. Standard library only (Decimal).
Prices are us-east-1 list prices in USD; see Section 2 for their sources."""
from decimal import Decimal as D, ROUND_HALF_UP

HOURS = D(730)                       # billing hours per month
MILLION = D(10) ** 6
SECONDS = HOURS * 3600               # 2,628,000 seconds per billing month

# ---- unit prices ---------------------------------------------------------
EC2_T4G_SMALL_HR = D("0.0168")
EBS_GP3_GB_MO = D("0.08")
RDS_T4G_MICRO_HR = D("0.016")
RDS_GP3_GB_MO = D("0.115")
NAT_HR = D("0.045")
IPV4_HR = D("0.005")
FARGATE_VCPU_HR = D("0.04048") * D("0.8")     # x86 rate; Arm taken at 80 %
FARGATE_GB_HR = D("0.004445") * D("0.8")
ALB_HR = D("0.0225")
ALB_LCU_HR = D("0.008")
HTTP_API_PER_M = D("1.00")
SQS_PER_M = D("0.40")
LAMBDA_REQ_PER_M = D("0.20")
LAMBDA_GB_SECOND = D("0.0000133334")          # arm64
DDB_WRU_PER_M = D("0.625")
OPS_ALLOWANCE = D("7.00")                     # assumption, not a list price

# ---- workload: one domain event through POST /ingest -----------------------
WRU_PER_REQUEST = D(2)               # domain app write + core write, same key
INGEST_SECONDS = D("0.050")          # per request
CORE_SECONDS = D("0.300")            # per 10-message batch
BATCH = D(10)
MEMORY_GB = D("0.25")                # 256 MB
SQS_REQUESTS_PER_MESSAGE = D("1.2")  # 1 send + 0.1 receive + 0.1 delete
NEW_CONNECTIONS_PER_LCU_SECOND = D(25)


def usd(value, places=2):
    q = D(value).quantize(D(10) ** -places, rounding=ROUND_HALF_UP)
    return "${:,.{p}f}".format(q, p=places)


def total(items):
    return sum(items.values(), D(0))


# ---- Option A: EC2 t4g.small + RDS db.t4g.micro + NAT Gateway (single AZ) ---
A_FIXED = {
    "EC2 t4g.small, on-demand": EC2_T4G_SMALL_HR * HOURS,
    "EBS gp3, 20 GB": D(20) * EBS_GP3_GB_MO,
    "RDS db.t4g.micro, single-AZ": RDS_T4G_MICRO_HR * HOURS,
    "RDS gp3 storage, 20 GB": D(20) * RDS_GP3_GB_MO,
    "NAT Gateway, hourly": NAT_HR * HOURS,
    "Public IPv4 x2 (EC2, NAT)": 2 * IPV4_HR * HOURS,
}
A_VAR = D(0)                         # no per-request fee; capacity is pre-bought

# ---- Option B: ECS Fargate (Arm, 0.25 vCPU / 0.5 GB, 1 task) + ALB + DynamoDB
B_FIXED = {
    "Fargate task, Arm 0.25 vCPU / 0.5 GB":
        (D("0.25") * FARGATE_VCPU_HR + D("0.5") * FARGATE_GB_HR) * HOURS,
    "ALB, hourly": ALB_HR * HOURS,
    "Public IPv4 x3 (ALB in 2 AZs, task)": 3 * IPV4_HR * HOURS,
}
B_VAR = {
    "DynamoDB writes": WRU_PER_REQUEST * DDB_WRU_PER_M / MILLION,
    "ALB LCU (new-connection bound)":
        ALB_LCU_HR / (NEW_CONNECTIONS_PER_LCU_SECOND * 3600),
}

# ---- Option C: AegisSkeleton (HTTP API + SQS + Graviton Lambda + DynamoDB) ---
C_FIXED = {}


def c_var(wru=WRU_PER_REQUEST, ingest_s=INGEST_SECONDS, core_s=CORE_SECONDS):
    req = LAMBDA_REQ_PER_M / MILLION
    return {
        "API Gateway HTTP API": HTTP_API_PER_M / MILLION,
        "SQS requests": SQS_REQUESTS_PER_MESSAGE * SQS_PER_M / MILLION,
        "Ingest Lambda": req + ingest_s * MEMORY_GB * LAMBDA_GB_SECOND,
        "Core Lambda": (req + core_s * MEMORY_GB * LAMBDA_GB_SECOND) / BATCH,
        "DynamoDB writes": wru * DDB_WRU_PER_M / MILLION,
    }


F_A, F_B, F_C = total(A_FIXED), total(B_FIXED), total(C_FIXED)
V_A, V_B, V_C = A_VAR, total(B_VAR), total(c_var())


def cost(fixed, var, n):
    return fixed + var * D(n)


def breakeven(fixed_hi, var_hi, fixed_lo, var_lo):
    """Monthly requests above which the high-fixed / low-variable option wins."""
    return (fixed_hi - fixed_lo) / (var_lo - var_hi)


def c_cost_with_free_tier(n):
    """Option C after Lambda (1M req, 400k GB-s) and SQS (1M req) free tiers."""
    n = D(n)
    gb_seconds = n * INGEST_SECONDS * MEMORY_GB + (n / BATCH) * CORE_SECONDS * MEMORY_GB
    lambda_requests = n + n / BATCH
    sqs_requests = n * SQS_REQUESTS_PER_MESSAGE

    def billed(used, free):
        return max(D(0), used - free)

    return (n * HTTP_API_PER_M / MILLION
            + billed(sqs_requests, MILLION) * SQS_PER_M / MILLION
            + billed(lambda_requests, MILLION) * LAMBDA_REQ_PER_M / MILLION
            + billed(gb_seconds, D(400_000)) * LAMBDA_GB_SECOND
            + n * WRU_PER_REQUEST * DDB_WRU_PER_M / MILLION)


# Idle polling estimate: 5 pollers, each long-polling about every 20 s (third-party measurement).
IDLE_RECEIVES = D(5) * 3 * 60 * HOURS


if __name__ == "__main__":
    print("Fixed monthly:  A {}  A+ops {}  B {}  C {}".format(
        usd(F_A), usd(F_A + OPS_ALLOWANCE), usd(F_B), usd(F_C)))
    print("Variable / 1M:  A {}  B {}  C {}".format(
        usd(V_A * MILLION, 4), usd(V_B * MILLION, 4), usd(V_C * MILLION, 4)))
    for label, n in (("Idle", 0), ("Moderate", 100_000), ("High", 10_000_000)):
        print("{:9}{:>11,} req   A {:>8}   A+ops {:>8}   B {:>8}   C {:>8}".format(
            label, n, usd(cost(F_A, V_A, n)), usd(cost(F_A + OPS_ALLOWANCE, V_A, n)),
            usd(cost(F_B, V_B, n)), usd(cost(F_C, V_C, n))))
    for name, hi, lo in (("A  vs C", (F_A, V_A), (F_C, V_C)),
                         ("A+ops vs C", (F_A + OPS_ALLOWANCE, V_A), (F_C, V_C)),
                         ("B  vs C", (F_B, V_B), (F_C, V_C))):
        n_star = breakeven(hi[0], hi[1], lo[0], lo[1])
        print("Break-even {:10} {:>12,.0f} req/month = {:.2f} req/s average".format(
            name, n_star, n_star / SECONDS))
    print("Idle SQS polling: {:,} receives/month = {} without the free tier".format(
        IDLE_RECEIVES, usd(IDLE_RECEIVES * SQS_PER_M / MILLION, 4)))
```

Expected output:

```text
Fixed monthly:  A $67.99  A+ops $74.99  B $34.58  C $0.00
Variable / 1M:  A $0.0000  B $1.3389  C $3.2167
Idle               0 req   A   $67.99   A+ops   $74.99   B   $34.58   C    $0.00
Moderate     100,000 req   A   $67.99   A+ops   $74.99   B   $34.72   C    $0.32
High      10,000,000 req   A   $67.99   A+ops   $74.99   B   $47.97   C   $32.17
Break-even A  vs C      21,138,022 req/month = 8.04 req/s average
Break-even A+ops vs C   23,314,187 req/month = 8.87 req/s average
Break-even B  vs C      18,416,980 req/month = 7.01 req/s average
Idle SQS polling: 657,000 receives/month = $0.2628 without the free tier
```

## Appendix B. Sources

Prices and behaviours were taken from these pages on 2026-11-06. Content was rephrased for compliance with licensing restrictions; check each page for the current figure.

* EC2 `t4g.small`: [economize.cloud](https://www.economize.cloud/resources/aws/pricing/ec2/t4g.small/), [doit.com](https://www.doit.com/compute/spot/us-east-1/t4g.small), [vantage.sh](https://instances.vantage.sh/aws/ec2/t4g.small)
* RDS `db.t4g.micro` and storage example: [usage.ai](https://instances.usage.ai/aws/rds/db.t4g.micro), [usage.ai RDS calculator](https://www.usage.ai/blogs/aws/reserved-instances/rds/pricing-calculator), [vantage.sh](https://instances.vantage.sh/aws/rds/db.t4g.micro?currency=USD)
* NAT Gateway: [AWS VPC documentation](https://docs.aws.amazon.com/vpc/latest/userguide/nat-gateway-pricing.html), [costgoat.com](https://costgoat.com/pricing/aws-nat-gateway), [awsfundamentals.com](https://awsfundamentals.com/blog/avoid-nat-gateway-costs)
* Public IPv4 charge: [AWS announcement](https://aws.amazon.com/about-aws/whats-new/2024/02/aws-free-tier-750-hours-free-public-ipv4-addresses/), [AWS re:Post](https://repost.aws/knowledge-center/vpc-optimize-ipv4-usage)
* Fargate (x86 rates): [dev.to](https://dev.to/clickit_devops/the-cost-of-hosting-an-app-in-aws-fargate-4j8g), [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/)
* Load balancer: [apis.io](https://apis.io/plans/amazon-elastic-load-balancing/amazon-elastic-load-balancing-plans-pricing/), [cloudchipr.com](https://cloudchipr.com/blog/aws-load-balancer-pricing)
* Lambda: [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing), [AWS Builder article (Arm rate)](https://builder.aws.com/content/3KVvIb9GOBUUwLwDHQiiLSgkPZo/you-dont-pay-for-the-wait-lambda-web-functions-for-ai), [cloudzero.com](https://www.cloudzero.com/blog/lambda-pricing/), [free tier](https://community.aws/content/3KlRhfU9w7K79H2be1uFAjiX62N)
* DynamoDB on-demand: [usage.ai](https://www.usage.ai/blogs/aws/reserved-instances/dynamodb/on-demand-vs-provisioned/), [dynobase.dev](https://dynobase.dev/dynamodb-pricing-calculator/), [AWS price-cut announcement](https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-dynamo-db-reduces-prices-on-demand-throughput-global-tables)
* HTTP API: [positioniseverything.net](https://www.positioniseverything.net/tools/amazon-api-gateway/pricing/)
* SQS free tier (1 million requests a month), as quoted in Amazon's SQS best-practice guidance: [advertising.amazon.com](https://advertising.amazon.com/API/docs/en-us/amazon-marketing-stream/aws-best-practices)
* SQS polling behaviour: [AWS Lambda documentation](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html), [AWS Knowledge Center](https://aws.amazon.com/premiumsupport/knowledge-center/sqs-high-charges/), [third-party measurement](https://medium.com/@mr-beem/the-hidden-cost-of-idle-sqs-queues-how-i-hit-1-million-api-calls-without-sending-a-single-message-1c7c5a16dbaf)
