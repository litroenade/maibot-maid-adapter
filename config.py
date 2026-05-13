from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase

SUPPORTED_CONFIG_VERSION = "0.3.3"
DEFAULT_SERVER_ID = "minecraft-local"
DEFAULT_AGENT_ID = "maibot"
DEFAULT_JAVA_SERVER_URI = "ws://127.0.0.1:8765/maidbridge"
DEFAULT_MAID_CHANNEL_NAME = "maid"
DEFAULT_CLIENT_ROLES = ["maid_api_query", "maid_api_call", "debug"]
DEFAULT_SUBSCRIPTIONS = [
    "maid.ai.*",
    "maid.api.registry.*",
    "bridge.session.ready",
    "maidbridge.server.*",
]
DEFAULT_REQUEST_TIMEOUT_MS = 30000
DEFAULT_MAX_MESSAGE_BYTES = 32768
DEFAULT_RECONNECT_MAX_ATTEMPTS = 5
DEFAULT_RECONNECT_INITIAL_DELAY_MS = 1000
DEFAULT_RECONNECT_MAX_DELAY_MS = 30000
DEFAULT_REPLY_GENERATION_MODEL = "replyer"
DEFAULT_REPLY_GENERATION_TEMPERATURE = 0.4
DEFAULT_REPLY_GENERATION_MAX_TOKENS = 256
DEFAULT_ACTION_PLANNING_TEMPERATURE = 0.1
DEFAULT_ACTION_PLANNING_MAX_TOKENS = 256


class MaidAdapterPluginOptions(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=False,
        description="是否启用 MaiBot 女仆适配器运行时。",
        json_schema_extra={
            "label": "启用插件",
            "hint": "关闭后插件仍会注册 API，但不会启动 MaidBridge WebSocket 运行时。",
            "order": 0,
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置结构版本。",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )


class MaidAdapterConnectionConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "MaidAdapter"
    __ui_icon__: ClassVar[str] = "bot"
    __ui_order__: ClassVar[int] = 1

    enable_maid_agent_turns: bool = Field(
        default=True,
        description="是否处理 MaidBridge 发来的 maid.agent.turn.request。",
        json_schema_extra={"label": "启用女仆 agent 接管", "order": 0},
    )
    default_maid_uuid: str = Field(
        default="",
        description="maid_query、maid_call、maid_message 未显式传 maid.uuid 时使用的默认女仆 UUID。",
        json_schema_extra={"label": "默认女仆 UUID", "order": 1, "placeholder": "可选"},
    )
    maid_channel_name: str = Field(
        default=DEFAULT_MAID_CHANNEL_NAME,
        description="兼容旧版配置；外部接管会话显示名优先使用 MaiBot 本体昵称。",
        json_schema_extra={"label": "兼容女仆频道名", "order": 2, "placeholder": DEFAULT_MAID_CHANNEL_NAME},
    )
    server_id: str = Field(
        default=DEFAULT_SERVER_ID,
        description="适配器向 Java MaidBridge 声明的客户端服务 ID。",
        json_schema_extra={"label": "客户端服务 ID", "order": 3, "placeholder": DEFAULT_SERVER_ID},
    )
    agent_id: str = Field(
        default=DEFAULT_AGENT_ID,
        description="适配器向 Java MaidBridge 声明的兼容标识；MaiBot 本体昵称读取失败时作为回退值。",
        json_schema_extra={
            "label": "兼容 agent 标识",
            "hint": "游戏内显示名优先取 MaiBot 全局配置 bot.nickname；这里主要用于连接调试和回退。",
            "order": 4,
            "placeholder": DEFAULT_AGENT_ID,
        },
    )
    enable_message_out_events: bool = Field(
        default=False,
        description="是否订阅 maid.message.out 观察事件。",
        json_schema_extra={
            "label": "启用女仆输出事件",
            "hint": "这里只观察 MaidBridge maid.message.out 帧，不会调用其他适配器插件。",
            "order": 5,
        },
    )
    server_uri: str = Field(
        default=DEFAULT_JAVA_SERVER_URI,
        description="Java MaidBridge WebSocket 服务地址。",
        json_schema_extra={
            "label": "Java MaidBridge 地址",
            "order": 6,
            "placeholder": DEFAULT_JAVA_SERVER_URI,
        },
    )
    access_token: str = Field(
        default="",
        description="和 Java MaidBridge 共用的 Bearer Token。",
        json_schema_extra={"label": "访问令牌", "input_type": "password", "order": 7, "placeholder": "可选"},
    )
    max_message_bytes: int = Field(
        default=DEFAULT_MAX_MESSAGE_BYTES,
        ge=1024,
        le=1048576,
        description="单个 WebSocket frame 最大字节数。",
        json_schema_extra={
            "label": "最大消息字节数",
            "hint": "需要和 Java MaidBridge 的 maxBridgeMessageBytes 保持兼容。",
            "order": 8,
        },
    )
    request_timeout_ms: int = Field(
        default=DEFAULT_REQUEST_TIMEOUT_MS,
        ge=1000,
        le=300000,
        description="等待 Java 领域响应或握手响应的超时时间。",
        json_schema_extra={
            "label": "请求超时毫秒",
            "hint": "用于 bridge.session.ready、maid.api.response、maid.message.response 等需要响应的请求。",
            "order": 9,
        },
    )
    reconnect_max_attempts: int = Field(
        default=DEFAULT_RECONNECT_MAX_ATTEMPTS,
        ge=0,
        description="WebSocket 启动失败或断开后的后台重连次数上限。",
        json_schema_extra={"label": "最大重连次数", "order": 10},
    )
    reconnect_initial_delay_ms: int = Field(
        default=DEFAULT_RECONNECT_INITIAL_DELAY_MS,
        ge=0,
        description="第一次后台重连前的等待时间。",
        json_schema_extra={"label": "初始重连延迟毫秒", "order": 11},
    )
    reconnect_max_delay_ms: int = Field(
        default=DEFAULT_RECONNECT_MAX_DELAY_MS,
        ge=0,
        description="指数退避重连等待时间上限。",
        json_schema_extra={"label": "最大重连延迟毫秒", "order": 12},
    )
    reply_generation_model: str = Field(
        default=DEFAULT_REPLY_GENERATION_MODEL,
        description="外部接管回复生成使用的 MaiBot LLM 任务名；留空时使用宿主默认任务。",
        json_schema_extra={"label": "回复生成模型", "order": 13, "placeholder": DEFAULT_REPLY_GENERATION_MODEL},
    )
    reply_generation_temperature: float = Field(
        default=DEFAULT_REPLY_GENERATION_TEMPERATURE,
        ge=0,
        le=2,
        description="外部接管回复生成温度。",
        json_schema_extra={"label": "回复生成温度", "order": 14},
    )
    reply_generation_max_tokens: int = Field(
        default=DEFAULT_REPLY_GENERATION_MAX_TOKENS,
        ge=32,
        le=2048,
        description="外部接管回复生成最大 token 数。",
        json_schema_extra={"label": "回复生成最大 token", "order": 15},
    )
    enable_agent_actions: bool = Field(
        default=True,
        description="是否允许外部接管回合把动作决策写回 Java actions。",
        json_schema_extra={
            "label": "启用女仆动作回写",
            "hint": "动作只在当前 pending 女仆回合内生效，不走 MaiBot 普通发送服务。",
            "order": 16,
        },
    )
    enable_agent_emoji_bubbles: bool = Field(
        default=True,
        description="是否允许动作规划器按外部 agent 意愿请求 Java 侧附加女仆表情气泡。",
        json_schema_extra={
            "label": "启用表情气泡规划",
            "hint": "只有 Java MaidBridge 暴露 show_emoji_bubble action 时才会实际生效。",
            "order": 17,
        },
    )
    action_planning_model: str = Field(
        default="",
        description="外部接管动作规划使用的 MaiBot LLM 任务名；留空时复用回复生成模型。",
        json_schema_extra={"label": "动作规划模型", "order": 18, "placeholder": "留空复用回复生成模型"},
    )
    action_planning_temperature: float = Field(
        default=DEFAULT_ACTION_PLANNING_TEMPERATURE,
        ge=0,
        le=2,
        description="外部接管动作规划温度。",
        json_schema_extra={"label": "动作规划温度", "order": 19},
    )
    action_planning_max_tokens: int = Field(
        default=DEFAULT_ACTION_PLANNING_MAX_TOKENS,
        ge=32,
        le=2048,
        description="外部接管动作规划最大 token 数。",
        json_schema_extra={"label": "动作规划最大 token", "order": 20},
    )


class MaiBotMaidAdapterSettings(PluginConfigBase):
    plugin: MaidAdapterPluginOptions = Field(default_factory=MaidAdapterPluginOptions)
    maid_adapter: MaidAdapterConnectionConfig = Field(default_factory=MaidAdapterConnectionConfig)

    @property
    def config_version(self) -> str:
        return self.plugin.config_version

    @property
    def enabled(self) -> bool:
        return self.plugin.enabled

    @property
    def server_id(self) -> str:
        return self.maid_adapter.server_id.strip() or DEFAULT_SERVER_ID

    @property
    def agent_id(self) -> str:
        return self.maid_adapter.agent_id.strip() or DEFAULT_AGENT_ID

    @property
    def websocket_role(self) -> str:
        return "client"

    @property
    def websocket_url(self) -> str:
        return self.maid_adapter.server_uri.strip() or DEFAULT_JAVA_SERVER_URI

    @property
    def access_token(self) -> str:
        return self.maid_adapter.access_token

    @property
    def max_message_bytes(self) -> int:
        return self.maid_adapter.max_message_bytes

    @property
    def request_timeout_ms(self) -> int:
        return self.maid_adapter.request_timeout_ms

    @property
    def reconnect_max_attempts(self) -> int:
        return self.maid_adapter.reconnect_max_attempts

    @property
    def reconnect_initial_delay_ms(self) -> int:
        return self.maid_adapter.reconnect_initial_delay_ms

    @property
    def reconnect_max_delay_ms(self) -> int:
        return self.maid_adapter.reconnect_max_delay_ms

    @property
    def client_roles(self) -> list[str]:
        roles: list[str] = []
        if self.enable_maid_agent_turns:
            roles.append("agent")
        if self.enable_message_out_events:
            roles.append("message")
        return _normalized_unique(roles, DEFAULT_CLIENT_ROLES)

    @property
    def subscriptions(self) -> list[str]:
        subscriptions: list[str] = []
        if self.enable_maid_agent_turns:
            subscriptions.append("maid.agent.turn.request")
        subscriptions.extend(DEFAULT_SUBSCRIPTIONS)
        if self.enable_message_out_events:
            subscriptions.append("maid.message.out")
        return _normalized_unique(subscriptions)

    @property
    def enable_message_out_events(self) -> bool:
        return self.maid_adapter.enable_message_out_events

    @property
    def enable_maid_agent_turns(self) -> bool:
        return self.maid_adapter.enable_maid_agent_turns

    @property
    def default_maid_uuid(self) -> str:
        return self.maid_adapter.default_maid_uuid.strip()

    @property
    def maid_channel_name(self) -> str:
        return self.maid_adapter.maid_channel_name.strip() or DEFAULT_MAID_CHANNEL_NAME

    @property
    def reply_generation_model(self) -> str:
        return self.maid_adapter.reply_generation_model.strip()

    @property
    def reply_generation_temperature(self) -> float:
        return self.maid_adapter.reply_generation_temperature

    @property
    def reply_generation_max_tokens(self) -> int:
        return self.maid_adapter.reply_generation_max_tokens

    @property
    def enable_agent_actions(self) -> bool:
        return self.maid_adapter.enable_agent_actions

    @property
    def enable_agent_emoji_bubbles(self) -> bool:
        return self.maid_adapter.enable_agent_emoji_bubbles

    @property
    def action_planning_model(self) -> str:
        return self.maid_adapter.action_planning_model.strip() or self.reply_generation_model

    @property
    def action_planning_temperature(self) -> float:
        return self.maid_adapter.action_planning_temperature

    @property
    def action_planning_max_tokens(self) -> int:
        return self.maid_adapter.action_planning_max_tokens


def _normalized_unique(*groups: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
    return result
