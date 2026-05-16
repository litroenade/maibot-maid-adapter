# maibot-maid-adapter

MaiBot 女仆适配器插件。它负责 MaidBridge WebSocket 运行时、TouhouLittleMaid 女仆回合接管、女仆查询/调用 API，以及女仆消息观察。

协议命名保持固定：

- Protocol：`maidbridge.maid`
- Java 侧域名：`MaidBridge`
- Python 帧模型：`BridgeFrame`

## 详细文档

- [交互与目录结构](docs/interaction-and-structure.md)：Java ↔ Python 所有主要帧、MaiBot 插入点、当前文件结构。

## 目录结构

```text
maibot-maid-adapter/
├── plugin.py              # MaiBot 插件注册入口
├── config.py              # WebUI 配置定义和运行时 settings
├── prompts/zh-CN/         # 可热更新的 planner / reply / action 提示词
└── src/
    ├── adapter.py         # 插件 mixin 组合入口
    ├── connection.py      # WebSocket 生命周期、重连、session initialize
    ├── api.py             # 对外公开 API
    ├── maid_turn/         # 外部女仆回合接管
    ├── protocol/          # MaidBridge 帧构造、查询帧和路由判定
    ├── runtime/           # 入站帧分发、pending request 和输出事件
    └── transport/         # WebSocket 传输实现
```

新增外部接管实现时优先看 `src/maid_turn/README.md`。`maid_turn/` 只关心“MaiBot 一轮回复如何被外部接管并最终提交”，协议和连接细节留在 `protocol/`、`runtime/`、`transport/`。

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
config_version = "0.3.10"

[maid_adapter]
enable_maid_agent_turns = true
maid_uuid = ""
server_id = "minecraft-local"
agent_id = "maibot"
server_uri = "ws://127.0.0.1:8765/maidbridge"
access_token = ""
max_message_bytes = 33554432
request_timeout_ms = 30000
reconnect_initial_delay_ms = 1000
reconnect_max_delay_ms = 30000
max_pending_maid_agent_turns = 256
reply_generation_model = "replyer"
reply_generation_temperature = 0.4
reply_generation_max_tokens = 256
enable_agent_actions = true
enable_agent_emoji_bubbles = true
action_planning_model = ""
action_planning_temperature = 0.1
action_planning_max_tokens = 256
```

需要接入 Minecraft 里的 MaidBridge mod 时，确认 `[plugin].enabled = true`，并确认 `server_uri`、`access_token` 和 Java 侧 MaidBridge 配置一致。关闭后插件仍会注册 API，但不会启动 WebSocket 运行时。

`maid_uuid` 填要接管的女仆 UUID。`server_id` 是连接 ID，单个 MaiBot 实例保持默认即可。外部接管时的显示名优先使用 MaiBot 全局配置 `bot.nickname`，没有设置昵称时使用 `agent_id`。

MaiBot 原生 `send_emoji` 会在外部接管回合内直桥为 Minecraft 女仆图片气泡，不再进入 MaiBot 普通发送服务。静态图片统一转为 PNG，GIF 动图会原样桥接，其他动图会转为 GIF 后桥接给客户端动态纹理。Java 端仍会校验外部表情包开关、PNG/GIF 格式与声明尺寸，最终载荷大小由 `max_message_bytes` 控制。`enable_agent_emoji_bubbles` 控制的是 TLM 本地随机表情或颜文字气泡，不影响 `send_emoji` 外部图片桥接。

## 公开 API

- `status`：查看运行时、协议和传输状态。
- `pending_requests`：查看等待 Java 领域响应或握手响应的请求。
- `maid_query`：发送 MaidBridge 查询帧。
- `maid_call`：发送 MaidBridge 调用帧。
- `registry_catalog`、`registry_list`、`registry_get`、`registry_search`、`endpoints`：查看 MaidBridge registry 能力。
