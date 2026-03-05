# 媒体文件缓存管理

## 功能概述

`MediaCache` 提供了一个简单有效的文件缓存管理系统，主要用于：

1. **去重下载**：避免重复下载同一个文件（使用 file_key 作为唯一标识）
2. **自动过期淘汰**：3个月（可配置）未访问的文件自动删除
3. **索引管理**：JSON 格式的索引，支持快速查询

## 缓存策略

### 索引存储

- **位置**：`~/.coffiebot/media/.index.json`
- **格式**：JSON，记录每个缓存文件的元信息
- **Key**：飞书 `file_key`（全局唯一）
- **Value**：
  - `path`：相对缓存目录的路径
  - `filename`：原始文件名
  - `message_id`：来源消息 ID
  - `cached_at`：首次缓存时间
  - `accessed_at`：最后访问时间（每次读取时更新）
  - `size`：文件大小（字节）

### 文件命名

缓存文件的本地名称生成规则：
```
{file_key[:16]}_{timestamp}{ext}

例如：
Acv/hCb2C1C5G_1709449578.pdf
```

### 过期淘汰

- **淘汰周期**：每 24 小时执行一次（由 feishu.py 的后台任务触发）
- **淘汰依据**：按 `accessed_at`（访问时间）判断，默认 90 天（3 个月）未访问则删除
- **淘汰触发**：
  - 自动：后台定期任务每 24 小时调用一次
  - 手动：调用 `cleanup_periodic()` 方法

## 使用示例

### 基本使用

```python
from coffiebot.media import MediaCache

# 初始化（默认 TTL 90 天）
cache = MediaCache()

# 保存媒体文件
file_key = "Acv/hCb2C1C5G"
data = open("resume.pdf", "rb").read()
local_path = cache.save_media(
    file_key=file_key,
    data=data,
    filename="resume.pdf",
    message_id="om_abc123"
)
print(f"Saved to: {local_path}")

# 查询缓存（如果存在，返回本地路径；如果过期或不存在，返回 None）
cached_path = cache.get_cached(file_key)
if cached_path:
    print(f"Cache hit: {cached_path}")
else:
    print("Cache miss, need to download from Feishu")

# 获取元信息
metadata = cache.get_metadata(file_key)
print(f"Metadata: {metadata}")

# 获取缓存统计
stats = cache.get_cache_stats()
print(f"Total files: {stats['total_files']}")
print(f"Total size: {stats['total_size_mb']} MB")
```

### 自定义 TTL

```python
# 创建 60 天过期的缓存（2个月）
cache = MediaCache(ttl_days=60)

# 或 365 天过期（1年）
cache = MediaCache(ttl_days=365)
```

## 集成到 Feishu Channel

在 `coffiebot/channels/feishu.py` 中已自动集成：

```python
class FeishuChannel(BaseChannel):
    def __init__(self, config: FeishuConfig, bus: MessageBus):
        # ...
        self.media_cache: MediaCache = MediaCache(ttl_days=90)

    async def _download_and_save_media(self, ...):
        # 优先检查缓存
        if msg_type in ("file", "audio", "media"):
            file_key = content_json.get("file_key")
            cached_path = self.media_cache.get_cached(file_key)
            if cached_path:
                return cached_path, f"[{msg_type}: ... (cached)]"

        # 若缓存未命中，执行下载
        data = download_from_feishu(...)

        # 保存到缓存
        local_path = self.media_cache.save_media(
            file_key=file_key,
            data=data,
            filename=filename,
            message_id=message_id
        )
        return local_path, f"[{msg_type}: {filename}]"

    async def _periodic_cache_cleanup(self):
        # 每 24 小时执行一次过期淘汰
        while self._running:
            await asyncio.sleep(24 * 3600)
            await loop.run_in_executor(None, self.media_cache.cleanup_periodic)
```

## 缓存目录结构

```
~/.coffiebot/media/
├── .index.json                      # 索引文件
│   {
│     "Acv/hCb2C1C5G": {
│       "path": "Acv/hCb2C1C5G_1709449578.pdf",
│       "filename": "resume.pdf",
│       "message_id": "om_abc123def456",
│       "cached_at": "2026-03-02T16:46:18.282000",
│       "accessed_at": "2026-03-02T17:30:00.000000",
│       "size": 524288
│     }
│   }
├── Acv/hCb2C1C5G_1709449578.pdf     # 实际文件
├── xyz/XyZ1aBc2dEf_1709450000.pdf
└── ...
```

## 性能特点

- **首次访问**：飞书 API 下载（网络 I/O）
- **缓存命中**：本地文件读取（毫秒级）
- **后台淘汰**：异步执行，不阻塞主线程
- **索引操作**：JSON 解析，毫秒级

## 注意事项

1. **磁盘空间**：确保 `~/.coffiebot/media/` 有足够磁盘空间
2. **并发访问**：多个进程同时访问时，索引文件可能出现冲突（当前实现假设单进程）
3. **文件完整性**：缓存文件删除后无法恢复，应根据需要调整 TTL
4. **权限**：确保有读写 `~/.coffiebot/media/` 目录的权限

## 调试命令

```python
# 查看缓存统计
stats = cache.get_cache_stats()
print(f"Cache dir: {stats['cache_dir']}")
print(f"Total files: {stats['total_files']}")
print(f"Total size: {stats['total_size_mb']} MB")
print(f"TTL: {stats['ttl_days']} days")

# 手动触发清理
cache.cleanup_periodic()

# 查看索引内容
import json
with open(Path.home() / ".coffiebot/media/.index.json") as f:
    index = json.load(f)
    for file_key, metadata in index.items():
        print(f"{file_key}: {metadata['filename']} ({metadata['size']} bytes)")
```
