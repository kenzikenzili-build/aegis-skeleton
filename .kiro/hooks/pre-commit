#!/bin/bash
echo "🛡️  Aegis Smite: Scanning for Mirage Phantoms..."
FORBIDDEN_PATTERNS=("localStorage" "sessionStorage" "IndexedDB")

for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
    if git diff --cached -S "$pattern" --quiet --exit-code; then
        : # 通過
    else
        echo "❌ [聖騎士淨化攔截] 檢測到虛假持久化層: $pattern"
        echo "⚠️  AegisSkeleton 已配置 DynamoDB，請使用標準 Serverless 連線池，禁止使用前端本地存儲偽造測試！"
        exit 1
    fi
done

echo "✅ 結界安全，允許提交。"
exit 0
