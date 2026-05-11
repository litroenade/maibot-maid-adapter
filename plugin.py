import asyncio
from collections.abc import Callable
from typing import Any, ClassVar

from maibot_sdk import MaiBotPlugin, PluginConfigBase

from .config import MaiBotMaidAdapterSettings
from .src.adapter import MaidBridgeMaidPlugin
from .src.runtime.runtime_router import RuntimeRouter
from .src.runtime.state import BridgeRuntimeState
from .src.transport import BridgeTransport


class MaiBotMaidAdapterPlugin(MaidBridgeMaidPlugin, MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = MaiBotMaidAdapterSettings

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
