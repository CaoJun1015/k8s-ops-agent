# ============================================================
#  K8s Ops Agent — Todo App Dockerfile（多阶段构建）
#  构建: docker build -t todo-app:v1 -f docker/Dockerfile .
# ============================================================

# ---- 构建阶段 ----
FROM python:3.11-slim AS builder
WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ---- 运行阶段 ----
FROM python:3.11-slim AS runner
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash appuser
WORKDIR /app
COPY --from=builder /root/.local /home/appuser/.local
COPY app/ .
ENV PATH=/home/appuser/.local/bin:$PATH
ENV FLASK_DEBUG=False
# A numeric identity lets Kubernetes enforce runAsNonRoot without having to
# inspect the image's /etc/passwd entry through the container runtime.
USER 10001:10001
EXPOSE 5000
CMD ["python", "run_gunicorn.py"]
