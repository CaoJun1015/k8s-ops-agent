FROM python:3.11-slim
RUN useradd --create-home --shell /usr/sbin/nologin appuser
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY demo_app.py .
COPY templates/ templates/
USER appuser
EXPOSE 5000
CMD ["python", "demo_app.py"]
