"""
测试用例：Flask Todo 应用

覆盖场景：
- 正常场景：CRUD 完整流程
- 边界场景：空 title、超长 title、不存在 ID 的操作
- 异常场景：无 JSON body、缺失 title 字段
- 健康检查：Redis 正常/异常两种状态
"""
import pytest
import json
from unittest.mock import patch, MagicMock
from app import app


@pytest.fixture
def client():
    """创建测试客户端"""
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def mock_redis():
    """Mock Redis 连接，避免依赖真实 Redis 服务"""
    with patch('app.cache') as mock_cache:
        # 默认健康检查通过
        mock_cache.ping.return_value = True
        yield mock_cache


class TestHealthCheck:
    """健康检查接口测试"""

    def test_health_ok(self, client, mock_redis):
        """场景：Redis 正常时返回 ok"""
        mock_redis.ping.return_value = True
        response = client.get('/health')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['status'] == 'ok'
        assert data['redis'] == 'connected'

    def test_health_redis_down(self, client, mock_redis):
        """场景：Redis 异常时返回 error"""
        mock_redis.ping.side_effect = Exception("connection refused")
        response = client.get('/health')
        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['status'] == 'error'
        assert data['redis'] == 'disconnected'


class TestCreateTodo:
    """添加待办测试"""

    def test_create_todo_success(self, client, mock_redis):
        """场景：正常创建待办"""
        response = client.post(
            '/api/todos',
            data=json.dumps({'title': '学习 K8s'}),
            content_type='application/json'
        )
        assert response.status_code == 201
        data = json.loads(response.data)
        assert data['title'] == '学习 K8s'
        assert data['done'] is False
        assert 'id' in data
        assert 'created_at' in data
        # 验证 Redis 写入
        mock_redis.hset.assert_called_once()

    def test_create_todo_missing_title(self, client, mock_redis):
        """场景：缺少 title 字段返回 400"""
        response = client.post(
            '/api/todos',
            data=json.dumps({}),
            content_type='application/json'
        )
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'error' in data

    def test_create_todo_empty_title(self, client, mock_redis):
        """场景：title 为空字符串或空白"""
        response = client.post(
            '/api/todos',
            data=json.dumps({'title': '   '}),
            content_type='application/json'
        )
        assert response.status_code == 400

    def test_create_todo_no_json_body(self, client, mock_redis):
        """场景：无 JSON body 的请求"""
        response = client.post('/api/todos')
        assert response.status_code == 400

    def test_create_todo_title_too_long(self, client, mock_redis):
        """场景：title 超过 200 字符"""
        response = client.post(
            '/api/todos',
            data=json.dumps({'title': 'x' * 201}),
            content_type='application/json'
        )
        assert response.status_code == 400


class TestGetTodos:
    """获取待办列表测试"""

    def test_get_empty_list(self, client, mock_redis):
        """场景：空列表"""
        mock_redis.scan_iter.return_value = []
        response = client.get('/api/todos')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert isinstance(data, list)
        assert len(data) == 0

    def test_get_todos_with_items(self, client, mock_redis):
        """场景：有多个待办时返回列表"""
        mock_redis.scan_iter.return_value = ['todo:abc', 'todo:def']
        mock_redis.hgetall.side_effect = [
            {'id': 'abc', 'title': '任务1', 'done': 'false', 'created_at': '2026-07-01T00:00:00Z'},
            {'id': 'def', 'title': '任务2', 'done': 'true', 'created_at': '2026-07-02T00:00:00Z'},
        ]
        response = client.get('/api/todos')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert len(data) == 2
        assert data[0]['done'] is True  # 按时间倒序，后创建的在前
        assert data[1]['done'] is False


class TestUpdateTodo:
    """更新待办测试"""

    def test_update_todo_not_found(self, client, mock_redis):
        """场景：更新不存在的待办"""
        mock_redis.exists.return_value = False
        response = client.put(
            '/api/todos/nonexist',
            data=json.dumps({'title': 'new title'}),
            content_type='application/json'
        )
        assert response.status_code == 404

    def test_update_todo_success(self, client, mock_redis):
        """场景：正常更新待办"""
        mock_redis.exists.return_value = True
        mock_redis.hgetall.return_value = {
            'id': 'abc123', 'title': '更新后的任务', 'done': 'true',
            'created_at': '2026-07-01T00:00:00Z'
        }
        response = client.put(
            '/api/todos/abc123',
            data=json.dumps({'title': '更新后的任务', 'done': True}),
            content_type='application/json'
        )
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['done'] is True
        assert data['title'] == '更新后的任务'


class TestDeleteTodo:
    """删除待办测试"""

    def test_delete_todo_not_found(self, client, mock_redis):
        """场景：删除不存在的待办"""
        mock_redis.exists.return_value = False
        response = client.delete('/api/todos/nonexist')
        assert response.status_code == 404

    def test_delete_todo_success(self, client, mock_redis):
        """场景：正常删除待办"""
        mock_redis.exists.return_value = True
        response = client.delete('/api/todos/abc123')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        mock_redis.delete.assert_called_once_with('todo:abc123')