# maibot-maid-adapter

MaiBot 女仆适配器插件。它负责 MaidBridge WebSocket 运行时、TouhouLittleMaid 女仆 agent 接管、女仆查询/调用 API，以及女仆消息观察。

协议命名保持固定：

- Protocol：`maidbridge.maid`
- Java 侧域名：`MaidBridge`
- Python 帧模型：`BridgeFrame`

## 详细文档

- [交互与目录结构](docs/interaction-and-structure.md)：Java ↔ Python 所有主要帧、MaiBot 插入点、当前文件结构。

## Prompt 文件

可编辑 prompt 位于 `prompts/zh-CN/`，格式与 MaiBot 本体 `prompts/<locale>/*.prompt` 保持一致：

- `planner_context.prompt`：注入 MaiSaka planner 的 MaidBridge 上下文提示。
- `reply_generator.prompt`：外部接管回复生成器提示。
- `action_planner.prompt`：Java actions 动作规划器提示。

插件每次构造请求时都会重新读取这些文件，修改后下一轮女仆回合即可生效。

## 配置

```toml
[plugin]
enabled = false
config_version = "0.3.2"

[maid_adapter]
enable_maid_agent_turns = true
default_maid_uuid = ""
maid_channel_name = "maid"
server_id = "minecraft-local"
enable_message_out_events = false
server_uri = "ws://127.0.0.1:8765/maidbridge"
access_token = ""
max_message_bytes = 32768
request_timeout_ms = 30000
reconnect_max_attempts = 5
reconnect_initial_delay_ms = 1000
reconnect_max_delay_ms = 30000
reply_generation_model = "replyer"
reply_generation_temperature = 0.4
reply_generation_max_tokens = 256
enable_agent_actions = true
enable_agent_emoji_bubbles = true
action_planning_model = ""
action_planning_temperature = 0.1
action_planning_max_tokens = 256
```

需要接入 Minecraft 里的 MaidBridge mod 时，确认 `[plugin].enabled = true`，并确认 Java 侧 WebSocket 服务地址和 token 一致。关闭后插件仍会注册，但不会启动 WebSocket 运行时。

## 公开 API

- `status`：查看运行时、协议和传输状态。
- `pending_requests`：查看等待 Java 领域响应或握手响应的请求。
- `maid_query`：发送 MaidBridge 查询帧。
- `maid_call`：发送 MaidBridge 调用帧。
- `maid_message`：发送 `maid.message.in` 到 Minecraft / TouhouLittleMaid。
- `registry_catalog`、`registry_list`、`registry_get`、`registry_search`、`endpoints`：查看 MaidBridge registry 能力。
