"""PhantomGuard — cloud waste sentry (Decimal only).

The sentry receives one cloud-audit event per resource and decides what the
zero-idle constitution requires. The decision itself lives in
:func:`classify_resource`, which is pure (no AWS, no clock, no float) and is
therefore exercised directly by the test suite; :func:`inspect_cloud_event`
adds the single-table keys and the 24-hour TTL and persists the row.

Resource discovery rules (mirrors ``.kiro/specs/phantom_guard.spec.json``):

* **DR-1 orphaned EBS volume** — a volume that declares an empty ``attachments``
  array or ``state == "available"`` bills for its provisioned size forever and
  serves nothing. Action ``FLAG_ORPHAN``. A volume event that carries neither
  field is *not* flagged: the sentry never invents unattachment.
* **DR-2 zombie EC2 / RDS** — always-on compute is unbudgeted by definition in
  a pay-per-request skeleton. Action ``TERMINATE_SIGNAL_SENT``; the action is a
  label on the audit row, the sentry calls no EC2 or RDS API.
* **DR-3 idle NAT gateway** — a NAT gateway bills a flat hourly charge whatever
  its traffic, so it is flagged as planned idle burn. Action ``FLAG_IDLE_NAT``.

Burn figures arrive as ``cost_per_hour`` and are quantized to micro-dollars
(``Decimal``), never to a binary float; an unparseable or non-finite figure
raises instead of being stored as a made-up number.
"""

import json
import os
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import boto3

# Lambda injects AWS_REGION while botocore resolves endpoints through
# AWS_DEFAULT_REGION; align both before the resource is built.
os.environ.setdefault("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))

dynamodb = boto3.resource('dynamodb')
table_name = os.environ.get('TABLE_NAME', 'AegisStateTable')
table = dynamodb.Table(table_name)

MICRO = Decimal("0.000001")
TTL_SECONDS = 86400                  # 24 hours
UNIDENTIFIED = "UNIDENTIFIED"

ACTION_ALLOW = "ALLOW"
ACTION_TERMINATE = "TERMINATE_SIGNAL_SENT"
ACTION_FLAG_ORPHAN = "FLAG_ORPHAN"
ACTION_FLAG_IDLE_NAT = "FLAG_IDLE_NAT"

RULE_ORPHAN_EBS = "DR-1"
RULE_PERSISTENT_COMPUTE = "DR-2"
RULE_IDLE_NAT = "DR-3"

EBS_TYPES = frozenset({"AWS::EC2::Volume", "aws_ebs_volume"})
NAT_TYPES = frozenset({"AWS::EC2::NatGateway", "aws_nat_gateway"})
PERSISTENT_COMPUTE_TYPES = frozenset({
    "AWS::EC2::Instance",
    "AWS::RDS::DBInstance",
    "aws_instance",
    "aws_db_instance",
})


def burn_text(value, field="cost_per_hour"):
    """Quantize an hourly burn to micro-dollars as a decimal string.

    ``None``/``""`` mean "no estimate reported" and become ``0.000000``; a
    boolean, a non-finite value or unparseable text raises.
    """
    if value is None or value == "":
        value = 0
    if isinstance(value, bool):
        raise ValueError("Burn field '{0}' must be numeric, got boolean.".format(field))
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("Unparseable burn field '{0}': {1!r}".format(field, value)) from error
    if not amount.is_finite():
        raise ValueError("Non-finite burn field '{0}': {1!r}".format(field, value))
    quantized = amount.quantize(MICRO, rounding=ROUND_HALF_UP)
    if quantized == 0:
        quantized = abs(quantized)   # never store "-0.000000"
    return str(quantized)


def _attachment_count(value):
    """Number of attachments declared by the event, or None when unknown.

    Accepts a list (Terraform shape) or its JSON text form (event shape), so an
    empty array is distinguishable from a field the producer never sent.
    """
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            decoded = json.loads(text)
        except ValueError:
            return None
        if isinstance(decoded, list):
            return len(decoded)
    return None



def classify_resource(event_body):
    """Decide one cloud-audit event. Pure: no AWS, no clock, no float."""
    body = event_body if isinstance(event_body, dict) else {}

    resource_type = body.get("resource_type") or "UNKNOWN"
    # Keep a missing id missing: the core falls back to the envelope id, so the
    # sentry must not claim an identity the producer never supplied.
    resource_id = body.get("resource_id") or None
    burn = burn_text(body.get("cost_per_hour"))
    attachments = _attachment_count(body.get("attachments"))
    state = body.get("state")

    if resource_type in PERSISTENT_COMPUTE_TYPES:
        action = ACTION_TERMINATE
        breaker = True
        rule = RULE_PERSISTENT_COMPUTE
        reason = "always-on compute violates the zero-idle rule"
    elif resource_type in EBS_TYPES and (attachments == 0 or state == "available"):
        action = ACTION_FLAG_ORPHAN
        breaker = False
        rule = RULE_ORPHAN_EBS
        reason = "volume is unattached; provisioned size bills every hour"
    elif resource_type in NAT_TYPES:
        action = ACTION_FLAG_IDLE_NAT
        breaker = False
        rule = RULE_IDLE_NAT
        reason = "NAT gateway bills a flat hourly charge regardless of traffic"
    else:
        action = ACTION_ALLOW
        breaker = False
        rule = None
        reason = "no waste-evidence rule matched"

    return {
        "resource_id": resource_id,
        "resource_type": resource_type,
        "estimated_hourly_burn": burn,
        "circuit_breaker_triggered": breaker,
        "action_taken": action,
        "rule_id": rule,
        "reason": reason,
    }


def inspect_cloud_event(event_body):
    """Classify one cloud-audit event and persist the decision (24h TTL)."""
    decision = classify_resource(event_body)
    now = int(time.time())
    resource_id = decision["resource_id"]
    stored_id = resource_id or UNIDENTIFIED

    item = {
        'PK': "GUARD#SECURITY_AUDIT",
        'SK': "RESOURCE#{0}".format(stored_id),
        'resource_type': decision["resource_type"],
        'estimated_hourly_burn': decision["estimated_hourly_burn"],
        'circuit_breaker_triggered': decision["circuit_breaker_triggered"],
        'action_taken': decision["action_taken"],
        'reason': decision["reason"],
        'audited_at': now,
        'ttl': now + TTL_SECONDS,
    }
    if resource_id:
        item['resource_id'] = resource_id
    if decision["rule_id"]:
        item['rule_id'] = decision["rule_id"]

    table.put_item(Item=item)
    return item
