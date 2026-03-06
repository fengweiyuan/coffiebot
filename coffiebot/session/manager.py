"""Session management for conversation history."""

import json
import shutil
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from coffiebot.utils.helpers import ensure_dir, safe_filename


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    The consolidation process writes summaries to MEMORY.md/HISTORY.md
    but does NOT modify the messages list or get_history() output.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files
    
    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()
    
    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a user turn."""
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]

        # Drop leading non-user messages to avoid orphaned tool_result blocks
        for i, m in enumerate(sliced):
            if m.get("role") == "user":
                sliced = sliced[i:]
                break

        out: list[dict[str, Any]] = []
        for m in sliced:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in m:
                    entry[k] = m[k]
            out.append(entry)
        return out
    
    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path, max_file_size_mb: int = 500, cleanup_size_mb: int = 100):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        from coffiebot.utils.helpers import get_data_path
        self.legacy_sessions_dir = get_data_path() / "sessions"
        self._cache: dict[str, Session] = {}
        self._max_file_size_bytes = max_file_size_mb * 1024 * 1024  # 触发清理的文件大小阈值
        self._cleanup_size_bytes = cleanup_size_mb * 1024 * 1024    # 每次清理目标释放字节数
    
    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.coffiebot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"
    
    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.
        
        Args:
            key: Session key (usually channel:chat_id).
        
        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]
        
        session = self._load(key)
        if session is None:
            session = Session(key=key)
        
        self._cache[key] = session
        return session
    
    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None
    
    def _write_to_disk(self, session: Session) -> Path:
        """将 session 序列化写入 JSONL 文件，返回文件路径。"""
        path = self._get_session_path(session.key)
        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return path

    def save(self, session: Session) -> None:
        """Save a session to disk, trimming old consolidated messages if file is too large."""
        path = self._write_to_disk(session)
        self._cache[session.key] = session

        # 文件超阈值时裁剪已 consolidated 的旧消息
        if self._max_file_size_bytes > 0:
            try:
                if path.stat().st_size > self._max_file_size_bytes:
                    self._trim_consolidated(session)
            except OSError:
                pass

    def _trim_consolidated(self, session: Session) -> None:
        """
        裁剪已 consolidated 的旧消息以缩减文件大小。

        仅删除 messages[0:last_consolidated] 范围内的消息（已提交 mem0，LLM 不再需要）。
        累计序列化字节达到 cleanup_size_bytes 后停止裁剪，然后重写文件。
        """
        if session.last_consolidated <= 0:
            logger.warning(
                "Session {} file exceeds size limit but last_consolidated=0, cannot trim",
                session.key,
            )
            return

        # 逐条累计已 consolidated 消息的序列化大小，直到达到清理目标
        cumulative = 0
        trim_count = 0
        for i in range(session.last_consolidated):
            line_bytes = len(json.dumps(session.messages[i], ensure_ascii=False).encode("utf-8")) + 1  # +1 for newline
            cumulative += line_bytes
            trim_count = i + 1
            if cumulative >= self._cleanup_size_bytes:
                break

        if trim_count <= 0:
            return

        logger.info(
            "Trimming {} consolidated messages (~{:.1f}MB) from session {}",
            trim_count, cumulative / (1024 * 1024), session.key,
        )

        session.messages = session.messages[trim_count:]
        session.last_consolidated = max(0, session.last_consolidated - trim_count)
        self._write_to_disk(session)
    
    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)
    
    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.
        
        Returns:
            List of session info dicts.
        """
        sessions = []
        
        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sessions.append({
                                "key": key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue
        
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
