"""Property-based tests for the AegisSkeleton ingestion core (hypothesis).

Where the example-based tests pin single cases, these assert invariants that must
hold for every input. They map to the rules in ``.kiro/steering/architecture.md``:

* Ingestion decoding: ``ingest_handler`` never raises on hostile input and only
  answers 202 or 400 (500 is reserved for a missing queue URL, 503 for SQS
  refusing the message).
* Rule 4 (Decimal): ``_money_text`` and ``_decimalize`` keep exact cents under
  ROUND_HALF_UP and never let IEEE-754 drift reach DynamoDB.
* Rule 2 (single table + TTL) and idempotent delivery: SQS batches of 0..10
  records write deterministic keys, always carry a ``ttl``, and replaying a batch
  produces no new rows.

The oracles are independent of the code under test: money is checked with exact
rational arithmetic (``fractions.Fraction``), and keys and TTL budgets are taken
from the steering tables rather than imported from ``app.py``.

Requirements: ``pip install pytest hypothesis`` (``boto3`` is optional; a minimal
stand-in is installed when it is absent, and nothing here touches AWS).
Run from the repository root:

    python3 -m pytest tests/test_property_invariants.py -v

Set ``HYPOTHESIS_EXAMPLES`` to change the number of examples per property
(default 150). The module is skipped, not failed, when hypothesis is missing.
"""

import base64
import contextlib
import decimal
import importlib.util
import json
import os
import re
import sys
import time
import types
from decimal import Decimal
from fractions import Fraction
from unittest import mock

import pytest

pytest.importorskip("hypothesis", reason="property tests need `pip install hypothesis`")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_PATH = os.path.join(REPO_ROOT, "src", "functions", "app.py")

QUEUE_URL = "https://sqs.invalid/000000000000/aegis-ingestion-test"
REQUIRED_FIELDS = ("id", "type", "payload")

# Steering Rule 2: partition -> TTL budget in seconds.
TTL_BUDGET_BY_PARTITION = {
    "RECONCILE#LEDGER": 604800,
    "GUARD#SECURITY_AUDIT": 86400,
    "TENANT#SYSTEM": 86400,
}
# Fields that legitimately change between two deliveries of the same message.
VOLATILE_FIELDS = frozenset({"created_at", "ttl", "reconciled_at", "audited_at"})

MONEY_TEXT = re.compile(r"^-?\d+\.\d{2}$")
BREAKERS = {"AWS::EC2::Instance", "AWS::RDS::DBInstance"}

COMMON = dict(
    max_examples=int(os.environ.get("HYPOTHESIS_EXAMPLES", "150")),
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


# --------------------------------------------------------------------------- #
# Loading the code under test
# --------------------------------------------------------------------------- #

def _ensure_aws_sdk():
    """Use the real AWS SDK when installed; otherwise register minimal stand-ins."""
    try:
        import boto3  # noqa: F401
        import botocore.exceptions  # noqa: F401
        return
    except ImportError:
        pass

    class ClientError(Exception):
        def __init__(self, error_response=None, operation_name=""):
            super().__init__(operation_name)
            self.response = error_response or {}

    boto3_stub = types.ModuleType("boto3")
    boto3_stub.resource = lambda *args, **kwargs: mock.MagicMock()
    boto3_stub.client = lambda *args, **kwargs: mock.MagicMock()
    botocore_stub = types.ModuleType("botocore")
    exceptions_stub = types.ModuleType("botocore.exceptions")
    exceptions_stub.ClientError = ClientError
    botocore_stub.exceptions = exceptions_stub
    sys.modules.update(
        {
            "boto3": boto3_stub,
            "botocore": botocore_stub,
            "botocore.exceptions": exceptions_stub,
        }
    )


def _load_core():
    _ensure_aws_sdk()
    spec = importlib.util.spec_from_file_location("aegis_core_app", CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app = _load_core()


class PutRecorder:
    """Stands in for a boto3 DynamoDB Table. PutItem overwrites by (PK, SK)."""

    def __init__(self):
        self.rows = {}
        self.calls = 0

    def put_item(self, Item, **_options):
        self.calls += 1
        self.rows[(Item["PK"], Item["SK"])] = Item


@contextlib.contextmanager
def wired_core():
    """Route every DynamoDB write of the core and both domain apps to recorders."""
    core = PutRecorder()
    domain = PutRecorder()
    patches = [mock.patch.object(app, "table", core)]
    for module_path, _entry in app.DOMAIN_ROUTES.values():
        patches.append(
            mock.patch.object(app._import_domain_module(module_path), "table", domain)
        )
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield core, domain


# --------------------------------------------------------------------------- #
# Independent oracles
# --------------------------------------------------------------------------- #

def expected_cents(decimal_text):
    """Exact ROUND_HALF_UP (half away from zero) to whole cents, as an int.

    Uses rational arithmetic, so it shares no code path with ``Decimal.quantize``.
    """
    scaled = Fraction(decimal_text) * 100
    magnitude = int((abs(scaled) + Fraction(1, 2)) // 1)
    return -magnitude if scaled < 0 else magnitude


def cents_text(cents):
    """Format whole cents as a canonical money string, e.g. -5 -> '-0.05'."""
    sign = "-" if cents < 0 else ""
    magnitude = abs(cents)
    return "{0}{1}.{2:02d}".format(sign, magnitude // 100, magnitude % 100)


def contains_float(node):
    if isinstance(node, float):
        return True
    if isinstance(node, dict):
        return any(contains_float(key) or contains_float(item) for key, item in node.items())
    if isinstance(node, (list, tuple)):
        return any(contains_float(item) for item in node)
    return False


def expected_key(envelope):
    """Steering Rule 2 key convention for one envelope."""
    payload = envelope["payload"]
    kind = envelope["type"]
    if kind == "reconciliation":
        return ("RECONCILE#LEDGER", "TX#{0}".format(payload.get("transaction_id") or envelope["id"]))
    if kind == "cloud_audit":
        return ("GUARD#SECURITY_AUDIT", "RESOURCE#{0}".format(payload.get("resource_id") or envelope["id"]))
    return ("TENANT#SYSTEM", "EVENT#{0}#{1}".format(kind, envelope["id"]))


def last_envelope_per_key(batch):
    """PutItem is last-write-wins, so the final row for a key comes from its last envelope."""
    latest = {}
    for envelope in batch:
        latest[expected_key(envelope)] = envelope
    return latest


def stable(rows):
    return {
        key: {name: value for name, value in item.items() if name not in VOLATILE_FIELDS}
        for key, item in rows.items()
    }


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #

FINITE_FLOATS = st.floats(allow_nan=False, allow_infinity=False)
MODEST_FLOATS = st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False)

JSON_LEAVES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10 ** 12), max_value=10 ** 12),
    MODEST_FLOATS,
    st.text(max_size=8),
)
JSON_VALUES = st.recursive(
    JSON_LEAVES,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=4), children, max_size=4),
    ),
    max_leaves=12,
)
NON_BLANK_PAYLOADS = st.one_of(
    st.integers(),
    st.booleans(),
    st.text(min_size=1, max_size=8),
    st.lists(JSON_LEAVES, max_size=4),
    st.dictionaries(st.text(max_size=4), JSON_LEAVES, max_size=4),
)

# A tiny alphabet makes identical ids (and so identical keys) common inside a batch.
IDS = st.text(alphabet="abc", min_size=1, max_size=2)

MONEY_INPUTS = st.one_of(
    st.integers(min_value=-(10 ** 9), max_value=10 ** 9),
    st.integers(min_value=-(10 ** 11), max_value=10 ** 11).map(cents_text),
    MODEST_FLOATS,
)
NON_NEGATIVE_COST = st.one_of(
    st.integers(min_value=0, max_value=1000),
    st.integers(min_value=0, max_value=10 ** 6).map(cents_text),
    st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False),
)

RECONCILIATION_ENVELOPES = st.builds(
    lambda envelope_id, tx_id, amount, status: {
        "id": envelope_id,
        "type": "reconciliation",
        "payload": dict(
            ({"transaction_id": tx_id} if tx_id is not None else {}),
            amount=amount,
            status=status,
        ),
    },
    IDS,
    st.one_of(st.none(), IDS),
    MONEY_INPUTS,
    st.sampled_from(["SUCCESS", "PENDING", "FAILED"]),
)
CLOUD_AUDIT_ENVELOPES = st.builds(
    lambda envelope_id, resource_id, resource_type, hourly: {
        "id": envelope_id,
        "type": "cloud_audit",
        "payload": dict(
            ({"resource_id": resource_id} if resource_id is not None else {}),
            resource_type=resource_type,
            cost_per_hour=hourly,
        ),
    },
    IDS,
    st.one_of(st.none(), IDS),
    st.sampled_from(["AWS::EC2::Instance", "AWS::RDS::DBInstance", "AWS::EC2::Volume", "AWS::Lambda::Function"]),
    NON_NEGATIVE_COST,
)
GENERIC_ENVELOPES = st.builds(
    lambda envelope_id, kind, payload: {"id": envelope_id, "type": kind, "payload": payload},
    IDS,
    st.sampled_from(["GENERIC_EVENT", "metric", "ping"]),
    st.dictionaries(st.text(max_size=4), JSON_VALUES, max_size=3),
)
ENVELOPES = st.one_of(RECONCILIATION_ENVELOPES, CLOUD_AUDIT_ENVELOPES, GENERIC_ENVELOPES)
BATCHES = st.lists(ENVELOPES, min_size=0, max_size=10)


def sqs_event(batch):
    return {
        "Records": [
            {"messageId": "m-{0}".format(index), "body": json.dumps(envelope)}
            for index, envelope in enumerate(batch)
        ]
    }


# --------------------------------------------------------------------------- #
# 1. Ingestion payload decoding
# --------------------------------------------------------------------------- #

def invoke_ingest(event, queue_url=QUEUE_URL):
    with mock.patch.object(app, "INGESTION_QUEUE_URL", queue_url), mock.patch.object(app, "sqs") as sqs:
        response = app.ingest_handler(event, None)
    return response, sqs


def assert_graceful(response, sqs):
    """Shared contract: 202 enqueues exactly once, 400 enqueues nothing, never 5xx."""
    assert response["statusCode"] in (202, 400), response
    assert response["headers"]["Content-Type"] == "application/json"
    body = json.loads(response["body"])
    assert isinstance(body, dict) and "status" in body
    if response["statusCode"] == 202:
        assert sqs.send_message.call_count == 1
        queued = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
        assert all(queued.get(field) not in (None, "") for field in REQUIRED_FIELDS)
    else:
        sqs.send_message.assert_not_called()
    return body


@settings(**COMMON)
@given(raw=st.binary(max_size=512))
def test_arbitrary_bytes_as_base64_body_never_raise(raw):
    event = {"isBase64Encoded": True, "body": base64.b64encode(raw).decode("ascii")}
    assert_graceful(*invoke_ingest(event))


@settings(**COMMON)
@given(raw=st.binary(max_size=512))
def test_arbitrary_bytes_as_raw_body_never_raise(raw):
    assert_graceful(*invoke_ingest({"body": raw}))


@settings(**COMMON)
@given(text=st.text(max_size=300))
def test_arbitrary_text_body_never_raises(text):
    assert_graceful(*invoke_ingest({"body": text}))


@settings(**COMMON)
@given(text=st.text(max_size=120))
def test_text_flagged_as_base64_never_raises(text):
    # Mostly invalid base64 or non-ASCII: must be a 400, not a crash.
    assert_graceful(*invoke_ingest({"isBase64Encoded": True, "body": text}))


@settings(**COMMON)
@given(
    body=st.one_of(
        st.none(),
        st.integers(),
        FINITE_FLOATS,
        st.booleans(),
        st.lists(st.integers(), max_size=4),
        st.dictionaries(st.text(max_size=4), st.integers(), max_size=4),
    )
)
def test_non_string_body_types_never_raise(body):
    assert_graceful(*invoke_ingest({"body": body}))


@settings(**COMMON)
@given(
    envelope_id=st.text(min_size=1, max_size=12),
    kind=st.text(min_size=1, max_size=12),
    payload=NON_BLANK_PAYLOADS,
)
def test_well_formed_envelopes_are_queued_unchanged(envelope_id, kind, payload):
    envelope = {"id": envelope_id, "type": kind, "payload": payload}
    response, sqs = invoke_ingest({"body": json.dumps(envelope)})
    body = assert_graceful(response, sqs)
    assert response["statusCode"] == 202
    assert body == {"status": "QUEUED", "message_id": envelope_id}
    call = sqs.send_message.call_args.kwargs
    assert call["QueueUrl"] == QUEUE_URL
    assert json.loads(call["MessageBody"]) == envelope
    assert call["MessageAttributes"]["type"]["StringValue"] == kind


@settings(**COMMON)
@given(
    blank=st.sets(st.sampled_from(REQUIRED_FIELDS), min_size=1, max_size=3),
    how=st.sampled_from(["absent", "null", "empty-string"]),
)
def test_missing_required_fields_are_reported_and_nothing_is_queued(blank, how):
    envelope = {"id": "tx-1", "type": "reconciliation", "payload": {"amount": "1.00"}}
    for field in blank:
        if how == "absent":
            del envelope[field]
        else:
            envelope[field] = None if how == "null" else ""
    response, sqs = invoke_ingest({"body": json.dumps(envelope)})
    body = assert_graceful(response, sqs)
    assert response["statusCode"] == 400
    assert body["missing"] == [field for field in REQUIRED_FIELDS if field in blank]


@settings(**COMMON)
@given(code=st.text(min_size=1, max_size=20))
def test_sqs_refusal_is_a_503_never_an_exception(code):
    with mock.patch.object(app, "INGESTION_QUEUE_URL", QUEUE_URL), mock.patch.object(app, "sqs") as sqs:
        sqs.send_message.side_effect = app.ClientError({"Error": {"Code": code}}, "SendMessage")
        response = app.ingest_handler(
            {"body": json.dumps({"id": "a", "type": "t", "payload": {"x": 1}})}, None
        )
    assert response["statusCode"] == 503
    assert json.loads(response["body"])["status"] == "UNAVAILABLE"


def test_missing_queue_url_is_the_only_500():
    response, sqs = invoke_ingest({"body": "{}"}, queue_url="")
    assert response["statusCode"] == 500
    sqs.send_message.assert_not_called()


@pytest.mark.xfail(
    strict=True,
    raises=RecursionError,
    reason=(
        "KNOWN DEFECT: ingest_handler catches (TypeError, ValueError) around json.loads, "
        "but deeply nested JSON raises RecursionError, so the Lambda fails with an unhandled "
        "error instead of returning 400. Remove this marker once the handler catches it."
    ),
)
@pytest.mark.parametrize("opener,closer", [("[", "]"), ('{"a":', "}")], ids=["arrays", "objects"])
def test_deeply_nested_json_returns_400(opener, closer):
    depth = 100000
    body = opener * depth + ("1" if opener != "[" else "") + closer * depth
    response, sqs = invoke_ingest({"body": body})
    assert response["statusCode"] == 400
    sqs.send_message.assert_not_called()


# --------------------------------------------------------------------------- #
# 2. Decimal quantization (Rule 4)
# --------------------------------------------------------------------------- #

BOUNDED_FLOATS = st.floats(min_value=-1e12, max_value=1e12, allow_nan=False, allow_infinity=False)


@settings(**COMMON)
@given(value=BOUNDED_FLOATS)
def test_money_text_is_exact_half_up_of_the_floats_decimal_text(value):
    text = app._money_text(value)
    assert isinstance(text, str) and MONEY_TEXT.match(text), text
    assert Fraction(text) * 100 == expected_cents(repr(value))


@settings(**COMMON)
@given(cents=st.integers(min_value=-(10 ** 13), max_value=10 ** 13))
def test_exact_cent_amounts_survive_a_float_round_trip(cents):
    text = cents_text(cents)
    assert app._money_text(text) == text
    assert app._money_text(float(text)) == text


@settings(**COMMON)
@given(
    left=st.integers(min_value=-(10 ** 10), max_value=10 ** 10),
    right=st.integers(min_value=-(10 ** 10), max_value=10 ** 10),
)
def test_float_subtraction_drift_is_absorbed_by_quantization(left, right):
    # e.g. 400.19 - 400.17 == 0.01999999999998181 in binary floating point.
    drifted = left / 100 - right / 100
    assert app._money_text(drifted) == cents_text(left - right)


@settings(**COMMON)
@given(value=BOUNDED_FLOATS)
def test_money_text_is_idempotent(value):
    once = app._money_text(value)
    assert app._money_text(once) == once


@settings(**COMMON)
@given(first=BOUNDED_FLOATS, second=BOUNDED_FLOATS)
def test_money_text_is_monotonic(first, second):
    low, high = sorted((first, second))
    assert Fraction(app._money_text(low)) <= Fraction(app._money_text(high))


@settings(**COMMON)
@given(value=FINITE_FLOATS)
def test_any_finite_float_is_exact_or_rejected_never_silently_wrong(value):
    # Beyond ~1e25 the 28-digit Decimal context cannot hold the cents. That must
    # surface as an exception (the message then retries and lands in the DLQ),
    # not as a corrupted amount.
    try:
        text = app._money_text(value)
    except (ValueError, decimal.DecimalException):
        return
    assert MONEY_TEXT.match(text), text
    assert Fraction(text) * 100 == expected_cents(repr(value))


@settings(**COMMON)
@given(raw=st.text(max_size=40))
def test_arbitrary_text_is_a_money_string_or_a_controlled_error(raw):
    try:
        text = app._money_text(raw)
    except (ValueError, decimal.DecimalException):
        return
    assert MONEY_TEXT.match(text), text


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), True, False, "abc", "1.2.3", "NaN", "Infinity"],
)
def test_non_numeric_and_non_finite_money_is_rejected(value):
    with pytest.raises(ValueError):
        app._money_text(value)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.285, "0.29"),
        (2.675, "2.68"),
        (1.005, "1.01"),
        (0.005, "0.01"),
        (-0.005, "-0.01"),
        (0.1 + 0.2, "0.30"),
        (400.19 - 400.17, "0.02"),
    ],
)
def test_classic_binary_float_rounding_traps(value, expected):
    # round(2.675, 2) == 2.67 and Decimal(2.675) rounds down: both are drift.
    assert app._money_text(value) == expected


def assert_converted(original, converted):
    if isinstance(original, float):
        assert isinstance(converted, Decimal)
        assert converted.is_finite()
        assert float(converted) == original  # exact round trip, no drift
        assert Fraction(converted) == Fraction(repr(original))  # the decimal text, not the binary expansion
    elif isinstance(original, dict):
        assert isinstance(converted, dict)
        assert list(converted) == list(original)
        for key in original:
            assert_converted(original[key], converted[key])
    elif isinstance(original, (list, tuple)):
        assert isinstance(converted, list)
        assert len(converted) == len(original)
        for before, after in zip(original, converted):
            assert_converted(before, after)
    else:
        assert type(converted) is type(original)
        assert converted == original


DECIMALIZE_TREES = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), FINITE_FLOATS, st.text(max_size=6)),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.tuples(children, children),
        st.dictionaries(st.text(max_size=4), children, max_size=4),
    ),
    max_leaves=15,
)


@settings(**COMMON)
@given(tree=DECIMALIZE_TREES)
def test_decimalize_removes_every_float_and_keeps_the_shape(tree):
    converted = app._decimalize(tree)
    assert not contains_float(converted)
    assert_converted(tree, converted)


@settings(**COMMON)
@given(value=BOUNDED_FLOATS)
def test_decimalize_does_not_change_the_quantized_amount(value):
    assert app._money_text(app._decimalize(value)) == app._money_text(value)


# --------------------------------------------------------------------------- #
# 3. SQS batch boundaries and idempotent delivery
# --------------------------------------------------------------------------- #

def process(event):
    response = app.lambda_handler(event, None)
    return response["statusCode"], json.loads(response["body"])["processed"]


def assert_row_follows_the_spec(key, row, envelope, started, finished):
    partition, sort_key = key
    assert (row["PK"], row["SK"]) == key
    assert isinstance(row["ttl"], int) and not isinstance(row["ttl"], bool)
    budget = TTL_BUDGET_BY_PARTITION[partition]
    assert started + budget <= row["ttl"] <= finished + budget

    payload = envelope["payload"]
    if partition == "RECONCILE#LEDGER":
        assert MONEY_TEXT.match(row["amount"]), row["amount"]
        assert Fraction(row["amount"]) * 100 == expected_cents(str(payload["amount"]))
        assert row["status"] == ("SETTLED" if payload["status"] == "SUCCESS" else "FLAGGED_DISCREPANCY")
        assert sort_key == "TX#{0}".format(payload.get("transaction_id") or envelope["id"])
    elif partition == "GUARD#SECURITY_AUDIT":
        assert MONEY_TEXT.match(row["estimated_hourly_burn"]), row["estimated_hourly_burn"]
        assert Fraction(row["estimated_hourly_burn"]) * 100 == expected_cents(str(payload["cost_per_hour"]))
        tripped = payload["resource_type"] in BREAKERS
        assert row["circuit_breaker_triggered"] is tripped
        assert row["action_taken"] == ("TERMINATE_SIGNAL_SENT" if tripped else "ALLOW")


@settings(**COMMON)
@given(batch=BATCHES)
def test_batches_of_up_to_ten_records_are_processed_idempotently(batch):
    event = sqs_event(batch)
    started = int(time.time())
    with wired_core() as (core, domain):
        first = process(event)
        core_after_first, domain_after_first = stable(core.rows), stable(domain.rows)
        writes_after_first = core.calls
        second = process(event)
        core_after_second, domain_after_second = stable(core.rows), stable(domain.rows)
        writes_after_second = core.calls
    finished = int(time.time())

    assert first == second == (200, len(batch))
    # Replaying the same delivery changes nothing.
    assert core_after_first == core_after_second
    assert domain_after_first == domain_after_second
    # One core write per record per delivery, never a fan-out or a skip.
    assert writes_after_first == len(batch)
    assert writes_after_second == 2 * len(batch)

    # Keys come from the steering convention; duplicates collapse to one row.
    expected = last_envelope_per_key(batch)
    assert set(core.rows) == set(expected)
    for key, envelope in expected.items():
        assert_row_follows_the_spec(key, core.rows[key], envelope, started, finished)

    # DynamoDB rejects float attributes; none may ever be sent.
    assert not contains_float(list(core.rows.values()))
    assert not contains_float(list(domain.rows.values()))


@settings(**COMMON)
@given(batch=BATCHES, cut=st.integers(min_value=0, max_value=10))
def test_final_state_does_not_depend_on_where_the_batch_boundary_falls(batch, cut):
    cut = min(cut, len(batch))
    with wired_core() as (whole_core, whole_domain):
        process(sqs_event(batch))
    with wired_core() as (split_core, split_domain):
        process(sqs_event(batch[:cut]))
        process(sqs_event(batch[cut:]))
    assert stable(whole_core.rows) == stable(split_core.rows)
    assert stable(whole_domain.rows) == stable(split_domain.rows)


POISON_BODIES = ["", "not json", "{", "null", "[1, 2]", '"text"']


@settings(**COMMON)
@given(
    batch=st.lists(ENVELOPES, min_size=0, max_size=9),
    poison=st.sampled_from(POISON_BODIES),
    position=st.integers(min_value=0, max_value=9),
)
def test_a_malformed_record_fails_the_batch_loudly_and_a_retry_converges(batch, poison, position):
    position = min(position, len(batch))
    delivery = sqs_event(batch)["Records"]
    delivery.insert(position, {"messageId": "poison", "body": poison})

    with wired_core() as (core, _domain):
        with pytest.raises(ValueError):
            app.lambda_handler({"Records": delivery}, None)
        # Records ahead of the poison were written; nothing after it was.
        assert set(core.rows) == set(last_envelope_per_key(batch[:position]))
        # SQS redelivers the batch once the bad record is gone: state converges.
        process(sqs_event(batch))
        assert set(core.rows) == set(last_envelope_per_key(batch))


@pytest.mark.parametrize("size", [0, 1, 9, 10])
def test_batch_size_boundaries(size):
    batch = [{"id": "e{0}".format(index), "type": "metric", "payload": {"n": index}} for index in range(size)]
    with wired_core() as (core, _domain):
        assert process(sqs_event(batch)) == (200, size)
        assert len(core.rows) == size
        snapshot = stable(core.rows)
        assert process(sqs_event(batch)) == (200, size)
        assert stable(core.rows) == snapshot


def test_event_without_records_is_a_no_op():
    with wired_core() as (core, domain):
        assert process({}) == (200, 0)
        assert core.calls == 0 and domain.calls == 0
