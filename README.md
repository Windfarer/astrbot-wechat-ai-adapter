# astrbot-wechat-ai-adapter

这是一个面向 [ChisBread/wechat-ai](https://github.com/ChisBread/wechat-ai) 的 AstrBot 平台适配器插件，使用 wechat-ai 项目提供的 MCP 接口作为传输桥接层。

## 功能范围

- 通过 MCP 的 `get_recent_chats` 和 `get_recent_messages` 轮询 `wechat-ai` 的入站消息
- 使用本地持久化状态文件对轮询到的消息进行去重
- 通过 MCP 发送文本、群聊 `@`、图片和文件消息
- 首个版本暂不包含高风险的群管理类操作

## 运行要求

- Python 3.11 及以上版本
- 支持平台适配器插件的 AstrBot 运行环境
- 一个正在运行且暴露了 MCP 接口的 `wechat-ai` 实例，例如 `http://localhost:8100/mcp`
- 有效的 `WECHAT_AI_MCP_TOKEN`
- 如果需要发送图片，适配器进程和 `wechat-ai` 进程必须共享一个双方都可读的目录，例如 `/config/exports`

## 仓库结构

- `metadata.yaml`：AstrBot 插件清单文件
- `main.py`：插件入口，负责导入并注册平台适配器
- `wechat_ai_platform_adapter.py`：轮询式适配器的主体实现
- `wechat_ai_platform_event.py`：出站消息事件实现
- `mcp_client.py`：官方 MCP SDK 的客户端封装
- `polling_state.py`：用于消息去重的持久化状态存储

## 平台配置示例

```json
{
  "type": "wechat_ai",
  "enable": true,
  "id": "wechat_ai_main",
  "mcp_url": "http://localhost:8100/mcp",
  "mcp_token": "replace-with-real-token",
  "mcp_timeout_seconds": 30,
  "poll_interval_seconds": 5,
  "recent_chats_limit": 20,
  "recent_messages_limit": 30,
  "state_path": "data/plugins/astrbot-wechat-ai-adapter/state.json",
  "include_non_text": true,
  "parse_media": true,
  "shared_media_dir": "/config/exports",
  "wake_all_group_messages": false
}
```

## 说明

- `mcp_token` 不应使用 `wechat-ai` 文档中给出的不安全默认值
- 出站群聊中的 `@` 会映射到 MCP `send_text_msg.at_user_name`；适配器不会把 `@name` 直接拼接进文本内容
- 入站群消息只有在表现为 `@机器人昵称 ...`、包含类似 `is_at_me` 的字段，或启用了 `wake_all_group_messages` 时，才会触发 AstrBot 回复
- 当前版本不会自动下载远程图片 URL；如需发送图片，请先将文件放到 `shared_media_dir` 中
- 状态文件会按会话在有上限的窗口内追加保存指纹，避免重启后重复消费同一批消息

## 已知限制

- 适配器依赖 `wechat-ai` MCP 工具返回结果的具体字段结构。如果你的部署返回了不同的消息 schema，可能需要调整 `wechat_ai_platform_adapter.py` 中的映射逻辑。
- 当前仅在入站媒体消息带有可读取的本地路径时，才会将其转换为图片消息组件。
- 出站发送不会做额外的联系人自动解析，只会使用入站事件中携带的 `session_id` 或 `contact_name`。

## 许可证

本项目使用 `GPL-3.0-or-later` 许可证，完整文本见仓库根目录下的 `LICENSE` 文件。