"""Mem0 REST API 客户端，封装 search / add / health 接口。"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger


class Mem0Client:
    """
    Mem0 自部署版本的轻量 HTTP 客户端。

    对接 open-source mem0 REST API（无 /v1 前缀）：
    - POST /search     — 语义搜索记忆
    - POST /memories   — 添加记忆
    - GET  /memories    — 列出记忆（兼作健康检查）

    Attributes:
        server_url (str): mem0 服务地址，如 http://localhost:9356
        user_id (str): mem0 的 user_id 标识
        agent_id (str): mem0 可选的 agent_id
        timeout (float): HTTP 请求超时秒数
    """

    def __init__(
        self,
        server_url: str,
        user_id: str = "coffiebot",
        agent_id: str = "",
        timeout: float = 15.0,
    ):
        self._server_url = server_url.rstrip("/")
        self._user_id = user_id
        self._agent_id = agent_id
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """
        确保 httpx 异步客户端已初始化。

        Returns:
            httpx.AsyncClient: 可复用的异步 HTTP 客户端
        """
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._server_url,
                headers={"Content-Type": "application/json"},
                timeout=None,  # 每个方法自行控制 timeout
            )
        return self._client

    # ── 健康检查 ──────────────────────────────────────────────────────

    async def health(self) -> bool:
        """
        检查 mem0 服务是否可用。尝试 GET /memories 确认连通性。

        Returns:
            bool: 服务可用返回 True
        """
        try:
            client = await self._ensure_client()
            params: dict[str, Any] = {"user_id": self._user_id}
            response = await client.get("/memories", params=params, timeout=self._timeout)
            response.raise_for_status()
            return True
        except Exception as error:
            logger.debug("Mem0 health check failed: {}", error)
            return False

    # ── 语义搜索 ──────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        语义搜索 mem0 记忆库。

        Params:
            query (str): 搜索查询文本
            limit (int): 返回结果数量上限

        Returns:
            list[dict]: 搜索结果列表，每项包含 id、memory、score 等字段
        """
        client = await self._ensure_client()
        payload: dict[str, Any] = {
            "query": query,
            "user_id": self._user_id,
            "limit": limit,
        }
        if self._agent_id:
            payload["agent_id"] = self._agent_id
        response = await client.post("/search", json=payload, timeout=self._timeout)
        response.raise_for_status()
        data = response.json()
        # open-source 版本返回 {"results": [...]} 或直接 [...]
        if isinstance(data, list):
            return data
        return data.get("results", [])

    # ── 添加记忆 ──────────────────────────────────────────────────────

    async def add(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """
        将对话消息提交给 mem0 进行记忆提取。

        Params:
            messages (list[dict]): 消息列表，每条包含 role 和 content

        Returns:
            dict: mem0 返回的结果
        """
        client = await self._ensure_client()
        payload: dict[str, Any] = {
            "messages": messages,
            "user_id": self._user_id,
        }
        if self._agent_id:
            payload["agent_id"] = self._agent_id
        import httpx
        response = await client.post("/memories", json=payload, timeout=httpx.Timeout(120.0))
        response.raise_for_status()
        return response.json()

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def close(self) -> None:
        """关闭底层 HTTP 客户端连接。"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
