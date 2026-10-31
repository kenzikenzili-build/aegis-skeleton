import json
import os
import time
import boto3

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('TABLE_NAME', 'AegisStateTable'))

def lambda_handler(event, context):
    """
    AegisSkeleton 核心處理邏輯：
    消費 SQS 佇列事件，完成消毒後寫入 DynamoDB 並附帶 24 小時 TTL。
    """
    records = event.get('Records', [])
    processed_count = 0

    for record in records:
        body = json.loads(record.get('body', '{}'))
        item_id = body.get('id', f"evt_{int(time.time() * 1000)}")
        event_type = body.get('type', 'GENERIC_EVENT')
        payload = body.get('payload', {})

        # 寫入狀態層，設定 86400 秒 (24h) TTL 避免冷資料積累成本
        table.put_item(
            Item={
                'PK': f"TENANT#SYSTEM",
                'SK': f"EVENT#{event_type}#{item_id}",
                'payload': payload,
                'created_at': int(time.time()),
                'ttl': int(time.time()) + 86400
            }
        )
        processed_count += 1

    return {
        'statusCode': 200,
        'body': json.dumps({
            'message': 'Aegis ward processing completed successfully.',
            'processed': processed_count
        })
    }
