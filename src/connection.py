import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from ..config import DEFAULT_AGENT_ID
from .constants import ADAPTER_STATE_NAME, PLATFORM, PROTOCOL, WEBSOCKET_ROLE
from .maid_turn.turn_service import MaidTurnService
from .protocol.frame import build_session_initialize_frame
from .runtime.runtime_router import RuntimeRouter
from .runtime.state import BridgeRuntimeState, PendingRequest
from .transport import AioHttpWebSocketBridgeTransport
from .utils import first_non_blank


class MaidBridgeConnection:
    if TYPE_CHECKING:
        _state: BridgeRuntimeState
        _transport: AioHttpWebSocketBridgeTransport | None
        _router: RuntimeRouter | None
        _maid_turn_service: MaidTurnService | None
        _runtime_task: asyncio.Task[None] | None
        _runtime_stop_event: asyncio.Event | None
        _runtime_closed_event: asyncio.Event | None
        _runtime_generation: int
        _reconnect_attempts: int
        _bot_name: str

        @property
        def ctx(self) -> Any:
            raise NotImplementedError

        def _settings(self) -> Any:
            raise NotImplementedError

        async def _send_frame(self, frame: Any) -> None:
            raise NotImplementedError

        async def _send_frame_await_reply(self, frame: Any, *, settings: Any) -> dict[str, Any]:
            raise NotImplementedError

    async def _start_runtime(
        self,
        settings: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._stop_runtime()
        self._runtime_stop_event = asyncio.Event()
        self._reconnect_attempts = 0
        self._runtime_task = asyncio.create_task(self._runtime_loop(settings, dict(metadata or {})))

    async def _stop_runtime(self) -> None:
        stop_event = self._runtime_stop_event
        if stop_event is not None:
            stop_event.set()

        task = self._runtime_task
        self._runtime_task = None
        self._runtime_generation += 1
        closed_event = self._runtime_closed_event
        if closed_event is not None:
            closed_event.set()

        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        await self._teardown_runtime(reason="runtime_stopped", publish=False)

    async def _runtime_loop(self, settings: Any, metadata: dict[str, Any]) -> None:
        reconnect_index = 0
        last_reason = "runtime_start"
        last_error = ""
        try:
            while self._runtime_should_run(settings):
                if reconnect_index > 0:
                    delay_ms = self._reconnect_delay_ms(settings, reconnect_index)
                    self.ctx.logger.warning(
                        f"MaidBridge WebSocket 将重连 [attempt={reconnect_index}, delay_ms={delay_ms}, "
                        f"url={settings.websocket_url}, reason={last_reason}, error={last_error}]"
                    )
                    if await self._wait_for_stop(delay_ms):
                        return

                self._reconnect_attempts = max(0, reconnect_index)
                generation = self._next_runtime_generation()
                self._runtime_closed_event = asyncio.Event()
                attempt_metadata = dict(metadata)
                if reconnect_index > 0:
                    attempt_metadata.update(
                        {
                            "reconnect_attempt": reconnect_index,
                            "last_reason": last_reason,
                        }
                    )

                try:
                    await self._connect_once(settings, generation=generation, metadata=attempt_metadata)
                    reconnect_index = 1
                    last_reason = "transport_closed"
                    last_error = ""
                    await self._wait_until_current_transport_closes()
                    if not self._runtime_should_run(settings):
                        return
                    await self._teardown_runtime(reason="transport_closed", publish=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_reason = "transport_start_failed"
                    last_error = str(exc)
                    await self._teardown_runtime(reason=last_reason, error=last_error, publish=True)
                    reconnect_index += 1
        finally:
            await self._teardown_runtime(reason="runtime_stopped", publish=False)

    async def _connect_once(self, settings: Any, *, generation: int, metadata: dict[str, Any]) -> None:
        self.ctx.logger.info(
            f"MaidBridge 正在连接 [url={settings.websocket_url}, "
            f"maid_turns={settings.enable_maid_agent_turns}]"
        )
        bot_name = await self._resolve_bot_name(settings)
        self._bot_name = bot_name
        maid_uuid = first_non_blank(settings.maid_uuid)
        if not maid_uuid:
            raise RuntimeError("MaidAdapter 必须填写女仆 UUID")

        transport = AioHttpWebSocketBridgeTransport(
            settings.websocket_url,
            access_token=settings.access_token,
            max_message_bytes=settings.max_message_bytes,
        )
        transport.on_close(lambda: self._mark_transport_closed(generation))
        maid_turn_handler = (
            MaidTurnService(
                ctx=self.ctx,
                settings=settings,
                send_frame=self._send_frame,
                bot_name=bot_name,
            )
            if settings.enable_maid_agent_turns
            else None
        )
        router = RuntimeRouter(
            ctx=self.ctx,
            transport=transport,
            state=self._state,
            max_message_bytes=settings.max_message_bytes,
            maid_turn_handler=maid_turn_handler,
        )

        self._transport = transport
        self._router = router
        self._maid_turn_service = maid_turn_handler
        await router.start()
        await self._send_session_initialize(settings)

        connection_id = f"{settings.server_id}@{settings.websocket_url}"
        self._state.mark_connected(server_id=settings.server_id, connection_id=connection_id)
        self._reconnect_attempts = 0
        self.ctx.logger.info(
            f"MaidBridge 运行时已就绪 [url={settings.websocket_url}, connection_id={connection_id}]"
        )
        await self._publish_adapter_state(
            runtime_connected=True,
            metadata={
                "enabled": True,
                "connection_id": connection_id,
                "websocket_role": WEBSOCKET_ROLE,
                **metadata,
            },
        )

    async def _teardown_runtime(
        self,
        *,
        reason: str,
        error: str = "",
        publish: bool,
    ) -> None:
        router = self._router
        transport = self._transport
        service = self._maid_turn_service
        self._router = None
        self._transport = None
        self._maid_turn_service = None

        if service is not None:
            service.cancel_pending(error or "MaidBridge 传输层已关闭")

        if router is not None:
            with contextlib.suppress(Exception):
                await router.stop()
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.stop()

        pending = self._state.mark_disconnected()
        self._complete_pending_requests(pending, error=error or "MaidBridge 传输层已关闭")

        if publish:
            await self._publish_adapter_state(
                runtime_connected=False,
                metadata={
                    "enabled": True,
                    "reason": reason,
                    "error": error,
                },
            )

    async def _wait_for_stop(self, delay_ms: int) -> bool:
        stop_event = self._runtime_stop_event
        if stop_event is None:
            return True
        if delay_ms <= 0:
            return stop_event.is_set()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay_ms / 1000)
            return True
        except TimeoutError:
            return False

    async def _wait_until_current_transport_closes(self) -> None:
        stop_event = self._runtime_stop_event
        closed_event = self._runtime_closed_event
        if stop_event is None or closed_event is None:
            return
        stop_task = asyncio.create_task(stop_event.wait())
        close_task = asyncio.create_task(closed_event.wait())
        try:
            await asyncio.wait({stop_task, close_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_task.cancel()
            close_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
            with contextlib.suppress(asyncio.CancelledError):
                await close_task

    def _runtime_should_run(self, settings: Any) -> bool:
        stop_event = self._runtime_stop_event
        return bool(settings.enabled) and not (stop_event is None or stop_event.is_set())

    def _next_runtime_generation(self) -> int:
        self._runtime_generation += 1
        return self._runtime_generation

    def _mark_transport_closed(self, generation: int) -> None:
        if generation != self._runtime_generation:
            return
        closed_event = self._runtime_closed_event
        if closed_event is not None:
            closed_event.set()

    def _mark_transport_unhealthy(self) -> None:
        closed_event = self._runtime_closed_event
        if closed_event is not None:
            closed_event.set()

    def _reconnect_delay_ms(self, settings: Any, attempt: int) -> int:
        initial_delay_ms = max(0, settings.reconnect_initial_delay_ms)
        max_delay_ms = max(0, settings.reconnect_max_delay_ms)
        uncapped_delay_ms = initial_delay_ms * (2 ** max(0, attempt - 1))
        return min(uncapped_delay_ms, max_delay_ms) if max_delay_ms else uncapped_delay_ms

    async def _send_session_initialize(self, settings: Any) -> None:
        if self._transport is None:
            return
        bot_name = first_non_blank(self._bot_name, settings.agent_id, DEFAULT_AGENT_ID)
        frame = build_session_initialize_frame(
            client_id=f"{settings.server_id}@{settings.websocket_url}",
            agent_id=settings.agent_id,
            agent_name=bot_name,
            roles=settings.client_roles,
            subscriptions=settings.subscriptions,
            deadline_ms=settings.request_timeout_ms,
        )
        reply = await self._send_frame_await_reply(frame, settings=settings)
        payload = reply["payload"]
        reply_type = reply["type"]
        if reply_type != "bridge.session.ready":
            error = str(payload.get("error") or "MaidBridge 会话初始化被拒绝")
            self.ctx.logger.warning(
                f"MaidBridge 会话初始化被拒绝 [request_id={frame.request_id}, "
                f"trace_id={frame.trace_id}, error={error}]"
            )
            raise RuntimeError(f"MaidBridge 会话初始化被拒绝：{error}")
        self.ctx.logger.info(
            f"MaidBridge 握手完成 [request_id={frame.request_id}, reply_type={reply_type}, "
            f"agent_id={settings.agent_id}, agent_name={bot_name}, "
            f"roles={len(settings.client_roles)}, subscriptions={len(settings.subscriptions)}]"
        )

    async def _resolve_bot_name(self, settings: Any) -> str:
        try:
            nickname = await self.ctx.config.get("bot.nickname", "")
        except Exception as exc:
            self.ctx.logger.debug(f"MaidBridge 读取 MaiBot 本体昵称失败，使用配置回退值 [error={exc}]")
            nickname = ""
        return first_non_blank(nickname, settings.agent_id, DEFAULT_AGENT_ID)

    def _bot_name_from_config(self, config_data: dict[str, Any]) -> str:
        bot_config = config_data.get("bot")
        if isinstance(bot_config, dict):
            return first_non_blank(bot_config.get("nickname"))
        return first_non_blank(config_data.get("bot.nickname"))

    async def _publish_adapter_state(self, *, runtime_connected: bool, metadata: dict[str, Any]) -> None:
        settings = self._settings()
        await self.ctx.gateway.update_state(
            ADAPTER_STATE_NAME,
            ready=runtime_connected,
            platform=PLATFORM,
            account_id=settings.maid_uuid,
            scope="",
            metadata={
                "protocol": PROTOCOL,
                "server_id": settings.server_id,
                "websocket_role": WEBSOCKET_ROLE,
                "websocket_url": settings.websocket_url,
                "reconnect_attempts": self._reconnect_attempts,
                **metadata,
            },
        )

    def _complete_pending_requests(self, pending: list[PendingRequest], *, error: str) -> None:
        completed = 0
        for request in pending:
            if request.future is None or request.future.done():
                continue
            request.future.set_result(
                {
                    "type": "bridge.error",
                    "reply_to": request.request_id,
                    "trace_id": request.trace_id,
                    "payload": {"error": error},
                }
            )
            completed += 1
        if completed:
            self.ctx.logger.warning(f"MaidBridge 已用错误结束 {completed} 个待响应请求：{error}")
