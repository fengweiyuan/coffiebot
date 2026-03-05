"""
一次性迁移脚本：将 MEMORY.md 内容提交到 OpenViking。

在服务器上执行：
    docker exec coffiebot-gateway python /app/scripts/migrate_memory_to_ov.py

或本地通过 SSH 端口转发执行：
    python scripts/migrate_memory_to_ov.py --server-url http://your-server:8890
"""

import asyncio
import argparse
import json
import sys
from pathlib import Path


async def migrate(server_url: str, api_key: str, agent_id: str, user_id: str, memory_file: Path) -> None:
    """
    将 MEMORY.md 内容作为一轮对话提交到 OpenViking，触发记忆提取。

    Params:
        server_url (str): OpenViking 服务地址
        api_key (str): API 认证密钥
        agent_id (str): OV account/agent 标识
        user_id (str): OV user 标识
        memory_file (Path): MEMORY.md 文件路径
    """
    # 延迟导入，支持在项目外执行时回退到 httpx 直接调用
    try:
        from coffiebot.openviking.client import OpenVikingClient
    except ImportError:
        print("coffiebot 包不可用，使用 httpx 直接调用")
        await _migrate_raw(server_url, api_key, agent_id, user_id, memory_file)
        return

    if not memory_file.exists():
        print(f"文件不存在: {memory_file}")
        sys.exit(1)

    content = memory_file.read_text(encoding="utf-8").strip()
    if not content:
        print("MEMORY.md 为空，无需迁移")
        return

    print(f"读取 MEMORY.md: {len(content)} 字符")

    client = OpenVikingClient(
        server_url=server_url,
        api_key=api_key,
        agent_id=agent_id,
        user_id=user_id,
        timeout=120.0,
    )

    try:
        # 健康检查
        if not await client.health():
            print("OpenViking 服务不可用")
            sys.exit(1)
        print("OpenViking 连接正常")

        # 创建会话
        session_id = await client.create_session()
        print(f"创建会话: {session_id}")

        # 将 MEMORY.md 内容按章节拆分，作为多条 user 消息提交
        sections = _split_sections(content)
        for index, section in enumerate(sections):
            if section.strip():
                await client.add_message(session_id, "user", section)
                print(f"  提交第 {index + 1}/{len(sections)} 段: {len(section)} 字符")

        # 提交触发记忆提取
        print("提交会话，等待 OV 记忆提取（可能需要 30-60 秒）...")
        result = await client.commit_session(session_id)
        print(f"迁移完成: {json.dumps(result, ensure_ascii=False, indent=2)}")

    finally:
        await client.close()


async def _migrate_raw(server_url: str, api_key: str, agent_id: str, user_id: str, memory_file: Path) -> None:
    """
    不依赖 coffiebot 包的原始 httpx 实现。

    Params:
        server_url (str): OpenViking 服务地址
        api_key (str): API 认证密钥
        agent_id (str): OV account/agent 标识
        user_id (str): OV user 标识
        memory_file (Path): MEMORY.md 文件路径
    """
    import httpx

    if not memory_file.exists():
        print(f"文件不存在: {memory_file}")
        sys.exit(1)

    content = memory_file.read_text(encoding="utf-8").strip()
    if not content:
        print("MEMORY.md 为空")
        return

    print(f"读取 MEMORY.md: {len(content)} 字符")

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "X-OpenViking-Account": agent_id,
        "X-OpenViking-User": user_id,
        "X-OpenViking-Agent": agent_id,
    }

    async with httpx.AsyncClient(base_url=server_url.rstrip("/"), headers=headers, timeout=120.0) as client:
        # 健康检查
        response = await client.get("/health")
        if response.json().get("status") != "ok":
            print("OpenViking 服务不可用")
            sys.exit(1)
        print("OpenViking 连接正常")

        # 创建会话
        response = await client.post("/api/v1/sessions", json={})
        data = response.json()
        session_id = data["result"]["session_id"]
        print(f"创建会话: {session_id}")

        # 提交内容
        sections = _split_sections(content)
        for index, section in enumerate(sections):
            if section.strip():
                await client.post(
                    f"/api/v1/sessions/{session_id}/messages",
                    json={"role": "user", "content": section},
                )
                print(f"  提交第 {index + 1}/{len(sections)} 段: {len(section)} 字符")

        # 提交触发记忆提取
        print("提交会话，等待 OV 记忆提取...")
        response = await client.post(f"/api/v1/sessions/{session_id}/commit", json={})
        result = response.json()
        print(f"迁移完成: {json.dumps(result, ensure_ascii=False, indent=2)}")


def _split_sections(content: str) -> list[str]:
    """
    按 Markdown 二级标题拆分内容，每段不超过 6000 字符。

    Params:
        content (str): MEMORY.md 全文

    Returns:
        list[str]: 拆分后的段落列表
    """
    lines = content.split("\n")
    sections: list[str] = []
    current: list[str] = []

    for line in lines:
        if line.startswith("## ") and current:
            sections.append("\n".join(current))
            current = []
        current.append(line)

    if current:
        sections.append("\n".join(current))

    # 过长的段再按字符数拆分
    result: list[str] = []
    for section in sections:
        if len(section) <= 6000:
            result.append(section)
        else:
            # 按 6000 字符拆分
            for start in range(0, len(section), 6000):
                result.append(section[start:start + 6000])

    return [section for section in result if section.strip()]


def main() -> None:
    """脚本入口。"""
    parser = argparse.ArgumentParser(description="将 MEMORY.md 迁移到 OpenViking")
    parser.add_argument("--server-url", default="http://your-server:8890", help="OpenViking 服务地址")
    parser.add_argument("--api-key", required=True, help="OpenViking API Key")
    parser.add_argument("--agent-id", default="coffiebot_team")
    parser.add_argument("--user-id", default="coffiebot_user")
    parser.add_argument("--memory-file", default=str(Path.home() / ".coffiebot/workspace/memory/MEMORY.md"))
    arguments = parser.parse_args()

    asyncio.run(migrate(
        server_url=arguments.server_url,
        api_key=arguments.api_key,
        agent_id=arguments.agent_id,
        user_id=arguments.user_id,
        memory_file=Path(arguments.memory_file),
    ))


if __name__ == "__main__":
    main()
