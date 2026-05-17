from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase

SUPPORTED_CONFIG_VERSION = "0.3.11"
DEFAULT_SERVER_ID = "minecraft-local"
DEFAULT_AGENT_ID = "麦麦"
DEFAULT_JAVA_SERVER_URI = "ws://127.0.0.1:8765/maidbridge"
DEFAULT_CLIENT_ROLES = ["maid_api_query", "maid_api_call", "debug"]
SERVER_CHAT_CLIENT_ROLE = "message"
DEFAULT_SUBSCRIPTIONS = [
    "maid.ai.*",
    "maid.api.registry.*",
    "bridge.session.ready",
    "maidbridge.server.*",
]
DEFAULT_REQUEST_TIMEOUT_MS = 30000
DEFAULT_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MAX_MESSAGE_BYTES_LIMIT = 64 * 1024 * 1024
DEFAULT_MAX_PENDING_MAID_AGENT_TURNS = 256
DEFAULT_RECONNECT_INITIAL_DELAY_MS = 1000
DEFAULT_RECONNECT_MAX_DELAY_MS = 30000


class MaidAdapterPluginOptions(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=False,
        description="是否启用 MaiBot 女仆适配器运行时。",
        json_schema_extra={
            "label": "启用插件",
            "hint": "关闭后不会连接 Minecraft。",
            "order": 0,
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置结构版本。",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )


class MaidAdapterConnectionConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "女仆接管"
    __ui_icon__: ClassVar[str] = "bot"
    __ui_order__: ClassVar[int] = 1

    enable_maid_agent_turns: bool = Field(
        default=True,
        json_schema_extra={
            "label": "接管女仆聊天",
            "order": 0,
        },
    )
    maid_uuid: str = Field(
        default="",
        description="要接管的女仆 UUID，同时作为 minecraft 平台路由账号。",
        json_schema_extra={
            "label": "女仆 UUID",
            "hint": "插件会用该 UUID 注册 minecraft 路由状态。",
            "order": 1,
            "placeholder": "填写女仆 UUID",
        },
    )
    server_id: str = Field(
        default=DEFAULT_SERVER_ID,
        description="MaiBot 侧用于区分这个 Minecraft 服务器的账号标识。",
        json_schema_extra={
            "label": "服务器标识符",
            "hint": "服务器群聊会用它作为 minecraft 平台账号；女仆接管仍用女仆 UUID 路由。",
            "order": 3,
            "placeholder": DEFAULT_SERVER_ID,
        },
    )
    agent_id: str = Field(
        default=DEFAULT_AGENT_ID,
        description="外部接管方的稳定协议标识符。",
        json_schema_extra={
            "label": "MaiBot 显示名称",
            "hint": "Minecraft 里显示的外部发言人名称，可以和女仆实体名不同。",
            "order": 4,
            "placeholder": DEFAULT_AGENT_ID,
        },
    )
    server_uri: str = Field(
        default=DEFAULT_JAVA_SERVER_URI,
        description="Minecraft 侧 MaidBridge 的连接地址。",
        json_schema_extra={
            "label": "MaidBridge 地址",
            "order": 6,
            "placeholder": DEFAULT_JAVA_SERVER_URI,
        },
    )
    access_token: str = Field(
        default="",
        description="连接需要的访问令牌。",
        json_schema_extra={
            "label": "访问令牌",
            "hint": "没有启用鉴权就留空。",
            "input_type": "password",
            "order": 7,
            "placeholder": "可选",
        },
    )
    max_message_bytes: int = Field(
        default=DEFAULT_MAX_MESSAGE_BYTES,
        ge=1024,
        le=MAX_MESSAGE_BYTES_LIMIT,
        description="单条 MaidBridge 消息允许的最大字节数。",
        json_schema_extra={
            "label": "单条消息大小上限",
            "hint": "外部高清表情包和 GIF 会占用较大的帧空间，需要与 Java 侧保持一致。",
            "order": 8,
        },
    )
    request_timeout_ms: int = Field(
        default=DEFAULT_REQUEST_TIMEOUT_MS,
        ge=1000,
        le=300000,
        description="等待 Java 端回复的最长时间。",
        json_schema_extra={
            "label": "请求超时毫秒",
            "hint": "网络较慢时可以调大。",
            "order": 9,
        },
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
    max_pending_maid_agent_turns: int = Field(
        default=DEFAULT_MAX_PENDING_MAID_AGENT_TURNS,
        ge=1,
        le=4096,
        description="同一时间允许保留的女仆消息处理数量。",
        json_schema_extra={
            "label": "待处理消息上限",
            "hint": "防止异常堆积，一般保持默认。",
            "order": 13,
        },
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
    def reconnect_initial_delay_ms(self) -> int:
        return self.maid_adapter.reconnect_initial_delay_ms

    @property
    def reconnect_max_delay_ms(self) -> int:
        return self.maid_adapter.reconnect_max_delay_ms

    @property
    def max_pending_maid_agent_turns(self) -> int:
        return self.maid_adapter.max_pending_maid_agent_turns

    @property
    def client_roles(self) -> list[str]:
        roles = list(DEFAULT_CLIENT_ROLES)
        roles.insert(0, SERVER_CHAT_CLIENT_ROLE)
        if self.enable_maid_agent_turns and self.maid_uuid:
            return ["agent", *roles]
        return roles

    @property
    def subscriptions(self) -> list[str]:
        subscriptions = list(DEFAULT_SUBSCRIPTIONS)
        if self.enable_maid_agent_turns and self.maid_uuid:
            return ["maid.agent.turn.request", *subscriptions]
        return subscriptions

    @property
    def enable_maid_agent_turns(self) -> bool:
        return self.maid_adapter.enable_maid_agent_turns

    @property
    def maid_uuid(self) -> str:
        return self.maid_adapter.maid_uuid.strip()
