#!/bin/bash
set -euo pipefail
# ============================================================
#  K8s Ops Agent — 故障自愈脚本
#  用法: bash ops/auto-fix.sh [--execute]
#  默认: 干运行模式（仅检查不修复），加 --execute 执行实际修复
#  功能: 自动检测并修复常见K8s故障
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${PROJECT_DIR}/config/threshold.conf"
LOG_FILE="${PROJECT_DIR}/reports/autofix-$(date +%Y%m%d).log"

source "$CONFIG_FILE" 2>/dev/null
mkdir -p "$(dirname "$LOG_FILE")"

log() { echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOG_FILE"; }

# 默认干运行模式，需显式指定 --execute 才执行实际修复
DRY_RUN=true
if [ "${1:-}" = "--execute" ]; then
    DRY_RUN=false
    log "运行模式: 执行修复"
else
    log "运行模式: 干运行（仅检查不修复），加 --execute 参数执行实际修复"
fi

log "========== 故障自愈检查开始 =========="

FIXED=0

# ---- 1. 重启CrashLoopBackOff的Pod ----
log "[1] 检查CrashLoopBackOff Pod"
CRASH_PODS=$(kubectl get pods --all-namespaces --no-headers 2>/dev/null | grep "CrashLoopBackOff")
if [ -n "$CRASH_PODS" ]; then
    while read -r NS POD REST; do
        [ -z "$NS" ] && continue
        if [ "$DRY_RUN" = true ]; then
            log "  [DRY-RUN] 将删除: ${NS}/${POD}"
        else
            log "  发现异常: ${NS}/${POD}，尝试删除重建"
            kubectl delete pod -n "$NS" "$POD" 2>/dev/null
        fi
        FIXED=$((FIXED + 1))
    done <<< "$CRASH_PODS"
else
    log "  无CrashLoopBackOff Pod"
fi

# ---- 2. 重启ImagePullBackOff的Pod ----
log "[2] 检查ImagePullBackOff Pod"
IMAGE_PODS=$(kubectl get pods --all-namespaces --no-headers 2>/dev/null | grep "ImagePullBackOff")
if [ -n "$IMAGE_PODS" ]; then
    echo "$IMAGE_PODS" | while read NS POD REST; do
        log "  镜像拉取失败: ${NS}/${POD}（需要手动检查镜像配置）"
    done
else
    log "  无ImagePullBackOff Pod"
fi

# ---- 3. 清理Evicted Pod ----
log "[3] 清理Evicted Pod"
EVICTED=$(kubectl get pods --all-namespaces --no-headers 2>/dev/null | grep "Evicted" | wc -l)
if [ "$EVICTED" -gt 0 ]; then
    log "  发现 ${EVICTED} 个Evicted Pod，清理中"
    kubectl get pods --all-namespaces --no-headers | grep "Evicted" | while read NS POD REST; do
        if [ "$DRY_RUN" = true ]; then
            log "  [DRY-RUN] 将删除: ${NS}/${POD}"
        else
            kubectl delete pod -n "$NS" "$POD" 2>/dev/null
        fi
    done
    FIXED=$((FIXED + EVICTED))
else
    log "  无Evicted Pod"
fi

# ---- 4. 检查PVC容量 ----
log "[4] 检查PVC状态"
PVC_PENDING=$(kubectl get pvc --all-namespaces --no-headers 2>/dev/null | grep "Pending" | wc -l)
if [ "$PVC_PENDING" -gt 0 ]; then
    log "  有 ${PVC_PENDING} 个PVC处于Pending状态（需要手动检查存储）"
else
    log "  PVC状态正常"
fi

# ---- 5. 检查节点资源 ----
log "[5] 检查节点资源"
NOT_READY=$(kubectl get nodes --no-headers 2>/dev/null | grep -v " Ready " | wc -l)
if [ "$NOT_READY" -gt 0 ]; then
    log "  有 ${NOT_READY} 个节点NotReady（需要手动排查）"
else
    log "  所有节点正常"
fi

# ---- 汇总 ----
log "========== 故障自愈检查完成 =========="
log "修复数量: ${FIXED}"

if [ "$FIXED" -gt 0 ]; then
    log "已自动修复 ${FIXED} 个问题，建议检查应用是否恢复正常"
fi
