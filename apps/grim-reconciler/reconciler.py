import json
import time
import os
import boto3

dynamodb = boto3.resource('dynamodb')
table_name = os.environ.get('TABLE_NAME', 'AegisStateTable')
table = dynamodb.Table(table_name)

def reconcile_transaction(event_body):
    """
    對帳處理核心：比對交易狀態，避免閒置輪詢，直接單次原子寫入
    """
    tx_id = event_body.get('transaction_id')
    amount = event_body.get('amount', 0)
    status = event_body.get('status', 'PENDING')
    
    # 決定對帳結果
    reconciled_status = "SETTLED" if status == "SUCCESS" else "FLAGGED_DISCREPANCY"
    
    # 寫入狀態層，設定 7 天 (604800 秒) TTL
    item = {
        'PK': f"RECONCILE#LEDGER",
        'SK': f"TX#{tx_id}",
        'amount': str(amount),
        'status': reconciled_status,
        'reconciled_at': int(time.time()),
        'ttl': int(time.time()) + 604800
    }
    table.put_item(Item=item)
    return item
