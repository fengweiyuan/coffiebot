"""Mem0 记忆桥接层，实现与 OpenViking MemoryBridge 相同的协议接口。"""

from __future__ import annotations

from typing import Any

from loguru import logger

from coffiebot.mem0.client import Mem0Client


# 单次 recall 注入的最大字符数（控制 token 消耗）
_MAX_RECALL_CHARS = 6000


class Mem0Bridge:
    """
    将 mem0 记忆能力桥接到 coffiebot 记忆系统。

    实现与 OpenViking MemoryBridge 相同的协议：
    - check_available() → 健康检查
    - is_available → 属性
    - recall(query) → 语义检索，返回格式化 Markdown
    - capture(session_key, messages) → 提交对话记忆

    Attributes:
        client (Mem0Client): mem0 HTTP 客户端
        _available (bool | None): 服务可用性缓存
    """

    def __init__(self, client: Mem0Client):
        self.client = client
        self._available: bool | None = None

    async def check_available(self) -> bool:
        """
        检查 mem0 服务是否可用，并缓存结果。

        Returns:
            bool: 服务可用返回 True
        """
        self._available = await self.client.health()
        if self._available:
            logger.info("Mem0 connected: {}", self.client._server_url)
        else:
            logger.warning("Mem0 unavailable, no memory service")
        return self._available

    @property
    def is_available(self) -> bool:
        """
        返回服务可用性状态。

        Returns:
            bool: 服务可用返回 True，未检测或不可用返回 False
        """
        return self._available is True

    # ── recall：检索记忆 ─────────────────────────────────────────────

    async def recall(self, query: str, limit: int | None = None) -> str:
        """
        从 mem0 检索相关记忆，格式化为 Markdown 文本。

        Params:
            query (str): 检索查询（通常是用户当前消息）
            limit (int | None): 返回条数上限，默认 10

        Returns:
            str: 格式化的记忆文本（Markdown），失败或无结果时返回空字符串
        """
        if not self.is_available:
            return ""

        try:
            results = await self.client.search(query, limit=limit or 10)
            if not results:
                return ""
            return self._format_results(results)
        except Exception as error:
            logger.warning("Mem0 recall failed: {}", error)
            # 单次 recall 失败不永久禁用，连接异常（如网络断开）才标记不可用
            if isinstance(error, (OSError, ConnectionError)):
                self._available = False
            return ""

    @staticmethod
    def _format_results(results: list[dict[str, Any]]) -> str:
        """
        将 mem0 搜索结果格式化为 Markdown 文本。

        Params:
            results (list[dict]): mem0 搜索返回的结果列表

        Returns:
            str: 格式化的 Markdown 文本
        """
        entries: list[str] = []
        total_chars = 0

        for i, item in enumerate(results, 1):
            memory_text = item.get("memory", "").strip()
            if not memory_text:
                continue
            score = item.get("score")
            if score is not None:
                entry = f"{i}. [score={score:.2f}] {memory_text}"
            else:
                entry = f"{i}. {memory_text}"

            if total_chars + len(entry) > _MAX_RECALL_CHARS:
                remaining = _MAX_RECALL_CHARS - total_chars
                if remaining > 100:
                    entries.append(entry[:remaining] + "...")
                break
            entries.append(entry)
            total_chars += len(entry)

        if not entries:
            return ""
        return "## Long-term Memory (Mem0)\n" + "\n".join(entries)

    # ── capture：提交对话记忆 ────────────────────────────────────────

    async def capture(self, session_key: str, messages: list[dict[str, Any]]) -> bool:
        """
        将对话消息提交到 mem0 进行记忆提取。

        Params:
            session_key (str): coffiebot 会话标识（用于日志追踪）
            messages (list[dict]): 待提交的消息列表

        Returns:
            bool: 提交成功返回 True，失败返回 False
        """
        if not self.is_available:
            return False

        try:
            # 过滤出 user/assistant 文本消息
            filtered: list[dict[str, str]] = []
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
                    continue
                # 截断过长消息
                truncated = content[:8000] if len(content) > 8000 else content
                filtered.append({"role": role, "content": truncated})

            if not filtered:
                logger.debug("Mem0 capture skipped: no valid messages for session {}", session_key)
                return True

            await self.client.add(filtered)
            logger.info("Mem0 capture done: {} messages submitted for session {}", len(filtered), session_key)
            return True
        except Exception as error:
            logger.opt(exception=True).warning("Mem0 capture failed for session {}: {}", session_key, error)
            return False

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def close(self) -> None:
        """关闭底层客户端连接。"""
        await self.client.close()
