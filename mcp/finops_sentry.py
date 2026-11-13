#!/usr/bin/env python3
"""AegisSkeleton — FinOps Sentry MCP server (JSON-RPC 2.0 over stdio).

The AI-agent audit door for the zero-idle constitution. It speaks the same
protocol frames, tool-result envelope and error codes as the governance server
(``mcp/aegis_mcp_server.py``): one UTF-8 JSON document per line on stdin, one
frame per line on stdout, diagnostics on stderr, standard library only.

Tools
-----
get_finops_guardrail_status
    Returns the ward status, the pay-per-request billing mode and the idle cost
    of the deployed skeleton. Money is returned as decimal strings (Rule 4), so
    no consumer can re-introduce IEEE-754 drift by parsing a float.

verify_compliance
    Checks the IaC contract (``template.yaml`` + ``.kiro/specs``) for banned
    always-on resources. Optionally scans a Terraform plan in the same call.

scan_terraform_plan
    Parses a Terraform execution plan (``fixtures/tfplan.json`` by default) and
    prices the idle burn of every planned resource with ``decimal.Decimal``:

    * ``aws_ebs_volume``  — unattached volume, priced from the gp2/gp3 list price
    * ``aws_nat_gateway`` — flat hourly charge whatever the traffic
    * ``aws_lambda_provisioned_concurrency_config`` — idle warm pool
    * ``aws_dynamodb_table`` — PROVISIONED capacity or a disabled TTL
    * ``aws_lambda_function`` — non-Graviton architecture / reserved concurrency
    * ``aws_instance`` / ``aws_db_instance`` — always-on compute (zero-idle)

    Hourly, monthly (730 h) and annual totals are decimal strings. Data
    processing GB, provisioned IOPS and instance-type rates are *not* in a plan,
    so they are never invented: such findings are flagged without a price.

CLI
---
    python3 mcp/finops_sentry.py               # serve JSON-RPC over stdio
    python3 mcp/finops_sentry.py --dry-run     # protocol + scanner self-test
    python3 mcp/finops_sentry.py --scan PATH   # one-shot plan scan (JSON)
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

JSONRPC_VERSION = "2.0"
SERVER_NAME = "finops-sentry"
SERVER_VERSION = "1.1.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAN_PATH = REPO_ROOT / "fixtures" / "tfplan.json"

# --------------------------------------------------------------------------- #
# Policy constants (us-east-1 list prices, quoted as Decimal only)             #
# --------------------------------------------------------------------------- #

MONEY_PLACES = Decimal("0.000001")      # six decimals: micro-dollar resolution
ZERO = Decimal("0")
HOURS_PER_MONTH = Decimal("730")
MONTHS_PER_YEAR = Decimal("12")
SECONDS_PER_HOUR = Decimal("3600")

EBS_USD_PER_GB_MONTH = {
    "gp3": Decimal("0.08"),
    "gp2": Decimal("0.10"),
}
NAT_GATEWAY_USD_PER_HOUR = Decimal("0.045")
LAMBDA_ARM64_PC_USD_PER_GB_SECOND = Decimal("0.0000041667")
DEFAULT_LAMBDA_RAM_GB = Decimal("0.25")   # 256 MB skeleton contract

SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

RULE_ORPHAN_EBS = "DR-1"
RULE_ALWAYS_ON_COMPUTE = "DR-2"
RULE_IDLE_NAT = "DR-3"
RULE_PROVISIONED_CONCURRENCY = "FINOPS-PC-1"
RULE_DYNAMODB_CAPACITY = "FINOPS-DDB-1"
RULE_DYNAMODB_TTL = "SCHEMA-1"
RULE_LAMBDA_ARCH = "ZERO-IDLE-1"
RULE_RESERVED_CONCURRENCY = "ZERO-IDLE-2"

NOTES = (
    "Only list prices declared in this module are applied; nothing is invented.",
    "NAT data-processing GB, provisioned IOPS and instance-type rates are absent "
    "from a Terraform plan, so those findings carry no monetary estimate.",
    "Monthly = hourly x 730 h; annual = monthly x 12.",
)


def _decimal(value: Any, field: str) -> Optional[Decimal]:
    """Parse a plan quantity into Decimal; ``None``/``""`` mean 'not declared'."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Plan field '{0}' must be numeric, got boolean.".format(field))
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("Unparseable plan field '{0}': {1!r}".format(field, value)) from error
    if not number.is_finite():
        raise ValueError("Non-finite plan field '{0}': {1!r}".format(field, value))
    return number


def _money(value: Optional[Decimal]) -> Optional[str]:
    """Format a Decimal as a six-decimal string, or None when unpriced."""
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 40
        quantized = Decimal(value).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)
    if quantized == 0:
        quantized = abs(quantized)   # never emit "-0.000000"
    return str(quantized)


def _quantity(value: Optional[Decimal]) -> str:
    """Format a plan quantity without trailing zeros (``100``, ``0.25``)."""
    if value is None:
        return "0"
    text = format(Decimal(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


# --------------------------------------------------------------------------- #
# Terraform plan inventory                                                    #
# --------------------------------------------------------------------------- #

def collect_plan_resources(plan: Any) -> List[Dict[str, Any]]:
    """Return every resource under ``planned_values`` (root + child modules).

    Raises ``ValueError`` when the document is not a Terraform plan: a plan we
    cannot read must fail loudly, never scan as "clean".
    """
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object, got {0}".format(type(plan).__name__))
    planned = plan.get("planned_values")
    if not isinstance(planned, dict):
        raise ValueError("plan is missing the planned_values object")
    root = planned.get("root_module")
    if not isinstance(root, dict):
        raise ValueError("plan is missing planned_values.root_module")

    module_stack = [root]
    collected: List[Dict[str, Any]] = []
    while module_stack:
        module = module_stack.pop()
        for resource in module.get("resources") or []:
            if isinstance(resource, dict) and resource.get("type"):
                collected.append(resource)
        for child in module.get("child_modules") or []:
            if isinstance(child, dict):
                module_stack.append(child)

    collected.sort(key=lambda resource: str(resource.get("address") or resource.get("type")))
    return collected


def lambda_ram_gb_by_name(resources: List[Dict[str, Any]]) -> Dict[str, Decimal]:
    """Map ``function_name -> memory in GB`` from ``aws_lambda_function``."""
    ram_gb_by_name: Dict[str, Decimal] = {}
    for resource in resources:
        if resource.get("type") != "aws_lambda_function":
            continue
        values = resource.get("values") or {}
        name = values.get("function_name") or resource.get("name")
        megabytes = _decimal(values.get("memory_size"), "memory_size")
        if name and megabytes is not None and megabytes > 0:
            ram_gb_by_name[str(name)] = megabytes / Decimal("1024")
    return ram_gb_by_name


def _finding(
    address: str,
    resource_type: str,
    status: str,
    severity: str,
    action: str,
    detail: str,
    rule: Optional[str] = None,
    hourly: Optional[Decimal] = None,
    monthly: Optional[Decimal] = None,
) -> Dict[str, Any]:
    """Build one findings entry; money stays a decimal string or None."""
    return {
        "address": address,
        "type": resource_type,
        "status": status,
        "severity": severity,
        "action": action,
        "rule": rule,
        "detail": detail,
        "hourly_waste_usd": _money(hourly),
        "monthly_waste_usd": _money(monthly),
    }


def evaluate_resource(
    resource: Dict[str, Any],
    ram_gb_by_name: Optional[Dict[str, Decimal]] = None,
) -> Optional[Dict[str, Any]]:
    """Classify and price one planned resource; None when no rule applies."""
    values = resource.get("values") or {}
    resource_type = str(resource.get("type") or "unknown")
    address = str(resource.get("address") or resource_type)

    if resource_type == "aws_ebs_volume":
        volume_type = str(values.get("type") or "gp3")
        rate = EBS_USD_PER_GB_MONTH.get(volume_type)
        size_gb = _decimal(values.get("size"), "size") or ZERO
        attachments = values.get("attachments")
        attached = isinstance(attachments, list) and len(attachments) > 0

        if attached:
            return _finding(
                address, resource_type, "ATTACHED", SEVERITY_OK, "KEEP",
                "{0} GB {1} · volume is attached".format(_quantity(size_gb), volume_type),
                "EBS-ATTACHED", ZERO, ZERO,
            )

        if rate is None:
            detail = "{0} GB {1} · unattached, but no list price for this volume type".format(
                _quantity(size_gb), volume_type
            )
            return _finding(
                address, resource_type, "ORPHANED_EBS_VOLUME", SEVERITY_CRITICAL,
                "SNAPSHOT_THEN_DELETE", detail, RULE_ORPHAN_EBS, None, None,
            )

        monthly = size_gb * rate
        hourly = monthly / HOURS_PER_MONTH
        detail = "{0} GB {1} · attachments: [] · unattached provisioned storage".format(
            _quantity(size_gb), volume_type
        )
        return _finding(
            address, resource_type, "ORPHANED_EBS_VOLUME", SEVERITY_CRITICAL,
            "SNAPSHOT_THEN_DELETE", detail, RULE_ORPHAN_EBS, hourly, monthly,
        )

    if resource_type == "aws_nat_gateway":
        hourly = NAT_GATEWAY_USD_PER_HOUR
        monthly = hourly * HOURS_PER_MONTH
        detail = "connectivity_type={0} · flat hourly charge regardless of traffic".format(
            values.get("connectivity_type") or "public"
        )
        return _finding(
            address, resource_type, "IDLE_NAT_GATEWAY", SEVERITY_WARNING,
            "REVIEW_OR_REPLACE_WITH_VPC_ENDPOINT", detail, RULE_IDLE_NAT, hourly, monthly,
        )


    if resource_type == "aws_lambda_provisioned_concurrency_config":
        pcu = _decimal(
            values.get("provisioned_concurrent_executions"),
            "provisioned_concurrent_executions",
        ) or ZERO
        function_name = str(values.get("function_name") or "")
        known = ram_gb_by_name or {}
        ram_gb = known.get(function_name)
        size_source = "from plan"
        if ram_gb is None:
            ram_gb = DEFAULT_LAMBDA_RAM_GB
            size_source = "256 MB contract default"
        detail = "qualifier={0} · {1} PCU × {2} GB ({3}) · idle provisioned concurrency".format(
            values.get("qualifier") or "?", _quantity(pcu), _quantity(ram_gb), size_source
        )
        if pcu <= 0:
            return _finding(
                address, resource_type, "NO_PROVISIONED_CAPACITY", SEVERITY_OK,
                "KEEP", detail, RULE_PROVISIONED_CONCURRENCY, ZERO, ZERO,
            )
        hourly = pcu * ram_gb * SECONDS_PER_HOUR * LAMBDA_ARM64_PC_USD_PER_GB_SECOND
        monthly = hourly * HOURS_PER_MONTH
        return _finding(
            address, resource_type, "IDLE_WARM_POOL", SEVERITY_WARNING,
            "REMOVE_PROVISIONED_CONCURRENCY", detail, RULE_PROVISIONED_CONCURRENCY,
            hourly, monthly,
        )

    if resource_type == "aws_dynamodb_table":
        billing_mode = str(values.get("billing_mode") or "PROVISIONED")
        ttl_entries = values.get("ttl")
        ttl_enabled = isinstance(ttl_entries, list) and any(
            isinstance(entry, dict) and entry.get("enabled") is True for entry in ttl_entries
        )
        if billing_mode != "PAY_PER_REQUEST":
            return _finding(
                address, resource_type, "PROVISIONED_CAPACITY", SEVERITY_CRITICAL,
                "SWITCH_TO_PAY_PER_REQUEST",
                "billing_mode={0} · provisioned capacity bills while idle".format(billing_mode),
                RULE_DYNAMODB_CAPACITY, ZERO, ZERO,
            )
        if not ttl_enabled:
            return _finding(
                address, resource_type, "TTL_DISABLED", SEVERITY_CRITICAL,
                "ENABLE_TTL",
                "billing_mode=PAY_PER_REQUEST but no enabled TTL entry",
                RULE_DYNAMODB_TTL, ZERO, ZERO,
            )
        return _finding(
            address, resource_type, "SERVERLESS_COMPLIANT", SEVERITY_OK, "KEEP",
            "billing_mode=PAY_PER_REQUEST · TTL enabled",
            RULE_DYNAMODB_TTL, ZERO, ZERO,
        )

    if resource_type == "aws_lambda_function":
        architectures = values.get("architectures")
        graviton = isinstance(architectures, list) and "arm64" in architectures
        reserved = values.get("reserved_concurrent_executions")
        reserved_label = "unreserved" if reserved in (-1, None) else "{0} reserved".format(reserved)
        if not graviton:
            return _finding(
                address, resource_type, "ARCH_DRIFT", SEVERITY_WARNING,
                "SWITCH_TO_ARM64",
                "architectures={0} · Graviton arm64 is the cheaper profile".format(
                    architectures if architectures is not None else "unset"
                ),
                RULE_LAMBDA_ARCH, ZERO, ZERO,
            )
        if isinstance(reserved, int) and not isinstance(reserved, bool) and reserved >= 0:
            return _finding(
                address, resource_type, "RESERVED_CONCURRENCY", SEVERITY_WARNING,
                "RELEASE_RESERVED_CONCURRENCY",
                "reserved_concurrent_executions={0} · capacity is held while idle".format(reserved),
                RULE_RESERVED_CONCURRENCY, ZERO, ZERO,
            )
        return _finding(
            address, resource_type, "GRAVITON_COMPLIANT", SEVERITY_OK, "KEEP",
            "architectures=arm64 · {0}".format(reserved_label),
            RULE_LAMBDA_ARCH, ZERO, ZERO,
        )

    if resource_type in ("aws_instance", "aws_db_instance"):
        return _finding(
            address, resource_type, "ALWAYS_ON_COMPUTE", SEVERITY_CRITICAL,
            "STOP_OR_REPLACE",
            "always-on compute breaks the scale-to-zero contract; the plan carries no list price",
            RULE_ALWAYS_ON_COMPUTE, None, None,
        )

    return None



# --------------------------------------------------------------------------- #
# Plan scan                                                                   #
# --------------------------------------------------------------------------- #

def scan_plan(plan: Any, source: str = "<inline plan>") -> Dict[str, Any]:
    """Scan a decoded Terraform plan and price its idle burn.

    Money that is not declared in the plan is never estimated: such findings
    carry ``null`` hourly/monthly values and are still counted as flagged.
    """
    resources = collect_plan_resources(plan)
    ram_gb_by_name = lambda_ram_gb_by_name(resources)

    findings: List[Dict[str, Any]] = []
    hourly_total = ZERO
    monthly_total = ZERO
    flagged = 0
    critical = 0
    warning = 0

    for resource in resources:
        finding = evaluate_resource(resource, ram_gb_by_name)
        if finding is None:
            continue
        findings.append(finding)
        if finding["severity"] == SEVERITY_CRITICAL:
            flagged += 1
            critical += 1
        elif finding["severity"] == SEVERITY_WARNING:
            flagged += 1
            warning += 1
        if finding["hourly_waste_usd"] is not None:
            hourly_total += Decimal(finding["hourly_waste_usd"])
        if finding["monthly_waste_usd"] is not None:
            monthly_total += Decimal(finding["monthly_waste_usd"])

    report = {
        "tool": "scan_terraform_plan",
        "source": source,
        "plan": {
            "format_version": plan.get("format_version"),
            "terraform_version": plan.get("terraform_version"),
            "timestamp": plan.get("timestamp"),
        },
        "resources_scanned": len(resources),
        "resources_evaluated": len(findings),
        "resources_flagged": flagged,
        "severity": {
            "critical": critical,
            "warning": warning,
            "ok": len(findings) - flagged,
        },
        "findings": findings,
        "waste": {
            "hourly_usd": _money(hourly_total),
            "monthly_usd": _money(monthly_total),
            "annual_usd": _money(monthly_total * MONTHS_PER_YEAR),
        },
        "policy": {
            "money_precision": "Decimal, 6 dp, ROUND_HALF_UP",
            "hours_per_month": _money(HOURS_PER_MONTH),
            "months_per_year": _money(MONTHS_PER_YEAR),
            "ebs_usd_per_gb_month": {
                key: _money(value) for key, value in sorted(EBS_USD_PER_GB_MONTH.items())
            },
            "nat_gateway_usd_per_hour": _money(NAT_GATEWAY_USD_PER_HOUR),
            "lambda_arm64_pc_usd_per_gb_second": _money(LAMBDA_ARM64_PC_USD_PER_GB_SECOND),
            "skeleton_idle_cost_usd_per_month": "0.000000",
        },
        "notes": list(NOTES),
        "ok": flagged == 0,
    }
    return report


def _relative(path: Path) -> str:
    """Repo-relative display path, falling back to the absolute path."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_plan_path(raw: Optional[str] = None) -> Path:
    """Resolve a plan path inside the repository; reject any escape attempt."""
    candidate = Path(raw) if raw else DEFAULT_PLAN_PATH
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    resolved = candidate.resolve()
    if resolved != REPO_ROOT and REPO_ROOT not in resolved.parents:
        raise ValueError("Plan path escapes the repository root: {0}".format(resolved))
    if not resolved.is_file():
        raise ValueError("Plan file not found: {0}".format(resolved))
    return resolved


def scan_terraform_plan(
    path: Optional[str] = None,
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Scan a plan from disk (default ``fixtures/tfplan.json``) or from memory."""
    if plan is not None:
        return scan_plan(plan, source="<inline plan>")
    resolved = resolve_plan_path(path)
    text = resolved.read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("Plan is not valid JSON: {0}".format(error))
    return scan_plan(document, source=_relative(resolved))



# --------------------------------------------------------------------------- #
# Guardrail status + IaC compliance                                           #
# --------------------------------------------------------------------------- #

def get_guardrail_status() -> Dict[str, Any]:
    """Return the ward status and the zero-idle cost posture.

    Every monetary figure is a decimal string; a consumer that wants to do
    arithmetic parses ``Decimal``, never ``float`` (Rule 4).
    """
    return {
        "status": "Aegis Ward Active",
        "billing_mode": "100% Pay-Per-Request (Serverless)",
        "idle_cost_per_month_usd": _money(ZERO),
        "cost_per_10k_invocations_usd": _money(Decimal("0.004")),
        "circuit_breaker": {
            "sqs_max_receive_count": 3,
            "lambda_concurrency_limit": 500,
            "lambda_timeout_seconds": 10,
        },
        "allowed_persistence": ["DynamoDB Pay-Per-Request with TTL"],
        "forbidden_patterns": ["localStorage", "sessionStorage", "IndexedDB", "EC2-AlwaysOn"],
        "terraform_regression_plan": _relative(DEFAULT_PLAN_PATH),
        "money_precision": "decimal.Decimal (no IEEE-754)",
    }


def verify_compliance(plan_path: Optional[str] = None) -> Dict[str, Any]:
    """Check the IaC contract for banned always-on resources.

    Optionally scans a Terraform plan in the same call; the plan verdict is
    reported separately so ``compliant`` keeps meaning "the IaC contract holds".
    """
    spec_path = REPO_ROOT / ".kiro" / "specs" / "core-architecture.spec.json"
    template_path = REPO_ROOT / "template.yaml"
    violations: List[str] = []

    if not spec_path.exists():
        violations.append("缺少 .kiro/specs/core-architecture.spec.json 引導文件")
    if not template_path.exists():
        violations.append("缺少 template.yaml IaC 定義檔")

    if template_path.exists():
        content = template_path.read_text(encoding="utf-8")
        if "AWS::EC2::Instance" in content:
            violations.append("檢測到長期運行的 EC2 實例，違反零閒置成本原則！")
        if "AWS::RDS::DBInstance" in content:
            violations.append("檢測到常駐型 RDS 實例，違反 Serverless 按需原則！")

    result: Dict[str, Any] = {
        "compliant": len(violations) == 0,
        "violations": violations,
        "recommendation": (
            "架構完全合規，符合防爆與零閒置成本規範。"
            if len(violations) == 0
            else "請修復上述違規項目。"
        ),
    }
    if plan_path is not None:
        result["terraform_plan"] = scan_terraform_plan(path=plan_path)
    return result



# --------------------------------------------------------------------------- #
# MCP tool registry (same envelope as mcp/aegis_mcp_server.py)                 #
# --------------------------------------------------------------------------- #

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "get_finops_guardrail_status",
        "title": "FinOps guardrail status",
        "description": "獲取 AegisSkeleton 零閒置成本結界與資源熔斷狀態（金額為 Decimal 字串）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "verify_compliance",
        "title": "IaC compliance",
        "description": "驗證 template.yaml 與 spec 是否符合無閒置成本規範；可同時掃描一份 Terraform plan。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_path": {
                    "type": "string",
                    "description": "Repository-relative path of a tfplan.json to scan in the same call.",
                }
            },
        },
    },
    {
        "name": "scan_terraform_plan",
        "title": "Terraform plan waste scan",
        "description": (
            "以 decimal.Decimal 解析 Terraform plan（預設 fixtures/tfplan.json），"
            "偵測未掛載 EBS、NAT Gateway、閒置 provisioned concurrency 等浪費，"
            "並輸出每小時 / 每月 / 每年 Decimal 金額。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative plan path; defaults to fixtures/tfplan.json.",
                },
                "plan": {
                    "type": "object",
                    "description": "Inline decoded Terraform plan document (alternative to `path`).",
                },
            },
        },
    },
]

TOOL_NAMES = tuple(tool["name"] for tool in TOOL_DEFINITIONS)


def _response(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": int(code), "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def _error_detail(exc: BaseException) -> Dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


def _tool_payload(payload: Dict[str, Any], is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}
        ],
        "isError": bool(is_error),
    }


def _initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    requested = params.get("protocolVersion")
    protocol = (
        requested
        if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
        else DEFAULT_PROTOCOL_VERSION
    )
    return {
        "protocolVersion": protocol,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": SERVER_NAME,
            "title": "Aegis FinOps Sentry",
            "version": SERVER_VERSION,
        },
        "instructions": (
            "Zero-idle FinOps tools: scan_terraform_plan prices idle EBS volumes, NAT "
            "gateways and provisioned concurrency with Decimal arithmetic; "
            "verify_compliance checks the IaC contract for always-on resources."
        ),
    }


def _call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name == "get_finops_guardrail_status":
        return get_guardrail_status()

    if name == "verify_compliance":
        plan_path = arguments.get("plan_path")
        if plan_path is not None and not isinstance(plan_path, str):
            raise ValueError("`plan_path` must be a string.")
        return verify_compliance(plan_path=plan_path or None)

    if name == "scan_terraform_plan":
        path = arguments.get("path")
        plan = arguments.get("plan")
        if path is not None and not isinstance(path, str):
            raise ValueError("`path` must be a string.")
        if plan is not None and not isinstance(plan, dict):
            raise ValueError("`plan` must be a JSON object.")
        if path is not None and plan is not None:
            raise ValueError("Pass either `path` or `plan`, not both.")
        return scan_terraform_plan(path=path, plan=plan)

    raise KeyError(name)



def _tool_result(frame: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Decode the JSON text carried by a tools/call result frame."""
    content = (frame or {}).get("result", {}).get("content", [])
    if content and isinstance(content[0], dict) and content[0].get("type") == "text":
        try:
            decoded = json.loads(content[0]["text"])
        except (json.JSONDecodeError, TypeError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def dry_run() -> int:
    """Exercise the real protocol path and the real scanner; never touches AWS."""
    checks: List[Dict[str, Any]] = []

    def record(label: str, passed: bool, detail: str = "") -> bool:
        checks.append({"check": label, "ok": bool(passed), "detail": "" if passed else detail})
        return bool(passed)

    initialize = process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "finops-dry-run", "version": SERVER_VERSION},
                },
            }
        )
    )
    init_result = (initialize or {}).get("result", {})
    record(
        "initialize",
        bool(initialize)
        and initialize.get("id") == 1
        and initialize.get("jsonrpc") == JSONRPC_VERSION
        and init_result.get("serverInfo", {}).get("name") == SERVER_NAME,
        "initialize did not return a valid serverInfo frame",
    )
    record(
        "initialize.protocolVersion",
        init_result.get("protocolVersion") == DEFAULT_PROTOCOL_VERSION,
        "unexpected protocolVersion negotiation",
    )
    record(
        "initialize.capabilities.tools",
        isinstance(init_result.get("capabilities", {}).get("tools"), dict),
        "tools capability missing",
    )

    notification = process_line(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    )
    record("notification.silent", notification is None, "notifications must not produce a frame")

    listing = process_line(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    tools = (listing or {}).get("result", {}).get("tools", [])
    listed_names = [tool.get("name") for tool in tools]
    record(
        "tools/list",
        listed_names == list(TOOL_NAMES),
        "expected {0}, got {1}".format(list(TOOL_NAMES), listed_names),
    )
    record(
        "tools/list:inputSchema",
        all(tool.get("inputSchema", {}).get("type") == "object" for tool in tools),
        "every tool must publish an object inputSchema",
    )

    scan_call = process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "scan_terraform_plan", "arguments": {}},
            }
        )
    )
    report = _tool_result(scan_call)
    record(
        "tools/call:scan_terraform_plan",
        report.get("tool") == "scan_terraform_plan" and report.get("ok") is False,
        "the regression plan must scan as not-ok (idle waste present)",
    )
    record(
        "tools/call:scan_terraform_plan.resources",
        report.get("resources_scanned") == 4 and report.get("resources_flagged") == 2,
        "expected 4 planned resources with 2 flagged, got {0}/{1}".format(
            report.get("resources_scanned"), report.get("resources_flagged")
        ),
    )
    waste = report.get("waste") or {}
    record(
        "tools/call:scan_terraform_plan.decimal_money",
        all(isinstance(waste.get(key), str) for key in ("hourly_usd", "monthly_usd", "annual_usd"))
        and isinstance(report.get("policy", {}).get("hours_per_month"), str),
        "every monetary figure must be a decimal string, never a float",
    )

    status_call = process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "get_finops_guardrail_status", "arguments": {}},
            }
        )
    )
    status = _tool_result(status_call)
    record(
        "tools/call:get_finops_guardrail_status",
        status.get("status") == "Aegis Ward Active"
        and isinstance(status.get("idle_cost_per_month_usd"), str),
        "guardrail payload is missing the ward status or decimal idle cost",
    )

    compliance_call = process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "verify_compliance", "arguments": {}},
            }
        )
    )
    compliance = _tool_result(compliance_call)
    record(
        "tools/call:verify_compliance",
        compliance.get("compliant") is True,
        "the shipped IaC contract reported violations: {0}".format(compliance.get("violations")),
    )

    escaped = process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "scan_terraform_plan", "arguments": {"path": "../../etc/passwd"}},
            }
        )
    )
    record(
        "tools/call:path escape -> isError",
        (escaped or {}).get("result", {}).get("isError") is True,
        "a plan path outside the repository must be refused",
    )

    unknown = process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "not_a_tool", "arguments": {}},
            }
        )
    )
    record(
        "tools/call:unknown -> -32602",
        (unknown or {}).get("error", {}).get("code") == -32602,
        "unknown tools must return JSON-RPC -32602",
    )

    malformed = process_line("{not valid json")
    record(
        "parse error -> -32700",
        (malformed or {}).get("error", {}).get("code") == -32700,
        "malformed frames must return JSON-RPC -32700",
    )

    missing = process_line(
        json.dumps({"jsonrpc": "2.0", "id": 8, "method": "aegis/does-not-exist"})
    )
    record(
        "method not found -> -32601",
        (missing or {}).get("error", {}).get("code") == -32601,
        "unknown methods must return JSON-RPC -32601",
    )

    ping = process_line(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}))
    record("ping", (ping or {}).get("result") == {}, "ping must answer with an empty result")

    frames = [
        frame
        for frame in (
            initialize, listing, scan_call, status_call, compliance_call, escaped, unknown, ping
        )
        if frame is not None
    ]
    try:
        for frame in frames:
            json.dumps(frame)
        serialisable = True
    except (TypeError, ValueError):
        serialisable = False
    record("json.serialisable", serialisable, "a protocol frame is not JSON serialisable")

    ok = all(check["ok"] for check in checks)
    print(
        json.dumps(
            {
                "tool": "dry-run",
                "server": SERVER_NAME,
                "version": SERVER_VERSION,
                "protocol": DEFAULT_PROTOCOL_VERSION,
                "repo_root": str(REPO_ROOT),
                "ok": ok,
                "checks": checks,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1



class ServerExit(Exception):
    """Raised by the `exit` notification to stop the stdio loop cleanly."""


def handle_message(message: Any) -> Optional[Dict[str, Any]]:
    """Route one decoded JSON-RPC document. Returns None for notifications."""
    if isinstance(message, list):
        return _error(None, -32600, "Invalid Request: batch requests are not supported.")
    if not isinstance(message, dict):
        return _error(None, -32600, "Invalid Request: expected a JSON object.")

    request_id = message.get("id")
    is_notification = "id" not in message
    method = message.get("method")
    if not isinstance(method, str) or not method:
        if is_notification:
            return None
        return _error(request_id, -32600, "Invalid Request: missing method.")

    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if is_notification:
            return None
        return _error(request_id, -32602, "Invalid params: expected an object.")

    if method == "initialize":
        return _response(request_id, _initialize(params))
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _response(request_id, {})
    if method == "tools/list":
        return _response(request_id, {"tools": TOOL_DEFINITIONS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or name not in TOOL_NAMES:
            return _error(
                request_id,
                -32602,
                "Invalid params: unknown tool {0!r}.".format(name),
                {"tools": list(TOOL_NAMES)},
            )
        if not isinstance(arguments, dict):
            return _error(
                request_id, -32602, "Invalid params: `arguments` must be a JSON object."
            )
        try:
            payload = _call_tool(name, arguments)
        except Exception as exc:  # tool faults surface as isError results, not crashes
            sys.stderr.write("finops-sentry tool {0} failed: {1}\n".format(name, exc))
            return _response(
                request_id,
                _tool_payload(
                    {"tool": name, "ok": False, "error": _error_detail(exc)}, is_error=True
                ),
            )
        return _response(request_id, _tool_payload(payload))
    if method == "resources/list":
        return _response(request_id, {"resources": []})
    if method == "prompts/list":
        return _response(request_id, {"prompts": []})
    if method == "logging/setLevel":
        return _response(request_id, {})
    if method == "shutdown":
        return _response(request_id, None)
    if method == "exit":
        raise ServerExit()

    return _error(request_id, -32601, "Method not found: {0}".format(method))


def process_line(line: str) -> Optional[Dict[str, Any]]:
    """Decode and route one stdio line — the single path used by serve/--dry-run."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        message = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return _error(None, -32700, "Parse error: {0} at column {1}.".format(exc.msg, exc.colno))
    return handle_message(message)


def serve(stdin=None, stdout=None) -> int:
    """Blocking stdio loop: one JSON-RPC frame in, one frame out."""
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    while True:
        try:
            line = source.readline()
        except (KeyboardInterrupt, EOFError):
            break
        if not line:
            break
        try:
            response = process_line(line)
        except ServerExit:
            break
        except Exception as exc:  # keep the loop alive, report on stderr only
            sys.stderr.write("finops-sentry dispatch failure: {0}\n".format(exc))
            response = _error(None, -32603, "Internal error while dispatching the request.")
        if response is None:
            continue
        try:
            sink.write(json.dumps(response, ensure_ascii=False) + "\n")
            sink.flush()
        except (BrokenPipeError, OSError):
            break
    return 0


def handle_rpc(line: str) -> Optional[Dict[str, Any]]:
    """Backwards-compatible one-shot handler: route a line and print the frame."""
    response = process_line(line)
    if response is not None:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return response


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Aegis FinOps Sentry MCP server")
    parser.add_argument("--dry-run", action="store_true", help="run the protocol self-test")
    parser.add_argument("--scan", metavar="PATH", help="scan one Terraform plan and print JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.dry_run:
        return dry_run()
    if args.scan:
        print(json.dumps(scan_terraform_plan(path=args.scan), indent=2, ensure_ascii=False))
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main())

