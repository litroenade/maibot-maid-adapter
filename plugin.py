import asyncio
from collections.abc import Callable
from typing import Any, ClassVar

from maibot_sdk import MaiBotPlugin, ON_BOT_CONFIG_RELOAD, PluginConfigBase

from .config import MaiBotMaidAdapterSettings
from .src.adapter import MaidBridgeMaidPlugin
from .src.runtime.runtime_router import RuntimeRouter
from .src.runtime.state import BridgeRuntimeState
from .src.transport import BridgeTransport


class MaiBotMaidAdapterPlugin(MaidBridgeMaidPlugin, MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = MaiBotMaidAdapterSettings
    config_reload_subscriptions: ClassVar[set[str]] = {ON_BOT_CONFIG_RELOAD}

    def __init__(
        self,
        *,
        transport_factory: Callable[[MaiBotMaidAdapterSettings], BridgeTransport] | None = None,
    ) -> None:
        super().__init__()
        self._state = BridgeRuntimeState()
        self._transport_factory = transport_factory
        self._transport: BridgeTransport | None = None
        self._router: RuntimeRouter | None = None
        self._maid_agent_turn_service: Any | None = None
        self._maid_request_phase_by_session: dict[str, str] = {}
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_attempts = 0
        self._reconnect_stopped = True
        self._bot_name = ""

    async def on_load(self) -> None:
        settings = self._settings()
        if not settings.enabled:
            self._reconnect_stopped = True
            await self._cancel_reconnect_task("plugin_disabled")
            self.ctx.logger.info("MaiBot 女仆适配器已关闭，不启动运行时")
            await self._publish_adapter_state(runtime_connected=False, metadata={"enabled": False})
            return
        self._reconnect_stopped = False
        self._reconnect_attempts = 0
        await self._start_runtime(settings)

    async def on_unload(self) -> None:
        self.ctx.logger.info("正在停止 MaiBot 女仆适配器运行时")
        self._reconnect_stopped = True
        await self._cancel_reconnect_task("plugin_unload")
        await self._stop_runtime()
        self._state.mark_disconnected()
        await self._publish_adapter_state(runtime_connected=False, metadata={"reason": "plugin_unload"})
        self.ctx.logger.info("MaiBot 女仆适配器运行时已停止")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == ON_BOT_CONFIG_RELOAD:
            settings = self._settings()
            old_name = str(getattr(self, "_bot_name", "") or "").strip()
            new_name = self._bot_name_from_config(config_data) or await self._resolve_bot_name(settings)
            if new_name == old_name:
                return
            self.ctx.logger.info(f"MaiBot 本体昵称已更新，正在刷新 MaidBridge 显示名 [name={new_name}]")
            self._bot_name = new_name
            if not settings.enabled:
                return
            self._reconnect_stopped = True
            await self._cancel_reconnect_task("bot_config_reload")
            await self._stop_runtime()
            self._reconnect_stopped = False
            self._reconnect_attempts = 0
            await self._start_runtime(settings, metadata={"bot_name": new_name, "config_version": version})
            return
        if scope != "self":
            return
        self.ctx.logger.info(f"正在重载 MaiBot 女仆适配器配置 [version={version}]")
        self._reconnect_stopped = True
        await self._cancel_reconnect_task("config_reload")
        await self._stop_runtime()
        self.set_plugin_config(config_data)
        settings = self._settings()
        if settings.enabled:
            self._reconnect_stopped = False
            self._reconnect_attempts = 0
            await self._start_runtime(settings, metadata={"config_version": version})
            return
        self._state.mark_disconnected()
        await self._publish_adapter_state(runtime_connected=False, metadata={"enabled": False, "config_version": version})
        self.ctx.logger.info(f"配置重载后 MaiBot 女仆适配器仍处于关闭状态 [version={version}]")

    def _settings(self) -> MaiBotMaidAdapterSettings:
        return self.config if isinstance(self.config, MaiBotMaidAdapterSettings) else MaiBotMaidAdapterSettings()


def create_plugin() -> MaiBotMaidAdapterPlugin:
    return MaiBotMaidAdapterPlugin()
