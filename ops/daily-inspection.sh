#!/bin/bash
# ============================================================
#  K8s Ops Agent — 日常巡检脚本
#  用法: bash ops/daily-inspection.sh
#  定时: crontab -e → 0 8 * * * /path/to/ops/daily-inspection.sh
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${PROJECT_DIR}/config/threshold.conf"
DATE=$(date +%Y%m%d_%H%M%S)
REPORT_DIR="${PROJECT_DIR}/reports"
REPORT_FILE="${REPORT_DIR}/inspection-${DATE}.txt"

# 加载配置
source "$CONFIG_FILE" 2>/dev/null

mkdir -p "$REPORT_DIR"

# ---- 工具函数 ----
write() { echo "$1" >> "$REPORT_FILE"; }
alert() { echo "[ALERT] $1" >> "$REPORT_FILE"; }

# ---- 报告头 ----
write "=========================================="
write "  K8s Ops Agent — 巡检报告"
write "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
write "  集群: $(kubectl config current-context 2>/dev/null || echo 'N/A')"
write "=========================================="
write ""

# ---- 1. 节点状态 ----
write "[1] 节点状态"
NOT_READY=$(kubectl get nodes --no-headers 2>/dev/null | grep -v " Ready " | wc -l)
if [ "$NOT_READY" -gt 0 ]; then
    alert "有 ${NOT_READY} 个节点NotReady"
    kubectl get nodes --no-headers | grep -v " Ready " >> "$REPORT_FILE"
else
    write "  所有节点正常"
    kubectl get nodes --no-headers >> "$REPORT_FILE"
fi
write ""

# ---- 2. 异常Pod ----
write "[2] 异常Pod"
ABNORMAL=$(kubectl get pods --all-namespaces --no-headers 2>/dev/null | grep -v "Running" | grep -v "Completed" | grep -v "Succeeded")
if [ -z "$ABNORMAL" ]; then
    write "  所有Pod正常"
else
    echo "$ABNORMAL" >> "$REPORT_FILE"
fi
write ""

# ---- 3. 高重启Pod ----
write "[3] 高重启Pod (>${pod_restart_warn}次)"
HIGH_RESTART=$(kubectl get pods --all-namespaces --no-headers 2>/dev/null | awk -v t="$pod_restart_warn" '$5 > t {print}')
if [ -z "$HIGH_RESTART" ]; then
    write "  无高重启Pod"
else
    echo "$HIGH_RESTART" >> "$REPORT_FILE"
fi
write ""

# ---- 4. 资源使用 ----
write "[4] 资源使用"
if kubectl top nodes 2>/dev/null | head -1 | grep -q "CPU"; then
    kubectl top nodes >> "$REPORT_FILE"
    kubectl top pods --all-namespaces --sort-by=cpu 2>/dev/null | head -10 >> "$REPORT_FILE"
else
    write "  metrics-server不可用，跳过"
fi
write ""

# ---- 5. 应用健康检查 ----
write "[5] 应用健康检查"
# 检查todo-app的Service
APP_SVC=$(kubectl get svc -l app=todo-app --no-headers 2>/dev/null | head -1)
if [ -n "$APP_SVC" ]; then
    write "  todo-app Service: 存在"
else
    alert "todo-app Service不存在"
fi

# 检查Redis
REDIS_SVC=$(kubectl get svc -l app=redis --no-headers 2>/dev/null | head -1)
if [ -n "$REDIS_SVC" ]; then
    write "  Redis Service: 存在"
else
    alert "Redis Service不存在"
fi
write ""

# ---- 6. 最近Warning事件 ----
write "[6] 最近Warning事件"
kubectl get events --all-namespaces --field-selector type=Warning --sort-by='.lastTimestamp' 2>/dev/null | tail -5 >> "$REPORT_FILE"
write ""

# ---- 汇总 ----
ALERT_COUNT=$(grep -c "\[ALERT\]" "$REPORT_FILE" 2>/dev/null || echo 0)
write "=========================================="
write "  巡检完成 | 告警数: ${ALERT_COUNT}"
write "  报告: ${REPORT_FILE}"
write "=========================================="

# 输出报告
cat "$REPORT_FILE"
