import asyncio
import json
from typing import Any, Mapping

from ..constants import DEFAULT_MAX_MESSAGE_BYTES
from ..protocol import BridgeFrame, BridgeProtocolError
from ..protocol.router import RouteDecision, route_frame
from .state import BridgeRuntimeState


class RuntimeRouter:
    def __init__(
        self,
        *,
        ctx: Any,
        transport: Any,
        state: BridgeRuntimeState,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        maid_turn_handler: Any | None = None,
    ) -> None:
        self._ctx = ctx
        self._transport = transport
        self._state = state
        self._max_message_bytes = max_message_bytes
        self._maid_turn_handler = maid_turn_handler
        self._started = False
        self._tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        if self._started:
            return
        self._transport.on_raw(self._handle_raw)
        await self._transport.start()
        self._started = True
        self._ctx.logger.debug("MaidBridge 运行时路由器已启动")

    async def stop(self) -> None:
        if not self._started:
            return
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.difference_update(tasks)
        await self._transport.stop()
        self._started = False
        self._ctx.logger.debug("MaidBridge 运行时路由器已停止")

    async def _handle_raw(self, raw: str) -> None:
        try:
            self._ctx.logger.debug(f"MaidBridge 收到原始入站载荷 [bytes={len(raw)}]")
            if len(raw.encode("utf-8")) > self._max_message_bytes:
                raise BridgeProtocolError("帧超过最大消息字节数")
            frame = BridgeFrame.loads(raw, max_bytes=self._max_message_bytes)
            decision = route_frame(frame, default_server_id=self._state.server_id)
            self._ctx.logger.debug(
                f"MaidBridge 入站帧已路由 [event_id={frame.id}, trace_id={frame.trace_id}, "
                f"type={frame.type}, decision={decision.kind}]"
            )
            await self._handle_decision(frame, decision)
        except (BridgeProtocolError, Exception) as exc:
            self._ctx.logger.warning(f"MaidBridge 入站载荷处理失败 [error={exc}]")

    async def _handle_decision(self, frame: BridgeFrame, decision: RouteDecision) -> None:
        if decision.kind == "session_ready":
            self._complete_pending_response(frame, decision.payload)
            self._record_session_ready(frame, decision)
            self._ctx.logger.info(
                f"MaidBridge 端点已就绪 [server={decision.payload['server_id']}, "
                f"endpoint={decision.payload['endpoint_id']}, trace_id={frame.trace_id}]"
            )
            return
        if decision.kind in {"api_response", "bridge_error"}:
            self._complete_pending_response(frame, decision.payload)
            if decision.kind == "bridge_error":
                self._ctx.logger.warning(
                    f"MaidBridge 领域响应失败 [type={frame.type}, reply_to={frame.reply_to}, "
                    f"error={_response_error(decision.payload)}]"
                )
            else:
                self._ctx.logger.debug(
                    f"MaidBridge 领域响应完成 [type={frame.type}, reply_to={frame.reply_to}, "
                    f"payload={_mapping_summary(decision.payload)}]"
                )
            return
        if decision.kind == "maid_turn":
            handler = self._maid_turn_handler
            maid_uuid = _decision_maid_uuid(decision)
            if handler is None:
                self._ctx.logger.warning(
                    f"MaidBridge 女仆回合已拒绝：处理器未启用 [event_id={frame.id}, trace_id={frame.trace_id}, "
                    f"maid_uuid={maid_uuid}]"
                )
                return
            self._ctx.logger.info(
                f"MaidBridge 收到女仆回合请求 [event_id={frame.id}, trace_id={frame.trace_id}, "
                f"maid_uuid={maid_uuid}, turn_id={decision.payload['turn_id']}, "
                f"request_id={decision.payload['request_id']}]"
            )
            task = asyncio.create_task(self._dispatch_maid_turn(frame, handler))
            self._tasks.add(task)
            task.add_done_callback(self._forget_task)
            return
        if decision.kind == "observe":
            self._ctx.logger.debug(
                f"MaidBridge 已观察事件 [type={frame.type}, event_id={frame.id}, payload={_mapping_summary(decision.payload)}]"
            )
            return
        self._ctx.logger.warning(
            f"MaidBridge 路由拒绝帧 [event_id={frame.id}, trace_id={frame.trace_id}, "
            f"error={decision.payload.get('error')}]"
        )

    async def _dispatch_maid_turn(self, frame: BridgeFrame, handler: Any) -> None:
        maid_uuid = _frame_maid_uuid(frame)
        turn_id = _frame_turn_id(frame)
        try:
            reply = await handler.handle(frame)
            self._ctx.logger.info(
                f"MaidBridge 女仆回合已完成 [event_id={frame.id}, trace_id={frame.trace_id}, "
                f"maid_uuid={maid_uuid}, turn_id={turn_id}]"
            )
            self._ctx.logger.debug(f"MaidBridge 女仆回合本地结果：{_mapping_summary(reply)}")
        except asyncio.CancelledError:
            self._ctx.logger.warning(
                f"MaidBridge 女仆回合已取消 [event_id={frame.id}, trace_id={frame.trace_id}, "
                f"maid_uuid={maid_uuid}, turn_id={turn_id}]"
            )
            raise
        except Exception as exc:
            self._ctx.logger.warning(
                f"MaidBridge 女仆回合处理失败 [event_id={frame.id}, trace_id={frame.trace_id}, "
                f"maid_uuid={maid_uuid}, turn_id={turn_id}, error={exc}]"
            )

    def _complete_pending_response(self, frame: BridgeFrame, routed_payload: Mapping[str, Any]) -> None:
        reply_to = str(routed_payload.get("reply_to") or frame.reply_to or frame.request_id or "").strip()
        if not reply_to:
            return
        request = self._state.pending_requests.get(reply_to)
        if request is None or request.future is None or request.future.done():
            self._ctx.logger.debug(
                f"MaidBridge 领域响应未匹配 pending [reply_to={reply_to}, type={frame.type}]"
            )
            return
        if not _response_matches_request(request.frame_type, frame.type):
            self._ctx.logger.warning(
                f"MaidBridge 领域响应类型不匹配，已保留 pending [request_id={reply_to}, "
                f"request_type={request.frame_type}, response_type={frame.type}]"
            )
            return
        self._state.pending_requests.pop(reply_to, None)
        raw_payload = routed_payload.get("payload")
        payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else dict(routed_payload)
        request.future.set_result(
            {
                "type": frame.type,
                "reply_to": reply_to,
                "trace_id": frame.trace_id,
                "payload": dict(payload),
            }
        )
        self._ctx.logger.debug(
            f"MaidBridge pending 请求已完成 [request_id={reply_to}, trace_id={frame.trace_id}, type={frame.type}]"
        )

    def _forget_task(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)

    def _record_session_ready(self, frame: BridgeFrame, decision: RouteDecision) -> None:
        self._state.register_endpoint(
            server_id=str(decision.payload["server_id"]),
            endpoint_id=str(decision.payload["endpoint_id"]),
            server_name=str(decision.payload["server_name"]),
            source_endpoint=frame.source_endpoint,
            target_endpoint=frame.target_endpoint,
            schema_version=str(decision.payload["schema_version"]),
            features=dict(decision.payload["features"]),
            capabilities=dict(decision.payload["capabilities"]),
        )


def _response_matches_request(request_type: str, response_type: str) -> bool:
    if response_type == "bridge.error":
        return True
    if request_type == "bridge.session.initialize":
        return response_type == "bridge.session.ready"
    if request_type.startswith("maid.api."):
        return response_type == "maid.api.response"
    return True


def _response_error(value: Mapping[str, Any]) -> str:
    payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else value
    if isinstance(payload, Mapping):
        return str(payload.get("error") or "")
    return ""


def _decision_maid_uuid(decision: RouteDecision) -> str:
    maid = decision.payload.get("maid")
    return str(maid.get("uuid") if isinstance(maid, Mapping) else "").strip()


def _frame_maid_uuid(frame: BridgeFrame) -> str:
    maid = frame.payload.get("maid")
    return str(maid.get("uuid") if isinstance(maid, Mapping) else "").strip()


def _frame_turn_id(frame: BridgeFrame) -> str:
    return str(frame.payload.get("turn_id") or frame.request_id or frame.id or "").strip()


def _mapping_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"type": type(value).__name__}
    return {
        "keys": sorted(str(key) for key in value.keys()),
        "bytes": len(json.dumps(value, ensure_ascii=False, default=str)),
    }
