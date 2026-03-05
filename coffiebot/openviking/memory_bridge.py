"""OpenViking 记忆桥接层，封装 recall（检索）和 capture（存储）语义。"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from coffiebot.openviking.client import OpenVikingClient


# 需要主动下探读取的记忆目录（entities 存放公司、人物等重要实体）
_ENTITY_DIRS = ("entities", "events", "preferences")
# 单次 recall 注入的最大字符数（控制 token 消耗）
_MAX_RECALL_CHARS = 6000


class MemoryBridge:
    """
    将 OpenViking 记忆能力桥接到 coffiebot 记忆系统。

    recall 策略：
    1. 先尝试 find API（语义搜索），如果返回有效结果（score 非 null）直接使用
    2. 如果 find 只返回目录索引（score=null），主动读取 entities 等目录下的记忆文件内容

    Attributes:
        client (OpenVikingClient): OpenViking HTTP 客户端
        user_id (str): OpenViking user 标识
        recall_limit (int): recall 返回的最大记忆条数
        recall_score_threshold (float): 最低相关性分数
        _available (bool | None): 服务可用性缓存（None 表示未检测）
    """

    def __init__(
        self,
        client: OpenVikingClient,
        user_id: str = "coffiebot_user",
        recall_limit: int = 5,
        recall_score_threshold: float = 0.01,
    ):
        self.client = client
        self.user_id = user_id
        self.recall_limit = recall_limit
        self.recall_score_threshold = recall_score_threshold
        self._available: bool | None = None

    async def check_available(self) -> bool:
        """
        检查 OpenViking 服务是否可用，并缓存结果。

        Returns:
            bool: 服务可用返回 True
        """
        self._available = await self.client.health()
        if self._available:
            logger.info("OpenViking connected: {}", self.client._server_url)
        else:
            logger.warning("OpenViking unavailable, no memory service")
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
        从 OpenViking 检索相关记忆，格式化为 Markdown 文本。

        策略：
        1. 调用 find API 尝试语义搜索
        2. 如果 find 返回的全是 score=null（目录索引），则主动读取记忆文件内容

        Params:
            query (str): 检索查询（通常是用户当前消息）
            limit (int | None): 返回条数上限，默认使用 self.recall_limit

        Returns:
            str: 格式化的记忆文本（Markdown），失败或无结果时返回空字符串
        """
        if not self.is_available:
            return ""

        effective_limit = limit or self.recall_limit
        try:
            result = await self.client.find(
                query=query,
                limit=effective_limit,
                score_threshold=self.recall_score_threshold,
            )
            memories = result.get("memories", [])
            resources = result.get("resources", [])
            logger.debug(
                "OpenViking find returned: {} memories, {} resources",
                len(memories), len(resources),
            )

            # 检查是否有真正的语义搜索结果（score 非 null）
            has_semantic_results = any(
                item.get("score") is not None
                for item in memories + resources
            )

            if has_semantic_results:
                # find 返回了有效的语义搜索结果，直接使用
                formatted = self._format_find_result(result)
                if formatted:
                    logger.debug("OpenViking semantic recall: {} chars", len(formatted))
                    return formatted

            # find 没有语义结果（只有目录索引），主动读取记忆文件内容
            logger.debug("OpenViking find returned no semantic results, reading entity files")
            return await self._read_entity_memories()

        except Exception as error:
            logger.warning("OpenViking recall failed: {}", error)
            self._available = False
            return ""

    async def _read_entity_memories(self) -> str:
        """
        主动读取 user memories 下 entities/events/preferences 目录中的记忆文件内容。

        遍历每个目录，读取所有 .md 文件内容，拼接后返回。

        Returns:
            str: 格式化的记忆文本，无内容时返回空字符串
        """
        base_uri = f"viking://user/{self.user_id}/memories"
        entries: list[str] = []
        total_chars = 0

        for directory_name in _ENTITY_DIRS:
            directory_uri = f"{base_uri}/{directory_name}"
            try:
                files = await self.client.list_dir(directory_uri)
            except Exception:
                logger.debug("Failed to list OV directory: {}", directory_uri)
                continue

            # 并行读取所有文件
            read_tasks = []
            for file_info in files:
                if file_info.get("isDir") or not file_info.get("uri", "").endswith(".md"):
                    continue
                read_tasks.append(self.client.read_content(file_info["uri"]))

            if not read_tasks:
                continue

            results = await asyncio.gather(*read_tasks, return_exceptions=True)
            for content in results:
                if isinstance(content, Exception) or not isinstance(content, str):
                    continue
                content = content.strip()
                if not content:
                    continue
                # 控制总字符数
                if total_chars + len(content) > _MAX_RECALL_CHARS:
                    remaining = _MAX_RECALL_CHARS - total_chars
                    if remaining > 200:
                        entries.append(content[:remaining] + "...")
                    break
                entries.append(content)
                total_chars += len(content)

            if total_chars >= _MAX_RECALL_CHARS:
                break

        if not entries:
            return ""

        formatted = "## Long-term Memory (OpenViking)\n\n" + "\n\n---\n\n".join(entries)
        logger.debug("OpenViking entity recall: {} entries, {} chars", len(entries), len(formatted))
        return formatted

    @staticmethod
    def _format_find_result(result: dict[str, Any]) -> str:
        """
        将 OpenViking find 的语义搜索结果格式化为 Markdown 文本。

        仅处理 score 非 null 的条目（真正的语义搜索结果）。

        Params:
            result (dict): OpenViking find API 返回的 result 字段

        Returns:
            str: 格式化的 Markdown 文本，无有效结果时返回空字符串
        """
        entries: list[str] = []
        index = 1

        for item in result.get("memories", []) + result.get("resources", []):
            score = item.get("score")
            if score is None:
                continue  # 跳过目录索引（非语义搜索结果）
            abstract = item.get("abstract", "").strip()
            if abstract:
                entries.append(f"{index}. [score={score:.2f}] {abstract}")
                index += 1

        if not entries:
            return ""

        return "## Long-term Memory (OpenViking)\n" + "\n".join(entries)

    # ── capture：提交对话记忆 ────────────────────────────────────────

    async def capture(self, session_key: str, messages: list[dict[str, Any]]) -> bool:
        """
        将对话消息提交到 OpenViking 进行记忆提取。

        异步执行，失败只打 warning，不影响主流程。

        Params:
            session_key (str): coffiebot 会话标识（用于日志追踪）
            messages (list[dict]): 待提交的消息列表，每条包含 role 和 content

        Returns:
            bool: 提交成功返回 True，失败返回 False
        """
        if not self.is_available:
            return False

        try:
            # 创建 OV 会话
            ov_session_id = await self.client.create_session()

            # 提交 user/assistant 消息
            submitted_count = 0
            for message in messages:
                role = message.get("role", "")
                content = message.get("content", "")
                # 只提交 user 和 assistant 的文本消息
                if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
                    continue
                # 截断过长消息，避免 OV 处理超时
                truncated_content = content[:8000] if len(content) > 8000 else content
                await self.client.add_message(ov_session_id, role, truncated_content)
                submitted_count += 1

            if submitted_count == 0:
                logger.debug("OpenViking capture skipped: no valid messages for session {}", session_key)
                return True

            # 提交触发记忆提取
            await self.client.commit_session(ov_session_id)
            logger.info(
                "OpenViking capture done: {} messages submitted for session {}",
                submitted_count, session_key,
            )
            return True
        except Exception as error:
            logger.opt(exception=True).warning("OpenViking capture failed for session {}: {}", session_key, error)
            return False

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def close(self) -> None:
        """关闭底层客户端连接。"""
        await self.client.close()
