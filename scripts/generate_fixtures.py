#!/usr/bin/env python3
"""AegisSkeleton - deterministic fixture generator (Stage 1).

Produces the reconciliation and FinOps regression inputs consumed by
``apps/grim-reconciler`` and ``apps/phantom-guard``:

  fixtures/stripe_settlements.csv  100 rows, Stripe export layout, UTC stamps,
                                   integer-cent native fields plus explicit
                                   ``*_decimal`` strings built with Decimal.
  fixtures/internal_gl.csv         100 matching rows bookkept in EST (fixed
                                   UTC-05:00, no DST), keyed by
                                   ``source_ref == balance_transaction``, with a
                                   deterministic -0.02 USD precision drift at
                                   row index 42.
  fixtures/tfplan.json             Terraform execution plan JSON 1.2 containing
                                   an unattached 100 GB gp3 EBS volume and an
                                   idle Lambda provisioned-concurrency config.

Standard library only (csv, json, os, datetime, decimal). No network access, no
third-party packages, no wall-clock or PRNG input: two runs are byte-identical.

Usage:
    python3 scripts/generate_fixtures.py
"""

import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"
)

ROW_COUNT = 100
DRIFT_INDEX = 42
DRIFT_AMOUNT = Decimal("-0.02")
CENT = Decimal("0.01")
FEE_RATE = Decimal("0.029")
FEE_FIXED = Decimal("0.30")

# Fixed EST offset (UTC-05:00). tzdata/zoneinfo is deliberately avoided so the
# fixture bytes never depend on the host timezone database.
EST = timezone(timedelta(hours=-5), "EST")

FIRST_SETTLEMENT_UTC = datetime(2026, 10, 1, 4, 30, 0, tzinfo=timezone.utc)
SETTLEMENT_INTERVAL = timedelta(minutes=37)

STRIPE_COLUMNS = [
    "id",
    "object",
    "amount",
    "fee",
    "net",
    "currency",
    "created",
    "created_utc",
    "status",
    "description",
    "customer_email",
    "balance_transaction",
    "gross_decimal",
    "fee_decimal",
    "net_decimal",
]

GL_COLUMNS = [
    "gl_entry_id",
    "source_ref",
    "transaction_date_est",
    "posted_at_est",
    "debit",
    "credit",
    "net_amount",
    "currency",
    "cost_center",
    "memo",
]


# --------------------------------------------------------------------------- #
# Money + identifier helpers (Decimal only - never float)
# --------------------------------------------------------------------------- #

def money(value):
    """Quantize a value to cents using Decimal arithmetic (Rule 4)."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def gross_amount(index):
    """Deterministic gross in the range 18.50 .. 604.49 USD."""
    cents = 1850 + (index * 977) % 58600
    return money(Decimal(cents) / Decimal(100))


def fee_for(gross):
    """Stripe-style processing fee: 2.9 % + 0.30 USD, half-up to the cent."""
    return money(gross * FEE_RATE + FEE_FIXED)


def net_for(gross, fee):
    return money(gross - fee)


def to_cents(amount):
    """Major-unit Decimal -> Stripe-native integer cents."""
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


def settlement_timestamp(index):
    return FIRST_SETTLEMENT_UTC + SETTLEMENT_INTERVAL * index


def charge_id(index):
    return "ch_{0:024x}".format(0xA1B2C3D4E5 + index * 7919)


def balance_transaction_id(index):
    return "txn_{0:024x}".format(0x5F0A1B2C + index * 104729)


def customer_email(index):
    return "tenant{0:02d}@aegis-fixtures.example".format((index % 7) + 1)


# --------------------------------------------------------------------------- #
# Settlement / GL row construction
# --------------------------------------------------------------------------- #

def build_settlement_row(index):
    settled_at = settlement_timestamp(index)
    gross = gross_amount(index)
    fee = fee_for(gross)
    net = net_for(gross, fee)
    return {
        "id": charge_id(index),
        "object": "charge",
        "amount": to_cents(gross),
        "fee": to_cents(fee),
        "net": to_cents(net),
        "currency": "usd",
        "created": int(settled_at.timestamp()),
        "created_utc": settled_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "succeeded",
        "description": "AEGIS-RECON order {0}".format(1000 + index),
        "customer_email": customer_email(index),
        "balance_transaction": balance_transaction_id(index),
        "gross_decimal": str(gross),
        "fee_decimal": str(fee),
        "net_decimal": str(net),
    }


def build_gl_row(index, settlement):
    settled_est = settlement_timestamp(index).astimezone(EST)
    gross = Decimal(settlement["gross_decimal"])
    fee = Decimal(settlement["fee_decimal"])
    net = Decimal(settlement["net_decimal"])
    memo = "MATCHED_STRIPE_SETTLEMENT"
    if index == DRIFT_INDEX:
        net = money(net + DRIFT_AMOUNT)
        memo = "PRECISION_DRIFT_FLOAT64_TRUNCATION"
    return {
        "gl_entry_id": "GL-2026-{0:05d}".format(1001 + index),
        "source_ref": settlement["balance_transaction"],
        "transaction_date_est": settled_est.strftime("%Y-%m-%d"),
        "posted_at_est": settled_est.strftime("%Y-%m-%dT%H:%M:%S-05:00"),
        "debit": str(gross),
        "credit": str(fee),
        "net_amount": str(net),
        "currency": "USD",
        "cost_center": "CC-FINTECH-{0:02d}".format((index % 4) + 1),
        "memo": memo,
    }


def write_csv(path, columns, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def generate_settlements(path):
    """Write the Stripe settlement export and return the in-memory rows."""
    settlements = [build_settlement_row(index) for index in range(ROW_COUNT)]
    write_csv(path, STRIPE_COLUMNS, settlements)
    return settlements


def generate_gl(path, settlements):
    """Write the internal GL export derived from the settlement rows."""
    entries = [build_gl_row(index, row) for index, row in enumerate(settlements)]
    write_csv(path, GL_COLUMNS, entries)
    return entries


# --------------------------------------------------------------------------- #
# Terraform plan JSON (format_version 1.2)
# --------------------------------------------------------------------------- #

AWS_PROVIDER = "registry.terraform.io/hashicorp/aws"
AWS_ACCOUNT = "111122223333"
AWS_REGION = "us-east-1"


def _spec(address, rtype, name, values, after_unknown, expressions):
    return {
        "address": address,
        "type": rtype,
        "name": name,
        "values": values,
        "after_unknown": after_unknown,
        "expressions": expressions,
    }


def _planned_resource(spec):
    return {
        "address": spec["address"],
        "mode": "managed",
        "type": spec["type"],
        "name": spec["name"],
        "provider_name": AWS_PROVIDER,
        "schema_version": 0,
        "values": spec["values"],
        "sensitive_values": {},
    }


def _resource_change(spec):
    return {
        "address": spec["address"],
        "mode": "managed",
        "type": spec["type"],
        "name": spec["name"],
        "provider_name": AWS_PROVIDER,
        "change": {
            "actions": ["create"],
            "before": None,
            "after": spec["values"],
            "after_unknown": spec["after_unknown"],
            "before_sensitive": False,
            "after_sensitive": {},
        },
    }


def _configuration_resource(spec):
    return {
        "address": spec["address"],
        "mode": "managed",
        "type": spec["type"],
        "name": spec["name"],
        "provider_config_key": "aws",
        "expressions": spec["expressions"],
        "schema_version": 0,
    }


def _spec_orphan_gp3_volume():
    values = {
        "availability_zone": "us-east-1a",
        "encrypted": True,
        "final_snapshot": False,
        "id": None,
        "iops": 3000,
        "multi_attach_enabled": False,
        "size": 100,
        "snapshot_id": None,
        "tags": {
            "Aegis": "phantom-audit",
            "Purpose": "unattached-ebs-drift",
            "Billing": "idle-waste",
        },
        "throughput": 125,
        "type": "gp3",
        "attachments": [],
        "timeouts": None,
    }
    after_unknown = {
        "arn": True,
        "id": True,
        "kms_key_id": True,
        "tags_all": True,
    }
    expressions = {
        "availability_zone": {"constant_value": "us-east-1a"},
        "size": {"constant_value": 100},
        "type": {"constant_value": "gp3"},
        "iops": {"constant_value": 3000},
        "throughput": {"constant_value": 125},
        "encrypted": {"constant_value": True},
        "tags": {
            "constant_value": {
                "Aegis": "phantom-audit",
                "Purpose": "unattached-ebs-drift",
                "Billing": "idle-waste",
            }
        },
    }
    return _spec(
        "aws_ebs_volume.orphaned_gp3",
        "aws_ebs_volume",
        "orphaned_gp3",
        values,
        after_unknown,
        expressions,
    )


def _spec_lambda_function():
    values = {
        "architectures": ["arm64"],
        "environment": [{"variables": {"TABLE_NAME": "AegisStateTable"}}],
        "function_name": "AegisCoreFunction",
        "handler": "app.lambda_handler",
        "id": None,
        "memory_size": 256,
        "package_type": "Zip",
        "reserved_concurrent_executions": -1,
        "role": "arn:aws:iam::{0}:role/AegisCoreFunctionRole".format(AWS_ACCOUNT),
        "runtime": "python3.12",
        "source_code_hash": None,
        "timeout": 10,
        "tracing_config": [{"mode": "PassThrough"}],
    }
    after_unknown = {
        "arn": True,
        "id": True,
        "invoke_arn": True,
        "last_modified": True,
        "qualified_arn": True,
        "source_code_hash": True,
        "version": True,
    }
    expressions = {
        "function_name": {"constant_value": "AegisCoreFunction"},
        "handler": {"constant_value": "app.lambda_handler"},
        "runtime": {"constant_value": "python3.12"},
        "architectures": {"constant_value": ["arm64"]},
        "memory_size": {"constant_value": 256},
        "timeout": {"constant_value": 10},
    }
    return _spec(
        "aws_lambda_function.aegis_core",
        "aws_lambda_function",
        "aegis_core",
        values,
        after_unknown,
        expressions,
    )


def _spec_provisioned_concurrency():
    values = {
        "function_name": "AegisCoreFunction",
        "function_version": "$LATEST",
        "id": None,
        "provisioned_concurrent_executions": 20,
        "qualifier": "$LATEST",
        "skip_destroy": False,
        "timeouts": None,
    }
    after_unknown = {"id": True}
    expressions = {
        "function_name": {
            "references": [
                "aws_lambda_function.aegis_core.function_name",
                "aws_lambda_function.aegis_core.arn",
            ]
        },
        "provisioned_concurrent_executions": {"constant_value": 20},
        "qualifier": {"constant_value": "$LATEST"},
    }
    return _spec(
        "aws_lambda_provisioned_concurrency_config.idle_warm_pool",
        "aws_lambda_provisioned_concurrency_config",
        "idle_warm_pool",
        values,
        after_unknown,
        expressions,
    )


def _spec_state_table():
    values = {
        "attribute": [
            {"name": "PK", "type": "S"},
            {"name": "SK", "type": "S"},
        ],
        "billing_mode": "PAY_PER_REQUEST",
        "deletion_protection_enabled": False,
        "hash_key": "PK",
        "id": None,
        "name": "AegisStateTable",
        "point_in_time_recovery": [{"enabled": False}],
        "range_key": "SK",
        "read_capacity": 0,
        "stream_enabled": False,
        "tags": {"Aegis": "single-table-state", "TTL": "enabled"},
        "ttl": [{"attribute_name": "ttl", "enabled": True}],
        "write_capacity": 0,
    }
    after_unknown = {"arn": True, "id": True, "tags_all": True}
    expressions = {
        "name": {"constant_value": "AegisStateTable"},
        "billing_mode": {"constant_value": "PAY_PER_REQUEST"},
        "hash_key": {"constant_value": "PK"},
        "range_key": {"constant_value": "SK"},
        "attribute": {
            "constant_value": [
                {"name": "PK", "type": "S"},
                {"name": "SK", "type": "S"},
            ]
        },
        "ttl": {"constant_value": [{"attribute_name": "ttl", "enabled": True}]},
    }
    return _spec(
        "aws_dynamodb_table.aegis_state",
        "aws_dynamodb_table",
        "aegis_state",
        values,
        after_unknown,
        expressions,
    )


def build_tfplan():
    specs = [
        _spec_orphan_gp3_volume(),
        _spec_lambda_function(),
        _spec_provisioned_concurrency(),
        _spec_state_table(),
    ]
    return {
        "format_version": "1.2",
        "terraform_version": "1.9.5",
        "variables": {
            "aws_region": {"value": AWS_REGION},
            "environment": {"value": "hackathon"},
        },
        "planned_values": {
            "root_module": {
                "resources": [_planned_resource(spec) for spec in specs],
                "child_modules": [],
            }
        },
        "resource_changes": [_resource_change(spec) for spec in specs],
        "resource_drift": [],
        "output_changes": {
            "queue_url": {
                "actions": ["create"],
                "before": None,
                "after": "https://sqs.{0}.amazonaws.com/{1}/AegisIngestionQueue".format(
                    AWS_REGION, AWS_ACCOUNT
                ),
                "after_unknown": False,
                "before_sensitive": False,
                "after_sensitive": False,
            },
            "state_table_arn": {
                "actions": ["create"],
                "before": None,
                "after": "arn:aws:dynamodb:{0}:{1}:table/AegisStateTable".format(
                    AWS_REGION, AWS_ACCOUNT
                ),
                "after_unknown": False,
                "before_sensitive": False,
                "after_sensitive": False,
            },
        },
        "configuration": {
            "provider_config": {
                "aws": {
                    "name": "aws",
                    "full_name": AWS_PROVIDER,
                    "version_constraint": "~> 5.0",
                }
            },
            "root_module": {
                "resources": [_configuration_resource(spec) for spec in specs]
            },
        },
        "timestamp": "2026-10-01T00:00:00Z",
        "applyable": True,
        "complete": True,
        "errored": False,
    }


def generate_tfplan(path):
    plan = build_tfplan()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(plan, handle, indent=2, sort_keys=False)
        handle.write("\n")
    return plan


# --------------------------------------------------------------------------- #
# Self-verification (re-reads every artifact and asserts the contract)
# --------------------------------------------------------------------------- #

def read_csv_rows(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _find_resource(resources, resource_type):
    for resource in resources:
        if resource.get("type") == resource_type:
            return resource
    return None


def verify_fixtures(stripe_path, gl_path, plan_path):
    """Assert row counts, Decimal-exact variances and the tfplan waste signals."""
    settlements = read_csv_rows(stripe_path)
    entries = read_csv_rows(gl_path)
    if len(settlements) != ROW_COUNT:
        raise SystemExit(
            "❌ stripe_settlements.csv has {0} rows, expected {1}".format(
                len(settlements), ROW_COUNT
            )
        )
    if len(entries) != ROW_COUNT:
        raise SystemExit(
            "❌ internal_gl.csv has {0} rows, expected {1}".format(len(entries), ROW_COUNT)
        )

    stripe_by_txn = {}
    for row in settlements:
        stripe_by_txn[row["balance_transaction"]] = row

    drift_rows = []
    for index, entry in enumerate(entries):
        stripe_row = stripe_by_txn.get(entry["source_ref"])
        if stripe_row is None:
            raise SystemExit(
                "❌ internal_gl.csv row {0}: source_ref {1} has no Stripe match".format(
                    index, entry["source_ref"]
                )
            )
        variance = Decimal(entry["net_amount"]) - Decimal(stripe_row["net_decimal"])
        expected = DRIFT_AMOUNT if index == DRIFT_INDEX else Decimal("0.00")
        if variance != expected:
            raise SystemExit(
                "❌ internal_gl.csv row {0}: variance {1} != expected {2}".format(
                    index, variance, expected
                )
            )
        if variance != Decimal("0.00"):
            drift_rows.append(index)

    if drift_rows != [DRIFT_INDEX]:
        raise SystemExit(
            "❌ drift canary mismatch: expected only row {0}, found {1}".format(
                DRIFT_INDEX, drift_rows
            )
        )

    with open(plan_path, "r", encoding="utf-8") as handle:
        plan = json.load(handle)
    if plan.get("format_version") != "1.2":
        raise SystemExit("❌ tfplan.json format_version is not 1.2")

    resources = plan["planned_values"]["root_module"]["resources"]
    volume = _find_resource(resources, "aws_ebs_volume")
    if volume is None:
        raise SystemExit("❌ tfplan.json is missing the orphaned EBS volume")
    volume_values = volume["values"]
    if volume_values.get("size") != 100 or volume_values.get("type") != "gp3":
        raise SystemExit("❌ tfplan.json EBS volume is not a 100 GB gp3 volume")
    if volume_values.get("attachments"):
        raise SystemExit("❌ tfplan.json EBS volume is attached; fixture must stay unattached")
    if _find_resource(resources, "aws_volume_attachment") is not None:
        raise SystemExit("❌ tfplan.json contains an aws_volume_attachment resource")

    warm_pool = _find_resource(resources, "aws_lambda_provisioned_concurrency_config")
    if warm_pool is None:
        raise SystemExit("❌ tfplan.json is missing the provisioned concurrency config")
    warm_values = warm_pool["values"]
    if warm_values.get("provisioned_concurrent_executions") != 20:
        raise SystemExit("❌ tfplan.json provisioned concurrency is not 20")
    if warm_values.get("qualifier") != "$LATEST":
        raise SystemExit("❌ tfplan.json provisioned concurrency qualifier is not $LATEST")

    state_table = _find_resource(resources, "aws_dynamodb_table")
    if state_table is None:
        raise SystemExit("❌ tfplan.json is missing the DynamoDB state table")
    table_values = state_table["values"]
    if table_values.get("billing_mode") != "PAY_PER_REQUEST":
        raise SystemExit("❌ tfplan.json table billing mode is not PAY_PER_REQUEST")
    if table_values.get("hash_key") != "PK" or table_values.get("range_key") != "SK":
        raise SystemExit("❌ tfplan.json table keys are not PK/SK")
    ttl_blocks = table_values.get("ttl") or []
    if (
        not ttl_blocks
        or ttl_blocks[0].get("attribute_name") != "ttl"
        or not ttl_blocks[0].get("enabled")
    ):
        raise SystemExit("❌ tfplan.json table TTL is not enabled on attribute 'ttl'")

    drift_entry = entries[DRIFT_INDEX]
    drift_variance = Decimal(drift_entry["net_amount"]) - Decimal(
        stripe_by_txn[drift_entry["source_ref"]]["net_decimal"]
    )
    return {
        "settlement_rows": len(settlements),
        "gl_rows": len(entries),
        "drift_index": DRIFT_INDEX,
        "drift_variance": str(drift_variance),
        "drift_memo": drift_entry["memo"],
        "orphan_volume_gb": volume_values["size"],
        "orphan_volume_type": volume_values["type"],
        "provisioned_concurrency": warm_values["provisioned_concurrent_executions"],
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def _print_summary(stripe_path, gl_path, plan_path, checks):
    repo_root = os.path.dirname(FIXTURES_DIR)
    print("🛠️  AegisSkeleton fixtures regenerated (deterministic, stdlib-only)")
    print("")
    print(
        "   {0:<32} {1:>9}  {2:>8}  {3}".format(
            "artifact", "rows", "bytes", "contract"
        )
    )
    print("   " + "-" * 96)
    print(
        "   {0:<32} {1:>9}  {2:>8}  {3}".format(
            os.path.relpath(stripe_path, repo_root),
            checks["settlement_rows"],
            os.path.getsize(stripe_path),
            "UTC stamps, integer cents + Decimal strings",
        )
    )
    print(
        "   {0:<32} {1:>9}  {2:>8}  {3}".format(
            os.path.relpath(gl_path, repo_root),
            checks["gl_rows"],
            os.path.getsize(gl_path),
            "EST stamps (fixed -05:00), Decimal strings",
        )
    )
    print(
        "   {0:<32} {1:>9}  {2:>8}  {3}".format(
            os.path.relpath(plan_path, repo_root),
            "plan 1.2",
            os.path.getsize(plan_path),
            "orphan {0} GB {1} + provisioned concurrency {2}".format(
                checks["orphan_volume_gb"],
                checks["orphan_volume_type"],
                checks["provisioned_concurrency"],
            ),
        )
    )
    print("")
    print(
        "   drift canary: internal_gl row {0} net variance = {1} USD ({2})".format(
            checks["drift_index"], checks["drift_variance"], checks["drift_memo"]
        )
    )
    print("   all other 99 rows: variance = 0.00 USD (Decimal-exact)")
    print("")
    print("✅ 驗證通過: fixtures 契約完整，可作為 reconciler 與 FinOps 的回歸輸入。")


def main():
    os.makedirs(FIXTURES_DIR, exist_ok=True)
    stripe_path = os.path.join(FIXTURES_DIR, "stripe_settlements.csv")
    gl_path = os.path.join(FIXTURES_DIR, "internal_gl.csv")
    plan_path = os.path.join(FIXTURES_DIR, "tfplan.json")

    settlements = generate_settlements(stripe_path)
    generate_gl(gl_path, settlements)
    generate_tfplan(plan_path)

    checks = verify_fixtures(stripe_path, gl_path, plan_path)
    _print_summary(stripe_path, gl_path, plan_path, checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
