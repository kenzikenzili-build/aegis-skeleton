"""Decimal-exact tests for the Cloud FinOps engine (PhantomGuard + finops_sentry).

Two surfaces are covered end to end:

* ``mcp/finops_sentry.py`` — the AI-agent audit tool: it parses a real Terraform
  plan (``fixtures/tfplan.json``), detects orphaned EBS volumes, NAT gateways,
  idle provisioned concurrency and IaC drift, and prices the burn with
  ``decimal.Decimal``.
* ``apps/phantom-guard/sentry.py`` — the event-level sentry: DR-1 orphaned EBS,
  DR-2 always-on compute, DR-3 idle NAT gateway, plus the single-table row it
  persists (24 h TTL).

Every monetary oracle below is recomputed here from list prices with Decimal
(never from the module's own constants), so a silent rate change fails the test.
MCP protocol frames are compared against ``mcp/aegis_mcp_server.py`` to prove the
two servers speak the same envelope.

Run from the repository root::

    python3 -m pytest tests/test_phantom_guard_finops.py -v
"""

import importlib.util
import json
import os
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_UP
from unittest import mock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTRY_PATH = os.path.join(REPO_ROOT, "apps", "phantom-guard", "sentry.py")
FINOPS_PATH = os.path.join(REPO_ROOT, "mcp", "finops_sentry.py")
MCP_SERVER_PATH = os.path.join(REPO_ROOT, "mcp", "aegis_mcp_server.py")
PLAN_FIXTURE = os.path.join(REPO_ROOT, "fixtures", "tfplan.json")

PLACES = Decimal("0.000001")
GP3_RATE = Decimal("0.08")                 # us-east-1 gp3, per GB-month
NAT_RATE = Decimal("0.045")                # us-east-1 NAT gateway, per hour
PC_RATE = Decimal("0.0000041667")          # arm64 provisioned concurrency, GB-s
HOURS_PER_MONTH = Decimal("730")


def _load(alias, path):
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


os.environ.setdefault("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
finops = _load("aegis_finops_sentry", FINOPS_PATH)
sentry = _load("aegis_phantom_sentry", SENTRY_PATH)


class PutRecorder:
    """Stands in for a boto3 DynamoDB Table: PutItem overwrites by (PK, SK)."""

    def __init__(self):
        self.rows = {}
        self.calls = 0

    def put_item(self, Item, **_options):
        self.calls += 1
        self.rows[(Item["PK"], Item["SK"])] = Item


def money(value):
    """Independent 6-dp oracle (ROUND_HALF_UP)."""
    quantized = Decimal(value).quantize(PLACES, rounding=ROUND_HALF_UP)
    return "0.000000" if quantized == 0 else str(quantized)


def contains_float(value):
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(contains_float(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(contains_float(item) for item in value)
    return False


def by_address(report, address):
    for finding in report["findings"]:
        if finding["address"] == address:
            return finding
    raise AssertionError("no finding for {0} in {1}".format(address, report["findings"]))


# --------------------------------------------------------------------------- #
# 1. The shipped regression plan                                             #
# --------------------------------------------------------------------------- #

def test_regression_plan_is_parsed_from_the_shipped_fixture():
    report = finops.scan_terraform_plan()
    assert report["tool"] == "scan_terraform_plan"
    assert report["source"] == "fixtures/tfplan.json"
    assert report["plan"]["format_version"] == "1.2"
    assert report["plan"]["timestamp"] == "2026-10-01T00:00:00Z"
    assert report["resources_scanned"] == 4
    assert report["resources_evaluated"] == 4
    assert report["resources_flagged"] == 2
    assert report["ok"] is False


def test_orphaned_ebs_burn_is_decimal_exact():
    report = finops.scan_terraform_plan()
    finding = by_address(report, "aws_ebs_volume.orphaned_gp3")
    assert finding["status"] == "ORPHANED_EBS_VOLUME"
    assert finding["severity"] == "critical"
    assert finding["action"] == "SNAPSHOT_THEN_DELETE"
    assert finding["rule"] == "DR-1"
    assert "attachments: []" in finding["detail"]
    expected_monthly = Decimal(100) * GP3_RATE
    assert finding["monthly_waste_usd"] == money(expected_monthly)
    assert finding["hourly_waste_usd"] == money(expected_monthly / HOURS_PER_MONTH)
    assert finding["hourly_waste_usd"] == "0.010959"
    assert finding["monthly_waste_usd"] == "8.000000"


def test_idle_provisioned_concurrency_burn_is_decimal_exact():
    report = finops.scan_terraform_plan()
    finding = by_address(
        report, "aws_lambda_provisioned_concurrency_config.idle_warm_pool"
    )
    assert finding["status"] == "IDLE_WARM_POOL"
    assert finding["severity"] == "warning"
    assert finding["rule"] == "FINOPS-PC-1"
    # 20 PCU x 256 MB (0.25 GB, read from the plan's aws_lambda_function)
    expected_hourly = Decimal(20) * Decimal("0.25") * Decimal(3600) * PC_RATE
    assert finding["hourly_waste_usd"] == money(expected_hourly)
    assert finding["monthly_waste_usd"] == money(expected_hourly * HOURS_PER_MONTH)
    assert finding["hourly_waste_usd"] == "0.075001"
    assert finding["monthly_waste_usd"] == "54.750438"


def test_compliant_resources_are_reported_but_not_flagged():
    report = finops.scan_terraform_plan()
    assert by_address(report, "aws_dynamodb_table.aegis_state")["status"] == "SERVERLESS_COMPLIANT"
    assert by_address(report, "aws_lambda_function.aegis_core")["status"] == "GRAVITON_COMPLIANT"
    assert report["severity"] == {"critical": 1, "warning": 1, "ok": 2}


def test_plan_totals_equal_the_sum_of_their_findings():
    report = finops.scan_terraform_plan()
    hourly = sum(
        (Decimal(finding["hourly_waste_usd"]) for finding in report["findings"]),
        Decimal("0"),
    )
    monthly = sum(
        (Decimal(finding["monthly_waste_usd"]) for finding in report["findings"]),
        Decimal("0"),
    )
    assert report["waste"]["hourly_usd"] == money(hourly)
    assert report["waste"]["monthly_usd"] == money(monthly)
    assert report["waste"]["annual_usd"] == money(monthly * Decimal(12))
    assert report["waste"]["monthly_usd"] == "62.750438"
    assert report["waste"]["annual_usd"] == "753.005256"


def test_report_contains_no_ieee754_value_anywhere():
    report = finops.scan_terraform_plan()
    assert not contains_float(report)
    encoded = json.dumps(report)
    assert "NaN" not in encoded and "Infinity" not in encoded
    for finding in report["findings"]:
        for key in ("hourly_waste_usd", "monthly_waste_usd"):
            assert finding[key] is None or isinstance(finding[key], str), (finding, key)


# --------------------------------------------------------------------------- #
# 2. Plan variants (inline documents, deterministic)                         #
# --------------------------------------------------------------------------- #

def make_plan(resources, children=None):
    root_module = {"resources": resources}
    if children is not None:
        root_module["child_modules"] = children
    return {
        "format_version": "1.2",
        "terraform_version": "1.9.5",
        "timestamp": "2026-10-01T00:00:00Z",
        "planned_values": {"root_module": root_module},
    }


def resource(address, resource_type, values):
    return {
        "address": address,
        "mode": "managed",
        "type": resource_type,
        "name": address.split(".")[-1],
        "values": values,
    }


def test_nat_gateway_is_flagged_with_its_flat_hourly_rate():
    report = finops.scan_terraform_plan(
        plan=make_plan(
            [resource("aws_nat_gateway.idle", "aws_nat_gateway", {"connectivity_type": "public"})]
        )
    )
    finding = by_address(report, "aws_nat_gateway.idle")
    assert finding["status"] == "IDLE_NAT_GATEWAY"
    assert finding["severity"] == "warning"
    assert finding["rule"] == "DR-3"
    assert finding["hourly_waste_usd"] == money(NAT_RATE)
    assert finding["monthly_waste_usd"] == money(NAT_RATE * HOURS_PER_MONTH)
    assert finding["hourly_waste_usd"] == "0.045000"
    assert finding["monthly_waste_usd"] == "32.850000"
    assert report["resources_flagged"] == 1
    assert report["waste"]["monthly_usd"] == "32.850000"


def test_attached_volume_is_not_waste():
    report = finops.scan_terraform_plan(
        plan=make_plan(
            [
                resource(
                    "aws_ebs_volume.attached",
                    "aws_ebs_volume",
                    {"size": 50, "type": "gp3", "attachments": [{"device_name": "/dev/sdf"}]},
                )
            ]
        )
    )
    finding = by_address(report, "aws_ebs_volume.attached")
    assert finding["status"] == "ATTACHED"
    assert finding["severity"] == "ok"
    assert finding["hourly_waste_usd"] == "0.000000"
    assert finding["monthly_waste_usd"] == "0.000000"
    assert report["resources_flagged"] == 0
    assert report["ok"] is True


def test_unpriced_volume_type_is_flagged_without_inventing_money():
    report = finops.scan_terraform_plan(
        plan=make_plan(
            [
                resource(
                    "aws_ebs_volume.io1",
                    "aws_ebs_volume",
                    {"size": 100, "type": "io1", "attachments": []},
                )
            ]
        )
    )
    finding = by_address(report, "aws_ebs_volume.io1")
    assert finding["status"] == "ORPHANED_EBS_VOLUME"
    assert finding["severity"] == "critical"
    assert finding["hourly_waste_usd"] is None
    assert finding["monthly_waste_usd"] is None
    assert "no list price" in finding["detail"]
    assert report["resources_flagged"] == 1
    assert report["waste"]["monthly_usd"] == "0.000000"


def test_dynamodb_provisioned_capacity_and_disabled_ttl_are_critical():
    report = finops.scan_terraform_plan(
        plan=make_plan(
            [
                resource(
                    "aws_dynamodb_table.provisioned",
                    "aws_dynamodb_table",
                    {"billing_mode": "PROVISIONED", "ttl": [{"enabled": True}]},
                ),
                resource(
                    "aws_dynamodb_table.no_ttl",
                    "aws_dynamodb_table",
                    {"billing_mode": "PAY_PER_REQUEST", "ttl": [{"enabled": False}]},
                ),
            ]
        )
    )
    assert by_address(report, "aws_dynamodb_table.provisioned")["status"] == "PROVISIONED_CAPACITY"
    assert by_address(report, "aws_dynamodb_table.no_ttl")["status"] == "TTL_DISABLED"
    assert report["severity"]["critical"] == 2
    assert report["resources_flagged"] == 2


def test_lambda_drift_is_flagged_without_a_monetary_guess():
    report = finops.scan_terraform_plan(
        plan=make_plan(
            [
                resource(
                    "aws_lambda_function.x86",
                    "aws_lambda_function",
                    {"architectures": ["x86_64"], "memory_size": 256},
                ),
                resource(
                    "aws_lambda_function.reserved",
                    "aws_lambda_function",
                    {"architectures": ["arm64"], "reserved_concurrent_executions": 5},
                ),
            ]
        )
    )
    assert by_address(report, "aws_lambda_function.x86")["status"] == "ARCH_DRIFT"
    assert by_address(report, "aws_lambda_function.reserved")["status"] == "RESERVED_CONCURRENCY"
    assert report["resources_flagged"] == 2
    assert report["waste"]["monthly_usd"] == "0.000000"
    assert contains_float(report) is False


def test_always_on_compute_breaks_the_zero_idle_contract():
    report = finops.scan_terraform_plan(
        plan=make_plan(
            [resource("aws_instance.zombie", "aws_instance", {"instance_type": "m5.large"})]
        )
    )
    finding = by_address(report, "aws_instance.zombie")
    assert finding["status"] == "ALWAYS_ON_COMPUTE"
    assert finding["severity"] == "critical"
    assert finding["rule"] == "DR-2"
    assert finding["hourly_waste_usd"] is None


def test_child_modules_are_scanned_too():
    child = {
        "address": "module.network",
        "resources": [
            resource(
                "module.network.aws_nat_gateway.egress",
                "aws_nat_gateway",
                {"connectivity_type": "public"},
            )
        ],
    }
    report = finops.scan_terraform_plan(
        plan=make_plan(
            [
                resource(
                    "aws_ebs_volume.root",
                    "aws_ebs_volume",
                    {"size": 10, "type": "gp3", "attachments": []},
                )
            ],
            children=[child],
        )
    )
    assert report["resources_scanned"] == 2
    assert report["resources_flagged"] == 2
    assert (
        by_address(report, "module.network.aws_nat_gateway.egress")["status"]
        == "IDLE_NAT_GATEWAY"
    )


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"planned_values": {}},
        {"planned_values": {"root_module": []}},
        {},
    ],
)
def test_unreadable_plans_fail_loudly(document):
    with pytest.raises(ValueError):
        finops.scan_terraform_plan(plan=document)


def test_missing_or_escaping_plan_paths_fail_loudly():
    with pytest.raises(ValueError):
        finops.scan_terraform_plan(path="fixtures/does-not-exist.json")
    with pytest.raises(ValueError):
        finops.scan_terraform_plan(path="../../etc/passwd")


# --------------------------------------------------------------------------- #
# 3. Event-level sentry decisions (DR-1 / DR-2 / DR-3)                       #
# --------------------------------------------------------------------------- #

def test_orphaned_volume_event_is_flagged_not_terminated():
    decision = sentry.classify_resource(
        {
            "resource_id": "vol-0a1",
            "resource_type": "AWS::EC2::Volume",
            "attachments": [],
            "cost_per_hour": "0.0109589041",
        }
    )
    assert decision["action_taken"] == "FLAG_ORPHAN"
    assert decision["circuit_breaker_triggered"] is False
    assert decision["rule_id"] == "DR-1"
    assert decision["estimated_hourly_burn"] == "0.010959"
    assert isinstance(decision["estimated_hourly_burn"], str)
    assert not contains_float(decision)


def test_volume_in_the_available_state_is_flagged_even_without_an_attachment_list():
    decision = sentry.classify_resource(
        {"resource_id": "vol-0a2", "resource_type": "AWS::EC2::Volume", "state": "available"}
    )
    assert decision["action_taken"] == "FLAG_ORPHAN"
    assert decision["rule_id"] == "DR-1"


def test_volumes_without_evidence_are_never_invented():
    decision = sentry.classify_resource(
        {"resource_id": "vol-0a3", "resource_type": "AWS::EC2::Volume"}
    )
    assert decision["action_taken"] == "ALLOW"
    assert decision["rule_id"] is None


@pytest.mark.parametrize(
    "attachments,expected",
    [
        ("[]", "FLAG_ORPHAN"),
        ('[{"device_name": "/dev/sdf"}]', "ALLOW"),
        ("not-json", "ALLOW"),
        ([], "FLAG_ORPHAN"),
        ([{"device_name": "/dev/sdf"}], "ALLOW"),
    ],
)
def test_attachment_evidence_accepts_list_and_json_text(attachments, expected):
    decision = sentry.classify_resource(
        {
            "resource_id": "vol-0a4",
            "resource_type": "AWS::EC2::Volume",
            "attachments": attachments,
        }
    )
    assert decision["action_taken"] == expected


def test_nat_gateway_event_is_flagged_as_idle_burn():
    decision = sentry.classify_resource(
        {
            "resource_id": "nat-0b1",
            "resource_type": "AWS::EC2::NatGateway",
            "cost_per_hour": "0.045",
        }
    )
    assert decision["action_taken"] == "FLAG_IDLE_NAT"
    assert decision["circuit_breaker_triggered"] is False
    assert decision["rule_id"] == "DR-3"
    assert decision["estimated_hourly_burn"] == "0.045000"


@pytest.mark.parametrize("resource_type", ["AWS::EC2::Instance", "AWS::RDS::DBInstance"])
def test_always_on_compute_trips_the_circuit_breaker(resource_type):
    decision = sentry.classify_resource(
        {"resource_id": "i-0c1", "resource_type": resource_type, "cost_per_hour": 1}
    )
    assert decision["action_taken"] == "TERMINATE_SIGNAL_SENT"
    assert decision["circuit_breaker_triggered"] is True
    assert decision["rule_id"] == "DR-2"
    assert decision["estimated_hourly_burn"] == "1.000000"


def test_a_resource_without_an_id_is_not_given_an_invented_identity():
    decision = sentry.classify_resource({"resource_type": "AWS::Lambda::Function"})
    assert decision["resource_id"] is None
    assert decision["action_taken"] == "ALLOW"

    recorder = PutRecorder()
    with mock.patch.object(sentry, "table", recorder):
        item = sentry.inspect_cloud_event({"resource_type": "AWS::Lambda::Function"})
    # The row is still deterministic, but no resource_id is claimed, so the core
    # can fall back to the SQS envelope id instead of storing RESOURCE#None.
    assert item["SK"] == "RESOURCE#UNIDENTIFIED"
    assert "resource_id" not in item


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.0109589041", "0.010959"),
        ("0.045", "0.045000"),
        ("-0.0000004", "0.000000"),
        ("0", "0.000000"),
        (None, "0.000000"),
    ],
)
def test_burn_is_quantized_to_micro_dollars(raw, expected):
    assert sentry.burn_text(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "NaN", "Infinity", True])
def test_unparseable_burns_raise(raw):
    with pytest.raises(ValueError):
        sentry.burn_text(raw)


def test_inspect_cloud_event_persists_the_single_table_row():
    payload = {
        "resource_id": "vol-0a1",
        "resource_type": "AWS::EC2::Volume",
        "attachments": [],
        "cost_per_hour": "0.0109589041",
    }
    recorder = PutRecorder()
    with mock.patch.object(sentry, "table", recorder):
        item = sentry.inspect_cloud_event(payload)

    assert recorder.calls == 1
    assert item["PK"] == "GUARD#SECURITY_AUDIT"
    assert item["SK"] == "RESOURCE#vol-0a1"
    assert item["rule_id"] == "DR-1"
    assert item["ttl"] - item["audited_at"] == 86400
    assert item["estimated_hourly_burn"] == "0.010959"
    assert not contains_float(item)
    assert recorder.rows[("GUARD#SECURITY_AUDIT", "RESOURCE#vol-0a1")] is item


def test_allow_rows_omit_rule_id_because_dynamodb_rejects_null():
    recorder = PutRecorder()
    with mock.patch.object(sentry, "table", recorder):
        item = sentry.inspect_cloud_event(
            {"resource_id": "fn-0d1", "resource_type": "AWS::Lambda::Function"}
        )
    assert item["action_taken"] == "ALLOW"
    assert "rule_id" not in item
    assert None not in item.values()


# --------------------------------------------------------------------------- #
# 4. MCP protocol parity with mcp/aegis_mcp_server.py                         #
# --------------------------------------------------------------------------- #

governance = _load("aegis_governance_server", MCP_SERVER_PATH)


def call_frame(tool_name, arguments):
    return finops.process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        )
    )


def decode_tool(frame):
    content = frame["result"]["content"]
    assert content[0]["type"] == "text"
    return json.loads(content[0]["text"])


def test_tools_list_publishes_the_finops_scanner():
    listing = finops.process_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    tools = listing["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == [
        "get_finops_guardrail_status",
        "verify_compliance",
        "scan_terraform_plan",
    ]
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)


def test_tool_results_use_the_same_envelope_as_the_governance_server():
    finops_frame = call_frame("scan_terraform_plan", {})
    governance_frame = governance.process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 43,
                "method": "tools/call",
                "params": {"name": "audit_ast_float_safety", "arguments": {"max_findings": 1}},
            }
        )
    )
    assert set(finops_frame) == set(governance_frame) == {"jsonrpc", "id", "result"}
    assert set(finops_frame["result"]) == set(governance_frame["result"]) == {"content", "isError"}
    assert set(finops_frame["result"]["content"][0]) == {"type", "text"}
    assert finops_frame["result"]["isError"] is False
    assert decode_tool(finops_frame)["tool"] == "scan_terraform_plan"


@pytest.mark.parametrize("server", [finops, governance])
def test_error_codes_and_notifications_match_the_protocol(server):
    unknown_method = server.process_line(
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "aegis/does-not-exist"})
    )
    assert unknown_method["error"]["code"] == -32601
    parse_error = server.process_line("{not valid json")
    assert parse_error["error"]["code"] == -32700
    unknown_tool = server.process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "not_a_tool", "arguments": {}},
            }
        )
    )
    assert unknown_tool["error"]["code"] == -32602
    assert server.process_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})) is None
    assert server.process_line(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}))["result"] == {}


def test_scan_tool_accepts_an_inline_plan():
    payload = decode_tool(
        call_frame(
            "scan_terraform_plan",
            {
                "plan": make_plan(
                    [
                        resource(
                            "aws_nat_gateway.inline",
                            "aws_nat_gateway",
                            {"connectivity_type": "public"},
                        )
                    ]
                )
            },
        )
    )
    assert payload["tool"] == "scan_terraform_plan"
    assert payload["source"] == "<inline plan>"
    assert payload["resources_flagged"] == 1


def test_tool_arguments_are_validated_and_fail_as_is_error():
    both = call_frame(
        "scan_terraform_plan", {"path": "fixtures/tfplan.json", "plan": make_plan([])}
    )
    assert both["result"]["isError"] is True
    payload = decode_tool(both)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "ValueError"


def test_guardrail_status_reports_decimal_strings():
    status = finops.get_guardrail_status()
    assert status["status"] == "Aegis Ward Active"
    assert status["idle_cost_per_month_usd"] == "0.000000"
    assert status["cost_per_10k_invocations_usd"] == "0.004000"
    assert Decimal(status["idle_cost_per_month_usd"]) == 0
    assert Decimal(status["cost_per_10k_invocations_usd"]) == Decimal("0.004")
    assert status["terraform_regression_plan"] == "fixtures/tfplan.json"


def test_verify_compliance_checks_the_iac_contract_and_can_scan_a_plan():
    plain = finops.verify_compliance()
    assert plain["compliant"] is True
    assert plain["violations"] == []
    assert "terraform_plan" not in plain

    with_plan = finops.verify_compliance(plan_path="fixtures/tfplan.json")
    assert with_plan["compliant"] is True
    assert with_plan["terraform_plan"]["resources_flagged"] == 2


# --------------------------------------------------------------------------- #
# 5. CLI contracts: --dry-run and --scan                                     #
# --------------------------------------------------------------------------- #

def run_cli(path, *arguments):
    return subprocess.run(
        [sys.executable, path] + list(arguments),
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        timeout=180,
        check=False,
    )


def test_finops_dry_run_exits_zero_with_every_check_green():
    completed = run_cli(FINOPS_PATH, "--dry-run")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert all(check["ok"] for check in payload["checks"]), payload["checks"]
    assert len(payload["checks"]) == 17


def test_governance_dry_run_is_still_fourteen_of_fourteen():
    completed = run_cli(MCP_SERVER_PATH, "--dry-run")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert all(check["ok"] for check in payload["checks"]), payload["checks"]
    assert len(payload["checks"]) == 14


def test_scan_cli_prints_the_decimal_report():
    completed = run_cli(FINOPS_PATH, "--scan", "fixtures/tfplan.json")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["resources_flagged"] == 2
    assert payload["waste"]["monthly_usd"] == "62.750438"
    assert not contains_float(payload)
