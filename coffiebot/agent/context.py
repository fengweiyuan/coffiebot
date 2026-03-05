"""
Context builder for assembling agent prompts.

【SKILLS 加载的完整执行路径】

消息进入系统：
┌─────────────────────────────────────────────┐
│ 用户消息 (Telegram/Discord/etc)            │
│ 或系统消息 (Cron/Heartbeat)                │
└──────────────┬──────────────────────────────┘
               ↓
        agent.run()

        消费消息队列 (bus.consume_inbound)
               ↓
        _dispatch(msg)
               ↓
        _process_message(msg)  ← 【SKILLS 加载开始】
               ↓
        build_messages()  ← 【核心入口】
               ↓
【═══════════════════════════════════════════】
【    ContextBuilder.build_system_prompt()    】
【═══════════════════════════════════════════】
        ↓
        1. _get_identity()
        2. _load_bootstrap_files()
        3. memory.get_memory_context()
        ↓
        4. skills.get_always_skills()  ← 【查询】
        5. skills.load_skills_for_context(always_skills)  ← 【加载完整内容】
        ↓
        6. skills.build_skills_summary()  ← 【生成摘要】
        ↓
        格式化并返回系统提示
        ↓
【═══════════════════════════════════════════】
【      返回 initial_messages                 】
【  [system prompt] + [history] + [user msg] 】
【═══════════════════════════════════════════】
               ↓
        _run_agent_loop(initial_messages)
               ↓
        while iteration < max:
            LLM call (sees system prompt + skills)
            ↓
            parse tool calls
            ↓
            execute tools (可调用 read_file 读取更多 SKILL.md)
            ↓
            feed back results
               ↓
        return response

【关键设计】
- Skills 在消息处理时被加载（lazy evaluation）
- always=true 的 skills 被完整加载
- 其他 skills 只在摘要中列出
- Agent 可通过 read_file 工具按需读取完整 SKILL.md
- 不在启动时预加载，节省 token 和启动时间
"""

import base64
import mimetypes
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from coffiebot.agent.memory import MemoryStore
from coffiebot.agent.skills import SkillsLoader


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""
    
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
    
    def build_system_prompt(self, skill_names: list[str] | None = None) -> str:
        """
        构建系统提示（System Prompt）。这是 SKILL 加载的主阵地。

        调用链：
        _process_message()
         ↓
        build_messages()
         ↓
        build_system_prompt() ← 【SKILL 加载的关键函数】
         ↓
        1. 加载身份信息
        2. 加载 bootstrap 文件
        3. 加载内存上下文
        4. 【加载 always=true 的 skills】→ get_always_skills() + load_skills_for_context()
        5. 【生成所有 skills 的摘要】→ build_skills_summary()

        后续步骤：
        - 返回的系统提示进入 initial_messages
        - initial_messages 被传给 _run_agent_loop()
        - _run_agent_loop() 调用 LLM，LLM 看到系统提示中的 skills 信息
        - Agent 可通过 read_file 工具按需读取完整的 SKILL.md
        """
        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        # 【SKILL 加载第一步】获取 always=true 的 skills 列表
        # 直接加载完整内容进上下文（如 memory skill）
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        # 【SKILL 加载第二步】生成所有 skills 的 XML 摘要
        # 列出所有可用/不可用 skills，agent 通过 read_file 按需加载完整内容
        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    async def build_system_prompt_async(
        self, query: str = "", skill_names: list[str] | None = None,
    ) -> str:
        """
        异步版本的系统提示构建。记忆段使用 OV 语义检索（若可用），其余与同步版本一致。

        Params:
            query (str): 当前用户消息，用于 OV 语义检索
            skill_names (list[str] | None): 指定加载的 skill 列表

        Returns:
            str: 组装好的系统提示文本
        """
        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        # 异步记忆检索：OV 优先，降级本地 MEMORY.md
        memory = await self.memory.get_memory_context_async(query)
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    async def build_messages_async(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        异步版本的消息列表构建。系统提示中的记忆段走 OV 语义检索。

        Params:
            history (list): 历史消息列表
            current_message (str): 当前用户消息
            skill_names (list[str] | None): 指定加载的 skill 列表
            media (list[str] | None): 附件文件路径列表
            channel (str | None): 渠道标识
            chat_id (str | None): 会话 ID

        Returns:
            list[dict[str, Any]]: 完整的消息列表
        """
        system_prompt = await self.build_system_prompt_async(
            query=current_message, skill_names=skill_names,
        )
        return [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": self._build_runtime_context(channel, chat_id)},
            {"role": "user", "content": self._build_user_content(current_message, media)},
        ]

    def _get_identity(self) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        
        return f"""# coffiebot 🐈

You are coffiebot, a helpful AI assistant.

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

## Memory
Long-term memory is managed automatically by OpenViking — it is injected into your context at the start of each conversation. Do NOT read or write any local memory files (MEMORY.md, HISTORY.md). Do NOT attempt to update memory manually. Memory is captured and updated by the system after each conversation ends.

## coffiebot Guidelines
- State intent before tool calls, but NEVER predict or claim results before receiving them.
- Before modifying a file, read it first. Do not assume files or directories exist.
- After writing or editing a file, re-read it if accuracy matters.
- If a tool call fails, analyze the error before retrying with a different approach.
- Ask for clarification when the request is ambiguous.

Reply directly with text for conversations. Only use the 'message' tool to send to a specific chat channel."""

    @staticmethod
    def _build_runtime_context(channel: str | None, chat_id: str | None) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)
    
    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []
        
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
        
        return "\n\n".join(parts) if parts else ""
    
    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        return [
            {"role": "system", "content": self.build_system_prompt(skill_names)},
            *history,
            {"role": "user", "content": self._build_runtime_context(channel, chat_id)},
            {"role": "user", "content": self._build_user_content(current_message, media)},
        ]

    @staticmethod
    def _ocr_pdf_pages(file_path: Path, max_pages: int = 10) -> str | None:
        """
        对扫描件/纯图片 PDF 执行 OCR 提取文本。

        使用 pymupdf 将 PDF 页面渲染为图片，再用 rapidocr-onnxruntime 识别文字。

        Params:
            file_path (Path): PDF 文件绝对路径
            max_pages (int): 最大处理页数（防止超大 PDF 耗时过长）

        Returns:
            str | None: OCR 提取的文本内容，失败时返回 None
        """
        try:
            import fitz  # pymupdf
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as import_error:
            logger.warning("OCR dependencies not available (pymupdf/rapidocr): {}", import_error)
            return None

        try:
            ocr_engine = RapidOCR()
            document = fitz.open(str(file_path))
            total_pages = len(document)
            process_pages = min(total_pages, max_pages)

            pages_text = []
            for page_index in range(process_pages):
                page = document[page_index]
                # 渲染为 1.5x 分辨率（平衡精度与内存，2x 在 1GB 容器中易 OOM）
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                image_bytes = pixmap.tobytes("png")
                del pixmap  # 及时释放大图内存

                # OCR 识别
                result, _ = ocr_engine(image_bytes)
                del image_bytes  # 释放 PNG 字节
                if result:
                    # result 格式: [[box, text, confidence], ...]
                    page_lines = [item[1] for item in result if item[1] and item[1].strip()]
                    if page_lines:
                        pages_text.append(
                            f"--- 第{page_index + 1}页 ---\n" + "\n".join(page_lines)
                        )

            document.close()

            if pages_text:
                total_chars = sum(len(t) for t in pages_text)
                logger.info(
                    "PDF OCR extracted: {} pages with text out of {} total, {} chars: {}",
                    len(pages_text), total_pages, total_chars, file_path.name,
                )
                suffix = ""
                if total_pages > max_pages:
                    suffix = f"\n\n(注意：该 PDF 共 {total_pages} 页，仅 OCR 了前 {max_pages} 页)"
                return "\n\n".join(pages_text) + suffix

            logger.warning("PDF OCR returned 0 text from {} pages: {}", process_pages, file_path.name)
            return None
        except Exception as error:
            logger.warning("PDF OCR failed for {}: {}", file_path.name, error)
            return None

    @staticmethod
    def _extract_pdf_text(file_path: Path) -> str | None:
        """
        从 PDF 文件提取纯文本内容。

        优先用 pypdf 提取嵌入文本（速度快），若 PDF 为扫描件/纯图片格式则
        fallback 到 OCR（pymupdf + rapidocr-onnxruntime）。

        Params:
            file_path (Path): PDF 文件绝对路径

        Returns:
            str | None: 提取的文本内容，失败时返回 None
        """
        try:
            from pypdf import PdfReader  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("pypdf not installed, cannot extract PDF text")
            return None

        try:
            reader = PdfReader(str(file_path))
            total_pages = len(reader.pages)
            pages_text = []
            for page_index, page in enumerate(reader.pages):
                page_text = page.extract_text()
                if page_text and page_text.strip():
                    pages_text.append(f"--- 第{page_index + 1}页 ---\n{page_text.strip()}")

            if pages_text:
                logger.debug(
                    "PDF text extracted: {} pages with text out of {} total, {} chars",
                    len(pages_text), total_pages,
                    sum(len(t) for t in pages_text),
                )
                return "\n\n".join(pages_text)

            # pypdf 提取不到文本 → fallback 到 OCR
            file_size_mb = file_path.stat().st_size / (1024 * 1024)
            logger.info(
                "PDF has no embedded text ({} pages, {:.1f}MB), trying OCR: {}",
                total_pages, file_size_mb, file_path.name,
            )
            ocr_result = ContextBuilder._ocr_pdf_pages(file_path)
            if ocr_result:
                return ocr_result

            # OCR 也失败，返回元信息提示
            return (
                f"(该 PDF 共 {total_pages} 页，大小 {file_size_mb:.1f}MB，"
                f"为扫描件或纯图片格式，文本提取和 OCR 均失败。"
                f"如需分析，请用户提供文字版本或截图关键页面。)"
            )
        except Exception as error:
            logger.warning("PDF extraction failed for {}: {}", file_path.name, error)
            return None

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images or extracted PDF text."""
        if not media:
            return text

        images = []
        pdf_texts = []
        file_paths: list[str] = []  # 其他文件类型：告知 LLM 真实路径供工具调用

        for path in media:
            file_path = Path(path)
            mime, _ = mimetypes.guess_type(path)

            if not file_path.is_file():
                continue

            if mime == "application/pdf" or path.lower().endswith(".pdf"):
                # PDF 文件：提取文本内容注入消息
                extracted = self._extract_pdf_text(file_path)
                if extracted:
                    pdf_texts.append(
                        f"[附件 PDF: {file_path.name}]\n\n{extracted}"
                    )
            elif mime and mime.startswith("image/"):
                # 图片文件：base64 编码
                b64 = base64.b64encode(file_path.read_bytes()).decode()
                images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            else:
                # 其他文件（ZIP、docx 等）：告知 LLM 真实磁盘路径，由 LLM 决定调用何种工具处理
                file_paths.append(f"[附件文件: {file_path.name}，路径: {file_path}]")

        # 拼接内容到用户消息
        prefix_parts = pdf_texts + file_paths
        if prefix_parts:
            text = "\n\n".join(prefix_parts) + "\n\n" + text

        if not images:
            return text
        return images + [{"type": "text", "text": text}]
    
    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: str,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages
    
    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        messages.append(msg)
        return messages
