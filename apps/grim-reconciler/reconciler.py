"""GrimReconciler — payment reconciliation core (Decimal only).

Rule 4 of the Zero-Idle constitution forbids IEEE-754 arithmetic on money.
Every monetary value that enters this module is parsed into ``decimal.Decimal``
and quantized to whole cents with ``ROUND_HALF_UP`` before it is compared or
stored, so ``17.665`` becomes ``17.67`` and replaying a message is idempotent.
Non-finite, boolean and unparseable values raise instead of being coerced —
an unreadable amount must never become a plausible-looking ledger row.

The decision itself lives in :func:`evaluate_transaction`, which touches no AWS
resource and no clock, so it is testable in isolation.
:func:`reconcile_transaction` persists that decision into ``AegisStateTable``
(single table, ``PK = RECONCILE#LEDGER``, 7-day TTL).
"""

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

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.02")          # ±$0.02 precision-drift tolerance
TTL_SECONDS = 604800                 # 7 days

STATUS_SETTLED = "SETTLED"
STATUS_MATCHED = "MATCHED"
STATUS_FLAGGED = "FLAGGED_DISCREPANCY"

REASON_CONFIRMED = "SETTLEMENT_CONFIRMED"
REASON_NON_SUCCESS = "NON_SUCCESS_STATUS"
REASON_EXACT_MATCH = "EXACT_CENT_MATCH"
REASON_PRECISION_DRIFT = "PRECISION_DRIFT"
REASON_EXCEEDS_TOLERANCE = "VARIANCE_EXCEEDS_TOLERANCE"


def money_text(value, field="amount"):
    """Return a two-decimal ROUND_HALF_UP string for a monetary value.

    ``bool`` is rejected (``True`` is not one dollar), ``float`` is read through
    its decimal text rather than its binary payload, and non-finite values raise.
    """
    if value is None or value == "":
        value = 0
    if isinstance(value, bool):
        raise ValueError("Monetary field '{0}' must be numeric, got boolean.".format(field))
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(
            "Unparseable monetary field '{0}': {1!r}".format(field, value)
        ) from error
    if not amount.is_finite():
        raise ValueError(
            "Non-finite monetary field '{0}': {1!r}".format(field, value)
        )
    quantized = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if quantized == 0:
        quantized = abs(quantized)   # never persist "-0.00"
    return str(quantized)


def evaluate_transaction(event_body):
    """Decide the reconciliation state of one transaction event.

    Decision table (first match wins, money compared in exact cents):

    ==============================  ===================================  =========================
    Evidence                        Result                               Reason
    ==============================  ===================================  =========================
    no ledger, status SUCCESS       ``SETTLED``                          SETTLEMENT_CONFIRMED
    no ledger, any other status     ``FLAGGED_DISCREPANCY``              NON_SUCCESS_STATUS
    ledger equal, status SUCCESS    ``SETTLED``                          EXACT_CENT_MATCH
    ledger equal, not SUCCESS       ``MATCHED``                          EXACT_CENT_MATCH
    ledger differs by <= $0.02      ``FLAGGED_DISCREPANCY``              PRECISION_DRIFT
    ledger differs by > $0.02       ``FLAGGED_DISCREPANCY``              VARIANCE_EXCEEDS_TOLERANCE
    ==============================  ===================================  =========================

    A missing ``ledger_amount`` never invents a variance: the reconciler only
    compares two sides when the producer actually supplied both.
    """
    body = event_body if isinstance(event_body, dict) else {}

    amount = money_text(body.get("amount"), "amount")
    ledger_source = body.get("ledger_amount")
    ledger = None
    if ledger_source is not None and ledger_source != "":
        ledger = money_text(ledger_source, "ledger_amount")

    success = body.get("status") == "SUCCESS"

    variance = None
    if ledger is not None:
        # GL variance, matching the console and the fixtures: ledger - settlement.
        variance = Decimal(ledger) - Decimal(amount)

    if variance is None:
        status = STATUS_SETTLED if success else STATUS_FLAGGED
        reason = REASON_CONFIRMED if success else REASON_NON_SUCCESS
    elif variance == 0:
        status = STATUS_SETTLED if success else STATUS_MATCHED
        reason = REASON_EXACT_MATCH
    else:
        status = STATUS_FLAGGED
        reason = (
            REASON_PRECISION_DRIFT
            if abs(variance) <= TOLERANCE
            else REASON_EXCEEDS_TOLERANCE
        )

    decision = {
        "transaction_id": body.get("transaction_id"),
        "amount": amount,
        "status": status,
        "reason": reason,
    }
    if ledger is not None:
        decision["ledger_amount"] = ledger
        variance_text = variance.quantize(CENT, rounding=ROUND_HALF_UP)
        if variance_text == 0:
            variance_text = abs(variance_text)
        decision["variance"] = str(variance_text)
    return decision


def reconcile_transaction(event_body):
    """Reconcile one transaction event and persist it (single table, 7-day TTL)."""
    decision = evaluate_transaction(event_body)
    now = int(time.time())

    item = {
        'PK': "RECONCILE#LEDGER",
        'SK': "TX#{0}".format(decision["transaction_id"]),
        'transaction_id': decision["transaction_id"],
        'amount': decision["amount"],
        'status': decision["status"],
        'reason': decision["reason"],
        'reconciled_at': now,
        'ttl': now + TTL_SECONDS,
    }
    if "ledger_amount" in decision:
        item['ledger_amount'] = decision["ledger_amount"]
        item['variance'] = decision["variance"]

    table.put_item(Item=item)
    return item

