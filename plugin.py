import asyncio
from collections.abc import Iterable
from typing import Any, ClassVar

from maibot_sdk import MaiBotPlugin, ON_BOT_CONFIG_RELOAD, PluginConfigBase

from .config import MaiBotMaidAdapterSettings
from .src.api import MaidApi
from .src.connection import MaidBridgeConnection
from .src.maid_turn.hooks import MaidPlannerHooks
from .src.maid_turn.outbound_gateway import MaidGateway
from .src.runtime.runtime_router import RuntimeRouter
from .src.runtime.state import BridgeRuntimeState
from .src.server_chat import ServerChatGateway, ServerChatPlannerHooks, ServerChatService


class MaiBotMaidAdapterPlugin(
    MaidPlannerHooks,
    MaidGateway,
    ServerChatPlannerHooks,
    ServerChatGateway,
    MaidApi,
    MaidBridgeConnection,
    MaiBotPlugin,
):
    config_model: ClassVar[type[PluginConfigBase] | None] = MaiBotMaidAdapterSettings
    config_reload_subscriptions: ClassVar[Iterable[str]] = (ON_BOT_CONFIG_RELOAD,)

    def __init__(self) -> None:
        super().__init__()
        self._state = BridgeRuntimeState()
        self._transport: Any | None = None
        self._router: RuntimeRouter | None = None
        self._maid_turn_service: Any | None = None
        self._server_chat_service: Any | None = None
        self._runtime_task: asyncio.Task[None] | None = None
        self._runtime_stop_event: asyncio.Event | None = None
        self._runtime_closed_event: asyncio.Event | None = None
        self._runtime_generation = 0
        self._reconnect_attempts = 0
        self._bot_name = ""

    async def on_load(self) -> None:
        settings = self._settings()
        if not settings.enabled:
            await self._stop_runtime()
            self.ctx.logger.info("MaiBot 女仆适配器已关闭，不启动运行时")
            await self._publish_adapter_state(runtime_connected=False, metadata={"enabled": False})
            return
        await self._start_runtime(settings)

    async def on_unload(self) -> None:
        self.ctx.logger.info("正在停止 MaiBot 女仆适配器运行时")
        await self._stop_runtime()
        self._state.mark_disconnected()
        await self._publish_adapter_state(runtime_connected=False, metadata={"reason": "plugin_unload"})
        self.ctx.logger.info("MaiBot 女仆适配器运行时已停止")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == ON_BOT_CONFIG_RELOAD:
            settings = self._settings()
            old_name = self._bot_name.strip()
            new_name = self._bot_name_from_config(config_data) or await self._resolve_bot_name(settings)
            if new_name == old_name:
                return
            self.ctx.logger.info(f"MaiBot 本体昵称已更新，正在刷新 MaidBridge 显示名 [name={new_name}]")
            self._bot_name = new_name
            if not settings.enabled:
                return
            await self._stop_runtime()
            await self._start_runtime(settings, metadata={"bot_name": new_name, "config_version": version})
            return

        if scope != "self":
            return
        self.ctx.logger.info(f"正在重载 MaiBot 女仆适配器配置 [version={version}]")
        await self._stop_runtime()
        self.set_plugin_config(config_data)
        settings = self._settings()
        if settings.enabled:
            await self._start_runtime(settings, metadata={"config_version": version})
            return
        self._state.mark_disconnected()
        await self._publish_adapter_state(runtime_connected=False, metadata={"enabled": False, "config_version": version})
        self.ctx.logger.info(f"配置重载后 MaiBot 女仆适配器仍处于关闭状态 [version={version}]")

    def _settings(self) -> MaiBotMaidAdapterSettings:
        return self.config if isinstance(self.config, MaiBotMaidAdapterSettings) else MaiBotMaidAdapterSettings()


def create_plugin() -> MaiBotMaidAdapterPlugin:
    return MaiBotMaidAdapterPlugin()
