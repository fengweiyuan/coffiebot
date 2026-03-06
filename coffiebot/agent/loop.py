"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import weakref
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from coffiebot.agent.context import ContextBuilder
from coffiebot.agent.subagent import SubagentManager
from coffiebot.agent.tools.cron import CronTool
from coffiebot.agent.tools.filesystem import EditFileTool, ExtractArchiveTool, ListDirTool, ReadFileTool, WriteFileTool
from coffiebot.agent.tools.message import MessageTool
from coffiebot.agent.tools.registry import ToolRegistry
from coffiebot.agent.tools.shell import ExecTool
from coffiebot.agent.tools.spawn import SpawnTool
from coffiebot.agent.tools.web import WebFetchTool, WebSearchTool
from coffiebot.bus.events import InboundMessage, OutboundMessage
from coffiebot.bus.queue import MessageBus
from coffiebot.providers.base import LLMProvider
from coffiebot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from coffiebot.config.schema import ChannelsConfig, ExecToolConfig, Mem0Config, OpenVikingConfig
    from coffiebot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    【SKILL 加载的完整执行链】

    消息处理流程：
    1. agent.run() 从 bus 消费消息
    2. _dispatch(msg) → _process_message(msg)
    3. _process_message() 调用 build_messages()
    4. build_messages() 调用 build_system_prompt()
    5. build_system_prompt() 【SKILLS LOADING】：
       a) get_always_skills() → list_skills() → 扫描目录
       b) load_skills_for_context() → 读取完整 SKILL.md
       c) build_skills_summary() → 生成 XML 摘要
    6. 返回 initial_messages（包含系统提示 + skills）
    7. _run_agent_loop(initial_messages)
    8. LLM 看到系统提示中的 skills 信息
    9. LLM 可调用 read_file 工具按需读取完整 SKILL.md

    核心职责：
    1. Receives messages from the bus
    2. Builds context with history, memory, skills  ← SKILLS 在此加载
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 500

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        brave_api_key: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        openviking_config: OpenVikingConfig | None = None,
        mem0_config: Mem0Config | None = None,
        session_max_file_size_mb: int = 500,
        session_cleanup_size_mb: int = 100,
    ):
        from coffiebot.config.schema import ExecToolConfig
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._openviking_config = openviking_config
        self._mem0_config = mem0_config

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(
            workspace,
            max_file_size_mb=session_max_file_size_mb,
            cleanup_size_mb=session_cleanup_size_mb,
        )
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._memory_bridge: Any = None  # MemoryBridgeProtocol 实例
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._processing_lock = asyncio.Lock()
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool, ExtractArchiveTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from coffiebot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    async def _connect_memory(self) -> None:
        """
        连接记忆后端（openviking 或 mem0，互斥）。

        两者不能同时启用，启动时检测冲突直接报错。
        """
        ov_enabled = self._openviking_config and self._openviking_config.enabled
        m0_enabled = self._mem0_config and self._mem0_config.enabled
        if ov_enabled and m0_enabled:
            raise RuntimeError("openviking 和 mem0 不能同时启用，请在配置中只启用其中一个")
        if ov_enabled:
            await self._connect_openviking()
        elif m0_enabled:
            await self._connect_mem0()

    async def _connect_openviking(self) -> None:
        """初始化 OpenViking 记忆桥接（一次性，启动时调用）。"""
        if not self._openviking_config or not self._openviking_config.enabled:
            return
        if not self._openviking_config.server_url:
            logger.warning("OpenViking enabled but server_url is empty, skipping")
            return

        from coffiebot.openviking.client import OpenVikingClient
        from coffiebot.openviking.memory_bridge import MemoryBridge

        client = OpenVikingClient(
            server_url=self._openviking_config.server_url,
            api_key=self._openviking_config.api_key,
            agent_id=self._openviking_config.agent_id,
            user_id=self._openviking_config.user_id,
            timeout=self._openviking_config.timeout,
        )
        bridge = MemoryBridge(
            client=client,
            user_id=self._openviking_config.user_id,
            recall_limit=self._openviking_config.recall_limit,
            recall_score_threshold=self._openviking_config.recall_score_threshold,
        )
        if await bridge.check_available():
            self._memory_bridge = bridge
            self.context.memory.set_bridge(bridge)
        else:
            logger.warning("OpenViking health check failed, running without OV memory")

    async def _connect_mem0(self) -> None:
        """初始化 mem0 记忆桥接（一次性，启动时调用）。"""
        if not self._mem0_config or not self._mem0_config.enabled:
            return
        if not self._mem0_config.server_url:
            logger.warning("Mem0 enabled but server_url is empty, skipping")
            return

        from coffiebot.mem0.client import Mem0Client
        from coffiebot.mem0.memory_bridge import Mem0Bridge

        client = Mem0Client(
            server_url=self._mem0_config.server_url,
            user_id=self._mem0_config.user_id,
            agent_id=self._mem0_config.agent_id,
            timeout=self._mem0_config.timeout,
        )
        bridge = Mem0Bridge(client=client)
        if await bridge.check_available():
            self._memory_bridge = bridge
            self.context.memory.set_bridge(bridge)
        else:
            logger.warning("Mem0 health check failed, running without Mem0 memory")

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """
        运行 agent 的核心迭代循环。这是工具执行的主阵地。

        【重要】这个函数 NOT 是 skills 加载的地方。
        Skills 已在调用此函数前，通过 build_messages() 组装进 initial_messages 的系统提示中。

        时间轴：
        1. 消息到达 → _process_message()
        2. build_messages() 调用 → 【Skills 在此阶段加载】
           - load_skills_for_context(): 加载 always=true 的 skills（如 memory）
           - build_skills_summary(): 生成所有 skills 的 XML 摘要（背景知识）
           - 都组装进 initial_messages 的系统提示
        3. _run_agent_loop(initial_messages) 被调用 ← 【本函数】
           - initial_messages 中系统提示已包含 skills 信息
           - Agent 在 LLM 上下文中看到自己有哪些 skills
           - Loop 内：调用 LLM → 解析工具调用 → 【执行工具】← 本阶段
           - Agent 需要时通过 read_file 工具读取完整的 SKILL.md 学习用法

        【关键区别】
        - Skills: 教学文档，背景知识库，在 loop 前加载进系统提示
        - Tools: 可执行的函数接口，在 loop 内反复调用执行

        参数：
            initial_messages: 已包含系统提示（含 skills 摘要）+ 历史 + 运行时上下文的消息列表
            on_progress: 进度回调函数

        返回：
            (final_content, tools_used, messages)
        """
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        while iteration < self.max_iterations:
            iteration += 1

            # 第一步：调用 LLM，LLM 在 messages 中看到系统提示（包含 skills 摘要）
            # LLM 基于 skills 背景知识和工具定义做出决策
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),  # 工具定义列表（tool schema）
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            if response.has_tool_calls:
                # 【工具执行主阵地】LLM 决定调用某些工具
                if on_progress:
                    clean = self._strip_think(response.content)
                    if clean:
                        await on_progress(clean)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                # 第二步：执行工具调用
                # LLM 在同一轮返回的多个 tool calls 并行执行（它们之间无依赖）
                # 有依赖的调用 LLM 会分多轮返回，天然保证顺序
                if len(response.tool_calls) == 1:
                    # 单个 tool call，直接执行（省去 gather 开销）
                    tc = response.tool_calls[0]
                    tools_used.append(tc.name)
                    args_str = json.dumps(tc.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tc.name, args_str[:200])
                    result = await self.tools.execute(tc.name, tc.arguments)
                    messages = self.context.add_tool_result(
                        messages, tc.id, tc.name, result
                    )
                else:
                    # 多个 tool calls，并行执行
                    for tc in response.tool_calls:
                        tools_used.append(tc.name)
                        args_str = json.dumps(tc.arguments, ensure_ascii=False)
                        logger.info("Tool call: {}({})", tc.name, args_str[:200])

                    async def _exec(tc):
                        return await self.tools.execute(tc.name, tc.arguments)

                    results = await asyncio.gather(
                        *[_exec(tc) for tc in response.tool_calls],
                        return_exceptions=True,
                    )
                    # 按原始顺序将结果添加到消息中
                    for tc, result in zip(response.tool_calls, results):
                        if isinstance(result, Exception):
                            result = f"Error: {result}"
                        messages = self.context.add_tool_result(
                            messages, tc.id, tc.name, result
                        )
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages

    async def run(self) -> None:
        """
        Run the agent loop, dispatching messages as tasks to stay responsive to /stop.

        【关键特性】SKILL 的加载机制（Naive Hot Loading）
        ──────────────────────────────────────────────

        这个 while 循环是无限运行的，每次都会：
        1. 从 bus 消费一条消息
        2. 创建 task 调用 _dispatch(msg)
        3. _dispatch → _process_message → build_messages → build_system_prompt
        4. build_system_prompt 中【重新加载 SKILL】

        当前实现（Naive Hot Loading）：
        ────────────────────────────
        ✓ SKILL 不是在 gateway() 启动时加载一次
        ✓ SKILL 不是在 AgentLoop.__init__() 时加载一次
        ✓ SKILL 是在【每条消息处理时】重新扫描和加载
        ✗ 没有缓存机制（cache layer）
        ✗ 没有后台监听机制（background monitor/watcher）
        ✗ 每条消息都触发文件系统扫描和文件读取

        实现细节：
        while self._running:  ← 无限循环，持续消费消息
            msg = await bus.consume_inbound()  ← 等待消息
            task = _dispatch(msg)  ← 为每条消息创建独立 task

        每次消息到达时：
        _process_message(msg)
          ↓
        build_messages()  ← 【调用点】
          ↓
        build_system_prompt()  ← 【SKILL 重新加载】
          ↓
        list_skills()  ← 【每次都扫描文件系统】iterdir()
          ↓
        load_skill(name)  ← 【每次都读取 SKILL.md 文件】read_text()

        性能特征：
        - 优点：SKILL 修改后下一条消息立即生效，无需重启服务
        - 优点：SKILL frontmatter 变化立即反映
        - 优点：依赖检查每次都执行（检测新安装的工具）
        - 代价：每条消息都需要扫描目录（iterdir()）和读取文件（read_text()）
        - 代价：没有缓存，重复工作多
        - 代价：高并发时可能有 IO 争抢

        与最佳实践的对比：
        ────────────────
        最佳实践（Background Monitor）：
          - 后台每 1 分钟检查一次文件系统
          - 检查 mtime（修改时间）判断是否变化
          - 只有变化时才更新内存中的 SKILL 缓存
          - 消息处理时直接用内存缓存，零 IO 开销
          - 性能好，延迟低，可预测

        当前实现（Naive Hot Loading）：
          - 每条消息都扫描文件系统
          - 没有缓存判断，全量重新读取
          - 消息处理时有 IO 开销
          - 实时性好，但性能差，不可预测

        未来优化方向：
        ────────────
        可以考虑实现：
        1. 内存缓存层：skills_cache = {}
        2. 后台监听线程：每 N 秒扫描一次 mtime
        3. 缓存失效策略：mtime 变化时更新缓存
        4. 消息处理时：直接用缓存，零 IO

        用户场景：
        1. 用户在 ~/.coffiebot/skills/ 中新增一个 skill
        2. 不需要重启 gateway 服务
        3. 下一条消息到达时，自动发现新 skill（因为重新扫描）
        4. 系统提示中自动包含新 skill 的摘要
        """
        self._running = True
        await self._connect_mcp()
        await self._connect_memory()
        self.context.skills.start_background_refresh()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        async with self._processing_lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        self.context.skills.stop_background_refresh()
        # 异步关闭 memory bridge 需要在事件循环中执行
        if self._memory_bridge:
            asyncio.ensure_future(self._memory_bridge.close())
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=self.memory_window)
            # 异步构建消息（OV 语义检索 + Skills 加载）
            messages = await self.context.build_messages_async(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
            )
            # 【工具执行的主阵地】此处调用 _run_agent_loop，开始工具执行循环
            final_content, _, all_msgs = await self._run_agent_loop(messages)
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            self._fire_capture(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # 处理斜杠命令和内存整理（在调用 _run_agent_loop 前）
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            self._consolidating.add(session.key)
            try:
                async with lock:
                    snapshot = session.messages[session.last_consolidated:]
                    if snapshot:
                        temp = Session(key=session.key)
                        temp.messages = list(snapshot)
                        if not await self._consolidate_memory(temp, archive_all=True):
                            return OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="Memory archival failed, session not cleared. Please try again.",
                            )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Memory archival failed, session not cleared. Please try again.",
                )
            finally:
                self._consolidating.discard(session.key)

            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="🐈 coffiebot commands:\n/new — Start a new conversation\n/stop — Stop the current task\n/help — Show available commands")

        unconsolidated = len(session.messages) - session.last_consolidated
        if (unconsolidated >= self.memory_window and session.key not in self._consolidating):
            self._consolidating.add(session.key)
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        if await self._consolidate_memory(session):
                            # consolidation 成功后立即持久化 last_consolidated，
                            # 防止进程 crash 后丢失指针更新
                            self.sessions.save(session)
                finally:
                    self._consolidating.discard(session.key)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=self.memory_window)
        # 异步构建消息（OV 语义检索 + Skills 加载）
        initial_messages = await self.context.build_messages_async(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        # 调用核心 agent 循环
        # 此时 initial_messages 已包含：系统提示（含 skills 摘要）+ 历史 + 运行时上下文
        # _run_agent_loop() 内部不再加载 skills，而是重复执行：
        #   LLM 调用 → 解析工具调用 → 执行工具 → 反馈结果 → 循环
        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages, on_progress=on_progress or _bus_progress,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)

        # 每轮对话结束后即时 OV capture（异步，不阻塞响应）
        self._fire_capture(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=msg.metadata or {},
        )

    def _fire_capture(self, session: Session) -> None:
        """
        异步触发 OV capture，不阻塞主流程。

        每轮对话结束后调用，将当前轮的 user+assistant 消息提交到 OV。

        Params:
            session (Session): 当前会话
        """
        if not self._memory_bridge or not self._memory_bridge.is_available:
            return
        # 提取最近一轮的 user/assistant 消息（从最后一条往前找到最近的 user 消息）
        turn_messages = []
        for message in reversed(session.messages):
            role = message.get("role", "")
            if role in ("user", "assistant") and message.get("content"):
                turn_messages.append(message)
            # 遇到上一轮的 user 消息就停止
            if role == "user" and turn_messages:
                break
        if turn_messages:
            turn_messages.reverse()
            asyncio.ensure_future(
                self._memory_bridge.capture(session.key, turn_messages)
            )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = {k: v for k, v in m.items() if k != "reasoning_content"}
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    continue
                if isinstance(content, list):
                    entry["content"] = [
                        {"type": "text", "text": "[image]"} if (
                            c.get("type") == "image_url"
                            and c.get("image_url", {}).get("url", "").startswith("data:image/")
                        ) else c for c in content
                    ]
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """
        整理记忆：仅走 OV capture，不再写本地 MEMORY.md/HISTORY.md。

        Params:
            session: 待整理的会话
            archive_all (bool): 是否归档全部消息

        Returns:
            bool: capture 是否成功
        """
        return await self.context.memory.capture_to_openviking(
            session, archive_all=archive_all, memory_window=self.memory_window,
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        await self._connect_memory()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""
