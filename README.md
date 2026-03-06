# coffiebot

一个轻量级个人 AI 助理框架，支持飞书、Telegram 等多渠道接入，集成可插拔的语义记忆后端（OpenViking / Mem0）。

## 核心特性

### 1. 可插拔的语义记忆后端

coffiebot 实现了 **MemoryBridgeProtocol** 协议，支持多种记忆存储后端：

#### OpenViking（推荐用于大规模部署）

OpenViking 是一个高性能的上下文记忆数据库，支持：
- **向量检索**：通过语义相似度召回相关记忆
- **分层记忆**：自动分类与组织用户交互历史
- **长期记忆保留**：无限制的记忆存储和智能聚合

配置示例：
```json
{
  "openviking": {
    "enabled": true,
    "server_url": "http://openviking-server:1933",
    "api_key": "your_api_key",
    "agent_id": "coffiebot",
    "user_id": "user123",
    "recall_limit": 5,
    "recall_score_threshold": 0.0
  }
}
```

#### Mem0（轻量级本地记忆）

Mem0 是一个开源的记忆管理框架，适合本地或小规模部署：
- **本地化存储**：可自托管，无外部依赖
- **灵活的记忆策略**：支持自定义记忆提取和召回规则
- **兼容 OpenViking 接口**：无缝切换两个后端

配置示例：
```json
{
  "mem0": {
    "enabled": true,
    "server_url": "http://mem0-server:9356",
    "user_id": "coffiebot",
    "agent_id": "optional_agent_id",
    "timeout": 15.0
  }
}
```

> **注意**：OpenViking 和 Mem0 互斥，同时启用会报错。请在配置中只启用其中一个。

### 2. 智能会话管理与自动清理

coffiebot 采用 **JSONL 追加式存储**，长期运行的对话文件会无限增长。为解决这一问题：

#### 消息 Consolidation（消息整理）
- 当会话中 **未整理的消息数**超过 `memory_window` 时，自动触发 consolidation
- Consolidation 将旧消息提交到语义记忆后端（OpenViking / Mem0）
- 更新 `last_consolidated` 指针，标记已安全存档的消息范围

#### 自动文件清理（Trimming）
- `save()` 时检查 JSONL 文件大小，超过阈值（默认 500MB）则触发清理
- 清理算法仅删除 `messages[0:last_consolidated]` 范围的消息（已安全存档）
- 逐条累计序列化大小，达到目标释放量后停止（默认 100MB）
- 重新写盘保存，释放磁盘空间

配置示例：
```json
{
  "agents": {
    "defaults": {
      "memory_window": 100,
      "session_max_file_size_mb": 500,
      "session_cleanup_size_mb": 100
    }
  }
}
```

**工作流程**：
```
消息到达
    ↓
会话达到 memory_window → consolidation → 提交到 OpenViking/Mem0
    ↓
save() 时检查文件大小 → > 500MB → trim → 删除已 consolidated 的旧消息
    ↓
重写 JSONL 文件（文件大小显著下降）
```

**安全性保证**：
- `last_consolidated` 只在 capture 成功后才推进，consolidation 失败不会丢失消息
- Trim 仅操作已 consolidated 的消息，LLM 不会看到被删除的消息（`get_history()` 跳过它们）
- 进程 Crash 不会导致指针丢失（consolidation 完成后立即 save）

### 3. 多渠道集成

支持以下通信渠道：
- **飞书（Lark）**：企业级消息应用，支持 WebSocket 长连接
- **Telegram**：基于 Bot API，支持消息、图片、文件
- **Discord**：游戏社区平台集成
- **Slack**：企业协作工具
- **WhatsApp**：通过第三方网桥支持
- **DingTalk**：钉钉企业应用
- **Matrix**：去中心化通讯协议（含 E2EE 支持）
- **QQ**：中文互联网社交应用
- **Email**：IMAP 收信 + SMTP 发信

### 4. 灵活的数据目录配置

coffiebot 支持通过 **COFFIEBOT_DATA_DIR** 环境变量指定数据根目录：

```bash
# 默认位置：~/.coffiebot/
export COFFIEBOT_DATA_DIR=/var/lib/coffiebot
coffiebot gateway

# Docker 部署推荐做法
docker run -e COFFIEBOT_DATA_DIR=/data -v /host/data:/data coffiebot
```

目录结构：
```
<COFFIEBOT_DATA_DIR>/
├── workspace/           # Agent 工作目录
│   ├── sessions/        # 会话 JSONL 文件
│   └── memory/          # 本地记忆（MEMORY.md、HISTORY.md）
├── config.json          # 全局配置
├── logs/                # 日志文件
├── media/               # 媒体缓存
└── cron/                # 定时任务配置
```

> 本地部署时，`COFFIEBOT_DATA_DIR` 可指向集中的数据目录（如 `/Users/fwy/soft/coffiebot/data`）

### 5. LLM 提供商支持

通过 LiteLLM 封装，支持主流 LLM 提供商：
- Anthropic Claude（推荐）
- OpenAI（GPT-4/3.5）
- OpenRouter（多模型聚合）
- DeepSeek
- Groq
- 阿里云通义千问（Dashscope）
- 火山引擎（VolcEngine）
- 硅基流动（SiliconFlow）
- 本地 vLLM（自托管）
- 以及 OpenAI 兼容的其他服务

自动检测已配置的提供商 API Key，无需手动指定。

### 6. 可扩展的工具系统

内置工具：
- **文件操作**：读、写、编辑、列目录
- **Shell 执行**：带超时和沙箱限制
- **Web 工具**：Web 搜索（Brave Search）、网页获取
- **定时任务**：Cron 表达式支持的后台任务
- **消息发送**：在任务中通过消息工具回复用户

MCP（Model Context Protocol）支持：
- 通过标准 MCP 协议连接外部工具
- Stdio 模式：本地命令
- HTTP 模式：远程服务

### 7. 会话隔离与并发处理

- 基于 `channel:chat_id` 的会话隔离
- 同一会话内的消息串行处理（通过锁保证）
- 多会话间并发无阻塞
- 子 Agent 支持：主 Agent 可动态生成子 Agent 处理复杂任务

## 快速开始

### 安装

```bash
pip install coffiebot
# 或从源代码安装
git clone https://github.com/HKUDS/coffiebot.git
cd coffiebot
pip install -e .
```

### 基础配置

在 `~/.coffiebot/config.json` 中配置：

```json
{
  "providers": {
    "anthropic": {
      "api_key": "sk-ant-..."
    }
  },
  "channels": {
    "feishu": {
      "enabled": true,
      "app_id": "your_app_id",
      "app_secret": "your_app_secret"
    }
  },
  "openviking": {
    "enabled": true,
    "server_url": "http://openviking:1933",
    "api_key": "your_key"
  }
}
```

### 启动网关

```bash
coffiebot gateway
```

### CLI 模式测试

```bash
coffiebot agent -m "Hello, what's the weather?"
```

## 架构设计

```
消息流向
    ↓
飞书/Telegram/Discord 等渠道 → MessageBus
    ↓
AgentLoop（核心循环）
    ├─ ContextBuilder
    │  ├─ MemoryStore（语义记忆召回）→ OpenViking/Mem0
    │  ├─ SkillsLoader（技能动态加载）
    │  └─ SystemPromptBuilder
    ├─ LLM 调用（支持多提供商）
    ├─ ToolRegistry（工具执行）
    │  ├─ 文件操作
    │  ├─ Web 工具
    │  ├─ Shell 执行
    │  └─ MCP 工具
    └─ SessionManager（会话存储）
       ├─ 消息 Consolidation
       └─ 自动文件清理
    ↓
响应回复 → 各渠道
    ↓
MemoryBridge（异步后台）
    └─ 将新消息提交到语义记忆后端
```

## 关键设计决策

### 消息储存策略：追加式 JSONL

- **为什么用追加式？** LLM 缓存友好，启用 Prompt Caching 时无需重新哈希
- **为什么是 JSONL？** 单行 JSON，易于流式读取，支持部分修复
- **如何处理无限增长？** Consolidation + Trimming 两层机制

### 语义记忆与本地历史分离

- **不再写本地 MEMORY.md/HISTORY.md**：专注 OpenViking/Mem0 这类高性能后端
- **LLM 不看已 consolidated 的消息**：通过 `get_history(max_messages=)` 自动过滤
- **消息被删除不影响安全**：Trim 前已确认 Consolidation 成功

### 会话锁与并发模型

- **行级锁（会话粒度）**：同会话串行，不同会话并发
- **Consolidation 不阻塞**：后台异步执行，避免响应延迟
- **强引用保护 Task**：防止 Consolidation 中途被 GC 清理

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `COFFIEBOT_DATA_DIR` | 数据根目录 | `~/.coffiebot` |
| `COFFIEBOT_LOG_LEVEL` | 日志级别 | `INFO` |
| 各 LLM 提供商密钥 | 如 `ANTHROPIC_API_KEY` | - |

## 故障排查

### 会话文件过大导致启动缓慢

检查会话文件大小：
```bash
ls -lh ~/.coffiebot/workspace/sessions/*.jsonl
```

如果接近或超过 500MB，下次消息到达时会自动触发清理。可手动调整 `session_max_file_size_mb` 降低阈值。

### Consolidation 一直失败

检查 OpenViking/Mem0 是否可用：
```bash
curl http://openviking:1933/health  # OpenViking
curl http://mem0:9356/health        # Mem0
```

如果不可用，设置 `enabled: false` 禁用，Agent 会在无记忆的模式下继续运行。

### 消息在 LLM 看不到

验证 `last_consolidated` 是否正确推进：
```bash
# 查看会话文件第一行（metadata）
head -1 ~/.coffiebot/workspace/sessions/*.jsonl | grep last_consolidated
```

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 PR！
