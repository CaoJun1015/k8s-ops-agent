#!/bin/bash
set -euo pipefail
# ============================================================
#  K8s Ops Agent — 自然语言巡检报告
#  用法: bash agent/report.sh
#  功能: 采集数据 + 生成可读的巡检报告
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DATE=$(date '+%Y-%m-%d %H:%M:%S')

# ---- 采集数据 ----
NODES=$(kubectl get nodes --no-headers 2>/dev/null)
PODS=$(kubectl get pods --all-namespaces --no-headers 2>/dev/null)
SERVICES=$(kubectl get svc --all-namespaces --no-headers 2>/dev/null)
EVENTS=$(kubectl get events --all-namespaces --field-selector type=Warning --sort-by='.lastTimestamp' 2>/dev/null | tail -5)

# 统计
TOTAL_PODS=$(echo "$PODS" | wc -l)
RUNNING_PODS=$(echo "$PODS" | grep "Running" | wc -l)
ABNORMAL_PODS=$(echo "$PODS" | grep -v "Running" | grep -v "Completed" | grep -v "Succeeded" | wc -l)
NODE_COUNT=$(echo "$NODES" | wc -l)
NOT_READY=$(echo "$NODES" | grep -v " Ready " | wc -l)

# ---- 生成报告 ----
cat << EOF
==========================================
  K8s Ops Agent — 巡检报告
  时间: ${DATE}
  集群: $(kubectl config current-context 2>/dev/null || echo 'N/A')
==========================================

集群概况:
  节点总数: ${NODE_COUNT}
  节点异常: ${NOT_READY}
  Pod总数: ${TOTAL_PODS}
  Pod运行中: ${RUNNING_PODS}
  Pod异常: ${ABNORMAL_PODS}

EOF

# 节点详情
echo "节点状态:"
echo "$NODES" | while read LINE; do
    STATUS=$(echo "$LINE" | awk '{print $2}')
    NODE=$(echo "$LINE" | awk '{print $1}')
    if [ "$STATUS" = "Ready" ]; then
        echo "  ✅ ${NODE}: ${STATUS}"
    else
        echo "  ❌ ${NODE}: ${STATUS}"
    fi
done

echo ""

# 异常Pod
if [ "$ABNORMAL_PODS" -gt 0 ]; then
    echo "异常Pod:"
    echo "$PODS" | grep -v "Running" | grep -v "Completed" | grep -v "Succeeded" | while read LINE; do
        NS=$(echo "$LINE" | awk '{print $1}')
        POD=$(echo "$LINE" | awk '{print $2}')
        STATUS=$(echo "$LINE" | awk '{print $4}')
        echo "  ❌ ${NS}/${POD}: ${STATUS}"
    done
else
    echo "所有Pod运行正常 ✅"
fi

echo ""

# Warning事件
if [ -n "$EVENTS" ]; then
    echo "最近Warning事件:"
    echo "$EVENTS" | while read LINE; do
        echo "  ⚠️  ${LINE}"
    done
else
    echo "无Warning事件 ✅"
fi

echo ""
echo "=========================================="
echo "  报告生成完成"
echo "=========================================="
