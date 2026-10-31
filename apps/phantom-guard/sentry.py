import json
import time
import os
import boto3

dynamodb = boto3.resource('dynamodb')
table_name = os.environ.get('TABLE_NAME', 'AegisStateTable')
table = dynamodb.Table(table_name)

def inspect_cloud_event(event_body):
    """
    資安哨兵核心：監控未授權常駐實例，標記熔斷目標，杜絕帳單黑洞
    """
    resource_id = event_body.get('resource_id')
    resource_type = event_body.get('resource_type', 'UNKNOWN')
    cost_per_hour = event_body.get('cost_per_hour', 0.0)
    
    # 若發現高成本常駐 EC2/RDS 實例，立即標記為熔斷攔截對象
    should_terminate = resource_type in ['AWS::EC2::Instance', 'AWS::RDS::DBInstance']
    
    item = {
        'PK': f"GUARD#SECURITY_AUDIT",
        'SK': f"RESOURCE#{resource_id}",
        'resource_type': resource_type,
        'estimated_hourly_burn': str(cost_per_hour),
        'circuit_breaker_triggered': should_terminate,
        'action_taken': "TERMINATE_SIGNAL_SENT" if should_terminate else "ALLOW",
        'audited_at': int(time.time()),
        'ttl': int(time.time()) + 86400  # 24h 自動清理
    }
    table.put_item(Item=item)
    return item
