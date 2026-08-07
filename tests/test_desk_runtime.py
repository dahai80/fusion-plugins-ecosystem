"""fusion-cowork runtime 对接层测试。"""

from __future__ import annotations

import logging

import pytest

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime


# ── 显存调度 ──


def test_acquire_vram_zero_mb_returns_true() -> None:
    rt = DeskRuntime()
    assert rt.acquire_vram("p1", 0) is True


def test_acquire_vram_success_no_budget() -> None:
    rt = DeskRuntime()
    assert rt.acquire_vram("p1", 100) is True
    assert rt.vram_usage() == {"p1": 100}


def test_acquire_vram_multiple_plugins() -> None:
    rt = DeskRuntime()
    rt.acquire_vram("p1", 50)
    rt.acquire_vram("p2", 30)
    assert rt.vram_usage() == {"p1": 50, "p2": 30}


def test_acquire_vram_resize_same_plugin() -> None:
    rt = DeskRuntime()
    rt.acquire_vram("p1", 50)
    assert rt.acquire_vram("p1", 80) is True
    assert rt.vram_usage() == {"p1": 80}


def test_acquire_vram_resize_within_budget() -> None:
    rt = DeskRuntime(vram_total_mb=100)
    rt.acquire_vram("p1", 60)
    assert rt.acquire_vram("p1", 90) is True
    assert rt.vram_usage() == {"p1": 90}


def test_acquire_vram_resize_exceeds_budget() -> None:
    rt = DeskRuntime(vram_total_mb=100)
    rt.acquire_vram("p1", 60)
    assert rt.acquire_vram("p1", 120) is False
    assert rt.vram_usage() == {"p1": 60}


def test_acquire_vram_zero_releases() -> None:
    rt = DeskRuntime()
    rt.acquire_vram("p1", 50)
    assert rt.acquire_vram("p1", 0) is True
    assert rt.vram_usage() == {}


def test_acquire_vram_exceeds_budget_returns_false() -> None:
    rt = DeskRuntime(vram_total_mb=100)
    assert rt.acquire_vram("p1", 80) is True
    assert rt.acquire_vram("p2", 40) is False
    assert rt.vram_usage() == {"p1": 80}


def test_release_vram_removes_allocation() -> None:
    rt = DeskRuntime()
    rt.acquire_vram("p1", 100)
    rt.release_vram("p1")
    assert rt.vram_usage() == {}


def test_release_vram_unknown_plugin_noop() -> None:
    rt = DeskRuntime()
    rt.release_vram("unknown")  # 不应抛异常
    assert rt.vram_usage() == {}


# ── 日志采集 ──


def test_log_with_desk_logger(caplog: pytest.LogCaptureFixture) -> None:
    desk_logger = logging.getLogger("test_desk_logger")
    desk_logger.setLevel(logging.DEBUG)
    rt = DeskRuntime(desk_logger=desk_logger)
    with caplog.at_level(logging.INFO, logger="test_desk_logger"):
        rt.log("p1", "INFO", "test message", extra="data")
    assert "plugin=p1" in caplog.text
    assert "test message" in caplog.text


def test_log_falls_back_to_module_logger(caplog: pytest.LogCaptureFixture) -> None:
    rt = DeskRuntime()
    with caplog.at_level(
        logging.WARNING, logger="fusion_plugins_ecosystem.desk_runtime"
    ):
        rt.log("p1", "WARNING", "warning msg")
    assert "plugin=p1" in caplog.text
    assert "warning msg" in caplog.text


def test_log_invalid_level_defaults_to_info(caplog: pytest.LogCaptureFixture) -> None:
    rt = DeskRuntime()
    with caplog.at_level(logging.DEBUG, logger="fusion_plugins_ecosystem.desk_runtime"):
        rt.log("p1", "INVALID_LEVEL", "fallback")
    assert "fallback" in caplog.text


# ── 文件权限 ──


def test_check_file_permission_empty_allowlist_returns_true() -> None:
    rt = DeskRuntime()
    assert rt.check_file_permission("p1", "/any/path") is True


def test_check_file_permission_granted_path_returns_true() -> None:
    rt = DeskRuntime()
    rt.grant_permission("p1", ["/data/"])
    assert rt.check_file_permission("p1", "/data/file.txt") is True


def test_check_file_permission_ungranted_path_returns_false() -> None:
    rt = DeskRuntime()
    rt.grant_permission("p1", ["/data/"])
    assert rt.check_file_permission("p1", "/other/file.txt") is False


def test_grant_permission_overwrites() -> None:
    rt = DeskRuntime()
    rt.grant_permission("p1", ["/a/"])
    rt.grant_permission("p1", ["/b/"])
    assert rt.check_file_permission("p1", "/a/file") is False
    assert rt.check_file_permission("p1", "/b/file") is True


# ── API 密钥 ──


def test_get_api_key_no_config_center_returns_none() -> None:
    rt = DeskRuntime()
    assert rt.get_api_key("volcengine_claude") is None


def test_get_api_key_from_config_center() -> None:
    store: dict[str, str] = {}

    class FakeConfigCenter:
        def get(self, key: str) -> str | None:
            return store.get(key)

        def set(self, key: str, value: str) -> None:
            store[key] = value

    rt = DeskRuntime(config_center=FakeConfigCenter())
    rt.set_api_key("volcengine_claude", "sk-test-123")
    assert rt.get_api_key("volcengine_claude") == "sk-test-123"


def test_set_api_key_no_config_center_noop() -> None:
    rt = DeskRuntime()
    rt.set_api_key("provider", "key")  # 不应抛异常


def test_get_api_key_config_center_exception_returns_none() -> None:
    class BrokenConfigCenter:
        def get(self, key: str) -> str | None:
            raise RuntimeError("db down")

        def set(self, key: str, value: str) -> None:
            raise RuntimeError("db down")

    rt = DeskRuntime(config_center=BrokenConfigCenter())
    assert rt.get_api_key("any") is None
    rt.set_api_key("any", "v")  # 不应抛异常


# ── MLX 推理 ──


class _FakeMLXClient:
    def __init__(self) -> None:
        self.chat_calls: list[tuple] = []
        self.embed_calls: list[tuple] = []
        self.health_result: bool = True

    async def chat(self, model: str, messages: list, **kw) -> dict:
        self.chat_calls.append((model, messages, kw))
        return {"content": "fake-response", "model": model}

    async def embed(self, text: str, model: str = "BGE-M3") -> dict:
        self.embed_calls.append((text, model))
        return {"vector": [0.1, 0.2], "model": model}

    async def health(self) -> bool:
        return self.health_result


async def test_mlx_chat_no_client_raises() -> None:
    rt = DeskRuntime()
    with pytest.raises(RuntimeError, match="fusion-mlx 客户端未注入"):
        await rt.mlx_chat("m", [])


async def test_mlx_chat_with_client() -> None:
    fake = _FakeMLXClient()
    rt = DeskRuntime(mlx_client=fake)
    result = await rt.mlx_chat(
        "qwen3.5", [{"role": "user", "content": "hi"}], temperature=0.5
    )
    assert result["content"] == "fake-response"
    assert len(fake.chat_calls) == 1


async def test_mlx_embed_with_client() -> None:
    fake = _FakeMLXClient()
    rt = DeskRuntime(mlx_client=fake)
    result = await rt.mlx_embed("hello world")
    assert result["vector"] == [0.1, 0.2]
    assert len(fake.embed_calls) == 1


async def test_mlx_health_no_client_returns_false() -> None:
    rt = DeskRuntime()
    assert await rt.mlx_health() is False


async def test_mlx_health_with_client() -> None:
    fake = _FakeMLXClient()
    fake.health_result = True
    rt = DeskRuntime(mlx_client=fake)
    assert await rt.mlx_health() is True


# ── 节点/调度器桥 ──


class _FakeNodeRegistry:
    def __init__(self) -> None:
        self._nodes = {"file_copy": {"name": "file_copy"}}

    def list(self) -> list[dict]:
        return [{"name": "file_copy"}]

    def get(self, name: str) -> dict | None:
        return self._nodes.get(name)


def test_list_nodes_no_registry_returns_empty() -> None:
    rt = DeskRuntime()
    assert rt.list_nodes() == []


def test_list_nodes_with_registry() -> None:
    rt = DeskRuntime(node_registry=_FakeNodeRegistry())
    nodes = rt.list_nodes()
    assert len(nodes) == 1
    assert nodes[0]["name"] == "file_copy"


def test_resolve_node_no_registry_returns_none() -> None:
    rt = DeskRuntime()
    assert rt.resolve_node("file_copy") is None


def test_resolve_node_with_registry() -> None:
    rt = DeskRuntime(node_registry=_FakeNodeRegistry())
    assert rt.resolve_node("file_copy") == {"name": "file_copy"}
    assert rt.resolve_node("unknown") is None


def test_list_scheduled_tasks_no_scheduler_returns_empty() -> None:
    rt = DeskRuntime()
    assert rt.list_scheduled_tasks() == []


def test_list_scheduled_tasks_with_scheduler() -> None:
    class FakeScheduler:
        def list_tasks(self) -> list:
            return [{"id": "task_1"}]

    rt = DeskRuntime(task_scheduler=FakeScheduler())
    tasks = rt.list_scheduled_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "task_1"


def test_gateway_info_default() -> None:
    rt = DeskRuntime(mcp_gateway_port=8080)
    info = rt.gateway_info()
    assert info["port"] == 8080
    assert info["transport"] == "stdio"
    assert info["protocol_version"] == "2026-07-28"


def test_gateway_info_no_port() -> None:
    rt = DeskRuntime()
    info = rt.gateway_info()
    assert info["port"] is None
