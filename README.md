# maibot-maid-adapter

`maibot-maid-adapter` 是 [MaiBot](https://github.com/mai-with-u/MaiBot) 连接 [MaidBridge](https://github.com/litroenade/MaidBridge) 的插件。它负责连到 Minecraft 侧的 MaidBridge，让 MaiBot 接管 Touhou Little Maid 女仆聊天，也可以接收和回复 Minecraft 服务器群聊。

## 使用前确认

- Minecraft 侧已经安装 MaidBridge。
- MaidBridge 的 WebSocket 已经打开。
- MaiBot 能正常加载插件。
- 要接管女仆聊天时，已经拿到女仆 UUID。
- ```
  /give @p maidbridge:maid_uuid_probe
  ```

暂时使用上述指令获取工具右击女仆进行获取女仆uuid行为

## 女仆聊天流程

1. 玩家和女仆说话。
2. MaidBridge 把这一轮女仆聊天发给插件。
3. 插件把上下文交给 MaiBot。
4. MaiBot 生成回复和可选动作。
5. 插件发回 MaidBridge。
6. Minecraft 里显示女仆回复。

如果 `maid_uuid` 没填，插件不会接管具体女仆。

## Prompt

提示词在：

```text
prompts/zh-CN/
```

| 文件                       | 用途                                  |
| -------------------------- | ------------------------------------- |
| `planner_context.prompt` | 告诉 MaiBot 当前是 Minecraft 女仆场景 |
| `reply_generator.prompt` | 整理女仆最终回复                      |
| `action_planner.prompt`  | 决定是否请求女仆动作                  |

改完 prompt 后，下一轮女仆回合会重新读取。

## 常见问题

| 问题             | 先检查                                                                          |
| ---------------- | ------------------------------------------------------------------------------- |
| 插件没启动       | `[plugin].enabled = true`，WebUI 是否保存并重载                               |
| 连接失败         | `server_uri`、MaidBridge 是否启动、端口、防火墙、Token                        |
| 女仆没被接管     | `maid_uuid`、`enable_maid_agent_turns`、MaidBridge 的 `maidAgentTurnMode` |
| 回复超时         | `request_timeout_ms`、LLM 响应速度、Minecraft 日志里的 `bridge.error`       |
| 表情没显示       | MaidBridge 的 `enableExternalAgentEmoji`，两侧消息大小上限                    |
| 服务器群聊没回写 | `enableServerChatBridge` 和 `enableExternalServerChatMessages`              |

Minecraft 侧排查命令：

```text
/maidbridge summary
/maidbridge ws
/maidbridge chat 20
/maidbridge turns 20
```

## 特别鸣谢

[Mai-with-u/MaiBot: MaiSaka, an LLM-based intelligent agent, is a digital lifeform devoted to understanding you and interacting in the style of a real human. She does not pursue perfection, nor does she seek efficiency; instead, she values warmth, authenticity, and genuine connection.](https://github.com/Mai-with-u/MaiBot)

[TartaricAcid/TouhouLittleMaid: A minecraft forge mod about the maid](https://github.com/TartaricAcid/TouhouLittleMaid)

[17TheWord/QueQiao: Minecraft 服务端 Mod/Plugin，实时接收玩家事件、API广播消息。](https://github.com/17TheWord/QueQiao?tab=MIT-1-ov-file)

## 开源协议

本项目采用 [GPL-v3.0](LICENSE) 协议开源
