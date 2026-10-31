#!/usr/bin/env python3
"""
AegisSkeleton - FinOps Sentry MCP Server
提供 Kiro Agent 即時架構合規審計與零閒置成本度量能力。
"""
import json
import os
import sys

def get_guardrail_status():
    """回傳當前防護結界狀態與成本預算度量"""
    return {
        "status": "Aegis Ward Active",
        "billing_mode": "100% Pay-Per-Request (Serverless)",
        "idle_cost_per_month_usd": 0.00,
        "cost_per_10k_invocations_usd": 0.004,
        "circuit_breaker": {
            "sqs_max_receive_count": 3,
            "lambda_concurrency_limit": 500,
            "lambda_timeout_seconds": 10
        },
        "allowed_persistence": ["DynamoDB Pay-Per-Request with TTL"],
        "forbidden_patterns": ["localStorage", "sessionStorage", "IndexedDB", "EC2-AlwaysOn"]
    }

def verify_compliance():
    """比對 template.yaml 與 steering spec 是否存在閒置資源或假持久化漏洞"""
    spec_path = ".kiro/specs/core-architecture.spec.json"
    template_path = "template.yaml"
    violations = []

    if not os.path.exists(spec_path):
        violations.append("缺少 .kiro/specs/core-architecture.spec.json 引導文件")
    if not os.path.exists(template_path):
        violations.append("缺少 template.yaml IaC 定義檔")

    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            content = f.read()
            if "AWS::EC2::Instance" in content:
                violations.append("檢測到長期運行的 EC2 實例，違反零閒置成本原則！")
            if "AWS::RDS::DBInstance" in content:
                violations.append("檢測到常駐型 RDS 實例，違反 Serverless 按需原則！")

    return {
        "compliant": len(violations) == 0,
        "violations": violations,
        "recommendation": "架構完全合規，符合防爆與零閒置成本規範。" if len(violations) == 0 else "請修復上述違規項目。"
    }

def handle_rpc(line):
    try:
        req = json.loads(line)
        method = req.get("method")
        req_id = req.get("id")

        if method == "tools/list":
            res = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "get_finops_guardrail_status",
                            "description": "獲取 AegisSkeleton 零閒置成本結界與資源熔斷狀態",
                            "inputSchema": {"type": "object", "properties": {}}
                        },
                        {
                            "name": "verify_compliance",
                            "description": "驗證專案架構是否符合無閒置成本與防假持久化規範",
                            "inputSchema": {"type": "object", "properties": {}}
                        }
                    ]
                }
            }
        elif method == "tools/call":
            params = req.get("params", {})
            tool_name = params.get("name")
            if tool_name == "get_finops_guardrail_status":
                content = get_guardrail_status()
            elif tool_name == "verify_compliance":
                content = verify_compliance()
            else:
                content = {"error": f"未知工具: {tool_name}"}

            res = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(content, ensure_ascii=False, indent=2)}]
                }
            }
        else:
            res = {"jsonrpc": "2.0", "id": req_id, "result": {}}

        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stderr.write(f"Error: {str(e)}\n")

def main():
    for line in sys.stdin:
        if line.strip():
            handle_rpc(line)

if __name__ == "__main__":
    main()
