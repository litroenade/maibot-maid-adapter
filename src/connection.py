import asyncio
import contextlib
from typing import Any

from .constants import ADAPTER_STATE_NAME, PLATFORM, PROTOCOL
from .agent_turn import MaidAgentTurnService
from .protocol.frame import build_session_initialize_frame
from .runtime.builder import build_runtime_bundle
from .runtime.runtime_router import RuntimeRouter
from .runtime.state import PendingRequest
from .transport import AioHttpWebSocketBridgeTransport, BridgeTransport
from .utils import first_non_blank, reconnect_attempt


class MaidBridgeConnection:
    async def _start_runtime(
        self,
        settings: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        metadata = metadata or {}
        attempt = reconnect_attempt(metadata)
        if attempt:
            self.ctx.logger.debug(
                f"MaidBridge 正在执行重连尝试 [attempt={attempt}, "
                f"max_attempts={settings.reconnect_max_attempts}, url={self._websocket_url(settings)}]"
            )
        else:
            self.ctx.logger.info(
                f"MaidBridge 正在连接 [url={self._websocket_url(settings)}, "
                f"message_out_events={settings.enable_message_out_events}, "
                f"maid_agent_turns={settings.enable_maid_agent_turns}]"
            )
        bot_name = await self._resolve_bot_name(settings)
        self._bot_name = bot_name
        transport = self._build_transport(settings)
        transport.on_open(lambda: self._handle_transport_open(settings, metadata=metadata))
        transport.on_close(lambda: self._handle_transport_close(settings))
        maid_agent_turn_handler = (
            MaidAgentTurnService(
                ctx=self.ctx,
                settings=settings,
                state=self._state,
                send_frame=self._send_frame,
                bot_name=bot_name,
            )
            if settings.enable_maid_agent_turns
            else None
        )
        self._maid_agent_turn_service = maid_agent_turn_handler
        router = RuntimeRouter(
            build_runtime_bundle(
                ctx=self.ctx,
                transport=transport,
                state=self._state,
                max_message_bytes=settings.max_message_bytes,
                enable_message_out_events=settings.enable_message_out_events,
                maid_agent_turn_handler=maid_agent_turn_handler,
            )
        )
        self._transport = transport
        self._router = router
        try:
            await router.start()
        except Exception as exc:
            if attempt:
                self.ctx.logger.debug(f"MaidBridge 重连尝试失败 [attempt={attempt}, error={exc}]")
            # 游戏端和 MaiBot 端经常分开启动，首次连接失败不能阻塞插件 API/配置页注册。
            with contextlib.suppress(Exception):
                await transport.stop()
            self._transport = None
            self._router = None
            self._state.mark_disconnected()
            await self._publish_adapter_state(
                runtime_connected=False,
                metadata={
                    "enabled": True,
                    "reason": "transport_start_failed",
                    "error": str(exc),
                },
            )
            await self._schedule_reconnect(settings, reason="transport_start_failed", error=str(exc))
            return
        self._reconnect_attempts = 0
        if self._reconnect_task is asyncio.current_task():
            self._reconnect_task = None
        if attempt:
            self.ctx.logger.info(
                f"MaidBridge 运行时已在重连后就绪 [attempt={attempt}, url={self._websocket_url(settings)}]"
            )
        else:
            self.ctx.logger.info(f"MaidBridge 运行时已就绪 [url={self._websocket_url(settings)}]")

    async def _stop_runtime(self) -> None:
        service = getattr(self, "_maid_agent_turn_service", None)
        if service is not None:
            service.cancel_pending("MaidBridge 适配器运行时已停止")
        self._maid_agent_turn_service = None
        phase_store = getattr(self, "_maid_request_phase_by_session", None)
        if isinstance(phase_store, dict):
            phase_store.clear()
        if self._router is not None:
            await self._router.stop()
        self._router = None
        self._transport = None

    async def _schedule_reconnect(
        self,
        settings: Any,
        *,
        reason: str,
        error: str = "",
    ) -> None:
        if self._reconnect_stopped or not settings.enabled:
            return
        task = self._reconnect_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            return
        max_attempts = max(0, settings.reconnect_max_attempts)
        if self._reconnect_attempts >= max_attempts:
            self.ctx.logger.error(
                f"MaidBridge 重连已停止：重试次数耗尽 [attempts={self._reconnect_attempts}, "
                f"max_attempts={max_attempts}, url={self._websocket_url(settings)}, "
                f"reason={reason}, error={error}]"
            )
            await self._publish_adapter_state(
                runtime_connected=False,
                metadata={
                    "enabled": True,
                    "reason": "transport_reconnect_exhausted",
                    "last_reason": reason,
                    "attempts": self._reconnect_attempts,
                    "max_attempts": max_attempts,
                    "error": error,
                },
            )
            return
        self._reconnect_attempts += 1
        attempt = self._reconnect_attempts
        delay_ms = self._reconnect_delay_ms(settings, attempt)
        if attempt == 1:
            self.ctx.logger.warning(
                f"MaidBridge Java 服务不可达，已安排重试 [next_attempt={attempt}, "
                f"max_attempts={max_attempts}, delay_ms={delay_ms}, "
                f"url={self._websocket_url(settings)}, reason={reason}, error={error}]"
            )
        else:
            self.ctx.logger.debug(
                f"MaidBridge 已安排重试 [next_attempt={attempt}, max_attempts={max_attempts}, "
                f"delay_ms={delay_ms}, reason={reason}, error={error}]"
            )
        self._reconnect_task = asyncio.create_task(
            self._run_reconnect(settings, attempt=attempt, delay_ms=delay_ms)
        )

    def _reconnect_delay_ms(self, settings: Any, attempt: int) -> int:
        initial_delay_ms = max(0, settings.reconnect_initial_delay_ms)
        max_delay_ms = max(0, settings.reconnect_max_delay_ms)
        uncapped_delay_ms = initial_delay_ms * (2 ** max(0, attempt - 1))
        return min(uncapped_delay_ms, max_delay_ms) if max_delay_ms else uncapped_delay_ms

    async def _run_reconnect(
        self,
        settings: Any,
        *,
        attempt: int,
        delay_ms: int,
    ) -> None:
        try:
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
            if self._reconnect_stopped or not settings.enabled:
                return
            await self._start_runtime(settings, metadata={"reconnect_attempt": attempt})
        except asyncio.CancelledError:
            self.ctx.logger.info(f"MaidBridge 重连已取消 [attempt={attempt}]")
            raise
        finally:
            if self._reconnect_task is asyncio.current_task():
                self._reconnect_task = None

    async def _cancel_reconnect_task(self, reason: str) -> None:
        task = self._reconnect_task
        self._reconnect_task = None
        self._reconnect_attempts = 0
        if task is None or task.done():
            return
        task.cancel()
        self.ctx.logger.info(f"MaidBridge 重连任务已取消 [reason={reason}]")
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _build_transport(self, settings: Any) -> BridgeTransport:
        if self._transport_factory is not None:
            return self._transport_factory(settings)
        return AioHttpWebSocketBridgeTransport(
            settings.websocket_url,
            access_token=settings.access_token,
            max_message_bytes=settings.max_message_bytes,
        )

    def _websocket_role(self, settings: Any) -> str:
        del settings
        return "client"

    def _websocket_url(self, settings: Any) -> str:
        return settings.websocket_url

    async def _handle_transport_open(
        self,
        settings: Any,
        *,
        metadata: dict[str, Any],
    ) -> None:
        connection_id = f"{settings.server_id}@{self._websocket_url(settings)}"
        self.ctx.logger.info(
            f"MaidBridge WebSocket 已连接 [url={self._websocket_url(settings)}, connection_id={connection_id}]"
        )
        await self._send_session_initialize(settings)
        self._state.mark_connected(server_id=settings.server_id, connection_id=connection_id)
        await self._publish_adapter_state(
            runtime_connected=True,
            metadata={
                "enabled": True,
                "enable_message_out_events": bool(settings.enable_message_out_events),
                "connection_id": connection_id,
                "websocket_role": self._websocket_role(settings),
                **metadata,
            },
        )

    async def _send_session_initialize(self, settings: Any) -> None:
        if self._transport is None:
            return
        bot_name = first_non_blank(getattr(self, "_bot_name", ""), settings.agent_id, "maibot")
        frame = build_session_initialize_frame(
            client_id=f"{settings.server_id}@{self._websocket_url(settings)}",
            # Java 端历史字段仍叫 agent_id/client_name，这里承载的是 MaiBot 本体显示名。
            agent_id=bot_name,
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
            f"roles={len(settings.client_roles)}, subscriptions={len(settings.subscriptions)}]"
        )

    async def _resolve_bot_name(self, settings: Any) -> str:
        try:
            nickname = await self.ctx.config.get("bot.nickname", "")
        except Exception as exc:
            self.ctx.logger.debug(f"MaidBridge 读取 MaiBot 本体昵称失败，使用配置回退值 [error={exc}]")
            nickname = ""
        return first_non_blank(nickname, settings.agent_id, "maibot")

    async def _handle_transport_close(self, settings: Any) -> None:
        pending = self._state.mark_disconnected()
        if self._reconnect_stopped or not settings.enabled:
            self.ctx.logger.debug(f"MaidBridge WebSocket 因运行时停止而关闭 [pending={len(pending)}]")
        else:
            self.ctx.logger.warning(
                f"MaidBridge WebSocket 已断开，将安排重连 [url={self._websocket_url(settings)}, "
                f"pending={len(pending)}]"
            )
        self._complete_pending_requests(pending, error="MaidBridge 传输层已关闭")
        service = getattr(self, "_maid_agent_turn_service", None)
        if service is not None:
            service.cancel_pending("MaidBridge 传输层已关闭")
        await self._publish_adapter_state(runtime_connected=False, metadata={"reason": "transport_closed"})
        self._router = None
        self._transport = None
        await self._schedule_reconnect(settings, reason="transport_closed")

    async def _publish_adapter_state(self, *, runtime_connected: bool, metadata: dict[str, Any]) -> None:
        settings = self._settings()
        await self.ctx.gateway.update_state(
            ADAPTER_STATE_NAME,
            # MaiBot SDK 的网关状态边界固定读取 ready 参数。
            ready=runtime_connected,
            platform=PLATFORM,
            account_id="",
            scope="",
            metadata={
                "protocol": PROTOCOL,
                "server_id": settings.server_id,
                "websocket_role": self._websocket_role(settings),
                "websocket_url": self._websocket_url(settings),
                "enable_message_out_events": bool(settings.enable_message_out_events),
                **metadata,
            },
        )

    def _pending_request(self, **kwargs: Any) -> PendingRequest:
        return PendingRequest(**kwargs)

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
