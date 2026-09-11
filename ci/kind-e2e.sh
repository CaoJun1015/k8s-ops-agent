#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="${KIND_CLUSTER_NAME:-ops-agent-e2e}"
LOCAL_PORT="${OPS_AGENT_E2E_PORT:-15000}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
BUILD_CONTEXT="$WORK_DIR/source"
PORT_FORWARD_PID=""

cleanup() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ] && kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    echo "===== kind E2E diagnostics =====" >&2
    kubectl get pods -o wide >&2 || true
    kubectl get events --sort-by=.lastTimestamp | tail -50 >&2 || true
    for selector in app=ops-agent app=ops-agent-worker app=ops-agent-execution-worker app=ops-agent-dispatcher; do
      kubectl logs -l "$selector" --all-containers --tail=80 >&2 || true
    done
    for log_file in "$WORK_DIR"/port-forward*.log; do
      if [ -f "$log_file" ]; then
        echo "===== $(basename "$log_file") =====" >&2
        tail -80 "$log_file" >&2 || true
      fi
    done
  fi
  if [ -n "$PORT_FORWARD_PID" ]; then
    kill "$PORT_FORWARD_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK_DIR"
  if [ "${KEEP_KIND_CLUSTER:-false}" != "true" ]; then
    kind delete cluster --name "$CLUSTER_NAME" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

for tool in docker kind kubectl curl python3 openssl; do
  command -v "$tool" >/dev/null || {
    echo "missing required tool: $tool" >&2
    exit 1
  }
done

# Docker/BuildKit may fail while walking ignored DrvFS files whose Windows ACLs
# cannot be translated by WSL. Copy only Git-visible project files into a native
# Linux temporary directory and use that as the build context. This also keeps
# uncommitted, non-ignored source changes available during local validation.
mkdir -p "$BUILD_CONTEXT"
(
  cd "$ROOT_DIR"
  git ls-files -co --exclude-standard -z -- app docker examples/demo-app 2>/dev/null \
    | tar --null --files-from=- --create --file=- \
    | tar --extract --file=- --directory="$BUILD_CONTEXT"
)

kind create cluster --name "$CLUSTER_NAME" --wait 120s

# Pull dependency images through the host Docker daemon, whose proxy is usable
# from WSL, then wrap them as single-platform images before loading them. Docker
# 29 can otherwise export an incomplete multi-platform manifest to kind.
while read -r source_image local_image; do
  docker pull --platform linux/amd64 "$source_image"
  printf 'FROM %s\n' "$source_image" \
    | docker build --platform linux/amd64 --provenance=false \
        -t "$local_image" -
  kind load docker-image "$local_image" --name "$CLUSTER_NAME"
done <<'IMAGES'
postgres:17-alpine postgres:e2e
redis:7-alpine redis:e2e
busybox:1.36 busybox:e2e
IMAGES

docker build -t ops-agent:e2e \
  -f "$BUILD_CONTEXT/docker/Dockerfile" "$BUILD_CONTEXT"
kind load docker-image ops-agent:e2e --name "$CLUSTER_NAME"
docker build -t demo-todo:e2e "$BUILD_CONTEXT/examples/demo-app"
kind load docker-image demo-todo:e2e --name "$CLUSTER_NAME"

DATABASE_PASSWORD="$(openssl rand -hex 24)"
ALERT_TOKEN="$(openssl rand -hex 32)"
OPERATOR_TOKEN="$(openssl rand -hex 32)"
DATABASE_URL="postgresql+psycopg://ops_agent:${DATABASE_PASSWORD}@postgres-svc:5432/ops_agent"

kubectl create secret generic postgres-secret \
  --from-literal=POSTGRES_DB=ops_agent \
  --from-literal=POSTGRES_USER=ops_agent \
  --from-literal=POSTGRES_PASSWORD="$DATABASE_PASSWORD"
kubectl create secret generic ops-agent-secret \
  --from-literal=DATABASE_URL="$DATABASE_URL" \
  --from-literal=ALERT_WEBHOOK_TOKEN="$ALERT_TOKEN" \
  --from-literal=OPERATOR_API_TOKEN="$OPERATOR_TOKEN"
kubectl create configmap ops-agent-config \
  --from-literal=REDIS_URL=redis://redis-svc:6379/0 \
  --from-literal=QUEUE_MODE=rq \
  --from-literal=EXECUTION_MODE=rq \
  --from-literal=DIAGNOSIS_PROVIDER=rules \
  --from-literal=AGENT_CORE_ENABLED=true \
  --from-literal=AGENT_REASONER_PROVIDER=rules \
  --from-literal=AUTO_REMEDIATE_ENABLED=false \
  --from-literal=ALLOWED_NAMESPACES=default \
  --from-literal=REQUIRE_OPERATOR_AUTH=true \
  --from-literal=PROMETHEUS_URL=

sed 's#image: postgres:17-alpine#image: postgres:e2e#' \
  "$ROOT_DIR/k8s/postgres.yaml" > "$WORK_DIR/postgres.yaml"
sed 's#image: redis:7-alpine#image: redis:e2e#' \
  "$ROOT_DIR/k8s/redis-deployment.yaml" > "$WORK_DIR/redis-deployment.yaml"
kubectl apply -f "$WORK_DIR/postgres.yaml"
kubectl apply -f "$WORK_DIR/redis-deployment.yaml"
kubectl apply -f "$ROOT_DIR/k8s/redis-service.yaml"
kubectl apply -f "$ROOT_DIR/k8s/rbac.yaml"
kubectl apply -f "$ROOT_DIR/k8s/app-service.yaml"
kubectl rollout status statefulset/postgres --timeout=180s
kubectl rollout status deployment/redis --timeout=180s

for manifest in migration-job app-deployment worker-deployment execution-worker-deployment dispatcher-deployment; do
  sed 's#image: ops-agent:v1#image: ops-agent:e2e#' \
    "$ROOT_DIR/k8s/${manifest}.yaml" > "$WORK_DIR/${manifest}.yaml"
done
kubectl apply -f "$WORK_DIR/migration-job.yaml"
kubectl wait --for=condition=complete job/ops-agent-db-migrate --timeout=180s
kubectl apply -f "$WORK_DIR/app-deployment.yaml"
kubectl apply -f "$WORK_DIR/worker-deployment.yaml"
kubectl apply -f "$WORK_DIR/execution-worker-deployment.yaml"
kubectl apply -f "$WORK_DIR/dispatcher-deployment.yaml"
kubectl rollout status deployment/ops-agent --timeout=180s
kubectl rollout status deployment/ops-agent-worker --timeout=180s
kubectl rollout status deployment/ops-agent-execution-worker --timeout=180s
kubectl rollout status deployment/ops-agent-dispatcher --timeout=180s

sed \
  -e 's#image: demo-todo:v1#image: demo-todo:e2e#' \
  -e 's#image: redis:7-alpine#image: redis:e2e#' \
  "$ROOT_DIR/examples/demo-app/k8s/demo-app.yaml" > "$WORK_DIR/demo-app.yaml"
kubectl apply -f "$WORK_DIR/demo-app.yaml"
kubectl rollout status deployment/demo-redis --timeout=180s
kubectl rollout status deployment/demo-todo --timeout=180s

kubectl port-forward service/ops-agent-svc "${LOCAL_PORT}:5000" \
  >"$WORK_DIR/port-forward.log" 2>&1 &
PORT_FORWARD_PID=$!
for _ in $(seq 1 60); do
  if curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/live" >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/live" >/dev/null

sed 's#image: busybox:1.36#image: busybox:e2e#' \
  "$ROOT_DIR/examples/demo-app/k8s/scenario-crashloop.yaml" \
  > "$WORK_DIR/scenario-crashloop.yaml"
kubectl apply -f "$WORK_DIR/scenario-crashloop.yaml"
for _ in $(seq 1 90); do
  POD_NAME="$(kubectl get pod -l app=demo-crashloop \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  REASON="$(kubectl get pod "$POD_NAME" \
    -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' \
    2>/dev/null || true)"
  if [ "$REASON" = "CrashLoopBackOff" ]; then
    break
  fi
  sleep 2
done
test "${REASON:-}" = "CrashLoopBackOff"

FINGERPRINT="kind-e2e-crashloop"
FIRING_PAYLOAD="$(printf '{"status":"firing","alerts":[{"status":"firing","fingerprint":"%s","labels":{"alertname":"KubePodCrashLooping","severity":"warning","namespace":"default","pod":"%s"},"annotations":{"summary":"kind demo crash loop"}}]}' "$FINGERPRINT" "$POD_NAME")"
curl --fail --silent -X POST \
  -H "Authorization: Bearer ${ALERT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$FIRING_PAYLOAD" \
  "http://127.0.0.1:${LOCAL_PORT}/api/alerts/prometheus" >/dev/null

INCIDENT_JSON="$(curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/api/incidents")"
INCIDENT_ID="$(printf '%s' "$INCIDENT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
RUN_RESPONSE="$(curl --fail --silent -X POST \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: kind-e2e-diagnosis' \
  -d '{}' \
  "http://127.0.0.1:${LOCAL_PORT}/api/incidents/${INCIDENT_ID}/agent-runs")"
RUN_ID="$(printf '%s' "$RUN_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

for _ in $(seq 1 90); do
  RUN_JSON="$(curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/api/agent-runs/${RUN_ID}")"
  if printf '%s' "$RUN_JSON" | python3 -c 'import json,sys; value=json.load(sys.stdin); assert value["status"] == "COMPLETED" and value["diagnosis"]["diagnosis_code"] == "CRASH_LOOP"' 2>/dev/null; then
    break
  fi
  sleep 2
done
printf '%s' "$RUN_JSON" | grep -q '"diagnosis_code":"CRASH_LOOP"'
EVIDENCE_JSON="$(curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/api/agent-runs/${RUN_ID}/evidence")"
printf '%s' "$EVIDENCE_JSON" | python3 -c 'import json,sys; items=json.load(sys.stdin); assert {"POD_STATUS", "PREVIOUS_LOGS", "K8S_EVENTS"}.issubset({item["evidence_type"] for item in items})'
STEPS_JSON="$(curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/api/agent-runs/${RUN_ID}/steps")"
printf '%s' "$STEPS_JSON" | python3 -c 'import json,sys; steps=json.load(sys.stdin); tools=[s["tool_invocation"]["tool_name"] for s in steps if s["tool_invocation"]]; assert len(set(tools)) >= 2; assert tools[:2] == ["get_pod_status", "get_previous_logs"]; assert steps[-1]["step_type"] == "COMPLETE"'
AUDIT_JSON="$(curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/api/audit-events?entity_type=AgentRun&entity_id=${RUN_ID}")"
printf '%s' "$AUDIT_JSON" | python3 -c 'import json,sys; events={x["event_type"] for x in json.load(sys.stdin)}; assert {"agent_step.started", "agent_tool.succeeded", "agent_run.completed"}.issubset(events)'
printf '%s' "$AUDIT_JSON" | python3 -c 'import json,sys; steps=[x["payload"] for x in json.load(sys.stdin) if x["event_type"] == "agent_step.started"]; assert steps; assert all(x["context_version"] == "agent-context-v2" for x in steps); assert any(x["selection"]["retained"] > 1 for x in steps)'
kubectl exec deployment/ops-agent -- python -c '
import os, sys
from sqlalchemy import select
from ops_agent.database import Database
from ops_agent.models import AgentStep
with Database(os.environ["DATABASE_URL"]).session_factory() as session:
    steps = list(session.scalars(select(AgentStep).where(AgentStep.agent_run_id == sys.argv[1])))
    assert steps and all(s.context_version == "agent-context-v2" for s in steps)
    assert any(s.context_snapshot["working_memory"]["confirmed"] for s in steps)
    assert all(set(s.evidence_ids or []).issubset({e.id for e in s.agent_run.evidence}) for s in steps)
print("context v2: persisted working memory and evidence references verified")
' "$RUN_ID"
test "$(kubectl auth can-i delete pods --as=system:serviceaccount:default:ops-agent-agent)" = "no"

kubectl patch configmap demo-crashloop-mode --type merge \
  -p '{"data":{"mode":"healthy"}}'
kubectl wait --for=condition=Ready "pod/${POD_NAME}" --timeout=240s
RESOLVED_PAYLOAD="$(printf '{"status":"resolved","alerts":[{"status":"resolved","fingerprint":"%s","labels":{"alertname":"KubePodCrashLooping","severity":"warning","namespace":"default","pod":"%s"},"annotations":{"summary":"kind demo recovered"}}]}' "$FINGERPRINT" "$POD_NAME")"
curl --fail --silent -X POST \
  -H "Authorization: Bearer ${ALERT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$RESOLVED_PAYLOAD" \
  "http://127.0.0.1:${LOCAL_PORT}/api/alerts/prometheus" >/dev/null

INCIDENT_JSON="$(curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/api/incidents/${INCIDENT_ID}")"
printf '%s' "$INCIDENT_JSON" | grep -q '"status":"RESOLVED"'

# Exercise the supported v0.3 -> v0.2 feature-flag rollback on the same real
# PostgreSQL/Redis/RQ stack. A new run must use the legacy collector and create
# no AgentStep timeline.
kubectl set env deployment/ops-agent deployment/ops-agent-worker \
  AGENT_CORE_ENABLED=false >/dev/null
kubectl rollout status deployment/ops-agent --timeout=180s
kubectl rollout status deployment/ops-agent-worker --timeout=180s
kill "$PORT_FORWARD_PID" 2>/dev/null || true
kubectl port-forward service/ops-agent-svc "${LOCAL_PORT}:5000" \
  >"$WORK_DIR/port-forward-legacy.log" 2>&1 &
PORT_FORWARD_PID=$!
for _ in $(seq 1 60); do
  if curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/live" >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/live" >/dev/null
LEGACY_POD_NAME="$(kubectl get pod -l app=demo-todo \
  -o jsonpath='{.items[0].metadata.name}')"
LEGACY_INCIDENT_PAYLOAD="$(printf '{"title":"feature flag rollback check","severity":"MEDIUM","fingerprint":"kind-e2e-legacy","cluster":"default","namespace":"default","resource_kind":"Pod","resource_name":"%s","source":"kind-e2e"}' "$LEGACY_POD_NAME")"
echo "checking AGENT_CORE_ENABLED=false fallback"
LEGACY_INCIDENT_JSON="$(curl --fail-with-body --show-error --silent -X POST \
  -H 'Content-Type: application/json' \
  -d "$LEGACY_INCIDENT_PAYLOAD" \
  "http://127.0.0.1:${LOCAL_PORT}/api/incidents")"
LEGACY_INCIDENT_ID="$(printf '%s' "$LEGACY_INCIDENT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
LEGACY_RUN_RESPONSE="$(curl --fail-with-body --show-error --silent -X POST \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: kind-e2e-legacy-diagnosis' \
  -d '{}' \
  "http://127.0.0.1:${LOCAL_PORT}/api/incidents/${LEGACY_INCIDENT_ID}/agent-runs")"
LEGACY_RUN_ID="$(printf '%s' "$LEGACY_RUN_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
for _ in $(seq 1 90); do
  LEGACY_RUN_JSON="$(curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/api/agent-runs/${LEGACY_RUN_ID}")"
  if printf '%s' "$LEGACY_RUN_JSON" | grep -q '"status":"COMPLETED"'; then
    break
  fi
  sleep 2
done
printf '%s' "$LEGACY_RUN_JSON" | grep -q '"status":"COMPLETED"'
test "$(curl --fail --silent "http://127.0.0.1:${LOCAL_PORT}/api/agent-runs/${LEGACY_RUN_ID}/steps")" = "[]"

# The kind database is disposable: validate PostgreSQL downgrade and re-upgrade
# only after stopping all processes that can access the schema.
kubectl scale deployment ops-agent ops-agent-worker ops-agent-execution-worker \
  ops-agent-dispatcher --replicas=0 >/dev/null
echo "checking PostgreSQL 0006 -> 0005 -> 0006 migration rollback"
sed \
  -e 's/name: ops-agent-db-migrate/name: ops-agent-db-downgrade/g' \
  -e 's/upgrade, head/downgrade, "0005"/' \
  "$WORK_DIR/migration-job.yaml" > "$WORK_DIR/migration-downgrade-job.yaml"
kubectl apply -f "$WORK_DIR/migration-downgrade-job.yaml"
kubectl wait --for=condition=complete job/ops-agent-db-downgrade --timeout=180s
sed 's/name: ops-agent-db-migrate/name: ops-agent-db-reupgrade/g' \
  "$WORK_DIR/migration-job.yaml" > "$WORK_DIR/migration-reupgrade-job.yaml"
kubectl apply -f "$WORK_DIR/migration-reupgrade-job.yaml"
kubectl wait --for=condition=complete job/ops-agent-db-reupgrade --timeout=180s
echo "kind E2E passed: Incident -> Evidence -> CRASH_LOOP -> verified RESOLVED"
