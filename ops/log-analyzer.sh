#!/bin/bash
set -euo pipefail
# ============================================================
#  K8s Ops Agent — 日志分析脚本
#  用法: bash ops/log-analyzer.sh [pod名] [命名空间]
#  示例: bash ops/log-analyzer.sh todo-app default
# ============================================================

POD_NAME="${1:-todo-app}"
NAMESPACE="${2:-default}"
LINES="${3:-100}"

echo "=========================================="
echo "  日志分析: ${NAMESPACE}/${POD_NAME}"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 获取最近日志
LOGS=$(kubectl logs -n "$NAMESPACE" "$POD_NAME" --tail="$LINES" 2>/dev/null)

if [ -z "$LOGS" ]; then
    echo "[ERROR] 无法获取日志，检查Pod是否存在"
    kubectl get pod -n "$NAMESPACE" "$POD_NAME" 2>/dev/null
    exit 1
fi

# ---- 日志级别统计 ----
echo ""
echo "[1] 日志级别统计"
echo "$LOGS" | grep -oE "\[(INFO|WARN|ERROR|DEBUG)\]" | sort | uniq -c | sort -rn

# ---- ERROR详情 ----
ERROR_COUNT=$(echo "$LOGS" | grep -c "\[ERROR\]" 2>/dev/null)
ERROR_COUNT=${ERROR_COUNT:-0}
echo ""
echo "[2] ERROR日志 (${ERROR_COUNT}条)"
if [ "$ERROR_COUNT" -gt 0 ]; then
    echo "$LOGS" | grep "\[ERROR\]" | tail -10
fi

# ---- 最近异常 ----
echo ""
echo "[3] 最近异常关键词"
echo "$LOGS" | grep -iE "exception|error|failed|timeout|refused|denied" | tail -5

# ---- 请求统计（如果有HTTP日志）----
echo ""
echo "[4] HTTP状态码统计"
echo "$LOGS" | grep -oE "HTTP/[0-9.]+\" [0-9]+" | awk '{print $2}' | sort | uniq -c | sort -rn | head -5

echo ""
echo "=========================================="
echo "  分析完成"
echo "=========================================="
