"""
媒体文件缓存管理器，支持按 file_key 缓存和自动过期淘汰。

缓存策略：
- Key：飞书 file_key（全局唯一）
- Value：本地文件路径 + 元信息
- 过期策略：3个月未访问的文件自动删除
- 索引文件：~/.coffiebot/media/.index.json
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger


class MediaCache:
    """
    Purpose:
        媒体文件缓存管理器，提供文件缓存、查询、淘汰等能力。

    Attributes:
        cache_dir (Path): 缓存目录路径，默认为 ~/.coffiebot/media/
        index_path (Path): 索引文件路径，默认为 ~/.coffiebot/media/.index.json
        ttl_days (int): 缓存过期时间（天数），默认 90 天（3个月）
        index (dict): 内存中的索引字典，结构为 {file_key: metadata}
    """

    def __init__(self, cache_dir: Optional[Path] = None, ttl_days: int = 90):
        """
        Purpose:
            初始化媒体缓存管理器。

        Params:
            cache_dir (Optional[Path]): 缓存目录路径，不指定则使用 ~/.coffiebot/media/
            ttl_days (int): 缓存过期时间（天数），默认 90 天

        Returns:
            None
        """
        self.cache_dir = cache_dir or Path.home() / ".coffiebot" / "media"
        self.index_path = self.cache_dir / ".index.json"
        self.ttl_days = ttl_days

        # 创建缓存目录
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # 加载或初始化索引
        self.index = self._load_index()

        # 首次初始化时执行一次淘汰
        self._cleanup_expired()

        logger.debug(
            "MediaCache initialized: dir={}, ttl={}d, entries={}",
            self.cache_dir, self.ttl_days, len(self.index)
        )

    def _load_index(self) -> dict:
        """
        Purpose:
            从磁盘加载索引文件。若不存在则返回空字典。

        Params:
            None

        Returns:
            dict: 索引字典，结构为 {file_key: {path, filename, message_id, cached_at, size, accessed_at}}
        """
        if not self.index_path.exists():
            return {}

        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load index from {}: {}", self.index_path, e)
            return {}

    def _save_index(self) -> None:
        """
        Purpose:
            将索引字典保存到磁盘（JSON 格式）。

        Params:
            None

        Returns:
            None
        """
        try:
            with open(self.index_path, "w", encoding="utf-8") as f:
                json.dump(self.index, f, indent=2, ensure_ascii=False)
        except IOError as e:
            logger.error("Failed to save index to {}: {}", self.index_path, e)

    def get_cached(self, file_key: str) -> Optional[str]:
        """
        Purpose:
            按 file_key 查询缓存，返回本地文件路径。同时更新访问时间。

        Params:
            file_key (str): 飞书文件唯一标识

        Returns:
            Optional[str]: 本地文件路径，若缓存不存在或文件已删除则返回 None
        """
        if file_key not in self.index:
            return None

        metadata = self.index[file_key]
        file_path = self.cache_dir / metadata["path"]

        # 检查文件是否仍存在
        if not file_path.exists():
            logger.warning("Cached file not found for {}: {}", file_key, file_path)
            del self.index[file_key]
            self._save_index()
            return None

        # 更新访问时间
        metadata["accessed_at"] = datetime.now().isoformat()
        self._save_index()

        return str(file_path)

    def save_media(
        self,
        file_key: str,
        data: bytes,
        filename: str,
        message_id: Optional[str] = None,
    ) -> str:
        """
        Purpose:
            保存媒体文件并记录元信息。若 file_key 已存在则直接返回本地路径（无需重复保存）。

        Params:
            file_key (str): 飞书文件唯一标识
            data (bytes): 文件二进制内容
            filename (str): 原始文件名（用于保存时的参考）
            message_id (Optional[str]): 来源消息 ID，可选

        Returns:
            str: 本地文件路径（绝对路径）
        """
        # 检查是否已缓存，若已缓存则直接返回
        cached_path = self.get_cached(file_key)
        if cached_path:
            logger.debug("File already cached for {}: {}", file_key, cached_path)
            return cached_path

        # 生成本地文件名（基于 file_key 的 hash）
        cache_filename = self._gen_cache_filename(file_key, filename)
        file_path = self.cache_dir / cache_filename

        # 保存文件
        file_path.write_bytes(data)
        logger.debug("Saved media file: {} ({} bytes)", file_path, len(data))

        # 记录索引
        self.index[file_key] = {
            "path": cache_filename,
            "filename": filename,
            "message_id": message_id or "",
            "cached_at": datetime.now().isoformat(),
            "accessed_at": datetime.now().isoformat(),
            "size": len(data),
        }
        self._save_index()

        return str(file_path)

    def _gen_cache_filename(self, file_key: str, original_filename: str) -> str:
        """
        Purpose:
            基于 file_key 和原始文件名生成缓存文件名。

        Params:
            file_key (str): 飞书文件唯一标识
            original_filename (str): 原始文件名

        Returns:
            str: 缓存文件名（不含目录）
        """
        # 取 file_key 的前 16 个字符作为前缀
        prefix = file_key[:16]

        # 保留原始文件的扩展名
        ext = Path(original_filename).suffix or ".bin"

        # 组合生成缓存文件名
        return f"{prefix}_{int(time.time())}{ext}"

    def _cleanup_expired(self) -> None:
        """
        Purpose:
            清理过期缓存文件。按照 ttl_days 判断，超过该天数的文件删除。

        Params:
            None

        Returns:
            None
        """
        cutoff_time = datetime.now() - timedelta(days=self.ttl_days)
        cutoff_timestamp = cutoff_time.isoformat()

        expired_keys = []
        deleted_count = 0
        freed_bytes = 0

        for file_key, metadata in self.index.items():
            # 使用 accessed_at（访问时间）作为淘汰依据
            # 若没有 accessed_at（旧数据），使用 cached_at（缓存时间）
            last_time_str = metadata.get("accessed_at") or metadata.get("cached_at")

            if not last_time_str:
                # 数据格式不正常，标记为过期
                expired_keys.append(file_key)
                continue

            # 比较时间
            if last_time_str < cutoff_timestamp:
                expired_keys.append(file_key)

        # 删除过期文件
        for file_key in expired_keys:
            metadata = self.index[file_key]
            file_path = self.cache_dir / metadata["path"]

            try:
                if file_path.exists():
                    freed_bytes += file_path.stat().st_size
                    file_path.unlink()
                    logger.debug("Deleted expired cache file: {}", file_path)

                del self.index[file_key]
                deleted_count += 1
            except Exception as e:
                logger.error("Failed to delete cache file {}: {}", file_path, e)

        # 保存更新后的索引
        if deleted_count > 0:
            self._save_index()
            logger.info(
                "Cleaned up {} expired files, freed {:.2f} MB",
                deleted_count, freed_bytes / (1024 * 1024)
            )

    def cleanup_periodic(self) -> None:
        """
        Purpose:
            定期清理过期缓存（通常由后台任务调用）。

        Params:
            None

        Returns:
            None
        """
        self._cleanup_expired()

    def get_metadata(self, file_key: str) -> Optional[dict]:
        """
        Purpose:
            获取指定 file_key 的缓存元信息。

        Params:
            file_key (str): 飞书文件唯一标识

        Returns:
            Optional[dict]: 元信息字典，若不存在则返回 None
        """
        return self.index.get(file_key)

    def get_cache_stats(self) -> dict:
        """
        Purpose:
            获取缓存统计信息。

        Params:
            None

        Returns:
            dict: 统计信息，包含总文件数、总大小等
        """
        total_files = len(self.index)
        total_size = sum(m.get("size", 0) for m in self.index.values())

        return {
            "total_files": total_files,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "cache_dir": str(self.cache_dir),
            "ttl_days": self.ttl_days,
        }
