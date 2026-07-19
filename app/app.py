from flask import Flask, jsonify, request, render_template
import redis
import os
import uuid
from datetime import datetime, timezone

app = Flask(__name__)

# Redis 连接配置（从环境变量读取，K8s ConfigMap/Secret 注入）
redis_host = os.environ.get('REDIS_HOST', 'localhost')
redis_port = int(os.environ.get('REDIS_PORT', 6379))
redis_password = os.environ.get('REDIS_PASSWORD', None)

cache = redis.Redis(
    host=redis_host,
    port=redis_port,
    password=redis_password,
    decode_responses=True
)

@app.route('/')
def index():
    """前端页面"""
    return render_template('index.html')

@app.route('/health')
def health():
    """健康检查"""
    try:
        cache.ping()
        return jsonify({"status": "ok", "redis": "connected"})
    except Exception:
        return jsonify({"status": "error", "redis": "disconnected"}), 500

@app.route('/api/todos', methods=['GET'])
def get_todos():
    """获取所有待办"""
    todos = []
    for key in cache.scan_iter("todo:*"):
        todo = cache.hgetall(key)
        if todo:
            todos.append({
                "id": todo.get("id"),
                "title": todo.get("title"),
                "done": todo.get("done") == "true",
                "created_at": todo.get("created_at")
            })
    # 按创建时间排序
    todos.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jsonify(todos)

@app.route('/api/todos', methods=['POST'])
def add_todo():
    """添加待办"""
    MAX_TITLE_LENGTH = 200
    data = request.get_json()
    if not data or not data.get('title'):
        return jsonify({"error": "title is required"}), 400

    title = data['title'].strip()
    if not title:
        return jsonify({"error": "title cannot be empty"}), 400
    if len(title) > MAX_TITLE_LENGTH:
        return jsonify({"error": f"title exceeds max length of {MAX_TITLE_LENGTH}"}), 400

    todo_id = str(uuid.uuid4())[:8]
    todo = {
        "id": todo_id,
        "title": title,
        "done": "false",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    cache.hset(f"todo:{todo_id}", mapping=todo)
    return jsonify(todo), 201

@app.route('/api/todos/<todo_id>', methods=['PUT'])
def update_todo(todo_id):
    """更新待办"""
    data = request.get_json()
    key = f"todo:{todo_id}"
    if not cache.exists(key):
        return jsonify({"error": "todo not found"}), 404

    if 'title' in data:
        cache.hset(key, "title", data['title'])
    if 'done' in data:
        cache.hset(key, "done", str(data['done']).lower())

    todo = cache.hgetall(key)
    return jsonify({
        "id": todo.get("id"),
        "title": todo.get("title"),
        "done": todo.get("done") == "true",
        "created_at": todo.get("created_at")
    })

@app.route('/api/todos/<todo_id>', methods=['DELETE'])
def delete_todo(todo_id):
    """删除待办"""
    key = f"todo:{todo_id}"
    if not cache.exists(key):
        return jsonify({"error": "todo not found"}), 404

    cache.delete(key)
    return jsonify({"success": True})

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=5000, debug=debug_mode)
