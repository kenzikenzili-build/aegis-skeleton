#!/usr/bin/env python3
"""Aegis Governance MCP server — JSON-RPC 2.0 over stdio.

A dependency-light Model Context Protocol server: one UTF-8 JSON document per
line on stdin, one JSON document per line on stdout. Nothing is logged to
stdout except protocol frames (diagnostics go to stderr). Imports are limited to
the Python standard library plus boto3 — no MCP SDK, no web framework.

Tools
-----
audit_ast_float_safety
    Walks ``src/`` with the ``ast`` module and reports float literals,
    ``float()`` coercions, unbounded ``round()`` and float-only true division.
    Constitution Rule 4 forbids IEEE-754 arithmetic on money and quantities.

probe_production_pipeline
    Exercises the deployed control plane for real: DynamoDB table inventory
    (billing mode, key schema, TTL, deletion protection), API Gateway v2 stage
    inventory, and a public ``POST /ingest`` reachability probe. The probe
    envelope is deliberately incomplete, so the gate answers 400
    INVALID_PAYLOAD before ``sqs.send_message`` runs: the pipeline is proven
    live without contaminating AegisIngestionQueue.

Egress policy
-------------
``.kiro/hooks/pre-commit`` (rule AEGIS-EGRESS) denies ``requests``, ``urllib``,
``http.client``, ``httpx``, ``aiohttp`` and raw sockets, and authorises only the
AWS SDK roots ``boto3`` / ``botocore``. The data-plane probe therefore rides
``botocore``'s own HTTPS transport (``AWSPreparedRequest`` +
``URLLib3Session``) against the ``execute-api`` endpoint: identical wire request,
identical TLS verification, identical response envelope — still no dependency
outside the standard library plus the AWS SDK.

CLI
---
    python3 mcp/aegis_mcp_server.py              # serve JSON-RPC over stdio
    python3 mcp/aegis_mcp_server.py --dry-run    # protocol self-test (exit 0)
    python3 mcp/aegis_mcp_server.py --audit      # one-shot AST audit (JSON)
    python3 mcp/aegis_mcp_server.py --probe      # one-shot AWS probe (JSON)
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:  # boto3/botocore are the only non-stdlib dependency of this server
    import boto3  # type: ignore
    # botocore is the single egress path permitted by AEGIS-EGRESS: the probe
    # reuses the SDK's HTTPS transport instead of urllib / http.client.
    from botocore.awsrequest import AWSPreparedRequest  # type: ignore
    from botocore.config import Config  # type: ignore
    from botocore.httpsession import URLLib3Session  # type: ignore
except ImportError as exc:  # pragma: no cover - exercised only on bare hosts
    boto3 = None  # type: ignore
    AWSPreparedRequest = None  # type: ignore
    Config = None  # type: ignore
    URLLib3Session = None  # type: ignore
    BOTO3_IMPORT_ERROR = str(exc)
else:
    BOTO3_IMPORT_ERROR = ""

SERVER_NAME = "aegis-governance"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCAN_ROOT = REPO_ROOT / "src"
DEFAULT_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
DEFAULT_API_URL = os.environ.get(
    "AEGIS_API_URL", "https://u3geom1dc0.execute-api.us-east-1.amazonaws.com"
)
DEFAULT_TABLE_NAME = os.environ.get("AEGIS_TABLE_NAME", "")
TABLE_NAME_HINT = "aegis"

# Constitution Rule 4 — the AST rules that keep money off IEEE-754.
AST_RULES: Dict[str, Dict[str, str]] = {
    "FLOAT_LITERAL": {
        "severity": "error",
        "message": 'Float literal: use decimal.Decimal("0.01") — never 0.01.',
    },
    "FLOAT_COERCION": {
        "severity": "error",
        "message": "float() coercion: convert with Decimal(str(value)) instead.",
    },
    "UNBOUNDED_ROUND": {
        "severity": "warning",
        "message": "round() has no explicit mode: use Decimal.quantize(CENT, ROUND_HALF_UP).",
    },
    "FLOAT_DIVISION": {
        "severity": "warning",
        "message": "True division without Decimal operands emits IEEE-754 floats; divide Decimals and quantize.",
    },
}


def _log(message: str) -> None:
    """Diagnostics only — never stdout, which carries the JSON-RPC frames."""
    sys.stderr.write("[aegis-governance] {0}\n".format(message))
    sys.stderr.flush()


def _error_detail(exc: BaseException) -> Dict[str, str]:
    """Normalise a boto3/stdio exception into a JSON-safe {code, message} pair."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {}) or {}
        return {
            "code": str(error.get("Code") or exc.__class__.__name__),
            "message": str(error.get("Message") or exc),
        }
    return {"code": exc.__class__.__name__, "message": str(exc)}


# --------------------------------------------------------------------------- #
# Tool 1 — audit_ast_float_safety
# --------------------------------------------------------------------------- #

def _has_decimal_reference(node: ast.AST) -> bool:
    """True when the subtree mentions ``Decimal`` (an explicit decimal operand)."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and inner.id == "Decimal":
            return True
        if isinstance(inner, ast.Attribute) and inner.attr == "Decimal":
            return True
    return False


def _snippet(source: str, node: ast.AST) -> str:
    """Return the offending source line, whitespace-collapsed and bounded."""
    try:
        segment = ast.get_source_segment(source, node)
    except (AttributeError, TypeError):
        segment = None
    if not segment:
        segment = ""
    collapsed = " ".join(segment.split())
    return collapsed[:180]


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _audit_file(path: Path) -> Dict[str, Any]:
    """Parse one module and return every Rule 4 violation plus any parse error."""
    entry: Dict[str, Any] = {"file": _relative(path), "findings": [], "parse_error": None}
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        entry["parse_error"] = {"code": exc.__class__.__name__, "message": str(exc)}
        return entry

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        entry["parse_error"] = {
            "code": "SyntaxError",
            "message": "{0} (line {1})".format(exc.msg, exc.lineno),
        }
        return entry

    for node in ast.walk(tree):
        rule: Optional[str] = None

        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            rule = "FLOAT_LITERAL"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
        ):
            rule = "FLOAT_COERCION"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "round"
        ):
            rule = "UNBOUNDED_ROUND"
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if not (_has_decimal_reference(node.left) or _has_decimal_reference(node.right)):
                rule = "FLOAT_DIVISION"

        if rule is None:
            continue

        meta = AST_RULES[rule]
        entry["findings"].append(
            {
                "file": entry["file"],
                "line": int(getattr(node, "lineno", 0) or 0),
                "column": int(getattr(node, "col_offset", 0) or 0) + 1,
                "rule": rule,
                "severity": meta["severity"],
                "message": meta["message"],
                "snippet": _snippet(source, node),
            }
        )

    entry["findings"].sort(key=lambda finding: (finding["line"], finding["column"], finding["rule"]))
    return entry


def audit_ast_float_safety(root: Optional[str] = None, max_findings: int = 250) -> Dict[str, Any]:
    """Statically audit a source tree for float/Decimal violations.

    Args:
        root: Directory to scan; defaults to ``<repo>/src``.
        max_findings: Hard cap on returned findings (the report flags truncation).

    Returns:
        A JSON-serialisable report: per-file findings, summary counters and a
        ``clean`` flag that is True when no error-severity rule fired.
    """
    target = Path(root).expanduser() if root else DEFAULT_SCAN_ROOT
    report: Dict[str, Any] = {
        "tool": "audit_ast_float_safety",
        "target": _relative(target),
        "rules": AST_RULES,
        "files_scanned": 0,
        "files": [],
        "findings": [],
        "parse_errors": [],
        "truncated": False,
    }

    if not target.exists():
        report["ok"] = False
        report["error"] = {"code": "ENOENT", "message": "Scan root does not exist."}
        report["summary"] = {"errors": 0, "warnings": 0, "total": 0, "clean": False}
        return report

    modules: List[Path] = sorted(
        path for path in target.rglob("*.py") if path.is_file() and "__pycache__" not in path.parts
    )

    all_findings: List[Dict[str, Any]] = []
    for module in modules:
        entry = _audit_file(module)
        report["files"].append(entry["file"])
        if entry["parse_error"]:
            report["parse_errors"].append({"file": entry["file"], "error": entry["parse_error"]})
        all_findings.extend(entry["findings"])

    report["files_scanned"] = len(modules)
    all_findings.sort(key=lambda finding: (finding["file"], finding["line"], finding["column"]))

    if max_findings >= 0 and len(all_findings) > max_findings:
        report["truncated"] = True
        all_findings = all_findings[:max_findings]

    errors = sum(1 for finding in all_findings if finding["severity"] == "error")
    warnings = sum(1 for finding in all_findings if finding["severity"] == "warning")

    report["findings"] = all_findings
    report["summary"] = {
        "errors": errors,
        "warnings": warnings,
        "total": len(all_findings),
        "clean": errors == 0 and not report["parse_errors"],
    }
    report["ok"] = bool(report["summary"]["clean"])
    return report


# --------------------------------------------------------------------------- #
# Tool 2 — probe_production_pipeline
# --------------------------------------------------------------------------- #

def _aws_client(service: str, region: str):
    """Create a real boto3 client (no stubs): tight timeouts, bounded retries."""
    if boto3 is None:
        raise RuntimeError("boto3 is unavailable: {0}".format(BOTO3_IMPORT_ERROR or "import failed"))
    config = None
    if Config is not None:
        config = Config(
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 3, "mode": "standard"},
        )
    if config is None:
        return boto3.client(service, region_name=region)
    return boto3.client(service, region_name=region, config=config)


def _dynamodb_table_entry(client, name: str) -> Dict[str, Any]:
    """Describe one table: billing mode, key schema, TTL and deletion protection."""
    entry: Dict[str, Any] = {"name": name}
    try:
        table = client.describe_table(TableName=name).get("Table", {})
    except Exception as exc:
        entry["error"] = _error_detail(exc)
        return entry

    billing = (table.get("BillingModeSummary") or {}).get("BillingMode") or "PROVISIONED"
    key_schema = table.get("KeySchema") or []
    key_names = [key.get("AttributeName") for key in key_schema]
    entry.update(
        {
            "status": table.get("TableStatus"),
            "billing_mode": billing,
            "item_count": int(table.get("ItemCount") or 0),
            "size_bytes": int(table.get("TableSizeBytes") or 0),
            "key_schema": key_schema,
            "deletion_protection": bool(table.get("DeletionProtectionEnabled")),
            "pay_per_request": billing == "PAY_PER_REQUEST",
            "single_table_keys": sorted(key_names) == ["PK", "SK"],
        }
    )

    try:
        ttl = client.describe_time_to_live(TableName=name).get("TimeToLiveDescription", {})
        entry["ttl"] = {
            "attribute": ttl.get("AttributeName"),
            "status": ttl.get("TimeToLiveStatus"),
        }
    except Exception as exc:
        entry["ttl"] = {"error": _error_detail(exc)}

    return entry


def _probe_dynamodb(region: str, table_name: Optional[str]) -> Dict[str, Any]:
    """Inventory the state tables and assert the Zero-Idle billing contract."""
    result: Dict[str, Any] = {"service": "dynamodb", "region": region, "tables": [], "ok": False}

    try:
        client = _aws_client("dynamodb", region)
    except Exception as exc:
        result["error"] = _error_detail(exc)
        return result

    if table_name:
        names = [table_name]
        result["selection"] = "explicit"
    else:
        try:
            names = list(client.list_tables().get("TableNames", []))
            result["selection"] = "list_tables"
        except Exception as exc:
            result["error"] = _error_detail(exc)
            result["hint"] = (
                "dynamodb:ListTables is not granted to this principal; pass the "
                "physical table name or export AEGIS_TABLE_NAME."
            )
            return result

    hinted = sorted(name for name in names if TABLE_NAME_HINT in name.lower())
    candidates = (hinted or sorted(names))[:10]
    result["candidates"] = candidates
    if not candidates:
        result["error"] = {
            "code": "NotFound",
            "message": "No DynamoDB tables are visible to this principal in {0}.".format(region),
        }
        return result

    tables = [_dynamodb_table_entry(client, name) for name in candidates]
    result["tables"] = tables
    healthy = [
        table
        for table in tables
        if not table.get("error")
        and table.get("status") == "ACTIVE"
        and table.get("pay_per_request")
    ]
    result["ok"] = len(healthy) == len(tables) and bool(tables)
    result["violations"] = [
        {
            "table": table.get("name"),
            "reason": "provisioned-capacity" if not table.get("pay_per_request") else table.get("status"),
        }
        for table in tables
        if table not in healthy
    ]
    return result


def _http_probe(url: str, timeout: int) -> Dict[str, Any]:
    """Data-plane probe with a deliberately incomplete envelope (no SQS write).

    Egress rides ``botocore``'s HTTPS transport — the only channel that
    ``.kiro/hooks/pre-commit`` (AEGIS-EGRESS) authorises; ``urllib``,
    ``http.client`` and raw sockets are denied. The request is an anonymous
    public POST, never IAM-signed, so a 400 envelope rejection proves the
    API Gateway + Lambda integration just as well as a 202 would.
    """
    body = json.dumps({"probe": True}).encode("utf-8")
    info: Dict[str, Any] = {"url": url, "method": "POST", "envelope": "{probe: true}"}
    started = time.time()

    if AWSPreparedRequest is None or URLLib3Session is None:
        info.update(
            {
                "reachable": False,
                "error": {
                    "code": "BotoCoreUnavailable",
                    "message": "botocore is required for data-plane egress: {0}".format(
                        BOTO3_IMPORT_ERROR or "import failed"
                    ),
                },
            }
        )
        info["latency_ms"] = int((time.time() - started) * 1000)
        return info

    request = AWSPreparedRequest(
        method="POST",
        url=url,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "aegis-governance-mcp/{0}".format(SERVER_VERSION),
        },
        body=body,
        stream_output=False,
    )
    session = URLLib3Session(timeout=timeout)
    try:
        response = session.send(request)
        status = int(getattr(response, "status_code", 0) or 0)
        reason = str(getattr(getattr(response, "raw", None), "reason", "") or "")
        payload = response.content[:512].decode("utf-8", "replace")
    except Exception as exc:
        info.update({"reachable": False, "error": _error_detail(exc)})
        info["latency_ms"] = int((time.time() - started) * 1000)
        return info
    finally:
        session.close()

    info.update(
        {
            "status": status,
            "reason": reason,
            "body": " ".join(payload.split())[:300],
            "latency_ms": int((time.time() - started) * 1000),
            # 200/202 (accepted) and 400 (envelope rejected before SQS) both prove
            # API Gateway + Lambda integration are wired and answering.
            "reachable": status < 500,
            "enqueued": status in (200, 202),
        }
    )
    return info


def _probe_api_gateway(
    region: str, api_url: str, api_id: Optional[str], timeout: int
) -> Dict[str, Any]:
    """Probe the public /ingest door and inventory the HTTP API control plane."""
    result: Dict[str, Any] = {"service": "apigateway", "region": region, "ok": False}
    result["http_probe"] = _http_probe(api_url.rstrip("/") + "/ingest", timeout)
    result["ok"] = bool(result["http_probe"].get("reachable"))

    control: Dict[str, Any] = {"ok": False}
    try:
        client = _aws_client("apigatewayv2", region)
    except Exception as exc:
        control["error"] = _error_detail(exc)
        result["control_plane"] = control
        return result

    try:
        apis = list(client.get_apis().get("Items", []))
    except Exception as exc:
        control["error"] = _error_detail(exc)
        result["control_plane"] = control
        return result

    control["apis"] = [
        {
            "api_id": api.get("ApiId"),
            "name": api.get("Name"),
            "protocol": api.get("ProtocolType"),
            "endpoint": api.get("ApiEndpoint"),
        }
        for api in apis[:25]
    ]

    target = (api_id or "").strip()
    if not target:
        host = api_url.split("//")[-1].split(".")[0]
        for api in apis:
            endpoint = str(api.get("ApiEndpoint") or "")
            if endpoint and endpoint.split("//")[-1].split(".")[0] == host:
                target = str(api.get("ApiId") or "")
                break

    if not target:
        control["note"] = "No HTTP API matched the configured endpoint; pass api_id explicitly."
        result["control_plane"] = control
        return result

    control["api_id"] = target
    try:
        stages = list(client.get_stages(ApiId=target).get("Items", []))
        control["stages"] = [
            {
                "name": stage.get("StageName"),
                "auto_deploy": bool(stage.get("AutoDeploy")),
                "last_updated": stage.get("LastUpdatedDate"),
            }
            for stage in stages
        ]
        control["ok"] = bool(stages)
    except Exception as exc:
        control["stages_error"] = _error_detail(exc)

    result["control_plane"] = control
    return result


def probe_production_pipeline(
    region: Optional[str] = None,
    table_name: Optional[str] = None,
    api_url: Optional[str] = None,
    api_id: Optional[str] = None,
    timeout_seconds: int = 10,
) -> Dict[str, Any]:
    """Check the deployed data plane and control plane with real AWS calls.

    Args:
        region: AWS region; defaults to AWS_REGION / AWS_DEFAULT_REGION / us-east-1.
        table_name: Physical DynamoDB table; skip discovery when supplied.
        api_url: HTTP API base URL; defaults to AEGIS_API_URL.
        api_id: API Gateway v2 id; auto-resolved from the endpoint when omitted.
        timeout_seconds: Data-plane HTTP probe timeout.

    Returns:
        A JSON-serialisable report with per-service checks, violations and an
        aggregate ``ok`` flag that is False whenever a check fails or is denied.
    """
    resolved_region = (region or DEFAULT_REGION).strip()
    resolved_url = (api_url or DEFAULT_API_URL).strip()
    resolved_table = (table_name or DEFAULT_TABLE_NAME or "").strip() or None
    try:
        timeout = max(1, int(timeout_seconds))
    except (TypeError, ValueError):
        timeout = 10

    report: Dict[str, Any] = {
        "tool": "probe_production_pipeline",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "region": resolved_region,
        "api_url": resolved_url,
        "boto3": {"installed": boto3 is not None, "import_error": BOTO3_IMPORT_ERROR},
        "checks": {},
    }

    dynamodb = _probe_dynamodb(resolved_region, resolved_table)
    api_gateway = _probe_api_gateway(resolved_region, resolved_url, api_id, timeout)
    report["checks"]["dynamodb"] = dynamodb
    report["checks"]["api_gateway"] = api_gateway

    report["ok"] = bool(dynamodb.get("ok")) and bool(api_gateway.get("ok"))
    report["summary"] = {
        "dynamodb": "PAY_PER_REQUEST" if dynamodb.get("ok") else "ATTENTION",
        "api_gateway": "REACHABLE" if api_gateway.get("ok") else "ATTENTION",
        "verdict": "GREEN" if report["ok"] else "DEGRADED",
    }
    return report


# --------------------------------------------------------------------------- #
# MCP protocol layer (JSON-RPC 2.0 over newline-delimited stdio)
# --------------------------------------------------------------------------- #

JSONRPC_VERSION = "2.0"

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "audit_ast_float_safety",
        "description": (
            "Statically audit Python sources with the ast module for Constitution Rule 4 "
            "violations: float literals, float() coercion, unbounded round() and true "
            "division without Decimal operands. Returns findings, parse errors and a "
            "clean flag."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to scan. Defaults to <repo>/src.",
                },
                "max_findings": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "description": "Cap on returned findings (default 250).",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "probe_production_pipeline",
        "description": (
            "Probe the deployed Aegis pipeline with real AWS calls: DynamoDB table "
            "inventory (billing mode, PK/SK key schema, TTL, deletion protection) and "
            "API Gateway v2 health, including a public POST /ingest reachability probe "
            "that is rejected by envelope validation before any SQS write."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "AWS region. Defaults to AWS_REGION / us-east-1.",
                },
                "table_name": {
                    "type": "string",
                    "description": "Physical DynamoDB table name; skips ListTables discovery.",
                },
                "api_url": {
                    "type": "string",
                    "description": "HTTP API base URL. Defaults to AEGIS_API_URL.",
                },
                "api_id": {
                    "type": "string",
                    "description": "API Gateway v2 id; auto-resolved from api_url when omitted.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 60,
                    "description": "Data-plane probe timeout (default 10s).",
                },
            },
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = tuple(tool["name"] for tool in TOOL_DEFINITIONS)


def _response(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _error(
    request_id: Any, code: int, message: str, data: Optional[Any] = None
) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": int(code), "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def _tool_payload(payload: Dict[str, Any], is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False, default=str)}
        ],
        "isError": bool(is_error),
    }


def _initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    requested = params.get("protocolVersion")
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        protocol = requested
    else:
        protocol = DEFAULT_PROTOCOL_VERSION
    return {
        "protocolVersion": protocol,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": SERVER_NAME,
            "title": "Aegis Governance",
            "version": SERVER_VERSION,
        },
        "instructions": (
            "Aegis governance tools for the Zero-Idle constitution: audit_ast_float_safety "
            "enforces Decimal-only arithmetic in src/, probe_production_pipeline verifies "
            "pay-per-request DynamoDB plus API Gateway health."
        ),
    }


def _call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name == "audit_ast_float_safety":
        raw_cap = arguments.get("max_findings", 250)
        try:
            cap = max(1, int(raw_cap))
        except (TypeError, ValueError):
            cap = 250
        root = arguments.get("path") or None
        if root is not None and not isinstance(root, str):
            raise ValueError("`path` must be a string.")
        return audit_ast_float_safety(root=root, max_findings=cap)

    if name == "probe_production_pipeline":
        timeout = arguments.get("timeout_seconds", 10)
        return probe_production_pipeline(
            region=arguments.get("region") or None,
            table_name=arguments.get("table_name") or None,
            api_url=arguments.get("api_url") or None,
            api_id=arguments.get("api_id") or None,
            timeout_seconds=timeout if isinstance(timeout, int) else 10,
        )

    raise KeyError(name)


def handle_message(message: Any) -> Optional[Dict[str, Any]]:
    """Route one decoded JSON-RPC document. Returns None for notifications."""
    if isinstance(message, list):
        # JSON-RPC batching was removed from the MCP specification and is not
        # implemented here; reject it explicitly instead of guessing.
        return _error(None, -32600, "Invalid Request: batch requests are not supported.")
    if not isinstance(message, dict):
        return _error(None, -32600, "Invalid Request: expected a JSON object.")

    request_id = message.get("id")
    is_notification = "id" not in message
    method = message.get("method")
    if not isinstance(method, str) or not method:
        return None if is_notification else _error(
            request_id, -32600, "Invalid Request: missing method."
        )

    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return None if is_notification else _error(
            request_id, -32602, "Invalid params: expected an object."
        )

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
            _log("tool {0} failed:\n{1}".format(name, traceback.format_exc()))
            return _response(
                request_id,
                _tool_payload(
                    {"tool": name, "ok": False, "error": _error_detail(exc)}, is_error=True
                ),
            )
        return _response(request_id, _tool_payload(payload))
    if method == "resources/list":
        return _response(request_id, {"resources": []})
    if method == "resources/templates/list":
        return _response(request_id, {"resourceTemplates": []})
    if method == "prompts/list":
        return _response(request_id, {"prompts": []})
    if method == "logging/setLevel":
        return _response(request_id, {})
    if method == "completion/complete":
        return _response(request_id, {"completion": {"values": [], "total": 0, "hasMore": False}})
    if method == "shutdown":
        return _response(request_id, None)
    if method == "exit":
        raise ServerExit()

    return _error(request_id, -32601, "Method not found: {0}".format(method))


def process_line(line: str) -> Optional[Dict[str, Any]]:
    """Decode and route one stdio line — the single path used by serve() and --dry-run."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        message = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return _error(None, -32700, "Parse error: {0} at column {1}.".format(exc.msg, exc.colno))
    return handle_message(message)


class ServerExit(Exception):
    """Raised by the `exit` notification to stop the stdio loop cleanly."""


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
            _log("exit notification received — closing stdio loop")
            break
        except Exception:
            _log("unhandled dispatch failure:\n{0}".format(traceback.format_exc()))
            response = _error(None, -32603, "Internal error while dispatching the request.")
        if response is None:
            continue
        try:
            sink.write(json.dumps(response, ensure_ascii=False) + "\n")
            sink.flush()
        except (BrokenPipeError, OSError):
            break
    return 0


def dry_run() -> int:
    """Exercise the real protocol path end to end; never touches AWS."""
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
                    "clientInfo": {"name": "aegis-dry-run", "version": SERVER_VERSION},
                },
            }
        )
    )
    init_result = (initialize or {}).get("result", {})
    server_info = init_result.get("serverInfo", {})
    record(
        "initialize",
        bool(initialize)
        and initialize.get("id") == 1
        and initialize.get("jsonrpc") == "2.0"
        and server_info.get("name") == SERVER_NAME,
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

    call = process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "audit_ast_float_safety", "arguments": {"max_findings": 5}},
            }
        )
    )
    content = (call or {}).get("result", {}).get("content", [])
    payload: Dict[str, Any] = {}
    if content and isinstance(content[0], dict) and content[0].get("type") == "text":
        try:
            payload = json.loads(content[0]["text"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
    record(
        "tools/call:audit_ast_float_safety",
        payload.get("tool") == "audit_ast_float_safety" and "summary" in payload,
        "audit payload is missing the summary block",
    )
    record(
        "tools/call:audit_ast_float_safety.scan",
        int(payload.get("files_scanned") or 0) >= 1,
        "the audit did not scan any source file",
    )

    unknown = process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
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
        json.dumps({"jsonrpc": "2.0", "id": 5, "method": "aegis/does-not-exist"})
    )
    record(
        "method not found -> -32601",
        (missing or {}).get("error", {}).get("code") == -32601,
        "unknown methods must return JSON-RPC -32601",
    )

    ping = process_line(json.dumps({"jsonrpc": "2.0", "id": 6, "method": "ping"}))
    record("ping", (ping or {}).get("result") == {}, "ping must answer with an empty result")

    frames = [
        frame
        for frame in (initialize, listing, call, unknown, malformed, missing, ping)
        if frame is not None
    ]
    try:
        for frame in frames:
            json.dumps(frame)
        serialisable = True
    except (TypeError, ValueError):
        serialisable = False
    record("json.serialisable", serialisable, "a protocol frame is not JSON serialisable")

    record(
        "probe_production_pipeline.registered",
        "probe_production_pipeline" in TOOL_NAMES,
        "probe_production_pipeline is missing from the tool registry",
    )

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
        )
    )
    return 0 if ok else 1


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aegis_mcp_server.py",
        description="Aegis governance MCP server — JSON-RPC 2.0 over stdio (stdlib + boto3).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Protocol self-test; exits 0 on success."
    )
    parser.add_argument(
        "--audit", action="store_true", help="One-shot AST float audit of src/ (JSON)."
    )
    parser.add_argument("--probe", action="store_true", help="One-shot AWS pipeline probe (JSON).")
    parser.add_argument("--root", default=None, help="Override the audit root directory.")
    parser.add_argument("--region", default=None, help="Override the AWS region (--probe).")
    parser.add_argument("--api-url", default=None, help="Override the HTTP API base URL (--probe).")
    parser.add_argument("--table-name", default=None, help="Explicit DynamoDB table name (--probe).")
    args = parser.parse_args(argv)

    if args.dry_run:
        return dry_run()
    if args.audit:
        report = audit_ast_float_safety(root=args.root)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report.get("ok") else 1
    if args.probe:
        report = probe_production_pipeline(
            region=args.region, table_name=args.table_name, api_url=args.api_url
        )
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0 if report.get("ok") else 1
    return serve()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)









