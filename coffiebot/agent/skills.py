"""
Skills loader for agent capabilities.

【架构】后台定时扫描 + mtime 缓存

消息处理时零 IO：所有公共方法从内存缓存 (_SkillsCache) 读取。
后台每 60 秒异步扫描一次磁盘，仅在 mtime 变化时 read_text()。
缓存快照原子替换，读取方无需加锁。

【加载路径】

路径 A：ALWAYS SKILLS（完整加载）
  build_system_prompt() → get_always_skills() → list_skills()
  → load_skills_for_context() → load_skill()
  全部走缓存，零 IO。

路径 B：SKILLS SUMMARY（摘要生成）
  build_system_prompt() → build_skills_summary() → list_skills()
  → get_skill_metadata()
  全部走缓存，零 IO。

【缓存刷新】
  后台 asyncio.Task 每 60 秒执行 _refresh_cache_sync()（通过 run_in_executor）：
  1. iterdir() 扫描目录
  2. stat() 获取 mtime
  3. mtime 未变 → 复用旧 entry（跳过 read_text）
  4. mtime 变化或新文件 → read_text() + 解析 frontmatter
  5. 构建新 _SkillsCache 对象，原子替换 self._cache
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

# 内置 skills 目录（相对于本文件）
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"

# 后台刷新间隔（秒）
_REFRESH_INTERVAL_SECONDS = 300


@dataclass
class _SkillCacheEntry:
    """单个 SKILL.md 的缓存条目。"""
    path: str               # SKILL.md 绝对路径
    source: str             # "workspace" | "builtin"
    mtime: float            # stat().st_mtime
    content: str            # read_text() 完整内容
    metadata: dict | None   # 解析后的 frontmatter


@dataclass
class _SkillsCache:
    """全量缓存快照，原子替换保证一致性。"""
    entries: dict[str, _SkillCacheEntry] = field(default_factory=dict)  # name -> entry
    built_at: float = 0.0


class SkillsLoader:
    """
    Skills 加载器（mtime 缓存版本）。

    所有公共方法从内存缓存读取，消息处理时零 IO。
    后台定时扫描磁盘，仅在文件变化时更新缓存。
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        """
        Purpose:
            初始化 SkillsLoader，设置目录路径和缓存字段。

        Params:
            workspace (Path): 工作空间根路径
            builtin_skills_dir (Path | None): 内置 skills 目录，默认使用项目自带目录
        """
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR

        # 缓存相关字段
        self._cache: _SkillsCache = _SkillsCache()
        self._refresh_task: asyncio.Task | None = None
        self._initialized: bool = False

    # ──────────────────────────────────────────────
    # 缓存构建（同步，在线程池中执行）
    # ──────────────────────────────────────────────

    @staticmethod
    def _parse_frontmatter(content: str) -> dict | None:
        """
        Purpose:
            从 SKILL.md 内容中解析 YAML frontmatter。

        Params:
            content (str): SKILL.md 完整内容

        Returns:
            dict | None: 解析后的 frontmatter 字典，无 frontmatter 时返回 None
        """
        if not content.startswith("---"):
            return None
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return None
        # 简单 YAML 解析（与原实现保持一致）
        metadata = {}
        for line in match.group(1).split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip().strip("\"'")
        return metadata

    def _refresh_cache_sync(self) -> _SkillsCache:
        """
        Purpose:
            同步扫描磁盘构建新的缓存快照。
            对每个 SKILL.md 检查 mtime，未变则复用旧 entry，变化则重新读取。
            此方法含阻塞 IO，应通过 run_in_executor 在线程池中执行。

        Returns:
            _SkillsCache: 新构建的缓存快照
        """
        old_entries = self._cache.entries
        new_entries: dict[str, _SkillCacheEntry] = {}

        # 扫描 workspace skills（优先级高）
        self._scan_directory(
            self.workspace_skills, "workspace", old_entries, new_entries,
        )

        # 扫描 builtin skills（优先级低，不覆盖同名 workspace skill）
        if self.builtin_skills:
            self._scan_directory(
                self.builtin_skills, "builtin", old_entries, new_entries,
            )

        new_cache = _SkillsCache(entries=new_entries, built_at=time.time())
        return new_cache

    def _scan_directory(
        self,
        skills_directory: Path,
        source: str,
        old_entries: dict[str, _SkillCacheEntry],
        new_entries: dict[str, _SkillCacheEntry],
    ) -> None:
        """
        Purpose:
            扫描单个 skills 目录，将发现的 skill 加入 new_entries。
            workspace 优先：如果 name 已在 new_entries 中则跳过。

        Params:
            skills_directory (Path): 要扫描的目录
            source (str): "workspace" 或 "builtin"
            old_entries (dict): 旧缓存条目，用于 mtime 比较
            new_entries (dict): 新缓存条目，构建中
        """
        if not skills_directory.exists():
            return
        try:
            children = list(skills_directory.iterdir())
        except OSError as error:
            logger.warning("无法扫描 skills 目录 {}: {}", skills_directory, error)
            return

        for skill_directory in children:
            if not skill_directory.is_dir():
                continue
            name = skill_directory.name
            # workspace 优先：已存在则不覆盖
            if name in new_entries:
                continue

            skill_file = skill_directory / "SKILL.md"
            if not skill_file.exists():
                continue

            try:
                current_mtime = skill_file.stat().st_mtime
            except OSError:
                continue

            skill_path = str(skill_file)
            old_entry = old_entries.get(name)

            # mtime 未变且路径一致 → 复用旧 entry（跳过 read_text）
            if (
                old_entry is not None
                and old_entry.path == skill_path
                and old_entry.mtime == current_mtime
            ):
                new_entries[name] = old_entry
                continue

            # mtime 变化或新文件 → read_text + 解析 frontmatter
            try:
                content = skill_file.read_text(encoding="utf-8")
            except OSError as error:
                logger.warning("无法读取 SKILL.md {}: {}", skill_file, error)
                continue

            metadata = self._parse_frontmatter(content)
            new_entries[name] = _SkillCacheEntry(
                path=skill_path,
                source=source,
                mtime=current_mtime,
                content=content,
                metadata=metadata,
            )

    # ──────────────────────────────────────────────
    # 后台刷新 + 生命周期
    # ──────────────────────────────────────────────

    async def _background_refresh_loop(self) -> None:
        """
        Purpose:
            后台协程：每 _REFRESH_INTERVAL_SECONDS 秒执行一次缓存刷新。
            通过 run_in_executor 在线程池中执行 _refresh_cache_sync，避免阻塞事件循环。
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)
                new_cache = await loop.run_in_executor(None, self._refresh_cache_sync)
                self._cache = new_cache
                logger.debug(
                    "Skills cache refreshed: {} entries", len(new_cache.entries),
                )
            except asyncio.CancelledError:
                break
            except Exception as error:
                logger.error("Skills cache refresh failed: {}", error)

    def _ensure_initialized(self) -> None:
        """
        Purpose:
            冷启动保障：首次调用时同步初始化缓存。
            保证第一条消息到达前缓存已可用，
            process_direct() 等不经过 run() 的路径也能正常工作。
        """
        if self._initialized:
            return
        self._cache = self._refresh_cache_sync()
        self._initialized = True
        logger.info(
            "Skills cache initialized: {} entries", len(self._cache.entries),
        )

    def start_background_refresh(self) -> None:
        """
        Purpose:
            启动后台缓存刷新 Task。在 AgentLoop.run() 中调用。
            如果缓存尚未初始化，先同步初始化一次。
        """
        self._ensure_initialized()
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(
                self._background_refresh_loop(),
            )
            logger.info("Skills background refresh started")

    def stop_background_refresh(self) -> None:
        """
        Purpose:
            取消后台刷新 Task。在 AgentLoop.stop() 中调用。
        """
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            logger.info("Skills background refresh stopped")
        self._refresh_task = None

    # ──────────────────────────────────────────────
    # 公共方法（全部走缓存，零 IO）
    # ──────────────────────────────────────────────

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        Purpose:
            列出所有可用的 skills（本地 + 内置）。
            从内存缓存读取，零 IO。

        Params:
            filter_unavailable (bool): True 时检查依赖（CLI 工具、环境变量），
                                       只返回可用的 skills；False 返回全部

        Returns:
            list[dict[str, str]]: skill 信息列表，每项包含 name / path / source
        """
        self._ensure_initialized()
        skills = [
            {"name": name, "path": entry.path, "source": entry.source}
            for name, entry in self._cache.entries.items()
        ]

        if filter_unavailable:
            return [
                skill for skill in skills
                if self._check_requirements(self._get_skill_meta(skill["name"]))
            ]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Purpose:
            按名称加载一个 skill 的完整内容。从缓存读取，零 IO。

        Params:
            name (str): skill 名称

        Returns:
            str | None: SKILL.md 完整内容（含 frontmatter），不存在时返回 None
        """
        self._ensure_initialized()
        entry = self._cache.entries.get(name)
        return entry.content if entry is not None else None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Purpose:
            加载指定 skills 的完整内容并格式化，用于注入系统提示。
            移除 YAML frontmatter，保留 markdown body。

        Params:
            skill_names (list[str]): 要加载的 skill 名称列表

        Returns:
            str: 格式化后的 skills 内容，多个 skill 之间用分隔线连接
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")
        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """
        Purpose:
            构建所有 skills 的 XML 摘要。从缓存读取，零 IO。
            _check_requirements 仍然实时检查（运行时依赖不适合缓存）。

        Returns:
            str: XML 格式的 skills 摘要
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(string: str) -> str:
            """转义 XML 特殊字符。"""
            return string.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for skill in all_skills:
            name = escape_xml(skill["name"])
            path = skill["path"]
            description = escape_xml(self._get_skill_description(skill["name"]))
            skill_meta = self._get_skill_meta(skill["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{description}</description>")
            lines.append(f"    <location>{path}</location>")

            # 不可用时显示缺失的依赖
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    def get_always_skills(self) -> list[str]:
        """
        Purpose:
            获取标记为 always=true 的 skills 列表（需满足依赖要求）。

        Returns:
            list[str]: always=true 且依赖满足的 skill 名称列表
        """
        result = []
        for skill in self.list_skills(filter_unavailable=True):
            metadata = self.get_skill_metadata(skill["name"]) or {}
            skill_meta = self._parse_coffiebot_metadata(metadata.get("metadata", ""))
            if skill_meta.get("always") or metadata.get("always"):
                result.append(skill["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Purpose:
            获取 skill 的 frontmatter 元数据。从缓存读取，零 IO。

        Params:
            name (str): skill 名称

        Returns:
            dict | None: frontmatter 字典，不存在时返回 None
        """
        self._ensure_initialized()
        entry = self._cache.entries.get(name)
        if entry is None:
            return None
        return entry.metadata

    # ──────────────────────────────────────────────
    # 内部辅助方法
    # ──────────────────────────────────────────────

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """获取缺失依赖的描述文本。"""
        missing = []
        requires = skill_meta.get("requires", {})
        for binary in requires.get("bins", []):
            if not shutil.which(binary):
                missing.append(f"CLI: {binary}")
        for environment_variable in requires.get("env", []):
            if not os.environ.get(environment_variable):
                missing.append(f"ENV: {environment_variable}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """从 frontmatter 获取 skill 描述，回退到 skill 名称。"""
        metadata = self.get_skill_metadata(name)
        if metadata and metadata.get("description"):
            return metadata["description"]
        return name

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """移除 markdown 内容的 YAML frontmatter。"""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_coffiebot_metadata(self, raw: str) -> dict:
        """解析 frontmatter 中的 coffiebot 元数据 JSON（支持 coffiebot 和 openclaw 两种 key）。"""
        try:
            data = json.loads(raw)
            return data.get("coffiebot", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """检查 skill 的运行时依赖是否满足（CLI 工具、环境变量）。实时检查，不缓存。"""
        requires = skill_meta.get("requires", {})
        for binary in requires.get("bins", []):
            if not shutil.which(binary):
                return False
        for environment_variable in requires.get("env", []):
            if not os.environ.get(environment_variable):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """获取 skill 的 coffiebot 元数据（从缓存的 frontmatter 中解析）。"""
        metadata = self.get_skill_metadata(name) or {}
        return self._parse_coffiebot_metadata(metadata.get("metadata", ""))
