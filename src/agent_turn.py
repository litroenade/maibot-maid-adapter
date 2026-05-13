from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import DEFAULT_MAX_PENDING_MAID_AGENT_TURNS
from .constants import (
    ADAPTER_STATE_NAME,
    DEFAULT_CLIENT_ENDPOINT_ID,
    DEFAULT_JAVA_ENDPOINT_ID,
)
from .protocol.frame import BridgeFrame, build_maid_agent_turn_complete_frame
from .turn_context import (
    TurnContext,
    build_maibot_message,
    build_planner_context,
    build_reply_generation_context,
    build_route_metadata,
    parse_turn_context,
)

SendFrame = Callable[[BridgeFrame], Awaitable[None]]


_PENDING_REGISTERED = "registered"
_PENDING_DUPLICATE_TURN = "duplicate_turn"
_PENDING_QUEUE_FULL = "queue_full"


@dataclass
class _PendingTurn:
    context: TurnContext
    future: asyncio.Future[dict[str, Any]]
    terminal_started: bool = False
    terminal_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dispatch_started: bool = False


class MaidAgentTurnService:
    def __init__(
        self,
        *,
        ctx: Any,
        settings: Any,
        send_frame: SendFrame,
        state: Any | None = None,
        gateway_name: str = ADAPTER_STATE_NAME,
        bot_name: str = "",
    ) -> None:
        self._ctx = ctx
        self._settings = settings
        self._state = state
        self._send_frame = send_frame
        self._gateway_name = gateway_name
        self._bot_name = str(bot_name or "").strip()
        self._pending_by_turn_id: dict[str, _PendingTurn] = {}
        # MaiBot planner hook 只提供 session_id，同一会话必须串行注入，避免回复错配到后来的 turn。
        self._active_by_scope: dict[str, _PendingTurn] = {}
        self._active_by_session_id: dict[str, _PendingTurn] = {}
        self._queues_by_session_id: dict[str, deque[_PendingTurn]] = {}
        self._dispatch_tasks: set[asyncio.Task[None]] = set()

    async def handle(self, frame: BridgeFrame) -> dict[str, Any]:
        context = parse_turn_context(frame, self._settings, bot_name=self._bot_name)
        loop = asyncio.get_running_loop()
        pending = _PendingTurn(context=context, future=loop.create_future())
        register_status = self._register_pending(pending)
        if register_status == _PENDING_DUPLICATE_TURN:
            self._ctx.logger.warning(
                f"MaidBridge 女仆 agent 轮次重复，已忽略重复帧 [turn_id={context.turn_id}, "
                f"trace_id={context.frame.trace_id}]"
            )
            return {
                "success": False,
                "external_message_id": context.turn_id,
                "error": "MaidBridge 女仆 agent 轮次已在等待中",
            }
        if register_status == _PENDING_QUEUE_FULL:
            self._ctx.logger.warning(
                f"MaidBridge 女仆 agent 轮次队列已满，按 no_reply 回写 Java [turn_id={context.turn_id}, "
                f"trace_id={context.frame.trace_id}, max_pending={self._max_pending_turns()}]"
            )
            return await self._send_context_no_reply(context, reason="maibot_pending_queue_full")
        try:
            return await pending.future
        finally:
            self._unregister_pending(pending)

    async def handle_no_reply_session(self, session_id: str, *, reason: str = "maibot_no_reply") -> dict[str, Any]:
        pending = self._pending_for_key(session_id)
        if pending is None:
            return {"success": False, "error": "没有待处理的 MaidBridge 轮次匹配当前 Maisaka 会话"}
        if pending.terminal_started:
            return await self._completed_turn_result(pending)
        if pending.future.done():
            return {"success": False, "error": "没有待处理的 MaidBridge 轮次匹配当前 Maisaka 会话"}
        return await self._complete_no_reply(pending, reason=reason)

    def has_pending_session(self, session_id: str) -> bool:
        return self._pending_for_key(session_id) is not None

    def planner_context_for_session(self, session_id: str) -> dict[str, Any]:
        pending = self._pending_for_key(session_id)
        if pending is None:
            return {}
        return build_planner_context(pending.context)

    def reply_context_for_session(self, session_id: str) -> dict[str, Any]:
        pending = self._pending_for_key(session_id)
        if pending is None:
            return {}
        return build_reply_generation_context(pending.context)

    async def handle_reply_session(
        self,
        session_id: str,
        reply_text: str,
        *,
        actions: list[dict[str, Any]] | None = None,
        reason: str = "maibot_external_reply",
    ) -> dict[str, Any]:
        del reason
        pending = self._pending_for_key(session_id)
        if pending is None:
            return {"success": False, "error": "没有待处理的 MaidBridge 轮次匹配当前 Maisaka 会话"}
        if pending.terminal_started:
            return await self._completed_turn_result(pending)
        if pending.future.done():
            return {"success": False, "error": "没有待处理的 MaidBridge 轮次匹配当前 Maisaka 会话"}
        text = str(reply_text or "").strip()
        if not text:
            return {"success": False, "error": "MaidBridge 女仆回复文本不能为空"}
        return await self._complete_reply(pending, text, actions or [])

    async def _complete_reply(self, pending: _PendingTurn, text: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        async with pending.terminal_lock:
            if pending.terminal_started:
                return await self._completed_turn_result(pending)
            context = pending.context
            pending.terminal_started = True
            frame = build_maid_agent_turn_complete_frame(
                turn_id=context.turn_id,
                maid_uuid=context.maid_uuid,
                outcome="reply",
                reply_text=text,
                tts_text=text,
                history_policy="append",
                actions=actions,
                trace_id=context.frame.trace_id,
                reply_to=context.frame.request_id or context.frame.id,
                source_endpoint=context.frame.target_endpoint or DEFAULT_CLIENT_ENDPOINT_ID,
                target_endpoint=context.frame.source_endpoint or DEFAULT_JAVA_ENDPOINT_ID,
            )
            try:
                await self._send_frame(frame)
            except Exception as exc:
                return self._fail_pending_completion(pending, "reply", exc)
            result = {
                "success": True,
                "external_message_id": context.turn_id,
                "outcome": "reply",
                "actions_count": len(actions),
            }
            if not pending.future.done():
                pending.future.set_result(result)
            self._unregister_pending(pending)
            return result

    async def _complete_no_reply(self, pending: _PendingTurn, *, reason: str) -> dict[str, Any]:
        async with pending.terminal_lock:
            if pending.terminal_started:
                return await self._completed_turn_result(pending)
            context = pending.context
            pending.terminal_started = True
            try:
                await self._send_no_reply_frame(context, reason=reason)
            except Exception as exc:
                return self._fail_pending_completion(pending, "no_reply", exc)
            result = {"success": True, "external_message_id": context.turn_id, "outcome": "no_reply", "reason": reason}
            if not pending.future.done():
                pending.future.set_result(result)
            self._unregister_pending(pending)
            return result

    async def _send_context_no_reply(self, context: TurnContext, *, reason: str) -> dict[str, Any]:
        try:
            await self._send_no_reply_frame(context, reason=reason)
        except Exception as exc:
            error = f"MaidBridge 女仆 agent no_reply 发送失败：{exc}"
            self._ctx.logger.warning(
                f"MaidBridge 女仆 agent no_reply 发送失败 [turn_id={context.turn_id}, "
                f"trace_id={context.frame.trace_id}, reason={reason}, error={exc}]"
            )
            return {"success": False, "external_message_id": context.turn_id, "outcome": "no_reply", "error": error}
        return {"success": True, "external_message_id": context.turn_id, "outcome": "no_reply", "reason": reason}

    async def _send_no_reply_frame(self, context: TurnContext, *, reason: str) -> None:
        frame = build_maid_agent_turn_complete_frame(
            turn_id=context.turn_id,
            maid_uuid=context.maid_uuid,
            outcome="no_reply",
            reason=reason,
            trace_id=context.frame.trace_id,
            reply_to=context.frame.request_id or context.frame.id,
            source_endpoint=context.frame.target_endpoint or DEFAULT_CLIENT_ENDPOINT_ID,
            target_endpoint=context.frame.source_endpoint or DEFAULT_JAVA_ENDPOINT_ID,
        )
        await self._send_frame(frame)

    def _fail_pending_completion(self, pending: _PendingTurn, outcome: str, exc: Exception) -> dict[str, Any]:
        error = f"MaidBridge 女仆 agent 终态帧发送失败：{exc}"
        self._ctx.logger.warning(
            f"MaidBridge 女仆 agent 终态帧发送失败 [turn_id={pending.context.turn_id}, "
            f"trace_id={pending.context.frame.trace_id}, outcome={outcome}, error={exc}]"
        )
        result = {
            "success": False,
            "external_message_id": pending.context.turn_id,
            "outcome": outcome,
            "error": error,
        }
        if not pending.future.done():
            pending.future.set_result(result)
        self._unregister_pending(pending)
        return result

    def cancel_pending(self, error: str) -> None:
        for task in tuple(self._dispatch_tasks):
            task.cancel()
        for pending in tuple(self._pending_by_turn_id.values()):
            if not pending.future.done():
                pending.future.set_result(
                    {
                        "success": False,
                        "external_message_id": pending.context.turn_id,
                        "error": error,
                    }
                )
            self._unregister_pending(pending)

    def _pending_for_key(self, key: str) -> _PendingTurn | None:
        normalized = str(key or "").strip()
        if not normalized:
            return None
        return (
            self._active_by_session_id.get(normalized)
            or self._active_by_scope.get(normalized)
        )

    def _register_pending(self, pending: _PendingTurn) -> str:
        if pending.context.turn_id in self._pending_by_turn_id:
            return _PENDING_DUPLICATE_TURN
        if len(self._pending_by_turn_id) >= self._max_pending_turns():
            return _PENDING_QUEUE_FULL
        self._pending_by_turn_id[pending.context.turn_id] = pending
        self._queues_by_session_id.setdefault(pending.context.session_id, deque()).append(pending)
        self._promote_next_for_session(pending.context.session_id)
        return _PENDING_REGISTERED

    def _unregister_pending(self, pending: _PendingTurn) -> None:
        self._pending_by_turn_id.pop(pending.context.turn_id, None)
        queue = self._queues_by_session_id.get(pending.context.session_id)
        if queue is not None:
            try:
                queue.remove(pending)
            except ValueError:
                pass
            if not queue:
                self._queues_by_session_id.pop(pending.context.session_id, None)
        was_active = self._active_by_session_id.get(pending.context.session_id) is pending
        if was_active:
            self._active_by_session_id.pop(pending.context.session_id, None)
        if self._active_by_scope.get(pending.context.scope) is pending:
            self._active_by_scope.pop(pending.context.scope, None)
        if was_active:
            self._promote_next_for_session(pending.context.session_id)

    async def _completed_turn_result(self, pending: _PendingTurn) -> dict[str, Any]:
        result = await pending.future
        if result.get("success") is False:
            return {"success": False, "error": str(result.get("error") or "MaidBridge 轮次完成失败")}
        return {"success": True, "external_message_id": pending.context.turn_id}

    def _promote_next_for_session(self, session_id: str) -> None:
        normalized = str(session_id or "").strip()
        if not normalized or normalized in self._active_by_session_id:
            return
        queue = self._queues_by_session_id.get(normalized)
        while queue:
            pending = queue[0]
            if pending.future.done() or self._pending_by_turn_id.get(pending.context.turn_id) is not pending:
                queue.popleft()
                continue
            self._active_by_session_id[normalized] = pending
            self._active_by_scope[pending.context.scope] = pending
            self._start_dispatch(pending)
            return
        self._queues_by_session_id.pop(normalized, None)

    def _start_dispatch(self, pending: _PendingTurn) -> None:
        if pending.dispatch_started or pending.future.done():
            return
        pending.dispatch_started = True
        task = asyncio.create_task(self._dispatch_to_maibot(pending))
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    async def _dispatch_to_maibot(self, pending: _PendingTurn) -> None:
        context = pending.context
        try:
            accepted = await self._ctx.gateway.route_message(
                gateway_name=self._gateway_name,
                message=build_maibot_message(context),
                route_metadata=build_route_metadata(context),
                external_message_id=context.turn_id,
                dedupe_key=context.turn_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not pending.future.done():
                self._ctx.logger.warning(f"MaidBridge 注入 MaiBot gateway 失败，按 no_reply 回写 Java [error={exc}]")
                await self._complete_no_reply(pending, reason="maibot_gateway_error")
            return
        if pending.future.done():
            return
        if not accepted:
            self._ctx.logger.warning("MaidBridge 女仆 agent 轮次被 MaiBot gateway 拒绝，按 no_reply 回写 Java")
            await self._complete_no_reply(pending, reason="maibot_gateway_rejected")

    def _max_pending_turns(self) -> int:
        value = getattr(self._settings, "max_pending_maid_agent_turns", DEFAULT_MAX_PENDING_MAID_AGENT_TURNS)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return DEFAULT_MAX_PENDING_MAID_AGENT_TURNS
