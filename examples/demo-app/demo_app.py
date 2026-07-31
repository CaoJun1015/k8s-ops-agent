"""Todo demo workload used for Ops Agent end-to-end fault exercises."""

import os
import uuid
from datetime import datetime, timezone

import redis
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
cache = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=int(os.environ.get("REDIS_PORT", 6379)),
    password=os.environ.get("REDIS_PASSWORD") or None,
    decode_responses=True,
)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    try:
        cache.ping()
        return jsonify({"status": "ok", "redis": "connected"})
    except Exception:
        return jsonify({"status": "error", "redis": "disconnected"}), 500


@app.get("/api/todos")
def get_todos():
    todos = []
    for key in cache.scan_iter("todo:*"):
        todo = cache.hgetall(key)
        if todo:
            todos.append(
                {
                    "id": todo.get("id"),
                    "title": todo.get("title"),
                    "done": todo.get("done") == "true",
                    "created_at": todo.get("created_at"),
                }
            )
    todos.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return jsonify(todos)


@app.post("/api/todos")
def add_todo():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    if len(title) > 200:
        return jsonify({"error": "title exceeds max length of 200"}), 400
    todo_id = str(uuid.uuid4())[:8]
    todo = {
        "id": todo_id,
        "title": title,
        "done": "false",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    cache.hset(f"todo:{todo_id}", mapping=todo)
    return jsonify({**todo, "done": False}), 201


@app.put("/api/todos/<todo_id>")
def update_todo(todo_id):
    key = f"todo:{todo_id}"
    if not cache.exists(key):
        return jsonify({"error": "todo not found"}), 404
    data = request.get_json(silent=True) or {}
    if "title" in data:
        title = str(data["title"]).strip()
        if not title or len(title) > 200:
            return jsonify({"error": "invalid title"}), 400
        cache.hset(key, "title", title)
    if "done" in data:
        cache.hset(key, "done", str(bool(data["done"])).lower())
    todo = cache.hgetall(key)
    return jsonify(
        {
            "id": todo.get("id"),
            "title": todo.get("title"),
            "done": todo.get("done") == "true",
            "created_at": todo.get("created_at"),
        }
    )


@app.delete("/api/todos/<todo_id>")
def delete_todo(todo_id):
    key = f"todo:{todo_id}"
    if not cache.exists(key):
        return jsonify({"error": "todo not found"}), 404
    cache.delete(key)
    return jsonify({"success": True})


@app.get("/api/fault")
def fault():
    """Return a controlled 500 when enabled for alerting exercises."""
    if os.environ.get("DEMO_HTTP_500", "false").lower() == "true":
        return jsonify({"error": "injected demo failure"}), 500
    return jsonify({"status": "fault injection disabled"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
