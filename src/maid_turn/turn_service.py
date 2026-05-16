import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..constants import (
    ADAPTER_STATE_NAME,
    DEFAULT_CLIENT_ENDPOINT_ID,
    DEFAULT_JAVA_ENDPOINT_ID,
)
from ..protocol.frame import BridgeFrame, build_maid_agent_turn_complete_frame
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
_BATCH_MERGED_REASON = "maibot_batch_context_merged"


@dataclass
class _PendingTurn:
    context: TurnContext
    future: asyncio.Future[dict[str, Any]]
    terminal_started: bool = False
    terminal_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    outbound_actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _TurnLease:
    session_id: str
    phase: str
    turn_ids: tuple[str, ...]


class MaidTurnService:
    def __init__(
        self,
        *,
        ctx: Any,
        settings: Any,
        send_frame: SendFrame,
        gateway_name: str = ADAPTER_STATE_NAME,
        bot_name: str = "",
    ) -> None:
        self._ctx = ctx
        self._settings = settings
        self._send_frame = send_frame
        self._gateway_name = gateway_name
        self._bot_name = str(bot_name or "").strip()
        self._pending_by_turn_id: dict[str, _PendingTurn] = {}
        self._pending_by_session_id: dict[str, list[_PendingTurn]] = {}
        self._known_session_ids: set[str] = set()
        # Hook 只带 session_id，租约用于把一轮 Maisaka 请求和当时的女仆 turn 快照绑定。
        self._lease_by_session_id: dict[str, _TurnLease] = {}
        self._dispatch_tasks: set[asyncio.Task[None]] = set()

    async def handle(self, frame: BridgeFrame) -> dict[str, Any]:
        context = parse_turn_context(frame, self._settings, bot_name=self._bot_name)
        loop = asyncio.get_running_loop()
        pending = _PendingTurn(context=context, future=loop.create_future())
        register_status = self._register_pending(pending)
        if register_status == _PENDING_DUPLICATE_TURN:
            self._ctx.logger.warning(
                f"MaidBridge 女仆回合重复，已忽略重复帧 [turn_id={context.turn_id}, "
                f"trace_id={context.frame.trace_id}]"
            )
            return {
                "success": False,
                "external_message_id": context.turn_id,
                "error": "MaidBridge 女仆回合已在等待中",
            }
        if register_status == _PENDING_QUEUE_FULL:
            self._ctx.logger.warning(
                f"MaidBridge 女仆回合队列已满，按 no_reply 回写 Java [turn_id={context.turn_id}, "
                f"trace_id={context.frame.trace_id}, max_pending={self._max_pending_turns()}]"
            )
            return await self._send_context_no_reply(context, reason="maibot_pending_queue_full")
        try:
            return await pending.future
        finally:
            self._unregister_pending(pending)

    async def handle_no_reply_session(
        self,
        session_id: str,
        *,
        reason: str = "maibot_no_reply",
        actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        batch = self._request_batch_for_session(session_id)
        if not batch:
            return {"success": False, "error": "没有待处理的 MaidBridge 回合匹配当前 Maisaka 会话"}
        return await self._complete_no_reply_batch(batch, reason=reason, actions=actions or [])

    def has_pending_session(self, session_id: str) -> bool:
        return bool(self._pending_batch_for_key(session_id))

    def is_known_session(self, session_id: str) -> bool:
        return str(session_id or "").strip() in self._known_session_ids

    def begin_request_session(self, session_id: str, phase: str) -> dict[str, Any]:
        normalized = str(session_id or "").strip()
        if not normalized:
            return {}
        batch = self._pending_batch_for_key(normalized)
        if not batch:
            self._lease_by_session_id.pop(normalized, None)
            return {}
        lease = _TurnLease(
            session_id=normalized,
            phase=str(phase or "").strip(),
            turn_ids=tuple(pending.context.turn_id for pending in batch),
        )
        self._lease_by_session_id[normalized] = lease
        return self._context_for_batch(batch, include_reply_context=False)

    def request_phase_for_session(self, session_id: str) -> str:
        lease = self._lease_for_session(session_id)
        return lease.phase if lease is not None else ""

    def planner_context_for_session(self, session_id: str) -> dict[str, Any]:
        batch = self._request_batch_for_session(session_id)
        if not batch:
            return {}
        return self._context_for_batch(batch, include_reply_context=False)

    def reply_context_for_session(self, session_id: str) -> dict[str, Any]:
        batch = self._request_batch_for_session(session_id)
        if not batch:
            return {}
        return self._context_for_batch(batch, include_reply_context=True)

    def context_for_outbound(self, message: dict[str, Any], route: dict[str, Any]) -> TurnContext | None:
        turn_id = _outbound_turn_id(message, route)
        if turn_id:
            pending = self._pending_by_turn_id.get(turn_id)
            if pending is not None:
                return pending.context
        session_id = _first_non_blank(message.get("session_id"), route.get("session_id"))
        pending = self._target_pending_for_key(session_id)
        return pending.context if pending is not None else None

    def add_outbound_action(self, session_id: str, action: dict[str, Any]) -> dict[str, Any]:
        pending = self._target_pending_for_key(session_id)
        if pending is None:
            return {"success": False, "error": "没有待处理的 MaidBridge 回合匹配当前 Maisaka 会话"}
        if pending.terminal_started or pending.future.done():
            return {"success": False, "error": "MaidBridge 回合已经进入终态，无法继续暂存 action"}
        pending.outbound_actions.append(dict(action))
        return {"success": True, "actions_count": len(pending.outbound_actions)}

    async def handle_reply_session(
        self,
        session_id: str,
        reply_text: str,
        *,
        actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        batch = self._request_batch_for_session(session_id)
        if not batch:
            return {"success": False, "error": "没有待处理的 MaidBridge 回合匹配当前 Maisaka 会话"}
        text = str(reply_text or "").strip()
        if not text:
            return {"success": False, "error": "MaidBridge 女仆回复文本不能为空"}
        return await self._complete_reply_batch(batch, text, actions or [])

    async def _complete_reply(self, pending: _PendingTurn, text: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        async with pending.terminal_lock:
            if pending.terminal_started:
                return await self._completed_turn_result(pending)
            context = pending.context
            pending.terminal_started = True
            final_actions = self._consume_actions(pending, actions)
            frame = build_maid_agent_turn_complete_frame(
                turn_id=context.turn_id,
                maid_uuid=context.maid_uuid,
                outcome="reply",
                reply_text=text,
                tts_text=text,
                history_policy="append",
                actions=final_actions,
                agent_id=self._external_id(context),
                agent_name=self._external_name(context),
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
                "actions_count": len(final_actions),
            }
            if not pending.future.done():
                pending.future.set_result(result)
            self._unregister_pending(pending)
            return result

    async def _complete_no_reply(
        self,
        pending: _PendingTurn,
        *,
        reason: str,
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        async with pending.terminal_lock:
            if pending.terminal_started:
                return await self._completed_turn_result(pending)
            context = pending.context
            pending.terminal_started = True
            final_actions = self._consume_actions(pending, actions)
            try:
                await self._send_no_reply_frame(context, reason=reason, actions=final_actions)
            except Exception as exc:
                return self._fail_pending_completion(pending, "no_reply", exc)
            result = {
                "success": True,
                "external_message_id": context.turn_id,
                "outcome": "no_reply",
                "reason": reason,
                "actions_count": len(final_actions),
            }
            if not pending.future.done():
                pending.future.set_result(result)
            self._unregister_pending(pending)
            return result

    async def _send_context_no_reply(self, context: TurnContext, *, reason: str) -> dict[str, Any]:
        try:
            await self._send_no_reply_frame(context, reason=reason)
        except Exception as exc:
            error = f"MaidBridge 女仆回合 no_reply 发送失败：{exc}"
            self._ctx.logger.warning(
                f"MaidBridge 女仆回合 no_reply 发送失败 [turn_id={context.turn_id}, "
                f"trace_id={context.frame.trace_id}, reason={reason}, error={exc}]"
            )
            return {"success": False, "external_message_id": context.turn_id, "outcome": "no_reply", "error": error}
        return {"success": True, "external_message_id": context.turn_id, "outcome": "no_reply", "reason": reason}

    async def _send_no_reply_frame(
        self,
        context: TurnContext,
        *,
        reason: str,
        actions: list[dict[str, Any]] | None = None,
    ) -> None:
        frame = build_maid_agent_turn_complete_frame(
            turn_id=context.turn_id,
            maid_uuid=context.maid_uuid,
            outcome="no_reply",
            reason=reason,
            actions=actions or [],
            agent_id=self._external_id(context),
            agent_name=self._external_name(context),
            trace_id=context.frame.trace_id,
            reply_to=context.frame.request_id or context.frame.id,
            source_endpoint=context.frame.target_endpoint or DEFAULT_CLIENT_ENDPOINT_ID,
            target_endpoint=context.frame.source_endpoint or DEFAULT_JAVA_ENDPOINT_ID,
        )
        await self._send_frame(frame)

    def _consume_actions(self, pending: _PendingTurn, planned_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        actions = [dict(action) for action in pending.outbound_actions]
        pending.outbound_actions.clear()
        actions.extend(dict(action) for action in planned_actions)
        return actions

    def _fail_pending_completion(self, pending: _PendingTurn, outcome: str, exc: Exception) -> dict[str, Any]:
        error = f"MaidBridge 女仆回合终态帧发送失败：{exc}"
        self._ctx.logger.warning(
            f"MaidBridge 女仆回合终态帧发送失败 [turn_id={pending.context.turn_id}, "
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
        self._lease_by_session_id.clear()

    def _pending_batch_for_key(self, key: str) -> list[_PendingTurn]:
        normalized = str(key or "").strip()
        if not normalized:
            return []
        pending = self._pending_by_turn_id.get(normalized)
        if pending is not None:
            return [pending] if self._is_live_pending(pending) else []
        pending_list = self._pending_by_session_id.get(normalized)
        if pending_list is not None:
            return [pending for pending in pending_list if self._is_live_pending(pending)]
        return []

    def _request_batch_for_session(self, session_id: str) -> list[_PendingTurn]:
        lease = self._lease_for_session(session_id)
        if lease is not None:
            batch = [
                pending
                for turn_id in lease.turn_ids
                if (pending := self._pending_by_turn_id.get(turn_id)) is not None and self._is_live_pending(pending)
            ]
            if batch:
                return batch
        return self._pending_batch_for_key(session_id)

    def _target_pending_for_key(self, key: str) -> _PendingTurn | None:
        batch = self._request_batch_for_session(key)
        return batch[-1] if batch else None

    def _register_pending(self, pending: _PendingTurn) -> str:
        if pending.context.turn_id in self._pending_by_turn_id:
            return _PENDING_DUPLICATE_TURN
        if len(self._pending_by_turn_id) >= self._max_pending_turns():
            return _PENDING_QUEUE_FULL
        self._known_session_ids.add(pending.context.session_id)
        self._pending_by_turn_id[pending.context.turn_id] = pending
        self._pending_by_session_id.setdefault(pending.context.session_id, []).append(pending)
        self._start_dispatch(pending)
        return _PENDING_REGISTERED

    def _unregister_pending(self, pending: _PendingTurn) -> None:
        self._pending_by_turn_id.pop(pending.context.turn_id, None)
        self._remove_pending_from_index(self._pending_by_session_id, pending.context.session_id, pending)
        self._drop_stale_leases()

    def _remove_pending_from_index(
        self,
        index: dict[str, list[_PendingTurn]],
        key: str,
        pending: _PendingTurn,
    ) -> None:
        items = index.get(key)
        if not items:
            return
        retained = [item for item in items if item is not pending]
        if retained:
            index[key] = retained
        else:
            index.pop(key, None)

    def _is_live_pending(self, pending: _PendingTurn) -> bool:
        # route_message 的 accepted 会等入站链路返回；hook 必须在此之前就能拿到 pending。
        return (
            not pending.terminal_started
            and not pending.future.done()
            and self._pending_by_turn_id.get(pending.context.turn_id) is pending
        )

    def _lease_for_session(self, session_id: str) -> _TurnLease | None:
        normalized = str(session_id or "").strip()
        if not normalized:
            return None
        lease = self._lease_by_session_id.get(normalized)
        if lease is None:
            return None
        if any(turn_id in self._pending_by_turn_id for turn_id in lease.turn_ids):
            return lease
        self._lease_by_session_id.pop(normalized, None)
        return None

    def _drop_stale_leases(self) -> None:
        for session_id, lease in tuple(self._lease_by_session_id.items()):
            if not any(turn_id in self._pending_by_turn_id for turn_id in lease.turn_ids):
                self._lease_by_session_id.pop(session_id, None)

    def _context_for_batch(self, batch: list[_PendingTurn], *, include_reply_context: bool) -> dict[str, Any]:
        target = batch[-1]
        context = (
            build_reply_generation_context(target.context)
            if include_reply_context
            else build_planner_context(target.context)
        )
        if len(batch) <= 1:
            return context
        context["pending_turn_count"] = len(batch)
        context["target_turn_id"] = target.context.turn_id
        context["batch_policy"] = "reply_latest_turn_and_no_reply_earlier_turns"
        context["pending_turns"] = [_batch_item(pending.context) for pending in batch]
        return context

    async def _complete_reply_batch(
        self,
        batch: list[_PendingTurn],
        text: str,
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        prior = batch[:-1]
        target = batch[-1]
        for pending in prior:
            await self._complete_no_reply(pending, reason=_BATCH_MERGED_REASON, actions=[])
        result = await self._complete_reply(target, text, actions)
        if result.get("success"):
            result["batched_turn_count"] = len(batch)
        return result

    async def _complete_no_reply_batch(
        self,
        batch: list[_PendingTurn],
        *,
        reason: str,
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        prior = batch[:-1]
        target = batch[-1]
        for pending in prior:
            await self._complete_no_reply(pending, reason=_BATCH_MERGED_REASON, actions=[])
        result = await self._complete_no_reply(target, reason=reason, actions=actions)
        if result.get("success"):
            result["batched_turn_count"] = len(batch)
        return result

    async def _completed_turn_result(self, pending: _PendingTurn) -> dict[str, Any]:
        result = await pending.future
        if result.get("success") is False:
            return {"success": False, "error": str(result.get("error") or "MaidBridge 回合完成失败")}
        return {"success": True, "external_message_id": pending.context.turn_id}

    def _start_dispatch(self, pending: _PendingTurn) -> None:
        if pending.future.done():
            return
        task = asyncio.create_task(self._dispatch_to_maibot(pending))
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    async def _dispatch_to_maibot(self, pending: _PendingTurn) -> None:
        context = pending.context
        try:
            self._ctx.logger.info(
                f"MaidBridge 正在注入 MaiBot 消息循环 [turn_id={context.turn_id}, "
                f"session_id={context.session_id}, scope={context.scope}]"
            )
            accepted = await self._ctx.gateway.route_message(
                gateway_name=self._gateway_name,
                message=build_maibot_message(context),
                route_metadata=build_route_metadata(context),
                external_message_id=context.turn_id,
                dedupe_key=context.turn_id,
            )
            self._ctx.logger.info(
                f"MaidBridge MaiBot 消息循环注入结果 [turn_id={context.turn_id}, "
                f"session_id={context.session_id}, accepted={accepted}]"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not pending.future.done():
                self._ctx.logger.warning(f"MaidBridge 注入 MaiBot gateway 失败，按 no_reply 回写 Java [error={exc}]")
                await self._complete_no_reply(pending, reason="maibot_gateway_error", actions=[])
            return
        if pending.future.done():
            return
        if not accepted:
            self._ctx.logger.warning("MaidBridge 女仆回合被 MaiBot gateway 拒绝，按 no_reply 回写 Java")
            await self._complete_no_reply(pending, reason="maibot_gateway_rejected", actions=[])

    def _external_id(self, context: TurnContext) -> str:
        return _first_non_blank(self._settings.agent_id, context.bot_name)

    def _external_name(self, context: TurnContext) -> str:
        return _first_non_blank(context.bot_name, self._settings.agent_id)

    def _max_pending_turns(self) -> int:
        return max(1, int(self._settings.max_pending_maid_agent_turns))


def _batch_item(context: TurnContext) -> dict[str, Any]:
    return {
        "turn_id": context.turn_id,
        "speaker": {
            "uuid": context.speaker_uuid,
            "name": context.speaker_name,
        },
        "message": {
            "text": context.text,
        },
    }


def _outbound_turn_id(message: Mapping[str, Any], route: Mapping[str, Any]) -> str:
    additional = _message_additional_config(message)
    return _first_non_blank(
        route.get("maidbridge_turn_id"),
        additional.get("maidbridge_turn_id"),
        message.get("maidbridge_turn_id"),
    )


def _message_additional_config(message: Mapping[str, Any]) -> dict[str, Any]:
    message_info = message.get("message_info")
    if not isinstance(message_info, Mapping):
        return {}
    additional_config = message_info.get("additional_config")
    return dict(additional_config) if isinstance(additional_config, Mapping) else {}


def _first_non_blank(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
