"""OpenViking HTTP 客户端，封装最小 API 子集用于记忆存储与检索。"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger


class OpenVikingClient:
    """
    OpenViking 上下文数据库的轻量 HTTP 客户端。

    Attributes:
        server_url (str): OpenViking 服务地址
        api_key (str): API 认证密钥
        agent_id (str): account 标识（对应 X-OpenViking-Account 和 X-OpenViking-Agent）
        user_id (str): user 标识（对应 X-OpenViking-User）
        timeout (float): 请求超时秒数
    """

    def __init__(
        self,
        server_url: str,
        api_key: str,
        agent_id: str = "coffiebot",
        user_id: str = "coffiebot_user",
        timeout: float = 15.0,
    ):
        self._server_url = server_url.rstrip("/")
        self._api_key = api_key
        self._agent_id = agent_id
        self._user_id = user_id
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
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self._api_key,
                    "X-OpenViking-Account": self._agent_id,
                    "X-OpenViking-User": self._user_id,
                    "X-OpenViking-Agent": self._agent_id,
                },
                timeout=self._timeout,
            )
        return self._client

    async def _post(
        self, path: str, payload: dict[str, Any] | None = None, *, timeout: float | None = None,
    ) -> dict[str, Any]:
        """
        发送 POST 请求并返回 JSON 响应。连接被服务端关闭时自动重建连接重试一次。

        Params:
            path (str): API 路径，如 /api/v1/sessions
            payload (dict | None): 请求体
            timeout (float | None): 单次请求超时秒数，None 时使用客户端默认值

        Returns:
            dict[str, Any]: 解析后的 JSON 响应

        Raises:
            OpenVikingError: 请求失败或响应状态非 ok
        """
        for attempt in range(2):
            try:
                client = await self._ensure_client()
                response = await client.post(path, json=payload or {}, timeout=timeout)
                break
            except httpx.RemoteProtocolError:
                # 服务端关闭了 keep-alive 连接，重建客户端后重试
                if attempt == 0:
                    logger.debug("OpenViking connection reset, reconnecting...")
                    await self.close()
                    continue
                raise
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "ok":
            error_info = data.get("error", {})
            raise OpenVikingError(
                f"OpenViking API error: {error_info.get('code', 'UNKNOWN')} - {error_info.get('message', str(data))}"
            )
        return data

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        发送 GET 请求并返回 JSON 响应。

        Params:
            path (str): API 路径
            params (dict | None): 查询参数

        Returns:
            dict[str, Any]: 解析后的 JSON 响应

        Raises:
            OpenVikingError: 请求失败或响应状态非 ok
        """
        client = await self._ensure_client()
        response = await client.get(path, params=params)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "ok":
            error_info = data.get("error", {})
            raise OpenVikingError(
                f"OpenViking API error: {error_info.get('code', 'UNKNOWN')} - {error_info.get('message', str(data))}"
            )
        return data

    # ── 文件系统 ──────────────────────────────────────────────────────

    async def list_dir(self, uri: str) -> list[dict[str, Any]]:
        """
        列出指定 URI 目录下的文件和子目录。

        Params:
            uri (str): Viking URI，如 viking://user/.../memories/entities

        Returns:
            list[dict]: 文件/目录列表，每项包含 uri、size、isDir、abstract 等字段
        """
        data = await self._get("/api/v1/fs/ls", params={"uri": uri})
        return data.get("result", [])

    async def read_content(self, uri: str) -> str:
        """
        读取指定 URI 文件的文本内容。

        Params:
            uri (str): Viking URI，如 viking://user/.../memories/entities/mem_xxx.md

        Returns:
            str: 文件内容文本
        """
        data = await self._get("/api/v1/content/read", params={"uri": uri})
        return data.get("result", "")

    # ── 会话管理 ──────────────────────────────────────────────────────

    async def create_session(self) -> str:
        """
        创建新的 OpenViking 会话。

        Returns:
            str: 新创建的 session_id
        """
        data = await self._post("/api/v1/sessions")
        session_id = data["result"]["session_id"]
        logger.debug("OpenViking session created: {}", session_id)
        return session_id

    async def add_message(self, session_id: str, role: str, content: str) -> None:
        """
        向会话中添加一条消息。

        Params:
            session_id (str): 会话 ID
            role (str): 消息角色，"user" 或 "assistant"
            content (str): 消息内容
        """
        await self._post(
            f"/api/v1/sessions/{session_id}/messages",
            {"role": role, "content": content},
        )

    async def commit_session(self, session_id: str) -> dict[str, Any]:
        """
        提交会话，触发 OpenViking 的记忆提取流程。
        commit 涉及 embedding + LLM 提取，耗时较长，使用独立的 120 秒超时。

        Params:
            session_id (str): 会话 ID

        Returns:
            dict[str, Any]: 提交结果，包含 status 和 archived 等信息
        """
        data = await self._post(
            f"/api/v1/sessions/{session_id}/commit",
            timeout=120.0,
        )
        logger.debug("OpenViking session committed: {}", session_id)
        return data.get("result", {})

    # ── 语义搜索 ──────────────────────────────────────────────────────

    async def find(
        self,
        query: str,
        *,
        limit: int = 5,
        score_threshold: float = 0.01,
    ) -> dict[str, Any]:
        """
        语义搜索 OpenViking 记忆库。不指定 target_uri 以搜索全部范围。

        Params:
            query (str): 搜索查询文本
            limit (int): 返回结果数量上限
            score_threshold (float): 最低相关性分数阈值

        Returns:
            dict[str, Any]: 搜索结果，包含 memories、resources、skills 等字段
        """
        payload: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "score_threshold": score_threshold,
        }
        data = await self._post("/api/v1/search/find", payload)
        return data.get("result", {})

    # ── 健康检查 ──────────────────────────────────────────────────────

    async def health(self) -> bool:
        """
        检查 OpenViking 服务是否可用。

        Returns:
            bool: 服务可用返回 True，否则返回 False
        """
        try:
            client = await self._ensure_client()
            response = await client.get("/health")
            return response.json().get("status") == "ok"
        except Exception as error:
            logger.debug("OpenViking health check failed: {}", error)
            return False

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def close(self) -> None:
        """关闭底层 HTTP 客户端连接。"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


class OpenVikingError(Exception):
    """OpenViking API 调用异常。"""
