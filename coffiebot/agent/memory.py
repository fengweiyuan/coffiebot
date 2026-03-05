"""Memory system — OpenViking 语义记忆为唯一数据源。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from coffiebot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from coffiebot.openviking.memory_bridge import MemoryBridge
    from coffiebot.session.manager import Session


class MemoryStore:
    """OpenViking 语义记忆存储，recall（检索）和 capture（存储）均走 OV。"""

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self._ov_bridge: MemoryBridge | None = None

    def set_openviking_bridge(self, bridge: MemoryBridge | None) -> None:
        """
        设置 OpenViking 记忆桥接实例。

        Params:
            bridge (MemoryBridge | None): 桥接实例，None 表示禁用
        """
        self._ov_bridge = bridge

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
        异步版本：仅走 OV 语义检索，OV 不可用时返回空。

        不再降级到本地 MEMORY.md，保证单一数据源（OpenViking）。

        Params:
            query (str): 当前用户消息，作为语义检索的查询词

        Returns:
            str: 格式化的记忆上下文文本，OV 不可用或无结果时返回空字符串
        """
        if self._ov_bridge and self._ov_bridge.is_available and query:
            ov_result = await self._ov_bridge.recall(query)
            if ov_result:
                logger.debug("Memory recall via OpenViking: {} chars", len(ov_result))
                return ov_result
            logger.debug("OpenViking recall returned empty for query: {}", query[:80])
            return ""

        if not self._ov_bridge or not self._ov_bridge.is_available:
            logger.debug("OpenViking not available, no memory context")
        return ""

    async def capture_to_openviking(
        self,
        session: Session,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """
        将会话消息提交到 OpenViking 进行记忆提取。

        不再写 MEMORY.md/HISTORY.md，仅走 OV capture。

        Params:
            session (Session): 待提交的会话
            archive_all (bool): 是否提交全部消息（/new 时使用）
            memory_window (int): 记忆窗口大小，用于计算待提交范围

        Returns:
            bool: 提交成功返回 True，OV 不可用或失败返回 False
        """
        if not self._ov_bridge or not self._ov_bridge.is_available:
            logger.debug("OpenViking not available, skipping capture")
            return False

        if archive_all:
            messages_to_capture = session.messages
            keep_count = 0
            logger.info("OV capture (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            messages_to_capture = session.messages[session.last_consolidated:-keep_count]
            if not messages_to_capture:
                return True
            logger.info("OV capture: {} messages to capture, {} keep", len(messages_to_capture), keep_count)

        success = await self._ov_bridge.capture(session.key, messages_to_capture)
        if success:
            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info("OV capture done: last_consolidated={}", session.last_consolidated)
        return success
