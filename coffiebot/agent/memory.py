"""Memory system — 语义记忆存储（支持 OpenViking / Mem0 后端）。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loguru import logger

from coffiebot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from coffiebot.session.manager import Session


@runtime_checkable
class MemoryBridgeProtocol(Protocol):
    """记忆桥接协议，所有记忆后端（OpenViking / Mem0）必须实现此接口。"""

    @property
    def is_available(self) -> bool: ...
    async def check_available(self) -> bool: ...
    async def recall(self, query: str, limit: int | None = None) -> str: ...
    async def capture(self, session_key: str, messages: list[dict[str, Any]]) -> bool: ...
    async def close(self) -> None: ...


class MemoryStore:
    """语义记忆存储，recall（检索）和 capture（存储）通过可插拔的 bridge 后端执行。"""

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self._bridge: MemoryBridgeProtocol | None = None

    def set_bridge(self, bridge: MemoryBridgeProtocol | None) -> None:
        """
        设置记忆桥接实例。

        Params:
            bridge (MemoryBridgeProtocol | None): 桥接实例，None 表示禁用
        """
        self._bridge = bridge

    def set_openviking_bridge(self, bridge: Any) -> None:
        """兼容别名，等价于 set_bridge()。"""
        self.set_bridge(bridge)

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        """同步版本：读取本地 MEMORY.md（降级/CLI 兼容）。"""
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    async def get_memory_context_async(self, query: str = "") -> str:
        """
        异步版本：通过 bridge 语义检索，bridge 不可用时返回空。

        Params:
            query (str): 当前用户消息，作为语义检索的查询词

        Returns:
            str: 格式化的记忆上下文文本，bridge 不可用或无结果时返回空字符串
        """
        if self._bridge and self._bridge.is_available and query:
            result = await self._bridge.recall(query)
            if result:
                logger.debug("Memory recall: {} chars", len(result))
                return result
            logger.debug("Memory recall returned empty for query: {}", query[:80])
            return ""

        if not self._bridge or not self._bridge.is_available:
            logger.debug("Memory bridge not available, no memory context")
        return ""

    async def capture(
        self,
        session: Session,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """
        将会话消息提交到记忆后端进行记忆提取。

        Params:
            session (Session): 待提交的会话
            archive_all (bool): 是否提交全部消息（/new 时使用）
            memory_window (int): 记忆窗口大小，用于计算待提交范围

        Returns:
            bool: 提交成功返回 True，bridge 不可用或失败返回 False
        """
        if not self._bridge or not self._bridge.is_available:
            logger.debug("Memory bridge not available, skipping capture")
            return False

        if archive_all:
            messages_to_capture = session.messages
            keep_count = 0
            logger.info("Memory capture (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            messages_to_capture = session.messages[session.last_consolidated:-keep_count]
            if not messages_to_capture:
                return True
            logger.info("Memory capture: {} messages to capture, {} keep", len(messages_to_capture), keep_count)

        success = await self._bridge.capture(session.key, messages_to_capture)
        if success:
            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info("Memory capture done: last_consolidated={}", session.last_consolidated)
        return success

    async def capture_to_openviking(
        self,
        session: Session,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """兼容别名，等价于 capture()。"""
        return await self.capture(session, archive_all=archive_all, memory_window=memory_window)
