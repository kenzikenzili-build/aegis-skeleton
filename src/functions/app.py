"""AegisSkeleton Ingestion Core (Stage 3: API Gateway & Domain Routing).

路徑：外部請求 → ``AegisIngestionQueue`` 緩衝 → 領域應用 → ``AegisStateTable``。

本模組提供兩個入口：

* ``ingest_handler`` — API Gateway HTTP API (``POST /ingest``)。驗證信封後立即
  投入 ``AegisIngestionQueue`` 並回傳 202，由佇列削峰 (Rule 1：所有外部寫入必
  經佇列緩衝)。
* ``lambda_handler`` — SQS 消費者。依 ``type`` 將訊息路由到
  ``apps/grim-reconciler``（對帳）或 ``apps/phantom-guard``（資安哨兵），並以單表
  ``PK``/``SK`` 搭配 TTL 落庫 (Rule 2)。

Financial values never touch ``float``：所有金額在寫入 DynamoDB 前都經
:class:`decimal.Decimal` 量化為字串 (Rule 4)。
"""

import base64
import importlib
import importlib.util
import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import boto3
from botocore.exceptions import ClientError

# Lambda 僅注入 AWS_REGION，而 botocore 以 AWS_DEFAULT_REGION 解析端點；領域應用
# 在 import 階段就會建立 boto3 resource，因此先對齊兩者以避免 NoRegionError。
os.environ.setdefault("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('TABLE_NAME', 'AegisStateTable'))
sqs = boto3.client('sqs')

INGESTION_QUEUE_URL = os.environ.get('INGESTION_QUEUE_URL', '')

# 請求信封 (envelope) 的必要欄位
REQUIRED_FIELDS = ("id", "type", "payload")

# 路由分派鍵
TYPE_RECONCILIATION = "reconciliation"
TYPE_CLOUD_AUDIT = "cloud_audit"
DEFAULT_EVENT_TYPE = "GENERIC_EVENT"

# 單表鍵空間 (Rule 2：一表、一組 PK/SK、一個 ttl 屬性)
PARTITION_RECONCILE = "RECONCILE#LEDGER"
PARTITION_SECURITY_AUDIT = "GUARD#SECURITY_AUDIT"
PARTITION_TENANT_SYSTEM = "TENANT#SYSTEM"

# TTL 契約：資安稽核 24h、對帳 7d
TTL_AUDIT_SECONDS = 86400
TTL_RECONCILE_SECONDS = 604800

CENT = Decimal("0.01")

# type -> (領域模組路徑, 入口函式)；連字號目錄於 import 時解析
DOMAIN_ROUTES = {
    TYPE_RECONCILIATION: ("apps.grim_reconciler.reconciler", "reconcile_transaction"),
    TYPE_CLOUD_AUDIT: ("apps.phantom_guard.sentry", "inspect_cloud_event"),
}


# --------------------------------------------------------------------------- #
# 共用小工具
# --------------------------------------------------------------------------- #

def _now():
    """目前 epoch 秒數 (TTL 與稽核時戳一律使用 UTC epoch)。"""
    return int(time.time())


def _log(message, **fields):
    """結構化日誌，CloudWatch Logs 可直接以 JSON 解析。"""
    payload = {"message": message}
    payload.update(fields)
    print(json.dumps(payload, default=str))


def _response(status_code, body):
    """組裝 API Gateway HTTP API (payload v2) 回應。"""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _api_body(event):
    """取出請求內文，兼容 base64 編碼與非字串 body。"""
    raw = event.get('body')
    if raw is None:
        return ""
    if event.get('isBase64Encoded'):
        raw = base64.b64decode(raw).decode("utf-8")
    if isinstance(raw, (dict, list)):
        return json.dumps(raw)
    return raw


def _money_text(value):
    """以 Decimal 量化金額並回傳字串；金額永不經過 float 運算 (Rule 4)。"""
    if value is None or value == "":
        value = 0
    if isinstance(value, bool):
        raise ValueError(f"Monetary value must be numeric, got boolean: {value!r}")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"Unparseable monetary value: {value!r}") from error
    if not amount.is_finite():
        raise ValueError(f"Non-finite monetary value: {value!r}")
    return str(amount.quantize(CENT, rounding=ROUND_HALF_UP))


def _decimalize(value):
    """遞迴把 float 轉為 Decimal，避免 DynamoDB 拒收 IEEE-754 浮點。"""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _decimalize(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimalize(inner) for inner in value]
    return value


# --------------------------------------------------------------------------- #
# 入口 1：API Gateway HTTP API (POST /ingest) — 只做驗證 + 入列
# --------------------------------------------------------------------------- #

def ingest_handler(event, context):
    """接收外部請求，消毒後投入 AegisIngestionQueue，回傳 202 Accepted。

    回應契約：
        202 -> {"status": "QUEUED", "message_id": <id>}
        400 -> 信封不合法 (缺 id/type/payload 或非 JSON)
        500 -> 未配置 INGESTION_QUEUE_URL
        503 -> SQS 拒收 (交由呼叫端重試)
    """
    if not INGESTION_QUEUE_URL:
        _log("ingest rejected: INGESTION_QUEUE_URL is not configured")
        return _response(
            500,
            {"status": "ERROR", "message": "Ingestion queue is not configured."},
        )

    try:
        envelope = json.loads(_api_body(event) or "{}")
    except (TypeError, ValueError):
        return _response(
            400,
            {"status": "INVALID_JSON", "message": "Request body must be valid JSON."},
        )

    if not isinstance(envelope, dict):
        return _response(
            400,
            {"status": "INVALID_PAYLOAD", "message": "Request body must be a JSON object."},
        )

    # 必填欄位：id / type / payload 缺一不可
    missing = [field for field in REQUIRED_FIELDS if envelope.get(field) in (None, "")]
    if missing:
        return _response(
            400,
            {
                "status": "INVALID_PAYLOAD",
                "message": "Missing required field(s): {0}".format(", ".join(missing)),
                "missing": missing,
            },
        )

    envelope["id"] = str(envelope["id"])
    envelope["type"] = str(envelope["type"])

    try:
        message_body = json.dumps(envelope)
        sqs.send_message(
            QueueUrl=INGESTION_QUEUE_URL,
            MessageBody=message_body,
            MessageAttributes={
                "type": {"DataType": "String", "StringValue": envelope["type"]},
            },
        )
    except TypeError as error:
        return _response(
            400,
            {
                "status": "INVALID_PAYLOAD",
                "message": "Payload is not JSON serialisable: {0}".format(error),
            },
        )
    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code", "Unknown")
        _log(
            "ingest queueing failed",
            message_id=envelope["id"],
            error_code=error_code,
        )
        return _response(
            503,
            {
                "status": "UNAVAILABLE",
                "message": "Ingestion queue rejected the message ({0}).".format(error_code),
            },
        )

    _log("ingest queued", message_id=envelope["id"], event_type=envelope["type"])
    return _response(202, {"status": "QUEUED", "message_id": envelope["id"]})


# --------------------------------------------------------------------------- #
# 領域模組解析 (支援 apps/grim-reconciler、apps/phantom-guard 連字號目錄)
# --------------------------------------------------------------------------- #

def _apps_directory():
    """定位 apps/ 目錄：先看 APPS_ROOT，再由目前檔案向上尋根。"""
    override = os.environ.get("APPS_ROOT")
    if override:
        probe = os.path.join(override, "apps")
        if os.path.isdir(probe):
            return probe
        if os.path.basename(os.path.normpath(override)) == "apps" and os.path.isdir(override):
            return override

    directory = os.path.dirname(os.path.abspath(__file__))
    while True:
        probe = os.path.join(directory, "apps")
        if os.path.isdir(probe):
            return probe
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _load_module_from_file(module_alias, file_path):
    """以檔案路徑載入模組，繞過無法以 import 陳述式表達的連字號目錄名。"""
    spec = importlib.util.spec_from_file_location(module_alias, file_path)
    if spec is None or spec.loader is None:
        raise ImportError("Unable to build an import spec for '{0}'.".format(file_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_alias] = module
    spec.loader.exec_module(module)
    return module


def _import_domain_module(module_path):
    """匯入領域模組：先試套件匯入 (含連字號變形)，再退回檔案載入。"""
    parts = module_path.split(".")
    package_dir = parts[-2]
    module_file = parts[-1]

    # 'apps.grim_reconciler.reconciler' 與 'apps.grim-reconciler.reconciler'
    # 前者需真正安裝為套件，後者對 namespace package 直接可用。
    attempts = (
        module_path,
        ".".join(parts[:-2] + [package_dir.replace("_", "-"), module_file]),
    )

    errors = []
    for candidate in attempts:
        try:
            return importlib.import_module(candidate)
        except ImportError as error:
            errors.append("{0}: {1}".format(candidate, error))

    apps_dir = _apps_directory()
    if apps_dir:
        file_path = os.path.join(apps_dir, package_dir.replace("_", "-"), module_file + ".py")
        if os.path.isfile(file_path):
            alias = "aegis_domain_" + package_dir.replace("-", "_")
            cached = sys.modules.get(alias)
            if cached is not None:
                return cached
            return _load_module_from_file(alias, file_path)

    raise ImportError(
        "Unable to resolve domain module '{0}'. Attempts -> {1}".format(
            module_path, "; ".join(errors)
        )
    )


def _resolve_domain_callable(module_path, attribute):
    """取得領域應用入口函式，例如 reconcile_transaction / inspect_cloud_event。"""
    module = _import_domain_module(module_path)
    entry_point = getattr(module, attribute, None)
    if not callable(entry_point):
        raise ImportError(
            "Domain module '{0}' does not expose callable '{1}'.".format(module_path, attribute)
        )
    return entry_point


# --------------------------------------------------------------------------- #
# 單表落庫 (Rule 2：PK/SK + ttl；PutItem 冪等，重送不產生重複列)
# --------------------------------------------------------------------------- #

def _persist(item):
    """寫入 AegisStateTable；金額欄位已為 Decimal 字串，float 一律轉 Decimal。"""
    table.put_item(Item=_decimalize(item))
    return item


def _write_reconciliation(result, payload, envelope_id):
    """對帳結果：PK=RECONCILE#LEDGER、SK=TX#<tx_id>、7 天 TTL。"""
    item = dict(result or {})
    tx_id = item.get("transaction_id") or payload.get("transaction_id") or envelope_id

    amount_source = item.get("amount")
    if amount_source is None:
        amount_source = payload.get("amount")

    item.update({
        "PK": PARTITION_RECONCILE,
        "SK": "TX#{0}".format(tx_id),
        "transaction_id": str(tx_id),
        "amount": _money_text(amount_source),
        "status": item.get("status") or "FLAGGED_DISCREPANCY",
        "reconciled_at": int(item.get("reconciled_at") or _now()),
        "created_at": _now(),
        "ttl": _now() + TTL_RECONCILE_SECONDS,
    })
    return _persist(item)


def _write_security_audit(result, payload, envelope_id):
    """資安稽核結果：PK=GUARD#SECURITY_AUDIT、SK=RESOURCE#<resource_id>、24h TTL。"""
    item = dict(result or {})
    resource_id = item.get("resource_id") or payload.get("resource_id") or envelope_id

    burn_source = item.get("estimated_hourly_burn")
    if burn_source is None:
        burn_source = payload.get("cost_per_hour")

    item.update({
        "PK": PARTITION_SECURITY_AUDIT,
        "SK": "RESOURCE#{0}".format(resource_id),
        "resource_id": str(resource_id),
        "resource_type": item.get("resource_type") or payload.get("resource_type") or "UNKNOWN",
        "estimated_hourly_burn": _money_text(burn_source),
        "circuit_breaker_triggered": bool(item.get("circuit_breaker_triggered", False)),
        "action_taken": item.get("action_taken") or "ALLOW",
        "audited_at": int(item.get("audited_at") or _now()),
        "ttl": _now() + TTL_AUDIT_SECONDS,
    })
    return _persist(item)


def _write_generic(event_type, payload, envelope_id):
    """保留原骨架行為：PK=TENANT#SYSTEM、SK=EVENT#<type>#<id>、24h TTL。"""
    item = {
        "PK": PARTITION_TENANT_SYSTEM,
        "SK": "EVENT#{0}#{1}".format(event_type, envelope_id),
        "payload": payload,
        "created_at": _now(),
        "ttl": _now() + TTL_AUDIT_SECONDS,
    }
    return _persist(item)


# --------------------------------------------------------------------------- #
# 入口 2：SQS 消費者 — 領域路由 + 單表落庫
# --------------------------------------------------------------------------- #

def _parse_record(record):
    """解析 SQS 記錄內文為信封 dict；格式錯誤直接拋錯，交由重試/DLQ 接手。"""
    body = record.get("body")
    if not body:
        raise ValueError("SQS record is missing a body payload.")
    try:
        envelope = json.loads(body)
    except ValueError as error:
        raise ValueError("SQS record body is not valid JSON: {0}".format(error)) from error
    if not isinstance(envelope, dict):
        raise ValueError("SQS record body must be a JSON object.")
    return envelope


def _route_envelope(envelope):
    """依 type 分派：reconciliation / cloud_audit / 通用事件。"""
    event_type = envelope.get("type") or DEFAULT_EVENT_TYPE
    item_id = envelope.get("id") or "evt_{0}".format(int(time.time() * 1000))

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        # 非物件 payload 仍保留可觀測性，包成 dict 後落庫
        payload = {"value": payload}

    if event_type == TYPE_RECONCILIATION:
        entry_point = _resolve_domain_callable(*DOMAIN_ROUTES[TYPE_RECONCILIATION])
        _write_reconciliation(entry_point(payload), payload, item_id)
    elif event_type == TYPE_CLOUD_AUDIT:
        entry_point = _resolve_domain_callable(*DOMAIN_ROUTES[TYPE_CLOUD_AUDIT])
        _write_security_audit(entry_point(payload), payload, item_id)
    else:
        _write_generic(event_type, payload, item_id)

    _log("event persisted", message_id=item_id, event_type=event_type)


def lambda_handler(event, context):
    """AegisSkeleton 核心處理邏輯：消費 SQS 佇列事件，路由領域應用後落庫。

    單筆失敗即整批拋錯，由 SQS 重送 (PutItem 冪等) 並在 3 次後進入
    AegisDeadLetterQueue，避免毒訊息被靜默吞掉。
    """
    records = event.get('Records') or []
    processed_count = 0

    for record in records:
        envelope = _parse_record(record)
        _route_envelope(envelope)
        processed_count += 1

    return {
        'statusCode': 200,
        'body': json.dumps({
            'message': 'Aegis ward processing completed successfully.',
            'processed': processed_count
        })
    }
