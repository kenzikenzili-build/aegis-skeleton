"""Decimal-exact tests for the GrimReconciler financial engine.

These tests exercise ``apps/grim-reconciler/reconciler.py`` end to end:

* the pure decision function ``evaluate_transaction`` (no AWS, no clock), and
* ``reconcile_transaction`` persistence (single-table keys, 7-day TTL).

The oracles are independent of the implementation: money is re-quantized here
with ``fractions.Fraction`` rational arithmetic, and the 100-row reconciliation
case is built from the real ``fixtures/stripe_settlements.csv`` +
``fixtures/internal_gl.csv`` pair, so it cannot drift from the shipped data.

Run from the repository root::

    python3 -m pytest tests/test_grim_reconciler_decimal.py -v
"""

import contextlib
import csv
import importlib.util
import json
import os
import sys
import types
from decimal import Decimal
from fractions import Fraction
from unittest import mock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECONCILER_PATH = os.path.join(REPO_ROOT, "apps", "grim-reconciler", "reconciler.py")
STRIPE_FIXTURE = os.path.join(REPO_ROOT, "fixtures", "stripe_settlements.csv")
GL_FIXTURE = os.path.join(REPO_ROOT, "fixtures", "internal_gl.csv")

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.02")
TTL_SECONDS = 604800


# --------------------------------------------------------------------------- #
# Loading the engine under test                                               #
# --------------------------------------------------------------------------- #

def _ensure_aws_sdk():
    """Use the real AWS SDK when installed; otherwise register a stand-in."""
    try:
        import boto3  # noqa: F401
        return
    except ImportError:
        pass

    boto3_stub = types.ModuleType("boto3")
    boto3_stub.resource = lambda *args, **kwargs: mock.MagicMock()
    sys.modules.setdefault("boto3", boto3_stub)


def _load_engine():
    _ensure_aws_sdk()
    os.environ.setdefault("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
    spec = importlib.util.spec_from_file_location("aegis_grim_reconciler", RECONCILER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine = _load_engine()


class PutRecorder:
    """Stands in for a boto3 DynamoDB Table: PutItem overwrites by (PK, SK)."""

    def __init__(self):
        self.rows = {}
        self.calls = 0

    def put_item(self, Item, **_options):
        self.calls += 1
        self.rows[(Item["PK"], Item["SK"])] = Item


@contextlib.contextmanager
def wired_engine():
    recorder = PutRecorder()
    with mock.patch.object(engine, "table", recorder):
        yield recorder


def contains_float(value):
    """True when any leaf of a document is an IEEE-754 float."""
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(contains_float(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(contains_float(item) for item in value)
    return False


def cents_text(cents):
    """Format whole cents as a canonical money string (-5 -> '-0.05')."""
    sign = "-" if cents < 0 else ""
    magnitude = abs(cents)
    return "{0}{1}.{2:02d}".format(sign, magnitude // 100, magnitude % 100)


def half_up_cents(value):
    """Exact ROUND_HALF_UP to whole cents with rational arithmetic."""
    scaled = Fraction(str(value)) * 100
    magnitude = int((abs(scaled) + Fraction(1, 2)) // 1)
    return -magnitude if scaled < 0 else magnitude


# --------------------------------------------------------------------------- #
# 1. Re-usable fixture pair (real artifacts)                                  #
# --------------------------------------------------------------------------- #

def read_csv(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def settlement_pairs():
    """Join Stripe settlements to GL entries on the balance transaction id."""
    settlements = read_csv(STRIPE_FIXTURE)
    gl_by_ref = {entry["source_ref"]: entry for entry in read_csv(GL_FIXTURE)}
    return [(settlement, gl_by_ref.get(settlement["balance_transaction"])) for settlement in settlements]


# --------------------------------------------------------------------------- #
# 2. State machine: SETTLED / MATCHED / FLAGGED_DISCREPANCY                   #
# --------------------------------------------------------------------------- #

def test_success_without_a_ledger_side_is_settled():
    decision = engine.evaluate_transaction(
        {"transaction_id": "tx-1", "amount": "17.66", "status": "SUCCESS"}
    )
    assert decision["status"] == "SETTLED"
    assert decision["reason"] == "SETTLEMENT_CONFIRMED"
    assert decision["amount"] == "17.66"
    assert "variance" not in decision


@pytest.mark.parametrize("status", ["PENDING", "FAILED", "UNKNOWN", "", None])
def test_non_success_status_is_flagged_without_a_ledger_side(status):
    decision = engine.evaluate_transaction(
        {"transaction_id": "tx-2", "amount": "17.66", "status": status}
    )
    assert decision["status"] == "FLAGGED_DISCREPANCY"
    assert decision["reason"] == "NON_SUCCESS_STATUS"


def test_exact_cent_match_on_a_confirmed_settlement_is_settled():
    decision = engine.evaluate_transaction(
        {
            "transaction_id": "tx-3",
            "amount": "17.66",
            "ledger_amount": "17.66",
            "status": "SUCCESS",
        }
    )
    assert decision["status"] == "SETTLED"
    assert decision["reason"] == "EXACT_CENT_MATCH"
    assert decision["variance"] == "0.00"


def test_exact_cent_match_without_confirmation_is_matched():
    decision = engine.evaluate_transaction(
        {
            "transaction_id": "tx-4",
            "amount": "416.10",
            "ledger_amount": "416.10",
            "status": "PENDING",
        }
    )
    assert decision["status"] == "MATCHED"
    assert decision["reason"] == "EXACT_CENT_MATCH"
    assert decision["variance"] == "0.00"


@pytest.mark.parametrize(
    "amount,ledger,variance,reason",
    [
        ("17.66", "17.67", "0.01", "PRECISION_DRIFT"),
        ("17.67", "17.66", "-0.01", "PRECISION_DRIFT"),
        ("416.08", "416.10", "0.02", "PRECISION_DRIFT"),
        ("416.10", "416.08", "-0.02", "PRECISION_DRIFT"),
        ("416.07", "416.10", "0.03", "VARIANCE_EXCEEDS_TOLERANCE"),
        ("416.10", "416.07", "-0.03", "VARIANCE_EXCEEDS_TOLERANCE"),
        ("117.66", "17.66", "-100.00", "VARIANCE_EXCEEDS_TOLERANCE"),
    ],
)
def test_any_cent_difference_is_flagged_at_the_tolerance_boundary(
    amount, ledger, variance, reason
):
    decision = engine.evaluate_transaction(
        {
            "transaction_id": "tx-5",
            "amount": amount,
            "ledger_amount": ledger,
            "status": "SUCCESS",
        }
    )
    assert decision["status"] == "FLAGGED_DISCREPANCY"
    assert decision["reason"] == reason
    assert decision["variance"] == variance
    if reason == "PRECISION_DRIFT":
        assert abs(Decimal(variance)) <= TOLERANCE
    else:
        assert abs(Decimal(variance)) > TOLERANCE


# --------------------------------------------------------------------------- #
# 3. Decimal rounding safety (Rule 4)                                         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("17.665", "17.67"),
        ("17.664", "17.66"),
        ("0.005", "0.01"),
        ("0.004", "0.00"),
        ("-0.005", "-0.01"),
        ("-0.004", "0.00"),
        ("-17.665", "-17.67"),
        ("0", "0.00"),
        ("-0", "0.00"),
        ("1e2", "100.00"),
        (17, "17.00"),
        (0.5, "0.50"),
    ],
)
def test_money_is_quantized_half_up_to_exact_cents(raw, expected):
    assert engine.money_text(raw) == expected
    assert engine.evaluate_transaction({"amount": raw})["amount"] == expected


@pytest.mark.parametrize("cents", list(range(-250, 251, 7)))
def test_string_amounts_never_leave_the_cent_grid(cents):
    text = cents_text(cents)
    decision = engine.evaluate_transaction({"amount": text, "status": "SUCCESS"})
    assert decision["amount"] == text
    assert Fraction(decision["amount"]) * 100 == half_up_cents(text)


def test_classic_binary_float_traps_are_absorbed_by_decimal_text():
    # 0.1 + 0.2 in IEEE-754 is 0.30000000000000004; its decimal text is "0.3".
    binary_sum = 0.1 + 0.2
    assert binary_sum != 0.3
    assert engine.evaluate_transaction({"amount": binary_sum})["amount"] == "0.30"
    decision = engine.evaluate_transaction(
        {"amount": binary_sum, "ledger_amount": "0.30", "status": "SUCCESS"}
    )
    assert decision["status"] == "SETTLED"
    assert decision["variance"] == "0.00"
    assert not contains_float(decision)


@pytest.mark.parametrize("raw", ["abc", "NaN", "Infinity", "-Infinity", True, False])
def test_unparseable_or_non_finite_amounts_raise(raw):
    with pytest.raises(ValueError):
        engine.money_text(raw)


def test_a_missing_amount_is_zero_never_an_invented_figure():
    assert engine.money_text(None) == "0.00"
    assert engine.money_text("") == "0.00"
    assert engine.evaluate_transaction({})["amount"] == "0.00"


# --------------------------------------------------------------------------- #
# 4. The shipped fixture ledgers                                              #
# --------------------------------------------------------------------------- #

def test_fixture_pair_joins_without_orphans(settlement_pairs):
    assert len(settlement_pairs) == 100
    assert all(ledger is not None for _settlement, ledger in settlement_pairs)


def test_fixture_ledgers_reconcile_one_hundred_rows_exactly():
    settlements = read_csv(STRIPE_FIXTURE)
    gl_by_ref = {entry["source_ref"]: entry for entry in read_csv(GL_FIXTURE)}
    assert len(settlements) == 100
    assert len(gl_by_ref) == 100

    statuses = {"SETTLED": 0, "MATCHED": 0, "FLAGGED_DISCREPANCY": 0}
    drifting = []
    gross_total = Decimal("0.00")

    for settlement in settlements:
        ledger = gl_by_ref[settlement["balance_transaction"]]
        decision = engine.evaluate_transaction(
            {
                "transaction_id": settlement["id"],
                "amount": settlement["net_decimal"],
                "ledger_amount": ledger["net_amount"],
                "status": "SUCCESS",
            }
        )
        statuses[decision["status"]] += 1
        gross_total += Decimal(settlement["gross_decimal"])
        if decision["status"] != "SETTLED":
            drifting.append((settlement["id"], decision["variance"], decision["reason"]))

    assert statuses == {"SETTLED": 99, "MATCHED": 0, "FLAGGED_DISCREPANCY": 1}
    assert drifting == [("ch_00000000000000a1b2c8e81b", "-0.02", "PRECISION_DRIFT")]
    assert gross_total == Decimal("26771.50")


# --------------------------------------------------------------------------- #
# 5. Persistence contract (single table, 7-day TTL, Decimal strings)          #
# --------------------------------------------------------------------------- #

def test_reconcile_transaction_persists_decimal_strings_and_a_seven_day_ttl():
    payload = {
        "transaction_id": "ch_test_0001",
        "amount": "17.665",
        "ledger_amount": "17.66",
        "status": "SUCCESS",
    }
    with wired_engine() as recorder:
        item = engine.reconcile_transaction(payload)

    assert recorder.calls == 1
    assert item["PK"] == "RECONCILE#LEDGER"
    assert item["SK"] == "TX#ch_test_0001"
    assert item["amount"] == "17.67"
    assert item["ledger_amount"] == "17.66"
    assert item["variance"] == "-0.01"
    assert item["status"] == "FLAGGED_DISCREPANCY"
    assert item["reason"] == "PRECISION_DRIFT"
    assert item["ttl"] - item["reconciled_at"] == TTL_SECONDS
    assert not contains_float(item)
    assert recorder.rows[("RECONCILE#LEDGER", "TX#ch_test_0001")] is item


def test_replaying_a_message_reproduces_the_same_decision_and_row():
    payload = {
        "transaction_id": "ch_test_0002",
        "amount": "28.27",
        "ledger_amount": "28.27",
        "status": "SUCCESS",
    }
    first = engine.evaluate_transaction(payload)
    second = engine.evaluate_transaction(payload)
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    with wired_engine() as recorder:
        row_one = engine.reconcile_transaction(payload)
        row_two = engine.reconcile_transaction(payload)

    assert recorder.calls == 2
    assert len(recorder.rows) == 1
    volatile = {"ttl", "reconciled_at"}
    assert {key: value for key, value in row_one.items() if key not in volatile} == {
        key: value for key, value in row_two.items() if key not in volatile
    }


def test_engine_exposes_decimal_policy_constants_only():
    for name in ("CENT", "TOLERANCE"):
        assert isinstance(getattr(engine, name), Decimal), name
    assert engine.CENT == CENT
    assert engine.TOLERANCE == TOLERANCE
    assert engine.TTL_SECONDS == TTL_SECONDS
