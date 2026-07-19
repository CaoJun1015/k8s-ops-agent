#!/bin/bash
set -euo pipefail
# ============================================================
#  K8s Ops Agent — AI日志分析
#  用法: bash agent/analyze.sh [pod名] [命名空间]
#  功能: 调用LLM分析日志，给出根因和修复建议
# ============================================================

POD_NAME="${1:-todo-app}"
NAMESPACE="${2:-default}"
LINES="${3:-50}"

echo "=========================================="
echo "  AI日志分析: ${NAMESPACE}/${POD_NAME}"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 获取日志
LOGS=$(kubectl logs -n "$NAMESPACE" "$POD_NAME" --tail="$LINES" 2>/dev/null)

if [ -z "$LOGS" ]; then
    echo "[ERROR] 无法获取日志"
    exit 1
fi

# 提取ERROR和WARN
ERRORS=$(echo "$LOGS" | grep -iE "\[ERROR\]|exception|failed|timeout" | tail -10)
WARNINGS=$(echo "$LOGS" | grep -iE "\[WARN\]|warning" | tail -5)

echo ""
echo "[1] 日志摘要"
echo "  总行数: $(echo "$LOGS" | wc -l)"
echo "  ERROR数: $(echo "$LOGS" | grep -ciE "\[ERROR\]|exception|failed" || echo 0)"
echo "  WARN数: $(echo "$LOGS" | grep -ciE "\[WARN\]|warning" || echo 0)"

echo ""
echo "[2] 错误详情"
if [ -n "$ERRORS" ]; then
    echo "$ERRORS"
else
    echo "  无错误日志"
fi

echo ""
echo "[3] AI分析"

# 构造prompt
PROMPT="你是一个K8s运维专家。请分析以下应用日志，给出：
1. 问题根因（用一句话总结）
2. 严重程度（低/中/高/紧急）
3. 修复建议（具体可执行的步骤）

应用: ${POD_NAME}
命名空间: ${NAMESPACE}

错误日志:
${ERRORS:-无错误}

警告日志:
${WARNINGS:-无警告}

请用中文回答，格式简洁。"

# 检查是否有LLM可用
if command -v openai &>/dev/null; then
    # 使用OpenAI CLI
    echo "$PROMPT" | openai api chat.completions.create -m gpt-4 -g 2>/dev/null

elif [ -n "$OPENAI_API_KEY" ]; then
    # 检查 jq 依赖
    if ! command -v jq &>/dev/null; then
        echo "  [ERROR] jq 未安装，请执行: apt-get install jq 或 brew install jq"
        exit 1
    fi
    # 使用curl调用OpenAI API
    RESPONSE=$(curl -s https://api.openai.com/v1/chat/completions \
        -H "Authorization: Bearer $OPENAI_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{
            \"model\": \"gpt-4\",
            \"messages\": [{\"role\": \"user\", \"content\": $(echo "$PROMPT" | jq -Rs .)}],
            \"max_tokens\": 500
        }" 2>/dev/null)

    echo "$RESPONSE" | jq -r '.choices[0].message.content' 2>/dev/null || echo "  API调用失败"

elif [ -n "$ANTHROPIC_API_KEY" ]; then
    # 检查 jq 依赖
    if ! command -v jq &>/dev/null; then
        echo "  [ERROR] jq 未安装，请执行: apt-get install jq 或 brew install jq"
        exit 1
    fi
    # 使用curl调用Claude API
    RESPONSE=$(curl -s https://api.anthropic.com/v1/messages \
        -H "x-api-key: $ANTHROPIC_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{
            \"model\": \"claude-sonnet-4-20250514\",
            \"max_tokens\": 500,
            \"messages\": [{\"role\": \"user\", \"content\": $(echo "$PROMPT" | jq -Rs .)}]
        }" 2>/dev/null)

    echo "$RESPONSE" | jq -r '.content[0].text' 2>/dev/null || echo "  API调用失败"

else
    echo "  [跳过] 未配置LLM API密钥"
    echo "  设置环境变量: export OPENAI_API_KEY=sk-xxx"
    echo "  或: export ANTHROPIC_API_KEY=sk-ant-xxx"
    echo ""
    echo "  [人工分析建议]"
    if [ -n "$ERRORS" ]; then
        echo "  - 检查错误日志中的关键词（timeout/refused/denied）"
        echo "  - 检查对应服务是否正常运行"
        echo "  - 检查资源使用是否超限"
    else
        echo "  - 日志无明显错误，检查网络和服务发现"
    fi
fi

echo ""
echo "=========================================="
echo "  分析完成"
echo "=========================================="
