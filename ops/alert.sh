#!/bin/bash
set -euo pipefail
# ============================================================
#  K8s Ops Agent — 告警通知脚本
#  用法: bash ops/alert.sh "告警内容"
#  集成: 飞书/钉钉/企业微信 Webhook
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${PROJECT_DIR}/config/threshold.conf"

source "$CONFIG_FILE" 2>/dev/null

ALERT_MSG="${1:-未知告警}"
ALERT_LEVEL="${2:-warn}"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
HOSTNAME=$(hostname 2>/dev/null || echo "unknown")

# ---- 构造告警消息 ----
MESSAGE="[${ALERT_LEVEL^^}] ${TIMESTAMP} | 主机: ${HOSTNAME} | 详情: ${ALERT_MSG}"

echo "=========================================="
echo "  告警通知"
echo "=========================================="
echo -e "$MESSAGE"
echo ""

# ---- 发送到Webhook ----
if [ -z "$webhook_url" ]; then
    echo "[INFO] 未配置Webhook地址，仅输出到终端"
    echo "[INFO] 编辑 config/threshold.conf 设置 webhook_url"
else
    # 自动检测Webhook类型
    if echo "$webhook_url" | grep -q "feishu"; then
        # 飞书
        curl -s -X POST "$webhook_url" \
            -H 'Content-Type: application/json' \
            -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"${MESSAGE}\"}}" \
            >/dev/null 2>&1 && echo "[OK] 飞书通知已发送" || echo "[ERROR] 飞书通知发送失败"

    elif echo "$webhook_url" | grep -q "dingtalk\|oapi.dingtalk"; then
        # 钉钉
        curl -s -X POST "$webhook_url" \
            -H 'Content-Type: application/json' \
            -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"${MESSAGE}\"}}" \
            >/dev/null 2>&1 && echo "[OK] 钉钉通知已发送" || echo "[ERROR] 钉钉通知发送失败"

    elif echo "$webhook_url" | grep -q "qyapi.weixin\|wechat"; then
        # 企业微信
        curl -s -X POST "$webhook_url" \
            -H 'Content-Type: application/json' \
            -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"${MESSAGE}\"}}" \
            >/dev/null 2>&1 && echo "[OK] 企业微信通知已发送" || echo "[ERROR] 企业微信通知发送失败"

    else
        # 通用Webhook
        curl -s -X POST "$webhook_url" \
            -H 'Content-Type: application/json' \
            -d "{\"message\":\"${MESSAGE}\"}" \
            >/dev/null 2>&1 && echo "[OK] 通知已发送" || echo "[ERROR] 通知发送失败"
    fi
fi

echo "=========================================="
