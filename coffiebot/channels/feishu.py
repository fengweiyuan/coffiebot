"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

import asyncio
import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from loguru import logger

from coffiebot.bus.events import OutboundMessage
from coffiebot.bus.queue import MessageBus
from coffiebot.channels.base import BaseChannel
from coffiebot.config.schema import FeishuConfig
from coffiebot.media import MediaCache

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        Emoji,
        GetFileRequest,
        GetMessageRequest,
        GetMessageResourceRequest,
        P2ImMessageReceiveV1,
    )
    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def _extract_share_card_content(content_json: dict, msg_type: str) -> str:
    """Extract text representation from share cards and interactive messages."""
    parts = []

    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")

    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: dict) -> list[str]:
    """Recursively extract text and links from interactive card content."""
    parts = []
    
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    if "title" in content:
        title = content["title"]
        if isinstance(title, dict):
            title_content = title.get("content", "") or title.get("text", "")
            if title_content:
                parts.append(f"title: {title_content}")
        elif isinstance(title, str):
            parts.append(f"title: {title}")

    for elements in content.get("elements", []) if isinstance(content.get("elements"), list) else []:
        for element in elements:
            parts.extend(_extract_element_content(element))

    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))

    header = content.get("header", {})
    if header:
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")
    
    return parts


def _extract_element_content(element: dict) -> list[str]:
    """Extract content from a single card element."""
    parts = []
    
    if not isinstance(element, dict):
        return parts
    
    tag = element.get("tag", "")
    
    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in element.get("fields", []):
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    c = field_text.get("content", "")
                    if c:
                        parts.append(c)

    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)

    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            c = text.get("content", "")
            if c:
                parts.append(c)
        url = element.get("url", "") or element.get("multi_url", {}).get("url", "")
        if url:
            parts.append(f"link: {url}")

    elif tag == "img":
        alt = element.get("alt", {})
        parts.append(alt.get("content", "[image]") if isinstance(alt, dict) else "[image]")

    elif tag == "note":
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    elif tag == "column_set":
        for col in element.get("columns", []):
            for ce in col.get("elements", []):
                parts.extend(_extract_element_content(ce))

    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)

    else:
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))
    
    return parts


def _extract_post_content(content_json: dict) -> tuple[str, list[str]]:
    """Extract text and image keys from Feishu post (rich text) message content.
    
    Supports two formats:
    1. Direct format: {"title": "...", "content": [...]}
    2. Localized format: {"zh_cn": {"title": "...", "content": [...]}}
    
    Returns:
        (text, image_keys) - extracted text and list of image keys
    """
    def extract_from_lang(lang_content: dict) -> tuple[str | None, list[str]]:
        if not isinstance(lang_content, dict):
            return None, []
        title = lang_content.get("title", "")
        content_blocks = lang_content.get("content", [])
        if not isinstance(content_blocks, list):
            return None, []
        text_parts = []
        image_keys = []
        if title:
            text_parts.append(title)
        for block in content_blocks:
            if not isinstance(block, list):
                continue
            for element in block:
                if isinstance(element, dict):
                    tag = element.get("tag")
                    if tag == "text":
                        text_parts.append(element.get("text", ""))
                    elif tag == "a":
                        text_parts.append(element.get("text", ""))
                    elif tag == "at":
                        text_parts.append(f"@{element.get('user_name', 'user')}")
                    elif tag == "img":
                        img_key = element.get("image_key")
                        if img_key:
                            image_keys.append(img_key)
        text = " ".join(text_parts).strip() if text_parts else None
        return text, image_keys
    
    # Try direct format first
    if "content" in content_json:
        text, images = extract_from_lang(content_json)
        if text or images:
            return text or "", images
    
    # Try localized format
    for lang_key in ("zh_cn", "en_us", "ja_jp"):
        lang_content = content_json.get(lang_key)
        text, images = extract_from_lang(lang_content)
        if text or images:
            return text or "", images
    
    return "", []


def _extract_post_text(content_json: dict) -> str:
    """Extract plain text from Feishu post (rich text) message content.
    
    Legacy wrapper for _extract_post_content, returns only text.
    """
    text, _ = _extract_post_content(content_json)
    return text


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.
    
    Uses WebSocket to receive events - no public IP or webhook required.
    
    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """
    
    name = "feishu"
    
    def __init__(self, config: FeishuConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._loop: asyncio.AbstractEventLoop | None = None
        self.media_cache: MediaCache = MediaCache(ttl_days=90)  # 3个月过期淘汰
    
    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return
        
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return
        
        self._running = True
        self._loop = asyncio.get_running_loop()
        
        # Create Lark client for sending messages
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        
        # Create event handler (only register message receive, ignore other events)
        event_handler = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        ).build()
        
        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO
        )
        
        # Start WebSocket client in a separate thread with reconnect loop
        def run_ws():
            while self._running:
                try:
                    self._ws_client.start()
                except Exception as e:
                    logger.warning("Feishu WebSocket error: {}", e)
                if self._running:
                    import time; time.sleep(5)
        
        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()
        
        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")

        # Start background task for periodic cache cleanup (每 24 小时执行一次)
        asyncio.create_task(self._periodic_cache_cleanup())

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the Feishu bot."""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning("Error stopping WebSocket client: {}", e)
        logger.info("Feishu bot stopped")

    async def _periodic_cache_cleanup(self) -> None:
        """
        Purpose:
            定期清理过期缓存文件的后台任务，每 24 小时执行一次。

        Params:
            None

        Returns:
            None
        """
        cleanup_interval = 24 * 3600  # 24 小时

        while self._running:
            try:
                await asyncio.sleep(cleanup_interval)
                if not self._running:
                    break

                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, self.media_cache.cleanup_periodic
                )

                # 输出缓存统计信息
                stats = self.media_cache.get_cache_stats()
                logger.info(
                    "Media cache cleanup completed: {} files, {:.2f} MB used",
                    stats["total_files"], stats["total_size_mb"]
                )
            except Exception as e:
                logger.error("Error during cache cleanup: {}", e)
    
    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """Sync helper for adding reaction (runs in thread pool)."""
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()
            
            response = self._client.im.v1.message_reaction.create(request)
            
            if not response.success():
                logger.warning("Failed to add reaction: code={}, msg={}", response.code, response.msg)
            else:
                logger.debug("Added {} reaction to message {}", emoji_type, message_id)
        except Exception as e:
            logger.warning("Error adding reaction: {}", e)

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        Add a reaction emoji to a message (non-blocking).
        
        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client or not Emoji:
            return
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)
    
    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    @staticmethod
    def _parse_md_table(table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [l.strip() for l in table_text.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            return None
        split = lambda l: [c.strip() for c in l.strip("|").split("|")]
        headers = split(lines[0])
        rows = [split(l) for l in lines[2:]]
        columns = [{"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
                   for i, h in enumerate(headers)]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [{f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """Split content into div/markdown + table elements for Feishu card."""
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end:m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            elements.append(self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)})
            last_end = m.end()
        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))
        return elements or [{"tag": "markdown", "content": content}]

    def _split_headings(self, content: str) -> list[dict]:
        """Split content by headings, converting headings to div elements."""
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks)-1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end:m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = m.group(2).strip()
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{text}**",
                },
            })
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    @staticmethod
    def _build_news_card(content: str, template_config: dict) -> dict | None:
        """
        根据模板配置和 LLM 输出的 JSON 数组，构建带 header 的飞书资讯卡片。

        参数：
            content: LLM 输出的原始文本，期望为 JSON 数组格式
            template_config: 卡片模板配置，包含 headerTitle, headerColor, sourceNote

        返回：
            飞书卡片 JSON dict，解析失败时返回 None（回退到通用渲染）
        """
        # 从 LLM 输出中提取 JSON 数组（兼容被 ```json 包裹的情况）
        text = content.strip()
        if text.startswith("```"):
            # 去掉首尾的代码块标记
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            items = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("卡片模板渲染：LLM 输出不是合法 JSON，回退到通用渲染")
            return None

        if not isinstance(items, list) or not items:
            logger.warning("卡片模板渲染：LLM 输出不是非空数组，回退到通用渲染")
            return None

        # 获取模板配置
        header_title = template_config.get("headerTitle", "📰 资讯日报")
        header_color = template_config.get("headerColor", "blue")
        source_note = template_config.get("sourceNote", "")

        # 生成日期副标题（当前日期，如 "3月2日"）
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
        subtitle = f"{now_sh.month}月{now_sh.day}日"

        # 构建卡片元素：每条资讯 + 分割线
        elements: list[dict] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            url = item.get("url", "")
            if not title:
                continue
            # 构建单条资讯的 markdown
            md = f"**{idx + 1}. {title}**"
            if url:
                md += f"\n🔗 [查看原文]({url})"
            elements.append({"tag": "markdown", "content": md})
            # 最后一条不加分割线
            if idx < len(items) - 1:
                elements.append({"tag": "hr"})

        if not elements:
            return None

        # 底部脚注
        if source_note:
            elements.append({"tag": "note", "elements": [
                {"tag": "plain_text", "content": source_note}
            ]})

        # 组装完整卡片
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "subtitle": {"tag": "plain_text", "content": subtitle},
                "template": header_color,
            },
            "elements": elements,
        }
        return card

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _FILE_TYPE_MAP = {
        ".opus": "opus", ".mp4": "mp4", ".pdf": "pdf", ".doc": "doc", ".docx": "doc",
        ".xls": "xls", ".xlsx": "xls", ".ppt": "ppt", ".pptx": "ppt",
    }

    def _upload_image_sync(self, file_path: str) -> str | None:
        """Upload an image to Feishu and return the image_key."""
        try:
            with open(file_path, "rb") as f:
                request = CreateImageRequest.builder() \
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(f)
                        .build()
                    ).build()
                response = self._client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    logger.debug("Uploaded image {}: {}", os.path.basename(file_path), image_key)
                    return image_key
                else:
                    logger.error("Failed to upload image: code={}, msg={}", response.code, response.msg)
                    return None
        except Exception as e:
            logger.error("Error uploading image {}: {}", file_path, e)
            return None

    def _upload_file_sync(self, file_path: str) -> str | None:
        """Upload a file to Feishu and return the file_key."""
        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                request = CreateFileRequest.builder() \
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    ).build()
                response = self._client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    logger.debug("Uploaded file {}: {}", file_name, file_key)
                    return file_key
                else:
                    logger.error("Failed to upload file: code={}, msg={}", response.code, response.msg)
                    return None
        except Exception as e:
            logger.error("Error uploading file {}: {}", file_path, e)
            return None

    def _download_image_sync(self, message_id: str, image_key: str) -> tuple[bytes | None, str | None]:
        """Download an image from Feishu message by message_id and image_key."""
        try:
            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(image_key) \
                .type("image") \
                .build()
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                # GetMessageResourceRequest returns BytesIO, need to read bytes
                if hasattr(file_data, 'read'):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error("Failed to download image: code={}, msg={}", response.code, response.msg)
                return None, None
        except Exception as e:
            logger.error("Error downloading image {}: {}", image_key, e)
            return None, None

    def _download_file_sync(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[bytes | None, str | None]:
        """Download a file/audio/media from a Feishu message by message_id and file_key."""
        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error("Failed to download {}: code={}, msg={}", resource_type, response.code, response.msg)
                return None, None
        except Exception:
            logger.exception("Error downloading {} {}", resource_type, file_key)
            return None, None

    async def _download_and_save_media(
        self,
        msg_type: str,
        content_json: dict,
        message_id: str | None = None
    ) -> tuple[str | None, str]:
        """
        Download media from Feishu and save to local disk with caching.

        For file/audio/media types: cache by file_key to avoid re-downloading.
        For image types: always download (images are typically smaller and less frequently repeated).

        Returns:
            (file_path, content_text) - file_path is None if download failed
        """
        loop = asyncio.get_running_loop()
        data, filename, file_key = None, None, None

        # 【缓存检查】针对 file/audio/media 类型，优先检查缓存
        if msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if file_key:
                cached_path = self.media_cache.get_cached(file_key)
                if cached_path:
                    logger.debug("Cache hit for {} {}", msg_type, file_key)
                    return cached_path, f"[{msg_type}: {Path(cached_path).name} (cached)]"

        # 执行下载
        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_image_sync, message_id, image_key
                )
                if not filename:
                    filename = f"{image_key[:16]}.jpg"

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if file_key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_file_sync, message_id, file_key, msg_type
                )
                if not filename:
                    ext = {"audio": ".opus", "media": ".mp4"}.get(msg_type, "")
                    filename = f"{file_key[:16]}{ext}"

        if data and filename:
            # 【缓存保存】针对 file/audio/media 类型，保存到缓存
            if msg_type in ("audio", "file", "media") and file_key:
                file_path = self.media_cache.save_media(
                    file_key=file_key,
                    data=data,
                    filename=filename,
                    message_id=message_id
                )
            else:
                # 图片等类型直接保存，不走缓存
                media_dir = Path.home() / ".coffiebot" / "media"
                media_dir.mkdir(parents=True, exist_ok=True)
                file_path = media_dir / filename
                file_path.write_bytes(data)

            logger.debug("Downloaded {} to {}", msg_type, file_path)
            return str(file_path), f"[{msg_type}: {filename}]"

        return None, f"[{msg_type}: download failed]"

    def _get_message_sync(self, message_id: str) -> tuple[str | None, dict]:
        """
        通过 message_id 查询飞书消息详情。

        Params:
            message_id (str): 飞书消息 ID

        Returns:
            (msg_type, content_json) - 消息类型和内容 JSON，查询失败时返回 (None, {})
        """
        try:
            request = GetMessageRequest.builder().message_id(message_id).build()
            response = self._client.im.v1.message.get(request)
            if response.success() and response.data and response.data.items:
                msg = response.data.items[0]
                msg_type = msg.msg_type or ""
                try:
                    content_json = json.loads(msg.body.content) if msg.body and msg.body.content else {}
                except (json.JSONDecodeError, AttributeError):
                    content_json = {}
                return msg_type, content_json
            else:
                logger.warning("Failed to get message {}: code={}, msg={}", message_id, response.code, response.msg)
                return None, {}
        except Exception as e:
            logger.warning("Error getting message {}: {}", message_id, e)
            return None, {}

    async def _get_parent_media(
        self, parent_id: str
    ) -> tuple[list[str], list[str]]:
        """
        查询父消息（被引用消息），提取其中的媒体文件并下载到本地。

        用于处理"回复文件消息再 @机器人"的场景：
        用户先发送文件，再右键回复并 @机器人，此时当前消息无附件，
        需通过 parent_id 查询父消息并下载其中的文件。

        Params:
            parent_id (str): 父消息 ID（被引用的消息）

        Returns:
            (media_paths, content_parts) - 下载后的本地文件路径列表和描述文本列表
        """
        loop = asyncio.get_running_loop()
        parent_msg_type, parent_content_json = await loop.run_in_executor(
            None, self._get_message_sync, parent_id
        )
        if not parent_msg_type:
            return [], []

        media_paths: list[str] = []
        content_parts: list[str] = []

        if parent_msg_type in ("image", "audio", "file", "media"):
            file_path, content_text = await self._download_and_save_media(
                parent_msg_type, parent_content_json, parent_id
            )
            if file_path:
                media_paths.append(file_path)
            content_parts.append(f"[引用消息附件] {content_text}")
        elif parent_msg_type == "post":
            text, image_keys = _extract_post_content(parent_content_json)
            if text:
                content_parts.append(f"[引用消息] {text}")
            for img_key in image_keys:
                file_path, content_text = await self._download_and_save_media(
                    "image", {"image_key": img_key}, parent_id
                )
                if file_path:
                    media_paths.append(file_path)
                content_parts.append(f"[引用消息附件] {content_text}")

        return media_paths, content_parts

    def _send_message_sync(self, receive_id_type: str, receive_id: str, msg_type: str, content: str) -> int | None:
        """发送单条消息（text/image/file/interactive），同步执行。

        返回值：成功返回 None，失败返回飞书错误码（int）。
        """
        try:
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                ).build()
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    "Failed to send Feishu {} message: code={}, msg={}, ext={}, log_id={}",
                    msg_type, response.code, response.msg,
                    getattr(response, "error", ""), response.get_log_id()
                )
                return response.code
            logger.debug("Feishu {} message sent to {}", msg_type, receive_id)
            return None
        except Exception as e:
            logger.error("Error sending Feishu {} message: {}", msg_type, e)
            return -1

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu, including media (images/files) if present."""
        if not self._client:
            logger.warning("Feishu client not initialized")
            return

        try:
            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    logger.warning("Media file not found: {}", file_path)
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                if ext in self._IMAGE_EXTS:
                    key = await loop.run_in_executor(None, self._upload_image_sync, file_path)
                    if key:
                        await loop.run_in_executor(
                            None, self._send_message_sync,
                            receive_id_type, msg.chat_id, "image", json.dumps({"image_key": key}, ensure_ascii=False),
                        )
                else:
                    key = await loop.run_in_executor(None, self._upload_file_sync, file_path)
                    if key:
                        media_type = "audio" if ext in self._AUDIO_EXTS else "file"
                        await loop.run_in_executor(
                            None, self._send_message_sync,
                            receive_id_type, msg.chat_id, media_type, json.dumps({"file_key": key}, ensure_ascii=False),
                        )

            if msg.content and msg.content.strip():
                # 检测是否有卡片模板配置，有则走模板渲染
                card_template = msg.metadata.get("card_template") if msg.metadata else None
                card = None
                if card_template and isinstance(card_template, dict):
                    # 仅在显式指定卡片模板时使用 interactive 消息
                    card = self._build_news_card(msg.content, card_template)
                    if card:
                        await loop.run_in_executor(
                            None, self._send_message_sync,
                            receive_id_type, msg.chat_id, "interactive", json.dumps(card, ensure_ascii=False),
                        )
                        return

                # 默认发送 Markdown 卡片（优化表格密度，避免超限）
                card = {"config": {"wide_screen_mode": True}, "elements": self._build_card_elements(msg.content)}
                err_code = await loop.run_in_executor(
                    None, self._send_message_sync,
                    receive_id_type, msg.chat_id, "interactive", json.dumps(card, ensure_ascii=False),
                )
                # 飞书卡片超限（230099）时降级为纯文本重发，避免消息丢失
                if err_code is not None:
                    logger.warning(
                        "Interactive card send failed (code={}), falling back to text message for {}",
                        err_code, msg.chat_id,
                    )
                    text_content = json.dumps({"text": msg.content}, ensure_ascii=False)
                    await loop.run_in_executor(
                        None, self._send_message_sync,
                        receive_id_type, msg.chat_id, "text", text_content,
                    )

        except Exception as e:
            logger.error("Error sending Feishu message: {}", e)
    
    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)
    
    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """Handle incoming message from Feishu."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # Skip bot messages
            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            # Add reaction
            await self._add_reaction(message_id, self.config.react_emoji)

            # Parse content
            content_parts = []
            media_paths = []

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    content_parts.append(text)

            elif msg_type == "post":
                text, image_keys = _extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                # Download images embedded in post
                for img_key in image_keys:
                    file_path, content_text = await self._download_and_save_media(
                        "image", {"image_key": img_key}, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(msg_type, content_json, message_id)
                if file_path:
                    media_paths.append(file_path)
                content_parts.append(content_text)

            elif msg_type in ("share_chat", "share_user", "interactive", "share_calendar_event", "system", "merge_forward"):
                # Handle share cards and interactive messages
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            content = "\n".join(content_parts) if content_parts else ""

            # 若当前消息无附件且存在父消息（用户回复了一条文件消息再 @机器人），
            # 则查询父消息并下载其中的媒体文件，合并到当前消息上下文中
            parent_id = getattr(message, "parent_id", None)
            if not media_paths and parent_id:
                parent_media_paths, parent_content_parts = await self._get_parent_media(parent_id)
                if parent_media_paths or parent_content_parts:
                    media_paths = parent_media_paths + media_paths
                    if parent_content_parts:
                        content = "\n".join(parent_content_parts) + ("\n" + content if content else "")
                    logger.debug(
                        "Resolved {} media file(s) from parent message {}",
                        len(parent_media_paths), parent_id
                    )

            if not content and not media_paths:
                return

            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                }
            )

        except Exception as e:
            logger.error("Error processing Feishu message: {}", e)
